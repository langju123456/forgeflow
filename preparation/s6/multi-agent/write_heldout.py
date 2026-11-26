"""Draft, check and freeze the held-out questions for the multi-agent comparison (preregistered.json, set "heldout").

The candidates below were written without the reports' full text at hand: each names a line item that the
disclosure rules for that kind of report prescribe, plus the patterns its printed line should match. ``draft``
resolves them against the page text of the hash-checked PDFs, proposes gold (figure, pages, accepted forms) and
writes a check list. Nothing here calls retrieval or a model. Every proposed question starts as "待人工核对";
``freeze`` refuses until a person has checked each one against the printed page.

    python preparation/s6/multi-agent/write_heldout.py draft --reports ~/code/finreports
    #   -> heldout-draft.json, heldout-candidates.json, heldout-checklist.md, heldout-checklist.html (open this one)
    python preparation/s6/multi-agent/write_heldout.py draft --reports ~/code/finreports --exclude MH03
    #   -> the same, with MH03 replaced by the next candidate; questions already checked keep their mark
    python preparation/s6/multi-agent/write_heldout.py freeze --review heldout-review.json
    #   -> heldout.json + heldout-freeze.json; commit both before any run over these questions

How gold is proposed for an answerable candidate: the first line, in page order, that matches one of its patterns
is the proposal (figure as printed). Its gold pages are that page plus every page where the same printed figure
appears again, next to the same label or, for a figure of seven or more digits, anywhere. Lines where the same
label carries a different figure are listed for the reviewer and are not gold. Composition: per report 5
same-language and 2 cross-language answerable questions and 2 + 1 absent ones, taken in the order written here.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import io
import json
import re
import sys
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[2]
PREREG = json.loads((HERE / "preregistered.json").read_text())
SOURCES = PROJECT / "preparation/s4/real-reports/sources.json"
DEV = PROJECT / PREREG["sets"]["dev"]["path"]
DRAFT, CANDIDATES_REPORT = HERE / "heldout-draft.json", HERE / "heldout-candidates.json"
CHECKLIST_MD, CHECKLIST_HTML = HERE / "heldout-checklist.md", HERE / "heldout-checklist.html"
FROZEN, FREEZE = PROJECT / PREREG["sets"]["heldout"]["path"], PROJECT / PREREG["sets"]["heldout"]["freeze"]
SET_ID = "s6-heldout-annual-reports-v1"
PENDING, VERIFIED = "pending", "verified"
LONG_FIGURE_DIGITS = 7
MAX_NEAR_LINES = 10
MAX_PAGE_IMAGES = 3

# Placeholders in a candidate's patterns. Text is matched after NFKC folding (full-width digits and punctuation
# become ASCII) with runs of whitespace collapsed to one space.
SLOTS = {
    "amount": r"\(?(?P<v>-?\d{1,3}(?:,\d{3})+(?:\.\d+)?)\)?",  # needs a thousands separator: skips note numbers
    "decimal": r"(?P<v>-?\d+\.\d+)",
    "count": r"(?P<v>\d{1,3}(?:,\d{3})+|\d+)",
    "note": r"(?:(?:[一二三四五六七八九十]+、 ?)?\d{1,3} )?",  # the 附注 / Notes column between label and amount
    "usd": r"(?:\(\d\) )?(?:\$ ?)?",  # an optional footnote mark and dollar sign before a 10-K figure
}


@dataclass(frozen=True)
class Candidate:
    id: str
    doc: str
    lang: str  # language of the question; cross-language when it differs from the report's
    kind: str  # answerable | absent
    query: str
    find: tuple[str, ...] = ()  # answerable: patterns with one value slot, tried in order
    where: str | None = None  # answerable: the page must also match this
    also: tuple[str, ...] = ()  # answerable: on the proposal's page, a second printed form of the same fact
    unit: str | None = None  # 元 | 千元 | RMB million | RMB billion | USD million: adds converted accepted forms
    also_unit: str | None = None
    lines: int = 1  # how many consecutive lines a pattern may span (sentences wrap; table rows do not)
    terms: tuple[str, ...] = ()  # absent: what to look up so a person can confirm the figure is not stated
    near: str = ""  # absent: what the report is expected to say instead
    note: str = ""


def A(id, doc, lang, query, find, **kw):  # noqa: N802 - compact constructors for the list below
    return Candidate(id, doc, lang, "answerable", query, find=tuple(find), **kw)


def N(id, doc, lang, query, terms, near):  # noqa: N802
    return Candidate(id, doc, lang, "absent", query, terms=tuple(terms), near=near)


MOUTAI, CATL = "moutai_2025_annual_zh", "catl_2025_annual_zh"
TENCENT, APPLE = "tencent_2025_annual_en", "apple_fy2025_10k_en"
MAIN = "主要会计数据"
FY = "for the year ended 31 December 2025"
MOVED = r"(?:increased|decreased|grew|rose|declined) by \d+% year-on-year to RMB ?{decimal} billion " + FY
# The line these candidates ask about was first seen in the excerpts the S4 run report keeps (rows retrieved for
# development questions). They come last in their group, so they are used only when the others cannot be found.
SEEN = "这一行最早是在 S4 运行报告留下的检索片段里见到的，对检索偏容易；排在本组最后，前面的候选不够时才用"

# Not asked in the development set for the same company: revenue, EPS, 茅台酒 revenue, overseas distributors,
# i茅台 sales, dividend per share, headcount (Moutai); revenue, net profit, battery sales volumes, market share, R&D
# spend and headcount, operating cash flow (CATL); total revenues, profit and non-IFRS profit attributable,
# Marketing Services, International Games, Weixin MAU, final dividend (Tencent); total net sales, iPhone,
# Services, Greater China, R&D, employees, repurchases (Apple).
CANDIDATES: tuple[Candidate, ...] = (
    # ---- 贵州茅台 2025 年年度报告 (figures in 元)
    A("MH01", MOUTAI, "zh", "贵州茅台 2025 年归属于上市公司股东的净利润是多少？",
      [r"^归属于上市公司股东的净利润 {amount}"], where=MAIN, unit="元"),
    A("MH02", MOUTAI, "zh", "截至 2025 年末，贵州茅台归属于上市公司股东的净资产是多少？",
      [r"^归属于上市公司股东的净资产 {amount}"], where=MAIN, unit="元"),
    A("MH03", MOUTAI, "zh", "2025 年贵州茅台其他系列酒实现营业收入多少？", [r"^(?:其他)?系列酒 {amount}"], unit="元"),
    A("MH04", MOUTAI, "zh", "贵州茅台 2025 年合并利润表里的销售费用是多少？", [r"^销售费用 {note}{amount}"], unit="元"),
    A("MH05", MOUTAI, "zh", "贵州茅台 2025 年合并利润表里的税金及附加是多少？", [r"^税金及附加 {note}{amount}"], unit="元"),
    A("MH07", MOUTAI, "zh", "贵州茅台 2025 年研发投入合计是多少？", [r"^研发投入合计 {amount}"], unit="元"),
    A("MH08", MOUTAI, "zh", "贵州茅台 2025 年的利润总额是多少？", [r"^利润总额 {amount}"], where=MAIN, unit="元"),
    A("MH09", MOUTAI, "zh", "贵州茅台 2025 年支付给职工及为职工支付的现金是多少？",
      [r"^支付给职工(?:以)?及为职工支付的现金 {note}{amount}"], unit="元"),
    A("MH06", MOUTAI, "zh", "2025 年茅台酒基酒的设计产能是多少吨？",
      [r"茅台酒基酒设计产能为 ?{amount} ?吨", r"^茅台酒(?:基酒)? {amount}"], where="设计产能", lines=2, note=SEEN),
    A("MH21", MOUTAI, "en", "What were Kweichow Moutai's total assets at the end of 2025?", [r"^总资产 {amount}"],
      where=MAIN, unit="元"),
    A("MH22", MOUTAI, "en", "What was Kweichow Moutai's weighted average return on net assets for 2025?",
      [r"^加权平均净资产收益率(?: ?\(%\))? {decimal}"]),
    A("MH23", MOUTAI, "en", "What was Kweichow Moutai's net cash flow from operating activities in 2025?",
      [r"^经营活动产生的现金流量净额 {amount}"], where=MAIN, unit="元"),
    A("MH24", MOUTAI, "en", "What was Kweichow Moutai's income tax expense for 2025?",
      [r"^(?:减:)?所得税费用 {note}{amount}"], unit="元"),
    N("MH31", MOUTAI, "zh", "贵州茅台 2025 年茅台酒的平均吨酒价格是多少？", ["吨价", "吨酒价", "均价", "平均售价"],
      "披露茅台酒的收入和销量，不给每吨价格"),
    N("MH32", MOUTAI, "zh", "2025 年贵州茅台第一大客户的销售额是多少？", ["前五名客户", "第一名", "最大客户"],
      "只披露前五名客户销售额合计和占比"),
    N("MH33", MOUTAI, "zh", "茅台 1935 这一款产品 2025 年的销售收入是多少？", ["1935"], "系列酒只有合计，不按单品拆分"),
    N("MH34", MOUTAI, "zh", "2025 年贵州茅台在日本市场的销售收入是多少？", ["日本"], "只披露“国外”合计，不按国家拆分"),
    N("MH41", MOUTAI, "en", "How many bottles of Feitian Moutai were sold in 2025?", ["飞天", "瓶"],
      "产销量按吨披露，没有按瓶的数量"),
    N("MH42", MOUTAI, "en", "What was Kweichow Moutai's share of China's baijiu market in 2025?",
      ["市场份额", "市场占有率", "市占率"], "谈行业地位，不给市场份额数字"),
    # ---- 宁德时代 2025 年年度报告 (figures in 千元)
    A("CH01", CATL, "zh", "宁德时代 2025 年基本每股收益是多少？", [r"^基本每股收益(?: ?\(元/股\))? {decimal}"]),
    A("CH02", CATL, "zh", "宁德时代 2025 年归属于上市公司股东的扣除非经常性损益的净利润是多少？",
      [r"^归属于上市公司股东的扣除非经常性损益的净利润 {amount}", r"扣除非经常性损益的净利润 {amount}"], unit="千元", lines=2),
    A("CH03", CATL, "zh", "宁德时代 2025 年境外营业收入是多少？", [r"^境外 {amount}"], unit="千元"),
    A("CH04", CATL, "zh", "宁德时代 2025 年研发投入占营业收入的比例是多少？", [r"^研发投入占营业收入比例 {decimal}%"]),
    A("CH05", CATL, "zh", "宁德时代 2025 年动力电池系统实现营业收入多少？", [r"^动力电池系统 {amount}"], unit="千元"),
    A("CH06", CATL, "zh", "宁德时代 2025 年储能电池系统实现营业收入多少？", [r"^储能电池系统 {amount}"], unit="千元"),
    A("CH07", CATL, "zh", "宁德时代 2025 年度利润分配预案是每 10 股派发现金分红多少元？",
      [r"每 ?10 ?股派发现金(?:分红|红利|股利)(?:人民币)? ?{decimal} ?元"], where=r"2025 ?年度利润分配预案", lines=3),
    A("CH08", CATL, "zh", "截至 2025 年末，宁德时代的总资产是多少？", [r"^(?:资产总额|总资产) {amount}"], unit="千元"),
    A("CH09", CATL, "zh", "宁德时代 2025 年的销售费用是多少？", [r"^销售费用 {note}{amount}"], unit="千元"),
    A("CH21", CATL, "en", "What were CATL's net assets attributable to shareholders of the listed company at the end "
      "of 2025?", [r"^归属于上市公司股东的净资产 {amount}"], unit="千元"),
    A("CH22", CATL, "en", "What was CATL's weighted average return on net assets in 2025?",
      [r"^加权平均净资产收益率 {decimal}%"]),
    A("CH23", CATL, "en", "What was CATL's revenue from mainland China (domestic) in 2025?", [r"^境内 {amount}"],
      unit="千元"),
    A("CH24", CATL, "en", "What were CATL's administrative expenses in 2025?", [r"^管理费用 {note}{amount}"], unit="千元"),
    N("CH31", CATL, "zh", "宁德时代 2025 年对比亚迪的销售额是多少？", ["比亚迪"], "前五名客户不具名"),
    N("CH32", CATL, "zh", "宁德时代 2025 年固态电池的出货量是多少 GWh？", ["固态电池", "固态"], "只谈技术进展，没有出货量"),
    N("CH33", CATL, "zh", "宁德时代 2025 年每 GWh 电池的生产成本是多少？", ["单位成本", "每GWh", "每 GWh", "单GWh"],
      "有营业成本合计，没有每 GWh 成本"),
    N("CH34", CATL, "zh", "宁德时代德国工厂 2025 年的产量是多少 GWh？", ["德国"], "提到德国工厂，不给分工厂产量"),
    N("CH41", CATL, "en", "What was the revenue of CATL's battery swap business in 2025?", ["换电", "巧克力"],
      "介绍换电业务，不单列收入"),
    N("CH42", CATL, "en", "What was CATL's revenue in the United States in 2025?", ["美国"], "分地区只有境内、境外"),
    # ---- Tencent Annual Report 2025 (RMB million in tables, RMB billion in the discussion)
    A("TH02", TENCENT, "en", "What were Tencent's FinTech and Business Services revenues in 2025?",
      [r"Revenues from FinTech and Business Services " + MOVED], also=(r"^FinTech and Business Services {amount}",),
      unit="RMB billion", also_unit="RMB million", lines=3),
    A("TH13", TENCENT, "en", "What were Tencent's general and administrative expenses in 2025?",
      [r"General and administrative expenses " + MOVED], unit="RMB billion", lines=3),
    A("TH14", TENCENT, "en", "What was Tencent's cost of revenues in 2025?", [r"Cost of revenues " + MOVED],
      unit="RMB billion", lines=3),
    A("TH15", TENCENT, "en", "What was Tencent's income tax expense for 2025?", [r"Income tax expense " + MOVED],
      unit="RMB billion", lines=3),
    A("TH09", TENCENT, "en", "What was Tencent's total remuneration cost for 2025?",
      [r"total remuneration costs? [^.]{0,120}?(?:was|were) RMB ?{decimal} billion"], unit="RMB billion", lines=3),
    A("TH11", TENCENT, "en", "What were Tencent's Social Networks revenues in 2025?",
      [r"Social Networks revenues (?:increased|decreased|grew|rose|declined) by \d+% (?:year-on-year )?to RMB ?"
       r"{decimal} billion"], where=FY, unit="RMB billion", lines=3),
    A("TH03", TENCENT, "en", "What were Tencent's Domestic Games revenues in 2025?",
      [r"Domestic Games revenues were RMB ?{decimal} billion"], where=FY, unit="RMB billion", lines=3, note=SEEN),
    A("TH06", TENCENT, "en", "What was Tencent's basic earnings per share for 2025?",
      [r"Basic and diluted EPS " + FY + r" were RMB ?{decimal} and"], lines=3, note=SEEN),
    A("TH07", TENCENT, "en", "What share of Tencent's total revenues came from its five largest customers in 2025?",
      [r"five largest customers of the Group accounted for approximately {decimal}%"], lines=3, note=SEEN),
    A("TH21", TENCENT, "zh", "腾讯 2025 年增值服务（VAS）业务的收入是多少？", [r"Revenues from VAS " + MOVED],
      also=(r"^VAS {amount}",), unit="RMB billion", also_unit="RMB million", lines=3),
    A("TH22", TENCENT, "zh", "截至 2025 年底，腾讯集团一共有多少名雇员？", [r"the Group had {count} employees"], lines=3),
    A("TH23", TENCENT, "zh", "腾讯 2025 年的毛利是多少？", [r"Gross ?profit " + MOVED], unit="RMB billion", lines=3),
    A("TH24", TENCENT, "zh", "腾讯 2025 年的销售及市场推广开支是多少？", [r"Selling and marketing expenses " + MOVED],
      unit="RMB billion", lines=3),
    N("TH31", TENCENT, "en", "What were the revenues of Tencent Video in 2025?", ["Tencent Video"],
      "mentions the service and subscribers, no revenue of its own"),
    N("TH32", TENCENT, "en", "How many daily active users did Weixin Mini Programs have in 2025?", ["Mini Programs"],
      "mentions Mini Programs, no user count"),
    N("TH33", TENCENT, "en", "What was Tencent's revenue from Video Accounts in 2025?", ["Video Accounts"],
      "named as a growth driver inside Marketing Services, no figure of its own"),
    N("TH34", TENCENT, "en", "How much did Tencent spend on GPUs in 2025?", ["GPU", "chips"],
      "capital expenditure in total, not by item"),
    N("TH41", TENCENT, "zh", "腾讯 2025 年在游戏业务上的研发投入是多少？", ["R&D", "research and development"],
      "研发开支只有集团合计，不按业务拆分"),
    N("TH42", TENCENT, "zh", "腾讯会议 2025 年的收入是多少？", ["Tencent Meeting"], "提到产品，不单列收入"),
    # ---- Apple Form 10-K, fiscal 2025 (USD million)
    A("AH01", APPLE, "en", "What were Apple's Mac net sales in fiscal 2025?", [r"^Mac {usd}{amount}"], unit="USD million"),
    A("AH02", APPLE, "en", "What were Apple's iPad net sales in fiscal 2025?", [r"^iPad {usd}{amount}"],
      unit="USD million"),
    A("AH03", APPLE, "en", "What were Apple's Wearables, Home and Accessories net sales in fiscal 2025?",
      [r"^Wearables, Home and Accessories {usd}{amount}"], unit="USD million"),
    A("AH04", APPLE, "en", "What were Apple's net sales in the Americas segment in fiscal 2025?",
      [r"^Americas {usd}{amount}"], unit="USD million"),
    A("AH05", APPLE, "en", "What was Apple's diluted earnings per share for fiscal 2025?", [r"^Diluted {usd}{decimal}"]),
    A("AH06", APPLE, "en", "What was Apple's effective tax rate for fiscal 2025?", [r"^Effective tax rate {decimal} ?%"]),
    A("AH07", APPLE, "en", "What was Apple's total gross margin percentage in fiscal 2025?",
      [r"^Total gross margin percentage {decimal} ?%"]),
    A("AH08", APPLE, "en", "What were Apple's net sales in the Rest of Asia Pacific segment in fiscal 2025?",
      [r"^Rest of Asia Pacific {usd}{amount}"], unit="USD million"),
    A("AH09", APPLE, "en", "How much in dividends and dividend equivalents did Apple declare per share in fiscal 2025?",
      [r"declared per share or RSU {usd}{decimal}"], lines=2),
    A("AH21", APPLE, "zh", "苹果 2025 财年欧洲分部的净销售额是多少？", [r"^Europe {usd}{amount}"], unit="USD million"),
    A("AH22", APPLE, "zh", "苹果 2025 财年的净利润是多少？", [r"^Net income {usd}{amount}"], unit="USD million"),
    A("AH23", APPLE, "zh", "苹果 2025 财年的销售、一般及行政费用是多少？",
      [r"^Selling, general and administrative {usd}{amount}"], unit="USD million"),
    A("AH24", APPLE, "zh", "苹果 2025 财年日本分部的净销售额是多少？", [r"^Japan {usd}{amount}"], unit="USD million"),
    N("AH31", APPLE, "en", "What were Apple's AirPods net sales in fiscal 2025?", ["AirPods"],
      "inside Wearables, Home and Accessories, no figure of its own"),
    N("AH32", APPLE, "en", "How many Apple retail stores were open at the end of fiscal 2025?", ["retail stores", "retail store"],
      "mentions retail stores, not how many"),
    N("AH33", APPLE, "en", "How many active devices did Apple report for fiscal 2025?", ["active devices", "installed base"],
      "mentions the installed base, no count"),
    N("AH34", APPLE, "en", "What were App Store net sales in fiscal 2025?", ["App Store"], "inside Services"),
    N("AH41", APPLE, "zh", "苹果 2025 财年 Apple Watch 的净销售额是多少？", ["Apple Watch"],
      "并在 Wearables, Home and Accessories 里，没有单独数字"),
    N("AH42", APPLE, "zh", "苹果 2025 财年 Apple Music 有多少订阅用户？", ["Apple Music", "subscriptions"],
      "提到订阅服务，不给用户数"),
)


# --------------------------------------------------------------------------- page text


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fold(text: str) -> str:
    """What patterns are matched against: NFKC (full-width forms to ASCII), whitespace runs to one space."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text or "")).strip()


