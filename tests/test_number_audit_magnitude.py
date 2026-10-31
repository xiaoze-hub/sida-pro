"""量级不一致护栏（2026-10-31）。

线上实例：600111 同日同源，主力净流出被写成「-61.93 万」，取数原文是「-6193 万」——
差 100 倍。原有对账只会把它归成「可能是推算值」，语气太软，用户看不出是单位换算错。
"""

import pytest

from src.core.number_audit import annotate, audit


EVIDENCE = (
    "get_capital_flow 返回：主力净流出 -6193万；超大单 -6241万；大单 +48万；"
    "参与度 77%；买占 46%；成交额 10.43亿。"
)


def test_100x_unit_error_is_detected():
    answer = "主力净流出 -61.93 万（超大单 -62.41 万），处于持续派发。"
    r = audit(answer, EVIDENCE)
    assert r["magnitude"], "差 100 倍必须被识别出来"
    hit = r["magnitude"][0]
    assert hit["claim"].replace(" ", "") == "-61.93万"
    assert "6193" in hit["evidence"].replace(",", "")
    assert int(hit["factor"]) == 100
    assert r["severe"] is True, "量级错误一条就该算严重"


def test_correct_value_is_not_flagged():
    answer = "主力净流出 -6193 万（超大单 -6241 万）。"
    r = audit(answer, EVIDENCE)
    assert r["magnitude"] == []


def test_missing_wan_unit_is_detected_as_10000x():
    """把「6193 万」写成「6193 元」= 差 10000 倍（万整个丢了）。"""
    r = audit("主力净流出 6193 元。", "主力净流出 6193万。")
    assert r["magnitude"] and int(r["magnitude"][0]["factor"]) == 10000


def test_percentage_claimed_as_money_is_not_magnitude_error():
    """百分比不是金额：不能因为恰好差 100 倍就当单位错。"""
    ev = "买撤单率 88.0%，卖撤单率 65.6%。"
    r = audit("买撤单率 0.88%。", ev)
    assert r["magnitude"] == []


def test_plain_arithmetic_slip_stays_unverified_not_magnitude():
    """差 2 倍这类普通算错仍走「未找到出处」，不冒充量级错误。"""
    r = audit("主力净流出 -3096 万。", EVIDENCE)
    assert r["magnitude"] == []
    assert r["unverified"]


def test_annotation_says_which_side_is_right():
    answer = "主力净流出 -61.93 万。"
    out = annotate(answer, audit(answer, EVIDENCE))
    assert "单位量级不一致" in out
    assert "-6193" in out
    assert "请以取数原文为准" in out
    assert out.startswith("主力净流出 -61.93 万。"), "不改正文，只在末尾追加"


def test_thousands_separator_and_spaces_do_not_break_detection():
    r = audit("主力净流出 -61.93 万元。", "-6,193 万元")
    assert r["magnitude"] and int(r["magnitude"][0]["factor"]) == 100


# ── 审查回归（2026-10-31）：非货币单位不许折成"元"，否则正确回答会被判量级错 ──

def test_shou_and_percent_are_not_folded_as_yuan():
    """6193手 × ¥100 ≈ 6193万元：两者同数字不同单位，不能判成单位写错。"""
    ev = "成交额 6193万元；成交量 6193手。"
    r = audit("成交额 6193 万元。", ev)
    assert r["magnitude"] == [], "同量纲出处存在时必须算已验证"
    assert r["severe"] is False


def test_lots_at_100_yuan_do_not_fake_a_10000x_error():
    ev = "成交量 12562手；成交额 1.2562亿元。"
    r = audit("成交额 1.2562 亿元。", ev)
    assert r["magnitude"] == []


def test_percent_token_never_triggers_magnitude():
    r = audit("买撤单率 0.88%。", "买撤单率 88.0%；主力净流出 -6193万。")
    assert r["magnitude"] == []


def test_wan_shou_is_not_money():
    """`量 23.94 万手 / 额 8.42 亿`：答案写 23.94亿元不能被"23.94万手"判成量级错。"""
    ev = "量 23.94 万手；额 8.42 亿；成交额 23.94亿元。"
    r = audit("成交额 23.94 亿元。", ev)
    assert r["magnitude"] == []
    assert r["severe"] is False


def test_same_number_other_unit_does_not_shadow_correct_source():
    """证据池里先出现 61.93元、后出现 61.93万元：存在同量纲出处就不能判错。"""
    ev = "现价 61.93 元；成交额 61.93 万元。"
    r = audit("成交额 61.93 万元。", ev)
    assert r["magnitude"] == []


def test_bare_price_is_not_treated_as_yuan():
    """`现价 61.93`（无单位）不能和 `成交额 61.93万元` 比出 10000 倍。"""
    ev = "成交额 61.93万元；主力净流出 -6193万。"
    r = audit("现价 61.93。", ev)
    assert r["magnitude"] == []
    assert r["severe"] is False


def test_explicit_yuan_still_detected_against_wan():
    r = audit("成交额 61.93 元。", "成交额 61.93万元。")
    assert r["magnitude"] and int(r["magnitude"][0]["factor"]) == 10000


def test_bare_evidence_price_does_not_collide_with_money_claim():
    """池里的裸数 `现价 61.93` 不是钱：不能和答里的 `61.93万元` 比出 10000 倍。"""
    r = audit("成交额 61.93 万元。", "现价 61.93；主力净流出 -6193万。")
    assert r["magnitude"] == []
    assert r["severe"] is False


def test_yi_gu_is_volume_not_money():
    """`437.69亿股` 是股数：既不能被当金额，也不能和 `437.69万`（金额）比出 10000 倍。"""
    assert audit("产量 437.69 亿股。", "产量 437.69万股；成交额 8.42亿。")["magnitude"] == []
    assert audit("产量 437.69 亿股。", "成交额 437.69万。")["magnitude"] == []


def test_wanyiyuan_billion_is_money():
    r = audit("成交额 3.2 万亿元。", "成交额 32000亿元。")
    assert r["magnitude"] == []
