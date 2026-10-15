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
        # ⚠️ 日期字面量禁写死: 实现按 now(_CST) 的「当日」过滤, 写死日期会在翻日(00:00)后
        # 变成「非当日」被剔除(2026-10-15 00:18 CI shard3 实锤) —— 夹具必须跟随当日。
        from datetime import datetime
        from zoneinfo import ZoneInfo

        _today = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
        conn.execute(text("INSERT INTO seal_quality_samples (ts, symbol, market) VALUES "
                          "(:ts, :s, 'CN')"),
                     {"ts": f"{_today}T09:31:00", "s": "000001"})
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


# ═══════════════ 读路径: orderbook-ob / klines _build_orderbook ═══════════════

import src.web.api.orderbook as obmod          # noqa: E402
import src.web.api.klines as kapi              # noqa: E402
import src.core.orderbook_engine as obe        # noqa: E402

_FAKE_RUN = {
    "events": [{"type": "托单", "side": "bid", "price_level": 1, "price": 11.27,
                "delta_hands": 5000, "duration_s": 0.3, "ts": 1.0, "note": ""}],
    "ob_series": [{"ts": 1.0, "dt": "2026-10-14T10:30:00", "bid_amt10": 123456.0,
                   "ask_amt10": 23456.0, "ob": 0.6807, "label": "买压"}],
    "ghost_ratio": 0.25,
    "summary": "symbol=USZA002361 快照数=1 用时=1.0s | 交易时段 | 事件数=1 | OB均值=0.6807",
}


def _frames(n=2):
    return [osnap.trim_frame(_snap(dt=f"2026-10-14T10:00:{i:02d}")) for i in range(n)]


def _snap_meta(age=5, n=2):
    frames = _frames(n)
    return {
        "symbol": "USZA002361", "frames": frames, "frame_count": len(frames),
        "latest_ts": frames[-1]["dt"], "collected_at": 0.0,
        "snapshot_age_sec": age, "source": "thsdk",
    }


@pytest.fixture(autouse=True)
def _no_img_and_ths_present(monkeypatch):
    monkeypatch.setattr(obe, "find_img_file", lambda *a, **k: None)
    monkeypatch.setattr(obe, "THS", object())


# ── ① 读快照优先 ──

def test_ob读快照优先不调run(monkeypatch):
    monkeypatch.setattr(obmod.orderbook_snapshot, "read_snapshot", lambda s: _snap_meta(age=5))
    monkeypatch.setattr(obe, "run", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("够新快照不该穿透实时")))

    out = obmod.get_orderbook_ob("002361")

    assert out["available"] is True
    assert out["source"] == "snapshot"
    assert out["snapshot_age_sec"] == 5
    assert out["as_of"] == "2026-10-14T10:00:01"
    assert len(out["ob_series"]) == 2
    assert "陈旧度" in out["note"]


def test_ob快照超龄穿透实时(monkeypatch):
    monkeypatch.setattr(obmod.orderbook_snapshot, "read_snapshot", lambda s: _snap_meta(age=999))
    monkeypatch.setattr(obmod, "_RETRY_BACKOFF_S", 0.0)
    called = {"n": 0}

    def _run(*a, **k):
        called["n"] += 1
        return _FAKE_RUN

    monkeypatch.setattr(obe, "run", _run)

    out = obmod.get_orderbook_ob("002361")
    assert called["n"] == 1
    assert out["available"] is True
    assert "snapshot" not in out                 # 穿透成功路径保持旧字段集
    assert set(out.keys()) == {"available", "ob_series", "events", "ghost_ratio", "note"}


def test_ob主动刷新穿透快照(monkeypatch):
    monkeypatch.setattr(obmod.orderbook_snapshot, "read_snapshot", lambda s: _snap_meta(age=1))
    monkeypatch.setattr(obmod, "_RETRY_BACKOFF_S", 0.0)
    called = {"n": 0}

    def _run(*a, **k):
        called["n"] += 1
        return _FAKE_RUN

    monkeypatch.setattr(obe, "run", _run)

    out = obmod.get_orderbook_ob("002361", refresh=True)
    assert called["n"] == 1 and out["available"] is True


