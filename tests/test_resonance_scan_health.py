"""共振扫描分片健康度测试(2026-10-08, P0-2 修复)。

过去 `_fetch_daily`/`_fetch_funds` 每片 `except → continue` 不留痕, TQ 断链时静默
返回空 dict, 上游却报 `ok=True, scanned=0` —— 断链三天无人发现。本文件锁定:
- TQ 日线全断 → `ok=False` + `reason` + 分片计数;
- 个别片失败(异常/空返回) → `ok=True` 但 `chunks_failed>0`(不静默);
- 资金全断 → 同样显式失败, 不静默降级;
- daily_job 原样透传 ok/健康度。

禁令: 断言强度不得降低; 测试不触真实网络(TQ rpc 一律 monkeypatch)。
"""
from __future__ import annotations

import src.core.resonance_scan as rs


def _codes(n: int) -> list[tuple[str, str]]:
    return [(f"{600000 + i:06d}.SH", f"股{i}") for i in range(n)]


def _bars_for(codes):
    return {
        c: {"Date": ["20260911"], "Open": [1.0], "Close": [2.0],
            "High": [2.0], "Low": [1.0], "Volume": [100.0]}
        for c in codes
    }


def _patch_eval(monkeypatch):
    monkeypatch.setattr(
        rs,
        "evaluate_one",
        lambda bars, fund: {"trend": "G区间", "activity": 4.0, "level": "强势",
                            "fund_net": fund, "hits": [True, True, True],
                            "resonance": True, "near": False},
    )
    monkeypatch.setattr(rs, "_upsert", lambda rows: len(rows))


def test_scan_all_outage_is_explicit_failure(monkeypatch):
    """TQ 全断(零数据): 不得再返回 ok=True, scanned=0 —— 必须 ok=False + 分片计数。"""
    monkeypatch.setattr(rs, "_stock_pool", lambda: _codes(250))
    import marketdata.vendors.tq as tq

    def _down(*a, **k):
        raise RuntimeError("tdx gateway down")

    monkeypatch.setattr(tq, "tq_rpc", _down)
    out = rs.scan()
    assert out["ok"] is False
    assert out["scanned"] == 0
    assert "TQ 日线全断" in out["reason"]
    assert out["chunks_total"] == 3 and out["chunks_failed"] == 3
    # 完整性契约: 失败体也带 complete=False + note 显式
    assert out["complete"] is False and out["note"]


def test_scan_partial_chunk_failure_is_not_silent(monkeypatch):
    """个别片失败: 仍 ok=True, 但必须带 chunks_failed>0 + complete=False + note(不静默)。"""
    _patch_eval(monkeypatch)
    monkeypatch.setattr(rs, "_stock_pool", lambda: _codes(550))   # 6 片
    import marketdata.vendors.tq as tq

    state = {"daily_calls": 0}

    def _rpc(name, params, timeout=None):
        if name == "get_market_data":
            state["daily_calls"] += 1
            if state["daily_calls"] == 3:      # 第 3 片断链
                raise RuntimeError("chunk3 down")
            if state["daily_calls"] == 5:      # 第 5 片空返回(rpc 通但无数据)
                return {}
            return _bars_for(params["stock_list"])
        if name == "formula_process_mul_zb":
            return {c: {"主力资金": 100.0} for c in params["stock_list"]}
        return {}

    monkeypatch.setattr(tq, "tq_rpc", _rpc)
    out = rs.scan()
    assert out["ok"] is True
    assert out["chunks_total"] == 6 and out["chunks_failed"] == 2
    assert out["scanned"] > 0
    assert out["fund_chunks_total"] >= 1
    # 部分失败不是全市场口径: complete=False 且 note 显式列出失败片
    assert out["complete"] is False
    assert out["note"] and "日线失败 2/6 片" in out["note"]


def test_scan_all_success_is_complete(monkeypatch):
    """全部成功: complete=True + note=None + 计数全 0(完整性契约正向)。"""
    _patch_eval(monkeypatch)
    monkeypatch.setattr(rs, "_stock_pool", lambda: _codes(250))   # 3 片
    import marketdata.vendors.tq as tq

    def _rpc(name, params, timeout=None):
        if name == "get_market_data":
            return _bars_for(params["stock_list"])
        if name == "formula_process_mul_zb":
            return {c: {"主力资金": 100.0} for c in params["stock_list"]}
        return {}

    monkeypatch.setattr(tq, "tq_rpc", _rpc)
    out = rs.scan()
    assert out["ok"] is True
    assert out["complete"] is True
    assert out["note"] is None
    assert out["chunks_failed"] == 0 and out["chunks_total"] == 3
    assert out["fund_chunks_failed"] == 0 and out["fund_chunks_total"] == 1


def test_scan_fund_outage_is_explicit_failure(monkeypatch):
    """日线好但资金全断(零数据): 三指标集体缺资金维 → 同样显式失败, 不静默降级。"""
    _patch_eval(monkeypatch)
    monkeypatch.setattr(rs, "_stock_pool", lambda: _codes(50))
    import marketdata.vendors.tq as tq

    def _rpc(name, params, timeout=None):
        if name == "get_market_data":
            return _bars_for(params["stock_list"])
        raise RuntimeError("supamo down")

    monkeypatch.setattr(tq, "tq_rpc", _rpc)
    out = rs.scan()
    assert out["ok"] is False
    assert "资金" in out["reason"] and out["fund_chunks_failed"] == 1
    assert out["complete"] is False and out["note"]


def test_daily_job_passes_through_health(monkeypatch):
    """daily_job 必须把 scan 的 ok/分片健康度原样透传, 不吞成成功。"""
    monkeypatch.setattr(rs, "scan", lambda: {"ok": False, "reason": "TQ 日线全断",
                                             "chunks_failed": 3, "chunks_total": 3,
                                             "complete": False})
    out = rs.daily_job()
    assert out["ok"] is False and out["chunks_failed"] == 3

