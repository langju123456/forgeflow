from decimal import Decimal

import pytest

from forgeflow import apple_sales
from forgeflow.contracts import ContextHit
from forgeflow.report_lookup import for_question


def source(services="90", prior="100", total="210", old_total="200"):
    return ("source\n[table] categories | in millions (x1000000 )\n | 2025 | 2024\n"
            f"Services (1) | {services} | {prior}\nTotal net sales | $ {total} | $ {old_total}")


def hit(body):
    return ContextHit(id=apple_sales.DOCUMENT + "#p1-t1", version="1", source_hash="a"*64,
                      content=body, classification=1, status="CURRENT")


def test_growth_and_contribution_can_have_different_signs_and_magnitudes():
    q = "Apple FY2025 服务收入增速和增长贡献"
    result = for_question(q).answer(q, [hit(source(total="195"))])
    ops = result["calculation"]["operations"]
    assert [(o["numerator"], o["denominator"], o["percent"]) for o in ops] == [
        (-10, 100, "-10.00"), (-10, -5, "200.00")]
    assert result["citations"] == [apple_sales.DOCUMENT + "#p1-t1"]


@pytest.mark.parametrize("intent", ["增速", "增长贡献"])
def test_only_requested_denominator_is_used_and_zero_is_explicit(intent):
    q = f"苹果2025财年服务收入{intent}"
    lookup = for_question(q)
    result = lookup.answer(q, [hit(source(prior="0", total="200"))])
    assert len(result["calculation"]["operations"]) == 1
    assert result["calculation"]["operations"][0]["percent"] is None and "无定义" in result["answer"]


def test_unit_company_period_and_conflicts_are_checked():
    q = "Apple FY2025 服务收入增速"
    lookup = for_question(q)
    assert lookup.value("other#p1-t1", source()) is None
    assert lookup.value(apple_sales.DOCUMENT + "#p1-t1", source().replace("in millions", "unknown")) is None
    assert lookup.value(apple_sales.DOCUMENT + "#p1-t1", source().replace("2024", "Change")) is None
    assert lookup.conflicting([hit(source()), hit(source("91"))])
    assert lookup.answer(q, [])["status"] == "PERIOD_UNVERIFIED"
    assert for_question("苹果2025财年服务净利润增速") is None


def test_percent_rounds_only_after_division():
    result = apple_sales.calculation(apple_sales.Sales(109158, 96169, 416161, 391035), 2025, "增长贡献")
    assert Decimal(result["operations"][0]["percent"]) == Decimal("51.70")
