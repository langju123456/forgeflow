"""Financial PDF ingestion: text chunks and tables as citable Knowledge rows.

Each row is a Knowledge document row (``id``, ``version``, ``content``, ``source_hash``, ``roles``,
``classification``, ``valid_until`` and the scope fields) plus a ``citation`` (file, file sha256, page,
bbox). The citation is also written into the first line of ``content``, so ``source_hash`` covers it and a
row read back from the Store still names its file, page and bbox. Rows go into the Store only through
``Store.put_document``; KnowledgeService authorization and exact-version citation checks apply unchanged.

Tables are kept as structured cells (header rows, column keys, row labels, page and bbox per cell) and
rendered to a compact ``label | value | value`` text for retrieval. Cell values keep the printed string next
to the parsed number; a cell that does not parse stays in the table with kind ``text`` or ``unparsed``.

Limits, stated rather than hidden: ruled tables come from pdfplumber's line finder, borderless tables from
a word-alignment heuristic (label at the left, numbers right-aligned in columns); row labels are assumed to
be in the first column; scanned or glyph-garbled pages are only flagged ``NEEDS_OCR`` (no OCR here);
there is no deletion of rows that disappear when a file is re-ingested at a new version.

pdfplumber is an optional extra (``pip install 'forgeflow[pdf]'``). Number and unit normalisation work
without it.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import statistics
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .contracts import DomainError, Scope

# --------------------------------------------------------------------------- numbers and units

EMPTY_MARKERS = frozenset({"", "-", "--", "—", "——", "–", "/"})
_CURRENCY_TOKENS = (("US$", "USD"), ("HK$", "HKD"), ("RMB", "CNY"), ("USD", "USD"), ("CNY", "CNY"),
                    ("$", "USD"), ("¥", "CNY"), ("€", "EUR"), ("£", "GBP"))
_NUMBER = re.compile(r"^(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$|^\.\d+$")
_YEAR = re.compile(r"^(?:19|20)\d{2}$")
NUMERIC_KINDS = frozenset({"number", "percent", "empty"})


@dataclass(frozen=True)
class ParsedValue:
    """One printed cell. ``value`` is the number as printed (sign applied, unit not applied)."""

    raw: str
    kind: str  # number | percent | empty | text | unparsed
    value: Decimal | None = None
    currency: str | None = None


def _strip_currency(text: str) -> tuple[str, str | None]:
    for token, code in _CURRENCY_TOKENS:
        if text.startswith(token):
            return text[len(token):], code
        if text.endswith(token) and len(text) > len(token):
            return text[: -len(token)], code
    return text, None


def parse_number(raw: str | None) -> ParsedValue:
    """Parse a printed financial number without guessing.

    ``1,234.5`` -> 1234.5; ``(1,234)`` and ``-1,234`` -> -1234; ``12.5%`` / ``(3.1)%`` -> percent;
    ``—``, ``-`` and blank -> empty. Thousands separators must group by three; anything else that is not a
    clean number is returned as ``text`` (has letters) or ``unparsed`` with the raw string kept.
    """
    raw = "" if raw is None else str(raw)
    text = unicodedata.normalize("NFKC", raw)
    text = text.replace("−", "-").replace("‐", "-").replace("‑", "-")
    if re.search(r"\d\s+\d", text):  # "12 34" may be two numbers in one cell; do not join them
        letters = any(ch.isalpha() for ch in text)
        return ParsedValue(raw, "text" if letters else "unparsed")
    compact = re.sub(r"\s+", "", text)
    if compact in EMPTY_MARKERS:
        return ParsedValue(raw, "empty")
    body, currency = _strip_currency(compact)
    negative = False
    if body.startswith("(") and body.endswith(")%"):
        body, negative = body[1:-2] + "%", True
    elif body.startswith("(") and body.endswith(")"):
        body, negative = body[1:-1], True
    body, more = _strip_currency(body)
    currency = currency or more
    percent = body.endswith("%")
    if percent:
        body = body[:-1]
    if body[:1] in {"-", "+"}:
        if negative:  # "(-1)" is ambiguous; do not guess a sign
            return ParsedValue(raw, "unparsed")
        negative, body = body[0] == "-", body[1:]
        body, more = _strip_currency(body)
        currency = currency or more
    if not _NUMBER.match(body):
        letters = any(ch.isalpha() for ch in compact)
        return ParsedValue(raw, "text" if letters else "unparsed")
    try:
        value = Decimal(body.replace(",", ""))
    except InvalidOperation:  # pragma: no cover - the pattern above admits only valid decimals
        return ParsedValue(raw, "unparsed")
    return ParsedValue(raw, "percent" if percent else "number", -value if negative else value, currency)


@dataclass(frozen=True)
class Unit:
    label: str  # 万元 / thousand / ...
    scale: Decimal
    currency: str | None
    source: str  # the text the unit was read from
    except_per_share: bool = False

    def as_dict(self) -> dict:
        return {"label": self.label, "scale": str(self.scale), "currency": self.currency,
                "source": self.source, "except_per_share": self.except_per_share}


_ZH_SCALES = {"元": 1, "千元": 1000, "万元": 10_000, "百万元": 1_000_000, "亿元": 100_000_000}
_ZH_MULTIPLIERS = {"": 1, "千": 1000, "万": 10_000, "百万": 1_000_000, "亿": 100_000_000}
_EN_SCALES = {"thousand": 1000, "million": 1_000_000, "billion": 1_000_000_000}
_ZH_CURRENCY = {"美元": "USD", "港元": "HKD", "欧元": "EUR"}
_ZH_UNIT = re.compile(r"(?:金额)?单位\s*[:：]\s*(?P<cur>人民币|港币)?\s*(?P<mult>百万|千|万|亿)?\s*"
                      r"(?P<ccy>美元|港元|欧元|元)")
_EN_UNIT = re.compile(r"\bin\s+(?P<unit>thousand|million|billion)s?\b", re.I)
# "RMB’Million" column headers (Tencent 2025 annual report), "RMB million", "US$ million", "HK$ billion".
_EN_CUR_UNIT = re.compile(r"(?:RMB|HK\$|US\$|USD|HKD)\s*['’]?\s*(?P<unit>thousand|million|billion)s?\b", re.I)
_EN_000 = re.compile(r"(?P<cur>RMB|USD|US\$|HK\$|\$)\s*['’]\s*000\b")
_EXCEPT_PER_SHARE = re.compile(r"except\s+(?:for\s+)?(?:share\s+and\s+)?per[\s-]share", re.I)
_INLINE_UNIT = re.compile(r"[(（]\s*(?P<unit>百万元|千元|万元|亿元|元)(?:\s*/\s*股)?\s*[)）]")
_INLINE_PERCENT = re.compile(r"[(（]\s*%\s*[)）]")
_PER_SHARE = re.compile(r"per[\s-]share|每股", re.I)
_CONTINUED = re.compile(r"[(（]\s*续\s*[)）]|\bcontinued\b", re.I)


def _en_currency(text: str) -> str | None:
    if re.search(r"U\.?\s?S\.?\s+dollars|\bUSD\b|US\$|\$", text):
        return "USD"
    if re.search(r"\bRMB\b|renminbi", text, re.I):
        return "CNY"
    return None


def detect_unit(text: str) -> Unit | None:
    """Read a declared unit from a caption or header: 单位：万元, (in thousands of U.S. dollars), RMB'000."""
    if not text:
        return None
    normalised = unicodedata.normalize("NFKC", text)
    match = _ZH_UNIT.search(normalised)
    if match:
        multiplier, ccy = match.group("mult") or "", match.group("ccy")
        currency = _ZH_CURRENCY.get(ccy) or ("HKD" if match.group("cur") == "港币" else "CNY")
        return Unit(multiplier + ccy, Decimal(_ZH_MULTIPLIERS[multiplier]), currency, match.group(0))
    per_share = bool(_EXCEPT_PER_SHARE.search(normalised))
    match = _EN_UNIT.search(normalised) or _EN_CUR_UNIT.search(normalised)
    if match:
        word = match.group("unit").lower()
        return Unit(word, Decimal(_EN_SCALES[word]), _en_currency(normalised), match.group(0), per_share)
    match = _EN_000.search(normalised)
    if match:
        return Unit("thousand", Decimal(1000), _en_currency(match.group("cur")), match.group(0), per_share)
    return None


