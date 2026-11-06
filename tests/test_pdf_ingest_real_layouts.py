"""Parser behaviour added after first contact with real annual reports (CATL, Tencent, Apple 2025)."""

from forgeflow.pdf_ingest import _normalise_grid, _numeric, detect_unit


def test_spacer_columns_split_headers_and_wrapped_labels_are_undone():
    # CATL 2025 key-figures table as pdfplumber's line finder returns it: blank spacer columns, each year in
    # its own column next to its numbers, a sub-header halfway down, a label wrapped onto a merged grid row.
    texts = [
        ["", "项目", "", "", "2025年", "", "2024年", ""],
        ["", "营业收入", "", "423,701,834", None, "362,012,554", None, None],
        ["", "归属于上市公司股东", "", "72,201,282", None, "50,744,682", None, None],
        [None, "的净利润", None, None, None, None, None, None],
        ["", "项目", "", "", "2025 年末", "", "2024 年末", ""],
        ["", "资产总额", "", "974,827,544", None, "786,658,123", None, None],
    ]
    grid = [[(j * 10.0, i * 10.0, j * 10.0 + 9, i * 10.0 + 9) if t is not None else None for j, t in enumerate(r)]
            for i, r in enumerate(texts)]
    rows, boxes = _normalise_grid(texts, grid)
    assert rows[0] == ["项目", "2025年", "2024年"]
    assert rows[1] == ["营业收入", "423,701,834", "362,012,554"]
    assert rows[2] == ["归属于上市公司股东的净利润", "72,201,282", "50,744,682"]
    assert rows[3] == ["项目", "2025 年末", "2024 年末"]
    assert rows[4] == ["资产总额", "974,827,544", "786,658,123"]
    assert len(boxes) == len(rows) and all(len(b) == 3 for b in boxes)


def test_a_grid_without_header_columns_is_left_alone():
    texts = [["项目", "2025", "2024"], ["营业收入", "1,000", "900"]]
    grid = [[(0, 0, 1, 1)] * 3, [(0, 1, 1, 2)] * 3]
    assert _normalise_grid(texts, grid)[0] == texts


def test_years_are_headers_not_numbers():
    for header in ("2025", "2025年", "2025 年度", "2025年末", "FY2025"):
        assert not _numeric(header), header
    for value in ("423,701,834", "17.04%", "(329,173)"):
        assert _numeric(value), value


def test_currency_million_headers_carry_a_unit():
    for text in ("RMB’Million", "RMB'Million", "RMB million", "US$ billion"):
        unit = detect_unit(text)
        assert unit is not None and unit.label in {"million", "billion"}, text
    assert detect_unit("RMB’Million").currency == "CNY"
