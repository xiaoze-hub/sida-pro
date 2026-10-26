"""通达信(TQ) Volume 单位标定回归 —— 审计遗留 P2(与 turnover 单位同族)。

在线标定证据(2026-10-08 收盘后, 本机 Tailscale 直连 N150 TQ 网关 :17709,
样本 002361/600519/000001/300750/002415/601318/000651 共 7 只 A股, 交叉腾讯):

  | 接口                          | 单位 | 与腾讯 qt.gtimg.cn parts[6](手)关系 |
  |-------------------------------|------|--------------------------------------|
  | get_market_snapshot.Volume    | 手   | 比值 1.0000 (7/7)                     |
  | get_market_data(1d).Volume    | 股   | 比值 100.000 (7/7)                    |

恒等式(002361.SZ, 2026-10-08):
  - 快照: Volume(手)1784413 × 100 × Average(VWAP)10.23 = 1,825,454,499 元
          ≈ Amount(万元)182560.53 × 1e4 = 1,825,605,300 元 (偏差 0.008%, VWAP 舍入)
  - 日线: Volume(股)178441360 × 10.23 ≈ 182560.53 × 1e4(同上)

契约分工(冻结 docs/_frozen/data.md):**两套契约, 单位不同, 都不是 bug**:
  - ``Quote.volume = 手`` —— 腾讯/东财/TQ 实时行情同口径; 恒等式
    ``turnover / (price × volume × 100)``(见 tests/test_vendor_missing_fields.py:90)。
  - ``Bar.volume = 股`` —— 腾讯/东财 fqkline 源为手需 ×100(vendors/kline.py);
    **TQ get_market_data 原生已是股, 不再 ×100**。

本文件把上述标定**锁死为回归**:任何"顺手再 ×100 / 漏 ×100"都会让这里变红。
全部 mock, 禁真网络(在线探针一次性结果已固化进常量)。
"""

from __future__ import annotations

import pytest

from marketdata.symbol import Symbol
from marketdata.vendors import tq as tqmod

# --- 2026-10-08 收盘 002361.SZ 实盘快照(TQ get_market_snapshot, 原样) -------------
_SNAP = {
    "Now": "10.10", "LastClose": "9.58", "Open": "9.77", "Max": "10.54", "Min": "9.59",
    "Volume": "1784413", "Amount": "182560.53", "Average": "10.23",
    "Inside": "778965", "Outside": "1005449", "ErrorId": "0",
}
_SNAP_VOLUME_LOTS = 1_784_413          # 手
_SNAP_AVG = 10.23                      # VWAP(元)
_SNAP_AMOUNT_WAN = 182_560.53          # 万元
# 腾讯同日同票: parts[6]=1784414(手) / parts[37]=182561(万元) / parts[35] 元额=1825605387
_TX_VOLUME_LOTS = 1_784_414
_TX_AMOUNT_YUAN = 1_825_605_387

# --- 2026-10-08 002361.SZ get_market_data(1d, front) 末柱 --------------------------
_DAILY_ROWS = {
    "Date": ["20261008"], "Open": ["9.77"], "Close": ["10.10"],
    "High": ["10.54"], "Low": ["9.59"],
    "Volume": ["178441360"],           # 股(原生), = 快照手值 ×100
    "Amount": ["182560.53"],           # 万元
}
_DAILY_VOLUME_SHARES = 178_441_360     # 股


def _patch_rpc(monkeypatch, handler):
    """把 tqmod._rpc 换成纯函数 handler(method, params) → 响应。"""
    def fake_rpc(method, params, timeout=None, **kw):  # noqa: ARG001
        return handler(method, params)

    monkeypatch.setattr(tqmod, "_rpc", fake_rpc)


# ---------------------------------------------------------------------------
# 快照: Volume = 手(Quote.volume 契约), 不做 ×100
# ---------------------------------------------------------------------------


def test_snapshot_volume_is_lots_not_shares(monkeypatch):
    """TQ 快照 Volume 必须原样落 手; 若被 ×100 成股(1.78e8)此断言立即红。"""
    _patch_rpc(monkeypatch, lambda m, p: dict(_SNAP) if m == "get_market_snapshot" else {})

    quotes = tqmod.TqQuoteVendor().fetch([Symbol.parse("002361", "CN")], {})
    assert len(quotes) == 1
    q = quotes[0]
    assert q.volume == pytest.approx(_SNAP_VOLUME_LOTS)      # 手, 非 ×100
    assert q.volume != pytest.approx(_SNAP_VOLUME_LOTS * 100, rel=1e-3)


