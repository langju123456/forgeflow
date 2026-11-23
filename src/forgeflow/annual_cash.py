"""Look up an explicitly dated cash-flow cell in the desk's published CATL annual report."""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from .contracts import ContextHit

VERSION = "annual-cash-v1"
DOCUMENT = "catl_2025_annual_zh"
METRIC = "经营活动产生的现金流量净额"
_QUESTION = re.compile(
    rf"(?:请问)?宁德时代(?:公司)?(?:的)?(?P<year>20\d{{2}})年(?:度|全年)?(?:的)?"
    rf"(?:{METRIC}|经营现金流净额)(?:是多少|为多少|多少)?[？?。]?"
)
_AMOUNT = re.compile(r"-?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?")


def requested_year(question: str) -> str | None:
    match = _QUESTION.fullmatch("".join(question.split()))
    return match["year"] if match else None


@dataclass(frozen=True)
class CashCell:
    year: str
    raw: str


def read_cell(identifier: str, content: str, year: str) -> CashCell | None:
    """Read the label, selected year, numeral and unit from the same citable table."""
    if identifier.partition("#")[0] != DOCUMENT:
        return None
    lines = content.splitlines()
    if len(lines) < 4 or not lines[1].startswith("[table] "):
        return None
    title = lines[1]
    if "母公司" in title or "季度" in title or "单位:千元 (x1000 CNY)" not in title:
        return None
    header = [col.strip() for col in lines[2].split("|")]
    if header[0] != "项目" or header.count(year + "年") != 1:
        return None
    column = header.index(year + "年")
    matches = []
    for line in lines[3:]:
        cells = [col.strip() for col in line.split("|")]
        if cells[0] == "项目":  # a new block with its own column headers
            break
        if cells[0] == METRIC:
            if len(cells) != len(header) or not _AMOUNT.fullmatch(cells[column]):
                return None
            matches.append(CashCell(year, cells[column]))
    return matches[0] if len(matches) == 1 else None


def annual_ranker(question: str, rows: list[dict]) -> list[tuple[float, dict]]:
    """Called by RankedKnowledgeService after the existing scope/role filter."""
    year = requested_year(question)
    if not year:
        return []
    first_by_value = {}
    repeated = []
    for row in sorted(rows, key=lambda item: item["id"]):
        cell = read_cell(row["id"], row["content"], year)
        if cell:
            key = cell.raw.replace(",", "")
            if key not in first_by_value:
                first_by_value[key] = (1.0, row)
            else:
                repeated.append((1.0, row))
    # Preserve a witness of each distinct value ahead of same-value duplicates.
    return [*first_by_value.values(), *repeated]


def differing_values(hits: Sequence[ContextHit], year: str) -> bool:
    cells = (read_cell(h.id, h.content, year) for h in hits)
    return len({c.raw.replace(",", "") for c in cells if c}) > 1


def answer_from_period(question: str, hits: Sequence[ContextHit]) -> dict | None:
    year = requested_year(question)
    if not year:
        return None
    evidence = []
    for hit in hits:
        cell = read_cell(hit.id, hit.content, year)
        if cell and hit.status == "CURRENT":
            evidence.append((hit.id, cell))
    values = {cell.raw.replace(",", "") for _, cell in evidence}
    if len(values) != 1:
        return {"status": "PERIOD_UNVERIFIED", "found": False, "model": VERSION,
                "answer": "待核对：当前可用证据无法确定该年度金额。请核对年度列表头、人民币单位和原表。",
                "citations": []}
    return {"status": "ANSWERED", "found": True, "model": VERSION,
            "answer": f"宁德时代 {year} 年全年{METRIC}为 {evidence[0][1].raw} 千元人民币。"
                      f"取自 2025 年报的“{METRIC}”行、“{year}年”列。",
            "citations": [identifier for identifier, _ in evidence]}
