"""Financial PDF ingestion: number/unit normalisation, Knowledge row contract, Store + KnowledgeService path.

Normalisation tests always run. PDF tests need the extras (``pip install -e '.[pdf,pdf-fixtures]'``) and skip
cleanly without them. Fixture PDFs are generated per test session from fixtures/financial-pdf/ground_truth.json.
"""

from __future__ import annotations

import hashlib
import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest

from forgeflow.context import KnowledgeService
from forgeflow.contracts import DomainError, Principal, Scope
from forgeflow.pdf_ingest import (
    _page_status,
    detect_unit,
    ingest_into_store,
    normalise_cell,
    parse_number,
    parse_source_line,
)

ROOT = Path(__file__).resolve().parents[1]
SCOPE = Scope(tenant_id="fin-test", workspace_id="reports", project_id="cc-2025")
OTHER = Scope(tenant_id="fin-test", workspace_id="reports", project_id="other")
ADMIN = Principal(subject_id="doc-admin", kind="user", roles=frozenset({"admin", "finance"}), scopes=(SCOPE,),
                  clearance=3)
READER = Principal(subject_id="finance-agent", kind="agent", roles=frozenset({"finance"}), scopes=(SCOPE,),
                   clearance=1)
NO_ROLE = Principal(subject_id="pm-agent", kind="agent", roles=frozenset({"pm"}), scopes=(SCOPE,), clearance=3)
LOW = Principal(subject_id="intern", kind="agent", roles=frozenset({"finance"}), scopes=(SCOPE,), clearance=0)
ZH, EN, Q3, HK = ("cc_annual_report_2025_zh.pdf", "cc_form10k_2025_en.pdf", "cc_q3_2025_report_zh.pdf",
                  "cc_interim_2025_hk_en.pdf")


# --------------------------------------------------------------------------- numbers (no PDF extra needed)


@pytest.mark.parametrize(
    ("raw", "kind", "value"),
    [
        ("1,234.56", "number", "1234.56"),
        ("128,456.32", "number", "128456.32"),
        ("(1,234)", "number", "-1234"),
        ("（1,234.00）", "number", "-1234.00"),  # full-width parentheses
        ("-1,234", "number", "-1234"),
        ("－215.40", "number", "-215.40"),  # full-width minus
        ("−2,316", "number", "-2316"),  # Unicode minus sign
        ("$ 152,340", "number", "152340"),
        ("$(2,316)", "number", "-2316"),
        ("¥1,000", "number", "1000"),
        ("0.00", "number", "0.00"),
        ("12.5%", "percent", "12.5"),
        ("(3.1)%", "percent", "-3.1"),
        ("(3.1%)", "percent", "-3.1"),
        ("-17.4%", "percent", "-17.4"),
        ("100.00％", "percent", "100.00"),  # full-width percent sign
        ("—", "empty", None),
        ("-", "empty", None),
        ("–", "empty", None),
        ("", "empty", None),
        ("   ", "empty", None),
        ("n/m", "text", None),
        ("不适用", "text", None),
        ("五、41", "text", None),
        ("增加1.24个百分点", "text", None),
        ("1,23.4", "unparsed", None),  # thousands groups must be three digits
        ("1234,567", "unparsed", None),
        ("12 34", "unparsed", None),  # two numbers in one cell are not joined
        ("(-5)", "unparsed", None),  # double negative is not guessed
        ("1,234.5.6", "unparsed", None),
    ],
)
def test_parse_number(raw, kind, value):
    parsed = parse_number(raw)
    assert parsed.kind == kind
    assert parsed.raw == raw  # the printed string is always kept
    assert (None if parsed.value is None else parsed.value) == (None if value is None else Decimal(value))


def test_parse_number_records_currency():
    assert parse_number("$ 10,854").currency == "USD"
    assert parse_number("¥1,000").currency == "CNY"
    assert parse_number("1,000").currency is None


