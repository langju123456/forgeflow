"""Measure forgeflow.pdf_ingest against the synthetic financial-PDF ground truth.

    .venv/bin/python preparation/s3/pdf-ingest/evaluate.py [--keep DIR] [--report PATH]

Generates the fixture PDFs from fixtures/financial-pdf/ground_truth.json (scripts/make_financial_pdfs.py),
ingests them, and scores table detection, cell values, units, cross-page stitching, page/bbox citations
and the OCR flag against the truth and the generator's drawn positions. It then writes every row into a
private throwaway SQLite file through the production store class and searches it with the existing
KnowledgeService. Nothing here reads runtime credentials or a live database.

The fixtures are synthetic and drawn by code from the same author as the parser: these numbers show the
parser is consistent with its own fixture family, not how it performs on real annual reports.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import platform
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
from decimal import Decimal
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
TRUTH = ROOT / "fixtures/financial-pdf/ground_truth.json"
GENERATOR = ROOT / "scripts/make_financial_pdfs.py"
sys.path.insert(0, str(ROOT / "src"))

from forgeflow import contracts  # noqa: E402
from forgeflow.context import KnowledgeService  # noqa: E402
from forgeflow.contracts import Principal, Scope  # noqa: E402
from forgeflow.evt_store import EVTBoundStore  # noqa: E402
from forgeflow.pdf_ingest import ingest_into_store, ingest_pdf, parse_source_line  # noqa: E402

SCOPE = Scope(tenant_id="eval-fin", workspace_id="pdf", project_id="annual-report")
WRITER = Principal(subject_id="document-admin", kind="user", roles=frozenset({"admin", "finance"}),
                   scopes=(SCOPE,), clearance=3)
READER = Principal(subject_id="finance-agent", kind="agent", roles=frozenset({"finance"}), scopes=(SCOPE,),
                   clearance=1)
NO_ROLE = Principal(subject_id="pm-agent", kind="agent", roles=frozenset({"pm"}), scopes=(SCOPE,), clearance=3)
LOW_CLEARANCE = Principal(subject_id="intern-agent", kind="agent", roles=frozenset({"finance"}), scopes=(SCOPE,),
                          clearance=0)
TABLE_IOU, CELL_PAD, TEXT_PAD = 0.9, 0.5, 2.0


def load_generator():
    spec = importlib.util.spec_from_file_location("make_financial_pdfs", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def norm(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or ""))


def iou(a, b) -> float:
    x0, top, x1, bottom = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x1 - x0) * max(0.0, bottom - top)
    area = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / area if area else 0.0


def inside(box, area, pad) -> bool:
    return (box[0] >= area[0] - pad and box[1] >= area[1] - pad and box[2] <= area[2] + pad
            and box[3] <= area[3] + pad)


def center_in(box, area, pad) -> bool:
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return area[0] - pad <= cx <= area[2] + pad and area[1] - pad <= cy <= area[3] + pad


def expected_cell(generator, table: dict, row: dict, cell: str, column: int) -> dict:
    kind = generator.classify(cell)
    if kind in {"blank", "empty_marker"}:
        return {"kind": "empty"}
    if kind == "text" or column in table.get("text_columns", []):  # note references (附注 11) are not amounts
        return {"kind": "text", "raw": cell}
    if kind == "percent":
        return {"kind": "percent", "base": Decimal(cell[:-1]) / 100}
    if column in table.get("percent_columns", []):  # header says (%), cells carry no % sign
        return {"kind": "percent", "base": Decimal(cell) / 100}
    scale = Decimal((row.get("unit") or table["unit"])["scale"])
    return {"kind": "number", "base": Decimal(cell) * scale}


def ratio(hit: int, total: int) -> dict:
    return {"hit": hit, "total": total, "rate": round(hit / total, 4) if total else None}


def subset(truth: dict, name: str) -> dict:
    documents = [d for d in truth["documents"] if d.get("set", "base") == name]
    files = {d["file"] for d in documents}
    tables = {k: v for k, v in truth["tables"].items() if v["file"] in files}
    paragraphs = {b["id"] for d in documents for p in d["pages"] for b in p["blocks"] if b["type"] == "paragraph"}
    return {**truth, "documents": documents, "tables": tables,
            "not_stitched": [c for c in truth["not_stitched"] if c["from"] in tables],
            "needs_ocr": [c for c in truth["needs_ocr"] if c["file"] in files],
            "queries": [q for q in truth["queries"] if q["target"] in tables or q["target"] in paragraphs]}


def evaluate(work: Path) -> dict:
    generator = load_generator()
    truth = json.loads(TRUTH.read_text(encoding="utf-8"))
    layout = generator.generate(work, TRUTH)
    sets = {}
    for name in truth["sets"]:
        sets[name] = score(generator, subset(truth, name), layout, work, name)
    return {
        "notice": "Synthetic fixtures generated by the same author's code; not a benchmark on real annual reports.",
        "measured_at": contracts.utcnow().isoformat(),
        "environment": {"python": platform.python_version(),
                        **{name: metadata.version(name) for name in ("pdfplumber", "pdfminer.six", "reportlab",
                                                                     "pillow")}},
        "code": {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
                 for path in ("src/forgeflow/pdf_ingest.py", "scripts/make_financial_pdfs.py",
                              "fixtures/financial-pdf/ground_truth.json", "preparation/s3/pdf-ingest/evaluate.py")},
        "git_head": _git_head(),
        "fixtures": layout["files"],
        "sets": sets,
    }


def score(generator, truth: dict, layout: dict, work: Path, label: str) -> dict:
    import pdfplumber

    results, timings = {}, {}
    for document in truth["documents"]:
        path = work / document["file"]
        started = time.perf_counter()
        results[document["file"]] = ingest_pdf(path, scope=SCOPE, roles=["finance"], classification=1)
        timings[document["file"]] = {"ms": round((time.perf_counter() - started) * 1000, 1),
                                     "pages": len(document["pages"])}
    misses: list[str] = []

    # ---- table detection: a truth table is found when a detected table's first segment overlaps part 0
    detected = [(file, table) for file, result in results.items() for table in result.tables]
    matched: dict[str, dict] = {}
    used: set[str] = set()
    for name, table in truth["tables"].items():
        part = layout["tables"][name]["parts"][0]
        best = max(((iou(t["segments"][0]["bbox"], part["bbox"]), t) for f, t in detected
                    if f == table["file"] and t["segments"][0]["page"] == part["page"] and t["id"] not in used),
                   default=(0.0, None), key=lambda v: v[0])
        if best[1] is not None and best[0] >= 0.5:
            matched[name] = best[1]
            used.add(best[1]["id"])
        else:
            misses.append(f"table {name}: not detected on page {part['page']}")
    false_tables = [t["id"] for _, t in detected if t["id"] not in used]

    # ---- cross-page stitching (positives) and the negative that must stay two tables
    stitch_expected = [n for n, t in truth["tables"].items() if len(t["parts"]) > 1]
    stitch_ok, stitch_details = 0, {}
    for name in stitch_expected:
        table, found = truth["tables"][name], matched.get(name)
        parts = layout["tables"][name]["parts"]
        repeated = len(table["header_rows"]) * (len(parts) - 1) if table.get("repeat_header") else 0
        ok = bool(found) and len(found["segments"]) == len(parts) and all(
            s["page"] == p["page"] and iou(s["bbox"], p["bbox"]) >= TABLE_IOU
            for s, p in zip(found["segments"], parts, strict=False)) and len(found["rows"]) == len(table["rows"]) \
            and found["dropped_repeated_header_rows"] == repeated
        stitch_details[name] = {"segments": len(found["segments"]) if found else 0, "expected_segments": len(parts),
                                "rows": len(found["rows"]) if found else 0, "expected_rows": len(table["rows"]),
                                "dropped_repeated_header_rows": found["dropped_repeated_header_rows"] if found else None,
                                "expected_dropped": repeated}
        stitch_ok += ok
        if not ok:
            misses.append(f"stitch {name}: {stitch_details[name]}")
    negatives_ok = 0
    for case in truth["not_stitched"]:
        a, b = matched.get(case["from"]), matched.get(case["to"])
        ok = bool(a and b) and a["id"] != b["id"] and len(a["segments"]) == len(
            layout["tables"][case["from"]]["parts"])
        negatives_ok += ok
        if not ok:
            misses.append(f"false stitch {case['from']} -> {case['to']}")
    over_stitched = sum(1 for name, t in matched.items()
                        if len(t["segments"]) > len(layout["tables"][name]["parts"]))

    # ---- cells, labels, column keys, units
    counts = {k: [0, 0] for k in ("number", "percent", "empty", "text", "raw_kept", "labels", "sections",
                                  "column_keys", "units", "row_unit_overrides", "cell_bbox", "table_bbox")}
    table_ious: list[float] = []
    for name, table in truth["tables"].items():
        found = matched.get(name)
        parts = layout["tables"][name]["parts"]
        counts["units"][1] += 1
        counts["column_keys"][1] += len(table["column_keys"])
        for index, part in enumerate(parts):
            counts["table_bbox"][1] += 1
            if found and index < len(found["segments"]):
                segment = found["segments"][index]
                value = iou(segment["bbox"], part["bbox"]) if segment["page"] == part["page"] else 0.0
                table_ious.append(round(value, 4))
                counts["table_bbox"][0] += value >= TABLE_IOU
        if not found:
            continue
        unit = found["unit"] or {}
        want = table["unit"]
        if (unit.get("scale") is not None and Decimal(unit["scale"]) == Decimal(want["scale"])
                and unit.get("currency") == want["currency"]
                and bool(unit.get("except_per_share")) == bool(want.get("except_per_share"))):
            counts["units"][0] += 1
        else:
            misses.append(f"unit {name}: got {unit} want {want}")
        keys = [c["key"] for c in found["columns"]]
        for j, key in enumerate(table["column_keys"]):
            hit = j < len(keys) and norm(keys[j]) == norm(key)
            counts["column_keys"][0] += hit
            if not hit:
                misses.append(f"column {name}[{j}]: got {keys[j] if j < len(keys) else None!r} want {key!r}")
        remaining = list(found["rows"])
        cell_boxes = {k: (p["page"], v) for p in parts for k, v in p["cells"].items()}
        for index, row in enumerate(table["rows"]):
            bucket = "sections" if row.get("section") else "labels"
            counts[bucket][1] += 1
            got = next((r for r in remaining if norm(r["label"]) == norm(row["label"])), None)
            if got is None:
                misses.append(f"row {name}: label {row['label']!r} not found")
                for j, cell in enumerate(row["cells"], start=1):
                    if not row.get("section"):
                        counts[expected_cell(generator, table, row, cell, j)["kind"]][1] += 1
                continue
            remaining.remove(got)
            counts[bucket][0] += 1
            if row.get("section"):
                continue
            style = row.get("negative_style", table["negative_style"])
            if style == "minus" and table["file"].endswith("_zh.pdf"):
                style = "fullwidth_minus"
            if row.get("unit"):
                counts["row_unit_overrides"][1] += 1
                scales = {c["scale"] for c in got["cells"] if c["kind"] == "number"}
                counts["row_unit_overrides"][0] += scales == {format(Decimal(row["unit"]["scale"]), "f")}
            for j, cell in enumerate(row["cells"], start=1):
                want_cell = expected_cell(generator, table, row, cell, j)
                counts[want_cell["kind"]][1] += 1
                have = next((c for c in got["cells"] if c["col"] == j), None)
                if have is None:
                    misses.append(f"cell {name} {row['label']} c{j}: missing")
                    continue
                if want_cell["kind"] in {"number", "percent"}:
                    ok = have["kind"] == want_cell["kind"] and have["base_value"] is not None and Decimal(
                        have["base_value"]) == want_cell["base"]
                elif want_cell["kind"] == "text":
                    ok = have["kind"] == "text" and norm(have["raw"]) == norm(cell)
                else:
                    ok = have["kind"] == "empty"
                counts[want_cell["kind"]][0] += ok
                if not ok:
                    misses.append(f"cell {name} {row['label']} c{j}: got {have['kind']} {have['raw']!r} "
                                  f"{have['base_value']} want {want_cell}")
                printed = generator.format_cell(cell, style)
                if printed:
                    counts["raw_kept"][1] += 1
                    dollar = "$" if row.get("dollar") and want_cell["kind"] == "number" else ""
                    counts["raw_kept"][0] += norm(have["raw"]) == norm(dollar + printed)
                if want_cell["kind"] in {"number", "percent"}:
                    counts["cell_bbox"][1] += 1
                    page, rect = cell_boxes[f"r{index}-c{j}"]
                    ok_box = have["bbox"] is not None and have["page"] == page and center_in(have["bbox"], rect,
                                                                                              CELL_PAD)
                    counts["cell_bbox"][0] += ok_box
                    if not ok_box:
                        misses.append(f"cell bbox {name} {row['label']} c{j}: {have['page']} {have['bbox']}")
        unexpected = [r["label"] for r in remaining]
        if unexpected:
            misses.append(f"table {name}: extra rows {unexpected}")

    # ---- text chunks: each truth paragraph inside one chunk on the right page with a covering bbox
    chunks = [(file, row) for file, result in results.items() for row in result.rows if row["kind"] == "text"]
    paragraph_hits, paragraph_cites, paragraph_total = 0, 0, 0
    paragraph_rows: dict[str, str] = {}
    for document in truth["documents"]:
        for page in document["pages"]:
            for block in page["blocks"]:
                if block["type"] != "paragraph":
                    continue
                paragraph_total += 1
                where = layout["paragraphs"][block["id"]]
                owner = next((row for file, row in chunks if file == document["file"]
                              and norm(block["text"]) in norm(row["content"].split("\n", 1)[1])), None)
                if owner is None:
                    misses.append(f"paragraph {block['id']}: not found in any chunk")
                    continue
                paragraph_hits += 1
                paragraph_rows[block["id"]] = owner["id"]
                cite = owner["citation"]
                ok = cite["page"] == where["page"] and inside(where["bbox"], cite["bbox"], TEXT_PAD)
                paragraph_cites += ok
                if not ok:
                    misses.append(f"paragraph {block['id']}: cited p{cite['page']} {cite['bbox']} "
                                  f"drawn p{where['page']} {where['bbox']}")
    furniture = {norm(f["text"]) for f in layout["furniture"]}
    leaks = [row["id"] for _, row in chunks if any(f and f in norm(row["content"]) for f in furniture)]

    # ---- citation round trip, independent of the generator: re-crop each cited bbox and compare its text.
    # Text rows: the chunk bbox. Table rows: every label bbox and every non-empty cell bbox, so a wrapped
    # label or a value is checked against exactly the area it cites.
    crop_ok, crop_total, crop_cells_ok, crop_cells_total = 0, 0, 0, 0
    for document in truth["documents"]:
        result = results[document["file"]]
        with pdfplumber.open(work / document["file"]) as pdf:
            def crop(page_number: int, bbox, pdf=pdf) -> str:
                page = pdf.pages[page_number - 1]
                x0, top, x1, bottom = bbox
                box = (max(0, x0 - 1), max(0, top - 1), min(page.width, x1 + 1), min(page.height, bottom + 1))
                return norm(page.crop(box).extract_text() or "")

            for row in result.rows:
                crop_total += 1
                if row["kind"] == "text":
                    ok = norm(row["content"].split("\n", 1)[1]) == crop(row["citation"]["page"],
                                                                        row["citation"]["bbox"])
                else:
                    ok = True
                    for r in row["table"]["rows"]:
                        areas = [(r["label"], r["label_bbox"])] + [(c["raw"], c["bbox"]) for c in r["cells"]
                                                                   if c["raw"].strip()]
                        for text, bbox in areas:
                            crop_cells_total += 1
                            good = bbox is not None and crop(r["page"], bbox) == norm(text)
                            crop_cells_ok += good
                            ok = ok and good
                crop_ok += ok
                if not ok:
                    misses.append(f"crop {row['id']}: cited bbox text does not match row content")

    # ---- OCR flag
    want_ocr = {(c["file"], c["page"]) for c in truth["needs_ocr"]}
    got_ocr = {(file, page) for file, result in results.items() for page in result.needs_ocr}
    ocr_hit = len(want_ocr & got_ocr)
    ocr_false = sorted(got_ocr - want_ocr)
    pages_total = sum(len(d["pages"]) for d in truth["documents"])

    # ---- rows: ACL fields and hashes
    all_rows = [row for result in results.values() for row in result.rows]
    acl_fields = ("tenant_id", "workspace_id", "project_id", "roles", "classification", "valid_until", "version")
    acl_ok = sum(all(field in row for field in acl_fields) and row["roles"] == ["finance"]
                 and row["classification"] == 1 for row in all_rows)
    hash_ok = sum(hashlib.sha256(row["content"].encode()).hexdigest() == row["source_hash"] for row in all_rows)
    source_ok = sum(parse_source_line(row["content"]) is not None and [
        (p["page"], [round(v, 1) for v in p["bbox"]]) for p in parse_source_line(row["content"])["pages"]] == [
        (p["page"], [round(v, 1) for v in p["bbox"]]) for p in row["citation"]["pages"]] for row in all_rows)

    # ---- Store + KnowledgeService end to end (private throwaway SQLite file)
    store = EVTBoundStore(f"sqlite:///{work / f'pdf-eval-{label}.sqlite'}")
    store.create_schema()
    receipts = {}
    started = time.perf_counter()
    for result in results.values():
        for row, receipt in zip(result.rows, ingest_into_store(store, WRITER, SCOPE, result), strict=True):
            receipts[row["id"]] = receipt
    write_ms = round((time.perf_counter() - started) * 1000, 1)
    stored_hash_ok = sum(receipts[row["id"]]["source_hash"] == row["source_hash"] for row in all_rows)
    cite_ok = 0
    for row in all_rows:
        cited = store.cite(READER, SCOPE, "document", receipts[row["id"]]["citation_id"])
        cite_ok += cited["content"] == row["content"] and cited["source_hash"] == row["source_hash"] and cited[
            "status"] == "CURRENT"
    again = {}
    for result in results.values():
        for row, receipt in zip(result.rows, ingest_into_store(store, WRITER, SCOPE, result), strict=True):
            again[row["id"]] = receipt
    idempotent = sum(again[k]["citation_id"] == receipts[k]["citation_id"] for k in receipts)
    target_rows = {name: t["id"] for name, t in matched.items()} | paragraph_rows
    service = KnowledgeService(store)
    query_results, top1, top5, hit_cites = [], 0, 0, 0
    latencies = []
    for query in truth["queries"]:
        started = time.perf_counter()
        hits = service.search(READER, SCOPE, query["query"], top_k=5)
        latencies.append((time.perf_counter() - started) * 1000)
        ids = [h.id for h in hits]
        target = target_rows.get(query["target"])
        rank = ids.index(target) + 1 if target in ids else None
        top1 += rank == 1
        top5 += rank is not None
        if rank is not None:
            hit = hits[rank - 1]
            parsed = parse_source_line(hit.content)
            row = next(r for r in all_rows if r["id"] == hit.id)
            hit_cites += bool(parsed) and parsed["file"] == row["citation"]["file"] and [
                p["page"] for p in parsed["pages"]] == [p["page"] for p in row["citation"]["pages"]] and \
                hit.source_hash == row["source_hash"]
        else:
            misses.append(f"query {query['query']!r}: target {query['target']} not in top 5 {ids}")
        query_results.append({"query": query["query"], "target": query["target"], "rank": rank, "top5": ids})
    leak_rows = len(store.document_rows(NO_ROLE, SCOPE)) + len(store.document_rows(LOW_CLEARANCE, SCOPE))
    leak_hits = sum(len(service.search(p, SCOPE, q["query"], top_k=5)) for p in (NO_ROLE, LOW_CLEARANCE)
                    for q in truth["queries"])
    visible = len(store.document_rows(READER, SCOPE))
    latencies.sort()

    return {
        "files": sorted(results),
        "timings": {"ingest": timings, "store_write_ms": write_ms,
                    "search_median_ms": round(latencies[len(latencies) // 2], 3), "search_max_ms": round(
                        latencies[-1], 3), "searches": len(latencies)},
        "metrics": {
            "tables_found": ratio(len(matched), len(truth["tables"])),
            "false_tables": false_tables,
            "numeric_cells_exact": ratio(counts["number"][0] + counts["percent"][0],
                                         counts["number"][1] + counts["percent"][1]),
            "number_cells_exact": ratio(*counts["number"]),
            "percent_cells_exact": ratio(*counts["percent"]),
            "empty_cells_kind": ratio(*counts["empty"]),
            "text_cells_kept": ratio(*counts["text"]),
            "raw_string_kept": ratio(*counts["raw_kept"]),
            "row_labels": ratio(*counts["labels"]),
            "section_rows": ratio(*counts["sections"]),
            "column_keys": ratio(*counts["column_keys"]),
            "table_units": ratio(*counts["units"]),
            "row_unit_overrides": ratio(*counts["row_unit_overrides"]),
            "stitched_tables": ratio(stitch_ok, len(stitch_expected)),
            "stitch_details": stitch_details,
            "not_stitched_negatives": ratio(negatives_ok, len(truth["not_stitched"])),
            "over_stitched": over_stitched,
            "table_segment_bbox_iou_ge_0.9": ratio(*counts["table_bbox"]),
            "table_segment_ious": table_ious,
            "cell_bbox_center_in_drawn_cell": ratio(*counts["cell_bbox"]),
            "paragraphs_found": ratio(paragraph_hits, paragraph_total),
            "paragraph_citation_page_and_bbox": ratio(paragraph_cites, paragraph_total),
            "furniture_leaks_in_chunks": {"leaks": leaks, "chunks": len(chunks)},
            "crop_round_trip_rows": ratio(crop_ok, crop_total),
            "crop_round_trip_table_cells": ratio(crop_cells_ok, crop_cells_total),
            "ocr_pages_flagged": ratio(ocr_hit, len(want_ocr)),
            "ocr_false_flags": {"pages": ocr_false, "text_pages": pages_total - len(want_ocr)},
            "rows_with_acl_fields": ratio(acl_ok, len(all_rows)),
            "rows_source_hash_matches_content": ratio(hash_ok, len(all_rows)),
            "rows_source_line_matches_citation": ratio(source_ok, len(all_rows)),
            "store_receipt_hash_equal": ratio(stored_hash_ok, len(all_rows)),
            "store_cite_exact_current": ratio(cite_ok, len(all_rows)),
            "reingest_idempotent": ratio(idempotent, len(receipts)),
            "reader_visible_rows": visible,
            "query_target_top1": ratio(top1, len(truth["queries"])),
            "query_target_top5": ratio(top5, len(truth["queries"])),
            "hit_citation_recoverable": ratio(hit_cites, top5),
            "acl_negative_rows_and_hits": {"rows": leak_rows, "hits": leak_hits,
                                           "queries": 2 * len(truth["queries"])},
        },
        "rows": {"total": len(all_rows), "tables": sum(r["kind"] == "table" for r in all_rows),
                 "text": sum(r["kind"] == "text" for r in all_rows)},
        "queries": query_results,
        "warnings": {file: result.warnings for file, result in results.items()},
        "misses": misses,
    }


def _git_head() -> str | None:
    done = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=False)
    return done.stdout.strip() or None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--keep", help="write the PDFs, layout.json and SQLite file here instead of a temp dir")
    parser.add_argument("--report", default=str(HERE / "report.json"))
    args = parser.parse_args(argv)
    if args.keep:
        report = evaluate(Path(args.keep))
    else:
        with tempfile.TemporaryDirectory(prefix="forgeflow-pdf-eval-") as folder:
            report = evaluate(Path(folder))
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for name, result in report["sets"].items():
        line = {k: (f"{v['hit']}/{v['total']}" if isinstance(v, dict) and "hit" in v else v)
                for k, v in result["metrics"].items() if k not in {"stitch_details", "table_segment_ious"}}
        print(json.dumps({"set": name, "metrics": line, "misses": result["misses"],
                          "warnings": result["warnings"]}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