def page_lines(pdf_path: Path) -> list[list[dict]]:
    """Per page, the text lines pdfplumber reads, each with its box: [{"text", "x0", "top", "x1", "bottom"}]."""
    import pdfplumber

    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            pages.append([{"text": line["text"], **{k: round(float(line[k]), 1) for k in ("x0", "top", "x1", "bottom")}}
                          for line in page.extract_text_lines() if line["text"].strip()])
    return pages


def load_reports(reports_dir: Path, sources: Path) -> dict[str, dict]:
    """The reports of sources.json whose PDF is present with the recorded hash; anything else stops the draft."""
    out = {}
    for report in json.loads(Path(sources).read_text())["reports"]:
        path = Path(reports_dir) / f"{report['key']}.pdf"
        if not path.is_file() or sha256(path) != report["sha256"]:
            raise SystemExit(f"{path}: missing or hash differs from {sources}")
        out[report["key"]] = {**report, "path": path}
    return out


# --------------------------------------------------------------------------- locating a candidate


def compile_pattern(pattern: str) -> re.Pattern:
    for name, expression in SLOTS.items():
        pattern = pattern.replace("{" + name + "}", expression)
    compiled = re.compile(pattern)
    if "v" not in compiled.groupindex:
        raise ValueError(f"pattern has no value slot: {pattern}")
    return compiled