@pytest.mark.parametrize(
    ("text", "label", "scale", "currency", "per_share"),
    [
        ("单位：万元", "万元", 10_000, "CNY", False),
        ("单位:元", "元", 1, "CNY", False),
        ("金额单位：人民币千元", "千元", 1000, "CNY", False),
        ("单位：亿元", "亿元", 100_000_000, "CNY", False),
        ("单位：百万元", "百万元", 1_000_000, "CNY", False),
        ("单位：万美元", "万美元", 10_000, "USD", False),
        ("单位：美元", "美元", 1, "USD", False),
        ("(In thousands of U.S. dollars, except per share data)", "thousand", 1000, "USD", True),
        ("(in millions)", "million", 1_000_000, None, False),
        ("($ in billions)", "billion", 1_000_000_000, "USD", False),
        ("(Amounts in RMB'000, except per share data)", "thousand", 1000, "CNY", True),
    ],
)
def test_detect_unit(text, label, scale, currency, per_share):
    unit = detect_unit(text)
    assert unit is not None
    assert (unit.label, unit.scale, unit.currency, unit.except_per_share) == (label, Decimal(scale), currency,
                                                                              per_share)


def test_detect_unit_returns_none_without_a_declaration():
    assert detect_unit("合并利润表") is None
    assert detect_unit("Consolidated Statements of Operations") is None


def test_normalise_cell_applies_the_governing_unit():
    wan = detect_unit("单位：万元")
    amount = normalise_cell("128,456.32", wan, "营业收入")
    assert (amount["value"], amount["scale"], amount["base_value"]) == ("128456.32", "10000", "1284563200.00")
    assert amount["raw"] == "128,456.32" and amount["unit"] == "万元"
    # percentages are never scaled by the table unit
    assert normalise_cell("56.32%", wan, "智能装备")["base_value"] == "0.5632"
    # a unit in the row label wins over the caption
    eps = normalise_cell("0.85", wan, "基本每股收益（元/股）")
    assert (eps["scale"], eps["base_value"]) == ("1", "0.85")
    # "(%)" in a column header makes bare numbers percentages
    assert normalise_cell("14.38", wan, "营业收入", "本年比上年增减(%)")["base_value"] == "0.1438"
    # a unit in a column header applies when the caption has none
    assert normalise_cell("98,765.43", None, "境内", "营业收入（万元）")["base_value"] == "987654300.00"
    # "except per share data" keeps per-share rows as printed
    usd = detect_unit("(In thousands of U.S. dollars, except per share data)")
    assert normalise_cell("$ 0.42", usd, "Net income per share: Basic")["base_value"] == "0.42"
    assert normalise_cell("$ 10,854", usd, "Net income")["base_value"] == "10854000"
    # a cell that does not parse stays, with its raw string and no value
    kept = normalise_cell("1,23.4", wan, "x")
    assert (kept["kind"], kept["raw"], kept["base_value"]) == ("unparsed", "1,23.4", None)


class _Page:
    def __init__(self, images=(), width=595.0, height=842.0):
        self.images, self.width, self.height = list(images), width, height


def test_page_status_flags_image_only_garbled_and_empty_pages():
    scan = {"x0": 0, "x1": 595, "top": 0, "bottom": 842}
    assert _page_status(_Page([scan]), [])[0] == "NEEDS_OCR"
    assert _page_status(_Page([]), [])[0] == "EMPTY"
    garbled = [{"text": "(cid:123)"}] * 8 + [{"text": "a"}] * 2
    status, reason, _ = _page_status(_Page([]), garbled)
    assert status == "NEEDS_OCR" and "cid" in reason
    assert _page_status(_Page([]), [{"text": "营"}] * 10)[0] == "TEXT"


# --------------------------------------------------------------------------- PDFs (need the extras)


@pytest.fixture(scope="module")
def pdfs(tmp_path_factory):
    pytest.importorskip("pdfplumber")
    pytest.importorskip("reportlab")
    pytest.importorskip("PIL")
    spec = importlib.util.spec_from_file_location("make_financial_pdfs", ROOT / "scripts/make_financial_pdfs.py")
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    folder = tmp_path_factory.mktemp("financial-pdfs")
    layout = generator.generate(folder)
    return folder, layout


@pytest.fixture(scope="module")
def ingested(pdfs):
    from forgeflow.pdf_ingest import ingest_pdf

    folder, _ = pdfs
    return {name: ingest_pdf(folder / name, scope=SCOPE, roles=["finance"], classification=1)
            for name in (ZH, EN, Q3, HK)}


def _table(result, title):
    return next(t for t in result.tables if t["title"] == title)


def _cell(table, label, column):
    row = next(r for r in table["rows"] if r["label"] == label)
    return next(c for c in row["cells"] if c["column"] == column)


