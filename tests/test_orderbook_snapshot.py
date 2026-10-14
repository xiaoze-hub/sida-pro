# -*- coding: utf-8 -*-
"""十档盘口快照后台采集专项测试(2026-10-14)。

覆盖:
  ① 采集落库: 滚动窗追加 + 截断(`_WINDOW`) + 旧→新顺序 + latest_ts;
  ② 空盘口不落库(无真实买卖价, 0 不冒充);
  ③ 取数失败/超时/thsdk 缺失 → 不抛, 显式 reason;
  ④ 节流: 窗口内跳过, force 绕过;
  ⑤ 非交易时段跳过;
  ⑥ 陈旧度: read_snapshot 标 age, fresh_snapshot 超龄 None, 阈值 env 可配;
  ⑦ rebuild_snapshot 往返(float 价格键 + 手数求和);
  ⑧ 无快照/库不可达 → None(不编造);
  ⑨ universe: 自选∪持仓∪当日异动, 去重 + 上限。

禁真实网络: thsdk / DB 全打桩, 库用内存 SQLite。
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

import src.core.orderbook_snapshot as osnap
import src.core.quote_snapshots as _qs
from src.web.migrations import _m194_orderbook_snapshots_table


# ─────────────────────────────── 夹具 ───────────────────────────────

def _mk_engine(with_seal: bool = False):
    eng = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with eng.begin() as conn:
        _m194_orderbook_snapshots_table(conn)
        if with_seal:
            conn.execute(text(
                "CREATE TABLE seal_quality_samples (ts TEXT, symbol TEXT, market TEXT)"
            ))
    return eng


def _use_engine(monkeypatch, eng):
    import src.db.session as dbs

    monkeypatch.setattr(dbs, "engine", eng)


def _snap(dt="2026-10-14T10:00:00", bid1=11.27, ask1=11.28, bid_vol=(100, 200), ask_vol=(80,)):
    bid_levels = [
        {"orderlevel": i + 1, "price": bid1 - i * 0.01, "ordersque": list(bid_vol)}
        for i in range(2)
    ]
    ask_levels = [
        {"orderlevel": i + 1, "price": ask1 + i * 0.01, "ordersque": list(ask_vol)}
        for i in range(2)
    ]
    return {
        "ts": 1.0, "dt": dt,
        "bid_levels": bid_levels, "ask_levels": ask_levels,
        "bid": {lv["price"]: sum(lv["ordersque"]) for lv in bid_levels},
        "ask": {lv["price"]: sum(lv["ordersque"]) for lv in ask_levels},
        "source": "thsdk",
    }


@pytest.fixture(autouse=True)
def _reset():
    osnap._reset_state_for_tests()
    try:
        from src.core.thsdk_breaker import reset_for_tests

        reset_for_tests()
    except Exception:  # pragma: no cover
        pass
    yield
    osnap._reset_state_for_tests()


def _patch_fetch(monkeypatch, counter=None):
    """patch obe.THS + obe.fetch_snapshot 为顺序返回的假快照(禁真网络)。"""
    import src.core.orderbook_engine as obe

    monkeypatch.setattr(obe, "THS", object())
    n = {"i": 0}

    def _fake(ths_code):
        n["i"] += 1
        if counter is not None:
            counter.append(ths_code)
        return _snap(dt=f"2026-10-14T10:00:{n['i']:02d}")

    monkeypatch.setattr(obe, "fetch_snapshot", _fake)
    return n


# ───────────────────── ① 采集落库 + 滚动窗 ─────────────────────

def test_采集落库滚动窗旧到新且截断(monkeypatch):
    eng = _mk_engine()
    _use_engine(monkeypatch, eng)
    _patch_fetch(monkeypatch)

    for _ in range(7):  # 超过窗口 _WINDOW=5
        r = osnap.collect_symbol("002361")
        assert r["written"] is True

    snap = osnap.read_snapshot("USZA002361")
    assert snap is not None
    assert snap["frame_count"] == osnap._WINDOW
    frames = snap["frames"]
    # 旧→新: dt 递增
    dts = [f["dt"] for f in frames]
    assert dts == sorted(dts)
    assert snap["latest_ts"] == frames[-1]["dt"] == "2026-10-14T10:00:07"
    # 十档裁剪: 每帧每侧 <= _LEVELS
    assert all(len(f["bid_levels"]) <= osnap._LEVELS for f in frames)


def test_裸代码与THS代码同键(monkeypatch):
    eng = _mk_engine()
    _use_engine(monkeypatch, eng)
    _patch_fetch(monkeypatch)

    osnap.collect_symbol("002361")
    # 读时给 THS 代码也能命中同一行
    assert osnap.read_snapshot("USZA002361") is not None
    with eng.connect() as conn:
        rows = conn.execute(text("SELECT COUNT(*) FROM orderbook_snapshots")).scalar()
    assert int(rows) == 1


# ───────────────────── ② 空盘口不落库 ─────────────────────

def test_空盘口不落库(monkeypatch):
    eng = _mk_engine()
    _use_engine(monkeypatch, eng)
    import src.core.orderbook_engine as obe

    monkeypatch.setattr(obe, "THS", object())
    monkeypatch.setattr(obe, "fetch_snapshot",
                        lambda ths_code: {"ts": 1.0, "dt": "x", "bid_levels": [], "ask_levels": []})

    r = osnap.collect_symbol("002361")
    assert r["written"] is False and "空盘口" in r["reason"]
    assert osnap.read_snapshot("002361") is None


# ───────────────────── ③ 取数失败/缺失 不抛 ─────────────────────

def test_取数失败不抛(monkeypatch):
    eng = _mk_engine()
    _use_engine(monkeypatch, eng)
    import src.core.orderbook_engine as obe

    monkeypatch.setattr(obe, "THS", object())

    def _boom(ths_code):
        raise RuntimeError("thsdk -6 超时")

    monkeypatch.setattr(obe, "fetch_snapshot", _boom)
    r = osnap.collect_symbol("002361")
    assert r["written"] is False and r["reason"] == "取数失败/超时"


def test_thsdk缺失不抛(monkeypatch):
    eng = _mk_engine()
    _use_engine(monkeypatch, eng)
    import src.core.orderbook_engine as obe

    monkeypatch.setattr(obe, "THS", None)
    r = osnap.collect_symbol("002361")
    assert r["written"] is False and "thsdk 未安装" in r["reason"]


# ───────────────────── ④ 节流 ─────────────────────

def test_节流窗口内跳过_force绕过(monkeypatch):
    eng = _mk_engine()
    _use_engine(monkeypatch, eng)
    _patch_fetch(monkeypatch)
    monkeypatch.setattr(_qs, "in_trading_window", lambda now=None: True)
    monkeypatch.setattr(osnap, "active_symbols", lambda limit=None: ["002361"])

    r1 = osnap.collect_once()
    assert r1["ok"] is True and r1["skipped"] is False and r1["written"] == 1

    r2 = osnap.collect_once()  # 距上轮 < interval → 跳过
    assert r2["skipped"] is True and r2["reason"] == "节流窗口内"

    r3 = osnap.collect_once(force=True)  # 绕过
    assert r3["skipped"] is False and r3["written"] == 1


# ───────────────────── ⑤ 非交易时段 ─────────────────────

def test_非交易时段跳过(monkeypatch):
    eng = _mk_engine()
    _use_engine(monkeypatch, eng)
    monkeypatch.setattr(_qs, "in_trading_window", lambda now=None: False)
    out = osnap.collect_once()
    assert out["skipped"] is True and out["reason"] == "非交易时段"
    with eng.connect() as conn:
        assert int(conn.execute(text("SELECT COUNT(*) FROM orderbook_snapshots")).scalar()) == 0


# ───────────────────── ⑥ 陈旧度 ─────────────────────

def test_陈旧度标注与超龄(monkeypatch):
    eng = _mk_engine()
    _use_engine(monkeypatch, eng)
    _patch_fetch(monkeypatch)
    monkeypatch.setenv("ORDERBOOK_SNAPSHOT_STALE_SEC", "60")

    # 采一帧, collected_at = 100 秒前 → 超龄
    import src.core.orderbook_snapshot as m

    frame = osnap.trim_frame(_snap())
    assert m.append_frame("002361", frame, now=__import__("time").time() - 100) is True

    snap = osnap.read_snapshot("002361")
    assert snap is not None and snap["snapshot_age_sec"] >= 95
    assert osnap.fresh_snapshot("002361") is None  # 超龄

    # 阈值放大到 300 → 变够新
    monkeypatch.setenv("ORDERBOOK_SNAPSHOT_STALE_SEC", "300")
    assert osnap.fresh_snapshot("002361") is not None


def test_阈值env非法回落默认(monkeypatch):
    monkeypatch.setenv("ORDERBOOK_SNAPSHOT_STALE_SEC", "abc")
    assert osnap.stale_threshold_sec() == osnap._STALE_DEFAULT
    monkeypatch.setenv("ORDERBOOK_SNAPSHOT_INTERVAL_SEC", "99999")  # 越界
    assert osnap.snapshot_interval_sec() == osnap._INTERVAL_DEFAULT


# ───────────────────── ⑦ rebuild 往返 ─────────────────────

def test_rebuild往返float价格键(monkeypatch):
    frame = osnap.trim_frame(_snap(bid_vol=(100, 200), ask_vol=(80,)))
    assert frame is not None
    # 模拟 JSON 往返
    frame2 = json.loads(json.dumps(frame))

    snap = osnap.rebuild_snapshot(frame2)
    assert snap is not None
    # 价格键必须是 float(不是字符串)
    assert all(isinstance(k, float) for k in snap["bid"].keys())
    assert all(isinstance(k, float) for k in snap["ask"].keys())
    assert snap["bid"][11.27] == 300          # 100 + 200
    assert snap["ask"][11.28] == 80
    assert min(snap["bid"].keys()) == 11.26   # float 比较正确


def test_rebuild空帧返回None():
    assert osnap.rebuild_snapshot(None) is None
    assert osnap.rebuild_snapshot({"bid_levels": [], "ask_levels": []}) is None


# ───────────────────── ⑧ 无快照 / 库不可达 ─────────────────────

def test_无快照返回None(monkeypatch):
    _use_engine(monkeypatch, _mk_engine())
    assert osnap.read_snapshot("002361") is None
    assert osnap.fresh_snapshot("002361") is None


def test_库不可达返回None(monkeypatch):
    class _Boom:
        def connect(self):
            raise RuntimeError("db down")

    _use_engine(monkeypatch, _Boom())
    assert osnap.read_snapshot("002361") is None


# ───────────────────── ⑨ universe ─────────────────────

def test_universe合并去重上限(monkeypatch):
    eng = _mk_engine(with_seal=True)
    _use_engine(monkeypatch, eng)
    from src.core import quote_snapshots as qs

    monkeypatch.setattr(qs, "collect_market_symbols",
                        lambda: {"CN": ["002361", "600000", "002361"], "US": ["AAPL"]})
    with eng.begin() as conn:
        conn.execute(text("INSERT INTO seal_quality_samples (ts, symbol, market) VALUES "
                          "(:ts, :s, 'CN')"),
                     {"ts": "2026-10-14T09:31:00", "s": "000001"})
        # 非当日/非法代码
        conn.execute(text("INSERT INTO seal_quality_samples (ts, symbol, market) VALUES "
                          "('2020-01-01T09:31:00', '000002', 'CN')"))

    syms = osnap.active_symbols()
    assert syms == ["002361", "600000", "000001"]   # 去重, US/非6位/非当日剔除
    assert osnap.active_symbols(limit=2) == ["002361", "600000"]


def test_universe来源失败不抛(monkeypatch):
    _use_engine(monkeypatch, _mk_engine())  # 无 seal 表

    class _Boom:
        def connect(self):
            raise RuntimeError("db down")

    from src.core import quote_snapshots as qs

    monkeypatch.setattr(qs, "collect_market_symbols", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    _use_engine(monkeypatch, _Boom())
    assert osnap.active_symbols() == []
