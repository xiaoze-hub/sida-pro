"""结构化未对账数字：审核只报告、不改正文（借 qovest E35「只读审核 + 阻断权」纪律）。

设计约束（照抄结论，别改）：
- `unverified` **保持 list[str]**。`src/core/evidence_row_audit.py` 用 `str(t)` 后做
  `c in bad_tokens` 的集合成员判断，`src/web/api/copilot_message_save.py` 用 `len(r["unverified"])`
  统计条数；把元素换成 dict 会让行级对账静默失效，而这两个文件不在本次可改范围。
- 结构化明细放在**新增键** `unverified_items`：`{"value", "kind", "context"}`，
  `kind ∈ {price, percent, amount, other}`，`context` ≤ 40 字，纯 JSON 可序列化。
- `annotate()` 只读 `unverified / magnitude / checked / severe`，因此文本输出必须逐字符不变。
"""
import json

from src.core.number_audit import annotate, audit

# 证据原文：现价 1680.52 / 涨幅 2.31% / 成交额 125628.31万元
EVIDENCE = "600519 现价 1680.52 涨幅 2.31% 成交额 125628.31万元"

# 改前（commit 2ebdc87c）跑出来的真实输出，逐字符冻结：annotate 一旦被改动这里就炸。
GOLDEN_PLAIN = (
    "## 【依据】\n- 现价 1680.52\n- 主力净流入 1.23 亿\n"
    "\n---\n⚠️ 数字对账：1 个数字（1.23亿）未在工具返回中找到出处，可能是推算值，请以原始读数为准。"
)
GOLDEN_MAGNITUDE = (
    "现价 32.80 元，涨幅 4.15%，主力净流出 -61.93 万，浮亏 -3450 元，筹码 87.5"
    "\n\n---\n⚠️ **单位量级不一致**：回答写 -61.93万，取数原文 -6193万（差 100 倍）。"
    "这类差异一般是万/亿换算错误（把万元当成百万元），**请以取数原文为准**。"
    "\n\n其余 5/5 个数字（32.80元、4.15%、-61.93万、-3450元、87.5）"
    "未在工具返回中找到出处，请以原始读数为准。"
)


def _values(result):
    return [it["value"] for it in result["unverified_items"]]


# ① 编造证据里没有的价格 → 进 unverified_items
def test_fabricated_price_is_listed():
    answer = "现价 1712.88，涨幅 2.31%"
    result = audit(answer, EVIDENCE)

    assert "1712.88" in _values(result)
    item = next(it for it in result["unverified_items"] if it["value"] == "1712.88")
    assert item["kind"] == "price"
    assert isinstance(item["context"], str) and item["context"], "前端要能定位到原句"
    assert len(item["context"]) <= 40
    assert "1712.88" in item["context"]


# ② 证据里查得到的数字 → 不进（已对上的数字绝不能污染列表）
def test_verified_numbers_are_not_listed():
    result = audit("现价 1680.52，涨幅 2.31%", EVIDENCE)

    assert result["unverified"] == []
    assert result["unverified_items"] == []
    assert result["checked"] == 2


def test_only_miss_is_listed_when_hit_and_miss_are_mixed():
    result = audit("现价 1680.52，涨幅 8.88%", EVIDENCE)

    assert _values(result) == ["8.88%"], "2.31% 在证据里，不该被顺带标出来"
    assert result["checked"] == 2


# ③ 百分比 / 金额 / 量各自归类
def test_kind_classification_for_percent_amount_and_volume():
    answer = "换手率 7.77%，主力净流出 -8.88亿，成交量 999.99万手，筹码峰 87.5"
    result = audit(answer, EVIDENCE)
    kinds = {it["value"]: it["kind"] for it in result["unverified_items"]}

    assert kinds["7.77%"] == "percent"
    assert kinds["-8.88亿"] == "amount"
    assert kinds["999.99万手"] == "other", "万手是量不是金额，折成元会造假 100 倍关系"
    assert kinds["87.5"] == "price", "无单位小数按价格口径归类"
    assert set(kinds) == set(result["unverified"]), "两个列表必须一一对应"


def test_payload_is_json_serializable():
    result = audit("现价 1712.88，涨幅 8.88%", EVIDENCE)
    round_trip = json.loads(json.dumps(result, ensure_ascii=False))

    assert round_trip["unverified_items"] == result["unverified_items"]
    assert all(set(it) == {"value", "kind", "context"} for it in round_trip["unverified_items"])


# ④ annotate 输出与改前逐字符一致
def test_annotate_output_is_byte_identical_to_pre_change():
    plain = "## 【依据】\n- 现价 1680.52\n- 主力净流入 1.23 亿\n"
    assert annotate(plain, audit(plain, EVIDENCE)) == GOLDEN_PLAIN

    mag_answer = "现价 32.80 元，涨幅 4.15%，主力净流出 -61.93 万，浮亏 -3450 元，筹码 87.5"
    mag_evidence = "现价 32.15 涨幅 3.02% 主力净流出 -6193万 成本 30.05 125628 手"
    mag_result = audit(mag_answer, mag_evidence)
    assert mag_result["magnitude"], "这条样本必须走量级分支，否则冻不住那段文案"
    assert annotate(mag_answer, mag_result) == GOLDEN_MAGNITUDE


def test_annotate_still_consumes_plain_string_list():
    """annotate 的入参契约没变：老调用方传 list[str] 也必须照旧工作。"""
    legacy = {"checked": 2, "unverified": ["1.5元", "2.5元"], "ratio": 1.0, "severe": True}
    assert "1.5元、2.5元" in annotate("正文", legacy)


# ⑤ 空证据：不抛异常，所有硬数字都算未对账
def test_empty_evidence_does_not_raise():
    answer = "现价 1712.88，涨幅 8.88%"
    result = audit(answer, "")

    assert result["checked"] == 2
    assert len(result["unverified_items"]) == 2
    assert all(it["context"] for it in result["unverified_items"])


def test_empty_answer_and_both_empty_are_safe():
    for answer, evidence in (("", EVIDENCE), ("", ""), (None, None)):  # type: ignore[var-annotated]
        result = audit(answer, evidence)  # type: ignore[arg-type]
        assert result["checked"] == 0
        assert result["unverified"] == [] and result["unverified_items"] == []
        assert result["severe"] is False
        assert annotate(answer or "", result) == (answer or "")


# 兼容性硬约束：老键一个都不能少
def test_backward_compatible_keys_are_all_present():
    result = audit("现价 1712.88", EVIDENCE)

    assert {"checked", "unverified", "magnitude", "ratio", "severe"} <= set(result)
    assert isinstance(result["unverified"], list)
    assert all(isinstance(t, str) for t in result["unverified"]), "evidence_row_audit 按字符串消费"
