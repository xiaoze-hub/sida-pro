"""AI 建议证据化(2026-10-10)的钉子: 证据链 + 失效条件 + 置信度校准 + 历史相似情形 + 提示词抽取。

覆盖(禁真网络, 纯函数 + monkeypatch):
  ① 证据链结构完整(triggers/as_of/invalidation 三件套);
  ② 失效条件必填(缺失 → 确定性默认, 标记 invalidation_defaulted, 绝不为空);
  ③ as_of 缺失/非今日显式(不默认成今日);
  ④ 置信度校准映射(实测命中率封顶)+ 样本不足显式「未校准」;
  ⑤ 历史相似情形 n<min_sample 显式样本不足不给百分比;
  ⑥ 提示词抽取后行为不变(文件=常量; 纯函数输出逐字不变);
  ⑦ 三个 AI/规则结论出口(resonance / intent / GS 解释面 / decision)都带上结构化证据。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.core import evidence_chain as ec

REPO_ROOT = Path(__file__).parent.parent

# ── ① / ② / ③ 证据链 ────────────────────────────────────────────────────────
def test_evidence_chain_structure_complete():
    ev = ec.build_evidence_chain(triggers=["趋势 G区间", "活跃度 26.68 ≥ 3"], as_of="20260911", today="20260911")
    assert set(("triggers", "as_of", "as_of_is_today", "as_of_note", "invalidation", "invalidation_defaulted")) <= set(ev)
    assert ev["triggers"] == ["趋势 G区间", "活跃度 26.68 ≥ 3"]
    assert ev["as_of"] == "20260911" and ev["as_of_is_today"] is True
    assert "今日" in ev["as_of_note"]
    assert ev["invalidation"]  # 必填非空
    assert ev["invalidation_defaulted"] is True  # 未给 → 回默认


def test_invalidation_required_falls_back_nonempty():
    ev = ec.build_evidence_chain(triggers=["x"], invalidation=[])
    assert ev["invalidation"] == [ec.DEFAULT_INVALIDATION]
    assert ev["invalidation_defaulted"] is True
    # 领域化默认优先于通用兜底
    ev2 = ec.build_evidence_chain(triggers=["x"], default_invalidation=["若 Y 则作废"])
    assert ev2["invalidation"] == ["若 Y 则作废"]


def test_invalidation_used_when_provided():
    ev = ec.build_evidence_chain(triggers=["x"], invalidation=["若 Z 则作废"])
    assert ev["invalidation"] == ["若 Z 则作废"]
    assert ev["invalidation_defaulted"] is False


def test_as_of_missing_is_explicit_not_today():
    ev = ec.build_evidence_chain(triggers=["x"], as_of=None)
    assert ev["as_of"] is None
    assert ev["as_of_is_today"] is False  # 缺失**不**默认成今日
    assert "时点缺失" in ev["as_of_note"]


def test_as_of_non_today_marked_lagging():
    ev = ec.build_evidence_chain(triggers=["x"], as_of="20260910", today="20260911")
    assert ev["as_of_is_today"] is False
    assert "非今日" in ev["as_of_note"] and "20260910" in ev["as_of_note"]


# ── ④ 置信度校准(缺样本显式未校准, 禁编造) ───────────────────────────────────
def _stats(n: int, hit_rate: float | None, *, kind: str = "resonance3", min_sample: int = 30) -> dict:
    insuff = hit_rate is None or n < min_sample
    return {
        "min_sample": min_sample,
        "rows": [{"signal_kind": kind, "horizons": {"t1": {"n": n, "hit_rate": hit_rate, "insufficient": insuff}}}],
    }


def test_calibration_caps_raw_by_measured_hit_rate():
    out = ec.calibrate_confidence(0.9, "resonance3", _stats(40, 0.55))
    assert out["calibrated"] is True
    assert out["cap"] == 0.55 and out["value"] == 0.55  # 原始 0.9 被命中率 0.55 封顶
    assert out["n"] == 40 and out["hit_rate"] == 0.55


def test_calibration_below_cap_keeps_raw():
    out = ec.calibrate_confidence(0.3, "resonance3", _stats(40, 0.55))
    assert out["value"] == 0.3 and out["calibrated"] is True


def test_calibration_insufficient_is_uncalibrated_no_number():
    out = ec.calibrate_confidence(0.9, "resonance3", _stats(3, 0.67))
    assert out["calibrated"] is False and out["value"] is None and out["cap"] is None
    assert "未校准" in out["note"] and "样本不足" in out["note"]


def test_calibration_missing_kind_is_uncalibrated():
    out = ec.calibrate_confidence(0.9, "resonance3", {"min_sample": 30, "rows": []})
    assert out["calibrated"] is False and out["value"] is None and "未校准" in out["note"]
    out_none = ec.calibrate_confidence("高", None, None)
    assert out_none["calibrated"] is False and "未校准" in out_none["note"]


# ── ⑤ 历史相似情形 ─────────────────────────────────────────────────────────
def test_similarity_sentence_when_enough_samples():
    out = ec.historical_similarity("resonance3", _stats(40, 0.55))
    assert out["insufficient"] is False and out["n"] == 40 and out["up"] == 22
    assert out["sentence"] == "历史上 40 次相似情形, 22 次后续上涨"


def test_similarity_insufficient_no_percentage():
    out = ec.historical_similarity("resonance3", _stats(12, 0.5))
    assert out["insufficient"] is True and out["up"] is None
    assert "样本不足" in out["sentence"] and "12" in out["sentence"]


# ── ⑥ 提示词抽取后行为不变 ──────────────────────────────────────────────────
@pytest.mark.parametrize(
    "mod_name,prompt_file,attr",
    [
        ("src.core.resonance_ai", "resonance_ai.txt", "SYSTEM_PROMPT"),
        ("src.core.resonance_ai", "resonance_ai_batch.txt", "BATCH_SYSTEM_PROMPT"),
        ("src.core.intent_explain", "intent_explain.txt", "_SYSTEM_PROMPT"),
    ],
)
def test_prompt_extracted_from_file_single_source(mod_name, prompt_file, attr):
    import importlib

    mod = importlib.import_module(mod_name)
    loaded = getattr(mod, attr)
    file_text = (REPO_ROOT / "prompts" / prompt_file).read_text(encoding="utf-8").rstrip("\n")
    assert loaded == file_text  # 单一事实来源: 常量即文件内容
    assert loaded.strip()  # 非空


def test_prompt_extraction_keeps_parse_behavior():
    """抽取后: build_user_content/parse 对既有字段逐字不变, 且容忍新增 invalidation 字段。"""
    from src.core import resonance_ai as rai

    detail = {"trade_date": "20260911", "trend": "G区间", "activity": 26.68, "level": "大牛", "fund_net": 2.3e8, "level3": "强"}
    txt = rai.build_user_content("300563", "神宇股份", detail, [{"date": "20260910", "activity": 20.1}])
    assert "- 趋势(GS): G区间" in txt and "+2.30亿" in txt
    ok = rai.parse_ai_verdict(
        '{"resonance":"强共振","confidence":1.4,"summary":"三对","reasons":["G区"],"risks":[],"watch":[],"missing":[],"invalidation":["若趋势转S则作废"]}'
    )
    assert ok["resonance"] == "强共振" and ok["confidence"] == 1.0 and ok["reasons"][0] == "G区"
    assert ok["invalidation"] == ["若趋势转S则作废"]
    # 旧输出(无 invalidation 字段)仍可解析 → 行为不变
    old = rai.parse_ai_verdict('{"resonance":"弱共振","confidence":0.6}')
    assert old["resonance"] == "弱共振" and old["invalidation"] == []


# ── ⑦ 三个出口的证据化 ─────────────────────────────────────────────────────
def test_resonance_verdict_evidence_deterministic_triggers_and_default_invalidation():
    from src.core import resonance_ai as rai

    detail = {"trade_date": "20260911", "trend": "G区间", "activity": 26.68, "level": "大牛", "fund_net": 2.3e8}
    ev = rai.build_verdict_evidence(detail, {"invalidation": []})
    assert any("趋势 G区间" in t for t in ev["triggers"])
    assert any("活跃度 26.68" in t for t in ev["triggers"])
    assert any("流入 2.30亿" in t for t in ev["triggers"])
    assert ev["invalidation"] and ev["invalidation_defaulted"] is True
    assert ev["as_of"] == "20260911"
    # LLM 给了失效条件 → 采用之, 不回落
    ev2 = rai.build_verdict_evidence(detail, {"invalidation": ["若资金转净流出则作废"]})
    assert ev2["invalidation"] == ["若资金转净流出则作废"]


def test_intent_evidence_triggers_and_invalidation_required():
    from src.core import intent_explain as ie

    dark = {"trade_date": "20260911", "signal": "超大单净流入", "inner_outer": {"buy_pct": 58.3}, "divergence": {"type": "托盘出货"}}
    ev = ie.build_intent_evidence(dark, {"invalidation": ""})
    assert any("规则结论" in t for t in ev["triggers"])
    assert any("内盘买占比" in t for t in ev["triggers"])
    assert any("托盘出货" in t for t in ev["triggers"])
    assert ev["invalidation"] and ev["invalidation_defaulted"] is True


def test_signal_evidence_from_factors():
    from src.core import signal_explain as se

    item = {"rank_score": 80, "snapshot_date": "2026-09-11", "score_breakdown": {"alpha_score": 5.0, "risk_penalty": 3.0}}
    se.enrich_signal(item)
    ev = item["evidence_chain"]
    assert ev["as_of"] == "20260911"
    assert any("正向因子" in t for t in ev["triggers"])
    assert ev["invalidation"]  # 失效条件非空(即便 LLM 未给)


def test_decision_evidence_from_parts():
    out = {
        "verdict": "动手",
        "phase": "向好",
        "parts": {"trend": "G信号", "activity": 26.68, "fund_net": 2.3e8},
        "computed_at": "2026-10-10T07:00:00",
    }
    extra = ec.build_decision_evidence(out, stats=_stats(40, 0.55))
    ev = extra["evidence"]
    assert any("趋势 G信号" in t for t in ev["triggers"])
    assert ev["invalidation"] and "动手" in ev["invalidation"][0]
    assert extra["similar"]["n"] == 40 and extra["similar"]["insufficient"] is False


# ── load_stats: 账本不可用降级为未校准, 绝不抛 ─────────────────────────────
def test_load_stats_degrades_on_engine_failure(monkeypatch):
    import src.db.session as dbs

    def boom():
        raise RuntimeError("no engine")

    monkeypatch.setattr(dbs, "get_read_engine", boom)
    out = ec.load_stats()
    assert out["rows"] == [] and out["min_sample"] == ec.DEFAULT_MIN_SAMPLE
    assert "不可用" in out["note"]


def test_decision_endpoint_attach_evidence_appends_not_replaces(monkeypatch):
    """决策端点证据装配: **追加**证据/相似字段, **不改** verdict/理由。"""
    import src.core.evidence_chain as ec_mod
    from src.web.api import decision as decapi

    monkeypatch.setattr(ec_mod, "load_stats", lambda **kw: _stats(40, 0.55, kind="resonance3"))
    out = {
        "verdict": "看看",
        "reason": "看看: 趋势G、活跃度26.68",
        "phase": "分歧",
        "parts": {"trend": "G信号", "activity": 26.68, "fund_net": None},
        "computed_at": "2026-10-10T07:00:00",
    }
    decapi._attach_evidence(out)
    assert out["verdict"] == "看看" and out["reason"].startswith("看看:")  # 原字段零改动
    assert out["evidence"]["invalidation"]
    assert out["similar"]["insufficient"] is False and out["similar"]["n"] == 40