def _inline_unit(text: str) -> Unit | str | None:
    """A unit written into one row label or column header, e.g. 基本每股收益（元/股） or 占比(%)."""
    normalised = unicodedata.normalize("NFKC", text or "")
    if _INLINE_PERCENT.search(normalised):
        return "percent"
    match = _INLINE_UNIT.search(normalised)
    if match:
        unit = match.group("unit")
        return Unit(unit, Decimal(_ZH_SCALES[unit]), "CNY", match.group(0))
    return None


def normalise_cell(raw: str | None, table_unit: Unit | None, row_text: str = "", column_text: str = "") -> dict:
    """Parse one cell and apply the unit that governs it: row label > column header > table caption.

    Percentages are never scaled; ``base_value`` of 12.5% is 0.125. Per-share rows under a caption that says
    "except per share" keep the printed value. Values are decimal strings, never floats.
    """
    parsed = parse_number(raw)
    row_unit, column_unit = _inline_unit(row_text), _inline_unit(column_text)
    kind = parsed.kind
    if kind == "number" and "percent" in (row_unit, column_unit):
        kind = "percent"
    unit: Unit | None
    if isinstance(row_unit, Unit):
        unit = row_unit
    elif table_unit is not None and table_unit.except_per_share and _PER_SHARE.search(row_text or ""):
        unit = Unit("per share (as printed)", Decimal(1), table_unit.currency, "except per share")
    elif isinstance(column_unit, Unit):
        unit = column_unit
    else:
        unit = table_unit
    result = {"raw": parsed.raw, "kind": kind, "value": None, "unit": None, "scale": None, "base_value": None,
              "currency": parsed.currency}
    if parsed.value is None:
        return result
    result["value"] = format(parsed.value, "f")
    if kind == "percent":
        result.update(unit="%", scale="0.01", base_value=format(parsed.value / 100, "f"))
    else:
        scale = unit.scale if unit else Decimal(1)
        result.update(unit=unit.label if unit else None, scale=format(scale, "f"),
                      base_value=format(parsed.value * scale, "f"))
        result["currency"] = parsed.currency or (unit.currency if unit else None)
    return result


# --------------------------------------------------------------------------- layout primitives

_CJK = re.compile(r"[⺀-〿㐀-䶿一-鿿豈-﫿＀-￯]")


def _cjk(ch: str) -> bool:
    return bool(ch) and bool(_CJK.match(ch))


def join_lines(parts: Iterable[str]) -> str:
    """Join wrapped lines: no space at a CJK boundary, one space between Latin words."""
    out = ""
    for part in parts:
        part = (part or "").strip()
        if not part:
            continue
        if out and not (_cjk(out[-1]) or _cjk(part[0])):
            out += " "
        out += part
    return out


def _round_bbox(bbox: Iterable[float]) -> list[float]:
    return [round(float(v), 2) for v in bbox]


def _union(boxes: Iterable[Iterable[float]]) -> list[float]:
    boxes = [list(b) for b in boxes]
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


def _center_inside(box: Iterable[float], area: Iterable[float], pad: float = 0.5) -> bool:
    x0, top, x1, bottom = box
    cx, cy = (x0 + x1) / 2, (top + bottom) / 2
    a0, atop, a1, abottom = area
    return a0 - pad <= cx <= a1 + pad and atop - pad <= cy <= abottom + pad


@dataclass
class _Line:
    words: list[dict]
    x0: float
    x1: float
    top: float
    bottom: float
    size: float
    bold: bool
    text: str
    segments: list[dict] = field(default_factory=list)
    used: bool = False  # consumed by a table or caption

    @property
    def bbox(self) -> list[float]:
        return [self.x0, self.top, self.x1, self.bottom]


def _word_join(words: list[dict]) -> str:
    out = ""
    previous = None
    for word in words:
        if previous is not None and word["x0"] - previous["x1"] > 0.15 * max(word.get("size", 9), 1):
            out += " "
        out += word["text"]
        previous = word
    return out


def _segments(words: list[dict], size: float) -> list[dict]:
    """Split a line where the horizontal gap is wider than a word space (table columns)."""
    gap = max(6.0, 0.8 * size)
    groups: list[list[dict]] = []
    for word in words:
        if groups and word["x0"] - groups[-1][-1]["x1"] <= gap:
            groups[-1].append(word)
        else:
            groups.append([word])
    segments = [{"text": _word_join(g), "x0": g[0]["x0"], "x1": g[-1]["x1"],
                 "top": min(w["top"] for w in g), "bottom": max(w["bottom"] for w in g)} for g in groups]
    merged: list[dict] = []
    for segment in segments:  # a lone "$" belongs to the number on its right
        if merged and merged[-1]["text"] in {"$", "US$", "HK$", "¥", "RMB", "€", "£"}:
            left = merged.pop()
            segment = {**segment, "text": left["text"] + " " + segment["text"], "x0": left["x0"],
                       "top": min(left["top"], segment["top"]), "bottom": max(left["bottom"], segment["bottom"])}
        merged.append(segment)
    return merged