def test_ob无快照实时不可用显式降级(monkeypatch):
    monkeypatch.setattr(obmod.orderbook_snapshot, "read_snapshot", lambda s: None)
    monkeypatch.setattr(obmod, "_RETRY_BACKOFF_S", 0.0)
    monkeypatch.setattr(obe, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))

    out = obmod.get_orderbook_ob("002361")
    assert out["available"] is False and out["source"] == "thsdk"
    assert "上游 thsdk" in out["note"] and out["as_of"]


def test_ob超龄且实时失败note标超龄(monkeypatch):
    monkeypatch.setattr(obmod.orderbook_snapshot, "read_snapshot", lambda s: _snap_meta(age=300))
    monkeypatch.setattr(obmod, "_RETRY_BACKOFF_S", 0.0)
    monkeypatch.setattr(obe, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))

    out = obmod.get_orderbook_ob("002361")
    assert out["available"] is False
    assert "后台快照超龄" in out["note"] and "300s" in out["note"]


def test_ob端点refresh参数透传(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setattr(obmod.orderbook_snapshot, "read_snapshot", lambda s: _snap_meta(age=1))
    monkeypatch.setattr(obmod, "_RETRY_BACKOFF_S", 0.0)
    monkeypatch.setattr(obe, "run", lambda *a, **k: _FAKE_RUN)

    app = FastAPI()
    app.include_router(obmod.router, prefix="/api/orderbook-ob")
    client = TestClient(app)

    # refresh=true → 穿透实时(旧字段集)
    r1 = client.get("/api/orderbook-ob", params={"symbol": "002361", "refresh": "true"})
    assert r1.status_code == 200 and r1.json() == {
        "available": True, "ob_series": _FAKE_RUN["ob_series"],
        "events": _FAKE_RUN["events"], "ghost_ratio": 0.25, "note": _FAKE_RUN["summary"],
    }
    # 默认 → 读快照
    r2 = client.get("/api/orderbook-ob/002361")
    assert r2.status_code == 200 and r2.json()["source"] == "snapshot"


# ── klines _build_orderbook ──

def test_build_orderbook读快照优先(monkeypatch):
    monkeypatch.setattr(osnap, "fresh_snapshot", lambda s: _snap_meta(age=3))
    monkeypatch.setattr(obe, "fetch_snapshot", lambda *a: (_ for _ in ()).throw(
        AssertionError("够新快照不该打 thsdk")))

    ob = kapi._build_orderbook("002361")
    assert ob is not None and ob["available"] is True
    assert ob["source"] == "snapshot" and ob["snapshot_age_sec"] == 3


def test_build_orderbook无快照回退img(monkeypatch):
    monkeypatch.setattr(osnap, "fresh_snapshot", lambda s: None)
    snaps = [obe.img_frame_to_snapshot(
        __import__("src.core.tdx_img_parser", fromlist=["ImgSnapshot"]).ImgSnapshot(
            t="10:00:00", bid_prices=[11.27], bid_vols=[10000],
            ask_prices=[11.28], ask_vols=[4000]),
        ts=1.0, dt_iso="10:00:00")]
    monkeypatch.setattr(obe, "find_img_file", lambda *a, **k: "/tmp/x.img")
    monkeypatch.setattr(obe, "load_snapshots_from_img", lambda *a, **k: snaps)

    ob = kapi._build_orderbook("002361")
    assert ob is not None and ob.get("img_path") == "/tmp/x.img"


def test_build_orderbook全无返回None(monkeypatch):
    monkeypatch.setattr(osnap, "fresh_snapshot", lambda s: None)
    monkeypatch.setattr(obe, "find_img_file", lambda *a, **k: None)
    monkeypatch.setattr(obe, "fetch_snapshot", lambda *a: None)

    ob = kapi._build_orderbook("002361")
    assert ob is not None and ob.get("available") is False   # 显式无数据, 不编造
