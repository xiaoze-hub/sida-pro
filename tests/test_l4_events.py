# -*- coding: utf-8 -*-
"""第4块 L4 事件数据源 单测: src/core/l4_events.py

覆盖:
  - 拆单簇: 同向小单聚簇 / 笔数不足 / 单笔超阈值(明摆着的大单) / 方向或价格断开
  - 撤单异常: 单笔大撤单 / 集中撤单 / 无撤单 → 空
  - 龙虎榜公告: wencai 可用 → 事件 / 不可用 → 空(不编造) / 零命中 → 空
  - 我的买卖点: 交割单读写 / 无记录 → 空 / DB 异常 → 空
  - 解套盘位: 筹码近似 / bars 不足 → 空
  - time 转换健壮性(非法值 → '--')
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.core import l4_events as le  # noqa: E402


# ---------------------------------------------------------------------------
# ① 拆单簇
# ---------------------------------------------------------------------------


def _trade(t_ms: int, price: float, vol: int, dir_: str = "B"):
    return {"t": t_ms, "price": price, "vol": vol, "dir": dir_, "amt": price * vol}


def test_split_cluster_detected():
    """6 笔同向小单(每笔 8.85 万 < 30 万), 总额 53 万 >= 30 万 → 拆单簇。"""
    trades = [_trade(143000000 + i * 1000, 10.00, 8850) for i in range(6)]
    ev = le.split_clusters(trades, "2026-09-01")
    assert len(ev) == 1
    assert ev[0]["kind"] == "split_cluster"
    assert ev[0]["date"] == "2026-09-01"
    assert ev[0]["count"] == 6
    assert ev[0]["amount"] == pytest.approx(10.00 * 8850 * 6, abs=1)
    assert "买" in ev[0]["label"]


def test_split_cluster_too_few_trades():
    """少于 5 笔 → 不算簇。"""
    trades = [_trade(143000000 + i * 1000, 10.00, 8850) for i in range(4)]
    assert le.split_clusters(trades, "2026-09-01") == []


def test_split_cluster_single_over_threshold_not_split():
    """簇里任一笔 >= 30 万 → 是明摆着的大单, 不算拆单。"""
    trades = [_trade(143000000 + i * 1000, 10.00, 40000) for i in range(6)]  # 单笔 40 万
    assert le.split_clusters(trades, "2026-09-01") == []


def test_split_cluster_total_below_threshold():
    """簇总额 < 30 万 → 不够格。"""
    trades = [_trade(143000000 + i * 1000, 10.00, 500) for i in range(6)]  # 总额 3 万
    assert le.split_clusters(trades, "2026-09-01") == []


def test_split_cluster_breaks_on_direction_change():
    """方向变了 → 断簇(买 4 笔 + 卖 4 笔, 各自都不足 5 笔)。"""
    trades = [_trade(143000000 + i * 1000, 10.00, 8850, "B") for i in range(4)]
    trades += [_trade(143004000 + i * 1000, 10.00, 8850, "S") for i in range(4)]
    assert le.split_clusters(trades, "2026-09-01") == []


def test_split_cluster_breaks_on_price_gap():
    """价格跳超过 1 个价位 → 断簇。"""
    trades = [_trade(143000000 + i * 1000, 10.00, 8850) for i in range(3)]
    trades += [_trade(143003000 + i * 1000, 10.50, 8850) for i in range(3)]  # 跳 0.5 元
    assert le.split_clusters(trades, "2026-09-01") == []


def test_split_cluster_breaks_on_time_gap():
    """相邻两笔间隔超过 30 秒 → 断簇。"""
    trades = [_trade(143000000 + i * 1000, 10.00, 8850) for i in range(3)]
    trades += [_trade(143100000 + i * 1000, 10.00, 8850) for i in range(3)]  # 跳 100 秒
    assert le.split_clusters(trades, "2026-09-01") == []


def test_split_cluster_empty_input():
    assert le.split_clusters([], "2026-09-01") == []


def test_split_cluster_sell_side_label():
    trades = [_trade(143000000 + i * 1000, 10.00, 8850, "S") for i in range(6)]
    ev = le.split_clusters(trades, "2026-09-01")
    assert len(ev) == 1 and "卖" in ev[0]["label"]


# ---------------------------------------------------------------------------
# ② 撤单异常
# ---------------------------------------------------------------------------


def test_cancel_big_single():
    cancels = [{"t": 143000000, "vol": 60000, "target": 1}]
    ev = le.cancel_anomalies(cancels, "2026-09-01")
    assert len(ev) == 1
    assert ev[0]["kind"] == "cancel_anomaly"
    assert ev[0]["shares"] == 60000
    assert ev[0]["time"] == "14:30:00"


def test_cancel_small_not_anomaly():
    assert le.cancel_anomalies([{"t": 143000000, "vol": 100, "target": 1}], "2026-09-01") == []


def test_cancel_burst_detected():
    """同一分钟内 >= 20 笔撤单 → 集中撤单。"""
    cancels = [{"t": 143000000 + i * 1000, "vol": 100, "target": i} for i in range(25)]
    ev = le.cancel_anomalies(cancels, "2026-09-01")
    burst = [e for e in ev if "集中撤单" in e["label"]]
    assert len(burst) == 1
    assert burst[0]["count"] == 25


def test_cancel_empty():
    assert le.cancel_anomalies([], "2026-09-01") == []


def test_cancel_time_robust_to_garbage():
    """时间字段非法 → '--', 不抛异常。"""
    ev = le.cancel_anomalies([{"t": None, "vol": 60000, "target": 1}], "2026-09-01")
    assert ev[0]["time"] == "--"


def test_ms_to_hms_edge_cases():
    assert le._ms_to_hms(93015000) == "09:30:15"  # 含毫秒
    assert le._ms_to_hms(0) == "--"
    assert le._ms_to_hms("bad") == "--"
    assert le._ms_to_hms(None) == "--"


# ---------------------------------------------------------------------------
# ③ 龙虎榜 / 公告 (wencai)
# ---------------------------------------------------------------------------


def test_dragon_tiger_when_wencai_available(monkeypatch):
    import src.web.api.wencai as wapi

    monkeypatch.setattr(wapi, "run_wencai", lambda q: {"available": True, "rows": [{"a": 1}, {"a": 2}]})
    ev = le.dragon_tiger_events("000977", "2026-09-01")
    assert len(ev) == 1
    assert ev[0]["kind"] == "dragon_tiger"
    assert ev[0]["count"] == 2
    assert "龙虎榜" in ev[0]["label"]


def test_announcement_when_wencai_available(monkeypatch):
    import src.web.api.wencai as wapi

    monkeypatch.setattr(wapi, "run_wencai", lambda q: {"available": True, "rows": [{"a": 1}]})
    ev = le.announcement_events("000977", "2026-09-01")
    assert ev[0]["kind"] == "announcement"


def test_wencai_unavailable_returns_empty(monkeypatch):
    """wencai 不可用 → 空列表, 不编造事件。"""
    import src.web.api.wencai as wapi

    monkeypatch.setattr(wapi, "run_wencai", lambda q: {"available": False, "rows": [], "note": "thsdk 不可用"})
    assert le.dragon_tiger_events("000977", "2026-09-01") == []
    assert le.announcement_events("000977", "2026-09-01") == []


def test_wencai_zero_hits_returns_empty(monkeypatch):
    """查询成功但零命中 → 空(不是"有龙虎榜")。"""
    import src.web.api.wencai as wapi

    monkeypatch.setattr(wapi, "run_wencai", lambda q: {"available": True, "rows": []})
    assert le.dragon_tiger_events("000977", "2026-09-01") == []


def test_wencai_raises_returns_empty(monkeypatch):
    import src.web.api.wencai as wapi

    monkeypatch.setattr(wapi, "run_wencai", lambda q: (_ for _ in ()).throw(RuntimeError("超时")))
    assert le.dragon_tiger_events("000977", "2026-09-01") == []


def test_dragon_tiger_query_contains_symbol(monkeypatch):
    """查询串里必须带代码, 否则查的不是这只票。"""
    import src.web.api.wencai as wapi

    seen = {}

    def fake(q):
        seen["q"] = q
        return {"available": True, "rows": [{"a": 1}]}

    monkeypatch.setattr(wapi, "run_wencai", fake)
    le.dragon_tiger_events("000977", "2026-09-01")
    assert "000977" in seen["q"] and "龙虎榜" in seen["q"]


# ---------------------------------------------------------------------------
# ④ 我的买卖点(交割单) — 2026-09-01 暂缓(交割单接口未透传 user_id, 多用户串号)
# ---------------------------------------------------------------------------
# 已撤掉 my_trade_events 函数, 等接口透传 user_id 后再补。
# 占位测例: 验证模块不再导出 my_trade_events (避免误用).
def test_my_trade_events_not_exported():
    assert not hasattr(le, "my_trade_events"), (
        "2026-09-01: my_trade_events 已暂缓, 模块不应再导出 (user_id 未透传前会串号)"
    )


# ---------------------------------------------------------------------------
# ⑤ 解套盘位 + 筹码结构 — 用标准接口 chip_distribution, 不自算
# ---------------------------------------------------------------------------


def _fake_chips(price_min=9.5, price_max=11.5, cost_50=10.5):
    """构造一个芯片结构 (类似 compute_near_term_chips 返回)."""
    return {
        "source": "tencent_price_dist",
        "price_min": price_min,
        "price_max": price_max,
        "cost_10": price_min + (price_max - price_min) * 0.1,
        "cost_50": cost_50,
        "cost_90": price_min + (price_max - price_min) * 0.9,
        "peak_price": 10.5,
        "peak_pct": 6.36,
        "profit_ratio": 51.16,
        "distribution": [
            {"price": round(price_min + i * 0.05, 2), "pct": 1.0}
            for i in range(int((price_max - price_min) / 0.05) + 1)
        ],
    }


def test_unlock_levels_from_chips_basic():
    """有 chips → 派生 2 条压力线(峰压 + 套牢90%)."""
    chips = _fake_chips()
    lv = le.unlock_levels_from_chips(chips)
    assert isinstance(lv, list)
    assert len(lv) >= 2
    assert all(x["kind"] == "pressure" for x in lv)
    # 至少包含筹码峰 + 套牢90分位两条
    labels = {x["label"] for x in lv}
    assert any("筹码峰" in l for l in labels)
    assert any("套牢" in l for l in labels)


def test_unlock_levels_from_chips_none():
    """chips=None → 空 (显式无数据, 不编造)."""
    assert le.unlock_levels_from_chips(None) == []


def test_unlock_levels_from_chips_empty():
    """chips 缺 distribution 字段 → 空, 不抛."""
    assert le.unlock_levels_from_chips({}) == []


# ---------------------------------------------------------------------------
# ⑥ find_tck_file 复用 dark_split 的实现
# ---------------------------------------------------------------------------


def test_find_tck_file_reexported(monkeypatch, tmp_path):
    """l4_events 复用 dark_split 的定位逻辑(同一套 PANWATCH_TCK_DIR 约定)."""
    (tmp_path / "sz000977_20260901.tck").write_bytes(b"x")
    monkeypatch.setenv("PANWATCH_TCK_DIR", str(tmp_path))
    assert le.find_tck_file("000977") is not None
    monkeypatch.delenv("PANWATCH_TCK_DIR", raising=False)
    assert le.find_tck_file("000977") is None