def test_snapshot_volume_matches_tencent_lots(monkeypatch):
    """跨源交叉: TQ 快照 Volume 与腾讯 parts[6](手)同日一致(±2 手舍入)。"""
    _patch_rpc(monkeypatch, lambda m, p: dict(_SNAP) if m == "get_market_snapshot" else {})
    q = tqmod.TqQuoteVendor().fetch([Symbol.parse("002361", "CN")], {})[0]
    assert q.volume == pytest.approx(_TX_VOLUME_LOTS, abs=2.0)


def test_snapshot_identity_vol_price_eq_amount(monkeypatch):
    """硬约束恒等式: Volume(手)×100×VWAP == Amount(万元)×1e4 (AGENTS vol×price==amt)。"""
    _patch_rpc(monkeypatch, lambda m, p: dict(_SNAP) if m == "get_market_snapshot" else {})
    q = tqmod.TqQuoteVendor().fetch([Symbol.parse("002361", "CN")], {})[0]

    lhs = q.volume * 100 * _SNAP_AVG                 # 手→股 × 元
    rhs = _SNAP_AMOUNT_WAN * 1e4                     # 万元→元
    assert lhs == pytest.approx(rhs, rel=1e-3)
    # Quote.turnover 已是元(万元×1e4), 与腾讯同日元额一致
    assert q.turnover == pytest.approx(_TX_AMOUNT_YUAN, rel=0.01)
    # 恒等式反解 VWAP 必须落回快照 Average
    assert q.turnover / (q.volume * 100) == pytest.approx(_SNAP_AVG, abs=0.02)


# ---------------------------------------------------------------------------
# 日线: Volume = 股(原生), 严禁再 ×100
# ---------------------------------------------------------------------------


def _fetch_bars(monkeypatch):
    def handler(method, params):
        if method == "refresh_kline":
            return {}
        if method == "get_market_data":
            return {"002361.SZ": {k: list(v) for k, v in _DAILY_ROWS.items()}}
        return {}

    _patch_rpc(monkeypatch, handler)
    monkeypatch.setattr(tqmod, "tq_bars_fresh", lambda dates: True)  # 隔离新鲜度门禁
    return tqmod.TqKlineVendor().fetch([Symbol.parse("002361", "CN")], {"days": 1})


def test_daily_bar_volume_is_shares_native(monkeypatch):
    """TQ 日线 Volume 原生即股; 若被误当手再 ×100(1.78e10)此断言立即红。"""
    bars = _fetch_bars(monkeypatch)
    assert len(bars) == 1
    assert bars[0].volume == pytest.approx(_DAILY_VOLUME_SHARES)
    assert bars[0].volume != pytest.approx(_DAILY_VOLUME_SHARES * 100, rel=1e-3)


def test_daily_bar_volume_identity(monkeypatch):
    """日线恒等式: Volume(股)×VWAP ≈ Amount(万元)×1e4。"""
    bar = _fetch_bars(monkeypatch)[0]
    assert bar.volume * _SNAP_AVG == pytest.approx(_SNAP_AMOUNT_WAN * 1e4, rel=1e-3)


# ---------------------------------------------------------------------------
# 消费方单位一致性: 同票同日 Quote(手)×100 ≈ Bar(股)
# ---------------------------------------------------------------------------


def test_quote_and_bar_volume_consistent_same_day(monkeypatch):
    """把两套契约归一后必须相等: Quote.volume(手)×100 == Bar.volume(股)。"""
    def handler(method, params):
        if method == "get_market_snapshot":
            return dict(_SNAP)
        if method == "refresh_kline":
            return {}
        if method == "get_market_data":
            return {"002361.SZ": {k: list(v) for k, v in _DAILY_ROWS.items()}}
        return {}

    _patch_rpc(monkeypatch, handler)
    monkeypatch.setattr(tqmod, "tq_bars_fresh", lambda dates: True)

    q = tqmod.TqQuoteVendor().fetch([Symbol.parse("002361", "CN")], {})[0]
    bar = tqmod.TqKlineVendor().fetch([Symbol.parse("002361", "CN")], {"days": 1})[0]

    assert q.volume * 100 == pytest.approx(bar.volume, rel=1e-5)
