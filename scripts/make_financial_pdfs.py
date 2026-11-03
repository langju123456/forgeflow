"""Write synthetic financial PDFs for a fictional company from one ground-truth JSON.

    python scripts/make_financial_pdfs.py --out /tmp/financial-pdfs

Reads ``fixtures/financial-pdf/ground_truth.json`` (or ``--truth``) and writes one PDF per document plus
``layout.json``: where each table part, value cell, paragraph and scanned page was drawn, in pdfplumber
coordinates (x0, top, x1, bottom from the page's top-left corner). Every number comes from the JSON; the
layout file only records positions so an evaluator can check page/bbox citations.

The output is deterministic for a given reportlab/Pillow version (``invariant=1``: no timestamps or random
document IDs). Needs reportlab and Pillow: ``pip install 'forgeflow[pdf-fixtures]'``. The fixtures are
synthetic and drawn by code from the same author as the parser; they are not real annual reports.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TRUTH = ROOT / "fixtures/financial-pdf/ground_truth.json"
LEFT, RIGHT = 60.0, 535.0
ZH_FONT = "STSong-Light"
EN_FONT, EN_BOLD = "Helvetica", "Helvetica-Bold"
SECTION_TOTAL = re.compile(r"^Total\b")


def classify(cell: str) -> str:
    """Cell grammar of the ground truth (see ``cell_grammar`` in the JSON)."""
    if cell == "":
        return "blank"
    if cell in {"—", "-", "–"}:
        return "empty_marker"
    if re.fullmatch(r"-?\d+(?:\.\d+)?%", cell):
        return "percent"
    if re.fullmatch(r"-?\d+(?:\.\d+)?", cell):
        return "number"
    return "text"


def format_cell(cell: str, negative_style: str = "paren") -> str:
    kind = classify(cell)
    if kind in {"blank", "empty_marker", "text"}:
        return cell
    percent = kind == "percent"
    body = cell[:-1] if percent else cell
    negative = body.startswith("-")
    whole, _, fraction = body.lstrip("-").partition(".")
    grouped = f"{int(whole):,}" + (f".{fraction}" if fraction else "")
    if negative:
        if negative_style == "paren":
            return f"({grouped})%" if percent else f"({grouped})"
        sign = "－" if negative_style == "fullwidth_minus" else "-"
        grouped = sign + grouped
    return grouped + ("%" if percent else "")


class _Doc:
    def __init__(self, path: Path, document: dict, tables: dict, layout: dict):
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.pdfgen import canvas

        if ZH_FONT not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(UnicodeCIDFont(ZH_FONT))
        self.metrics = pdfmetrics
        self.width, self.height = A4
        self.doc, self.tables, self.layout = document, tables, layout
        self.zh = document["lang"] == "zh"
        self.font = ZH_FONT if self.zh else EN_FONT
        self.bold = ZH_FONT if self.zh else EN_BOLD
        self.canvas = canvas.Canvas(str(path), pagesize=A4, invariant=1)
        self.canvas.setTitle(f"{document['file']} (synthetic fixture)")
        self.canvas.setAuthor("ForgeFlow synthetic fixture generator")
        self.page = 0

    # -- primitives -------------------------------------------------------------------------------------------
    def box(self, x: float, baseline: float, text: str, font: str, size: float) -> list[float]:
        face = self.metrics.getFont(font).face
        descent = face.descent / 1000.0 * size
        width = self.metrics.stringWidth(text, font, size)
        return [round(x, 3), round(self.height - (baseline + descent + size), 3),
                round(x + width, 3), round(self.height - (baseline + descent), 3)]

    def text(self, x: float, baseline: float, text: str, font: str, size: float, align: str = "left") -> list[float]:
        width = self.metrics.stringWidth(text, font, size)
        if align == "right":
            x -= width
        elif align == "center":
            x -= width / 2
        self.canvas.setFont(font, size)
        self.canvas.drawString(x, baseline, text)
        return self.box(x, baseline, text, font, size)

    def rect(self, x0: float, top: float, x1: float, bottom: float) -> list[float]:
        """Draw a cell border given reportlab y of its top and bottom edges; return pdfplumber bbox."""
        self.canvas.setLineWidth(0.5)
        self.canvas.rect(x0, bottom, x1 - x0, top - bottom, stroke=1, fill=0)
        return [round(x0, 3), round(self.height - top, 3), round(x1, 3), round(self.height - bottom, 3)]

    def wrap(self, text: str, font: str, size: float, width: float) -> list[str]:
        if not self.zh:
            lines, current = [], ""
            for word in text.split():
                trial = f"{current} {word}".strip()
                if current and self.metrics.stringWidth(trial, font, size) > width:
                    lines.append(current)
                    current = word
                else:
                    current = trial
            return lines + ([current] if current else [])
        # CJK: break between any two characters except inside a run of ASCII (numbers stay whole).
        tokens = re.findall(r"[\x21-\x7e]+|.", text)
        lines, current = [], ""
        for token in tokens:
            if current and self.metrics.stringWidth(current + token, font, size) > width:
                lines.append(current)
                current = token
            else:
                current += token
        return lines + ([current] if current else [])

    # -- page structure ---------------------------------------------------------------------------------------
    def start_page(self, number: int, total: int, header: bool = True) -> float:
        self.page = number
        furniture = self.layout["furniture"]
        if header:
            running = self.doc["running_header"]
            self.text(self.width / 2, self.height - 32, running, self.font, 8, "center")
            furniture.append({"file": self.doc["file"], "page": number, "text": running})
        footer = self.doc["footer"].format(page=number, pages=total)
        self.text(self.width / 2, 28, footer, self.font, 8, "center")
        furniture.append({"file": self.doc["file"], "page": number, "text": footer})
        return self.height - 70

    def heading(self, block: dict, y: float) -> float:
        size = float(block.get("size", 12))
        baseline = y - size
        box = self.text(LEFT, baseline, block["text"], self.bold, size)
        self.layout["headings"].append({"file": self.doc["file"], "page": self.page, "text": block["text"],
                                        "bbox": box})
        return baseline - 0.7 * size

    def paragraph(self, block: dict, y: float) -> float:
        size = 10.5 if self.zh else 10.0
        leading = 1.55 * size
        boxes = []
        baseline = y - size
        for line in self.wrap(block["text"], self.font, size, RIGHT - LEFT):
            boxes.append(self.text(LEFT, baseline, line, self.font, size))
            baseline -= leading
        self.layout["paragraphs"][block["id"]] = {"file": self.doc["file"], "page": self.page,
                                                  "bbox": _union(boxes), "lines": len(boxes)}
        return baseline + leading - 0.5 * size - 9

    def scan(self, block: dict) -> None:
        from PIL import Image, ImageDraw, ImageFont
        from reportlab.lib.utils import ImageReader

        image = Image.new("L", (1240, 1754), 255)
        draw = ImageDraw.Draw(image)
        font = ImageFont.load_default(size=34)
        y = 240
        for line in block["lines"]:
            draw.text((150, y), line, fill=30, font=font)
            y += 90
        for k in range(14):  # body-text-like strokes of a scanned page
            draw.rectangle((150, 700 + k * 48, 1090 - (k % 3) * 120, 716 + k * 48), fill=90)
        draw.ellipse((820, 1380, 1080, 1640), outline=60, width=8)
        image = image.rotate(0.6, fillcolor=255)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        buffer.seek(0)
        self.canvas.drawImage(ImageReader(buffer), 0, 0, self.width, self.height)
        self.layout["scans"].append({"file": self.doc["file"], "page": self.page, "id": block["id"]})

    # -- tables -----------------------------------------------------------------------------------------------
    def columns(self, table: dict) -> list[float]:
        xs = [LEFT]
        for width in table["col_widths"]:
            xs.append(xs[-1] + width)
        return xs

    def caption(self, table: dict, part: int, y: float, centred: bool) -> float:
        title = table["caption"] if part == 0 else table.get("continued_caption")
        if title is None:
            return y
        if centred:
            self.text(self.width / 2, y - 11, title, self.bold, 11, "center")
        else:
            self.text(LEFT, y - 11, title, self.bold, 11)
        y -= 17
        if not table.get("unit_line"):
            return y - 2  # unit declared elsewhere (column headers), no unit line
        if centred:
            self.text(RIGHT, y - 9, table["unit_line"], self.font, 9, "right")
        else:
            self.text(LEFT, y - 9, table["unit_line"], self.font, 9)
        return y - 15

    def ruled(self, name: str, table: dict, part: int, y: float) -> float:
        xs = self.columns(table)
        start, end = table["parts"][part]["rows"]
        y = self.caption(table, part, y, centred=True)
        top = y
        cells: dict[str, list[float]] = {}
        if part == 0 or table.get("repeat_header"):
            header = table["header_rows"]
            for i, row in enumerate(header):
                for j, cell in enumerate(row):
                    if cell is None:
                        continue
                    spec = cell if isinstance(cell, dict) else {"text": cell}
                    colspan, rowspan = spec.get("colspan", 1), spec.get("rowspan", 1)
                    cell_top = y - 20 * i
                    cell_bottom = cell_top - 20 * rowspan
                    self.rect(xs[j], cell_top, xs[j + colspan], cell_bottom)
                    if spec["text"]:
                        self.text((xs[j] + xs[j + colspan]) / 2, (cell_top + cell_bottom) / 2 - 3, spec["text"],
                                  self.font, 9, "center")
            y -= 20 * len(header)
        for index in range(start, end):
            row = table["rows"][index]
            label_lines = _chunks(row["label"], row["wrap"]) if row.get("wrap") else [row["label"]]
            height = 20 if len(label_lines) == 1 else 11 * len(label_lines) + 9
            row_top, row_bottom = y, y - height
            self.rect(xs[0], row_top, xs[1], row_bottom)
            for k, line in enumerate(label_lines):
                self.text(xs[0] + 4, row_top - 13 - 11 * k, line, self.font, 9)
            style = row.get("negative_style", table["negative_style"])
            if style == "minus" and self.zh:
                style = "fullwidth_minus"
            for j, cell in enumerate(row["cells"], start=1):
                cells[f"r{index}-c{j}"] = self.rect(xs[j], row_top, xs[j + 1], row_bottom)
                printed = format_cell(cell, style)
                if not printed:
                    continue
                if classify(cell) == "text" or j in table.get("text_columns", []):
                    self.text((xs[j] + xs[j + 1]) / 2, row_top - 13, printed, self.font, 9, "center")
                else:
                    self.text(xs[j + 1] - 4, row_top - 13, printed, self.font, 9, "right")
            y = row_bottom
        bbox = [round(xs[0], 3), round(self.height - top, 3), round(xs[-1], 3), round(self.height - y, 3)]
        self.layout["tables"].setdefault(name, {"parts": []})["parts"].append(
            {"file": self.doc["file"], "page": self.page, "bbox": bbox, "cells": cells, "rows": [start, end]})
        return y - 22

    def borderless(self, name: str, table: dict, part: int, y: float) -> float:
        xs = self.columns(table)
        start, end = table["parts"][part]["rows"]
        y = self.caption(table, part, y, centred=False)
        boxes: list[list[float]] = []
        cells: dict[str, list[float]] = {}
        size, pitch = 9.0, 15.0
        if part == 0 or table.get("repeat_header"):
            for row in table["header_rows"]:
                baseline = y - 11
                for j, cell in enumerate(row):
                    if cell is None:
                        continue
                    spec = cell if isinstance(cell, dict) else {"text": cell}
                    if not spec["text"]:
                        continue
                    colspan = spec.get("colspan", 1)
                    if colspan > 1:
                        boxes.append(self.text((xs[j] + xs[j + colspan]) / 2, baseline, spec["text"], self.bold,
                                               size, "center"))
                        self.canvas.setLineWidth(0.5)
                        self.canvas.line(xs[j] + 10, baseline - 3, xs[j + colspan] - 4, baseline - 3)
                    elif j == 0:  # label-column header (项目), left-aligned like the labels
                        boxes.append(self.text(xs[0], baseline, spec["text"], self.bold, size))
                    else:
                        boxes.append(self.text(xs[j + 1] - 4, baseline, spec["text"], self.bold, size, "right"))
                y -= 14
            y -= 2
        indent = False
        for index in range(start, end):
            row = table["rows"][index]
            baseline = y - 11
            if row.get("section"):
                boxes.append(self.text(xs[0], baseline, row["label"], self.font, size))
                indent = True
                y -= pitch
                continue
            if SECTION_TOTAL.match(row["label"]):
                indent = False
            boxes.append(self.text(xs[0] + (10 if indent else 0), baseline, row["label"], self.font, size))
            style = row.get("negative_style", table["negative_style"])
            band = [round(self.height - (baseline + 0.72 * pitch), 3), round(self.height - (baseline - 0.28 * pitch), 3)]
            for j, cell in enumerate(row["cells"], start=1):
                cells[f"r{index}-c{j}"] = [round(xs[j], 3), band[0], round(xs[j + 1], 3), band[1]]
                printed = format_cell(cell, style)
                if not printed:
                    continue
                if row.get("dollar") and classify(cell) == "number":
                    boxes.append(self.text(xs[j] + 12, baseline, "$", self.font, size))
                if j in table.get("text_columns", []):  # note references are centred, not amounts
                    boxes.append(self.text((xs[j] + xs[j + 1]) / 2, baseline, printed, self.font, size, "center"))
                else:
                    boxes.append(self.text(xs[j + 1] - 4, baseline, printed, self.font, size, "right"))
            y -= pitch
        bbox = _union(boxes)
        self.layout["tables"].setdefault(name, {"parts": []})["parts"].append(
            {"file": self.doc["file"], "page": self.page, "bbox": bbox, "cells": cells, "rows": [start, end]})
        return y - 18

    def render(self) -> None:
        pages = self.doc["pages"]
        for page in pages:
            scan = [b for b in page["blocks"] if b["type"] == "scan"]
            y = self.start_page(page["page"], len(pages), header=not scan)
            for block in page["blocks"]:
                if block["type"] == "heading":
                    y = self.heading(block, y)
                elif block["type"] == "paragraph":
                    y = self.paragraph(block, y)
                elif block["type"] == "scan":
                    self.scan(block)
                elif block["type"] == "table":
                    table = self.tables[block["table"]]
                    draw = self.ruled if table["style"] == "ruled" else self.borderless
                    y = draw(block["table"], table, block["part"], y)
                else:
                    raise ValueError(f"unknown block type {block['type']}")
            if y < 50:
                raise ValueError(f"{self.doc['file']} page {page['page']} overflows the page")
            self.canvas.showPage()
        self.canvas.save()


def _chunks(text: str, size: int) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)]


def _union(boxes: list[list[float]]) -> list[float]:
    return [round(min(b[0] for b in boxes), 3), round(min(b[1] for b in boxes), 3),
            round(max(b[2] for b in boxes), 3), round(max(b[3] for b in boxes), 3)]


def generate(out_dir: str | Path, truth_path: str | Path = TRUTH) -> dict:
    """Write the PDFs and layout.json into ``out_dir``; return the layout (with file sha256s)."""
    truth = json.loads(Path(truth_path).read_text(encoding="utf-8"))
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    layout: dict = {"truth_sha256": hashlib.sha256(Path(truth_path).read_bytes()).hexdigest(),
                    "coordinates": "pdfplumber: x0, top, x1, bottom in points from the page's top-left",
                    "files": {}, "tables": {}, "paragraphs": {}, "headings": [], "scans": [], "furniture": []}
    for document in truth["documents"]:
        path = out / document["file"]
        _Doc(path, document, truth["tables"], layout).render()
        layout["files"][document["file"]] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                             "bytes": path.stat().st_size, "pages": len(document["pages"])}
    (out / "layout.json").write_text(json.dumps(layout, ensure_ascii=False, indent=2), encoding="utf-8")
    return layout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--truth", default=str(TRUTH))
    parser.add_argument("--out", default=str(ROOT / "fixtures/financial-pdf/generated"))
    args = parser.parse_args(argv)
    layout = generate(args.out, args.truth)
    print(json.dumps(layout["files"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