def _lines(words: list[dict]) -> list[_Line]:
    ordered = sorted(words, key=lambda w: (w["top"], w["x0"]))
    groups: list[list[dict]] = []
    for word in ordered:
        if groups and abs(word["top"] - groups[-1][0]["top"]) <= 2.5:
            groups[-1].append(word)
        else:
            groups.append([word])
    lines = []
    for group in groups:
        group.sort(key=lambda w: w["x0"])
        size = max(float(w.get("size", 9) or 9) for w in group)
        line = _Line(group, min(w["x0"] for w in group), max(w["x1"] for w in group),
                     min(w["top"] for w in group), max(w["bottom"] for w in group), size,
                     any("bold" in str(w.get("fontname", "")).lower() for w in group), _word_join(group))
        line.segments = _segments(group, size)
        lines.append(line)
    return lines


# --------------------------------------------------------------------------- per-page analysis


@dataclass
class _Row:
    label: str
    cells: list[dict]  # {"raw": str, "bbox": list | None, "merged": bool}
    bbox: list[float]
    page: int
    section: bool = False
    label_bbox: list[float] | None = None


@dataclass
class _Table:
    page: int
    style: str  # ruled | borderless
    bbox: list[float]
    header_rows: list[list[str]]
    column_keys: list[str]
    column_bounds: list[tuple[float, float]]
    rows: list[_Row]
    title: str | None = None
    caption_lines: list[str] = field(default_factory=list)
    unit: Unit | None = None
    continued_marker: bool = False
    segments: list[dict] = field(default_factory=list)
    dropped_header_rows: int = 0


@dataclass
class _Page:
    number: int
    width: float
    height: float
    status: str
    reason: str
    lines: list[_Line]
    tables: list[_Table]
    furniture: list[str]
    content_chars: int
    image_coverage: float


def _header_like(texts: list[str | None]) -> bool:
    if any(t is None for t in texts):
        return True  # merged cells occur in header bands of financial statements
    rest = [(t or "").strip() for t in texts[1:]]
    if not any(rest):
        return False  # label-only row: a section such as 流动资产：
    for value in rest:
        parsed = parse_number(value)
        if parsed.kind in {"number", "percent"} and not _YEAR.match(value):
            return False
    return True


def _column_keys(header_rows: list[list[str]], width: int) -> list[str]:
    keys = []
    for j in range(width):
        path: list[str] = []
        for row in header_rows:
            text = (row[j] if j < len(row) else "") or ""
            text = text.strip()
            if text and (not path or path[-1] != text):
                path.append(text)
        keys.append(" / ".join(path))
    return keys


def _blank(text: str | None) -> bool:
    return text is None or not text.strip()


_YEAR_HEADER = re.compile(r"^(?:FY\s*)?(?:19|20)\d{2}\s*(?:年\s*(?:度|末)?)?$", re.IGNORECASE)


def _numeric(text: str | None) -> bool:
    """A body value; a bare year such as 2025, 2025年 or 2025 年度 is a header, not a number."""
    return (not _blank(text) and parse_number(text).kind in {"number", "percent"}
            and not _YEAR_HEADER.match(text.strip()))


def _normalise_grid(texts: list[list[str | None]], grid: list[list[Any]]) -> tuple[list[list[str | None]], list[list[Any]]]:
    """Undo three ways a ruled grid misrepresents a financial table (seen first on CATL's 2025 annual report).

    1. Spacer columns: rectangles drawn for padding become columns that are blank in every row. Dropped.
    2. Split header and value columns: the year sits in its own grid column next to the column holding that
       year's numbers. A column with text only in the header band is merged into an adjacent column that has
       text only below it.
    3. Wrapped labels: a long row label continues on a grid row whose other cells are all merged (None);
       that line is appended to the label above instead of becoming a row of its own.
    """
    width = max(len(r) for r in texts)
    texts = [list(r) + [None] * (width - len(r)) for r in texts]
    grid = [list(r) + [None] * (width - len(r)) for r in grid]
    keep = [j for j in range(width) if any(not _blank(r[j]) for r in texts)]
    if len(keep) < 2:
        return texts, grid
    texts = [[r[j] for j in keep] for r in texts]
    grid = [[r[j] for j in keep] for r in grid]
    width = len(keep)
    numeric_rows = [i for i, r in enumerate(texts) if any(_numeric(t) for t in r[1:])]
    band = numeric_rows[0] if numeric_rows else len(texts)
    if 0 < band < len(texts):
        # Rows without any number (the header band, and sub-headers such as "项目 | 2025 年末 | ..." halfway down
        # CATL's key-figures table) carry headers; rows with numbers carry values.
        numeric = set(numeric_rows)
        wordy = [i for i in range(len(texts)) if i not in numeric]

        def header_only(j: int) -> bool:
            return (any(not _blank(texts[i][j]) for i in wordy if i < band)
                    and all(_blank(texts[i][j]) for i in numeric))

        def value_only(j: int) -> bool:
            return (all(_blank(texts[i][j]) for i in wordy if i < band)
                    and any(not _blank(texts[i][j]) for i in numeric))

        drop: set[int] = set()
        for j in range(1, width):
            if j in drop or not header_only(j):
                continue
            target = next((k for k in (j - 1, j + 1) if 0 < k < width and k not in drop and value_only(k)), None)
            if target is None:
                continue
            for i in wordy:
                if _blank(texts[i][target]):
                    texts[i][target], grid[i][target] = texts[i][j], grid[i][j]
            drop.add(j)
        if drop:
            texts = [[t for j, t in enumerate(r) if j not in drop] for r in texts]
            grid = [[c for j, c in enumerate(r) if j not in drop] for r in grid]
    merged_texts: list[list[str | None]] = []
    merged_grid: list[list[Any]] = []
    for i, row in enumerate(texts):
        continuation = (i > band and merged_texts and not _blank(row[0]) and all(t is None for t in row[1:])
                        and any(not _blank(t) for t in merged_texts[-1][1:]))
        if continuation:
            merged_texts[-1][0] = join_lines([merged_texts[-1][0] or "", row[0] or ""])
            above, below = merged_grid[-1][0], grid[i][0]
            if above is not None and below is not None:
                merged_grid[-1][0] = (min(above[0], below[0]), min(above[1], below[1]),
                                      max(above[2], below[2]), max(above[3], below[3]))
            continue
        merged_texts.append(list(row))
        merged_grid.append(list(grid[i]))
    return merged_texts, merged_grid


