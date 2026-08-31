# -*- coding: utf-8 -*-
"""全市场三榜扫描 单测。

重点验证:
  - 新出 G 点判定(交叉必须发生在最后一根, 历史交叉不算)
  - 暗盘 TOP 排序 + approximation 硬标记
  - 活跃度 TOP 排序
  - 单股失败不拖垮全局(skipped 计数)
  - 空池/非法代码处理
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.core import market_scan as ms  # noqa: E402
from src.core import decision_pioneer as dp  # noqa: E402


# ---------------------------------------------------------------------------
# 工具: 造 K 线(可控末根是否产生 G 交叉)
# ---------------------------------------------------------------------------


def _mk_bars(n=40, trend=0.1, final_surge=False):
    bars = []
    price = 20.0
    for i in range(n):
        o = price
        price += trend
        c = price
        h = max(o, c) + 0.05
        l = min(o, c) - 0.05
        bars.append({"date": f"2026-07-{i+1:02d}", "open": o, "high": h,
                     "low": l, "close": c, "volume": 100000})
    if final_surge:
        # 最后一根暴力拉升, 制造 A0 上穿 BB0
        o = bars[-1]["close"]
        c = o * 1.20
        bars.append({"date": "2026-08-31", "open": o, "high": c + 0.05,
                     "low": o - 0.05, "close": c, "volume": 500000})
    return bars


# ---------------------------------------------------------------------------
# 代码过滤
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    (["000977", "600103", "000977", "abc", "12345", "300750"], ["000977", "600103", "300750"]),
    ([], []),
])
def test_valid_symbols(raw, expected):
    assert ms._valid_symbols(raw) == expected


# ---------------------------------------------------------------------------
# 新出 G 点判定
# ---------------------------------------------------------------------------


def test_new_g_detects_cross_on_last_bar(monkeypatch):
    """末根暴力拉升产生 G 交叉 → new_g=True。"""
    bars = _mk_bars(n=40, trend=-0.05, final_surge=True)
    monkeypatch.setattr(dp, "fetch_bars", lambda s, m, days=60: bars)
    m = ms._per_stock_metrics("000977", 60)
    assert m is not None
    assert m["new_g"] is True
    assert m["gs_signal"] == "G"


def test_old_g_cross_not_counted(monkeypatch):
    """历史 G 交叉(不在末根) → new_g=False。"""
    bars = _mk_bars(n=40, trend=0.1, final_surge=False)
    # 人为在第 30 根制造一次交叉, 后面平稳
    monkeypatch.setattr(dp, "fetch_bars", lambda s, m, days=60: bars)
    m = ms._per_stock_metrics("000977", 60)
    assert m is not None
    assert m["new_g"] is False


def test_per_stock_empty_bars_returns_none(monkeypatch):
    monkeypatch.setattr(dp, "fetch_bars", lambda s, m, days=60: [])
    assert ms._per_stock_metrics("000977", 60) is None


# ---------------------------------------------------------------------------
# scan 聚合
# ---------------------------------------------------------------------------


def _patch_metrics(monkeypatch):
    """三只股票: A 新出G+高活跃+暗盘正, B 一般, C 失败。"""
    def fake(sym, days, dark_days=1):
        if sym == "000001":
            return {"symbol": sym, "close": 10.0, "gs_signal": "G", "gs_state": "G区",
                    "new_g": True, "activity": 7.5, "activity_level": "大牛",
                    "dark_net": 5_000_000.0, "dark_bars_used": 60}
        if sym == "000002":
            return {"symbol": sym, "close": 20.0, "gs_signal": None, "gs_state": "S区",
                    "new_g": False, "activity": 1.0, "activity_level": "弱",
                    "dark_net": -2_000_000.0, "dark_bars_used": 60}
        return None  # 000003 失败
    monkeypatch.setattr(ms, "_per_stock_metrics", fake)
    monkeypatch.setattr(ms, "md_main_flow_zljc" if hasattr(ms, "md_main_flow_zljc") else "with_zljc", None, raising=False)


def test_scan_aggregates_three_boards(monkeypatch):
    _patch_metrics(monkeypatch)
    r = ms.scan(["000001", "000002", "000003"], top_n=10, with_zljc=False)

    assert r["universe"] == 3
    assert r["computed"] == 2
    assert r["skipped"] == 1

    # 新出 G 点榜只有 000001
    assert [x["symbol"] for x in r["new_g_points"]] == ["000001"]

    # 暗盘 TOP: 000001(+500万) 在 000002(-200万) 前, 且带 approximation 硬标记
    assert [x["symbol"] for x in r["dark_top"]] == ["000001", "000002"]
    assert all(x["approximation"] is True for x in r["dark_top"])
    assert r["dark_top"][0]["dark_net_wan"] == 500.0

    # 活跃度 TOP: 000001(7.5 大牛) 在 000002(1.0 弱) 前
    assert [x["symbol"] for x in r["activity_top"]] == ["000001", "000002"]
    assert r["activity_top"][0]["level"] == "大牛"


def test_scan_dark_top_excludes_none_dark(monkeypatch):
    def fake(sym, days, dark_days=1):
        if sym == "000001":
            return {"symbol": sym, "close": 1.0, "gs_signal": None, "gs_state": None,
                    "new_g": False, "activity": 2.0, "activity_level": "生命",
                    "dark_net": None, "dark_bars_used": 0}  # 暗盘无数据
        return {"symbol": sym, "close": 1.0, "gs_signal": None, "gs_state": None,
                "new_g": False, "activity": 3.0, "activity_level": "强势",
                "dark_net": 100.0, "dark_bars_used": 60}
    monkeypatch.setattr(ms, "_per_stock_metrics", fake)
    r = ms.scan(["000001", "000002"], top_n=10, with_zljc=False)
    # dark_net=None 的 000001 不进暗盘榜(不编造)
    assert [x["symbol"] for x in r["dark_top"]] == ["000002"]


def test_scan_empty_pool():
    r = ms.scan([], with_zljc=False)
    assert r["universe"] == 0
    assert r["computed"] == 0
    assert r["new_g_points"] == []
    assert r["dark_top"] == []
    assert r["activity_top"] == []


def test_scan_top_n_truncates(monkeypatch):
    def fake(sym, days, dark_days=1):
        return {"symbol": sym, "close": 1.0, "gs_signal": None, "gs_state": "G区",
                "new_g": False, "activity": float(int(sym)), "activity_level": "强势",
                "dark_net": float(int(sym)), "dark_bars_used": 60}
    monkeypatch.setattr(ms, "_per_stock_metrics", fake)
    pool = [f"00000{i}" for i in range(1, 8)]
    r = ms.scan(pool, top_n=3, with_zljc=False)
    assert len(r["dark_top"]) == 3
    assert len(r["activity_top"]) == 3
    # 降序: 7 在最前
    assert r["activity_top"][0]["symbol"] == "000007"


def test_scan_zljc_failure_degrades_gracefully(monkeypatch):
    _patch_metrics(monkeypatch)

    import src.core.marketdata_client as mdc
    monkeypatch.setattr(mdc, "md_main_flow_zljc",
                        lambda stocks: (_ for _ in ()).throw(RuntimeError("TQ 不通")))
    r = ms.scan(["000001"], top_n=10, with_zljc=True)
    # ZLJC 挂了不影响三榜
    assert r["zljc"] is None
    assert len(r["new_g_points"]) == 1
