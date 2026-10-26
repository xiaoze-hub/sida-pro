"""数字对账护栏（2026-10-21 线上：三花智控筹码带被写成强瑞技术的量级）。"""
from src.core.number_audit import annotate, audit, extract_claims

EV = (
    "[数据源: 自算CYQ] 002050 现价 32.91 | 底仓成本(COST50)：36.62 元 ｜ "
    "筹码峰：36.12 元（占比 2.5%） | 70% 筹码带：33.62 – 44.52 元 ｜ "
    "获利盘：9.9% | 现价相对底仓成本：-10.13%"
)


def test_extract_claims_ignores_plain_integers_and_codes():
    claims = extract_claims("301128 现价 87.03，涨 2.75%，成交 3 万手，已连 3 板，2026-10-21")
    assert "87.03" in claims and "2.75%" in claims
    assert "3" not in claims, "纯整数无单位不是硬数字"
    assert not any("2026" in c for c in claims)


def test_audit_passes_real_numbers():
    answer = "002050 现价 32.91，底仓成本 36.62，筹码带 33.62–44.52，获利盘 9.9%，距底仓 -10.13%"
    r = audit(answer, EV)
    assert r["unverified"] == []
    assert r["severe"] is False


def test_audit_flags_cross_symbol_numbers():
    """把强瑞技术量级的数字写到三花头上 —— 必须抓出来。"""
    answer = "002050 现价 32.91，筹码峰 87.5，获利盘 39%，成本带 83.12–91.88"
    r = audit(answer, EV)
    assert set(r["unverified"]) >= {"87.5", "39%", "83.12", "91.88"}
    assert r["severe"] is True


def test_audit_allows_derived_ratio():
    """模型自己算的比率（凭两个读数）不算编造。"""
    r = audit("底仓 36.62、现价 32.91，相差 11.27%", EV)
    assert "11.27%" not in r["unverified"], "两个读数相除得出来的比率不是编造"


def test_annotate_keeps_body_and_appends_warning():
    answer = "002050 现价 32.91，成本带 83.12–91.88"
    out = annotate(answer, audit(answer, EV))
    assert out.startswith(answer), "正文一个字都不许改"
    assert "数字对账" in out and "83.12" in out


def test_empty_answer_is_safe():
    assert audit("", EV)["checked"] == 0
    assert annotate("", audit("", EV)) == ""


def test_unit_rounding_is_allowed():
    """115 万手 是 1,150,000 的合理四舍五入，不算编造。"""
    ev = "外盘 1150000 手"
    r = audit("外盘 115 万手", ev)
    assert "115万" not in r["unverified"]


def test_unit_misstatement_still_caught():
    """109,167 写成 11.6 万（实际 10.9 万）—— 这不是四舍五入，是编。"""
    ev = "外盘 109167 手"
    r = audit("外盘 11.6 万手", ev)
    assert "11.6万" in r["unverified"]


def test_six_digit_quantity_is_not_treated_as_code():
    """125,628 手 是数量，不是股票代码 —— 抹掉它会让证据池缺数、把真数判成编造。"""
    ev = "外盘 125628 手 / 内盘 111261 手"
    r = audit("外盘 125,628 手 / 内盘 111,261 手", ev)
    assert r["unverified"] == []


def test_market_tagged_code_is_stripped():
    claims = extract_claims("002050.SZ 现价 32.91，CN:002050 涨 1.2%")
    assert "002050" not in claims and "32.91" in claims


def test_arithmetic_from_sourced_numbers_is_not_flagged():
    """2026-10-22 误报回归：浮亏是算出来的，不是编的。

    真实回答里写「成本 36.522、现价 35.77 → -0.75 元/股 × 4600 股 ≈ -3,450 元」，
    旧实现对账把 -0.75 与 -3,450 全判成"找不到出处"，用户在答案末尾看到假警告。
    """
    from src.core.number_audit import audit

    evidence = "价格 35.77 元，涨跌幅 -0.3066%，成交量约 42.1 万手\n持仓成本 36.522 元，持仓 4600 股"
    answer = (
        "现价 35.77 元，今日微跌 0.31%。持仓成本 36.522 元，"
        "浮亏约 -2.06%（-0.75 元/股 × 4600 股 ≈ -3,450 元）。"
    )
    assert audit(answer, evidence, "")["unverified"] == []


def test_fabricated_price_still_flagged():
    from src.core.number_audit import audit

    r = audit("现价 41.23 元，涨 5.6%", "价格 35.77 元，涨跌幅 -0.3066%", "")
    assert "41.23元" in r["unverified"]


def test_rounded_percent_matches_evidence():
    """-0.3066% 写成 0.31% 是四舍五入，不是编造。"""
    from src.core.number_audit import audit

    assert audit("今日微跌 0.31%", "涨跌幅 -0.3066%", "")["unverified"] == []