def _ruled_tables(page: Any, number: int) -> list[_Table]:
    settings = {"vertical_strategy": "lines", "horizontal_strategy": "lines", "snap_tolerance": 3,
                "join_tolerance": 3, "intersection_tolerance": 3}
    tables = []
    for found in page.find_tables(settings):
        grid = [list(row.cells) for row in found.rows]
        texts = [[None if c is None else join_lines(c.split("\n")) for c in row] for row in found.extract()]
        if len(grid) < 2 or max(len(r) for r in grid) < 2:
            continue
        texts, grid = _normalise_grid(texts, grid)
        if max(len(r) for r in grid) < 2:
            continue
        width = max(len(r) for r in grid)
        bounds = []
        for j in range(width):
            x0s = [r[j][0] for r in grid if j < len(r) and r[j] is not None]
            x1s = [r[j][2] for r in grid if j < len(r) and r[j] is not None]
            bounds.append((min(x0s) if x0s else 0.0, statistics.median(x1s) if x1s else 0.0))
        filled: list[list[str | None]] = [list(r) + [None] * (width - len(r)) for r in texts]
        cells = [list(r) + [None] * (width - len(r)) for r in grid]
        header_count = 0
        for row in filled[:4]:
            if not _header_like(row):
                break
            header_count += 1
        if header_count == len(filled):
            continue  # no body: not a data table
        # Fill merged header positions: from the left for column spans, from above for row spans.
        header = [list(r) for r in filled[:header_count]]
        for i in range(header_count):
            for j in range(width):
                if header[i][j] is not None:
                    continue
                centre = (bounds[j][0] + bounds[j][1]) / 2
                source = None
                for k in range(j - 1, -1, -1):
                    if cells[i][k] is not None:
                        if cells[i][k][2] > centre:
                            source = header[i][k]
                        break
                if source is None:
                    row_mid = None
                    for k in range(i - 1, -1, -1):
                        if cells[k][j] is not None:
                            row_mid = k
                            break
                    if row_mid is not None:
                        source = header[row_mid][j]
                header[i][j] = source or ""
        rows = []
        for i in range(header_count, len(filled)):
            row_cells = []
            for j in range(1, width):
                bbox = cells[i][j]
                row_cells.append({"raw": filled[i][j] or "", "merged": filled[i][j] is None,
                                  "bbox": _round_bbox(bbox) if bbox else None})
            label = filled[i][0] or ""
            boxes = [c for c in cells[i] if c is not None]
            section = bool(label) and not any(c["raw"].strip() for c in row_cells)
            rows.append(_Row(label, row_cells, _round_bbox(_union(boxes)), number, section,
                             _round_bbox(cells[i][0]) if cells[i][0] else None))
        body = [r for r in rows if not r.section]
        crammed = [r for r in rows if r.section and len(re.findall(r"\d[\d,]*\.?\d*", r.label)) >= 2]
        if len(crammed) > max(2, len(body)):
            # Horizontal rules but no column rules (Apple's 10-K statements): the "grid" is one cell per line,
            # with the numbers inside the label. Not a table of cells; leave the lines to the borderless pass.
            continue
        tables.append(_Table(number, "ruled", _round_bbox(found.bbox), [[h or "" for h in r] for r in header],
                             _column_keys(header, width), bounds, rows))
    return tables


def _value_kind(text: str) -> str:
    return parse_number(text).kind


def _body_like(line: _Line) -> bool:
    segments = line.segments
    if len(segments) < 2 or _value_kind(segments[0]["text"]) != "text":
        return False
    return any(_value_kind(s["text"]) in NUMERIC_KINDS for s in segments[1:])


def _label_header(line: _Line, zone: float) -> bool:
    """A header band with the label-column title at the left (项目 | 本期发生额 | 上期发生额)."""
    first, rest = line.segments[0], line.segments[1:]
    return (bool(rest) and first["x1"] < zone - 2 and all(s["x0"] >= zone - 5 for s in rest)
            and all(_value_kind(s["text"]) not in {"number", "percent"} or _YEAR.match(s["text"].strip())
                    for s in rest))


def _colon(line: _Line) -> bool:
    return line.text.rstrip().endswith((":", "："))