def _joined(lines: list[dict], joiner: str) -> str:
    """Consecutive lines as one string: nothing between Chinese lines, a space between Latin ones unless the
    line ends in a hyphen (``year-on-`` / ``year``)."""
    text = ""
    for line in lines:
        text += ("" if not text or text.endswith("-") else joiner) + line["text"].strip()
    return fold(text)


def _box(lines: list[dict]) -> list[float]:
    return [min(line["x0"] for line in lines), min(line["top"] for line in lines),
            max(line["x1"] for line in lines), max(line["bottom"] for line in lines)]


def find_matches(patterns: tuple[str, ...], pages: list[list[dict]], *, span: int, joiner: str,
                 where: str | None = None) -> list[dict]:
    """Every place a pattern matches, in page order.

    Single lines are tried first; a sentence that wraps is then matched over two (or ``span``) consecutive lines
    that no shorter match already covers, so one printed statement is found once, with the smallest box.
    """
    compiled = [compile_pattern(p) for p in patterns]
    condition = re.compile(where) if where else None
    found = []
    for number, lines in enumerate(pages, start=1):
        if condition and not condition.search(fold(" ".join(line["text"] for line in lines))):
            continue
        covered: set[int] = set()
        on_page = []
        for size in range(1, span + 1):
            for index in range(len(lines) - size + 1):
                if covered & set(range(index, index + size)):
                    continue
                chunk = lines[index:index + size]
                text = _joined(chunk, joiner)
                for order, pattern in enumerate(compiled):
                    match = pattern.search(text)
                    if match:
                        covered |= set(range(index, index + size))
                        on_page.append((index, {"page": number, "value": match.group("v"), "pattern": order,
                                                "line": " ".join(line["text"] for line in chunk), "box": _box(chunk)}))
                        break
        found += [item for _, item in sorted(on_page, key=lambda pair: pair[0])]
    return found


