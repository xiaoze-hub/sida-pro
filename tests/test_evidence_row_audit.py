"""依据行数字对账：只陈述"这个读数在本轮取数里找不到"，不归属来源、不判编造。"""
from src.core.evidence_row_audit import audit_rows, evidence_section, split_rows

ANSWER = """## 【结论】
神剑股份现价 9.65 元，跌 -0.52%。

## 【依据】
| 维度 | 关键读数 | 口径 |
| --- | --- | --- |
| 实时行情 | 现价 9.65 元 / 昨收 9.70 元 | 腾讯 |

- 换手率 4.82%，量比 0.98
- 主力净流入 1.23 亿

## 【预案】
若跌破 9.32 元则减仓。
"""

EVIDENCE = (
    "002361 现价 9.65 昨收 9.70 成交额 163278.31万元 "
    "换手率 4.82% 量比 0.98 最低 9.32 主力净流入 9999万"
)


def test_evidence_section_is_isolated_from_other_sections():
    section = evidence_section(ANSWER)
    assert section is not None
    assert "实时行情" in section
    assert "【预案】" not in section and "若跌破" not in section


def test_missing_section_returns_none_instead_of_whole_answer():
    assert evidence_section("【结论】直接给结论，没有依据段") is None
    assert evidence_section("") is None


def test_table_header_and_separator_are_not_rows():
    rows = split_rows(evidence_section(ANSWER) or "")
    assert rows and all("关键读数" not in r for r in rows)
    assert rows[0].startswith("实时行情")


def test_row_with_unsupported_number_is_flagged_alone():
    out = audit_rows(ANSWER, EVIDENCE)
    assert out["basis"] == "number_containment"
    flagged = [r for r in out["rows"] if r["unverified"]]
    assert len(flagged) == 1
    assert flagged[0]["unverified"] == ["1.23亿"]
    # 同一段里其它行不受牵连（整段脚注做不到这一点）
    clean = [r for r in out["rows"] if not r["unverified"]]
    assert len(clean) >= 2


def test_supported_rows_are_not_flagged():
    answer = "## 【依据】\n- 现价 9.65 元，昨收 9.70 元\n- 换手率 4.82%"
    out = audit_rows(answer, EVIDENCE)
    assert out["rows"] and all(not r["unverified"] for r in out["rows"])
    assert out["unverified_total"] == 0 and out["checked"] == 3


def test_rows_without_hard_numbers_are_skipped_but_reason_stays_empty():
    out = audit_rows("## 【依据】\n- 该股今日走弱，资金分歧\n", EVIDENCE)
    assert out["rows"] == []
    assert out["reason"] == "no_numeric_rows"
    assert out["checked"] == 0


def test_no_section_is_reported_not_faked():
    out = audit_rows("## 【结论】直接结论 9.65 元", EVIDENCE)
    assert out["rows"] == [] and out["reason"] == "no_evidence_section"


def test_empty_evidence_flags_every_number_rather_than_passing():
    out = audit_rows("## 【依据】\n- 现价 9.65 元", "")
    assert out["unverified_total"] == 1


def test_decimal_rounding_is_not_treated_as_fabrication():
    """工具给 -0.3066，回答写 0.31 —— 是这个平台既有口径，行级也必须一致。"""
    out = audit_rows("## 【依据】\n- 涨跌幅 0.31%", "涨跌幅 -0.3066%")
    assert out["unverified_total"] == 0


def test_derived_product_is_not_flagged():
    """浮亏 -3,450 元 = 价差 0.752 × 4600 股：派生值不是编造。"""
    answer = "## 【依据】\n- 浮亏 -3,450 元"
    evidence = "成本 10.402 现价 9.65 持仓 4600 股 价差 0.752"
    out = audit_rows(answer, evidence)
    assert out["unverified_total"] == 0


# ---------------------------------------------------------------------------
# 审查意见回归（2026-10-30 第二轮）
# ---------------------------------------------------------------------------

def test_footer_from_annotate_is_not_counted_as_an_evidence_row():
    """annotate 会在结尾追加"⚠️ 数字对账：…（9.65、1.23亿）"。它若落进依据段，
    就会被当成一条依据行再核一遍，把脚注自己报回来。"""
    answer = (
        "## 【依据】\n- 现价 9.65 元\n\n---\n"
        "⚠️ 数字对账：2 个数字（1.23亿、4.56亿）未在工具返回中找到出处"
    )
    rows = split_rows(evidence_section(answer) or "")
    assert rows == ["现价 9.65 元"]
    out = audit_rows(answer, "现价 9.65")
    assert out["unverified_total"] == 0, "脚注里的数字不许被重新计入"


def test_sections_after_evidence_are_boundaries_too():
    """真实回答常用【数据缺口】【免责】收尾，不带【预案】/【口径】；它们不是依据行。"""
    answer = (
        "## 【依据】\n- 现价 9.65 元\n\n"
        "## 【数据缺口】\n- 北向资金缺失 3.33 亿\n\n"
        "## 【免责】\n- 以上不构成投资建议，回撤 8.88% 风险自负\n"
    )
    rows = split_rows(evidence_section(answer) or "")
    assert rows == ["现价 9.65 元"]


def test_only_one_audit_call_for_many_rows(monkeypatch):
    """派生集合是 O(池²)：逐行各调一次会让一次回答付 N 倍代价（实测 12 行 = 3.8s）。"""
    import src.core.evidence_row_audit as mod

    calls = []
    real = mod._audit_numbers

    def counting(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(mod, "_audit_numbers", counting)
    rows = "\n".join(f"| 维度{i} | 读数 {i}.5 元 | 腾讯 |" for i in range(12))
    answer = "## 【依据】\n| 维度 | 关键读数 | 口径 |\n| --- | --- | --- |\n" + rows
    audit_rows(answer, "读数 1.5 元 2.5 元 3.5 元")
    assert len(calls) == 1


def test_rows_are_capped_and_reported_as_truncated(monkeypatch):
    import src.core.evidence_row_audit as mod

    monkeypatch.setattr(mod, "MAX_ROWS", 3)
    rows = "\n".join(f"- 读数 {i}.5 元" for i in range(10))
    out = audit_rows("## 【依据】\n" + rows, "读数 1.5 元")
    assert len(out["rows"]) == 3 and out["truncated"] is True