def _borderless_tables(lines: list[_Line], number: int) -> list[_Table]:
    tables: list[_Table] = []
    free = [line for line in lines if not line.used]
    i = 0
    while i < len(free):
        if not _body_like(free[i]):
            i += 1
            continue
        block = [free[i]]
        j = i + 1
        while j < len(free):
            line, previous = free[j], block[-1]
            if line.top - previous.bottom > 2.0 * max(previous.size, line.size):
                break
            if _body_like(line):
                block.append(line)
            elif len(line.segments) == 1 and _value_kind(line.segments[0]["text"]) == "text":
                zone = min(s["x0"] for row in block if _body_like(row) for s in row.segments[1:])
                if line.x1 >= zone - 2:
                    break
                block.append(line)
            else:
                break
            j += 1
        trailing: list[_Line] = []
        while block and not _body_like(block[-1]):
            trailing.insert(0, block.pop())
        if (block and trailing and all(_colon(t) for t in trailing)
                and not any(other.top > trailing[-1].bottom for other in lines)):
            block += trailing  # a section header orphaned at the page bottom belongs to the continuing table
        body = [line for line in block if _body_like(line)]
        if len(body) < 2:
            i += 1
            continue
        # Columns: value segments cluster on their right edge (numbers are right-aligned).
        values = sorted((s for line in body for s in line.segments[1:]), key=lambda s: s["x1"])
        clusters: list[list[dict]] = []
        for segment in values:
            if clusters and segment["x1"] - clusters[-1][-1]["x1"] <= 6.0:
                clusters[-1].append(segment)
            else:
                clusters.append([segment])
        anchors = [statistics.median(s["x1"] for s in c) for c in clusters]
        bounds = [(min(s["x0"] for s in c), max(s["x1"] for s in c)) for c in clusters]
        zone = min(b[0] for b in bounds)
        label_x0 = min(line.segments[0]["x0"] for line in body)
        # Header rows above the first row: segments only over value columns; colon sections may sit between.
        start = free.index(block[0])
        headers: list[_Line] = []
        pending: list[_Line] = []
        k = start - 1
        reference = block[0]
        while k >= 0 and len(headers) < 4:
            line = free[k]
            # 3.2 line heights: Tencent's statements leave a blank line between the header band and the first row.
            if line.used or reference.top - line.bottom > 3.2 * max(reference.size, line.size):
                break  # never take a line that an earlier table on this page already owns
            # Headers are centred over their column, so a wide one ("RMB'Million", "31 December") starts up to
            # ~25pt left of the numbers below it (Tencent 2025 annual report); a label line starts at the margin.
            if all(s["x0"] >= zone - 30 for s in line.segments) or _label_header(line, zone):
                headers.insert(0, line)
                block = pending + block
                pending = []
            elif len(line.segments) == 1 and line.x1 < zone - 2 and _colon(line):
                pending.insert(0, line)
            else:
                break
            reference = line
            k -= 1
        if not headers and pending and not any(other.bottom <= pending[0].top for other in lines
                                               if other not in pending):
            block = pending + block  # top of a continuation page: section rows before the first body row
        width = len(anchors) + 1

        def column_of(segment: dict, anchors: list[float] = anchors) -> int:
            distances = [abs(segment["x1"] - anchor) for anchor in anchors]
            return distances.index(min(distances)) + 1

        def header_row(segments: list[dict], width: int = width, zone: float = zone,
                       bounds: list[tuple[float, float]] = bounds) -> list[str]:
            """Place header segments by position: label zone -> column 0, else every column they overlap."""
            grid = [""] * width
            for segment in segments:
                if segment["x1"] < zone - 2:
                    grid[0] = join_lines([grid[0], segment["text"]])
                    continue
                hits = [c + 1 for c, (x0, x1) in enumerate(bounds)
                        if min(segment["x1"], x1 + 2) - max(segment["x0"], x0 - 2) > 0]
                for c in hits or [column_of(segment)]:
                    grid[c] = join_lines([grid[c], segment["text"]]) if grid[c] else segment["text"]
            return grid

        header_grid: list[list[str]] = [header_row(line.segments) for line in headers]
        rows: list[_Row] = []
        first_body = True
        for line in block:
            if not _body_like(line):
                rows.append(_Row(line.text.strip(), [{"raw": "", "merged": False, "bbox": None}
                                                     for _ in range(width - 1)], _round_bbox(line.bbox),
                                 number, True, _round_bbox(line.bbox)))
                continue
            label_segment, rest = line.segments[0], line.segments[1:]
            if first_body and not header_grid and all(_YEAR.match(s["text"].strip()) for s in rest):
                # "Notes | 2025 | 2024" (or "项目 | 2025 | 2024"): a header band, placed by position so a
                # title over a value column is not taken for the row label.
                header_grid.append(header_row(line.segments))
                first_body = False
                continue
            first_body = False
            row_cells = [{"raw": "", "merged": False, "bbox": None} for _ in range(width - 1)]
            for segment in rest:
                c = column_of(segment) - 1
                if row_cells[c]["raw"]:  # two printed values claim one column: keep both, never drop
                    row_cells[c]["raw"] += " " + segment["text"]
                    row_cells[c]["bbox"] = _round_bbox(_union([row_cells[c]["bbox"], _seg_box(segment)]))
                else:
                    row_cells[c] = {"raw": segment["text"], "merged": False, "bbox": _round_bbox(_seg_box(segment))}
            label, label_box = label_segment["text"].strip(), _round_bbox(_seg_box(label_segment))
            previous = rows[-1] if rows else None
            if (previous is not None and previous.section and re.match(r"[a-z(]", label)
                    and not previous.label.rstrip().endswith((":", "：")) and previous.bbox is not None
                    and line.top - previous.bbox[3] < 1.5 * line.size):
                # An English label wrapped onto the line with the numbers ("Non-IFRS profit attributable to" /
                # "equity holders of the Company | 259,626", Tencent 2025): one row, not a section and a row.
                rows.pop()
                label = previous.label + " " + label
                label_box = _round_bbox(_union([previous.label_bbox or previous.bbox, label_box]))
            rows.append(_Row(label, row_cells, _round_bbox(line.bbox), number, label_bbox=label_box))
        everything = headers + block
        for line in everything:
            line.used = True
        bbox = _union([line.bbox for line in everything])
        column_bounds = [(label_x0, zone)] + bounds
        tables.append(_Table(number, "borderless", _round_bbox(bbox), header_grid,
                             _column_keys(header_grid, width), column_bounds, rows))
        i = free.index(block[-1]) + 1 if block[-1] in free else j
    return tables


def _seg_box(segment: dict) -> list[float]:
    return [segment["x0"], segment["top"], segment["x1"], segment["bottom"]]


def _attach_captions(page: _Page) -> None:
    """Unit lines and the title directly above each table, nearest first; the title ends the caption.

    A caption line is short (not a full-width body line), close to the table and does not end like a
    sentence. Consumed caption lines are not repeated as text chunks.
    """
    for table in page.tables:
        above = sorted((line for line in page.lines if not line.used and line.bottom <= table.bbox[1] + 1),
                       key=lambda line: -line.bottom)
        reference_top = table.bbox[1]
        for line in above[:4]:
            text = line.text.strip()
            if (reference_top - line.bottom > max(16.0, 1.8 * line.size)
                    or line.x1 - line.x0 > 0.62 * page.width):
                break
            unit = detect_unit(text)
            if unit is None and text.endswith(("。", ".", "；", ";", "，", ",")):
                break  # the end of a paragraph, not a title
            line.used = True
            table.caption_lines.insert(0, text)
            reference_top = line.top
            if unit is not None:
                table.unit = table.unit or unit
                continue
            table.title = text
            break
        table.continued_marker = bool(table.title and _CONTINUED.search(table.title))
        if table.unit is None:
            header_text = " ".join(" ".join(row) for row in table.header_rows)
            table.unit = detect_unit(header_text)
        if table.unit is None:  # 营业收入（万元） | 营业成本（万元）: one amount unit declared in the headers
            inline = {u.scale: u for key in table.column_keys[1:] if isinstance(u := _inline_unit(key), Unit)}
            if len(inline) == 1:
                unit = next(iter(inline.values()))
                table.unit = Unit(unit.label, unit.scale, unit.currency, f"column headers {unit.source}")


