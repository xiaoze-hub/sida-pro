# -*- coding: utf-8 -*-
"""GS 单一事实源一致性契约(2026-10-13 用户实报 bug 的回归).

## 背景
用户实报(600111 收盘后): K 线图 GS 买卖点显示 **G**(红▲), 但右侧数智决策面板
「GS 信号(机会/风险)」显示 **S区** —— 同一只股票同一时刻「两张脸」。

根因: 两套 GS 并存 —— ①K 线图层走 `gs_strategy.compute_gs_signals`(逐根交叉,
confirmed/pending; 区间色带只由 **confirmed** 锚点建立, 见前端 `gs-zones.ts`);
②面板走 `decision_pioneer` 自带的 a0/bb0 比较(`compute_gs_signal.state`),
吃末根盘中价 ⇒ 末根出现**疑似**反向交叉时面板立刻翻面, 图表仍停在上一根已确认信号。

## 本测试钉住的契约
1. **图表标记态 == 面板区态**: 同一 `gs_strategy` 输出 → `gs_state.resolve_gs_state`
   的 zone 必等于「前端 gsZonePoints 口径」推出的区间(三态各钉一例: confirmed G /
   confirmed S / pending)。
2. **pending 语义**: 末根疑似交叉只标「待确认」, 不接管区间, 不冒充定论;
   文案与 K 线图图例逐字一致。
3. **旧 decision_pioneer zone 逻辑退役**: 面板不再等于 `compute_gs_signal.state`
   (那个会抢跑的原始方向)。
4. 缺数据显式「无数据」, 不编造。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.core import decision_pioneer as dp  # noqa: E402
from src.core import gs_state, gs_strategy  # noqa: E402
from src.core.decision_pioneer import compute_gs_signal  # noqa: E402
from src.core.gs_state import resolve_gs_state  # noqa: E402


# ── 构造日K(fixture 与用户实报同形: 末根疑似反向交叉) ────────────────────────
def _mk(closes: list[float]) -> list[dict]:
    out = []
    for i, c in enumerate(closes):
        mo = 9 if i < 28 else 10
        d = i + 1 if i < 28 else i - 27
        out.append({"date": f"2026-{mo:02d}-{d:02d}", "open": c,
                    "high": c * 1.02, "low": c * 0.98, "close": c, "volume": 1000})
    return out


def _decline_then_rise() -> list[float]:
    decl = [24.0 - 0.13 * i for i in range(30)]
    return decl + [decl[-1] + 0.45 * (i + 1) for i in range(12)]


def bars_confirmed_g() -> list[dict]:
    """慢跌后持续上涨 → G 上穿**已确认**(交叉不在末根)。"""
    return _mk(_decline_then_rise())


def bars_confirmed_s() -> list[dict]:
    """慢跌后上涨再回落 → S 上穿**已确认**。"""
    base = [24.0 - 0.13 * i for i in range(18)]
    up = [base[-1] + 0.4 * (i + 1) for i in range(8)]
    down = [up[-1] - 0.25 * (i + 1) for i in range(12)]
    return _mk(base + up + down)


def bars_pending_s() -> list[dict]:
    """**用户实报同形**: 已确认 G 之后, 末根大阴线 → 疑似转 S(未定型)。"""
    base = _decline_then_rise()
    return _mk(base[:-1] + [base[-1] - 3.0])


def bars_pending_g() -> list[dict]:
    """尚无已确认信号, 末根才出现疑似 G 上穿(未定型)。"""
    decl = [24.0 - 0.12 * i for i in range(34)]
    return _mk(decl + [decl[-1] + 0.03 * (i + 1) for i in range(11)])


# ── 「图表口径」参照实现: 逐字复刻前端 gs-zones.ts::gsZonePoints ──────────────
def chart_zone(seq: list[dict], bar_dates: list[str]):
    """由 compute_gs_signals 输出 + K线日期, 推出**最后**一个区间(图表色带口径)。"""
    anchors = sorted(
        (s["date"], s["side"])
        for s in (seq or [])
        if s.get("confirmed") and s.get("side") in ("G", "S") and s.get("date")
    )
    cur = None
    ai = 0
    seen = []
    for d in bar_dates:
        while ai < len(anchors) and anchors[ai][0] <= d:
            cur = anchors[ai][1]
            ai += 1
        if cur:
            seen.append(cur)
    return (f"{seen[-1]}区" if seen else None)


def _dates(bars):
    return [str(b["date"])[:10] for b in bars]


# ── ① 图表标记态 == 面板区态(三态各钉一例) ───────────────────────────────────
@pytest.mark.parametrize("mk_bars, expect_zone, expect_pending, expect_side, expect_confirmed", [
    (bars_confirmed_g, "G区", False, "G", True),
    (bars_confirmed_s, "S区", False, "S", True),
    (bars_pending_s, "G区", True, "G", True),     # ← 用户实报: 面板必须仍显 G区
])
def test_panel_zone_equals_chart_zone(mk_bars, expect_zone, expect_pending,
                                      expect_side, expect_confirmed):
    bars = mk_bars()
    seq = gs_strategy.compute_gs_signals(bars)
    st = resolve_gs_state(bars)

    # 图表侧(前端色带口径)
    assert chart_zone(seq, _dates(bars)) == expect_zone
    # 面板侧(单一事实源)
    assert st["zone"] == expect_zone
    assert st["pending"] is expect_pending
    assert st["signal"] == expect_side
    assert st["signal_confirmed"] is expect_confirmed
    assert st["available"] is True


def test_panel_matches_chart_for_every_bar_prefix():
    """逐根截断都对得上(防「只有末根一致」的假修)。"""
    for mk_bars in (bars_confirmed_g, bars_confirmed_s, bars_pending_s, bars_pending_g):
        bars = mk_bars()
        for k in range(28, len(bars) + 1):
            sub = bars[:k]
            st = resolve_gs_state(sub)
            cz = chart_zone(gs_strategy.compute_gs_signals(sub), _dates(sub))
            assert st["zone"] == cz, f"{mk_bars.__name__} 前 {k} 根: 面板={st['zone']} 图表={cz}"


# ── ② pending 语义(不冒充定论 + 文案逐字一致) ────────────────────────────────
def test_pending_does_not_take_over_zone():
    """末根疑似反向交叉 → 区间仍停在已确认信号, pending 只标注。"""
    bars = bars_pending_s()
    st = resolve_gs_state(bars)
    assert st["zone"] == "G区"          # 不是 S区
    assert st["pending"] is True
    assert st["pending_side"] == "S"
    assert st["signal"] == "G"          # 最近已确认信号仍是 G


def test_pending_label_matches_kline_legend_verbatim():
    """面板待确认文案与 K 线图图例逐字一致(防两处各说各话)。"""
    st = resolve_gs_state(bars_pending_s())
    assert st["pending_label"] == "○S 待确认(盘中价, 收盘才定死)"

    legend = (ROOT / "frontend/packages/biz-ui/src/components/KlineChart.tsx").read_text(encoding="utf-8")
    assert "空心 ○ = 待确认(盘中价, 收盘才定死)" in legend
    # 图例口径 = 面板口径(去掉图例前缀后逐字相同)
    assert "待确认(盘中价, 收盘才定死)" in st["pending_label"]


def test_pending_without_anchor_is_not_a_zone():
    """无已确认锚点时 zone 必须为 None(不冒充区间), 但显式标待确认。"""
    bars = bars_pending_g()
    st = resolve_gs_state(bars)
    assert st["zone"] is None
    assert st["available"] is False
    assert st["pending"] is True and st["pending_side"] == "G"
    assert st["label"] == "G区"          # 疑似方向(供展示)
    assert st["pending_label"] == "○G 待确认(盘中价, 收盘才定死)"


def test_no_data_is_explicit():
    for bars in ([], bars_pending_g()[:10]):
        st = resolve_gs_state(bars)
        assert st["zone"] is None and st["available"] is False and st["signal"] is None
        assert st["label"] == "无数据"
        assert st["pending"] is False
        assert st["note"]


# ── ③ 旧 decision_pioneer zone 逻辑退役(不回归) ──────────────────────────────
def test_retired_zone_logic_no_longer_drives_panel():
    """末根疑似交叉时, 面板**不得**再用 compute_gs_signal.state(会抢跑的原始方向)。"""
    bars = bars_pending_s()
    old = compute_gs_signal(bars)
    assert old is not None
    assert old["state"] == "S区"          # 旧逻辑: 立刻翻面(与图表打架)
    st = resolve_gs_state(bars)
    assert st["zone"] == "G区"            # 新口径: 停在已确认信号(与图表一致)
    assert st["zone"] != old["state"]


def test_panel_response_gs_is_single_source(monkeypatch):
    """`fetch_decision_pioneer().gs` 必须由 gs_state 产出, 并带齐 confirmed/pending 字段。"""
    bars = bars_pending_s()
    monkeypatch.setattr(dp, "fetch_bars", lambda symbol, market="CN", days=60: bars)
    monkeypatch.setattr(dp, "fetch_tq_l2", lambda symbol, market="CN": None)
    d = dp.fetch_decision_pioneer("600111", "CN")
    g = d["gs"]
    st = resolve_gs_state(bars)
    assert g["state"] == st["zone"] == "G区"
    assert g["signal"] == "G" and g["signal_confirmed"] is True
    assert g["pending"] is True and g["pending_side"] == "S"
    assert g["pending_label"] == "○S 待确认(盘中价, 收盘才定死)"
    assert g["signal_date"] == st["signal_date"]
    # 快/慢线是展示项, 与区间同源(取自已确认口径的同一份 bars)
    assert g["a0"] == st["a0"] and g["bb0"] == st["bb0"]


def test_panel_text_marks_pending(monkeypatch):
    """推送/助手文案不得把疑似当定论。"""
    bars = bars_pending_s()
    monkeypatch.setattr(dp, "fetch_bars", lambda symbol, market="CN", days=60: bars)
    monkeypatch.setattr(dp, "fetch_tq_l2", lambda symbol, market="CN": None)
    monkeypatch.setattr(dp, "compute_institution_activity", lambda b: None)
    txt = dp.decision_pioneer_text("600111", "CN")
    assert "G区(已确认)" in txt
    assert "待确认" in txt


def test_market_scan_uses_single_source(monkeypatch):
    """全市场扫描的 gs_state/gs_signal 也走单一事实源; 账本只收已确认交叉。"""
    from src.core import market_scan

    bars = bars_pending_s()
    monkeypatch.setattr(market_scan, "_log_gs_signals", lambda *a, **k: 0)
    monkeypatch.setattr(dp, "fetch_bars", lambda symbol, market="CN", days=60: bars)
    monkeypatch.setattr(dp, "compute_institution_activity", lambda b: None)
    m = market_scan._per_stock_metrics("600111", 60, 1)
    assert m is not None
    st = resolve_gs_state(bars)
    assert m["gs_state"] == st["zone"] == "G区"           # 不是 compute_gs_signal 的 S区
    assert m["gs_signal"] == "G" and m["gs_signal_confirmed"] is True
    assert m["gs_pending"] is True and m["new_g"] is False  # 疑似 S, 不是新 G
    # 账本口径: 已确认交叉带**信号时点**价与信号日(不是末根)
    assert m["gs_cross_side"] == "G"
    assert m["gs_cross_price"] == st["signal_price"]
    assert m["gs_cross_date"] == st["signal_date"]


def test_gs_windows_raw_state_is_documented_as_not_zone():
    """`compute_gs_windows.state` 是原始 0 轴方向(低层), 文档必须标明非面板区间。"""
    doc = dp.compute_gs_windows.__doc__ or ""
    assert "原始 0 轴方向" in doc and "resolve_gs_state" in doc


def test_signal_and_pending_prices_are_their_own_bar_closes():
    """信号/疑似交叉各自带**当日**收盘价(不拿末根/后来的价冒充)。"""
    bars = bars_pending_s()
    seq = gs_strategy.compute_gs_signals(bars)
    st = resolve_gs_state(bars)
    close_by_date = {str(b["date"])[:10]: b["close"] for b in bars}
    conf = [s for s in seq if s["confirmed"]][0]
    pend = [s for s in seq if not s["confirmed"]][0]
    assert st["signal_price"] == conf["price"] == close_by_date[conf["date"]]
    assert st["pending_price"] == pend["price"] == bars[-1]["close"]
    assert st["pending_price"] != st["signal_price"]


def test_ledger_rows_carry_signal_day_price_and_status(monkeypatch):
    """账本行: 日期/价取**信号当日**, 且显式带确认状态(疑似不冒充已确认)。"""
    from src.core import market_scan
    import src.core.decision_log as dl

    captured: dict = {}

    def _capture(rows, source=None):
        captured["rows"] = rows
        return len(rows)

    monkeypatch.setattr(dl, "record_many_safe", _capture)

    metrics = [{
        "symbol": "209777", "gs_state": None,
        "gs_cross_side": "G", "gs_cross_status": "待确认",
        "gs_cross_date": "2026-09-29", "gs_cross_price": 15.0,
    }]
    assert market_scan._log_gs_signals(metrics, trade_date="20261013") == 1
    row = captured["rows"][0]
    assert row["trade_date"] == "20260929"              # 信号当日(非末根), 紧凑格式
    assert row["price"] == 15.0                         # 信号当日收盘价
    assert row["context"]["status"] == "待确认"          # 不冒充已确认
    assert row["context"]["side"] == "G"


def test_ledger_skips_rows_without_signal_day_price(monkeypatch):
    """拿不到信号价 → 不入账(不硬填 0)。"""
    from src.core import market_scan
    import src.core.decision_log as dl

    called = {"n": 0}
    monkeypatch.setattr(dl, "record_many_safe",
                        lambda rows, source=None: called.__setitem__("n", called["n"] + 1) or len(rows))
    metrics = [
        {"symbol": "209777", "gs_cross_side": "G", "gs_cross_date": "2026-09-29", "gs_cross_price": None},
        {"symbol": "209777", "gs_cross_side": "S", "gs_cross_date": "2026-09-29", "gs_cross_price": 0},
        {"symbol": "209777", "gs_cross_side": "G", "gs_cross_date": None, "gs_cross_price": 12.0},
    ]
    assert market_scan._log_gs_signals(metrics, trade_date=None) == 0
    assert called["n"] == 0
