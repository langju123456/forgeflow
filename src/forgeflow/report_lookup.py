"""Dispatch explicit desk questions to read-only, citable numerical tools."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from . import annual_cash, apple_sales
from .contracts import ContextHit


@dataclass(frozen=True)
class Lookup:
    year: str
    sales_intent: str | None = None

    @property
    def gate(self) -> str:
        return "SALES_CALCULATION" if self.sales_intent else "ANNUAL_COLUMN"

    def value(self, identifier: str, content: str):
        if self.sales_intent:
            return apple_sales.read_sales(identifier, content, int(self.year))
        cell = annual_cash.read_cell(identifier, content, self.year)
        return cell.raw.replace(",", "") if cell else None

    def rank(self, question: str, rows: list[dict]) -> list[tuple[float, dict]]:
        distinct = {}
        repeats = []
        for row in sorted(rows, key=lambda item: item["id"]):
            value = self.value(row["id"], row["content"])
            if value is None:
                continue
            if value in distinct:
                repeats.append((1.0, row))
            else:
                distinct[value] = (1.0, row)
        return [*distinct.values(), *repeats]

    def conflicting(self, hits: Sequence[ContextHit]) -> bool:
        return len({self.value(h.id, h.content) for h in hits}) > 1

    def answer(self, question: str, hits: Sequence[ContextHit]) -> dict:
        if not self.sales_intent:
            return annual_cash.answer_from_period(question, hits)
        evidence = {h.id: self.value(h.id, h.content) for h in hits if h.status == "CURRENT"}
        values = set(evidence.values())
        if len(values) != 1 or None in values:
            return {"status": "PERIOD_UNVERIFIED", "found": False, "model": apple_sales.VERSION,
                    "answer": "待核对：需要同一披露单位下两年的服务收入和总收入。", "citations": []}
        result = apple_sales.calculation(values.pop(), int(self.year), self.sales_intent)
        return {"status": "ANSWERED", "found": True, "model": apple_sales.VERSION, "calculation": result,
                "answer": apple_sales.describe(result), "citations": list(evidence)}


def for_question(question: str) -> Lookup | None:
    year = annual_cash.requested_year(question)
    if year:
        return Lookup(year)
    sales = apple_sales.request(question)
    if sales:
        return Lookup(str(sales[0]), sales[1])
    return None
