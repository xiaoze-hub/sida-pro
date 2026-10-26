# -*- coding: utf-8 -*-
"""情绪序列**本地自算**(2026-10-14, 替代已弃用的云 SC)的钉子。

钉什么:
  ① 涨停/炸板家数口径 —— 复用 zdt 真涨停候选(既有 `parse_zdt_snapshot`) + **封住**
     走 `limit_up_events` 同一口径(收盘价 ≥ 涨停价(昨收), `limit_rules.limit_up_price`);
  ② 封板成功/失败资金 = 封单量(手)×100×收盘价 按封住/未封分侧求和, 落库口径 = 亿元
     (SC15 同口径), 且**显式标近似**;
  ③ 连板家数 = `limit_up_events` 聚合(走 `market_phase.consecutive_boards` 单一事实源);
     交易日序必须传日历, 否则中间没涨停的日子被跳过 ⇒ 连板数被高估;
  ④ 派生(炸板比/封板成功率)一律委托 `sentiment_thermometer.derive_thermometer`;
  ⑤ 分片铁律: 单次 TQ 请求 ≤50 只; 连续 2 片失败即**中止**(绝不压测式重试);
  ⑥ 缺数据显式: 价格缺失计数、快照连续失败/无真涨停 → ok=False **不落库**;
  ⑦ 落库幂等: `market_sentiment_daily` 兼容列只填**空位**(COALESCE, 不覆盖已有真值),
     明细表按 (trade_date, market) 原地 upsert。

全部离线: 数据源全部注入 / monkeypatch, 禁真实网络。DB 走测试 SQLite。
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest
from sqlalchemy import text

from src.core import sentiment_selfcalc as sc
from src.core import sentiment_thermometer as st


# ── ① 封住判定: 与 limit_up_events 生成侧同一口径 ───────────────────────────

def test_is_sealed_close_matches_limit_up_events_caliber():
    # 主板 10%: 昨收 12.00 → 涨停价 13.20
    assert sc.is_sealed_close(13.20, 12.00, "001234", is_st=False) is True
    assert sc.is_sealed_close(13.19, 12.00, "001234", is_st=False) is False
    # 创业板 20%: 昨收 200 → 涨停价 240
    assert sc.is_sealed_close(240.0, 200.0, "300750", is_st=False) is True
    assert sc.is_sealed_close(230.0, 200.0, "300750", is_st=False) is False
    # ST(名称判定) → 5% 线: 昨收 10 → 涨停价 10.50
    assert sc.is_sealed_close(10.50, 10.0, "001234", is_st=True) is True
    assert sc.is_sealed_close(10.40, 10.0, "001234", is_st=True) is False
    # 同一价: 非 ST(10% 线 ⇒ 11.00) 就不算封住 —— ST 判定确实改变结论
    assert sc.is_sealed_close(10.50, 10.0, "001234", is_st=False) is False
    # 缺数据 → None(显式, 不猜)
    assert sc.is_sealed_close(None, 12.0, "001234", is_st=False) is None
    assert sc.is_sealed_close(13.2, None, "001234", is_st=False) is None
    assert sc.is_sealed_close(13.2, 0.0, "001234", is_st=False) is None


def test_is_sealed_close_reuses_limit_rules_not_a_second_rule():
    """口径钉子: 必须与 limit_rules.limit_up_price 一致(禁自创第二套)。"""
    from src.core.limit_rules import limit_up_price

    lp = limit_up_price("600519", 1500.0, False)
    assert sc.is_sealed_close(lp, 1500.0, "600519", is_st=False) is True
    assert sc.is_sealed_close(lp - 0.01, 1500.0, "600519", is_st=False) is False


# ── ② 涨停/炸板家数 + 封板资金 ─────────────────────────────────────────────

_SNAP = {
    "001234.SZ": {"FDVolMaxZT": "1200", "VolZT": "900", "FirstTimeZT": "09:31:05",
                  "OpenTimesZT": "0", "ZDTStatusNow": "1"},   # 封住, 全天未开板
    "600519.SH": {"FDVolMaxZT": "88", "VolZT": "50", "FirstTimeZT": 145503,
                  "OpenTimesZT": "2", "ZDTStatusNow": "1"},    # 开过 2 次但收盘封住(回封)
    "300750.SZ": {"FDVolMaxZT": "500", "VolZT": "400", "FirstTimeZT": "10:02:00",
                  "OpenTimesZT": "3", "ZDTStatusNow": "9"},    # 炸板, 收盘未封住
}
_PRICES = {
    "001234": {"close": 13.20, "prev_close": 12.00},
    "600519": {"close": 1650.0, "prev_close": 1500.0},
    "300750": {"close": 230.0, "prev_close": 200.0},
}
_NAMES = {"001234": "甲股", "600519": "乙股", "300750": "丙股"}


def test_zt_metrics_counts_and_money():
    m = sc.zt_metrics(_SNAP, _PRICES, _NAMES)
    # ① 涨停家数 = zdt 真涨停候选(含炸板) —— 与 SC3 v1「涨停/曾涨停」口径一致
    assert m["limit_up_count"] == 3
    # 炸板 = OpenTimesZT>0 或 曾涨停未封住 ⇒ 600519(回封)+300750
    assert m["limit_up_open_count"] == 2
    assert m["sealed_count"] == 2          # 001234 + 600519 收盘封住
    assert m["open_times_gt0"] == 2
    assert m["price_missing"] == 0
    # ② 封单额(元) = 手×100×收盘价; 落库口径 亿元
    assert m["seal_success_money_yuan"] == pytest.approx(1200 * 100 * 13.20 + 88 * 100 * 1650.0)
    assert m["seal_fail_money_yuan"] == pytest.approx(500 * 100 * 230.0)
    assert m["seal_success_money"] == pytest.approx(round((1200 * 100 * 13.20 + 88 * 100 * 1650.0) / 1e8, 4))
    assert m["seal_fail_money"] == pytest.approx(round(500 * 100 * 230.0 / 1e8, 4))
    # 近似 + 口径 + 单位显式
    assert m["approx"] is True
    assert m["caliber"] == "close_ge_limit_price"
    assert m["money_unit"] == "亿元"


def test_zt_metrics_blow_is_subset_of_limit_up():
    """炸板家数必须 ≤ 涨停家数(炸板是候选的子集), 否则炸板比语义就崩了。"""
    m = sc.zt_metrics(_SNAP, _PRICES, _NAMES)
    assert m["limit_up_open_count"] <= m["limit_up_count"]


def test_zt_metrics_price_missing_is_explicit_not_counted():
    prices = dict(_PRICES)
    prices.pop("300750")  # 取不到价量
    m = sc.zt_metrics(_SNAP, prices, _NAMES)
    assert m["price_missing"] == 1
    assert m["limit_up_count"] == 3          # 候选仍在(缺价不改候选)
    assert m["sealed_count"] == 2
    # 300750 无价 ⇒ 既不计成功也不计失败(不拿 0 冒充)
    assert m["seal_fail_money_yuan"] == pytest.approx(0.0)
    # 但"打开过"仍可判 ⇒ 炸板计数不受价格缺失影响
    assert m["limit_up_open_count"] == 2


def test_zt_metrics_empty_snapshot_is_zero_candidates():
    m = sc.zt_metrics({}, {}, {})
    assert m["limit_up_count"] == 0 and m["limit_up_open_count"] == 0


def test_zt_metrics_all_prices_missing_keeps_money_none():
    """候选全都取不到价量 ⇒ 封板资金是**未知**, 必须 None —— 落 0 会污染封板成功率基线。"""
    m = sc.zt_metrics(_SNAP, {}, _NAMES)
    assert m["price_missing"] == 3
    assert m["seal_success_money"] is None and m["seal_fail_money"] is None
    assert m["prices_available"] is False
    assert m["limit_up_count"] == 3, "候选家数不依赖价格(仍可判)"


def test_collect_all_prices_missing_marks_money_none():
    p = _providers(prices={})
    out = sc.collect_and_compute("20261014", **p)
    assert out["ok"] is True
    assert out["seal_success_money"] is None and out["seal_fail_money"] is None
    assert out["seal_success_rate"] is None, "资金未知 ⇒ 封板成功率必须 None, 不是 0"
    assert out["blow_ratio"] is not None, "炸板比只依赖家数, 仍应可算"
    assert any("价量" in e for e in out["errors"])


def test_zt_metrics_zdt_status_mismatch_flagged_not_decided():
    """ZDTStatusNow 枚举未标定 ⇒ 只做交叉核对(冲突计数), 绝不据此改封住结论。"""
    snap = {"001234.SZ": {"FDVolMaxZT": "1", "OpenTimesZT": "0", "ZDTStatusNow": "1"}}
    prices = {"001234": {"close": 10.50, "prev_close": 10.00}}  # 只涨 5% ⇒ 未封住
    m = sc.zt_metrics(snap, prices, {"001234": "甲股"})
    assert m["sealed_count"] == 0            # 结论按 close ≥ 涨停价(10%线 ⇒ 11.00)
    assert m["status_mismatch"] == 1         # 但状态字段说"1"⇒ 记冲突
    assert m["zdt_status_dist"]["1"] == 1


def test_zt_metrics_st_name_uses_five_percent_line():
    snap = {"001234.SZ": {"FDVolMaxZT": "1", "OpenTimesZT": "0", "ZDTStatusNow": "1"}}
    prices = {"001234": {"close": 10.50, "prev_close": 10.00}}
    # 名称含 ST ⇒ 5% 线 ⇒ 10.50 即封住
    m = sc.zt_metrics(snap, prices, {"001234": "*ST甲股"})
    assert m["sealed_count"] == 1


# ── ③ 连板家数(limit_up_events 聚合) ──────────────────────────────────────

_EVENTS = [
    {"trade_date": "20261013", "symbol": "X", "sealed": True, "open_count": 0},
    # 20261014 场景: X 开板后回封(炸板计数内), Y 干净封住, W 触及未封(回落)
    {"trade_date": "20261014", "symbol": "X", "sealed": True, "open_count": 1},
    {"trade_date": "20261014", "symbol": "Y", "sealed": True, "open_count": 0},
    {"trade_date": "20261012", "symbol": "Z", "sealed": True, "open_count": 0},
    {"trade_date": "20261013", "symbol": "Z", "sealed": True, "open_count": 0},
    {"trade_date": "20261014", "symbol": "W", "sealed": False, "open_count": 1},
]
_DAYS = ["20261012", "20261013", "20261014"]


def test_streak_count_counts_ge2_only():
    assert sc.streak_count_from_events(_EVENTS, "20261014", _DAYS) == 1  # 仅 X


def test_streak_count_needs_calendar_else_overcounts():
    """不传交易日序时, 中间没涨停的日子会被跳过 ⇒ 连板数被高估(故生产必须传日历)。"""
    events = [
        {"trade_date": "20261012", "symbol": "X", "sealed": True},
        {"trade_date": "20261014", "symbol": "X", "sealed": True},
    ]
    assert sc.streak_count_from_events(events, "20261014", _DAYS) == 0        # 中间断了 ⇒ 首板
    assert sc.streak_count_from_events(events, "20261014", None) == 1         # 误当成 2 连板


def test_streak_count_absent_day_is_none_not_zero():
    events = [{"trade_date": "20261012", "symbol": "X", "sealed": True}]
    assert sc.streak_count_from_events(events, "20261014", _DAYS) is None


def test_streak_count_reuses_market_phase_consecutive_boards(monkeypatch):
    """口径钉子: 必须走 market_phase.consecutive_boards(禁自创第二套连板算法)。"""
    called = {}
    import src.core.market_phase as mp

    orig = mp.consecutive_boards

    def spy(dates, sealed):
        called["yes"] = True
        return orig(dates, sealed)

    monkeypatch.setattr(mp, "consecutive_boards", spy)
    sc.streak_count_from_events(_EVENTS, "20261014", _DAYS)
    assert called.get("yes") is True


# ── ④ 派生: 与 thermometer 同口径 ─────────────────────────────────────────

def test_derive_ratios_matches_thermometer():
    r = sc.derive_ratios(79, 28, 506.2, 298.63)
    t = st.derive_thermometer({
        "trade_date": "", "limit_up_count": 79, "limit_up_open_count": 28,
        "seal_success_money": 506.2, "seal_fail_money": 298.63,
    })
    assert r["blow_ratio"] == t["blow_ratio"]
    assert r["seal_success_rate"] == t["seal_success_rate"]
    assert abs(r["blow_ratio"] - 28 / 79) < 1e-9


def test_derive_ratios_missing_is_none():
    r = sc.derive_ratios(0, 0, 0.0, 0.0)
    assert r["blow_ratio"] is None and r["seal_success_rate"] is None


# ── ⑤ 分片铁律 + 中止 ────────────────────────────────────────────────────

def test_fetch_snapshot_chunks_at_50(monkeypatch):
    import marketdata.vendors.tq as tqmod

    seen: list[int] = []

    def _fake(codes):
        seen.append(len(codes))
        return {c: {"OpenTimesZT": "0"} for c in codes}

    monkeypatch.setattr(tqmod, "zdt_snapshot", _fake)
    codes = [f"{i:06d}.SZ" for i in range(120)]
    snap, stats = sc.fetch_snapshot(codes)
    assert stats["chunks_total"] == 3 and stats["chunks_failed"] == 0 and stats["aborted"] is False
    assert max(seen) <= 50, "事故铁律: 单次 TQ 请求 ≤50 只"
    assert len(snap) == 120


def test_fetch_snapshot_aborts_after_two_consecutive_failures(monkeypatch):
    import marketdata.vendors.tq as tqmod

    calls = {"n": 0}

    def _boom(codes):
        calls["n"] += 1
        raise RuntimeError("TQ 客户端假死")

    monkeypatch.setattr(tqmod, "zdt_snapshot", _boom)
    codes = [f"{i:06d}.SZ" for i in range(500)]
    snap, stats = sc.fetch_snapshot(codes)
    assert stats["aborted"] is True and stats["chunks_failed"] == 2
    assert calls["n"] == 2, "连续 2 片失败必须立刻中止(绝不压测式重试)"
    assert snap == {}


def test_fetch_prices_chunks_and_maps_fields(monkeypatch):
    import marketdata.vendors.tq as tqmod

    seen: list[int] = []

    def _fake(codes):
        seen.append(len(codes))
        return {c: {"Now": 10.0, "LastClose": 9.0} for c in codes}

    monkeypatch.setattr(tqmod, "pricevol", _fake)
    prices, stats = sc.fetch_prices([f"{i:06d}.SZ" for i in range(60)])
    assert max(seen) <= 50
    assert stats["price_chunks_total"] == 2
    assert prices["000000"]["close"] == 10.0 and prices["000000"]["prev_close"] == 9.0


# ── ⑥ 编排: 全注入, 端到端(不落库) ───────────────────────────────────────

def _providers(snap=None, prices=None, events=None, days=None, codes=None, names=None):
    return {
        "universe_fn": lambda: (codes if codes is not None else ["001234.SZ", "600519.SH", "300750.SZ"],
                                names if names is not None else _NAMES),
        "snapshot_fn": lambda cs: (snap if snap is not None else _SNAP,
                                   {"chunks_total": 1, "chunks_failed": 0, "aborted": False}),
        "price_fn": lambda cs: (prices if prices is not None else _PRICES,
                                {"price_chunks_total": 1, "price_chunks_failed": 0}),
        "events_fn": lambda td: (events if events is not None else _EVENTS),
        "calendar_fn": lambda s, e: (days if days is not None else _DAYS),
    }


def test_collect_and_compute_happy_path():
    out = sc.collect_and_compute("20261014", **_providers())
    assert out["ok"] is True
    assert out["trade_date"] == "20261014"
    assert out["limit_up_count"] == 3 and out["limit_up_open_count"] == 2
    assert out["streak_count"] == 1
    assert out["seal_success_money"] == pytest.approx(0.161)
    assert out["approximation"] is True and out["source"] == "tdx_selfcalc"
    assert out["caliber"] == "close_ge_limit_price" and out["money_unit"] == "亿元"
    assert out["blow_ratio"] == pytest.approx(2 / 3)
    assert out["seal_success_rate"] == pytest.approx(
        out["seal_success_money"] / (out["seal_success_money"] + out["seal_fail_money"])
    )
    assert out["extra"]["approximation"]["seal_money"]


def test_collect_and_compute_aborted_snapshot_is_explicit_failure():
    p = _providers()
    p["snapshot_fn"] = lambda cs: ({}, {"chunks_total": 2, "chunks_failed": 2, "aborted": True})
    out = sc.collect_and_compute("20261014", **p)
    assert out["ok"] is False
    assert "中止" in out["reason"] and "事故铁律" in out["reason"]


def test_collect_and_compute_no_zt_records_is_explicit_failure():
    """快照拿回来了但无真涨停条目 ⇒ 显式失败, 绝不落 0 冒充(会污染历史基线)。"""
    p = _providers(snap={"002238.SZ": {"TimeNow": "15:00:00"}})
    out = sc.collect_and_compute("20261014", **p)
    assert out["ok"] is False and "无真涨停条目" in out["reason"]


def test_collect_and_compute_empty_universe_is_explicit_failure():
    out = sc.collect_and_compute("20261014", **_providers(codes=[]))
    assert out["ok"] is False and "码表" in out["reason"]


def test_collect_and_compute_missing_events_keeps_streak_none():
    out = sc.collect_and_compute("20261014", **_providers(events=[]))
    assert out["ok"] is True
    assert out["streak_count"] is None, "没有 limit_up_events 就落 None, 不落 0"
    assert any("limit_up_events" in e for e in out["errors"])


def test_target_trade_date_uses_calendar():
    """周六 ⇒ 目标交易日 = 上周五(快照型接口给的就是最近已收盘交易日)。"""
    # 2026-10-10 是周六
    assert sc.target_trade_date(datetime(2026, 10, 10, 9, 0)) == "20261009"


# ── ⑦ 落库: 幂等 + 不覆盖已有真值 + 拒绝失败态 ────────────────────────────

def _db():
    from src.web.database import SessionLocal

    return SessionLocal()


def _cleanup(db) -> None:
    db.execute(text("DELETE FROM market_sentiment_daily WHERE trade_date = '20261014'"))
    db.execute(text("DELETE FROM sentiment_selfcalc_daily WHERE trade_date = '20261014'"))
    db.commit()


def test_persist_writes_compat_columns_and_detail_row():
    db = _db()
    try:
        _cleanup(db)
        out = sc.collect_and_compute("20261014", **_providers())
        w = sc.persist(out, db=db)
        assert w["ok"] is True and w["written"] == 1

        row = db.execute(text(
            "SELECT limit_up_count, limit_up_open_count, streak_count, seal_success_money, "
            "seal_fail_money, source FROM market_sentiment_daily "
            "WHERE trade_date='20261014' AND market='CN'"
        )).first()
        assert row[0] == 3 and row[1] == 2 and row[2] == 1
        assert row[3] == pytest.approx(0.161) and row[4] == pytest.approx(0.115)
        assert row[5] == "tdx_selfcalc"

        det = db.execute(text(
            "SELECT approximation, caliber, money_unit, sealed_count, open_times_gt0, "
            "chunks_total, extra FROM sentiment_selfcalc_daily "
            "WHERE trade_date='20261014' AND market='CN'"
        )).first()
        assert det[0] == 1 and det[1] == "close_ge_limit_price" and det[2] == "亿元"
        assert det[3] == 2 and det[4] == 2 and det[5] == 1
        extra = json.loads(det[6])
        assert extra["approximation"]["seal_money"]
        assert extra["commission"].startswith("自算替代云 SC")
    finally:
        _cleanup(db)
        db.close()


def test_persist_is_idempotent():
    db = _db()
    try:
        _cleanup(db)
        out = sc.collect_and_compute("20261014", **_providers())
        sc.persist(out, db=db)
        sc.persist(out, db=db)
        n1 = db.execute(text(
            "SELECT COUNT(*) FROM sentiment_selfcalc_daily WHERE trade_date='20261014'"
        )).scalar()
        n2 = db.execute(text(
            "SELECT COUNT(*) FROM market_sentiment_daily WHERE trade_date='20261014'"
        )).scalar()
        assert n1 == 1 and n2 == 1
    finally:
        _cleanup(db)
        db.close()


def test_persist_does_not_overwrite_existing_truth():
    """兼容列只填空位(COALESCE): 云 SC 真值若在, 自算**不覆盖**(可选增强源语义)。"""
    db = _db()
    try:
        _cleanup(db)
        db.execute(text(
            "INSERT INTO market_sentiment_daily (trade_date, market, limit_up_count, source) "
            "VALUES ('20261014', 'CN', 99, 'tdx_tq')"
        ))
        db.commit()
        out = sc.collect_and_compute("20261014", **_providers())
        sc.persist(out, db=db)
        row = db.execute(text(
            "SELECT limit_up_count, limit_up_open_count, streak_count, source "
            "FROM market_sentiment_daily WHERE trade_date='20261014' AND market='CN'"
        )).first()
        assert row[0] == 99, "已有真值不被自算覆盖"
        assert row[1] == 2 and row[2] == 1, "空位仍被自算填上"
        assert row[3] == "tdx_tq", "首写来源保留"
    finally:
        _cleanup(db)
        db.close()


def test_persist_refuses_failed_result():
    db = _db()
    try:
        _cleanup(db)
        w = sc.persist({"ok": False, "reason": "快照无真涨停条目", "trade_date": "20261014"}, db=db)
        assert w["ok"] is False and w["written"] == 0
        n = db.execute(text(
            "SELECT COUNT(*) FROM market_sentiment_daily WHERE trade_date='20261014'"
        )).scalar()
        assert n == 0, "失败态绝不写半行"
    finally:
        _cleanup(db)
        db.close()


# ── 常量约束 ──────────────────────────────────────────────────────────────

def test_chunk_constant_respects_accident_rule():
    assert sc._CHUNK <= 50, "事故铁律(2026-09-23 客户端假死): 单次 TQ 请求 ≤50 只"
    assert sc._MAX_CHUNK_FAILS >= 1


def test_full_market_zero_fields_not_counted():
    # 全市场口径: ZT 字段存在但全 0/空的普通票不得计入涨停家数(回归: 5581 假数)
    snap = {
        "000001.SZ": {"OpenTimesZT": "0", "FDVolMaxZT": "0", "FirstTimeZT": ""},
        "000002.SZ": {"OpenTimesZT": "0", "FDVolMaxZT": "0"},
    }
    prices = {
        "000001": {"close": 10.0, "prev_close": 9.5},
        "000002": {"close": 20.0, "prev_close": 19.0},
    }
    m = sc.zt_metrics(snap, prices, {"000001": "甲股", "000002": "乙股"})
    assert m["limit_up_count"] == 0, m