def test_every_row_carries_acl_fields_hash_and_citation(ingested):
    rows = [row for result in ingested.values() for row in result.rows]
    assert len(rows) > 10
    for row in rows:
        assert (row["tenant_id"], row["workspace_id"], row["project_id"]) == SCOPE.key()
        assert row["roles"] == ["finance"] and row["classification"] == 1 and row["valid_until"] is None
        assert row["status"] == "CURRENT" and row["version"]
        assert row["source_hash"] == hashlib.sha256(row["content"].encode()).hexdigest()
        citation = row["citation"]
        assert citation["file"].endswith(".pdf") and len(citation["file_sha256"]) == 64
        assert isinstance(citation["page"], int) and len(citation["bbox"]) == 4
        assert citation["pages"][0] == {"page": citation["page"], "bbox": citation["bbox"]}
        x0, top, x1, bottom = citation["bbox"]
        assert x0 < x1 and top < bottom
        # the citation survives inside the hashed content, so a Store row alone still names file/page/bbox
        parsed = parse_source_line(row["content"])
        assert parsed["file"] == citation["file"] and citation["file_sha256"].startswith(parsed["file_sha256_prefix"])
        assert [p["page"] for p in parsed["pages"]] == [p["page"] for p in citation["pages"]]
    assert len({row["id"] for row in rows}) == len(rows)


def test_image_only_page_is_flagged_and_yields_no_rows(ingested):
    result = ingested[ZH]
    assert result.needs_ocr == [5]
    page = next(p for p in result.pages if p["page"] == 5)
    assert page["status"] == "NEEDS_OCR" and page["rows"] == [] and page["image_coverage"] > 0.9
    assert all(p["status"] == "TEXT" for p in result.pages if p["page"] != 5)


def test_tables_are_structured_with_units_and_raw_values(ingested):
    income = _table(ingested[ZH], "合并利润表")
    assert income["unit"]["label"] == "万元" and income["unit"]["scale"] == "10000"
    assert [c["key"] for c in income["columns"]] == ["项目", "附注", "2025年度", "2024年度"]
    assert [c["role"] for c in income["columns"]] == ["label", "note", "value", "value"]
    finance = _cell(income, "财务费用", "2025年度")
    assert (finance["raw"], finance["base_value"]) == ("(627.62)", "-6276200.00")
    wrapped = _cell(income, "其中：对联营企业和合营企业的投资收益", "2024年度")  # a two-line label cell
    assert wrapped["kind"] == "empty" and wrapped["raw"] == "—"
    mix = _table(ingested[ZH], "营业收入构成")  # merged two-level header
    assert [c["key"] for c in mix["columns"]][1:3] == ["2025年度 / 金额", "2025年度 / 占比"]
    assert _cell(mix, "智能装备", "2025年度 / 占比")["base_value"] == "0.5632"
    ops = _table(ingested[EN], "Consolidated Statements of Operations")  # borderless
    assert ops["style"] == "borderless" and ops["unit"]["except_per_share"] is True
    assert _cell(ops, "Interest expense", "Year Ended December 31, / 2025")["base_value"] == "-2316000"
    assert _cell(ops, "Basic", "Year Ended December 31, / 2025")["base_value"] == "0.42"
    assert _cell(ops, "Restructuring charges", "% Change")["kind"] == "text"


def test_cross_page_tables_are_stitched_and_new_titles_are_not(ingested):
    balance = _table(ingested[ZH], "合并资产负债表")
    assert [s["page"] for s in balance["segments"]] == [2, 3]
    assert balance["dropped_repeated_header_rows"] == 1 and len(balance["rows"]) == 41
    assert [p["page"] for p in next(r for r in ingested[ZH].rows if r["id"] == balance["id"])["citation"]["pages"]] \
        == [2, 3]
    assert [s["page"] for s in _table(ingested[ZH], "合并利润表")["segments"]] == [1]  # p2 opens a new title
    ops = _table(ingested[EN], "Consolidated Statements of Operations")
    assert [s["page"] for s in ops["segments"]] == [1, 2] and len(ops["rows"]) == 22
    cash = _table(ingested[Q3], "合并现金流量表（节选）")  # section header orphaned at the page bottom
    assert [s["page"] for s in cash["segments"]] == [1, 2] and len(cash["rows"]) == 14
    profit = _table(ingested[HK], "Condensed Consolidated Statement of Profit or Loss")  # "(continued)" + header
    assert [s["page"] for s in profit["segments"]] == [1, 2] and profit["dropped_repeated_header_rows"] == 1