def _page_status(page: Any, content_chars: list[dict]) -> tuple[str, str, float]:
    area = float(page.width * page.height) or 1.0
    covered = 0.0
    for image in page.images:
        x0, x1 = max(0.0, float(image["x0"])), min(float(page.width), float(image["x1"]))
        top, bottom = max(0.0, float(image["top"])), min(float(page.height), float(image["bottom"]))
        covered += max(0.0, x1 - x0) * max(0.0, bottom - top)
    coverage = min(1.0, covered / area)
    if not content_chars:
        if page.images:
            return "NEEDS_OCR", "no extractable text; page content is an image", coverage
        return "EMPTY", "no text and no images", coverage
    garbled = sum(1 for c in content_chars if str(c.get("text", "")).startswith("(cid:"))
    if garbled / len(content_chars) > 0.3:
        return "NEEDS_OCR", "glyphs without a Unicode mapping (cid codes)", coverage
    return "TEXT", "", coverage


def _analyse(page: Any) -> _Page:
    number, width, height = int(page.page_number), float(page.width), float(page.height)
    band = 0.06 * height
    words = page.extract_words(x_tolerance=3, y_tolerance=3, extra_attrs=["size", "fontname"])
    furniture_words = [w for w in words if w["bottom"] <= band or w["top"] >= height - band]
    content_words = [w for w in words if w not in furniture_words]
    content_chars = [c for c in page.chars if str(c.get("text", "")).strip()
                     and band < (c["top"] + c["bottom"]) / 2 < height - band]
    status, reason, coverage = _page_status(page, content_chars)
    furniture = [line.text for line in _lines(furniture_words)]
    if status != "TEXT":
        return _Page(number, width, height, status, reason, [], [], furniture, len(content_chars), coverage)
    lines = _lines(content_words)
    tables = _ruled_tables(page, number)
    for line in lines:
        if any(_center_inside(line.bbox, t.bbox) for t in tables):
            line.used = True
    tables += _borderless_tables(lines, number)
    tables.sort(key=lambda t: (t.bbox[1], t.bbox[0]))
    result = _Page(number, width, height, status, reason, lines, tables, furniture, len(content_chars), coverage)
    _attach_captions(result)
    for table in tables:
        table.segments = [{"page": number, "bbox": table.bbox, "rows": [0, len(table.rows)]}]
    return result


# --------------------------------------------------------------------------- cross-page stitching


def _content_below(page: _Page, table: _Table) -> bool:
    return any(line.top > table.bbox[3] and not line.used for line in page.lines) or any(
        other.bbox[1] > table.bbox[3] for other in page.tables if other is not table)


def _content_above(page: _Page, table: _Table) -> bool:
    return any(line.bottom < table.bbox[1] and not line.used for line in page.lines) or any(
        other.bbox[3] < table.bbox[1] for other in page.tables if other is not table)


def _columns_align(left: _Table, right: _Table) -> bool:
    if left.style != right.style or len(left.column_keys) != len(right.column_keys):
        return False
    tolerance = 3.0 if left.style == "ruled" else 8.0
    pairs = zip(left.column_bounds[1:], right.column_bounds[1:], strict=True)
    if left.style == "ruled":
        return all(abs(a[0] - b[0]) <= tolerance and abs(a[1] - b[1]) <= tolerance for a, b in pairs)
    return all(abs(a[1] - b[1]) <= tolerance for a, b in pairs)


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or ""))


def _stitch(pages: list[_Page], warnings: list[str]) -> list[_Table]:
    """Join a table that ends a page with the table that starts the next page.

    Required: nothing below the first part, nothing above the second part except a 续/continued caption,
    no new (non-continued) title on the second part, same column count and column positions. A repeated
    header band on the second part is dropped once and counted.
    """
    logical: list[_Table] = []
    open_table: _Table | None = None
    open_page: _Page | None = None
    for page in pages:
        candidate = page.tables[0] if page.tables else None
        if (open_table is not None and open_page is not None and candidate is not None
                and page.number == open_page.number + 1
                and not _content_above(page, candidate)
                and (candidate.title is None or candidate.continued_marker)
                and _columns_align(open_table, candidate)):
            conflict = ""
            if (candidate.unit is not None and open_table.unit is not None
                    and candidate.unit.scale != open_table.unit.scale):
                conflict = "continuation declares a different unit"
            repeated = candidate.header_rows
            if repeated and [[_norm(c) for c in r[1:]] for r in repeated] != [
                    [_norm(c) for c in r[1:]] for r in open_table.header_rows[: len(repeated)]]:
                conflict = "continuation has different column headers"
            if conflict:
                warnings.append(f"page {page.number}: {conflict}; not stitched")
            else:
                dropped = len(repeated)
                label_header = join_lines(r[0] for r in repeated)
                if label_header and _norm(label_header) != _norm(join_lines(r[0] for r in open_table.header_rows)):
                    # e.g. 资产 -> 负债和所有者权益: the new label header is kept as a section row, not dropped
                    width = len(open_table.column_keys)
                    candidate.rows.insert(0, _Row(label_header, [{"raw": "", "merged": False, "bbox": None}
                                                                 for _ in range(width - 1)],
                                                  list(candidate.bbox), page.number, True))
                start = len(open_table.rows)
                open_table.rows.extend(candidate.rows)
                open_table.segments.append({"page": page.number, "bbox": candidate.bbox,
                                            "rows": [start, len(open_table.rows)]})
                open_table.dropped_header_rows += dropped
                page.tables = page.tables[1:]
                remaining = page.tables
                if not remaining and not any(line.top > candidate.bbox[3] and not line.used for line in page.lines):
                    open_page = page
                    continue
                open_table, open_page = None, None
        for table in page.tables:
            logical.append(table)
        last = page.tables[-1] if page.tables else None
        if last is not None and not _content_below(page, last):
            open_table, open_page = last, page
        else:
            open_table, open_page = None, None
    return logical


# --------------------------------------------------------------------------- rows


@dataclass
class IngestResult:
    file: str
    file_sha256: str
    pages: list[dict]
    tables: list[dict]
    rows: list[dict]
    warnings: list[str]

    @property
    def needs_ocr(self) -> list[int]:
        return [p["page"] for p in self.pages if p["status"] == "NEEDS_OCR"]

    def row(self, identifier: str) -> dict:
        return next(r for r in self.rows if r["id"] == identifier)

    def to_dict(self) -> dict:
        return {"file": self.file, "file_sha256": self.file_sha256, "pages": self.pages,
                "tables": self.tables, "rows": self.rows, "warnings": self.warnings,
                "needs_ocr": self.needs_ocr}


