"""Two reproducible financial ratios, calculated from an authorized Apple net sales table."""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

DOCUMENT = "apple_fy2025_10k_en"
VERSION = "apple-services-v1"
QUESTION = re.compile(r"(?:Apple|苹果)(?:FY)?(20\d{2})(?:财年|年)?服务收入(增速和增长贡献|增速|增长贡献)[？?。]?", re.I)
_NUM = re.compile(r"(?:\d{1,3}(?:,\d{3})+|\d+)")


@dataclass(frozen=True)
class Sales:
    current_services: int
    prior_services: int
    current_total: int
    prior_total: int


def request(question: str) -> tuple[int, str] | None:
    match = QUESTION.fullmatch("".join(question.split()))
    return (int(match[1]), match[2]) if match else None


def read_sales(identifier: str, content: str, year: int) -> Sales | None:
    if identifier.partition("#")[0] != DOCUMENT:
        return None
    lines = content.splitlines()
    if len(lines) < 5 or not lines[1].startswith("[table] ") or "in millions (x1000000 " not in lines[1]:
        return None
    headers = [c.strip() for c in lines[2].split("|")]
    periods = [str(year), str(year - 1)]
    if any(headers.count(p) != 1 for p in periods):
        return None
    positions = [headers.index(p) for p in periods]
    figures = {}
    for line in lines[3:]:
        cells = [c.strip() for c in line.split("|")]
        label = cells[0].removesuffix(" (1)")
        if label not in {"Services", "Total net sales"}:
            continue
        if label in figures or len(cells) != len(headers):
            return None
        numbers = [cells[pos].removeprefix("$").strip() for pos in positions]
        if not all(_NUM.fullmatch(n) for n in numbers):
            return None
        figures[label] = [int(n.replace(",", "")) for n in numbers]
    if "Services" in figures and "Total net sales" in figures:
        return Sales(*figures["Services"], *figures["Total net sales"])
    return None


def calculation(sales: Sales, year: int, intent: str) -> dict:
    change = sales.current_services - sales.prior_services
    total_change = sales.current_total - sales.prior_total
    operations = [
        ("增速", "服务收入增速", "服务收入增量 / 上期服务收入", sales.prior_services),
        ("增长贡献", "服务对公司收入增量的贡献", "服务收入增量 / 公司收入增量", total_change),
    ]
    rows = []
    for key, label, formula, denominator in operations:
        if key != intent and intent != "增速和增长贡献":
            continue
        pct = None if denominator == 0 else format(Decimal(change) * 100 / Decimal(denominator), ".2f")
        rows.append({"metric": label, "formula": formula, "numerator": change, "denominator": denominator,
                     "percent": pct})
    return {"periods": [year, year-1], "unit": "百万美元",
            "services": [sales.current_services, sales.prior_services],
            "total_sales": [sales.current_total, sales.prior_total], "operations": rows}


def describe(result: dict) -> str:
    values = []
    for row in result["operations"]:
        value = "分母为零，比例无定义" if row["percent"] is None else row["percent"] + "%"
        values.append(f"{row['metric']}为{value}（{row['numerator']:,} / {row['denominator']:,}）")
    return f"Apple FY{result['periods'][0]}，" + "；".join(values) + "。金额单位为百万美元。"