def _digits(value: str) -> str:
    return value.replace(",", "").lstrip("-")


def figure_pages(value: str, pages: list[list[dict]]) -> list[dict]:
    """Lines that print exactly this figure (same digits, thousands separators ignored), in page order."""
    wanted = _digits(value)
    out = []
    for number, lines in enumerate(pages, start=1):
        for line in lines:
            if any(_digits(token) == wanted for token in re.findall(r"\d[\d,]*(?:\.\d+)?", fold(line["text"]))):
                out.append({"page": number, "line": line["text"], "box": _box([line])})
    return out


def _quantize(value: Decimal, places: int) -> str:
    return str(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def _padded(text: str) -> bool:
    """3693.00 and 3693.0 say nothing 3693 does not."""
    return "." in text and set(text.split(".", 1)[1]) == {"0"}


def accept_forms(value: str, unit: str | None) -> list[str]:
    """The printed figure, plus the same amount in 亿 and in billions when the unit is known.

    The registered matching compares a pure number as a whole numeric token and a form with a unit as a bounded
    substring, so "823.20亿" and "823.2亿" are different forms and both are listed.
    """
    forms = [value.lstrip("-")]
    scale = {"元": (Decimal(10) ** -8, None), "千元": (Decimal(10) ** -5, Decimal(10) ** -6),
             "RMB million": (Decimal("0.01"), Decimal("0.001")), "RMB billion": (Decimal(10), None),
             "USD million": (Decimal("0.01"), Decimal("0.001"))}.get(unit or "")
    if scale is None:
        return forms
    amount = abs(Decimal(_digits(value)))
    yi, billion = amount * scale[0], amount * scale[1] if scale[1] is not None else None
    if yi >= 1:
        forms += [f"{text}亿" for text in (_quantize(yi, places) for places in (2, 1, 0)) if not _padded(text)]
    if billion is not None and billion >= 1:
        forms += [f"{_quantize(billion, places)} billion" for places in (1, 2)]
    return list(dict.fromkeys(forms))


def locate(candidate: Candidate, pages: list[list[dict]], report_language: str) -> dict:
    """Resolve one candidate against its report's page text. No retrieval, no model: patterns over printed lines."""
    joiner = "" if report_language == "zh" else " "
    base = {"id": candidate.id, "doc": candidate.doc, "lang": candidate.lang, "kind": candidate.kind,
            "query": candidate.query, "cross_language": candidate.lang != report_language}
    if candidate.kind == "absent":
        seen = []
        for number, lines in enumerate(pages, start=1):
            for line in lines:
                hit = next((t for t in candidate.terms if t.casefold() in fold(line["text"]).casefold()), None)
                if hit:
                    seen.append({"page": number, "term": hit, "line": line["text"]})
        return base | {"located": True, "near": candidate.near, "searched": list(candidate.terms),
                       "mentions": len(seen), "evidence": seen[:MAX_NEAR_LINES]}
    matches = find_matches(candidate.find, pages, span=candidate.lines, joiner=joiner, where=candidate.where)
    if not matches:
        return base | {"located": False, "reason": "no line matches any pattern"
                       + (f" on a page that also matches {candidate.where!r}" if candidate.where else "")}
    first = matches[0]
    value = first["value"]
    accept = accept_forms(value, candidate.unit)
    evidence = [{"page": first["page"], "line": first["line"], "box": first["box"], "why": "标签和数字"}]
    labelled = find_matches(candidate.find, pages, span=candidate.lines, joiner=joiner)
    long_figure = len(re.sub(r"\D", "", value)) >= LONG_FIGURE_DIGITS
    for again in labelled:
        if _digits(again["value"]) == _digits(value) and (again["page"], again["line"]) != (first["page"], first["line"]):
            evidence.append({"page": again["page"], "line": again["line"], "box": again["box"],
                             "why": "同一标签、同一数字"})
    if long_figure:
        for again in figure_pages(value, pages):
            if all((again["page"], again["line"]) != (e["page"], e["line"]) and again["line"] not in e["line"]
                   for e in evidence):
                evidence.append({**again, "why": "同一数字在别处又印了一次"})
    for extra in find_matches(candidate.also, [pages[first["page"] - 1]], span=1, joiner=joiner) if candidate.also else []:
        accept += [f for f in accept_forms(extra["value"], candidate.also_unit) if f not in accept]
        evidence.append({"page": first["page"], "line": extra["line"], "box": extra["box"],
                         "why": "同一页上的另一种写法"})
    # The same label with another figure (a quarter, the parent company, last year's proposal): shown, never gold.
    others = [{"page": m["page"], "value": m["value"], "line": m["line"]} for m in labelled
              if _digits(m["value"]) != _digits(value) and m["pattern"] == first["pattern"]]
    return base | {
        "located": True, "pages": sorted({e["page"] for e in evidence}), "answer": f"{value}（见 p.{first['page']}：{first['line'][:80]}）",
        "accept": accept, "unit": candidate.unit, "evidence": evidence, "other_values": others[:8],
        "note": candidate.note,
    }


def choose(located: list[dict], per_report: dict[str, int], excluded: set[str]) -> tuple[list[dict], list[str]]:
    """Per report, the first located candidates of each kind in the order written, up to the registered counts."""
    want = {("answerable", False): per_report["answerable"] - per_report["answerable_cross_language"],
            ("answerable", True): per_report["answerable_cross_language"]}
    cross_absent = 1 if per_report["absent"] >= 3 else 0
    want |= {("absent", False): per_report["absent"] - cross_absent, ("absent", True): cross_absent}
    chosen, short = [], []
    for doc in dict.fromkeys(item["doc"] for item in located):
        for (kind, cross), count in want.items():
            pool = [item for item in located if item["doc"] == doc and item["kind"] == kind
                    and item["cross_language"] == cross and item["located"] and item["id"] not in excluded]
            chosen += pool[:count]
            if len(pool) < count:
                short.append(f"{doc}: {len(pool)} of {count} {'cross-language ' if cross else ''}{kind}")
    return chosen, short


# --------------------------------------------------------------------------- check list


def _review(status: str = PENDING) -> dict:
    return {"status": status, "label": "待人工核对" if status == PENDING else "已核对", "by": None, "at": None, "note": ""}


def proposal_hash(q: dict) -> str:
    """What a reviewer's mark is about: the question and its proposed gold. A mark on another proposal is void."""
    material = [q.get(k) for k in ("id", "doc", "lang", "kind", "query", "pages", "accept", "near")]
    return hashlib.sha256(json.dumps(material, ensure_ascii=False).encode()).hexdigest()[:16]


def checklist_markdown(draft: dict, reports: dict[str, dict]) -> str:
    out = ["# 留出题核对清单", "",
           f"草稿 `{DRAFT.name}`，共 {len(draft['questions'])} 题，全部**待人工核对**。对着年报原页逐题看：",
           "有答案题看“题目问的是不是这一格、数字和页码对不对”；无答案题看“列出的这些地方是不是都没有写出题目问的那个数”。",
           "带原页截图、可以直接点选并导出结果的版本是同目录的 `heldout-checklist.html`（不进 git）。", ""]
    for q in draft["questions"]:
        report = reports[q["doc"]]
        out += [f"## {q['id']}　{'有答案' if q['kind'] == 'answerable' else '无答案'}　{report['company']}"
                f"（问 {q['lang']} / 报告 {report['language']}）　待人工核对", "", f"**题目**：{q['query']}", ""]
        if q["kind"] == "answerable":
            out += [f"**初标答案**：{q['accept'][0]}　**初标 gold 页**：{', '.join(f'p.{p}' for p in q['pages'])}",
                    f"**可接受的写法**：{' / '.join(q['accept'])}", "", "| 页 | 原文片段 | 为什么算 |", "|---|---|---|"]
            out += [f"| p.{e['page']} | {e['line'].replace('|', '¦')} | {e['why']} |" for e in q["evidence"]]
            if q["other_values"]:
                out += ["", "同一个标签在别处印的是别的数（不算 gold，核对时留意题目有没有歧义）：", ""]
                out += [f"- p.{o['page']}：{o['value']}　`{o['line'][:100]}`" for o in q["other_values"]]
            if q.get("note"):
                out += ["", f"注：{q['note']}"]
        else:
            out += [f"**预期**：{q['near']}", f"**全文查过的词**：{'、'.join(q['searched'])}（共 {q['mentions']} 处）", ""]
            out += [f"- p.{e['page']}（{e['term']}）：{e['line'][:140]}" for e in q["evidence"]] or ["- 全文没有出现这些词"]
        out.append("")
    return "\n".join(out) + "\n"


def _page_image(pdf: Any, page_number: int, boxes: list[list[float]]) -> str:
    """The printed page with the proposed lines outlined, as a data URL (PNG or JPEG, whichever is smaller)."""
    image = pdf.pages[page_number - 1].to_image(resolution=96)
    for box in boxes:
        image.draw_rect((box[0] - 3, box[1] - 2, box[2] + 3, box[3] + 2), fill=(255, 196, 0, 70),
                        stroke=(214, 96, 0), stroke_width=2)
    png, jpeg = io.BytesIO(), io.BytesIO()
    image.annotated.save(png, format="PNG", optimize=True)
    image.annotated.convert("RGB").save(jpeg, format="JPEG", quality=72)
    kind, data = ("png", png.getvalue()) if png.tell() <= jpeg.tell() else ("jpeg", jpeg.getvalue())
    return f"data:image/{kind};base64,{base64.b64encode(data).decode()}"


def checklist_html(draft: dict, reports: dict[str, dict]) -> str:
    import pdfplumber

    cards = []
    opened: dict[str, Any] = {}
    try:
        for q in draft["questions"]:
            report = reports[q["doc"]]
            link = report["path"].resolve().as_uri()
            body = []
            if q["kind"] == "answerable":
                forms = " ".join(f"<code>{html.escape(f)}</code>" for f in q["accept"])
                body.append(f"<p class='gold'>初标答案 <b>{html.escape(q['accept'][0])}</b> · gold 页 "
                            + " ".join(f"<a href='{link}#page={p}' target='_blank'>p.{p}</a>" for p in q["pages"])
                            + f"</p><p class='forms'>可接受的写法 {forms}</p>")
                rows = "".join(f"<tr><td><a href='{link}#page={e['page']}' target='_blank'>p.{e['page']}</a></td>"
                               f"<td>{html.escape(e['line'])}</td><td>{html.escape(e['why'])}</td></tr>"
                               for e in q["evidence"])
                body.append(f"<table><tr><th>页</th><th>原文片段</th><th>为什么算</th></tr>{rows}</table>")
                if q["other_values"]:
                    items = "".join(f"<li>p.{o['page']}：<b>{html.escape(o['value'])}</b> "
                                    f"<span class='dim'>{html.escape(o['line'][:120])}</span></li>" for o in q["other_values"])
                    body.append(f"<details><summary>同一个标签在别处印的是别的数（不算 gold，留意题目有没有歧义）</summary>"
                                f"<ul>{items}</ul></details>")
                if q.get("note"):
                    body.append(f"<p class='dim'>注：{html.escape(q['note'])}</p>")
                if q["doc"] not in opened:
                    opened[q["doc"]] = pdfplumber.open(report["path"])
                pdf = opened[q["doc"]]
                for page in q["pages"][:MAX_PAGE_IMAGES]:
                    boxes = [e["box"] for e in q["evidence"] if e["page"] == page]
                    body.append(f"<figure><figcaption>原页 p.{page}（橙色框是初标依据）</figcaption>"
                                f"<img alt='p.{page}' src='{_page_image(pdf, page, boxes)}'></figure>")
            else:
                items = "".join(f"<li><a href='{link}#page={e['page']}' target='_blank'>p.{e['page']}</a>"
                                f"（{html.escape(e['term'])}）{html.escape(e['line'][:200])}</li>" for e in q["evidence"])
                body.append(f"<p class='gold'>预期：{html.escape(q['near'])}</p><p class='forms'>全文查过的词："
                            f"{html.escape('、'.join(q['searched']))}（共 {q['mentions']} 处"
                            f"{'，下面列前 ' + str(len(q['evidence'])) + ' 处' if q['mentions'] > len(q['evidence']) else ''}）</p>"
                            f"<ul>{items or '<li>全文没有出现这些词</li>'}</ul>"
                            "<p class='dim'>确认这些地方都没有写出题目问的那个数；写了的话选“剔除”。</p>")
            kind = "有答案" if q["kind"] == "answerable" else "无答案"
            cards.append(
                f"<section class='card' data-id='{q['id']}' data-proposal='{proposal_hash(q)}' "
                f"data-status='{q['review']['status'] if q['review']['status'] == VERIFIED else ''}'>"
                f"<header><span class='id'>{q['id']}</span>"
                f"<span class='tag {q['kind']}'>{kind}</span><span class='doc'>{html.escape(report['company'])} · "
                f"问 {q['lang']} / 报告 {report['language']}</span><span class='state'>待人工核对</span></header>"
                f"<h2>{html.escape(q['query'])}</h2>{''.join(body)}"
                "<div class='review'><label><input type='radio' value='verified'> 对，通过</label>"
                "<label><input type='radio' value='fix'> 要改（页码 / 数字 / 写法）</label>"
                "<label><input type='radio' value='drop'> 剔除这题</label>"
                "<input type='text' placeholder='要改或剔除的话，写一句原因'></div></section>")
    finally:
        for pdf in opened.values():
            pdf.close()
    return _PAGE.replace("__CARDS__", "\n".join(cards)).replace("__TOTAL__", str(len(draft["questions"]))) \
        .replace("__SET__", SET_ID)


_PAGE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>留出题核对清单</title>
<style>
:root{--ink:#1c2330;--dim:#667085;--line:#e3e7ee;--bg:#f5f6f8;--ok:#0f7b4f;--fix:#b25e00;--drop:#b3261e;--accent:#d66000}
*{box-sizing:border-box}body{margin:0;font:15px/1.6 -apple-system,"PingFang SC","Helvetica Neue",sans-serif;color:var(--ink);background:var(--bg)}
.bar{position:sticky;top:0;z-index:5;background:#fff;border-bottom:1px solid var(--line);padding:12px 24px;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
.bar h1{font-size:17px;margin:0 8px 0 0}.bar input{padding:7px 10px;border:1px solid var(--line);border-radius:8px;font:inherit;width:150px}
.bar button{padding:8px 16px;border:0;border-radius:8px;background:var(--ink);color:#fff;font:inherit;cursor:pointer}.bar button:disabled{opacity:.35;cursor:default}
.meter{flex:1;min-width:160px;height:8px;border-radius:4px;background:var(--line);overflow:hidden}.meter i{display:block;height:100%;width:0;background:var(--ok);transition:width .25s}
.count{font-variant-numeric:tabular-nums;color:var(--dim)}main{max-width:1040px;margin:20px auto;padding:0 20px}
.card{background:#fff;border:1px solid var(--line);border-left:5px solid var(--line);border-radius:12px;padding:18px 22px;margin-bottom:18px;transition:border-color .2s}
.card.verified{border-left-color:var(--ok)}.card.fix{border-left-color:var(--fix)}.card.drop{border-left-color:var(--drop)}
header{display:flex;gap:10px;align-items:center;font-size:13px;color:var(--dim)}.id{font-weight:700;color:var(--ink)}
.tag{padding:1px 8px;border-radius:10px;background:#eef2ff;color:#3447a6}.tag.absent{background:#fdf1e7;color:var(--fix)}.state{margin-left:auto}
h2{font-size:18px;margin:8px 0 10px}.gold b{font-size:17px}.forms,.dim{color:var(--dim);font-size:13px}code{background:var(--bg);padding:1px 6px;border-radius:5px;margin-right:4px}
table{border-collapse:collapse;width:100%;font-size:13px;margin:8px 0}th,td{border-top:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}th{color:var(--dim);font-weight:500}
figure{margin:12px 0 0}figcaption{font-size:13px;color:var(--dim);margin-bottom:4px}img{max-width:100%;border:1px solid var(--line);border-radius:8px}
a{color:var(--accent)}.review{display:flex;gap:16px;align-items:center;flex-wrap:wrap;margin-top:14px;padding-top:12px;border-top:1px dashed var(--line)}
.review input[type=text]{flex:1;min-width:220px;padding:7px 10px;border:1px solid var(--line);border-radius:8px;font:inherit}
</style></head><body>
<div class="bar"><h1>留出题核对清单</h1><div class="meter"><i id="fill"></i></div><span class="count" id="count"></span>
<input id="who" placeholder="核对人姓名"><button id="export" disabled>导出核对结果</button></div>
<main>
<p class="dim">每题对着原页截图（或点页码打开原 PDF）看一眼：有答案题确认“题目问的就是这一格，数字和页码对”；无答案题确认“列出的地方都没有写出题目问的那个数”。
全部选完、填上姓名后点“导出核对结果”，把下载的 heldout-review.json 放到 preparation/s6/multi-agent/ 下。进度存在这台电脑的浏览器里，刷新不丢。</p>
__CARDS__
</main>
<script>
const KEY="__SET__",cards=[...document.querySelectorAll(".card")],all=JSON.parse(localStorage.getItem(KEY)||"{}");
const TEXT={verified:"已核对",fix:"要改",drop:"剔除"},who=document.getElementById("who"),saved={};
// A mark belongs to one proposal: when a question's proposed gold changed, its old mark is not shown again.
for(const c of cards){const m=all[c.dataset.id];if(m&&m.proposal===c.dataset.proposal)saved[c.dataset.id]=m;
else if(c.dataset.status)saved[c.dataset.id]={status:c.dataset.status,note:"",proposal:c.dataset.proposal}}
who.value=all.__who||"";
function refresh(){let done=0;for(const c of cards){const s=(saved[c.dataset.id]||{}).status;c.classList.remove("verified","fix","drop");
if(s){c.classList.add(s);done++}c.querySelector(".state").textContent=s?TEXT[s]:"待人工核对"}
document.getElementById("fill").style.width=(100*done/cards.length)+"%";document.getElementById("count").textContent=done+" / __TOTAL__";
document.getElementById("export").disabled=done<cards.length||!who.value.trim();
localStorage.setItem(KEY,JSON.stringify({...saved,__who:who.value}))}
for(const c of cards){const id=c.dataset.id,note=c.querySelector("input[type=text]");note.value=(saved[id]||{}).note||"";
for(const r of c.querySelectorAll("input[type=radio]")){r.name=id;r.checked=(saved[id]||{}).status===r.value;
r.onchange=()=>{saved[id]={status:r.value,note:note.value,proposal:c.dataset.proposal};refresh()}}
note.oninput=()=>{if(saved[id]){saved[id].note=note.value;refresh()}}}
who.oninput=refresh;
document.getElementById("export").onclick=()=>{const items={};for(const c of cards)items[c.dataset.id]=saved[c.dataset.id];
const out={set:"__SET__",reviewer:who.value.trim(),exported_at:new Date().toISOString(),items};
const a=document.createElement("a");a.href=URL.createObjectURL(new Blob([JSON.stringify(out,null,1)],{type:"application/json"}));
a.download="heldout-review.json";a.click()};refresh();
</script></body></html>
"""


# --------------------------------------------------------------------------- draft and freeze


def check_candidates(candidates: tuple[Candidate, ...], reports: dict[str, dict], dev_queries: set[str]) -> None:
    """What can be checked without the PDFs: ids, reports, patterns, and that no report tool answers a question."""
    from forgeflow import report_lookup

    if len({c.id for c in candidates}) != len(candidates):
        raise SystemExit("candidate ids are not unique")
    for c in candidates:
        if c.doc not in reports:
            raise SystemExit(f"{c.id}: unknown report {c.doc}")
        if c.kind == "answerable" and not c.find or c.kind == "absent" and not c.terms:
            raise SystemExit(f"{c.id}: nothing to look for")
        for pattern in c.find + c.also:
            compile_pattern(pattern)
        if report_lookup.for_question(c.query):
            raise SystemExit(f"{c.id}: this question is answered by a report tool, not by the model")
        if c.query in dev_queries:
            raise SystemExit(f"{c.id}: the development set already asks this")


def draft(reports_dir: Path, *, candidates: tuple[Candidate, ...] = CANDIDATES, sources: Path = SOURCES,
          per_report: dict[str, int] | None = None, exclude: tuple[str, ...] = (), out: Path = HERE,
          images: bool = True) -> int:
    per_report = per_report or PREREG["sets"]["heldout"]["per_report"]
    reports = load_reports(reports_dir, sources)
    dev = {q["query"] for q in json.loads(DEV.read_text())["questions"]}
    check_candidates(candidates, reports, dev)
    draft_path = out / DRAFT.name
    earlier = json.loads(draft_path.read_text()) if draft_path.exists() else {"questions": [], "excluded": []}
    excluded = set(earlier.get("excluded", [])) | set(exclude)
    pages = {key: page_lines(report["path"]) for key, report in reports.items()}
    located = [locate(c, pages[c.doc], reports[c.doc]["language"]) for c in candidates]
    chosen, short = choose(located, per_report, excluded)
    before = {q["id"]: q for q in earlier["questions"]}
    questions = []
    for item in chosen:
        question = {k: v for k, v in item.items() if k not in {"located", "cross_language"}}
        kept = before.get(item["id"])
        question["review"] = kept["review"] if kept and kept["review"]["status"] == VERIFIED \
            and proposal_hash(kept) == proposal_hash(question) else _review()
        questions.append(question)
    document = {
        "id": SET_ID, "status": "DRAFT: 每题待人工核对；冻结前不能在这批题上检索或调用模型",
        "written": f"{datetime.now(UTC):%Y-%m-%d}, by write_heldout.py from the page text (pdfplumber) of the PDFs in "
                   "sources.json; no retrieval and no model was run over them",
        "author_note": "Candidates were written without the reports' full text, from the line items the disclosure "
                       "rules prescribe; the script proposed figure and pages; a person checks every question "
                       "against the printed page before the set is frozen.",
        "answer_rule": json.loads(DEV.read_text())["answer_rule"],
        "sources_sha256": sha256(sources), "writer_sha256": sha256(Path(__file__)),
        "excluded": sorted(excluded), "questions": questions,
    }
    out.mkdir(parents=True, exist_ok=True)
    draft_path.write_text(json.dumps(document, ensure_ascii=False, indent=1) + "\n")
    (out / CANDIDATES_REPORT.name).write_text(json.dumps(
        {"note": "Every candidate and what the script found for it; the draft takes the first located ones per report.",
         "chosen": [q["id"] for q in questions], "excluded": sorted(excluded), "short": short,
         "candidates": located},
        ensure_ascii=False, indent=1) + "\n")
    (out / CHECKLIST_MD.name).write_text(checklist_markdown(document, reports))
    if images:
        (out / CHECKLIST_HTML.name).write_text(checklist_html(document, reports))
    missing = [item["id"] for item in located if not item["located"]]
    print(json.dumps({"draft": str(draft_path), "questions": len(questions),
                      "pending": sum(q["review"]["status"] != VERIFIED for q in questions),
                      "candidates": len(located), "not_located": missing, "short": short,
                      "checklist": str(out / (CHECKLIST_HTML.name if images else CHECKLIST_MD.name))},
                     ensure_ascii=False, indent=1))
    if short:
        print("not enough located candidates: " + "; ".join(short) + " (see heldout-candidates.json, add candidates)",
              file=sys.stderr)
        return 2
    return 0


def composition(questions: list[dict], languages: dict[str, str]) -> dict:
    cross = [q for q in questions if q["kind"] == "answerable" and languages[q["doc"]] != q["lang"]]
    per_report = {}
    for doc in dict.fromkeys(q["doc"] for q in questions):
        own = [q for q in questions if q["doc"] == doc]
        per_report[doc] = {"answerable": sum(q["kind"] == "answerable" for q in own),
                           "answerable_cross_language": sum(q in cross for q in own),
                           "absent": sum(q["kind"] == "absent" for q in own)}
    return {"count": len(questions), "answerable": sum(q["kind"] == "answerable" for q in questions),
            "absent": sum(q["kind"] == "absent" for q in questions), "answerable_cross_language": len(cross),
            "per_report": per_report}


def freeze(*, reviewer: str | None = None, review: Path | None = None, confirm_all: bool = False,
           sources: Path = SOURCES, out: Path = HERE, frozen: Path = FROZEN, freeze_file: Path = FREEZE,
           expected: dict | None = None) -> int:
    """Write heldout.json and its freeze record once every question carries a person's "verified"."""
    expected = expected or PREREG["sets"]["heldout"]
    draft_path = out / DRAFT.name
    if frozen.exists() or freeze_file.exists():
        raise SystemExit(f"{frozen.name} is already frozen; a revision is a new versioned set and the old results stay")
    marks = json.loads(Path(review).read_text()) if review is not None else {}
    reviewer = (reviewer or marks.get("reviewer") or "").strip()  # the name typed into the check list, if any
    if not reviewer:
        raise SystemExit("--reviewer: who checked the questions against the printed pages?")
    document = json.loads(draft_path.read_text())
    method = "marks kept in the draft"
    if review is not None:
        for q in document["questions"]:
            mark = (marks.get("items") or {}).get(q["id"]) or {}
            if mark.get("proposal") != proposal_hash(q):
                continue  # the mark was given for another proposal of this question (or not at all)
            q["review"] = _review(VERIFIED if mark.get("status") == VERIFIED else PENDING) | {
                "note": mark.get("note", ""), "asked": mark.get("status")}
        method = "review file exported from heldout-checklist.html"
    elif confirm_all:
        for q in document["questions"]:
            q["review"] = _review(VERIFIED)
        method = "the reviewer confirmed every question in conversation"
    draft_path.write_text(json.dumps(document, ensure_ascii=False, indent=1) + "\n")  # marks survive a re-draft
    open_items = [q for q in document["questions"] if q["review"]["status"] != VERIFIED]
    if open_items:
        for q in open_items:
            print(f"{q['id']}: {q['review'].get('asked') or '待人工核对'} {q['review'].get('note', '')}".rstrip(),
                  file=sys.stderr)
        raise SystemExit(f"{len(open_items)} question(s) not verified. To change one, edit it in {draft_path.name} "
                         "and set its review.status to verified once the reviewer agrees; to replace one, run "
                         "draft --exclude ID and have the new question checked")
    languages = {r["key"]: r["language"] for r in json.loads(Path(sources).read_text())["reports"]}
    made = composition(document["questions"], languages)
    for key in ("count", "answerable", "absent", "answerable_cross_language"):
        if made[key] != expected[key]:
            raise SystemExit(f"{key}: {made[key]}, registered {expected[key]}")
    if any(counts != expected["per_report"] for counts in made["per_report"].values()):
        raise SystemExit(f"per-report composition differs from the registered one: {made['per_report']}")
    now = datetime.now(UTC).isoformat()
    for q in document["questions"]:
        q["review"] = {"status": VERIFIED, "label": "已核对", "by": reviewer, "at": now,
                       "note": q["review"].get("note", "")}
        for item in q.get("evidence", []):
            item.pop("box", None)
    document["status"] = "FROZEN"
    document.pop("excluded", None)
    frozen.write_text(json.dumps(document, ensure_ascii=False, indent=1) + "\n")
    freeze_file.write_text(json.dumps({
        "set_id": SET_ID, "recorded_at": now, "verified_by": reviewer, "verified_how": method,
        "files": {frozen.name: sha256(frozen), Path(__file__).name: sha256(Path(__file__))},
        "sources": {"path": str(Path(sources).relative_to(PROJECT)) if Path(sources).is_relative_to(PROJECT) else str(sources),
                    "sha256": sha256(sources)},
        "questions": made["count"], "answerable": made["answerable"], "absent": made["absent"],
        "answerable_cross_language": made["answerable_cross_language"],
        "instruction": "Frozen before any retrieval or model call over these questions. Do not edit; a revision is "
                       "a new versioned set and the old results stay.",
    }, ensure_ascii=False, indent=1) + "\n")
    print(json.dumps({"frozen": str(frozen), "sha256": sha256(frozen), "questions": made["count"],
                      "verified_by": reviewer}, ensure_ascii=False, indent=1))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    drafting = commands.add_parser("draft", help="resolve the candidates against the PDFs and write the check list")
    drafting.add_argument("--reports", type=Path, required=True, help="folder with <key>.pdf (scripts/fetch_real_reports.sh)")
    drafting.add_argument("--exclude", nargs="*", default=[], help="candidate ids a reviewer rejected")
    drafting.add_argument("--no-images", action="store_true", help="skip heldout-checklist.html (page images)")
    freezing = commands.add_parser("freeze", help="write heldout.json once every question is verified")
    freezing.add_argument("--reviewer", help="the person who checked the questions (default: the name in --review)")
    freezing.add_argument("--review", type=Path, help="heldout-review.json exported from the check list")
    freezing.add_argument("--confirm-all", action="store_true",
                          help="the reviewer said every question is right (no review file)")
    args = parser.parse_args(argv)
    if args.command == "draft":
        return draft(args.reports.expanduser().resolve(), exclude=tuple(args.exclude), images=not args.no_images)
    return freeze(reviewer=args.reviewer, review=args.review, confirm_all=args.confirm_all)


if __name__ == "__main__":
    sys.exit(main())
