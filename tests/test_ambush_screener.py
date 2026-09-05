"""埋伏漏斗测试(规则层全 mock, 不碰行情/LLM)。"""

from __future__ import annotations

from src.core import catalyst_screener as S


def _cal():
    return [
        {"date": "2026-09-08", "type": "解禁", "symbol": "000001", "title": "A解禁", "detail": ""},
        {"date": "2026-09-20", "type": "宏观窗口", "symbol": "", "title": "PMI", "detail": ""},
        {"date": "2026-09-08", "type": "解禁", "symbol": "000001", "title": "A解禁重复", "detail": ""},
    ]


def test_rule_score_drops_limitup():
    assert S._rule_score({"date": "2026-09-08"}, {"change_pct": 10.0}, "2026-09-05") is None
    assert S._rule_score({"date": "2026-09-08"}, {"change_pct": 9.0}, "2026-09-05") is None
    s = S._rule_score({"date": "2026-09-06"}, {"change_pct": 1.0, "volume_ratio": 2.0}, "2026-09-05")
    assert s is not None and s > 30


def test_build_ambush_list_funnel(monkeypatch):
    import src.core.catalyst_screener as M

    monkeypatch.setattr(M, "_snapshots", lambda syms: {"000001": {"change_pct": 2.0, "volume_ratio": 1.5}})

    def _fake_llm(sym, extra):
        assert "解禁" in extra[0]
        return {
            "catalyst": "解禁压力测试",
            "expectation_gap": {"level": "高", "note": "涨2%未反应"},
            "reason": "近端解禁但位置低",
            "beneficiary_pool": ["同板块龙头XYZ"],
        }

    monkeypatch.setattr(
        "src.core.beneficiary_resolver.resolve_beneficiaries",
        lambda names: [{"name": n, "symbol": "000002", "via": "exact", "confidence": "高"} for n in names],
    )
    out = M.build_ambush_list(_cal(), today="2026-09-05", topn=12, run_llm=_fake_llm)
    assert len(out) == 1 and out[0]["symbol"] == "000001" and out[0]["gap"] == "高"
    assert out[0]["codes"] and out[0]["codes"][0]["symbol"] == "000002"


def test_empty_calendar_or_snapshot(monkeypatch):
    import src.core.catalyst_screener as M

    assert M.build_ambush_list([], today="2026-09-05") == []
    monkeypatch.setattr(M, "_snapshots", lambda syms: {})
    assert M.build_ambush_list(_cal(), today="2026-09-05") == []