def _bbox_text(bbox: list[float]) -> str:
    return "[" + ",".join(f"{v:.1f}" for v in bbox) + "]"


def source_line(file: str, file_sha256: str, pages: list[dict]) -> str:
    places = "; ".join(f"p.{p['page']} {_bbox_text(p['bbox'])}" for p in pages)
    return f"[source] {file} | {places} | sha256 {file_sha256[:16]}"


_SOURCE = re.compile(r"^\[source\] (?P<file>.+?) \| (?P<places>p\.\d+ \[[^\]]+\](?:; p\.\d+ \[[^\]]+\])*) "
                     r"\| sha256 (?P<sha>[0-9a-f]{16})$")


def parse_source_line(content: str) -> dict | None:
    """Recover file, pages and bboxes from a stored row's first line (what a Store hit carries)."""
    first = (content or "").split("\n", 1)[0]
    match = _SOURCE.match(first)
    if not match:
        return None
    pages = [{"page": int(m.group(1)), "bbox": [float(v) for v in m.group(2).split(",")]}
             for m in re.finditer(r"p\.(\d+) \[([^\]]+)\]", match.group("places"))]
    return {"file": match.group("file"), "file_sha256_prefix": match.group("sha"), "pages": pages}


_SECTION_TOTAL = re.compile(r"^total\b|(?:合计|总计|小计)$", re.I)
_NOTE_HEADER = re.compile(r"^(?:notes?|附注|注释|注)$", re.I)


def _note_cell(raw: str) -> dict:
    """附注 / Notes cells are references (11, 五、41), never amounts: kept as text, never scaled."""
    kind = "text" if (raw or "").strip() else "empty"
    return {"raw": raw, "kind": kind, "value": None, "unit": None, "scale": None, "base_value": None,
            "currency": None}


def _table_dict(table: _Table, identifier: str) -> dict:
    keys = table.column_keys
    notes = {j for j, key in enumerate(keys) if j and _NOTE_HEADER.match(_norm(key))}
    rows = []
    section = ""
    for index, row in enumerate(table.rows):
        if row.section:
            section = row.label
        context = f"{section} {row.label}" if not row.section else row.label
        row_section = section if not row.section else ""
        if not row.section and section and _SECTION_TOTAL.search(row.label.strip()):
            section = ""  # 流动资产合计 / Total revenue closes its section
        cells = []
        for offset, cell in enumerate(row.cells, start=1):
            if offset in notes:
                normalised = _note_cell(cell["raw"])
            else:
                normalised = normalise_cell(cell["raw"], table.unit, context, keys[offset] if offset < len(keys) else "")
            if cell.get("merged"):
                normalised["kind"] = "merged"
            cells.append({"col": offset, "column": keys[offset] if offset < len(keys) else "",
                          **normalised, "bbox": cell["bbox"], "page": row.page})
        rows.append({"index": index, "label": row.label, "section": row_section, "is_section": row.section,
                     "page": row.page, "bbox": row.bbox, "label_bbox": row.label_bbox, "cells": cells})
    roles = ["label"]
    for j in range(1, len(keys)):
        kinds = [c["kind"] for r in rows for c in r["cells"] if c["col"] == j and c["kind"] not in {"empty", "merged"}]
        numeric = sum(k in {"number", "percent"} for k in kinds)
        roles.append("note" if j in notes else "value" if kinds and numeric * 2 >= len(kinds) else "text")
    return {
        "id": identifier,
        "style": table.style,
        "title": table.title,
        "caption_lines": table.caption_lines,
        "unit": table.unit.as_dict() if table.unit else None,
        "header_rows": table.header_rows,
        "columns": [{"index": j, "key": key, "role": roles[j]} for j, key in enumerate(keys)],
        "rows": rows,
        "segments": table.segments,
        "continued": len(table.segments) > 1,
        "dropped_repeated_header_rows": table.dropped_header_rows,
    }


def table_text(table: dict) -> str:
    """Compact retrieval text: title, unit, header keys, then one ``label | raw | raw`` line per row."""
    unit = table["unit"]
    head = f"[table] {table['title'] or '(untitled)'}"
    if unit:
        head += f" | {unit['source']} (x{unit['scale']} {unit['currency'] or ''})".rstrip()
        if unit["except_per_share"]:
            head += " except per share"
    lines = [head, " | ".join(c["key"] for c in table["columns"])]
    for row in table["rows"]:
        if row["is_section"]:
            lines.append(row["label"])
        else:
            lines.append(" | ".join([row["label"]] + [c["raw"] for c in row["cells"]]))
    return "\n".join(lines)


def _text_chunks(page: _Page, max_chars: int) -> list[tuple[str, list[float]]]:
    lines = [line for line in page.lines if not line.used]
    if not lines:
        return []
    body_size = statistics.median(line.size for line in lines)
    chunks: list[list[_Line]] = []
    current: list[_Line] = []
    only_headings = True
    previous: _Line | None = None
    for line in lines:
        heading = (line.size >= 1.12 * body_size or line.bold) and line.x1 - line.x0 < 0.75 * page.width
        new_paragraph = previous is None or line.top - previous.bottom > 0.9 * previous.size or heading or (
            previous.size >= 1.12 * body_size)
        if new_paragraph and current and not only_headings:  # headings stay with the paragraph they open
            chunks.append(current)
            current, only_headings = [], True
        if current and sum(len(x.text) for x in current) + len(line.text) > max_chars:
            chunks.append(current)
            current, only_headings = [], True
        current.append(line)
        only_headings = only_headings and heading
        previous = line
    if current:
        chunks.append(current)
    result = []
    for chunk in chunks:
        parts: list[str] = []
        paragraph: list[str] = []
        prior: _Line | None = None
        for line in chunk:
            if prior is not None and (line.top - prior.bottom > 0.9 * prior.size or prior.size >= 1.12 * body_size
                                      or prior.bold):
                parts.append(join_lines(paragraph))
                paragraph = []
            paragraph.append(line.text)
            prior = line
        parts.append(join_lines(paragraph))
        result.append(("\n".join(p for p in parts if p), _round_bbox(_union(line.bbox for line in chunk))))
    return result


