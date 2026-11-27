"""Reproduce the local workflow and two public development comparisons using the real Apple PDF."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from forgeflow import apple_sales, research_store
from forgeflow.contracts import canonical, digest
from forgeflow.report_desk import SOURCES, ReportDesk, mount_report_desk


def compare_formats(question, text, html, data):
    calc = data["payload"]["evidence"]["calculation"]
    amounts = [*calc["services"], *calc["total_sales"]]
    files = {"text": text, "html": html, "json": canonical(data)}
    out = {}
    for name, content in files.items():
        normal = content.replace(",", "")
        out[name] = {"utf8_bytes": len(content.encode()), "has_question": question in content,
                     "has_full_pdf_sha": data["payload"]["evidence"]["source"]["sha256"] in content,
                     "has_all_four_amounts": all(str(n) in normal for n in amounts),
                     "has_revision_digest": data["digest"] in content, "model_calls": 0}
    return out


def compare_binding(first, second, sibling):
    # Four candidate checks on identical requests and facts, without modifying any approved revision.
    inputs = [("原版", first, "reviewer", "submitted"), ("同稿新版", second, "reviewer", "submitted"),
              ("另一份第一版", sibling, "reviewer", "submitted"), ("作者操作", first, "author", "submitted"),
              ("尚未送审", first, "reviewer", "draft")]
    results = {}
    for name, v, role, state in inputs:
        results[name] = {"document_id": first["id"] == v["id"], "version_number": first["version"] == v["version"],
                         "content_digest": first["head"] == v["head"],
                         "assigned_reviewer_head_state": (first["id"] == v["id"] and first["head"] == v["head"]
                                                          and role == "reviewer" and state == "submitted")}
    return results


def run(reports, output, cache):
    report = next(r for r in json.loads(SOURCES.read_text())["reports"] if r["key"] == apple_sales.DOCUMENT)
    output.mkdir(parents=True, exist_ok=True)
    traces = []
    query_refs = {}
    with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"FORGEFLOW_RESEARCH_DIR": temp + "/private"}):
        root = Path(temp)
        catalogue = root / "sources.json"
        catalogue.write_text(json.dumps({"reports": [report]}))
        if cache:
            cached = report["sha256"] + ".json"
            (root / "ingest-cache").mkdir()
            shutil.copyfile(cache / cached, root / "ingest-cache" / cached)
        desk = ReportDesk(reports, root, sources=catalogue, ollama_url="http://127.0.0.1:1")
        desk.load()
        if desk.reports[0]["state"] != "ok":
            raise RuntimeError("需要登记指纹对应的 Apple FY2025 PDF。")
        app = FastAPI()
        mount_report_desk(app, desk)
        users = {name: {"Authorization": "Bearer " + research_store.issue(root / "private", name, role)}
                 for name, role in [("dev-author", "author"), ("dev-reviewer", "reviewer")]}
        def post(path, body, who=None, expected=200):
            start = time.perf_counter()
            res = c.post(path, json=body, headers=users.get(who))
            assert res.status_code == expected, res.text
            data = res.json()
            if data.get("research_grant"):
                query_refs[data["research_grant"]] = data["run_id"]
            # The archived public input can be replayed by resolving the fresh grant for this logical source run.
            params = {k: v for k, v in body.items() if k != "grant_id"}
            if "grant_id" in body:
                params["source_run_id"] = query_refs[body["grant_id"]]
            traces.append({"seq": len(traces) + 1, "path": path, "actor": who, "params": params,
                           "http": res.status_code, "result_digest": data.get("head"), "run_id": data.get("run_id"),
                           "elapsed_ms": round((time.perf_counter()-start)*1000, 2)})
            return data

        with TestClient(app, client=("127.0.0.1", 40000)) as c:
            author, reviewer = "dev-author", "dev-reviewer"
            question = "Apple FY2025 服务收入增速和增长贡献"
            ask = post("/reports/ask", {"question": question}, author)
            new = {"grant_id": ask["research_grant"], "note": "经营分析会：服务收入同比增长多少，拉动公司收入增量多少？"}
            first = post("/research/drafts", new, author)
            sibling = post("/research/drafts", new, author)
            path = "/research/drafts/" + first["id"]
            def export(version, fmt):
                r = c.get(f"{path}/versions/{version}/export/{fmt}", headers=users[author])
                r.raise_for_status()
                return r
            initial = export(1, "json").json()
            formats = compare_formats(question, ask["answer"], export(1, "html").text, initial)
            action = path + "/decisions"
            post(action, {"expected": first["head"], "action": "submit", "reviewer": reviewer}, author)
            post(action, {"expected": first["head"], "action": "approve"}, author, 403)
            post(action, {"expected": first["head"], "action": "return", "memo": "会议只要增量贡献，请保留分母及原页。"}, reviewer)
            focused = post("/reports/ask", {"question": "Apple FY2025 服务收入增长贡献"}, author)
            second = post(path + "/revisions", {"grant_id": focused["research_grant"], "expected": first["head"],
                          "note": "按审阅意见仅保留贡献。分母取全公司收入增量，金额单位为百万美元。"}, author)
            post(action, {"expected": second["head"], "action": "submit", "reviewer": reviewer}, author)
            post(action, {"expected": first["head"], "action": "approve"}, reviewer, 409)
            post(action, {"expected": second["head"], "action": "approve"}, reviewer)
            delivered = post(action, {"expected": second["head"], "action": "deliver"}, author)
            assert post(action, {"expected": second["head"], "action": "deliver"}, author) == delivered
            assert sum(e["kind"] == "delivered" for e in delivered["events"]) == 1
            machine = export(2, "json")
            data = machine.json()
            assert digest(data["payload"]) == data["digest"] == second["head"]
            assert data["payload"]["parent_digest"] == initial["digest"] and data["local_receipt"]
            assert data["payload"]["evidence"]["calculation"]["operations"][0]["percent"] == "51.70"
            refs = []
            for name, content in [("apple-review-v1.json", export(1, "json").content), ("apple-review-v2.json", machine.content),
                                  ("apple-review-v2.html", export(2, "html").content)]:
                (output / name).write_bytes(content)
                refs.append({"file": name, "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)})
        fresh = FastAPI()
        mount_report_desk(fresh, desk)
        with TestClient(fresh, client=("127.0.0.1", 40001)) as c:
            assert export(2, "json").content == machine.content
        return {"schema_version": 1, "case_id": "apple-services-growth-contribution-dev", "split": "public-dev",
                "draft_id": first["id"],
                "versions": [{"version": v["version"], "revision_digest": v["head"],
                              "run_id": v["payload"]["evidence"]["run_id"]} for v in (first, second)],
                "roles": {"dev-author": "scripted local author", "dev-reviewer": "scripted local reviewer"},
                "tool": apple_sales.VERSION, "model": None, "external_cost": 0,
                "driver": "real FastAPI ASGI + SQLite, scripted roles, verified public PDF",
                "parse": "reused local parser output" if cache else "fresh PDF parse", "calls": traces,
                "tool_run_ids": [ask["run_id"], focused["run_id"]], "format_comparison": formats,
                "approval_comparison": compare_binding(first, second, sibling), "artifact_refs": refs,
                "same_bytes_on_restart": True, "local_receipt": data["local_receipt"], "model_calls": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--cache", type=Path, help="可选。复用本机以 PDF 指纹命名的解析缓存。")
    args = parser.parse_args()
    data = run(args.reports, args.output, args.cache)
    (args.output / "review-dev.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