def test_note_reference_columns_are_never_amounts(ingested):
    position = _table(ingested[HK], "Condensed Consolidated Statement of Financial Position")
    note = _cell(position, "Property, plant and equipment", "Notes")
    assert (note["kind"], note["raw"], note["base_value"]) == ("text", "11", None)
    profit = _table(ingested[HK], "Condensed Consolidated Statement of Profit or Loss")
    assert [c["key"] for c in profit["columns"]] == ["", "Notes", "2025", "2024"]
    assert _cell(profit, "Revenue", "Notes")["base_value"] is None
    assert _cell(profit, "Revenue", "2025")["base_value"] == "1284563000"


def test_header_and_footer_are_not_text_evidence(ingested):
    for result in ingested.values():
        furniture = {line for page in result.pages for line in page["furniture"]}
        assert furniture
        for row in result.rows:
            assert not any(line in row["content"] for line in furniture)


def test_invalid_acl_inputs_are_refused(pdfs):
    from forgeflow.pdf_ingest import ingest_pdf

    folder, _ = pdfs
    with pytest.raises(DomainError) as error:
        ingest_pdf(folder / EN, scope=SCOPE, roles=[], classification=1)
    assert error.value.code == "ROLES_REQUIRED"
    with pytest.raises(DomainError) as error:
        ingest_pdf(folder / EN, scope=SCOPE, roles=["finance"], classification=4)
    assert error.value.code == "INVALID_CLASSIFICATION"


@pytest.fixture
def store(tmp_path):
    from forgeflow.evt_store import EVTBoundStore

    store = EVTBoundStore(f"sqlite:///{tmp_path / 'pdf.sqlite'}")
    store.create_schema()
    return store


def test_end_to_end_store_search_returns_a_cited_table_row(ingested, store):
    result = ingested[ZH]
    receipts = ingest_into_store(store, ADMIN, SCOPE, result)
    assert [r["source_hash"] for r in receipts] == [row["source_hash"] for row in result.rows]
    hits = KnowledgeService(store).search(READER, SCOPE, "货币资金", top_k=5)
    assert hits, "the balance-sheet row must be retrievable"
    top = hits[0]
    balance = _table(result, "合并资产负债表")
    row = result.row(balance["id"])
    assert top.id == balance["id"] and top.status == "CURRENT" and top.classification == 1
    assert top.version == row["version"] and top.source_hash == row["source_hash"]
    cited = parse_source_line(top.content)
    assert cited["file"] == ZH and [p["page"] for p in cited["pages"]] == [2, 3]
    assert result.file_sha256.startswith(cited["file_sha256_prefix"])
    assert [p["bbox"] for p in cited["pages"]] == [[round(v, 1) for v in s["bbox"]] for s in balance["segments"]]
    assert "货币资金 | 五、1 | 45,678.90 | 38,901.23" in top.content
    citation = store.cite(READER, SCOPE, "document", receipts[result.rows.index(row)]["citation_id"])
    assert citation["content"] == row["content"] and citation["status"] == "CURRENT"
    # the same ACL as any Knowledge row: wrong role or too low clearance sees nothing
    assert store.document_rows(NO_ROLE, SCOPE) == [] and store.document_rows(LOW, SCOPE) == []
    assert KnowledgeService(store).search(NO_ROLE, SCOPE, "货币资金") == []
    assert KnowledgeService(store).search(LOW, SCOPE, "货币资金") == []


def test_reingest_is_idempotent(ingested, store):
    first = ingest_into_store(store, ADMIN, SCOPE, ingested[EN])
    second = ingest_into_store(store, ADMIN, SCOPE, ingested[EN])
    assert [r["citation_id"] for r in first] == [r["citation_id"] for r in second]


def test_store_write_fails_closed_on_scope_or_hash_mismatch(ingested, store):
    with pytest.raises(DomainError) as error:
        ingest_into_store(store, ADMIN, OTHER, ingested[EN])
    assert error.value.code == "SCOPE_MISMATCH"

    class Tampering:
        def put_document(self, *args):
            return {"source_hash": "0" * 64}

    with pytest.raises(DomainError) as error:
        ingest_into_store(Tampering(), ADMIN, SCOPE, ingested[EN])
    assert error.value.code == "SOURCE_HASH_MISMATCH"