def _require_pdfplumber():
    try:
        import pdfplumber
    except ImportError as error:  # pragma: no cover - exercised only without the extra
        raise DomainError("PDF_EXTRA_MISSING", "Install the PDF extra: pip install 'forgeflow[pdf]'", 501) from error
    return pdfplumber


def _doc_key(path: Path, doc_key: str | None) -> str:
    key = doc_key or re.sub(r"[^\w.-]+", "-", path.stem).strip("-")
    key = key[:80] or "pdf"
    if not re.fullmatch(r"[\w.-]{1,80}", key):
        raise DomainError("INVALID_DOC_KEY", "doc_key must be 1-80 word characters, dots or dashes", 422)
    return key


def ingest_pdf(path: str | Path, *, scope: Scope, roles: Iterable[str], classification: int,
               valid_until: str | None = None, doc_key: str | None = None,
               max_chunk_chars: int = 900) -> IngestResult:
    """Parse one PDF into Knowledge rows stamped with scope, roles, classification and a content hash."""
    role_list = sorted({str(r) for r in roles if str(r)})
    if not role_list:
        raise DomainError("ROLES_REQUIRED", "Every Knowledge row needs at least one role", 422)
    if isinstance(classification, bool) or not isinstance(classification, int) or not 0 <= classification <= 3:
        raise DomainError("INVALID_CLASSIFICATION", "classification must be an integer 0-3", 422)
    pdfplumber = _require_pdfplumber()
    path = Path(path)
    data = path.read_bytes()
    file_sha = hashlib.sha256(data).hexdigest()
    key = _doc_key(path, doc_key)
    version = file_sha[:16]
    warnings: list[str] = []
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:  # parse exactly the bytes that were hashed
            pages = [_analyse(page) for page in pdf.pages]
    except DomainError:
        raise
    except Exception as error:  # noqa: BLE001 - any parser failure is one closed error for the caller
        raise DomainError("PDF_UNREADABLE", f"{path.name}: {type(error).__name__}", 422) from error
    first_index: dict[int, int] = {}
    for page in pages:
        for table in page.tables:
            first_index[id(table)] = len([t for t in page.tables if t.bbox[1] < table.bbox[1]]) + 1
    logical = _stitch(pages, warnings)
    scope_fields = scope.model_dump()
    base = {"version": version, "roles": role_list, "classification": classification,
            "valid_until": valid_until, "status": "CURRENT", **scope_fields}

    def make_row(identifier: str, kind: str, places: list[dict], body: str, **extra: Any) -> dict:
        content = source_line(path.name, file_sha, places) + "\n" + body
        citation = {"file": path.name, "file_sha256": file_sha, "page": places[0]["page"],
                    "bbox": places[0]["bbox"], "pages": places}
        return {"id": identifier, **base, "kind": kind, "content": content,
                "source_hash": hashlib.sha256(content.encode()).hexdigest(), "citation": citation, **extra}

    rows: list[dict] = []
    tables: list[dict] = []
    for table in logical:
        identifier = f"{key}#p{table.page}-t{first_index[id(table)]}"
        structured = _table_dict(table, identifier)
        tables.append(structured)
        places = [{"page": s["page"], "bbox": s["bbox"]} for s in table.segments]
        rows.append(make_row(identifier, "table", places, table_text(structured), table=structured))
    for page in pages:
        for ordinal, (text, bbox) in enumerate(_text_chunks(page, max_chunk_chars), start=1):
            rows.append(make_row(f"{key}#p{page.number}-c{ordinal}", "text",
                                 [{"page": page.number, "bbox": bbox}], text))
    rows.sort(key=lambda r: (r["citation"]["page"], r["citation"]["bbox"][1], r["id"]))
    for row in rows:
        if len(row["id"]) > 128:
            raise DomainError("INVALID_DOC_KEY", "row id exceeds the Store's 128 characters", 422)
    page_reports = []
    for page in pages:
        page_reports.append({
            "page": page.number, "width": round(page.width, 2), "height": round(page.height, 2),
            "status": page.status, "reason": page.reason, "content_chars": page.content_chars,
            "image_coverage": round(page.image_coverage, 4), "furniture": page.furniture,
            "rows": [r["id"] for r in rows if any(p["page"] == page.number for p in r["citation"]["pages"])],
        })
    return IngestResult(path.name, file_sha, page_reports, tables, rows, warnings)


def ingest_into_store(store: Any, principal: Any, scope: Scope, result: IngestResult) -> list[dict]:
    """Write rows through ``Store.put_document`` (the existing write path) and check the stored hash.

    The Store recomputes ``source_hash`` from the content it stored; a mismatch fails closed. Re-ingesting
    the same file is idempotent (same id, version and content); a changed file is a new version and the
    previous one becomes SUPERSEDED for citation.
    """
    receipts = []
    for row in result.rows:
        if (row["tenant_id"], row["workspace_id"], row["project_id"]) != scope.key():
            raise DomainError("SCOPE_MISMATCH", "Rows were stamped for a different scope", 409)
        receipt = store.put_document(principal, scope, row["id"], row["version"], row["content"],
                                     set(row["roles"]), row["classification"], row["valid_until"])
        if receipt["source_hash"] != row["source_hash"]:
            raise DomainError("SOURCE_HASH_MISMATCH", f"{row['id']}: stored content differs", 409)
        receipts.append(receipt)
    return receipts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Parse a financial PDF into citable Knowledge rows (JSON).")
    parser.add_argument("pdf")
    parser.add_argument("--tenant", default="local")
    parser.add_argument("--workspace", default="local")
    parser.add_argument("--project", default="finance")
    parser.add_argument("--roles", default="finance", help="comma-separated roles that may read the rows")
    parser.add_argument("--classification", type=int, default=1)
    parser.add_argument("--summary", action="store_true", help="print a per-page summary instead of all rows")
    args = parser.parse_args(argv)
    scope = Scope(tenant_id=args.tenant, workspace_id=args.workspace, project_id=args.project)
    result = ingest_pdf(args.pdf, scope=scope, roles=args.roles.split(","), classification=args.classification)
    if args.summary:
        output: Any = {"file": result.file, "file_sha256": result.file_sha256, "needs_ocr": result.needs_ocr,
                       "warnings": result.warnings,
                       "pages": [{k: p[k] for k in ("page", "status", "reason", "rows")} for p in result.pages],
                       "tables": [{"id": t["id"], "title": t["title"], "unit": t["unit"], "rows": len(t["rows"]),
                                   "pages": [s["page"] for s in t["segments"]]} for t in result.tables]}
    else:
        output = result.to_dict()
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
