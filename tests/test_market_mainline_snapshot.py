# -*- coding: utf-8 -*-
"""市场主线缓存收口: 日频快照落库 + 读侧 stale 标注(2026-10-14)。

背景(落库缺口审计): `/api/market/mainline` 原来是**模块内进程 dict 缓存** —— 多 worker
各存各的、重启即丢(重启后第一个请求必吃 16~37s 冷启动重拉涨停池), 且违反 AGENTS
『缓存一律走 `src/web/cache/biz_cache.py`』。修法: ① biz_cache(L1 内存 + L2 Redis);
② 每次成功聚合 upsert 当日快照进 `mainline_daily_snapshot`(先例 market_data 的
market_flow_snapshots); ③ 读侧『够新读缓存/读库、过旧回源 + stale 标注』(SWR 不阻塞)。

本文件钉(禁真网络; 落库回环用测试 SQLite, 其余全 mock):
  ① 日频快照 upsert → 读回一致 + 同日覆盖只留一行(date 主键);
  ② 快照缺 cache_ts 时用列 computed_ts 回填(下游才算得出数据年龄);
  ③ 读侧"够新读库": 库里有新鲜快照 → **不回源**(零网络);
  ④ 读侧"过旧": 先返旧值(毫秒级, 不阻塞) + stale=True + age_s, 后台单飞回源刷新;
  ⑤ 两层都空(全新部署) → 同步取一次并回填 biz_cache;
  ⑥ 读库失败静默返回 None(回源兜底, 不 500)。
"""
from __future__ import annotations

import json
import threading
import time
from datetime import date

import pytest
from sqlalchemy import text

from src.web.api import market_mainline as mm
from src.web.cache.biz_cache import biz_cache


def _engine():
    from src.web.database import engine

    return engine


def _today() -> str:
    return date.today().isoformat()


def _delete_today() -> None:
    try:
        with _engine().begin() as conn:
            conn.execute(
                text("DELETE FROM mainline_daily_snapshot WHERE date = :d"), {"d": _today()}
            )
    except Exception:
        pass


def _snap_payload(note: str = "snap", age_s: float = 0.0) -> dict:
    return {
        "total_groups": 3,
        "ranked_groups": [{"name": "AI算力", "rank": 1}],
        "unranked": [],
        "note": note,
        "cache_ts": time.time() - age_s,
    }


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """关 L2(不连真 Redis) + 清 biz_cache/响应缓存/单飞标志 + 清当日快照行。

    落库是后台线程(payload 里 `_try_write_daily_snapshot_async`)。测试里把它换成
    "只记录调用" —— 避免后台写线程与用例 teardown 删行赛跑; 真·DB 回环由
    `_upsert_daily_snapshot` 直测。
    """
    monkeypatch.setattr(biz_cache, "_enabled", False, raising=False)
    biz_cache._l1.clear()
    mm.clear_market_mainline_cache()
    mm._refreshing = False
    _delete_today()

    writes: list[dict] = []
    monkeypatch.setattr(
        mm, "_try_write_daily_snapshot_async", lambda p: writes.append(dict(p))
    )
    yield writes

    biz_cache._l1.clear()
    mm.clear_market_mainline_cache()
    mm._refreshing = False
    _delete_today()


# ──────────── ① / ② 日频快照落库回环 ────────────
def test_daily_snapshot_upsert_then_read_roundtrip(_isolate):
    """① upsert 当日一行 → 读回一致; 同日再写覆盖(= 收盘后那次即定稿), 只留一行。"""
    p = _snap_payload("收盘定稿")
    assert mm._upsert_daily_snapshot(p) is True

    back = mm._read_daily_snapshot()
    assert back is not None
    assert back["note"] == "收盘定稿"
    assert back["ranked_groups"] == [{"name": "AI算力", "rank": 1}]
    assert abs(float(back["cache_ts"]) - float(p["cache_ts"])) < 1.0

    assert mm._upsert_daily_snapshot(_snap_payload("再次刷新")) is True
    again = mm._read_daily_snapshot()
    assert again is not None and again["note"] == "再次刷新"

    with _engine().connect() as conn:
        cnt = conn.execute(
            text("SELECT COUNT(*) FROM mainline_daily_snapshot WHERE date = :d"),
            {"d": _today()},
        ).scalar()
    assert cnt == 1, "date 主键应保证同日只留一行"


def test_snapshot_missing_cache_ts_backfilled_from_computed_ts(_isolate):
    """② payload 没有 cache_ts 时用列 computed_ts 回填(诚实标注数据年龄)。"""
    old = time.time() - 3600
    body = json.dumps({"total_groups": 1, "note": "无cache_ts", "ranked_groups": []},
                      ensure_ascii=False)
    with _engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO mainline_daily_snapshot (date, payload, computed_ts)"
                " VALUES (:d, :p, :c)"
            ),
            {"d": _today(), "p": body, "c": old},
        )

    snap = mm._read_daily_snapshot()
    assert snap is not None and snap["note"] == "无cache_ts"
    assert abs(float(snap["cache_ts"]) - old) < 1.0


def test_read_snapshot_failure_is_silent(monkeypatch):
    """⑥ 读库炸了(库不可达/表缺) → 返回 None, 由上层回源兜底, 不向上抛。"""
    from src.web import database as db_mod

    class _Broken:
        def connect(self):
            raise RuntimeError("db down")

    monkeypatch.setattr(db_mod, "engine", _Broken())
    assert mm._read_daily_snapshot() is None


# ──────────── ③ 够新读库, 不回源 ────────────
def test_fresh_db_snapshot_served_without_refetch(_isolate, monkeypatch):
    fresh = _snap_payload("库新鲜", age_s=1.0)
    monkeypatch.setattr(mm, "_read_daily_snapshot", lambda: dict(fresh))

    def _boom():
        raise AssertionError("库里有新鲜快照时不得回源")

    monkeypatch.setattr(mm, "_fetch_mainline", _boom)

    out = mm.get_market_mainline()
    assert out["note"] == "库新鲜"
    assert out["stale"] is False
    assert out["age_s"] <= mm._CACHE_TTL_S
    assert not _isolate, "新鲜快照不需要重新落库"


def test_fresh_db_snapshot_warms_biz_cache_for_next_call(_isolate, monkeypatch):
    """够新读库后回填 biz_cache → 下一个请求直接命中缓存(不再读库)。"""
    fresh = _snap_payload("库新鲜", age_s=1.0)
    reads = {"n": 0}

    def _read_once():
        reads["n"] += 1
        return dict(fresh)

    monkeypatch.setattr(mm, "_read_daily_snapshot", _read_once)
    monkeypatch.setattr(mm, "_fetch_mainline", lambda: (_ for _ in ()).throw(AssertionError))

    first = mm.get_market_mainline()
    second = mm.get_market_mainline()
    assert first["note"] == second["note"] == "库新鲜"
    assert reads["n"] == 1, "第二次应命中 biz_cache, 不再读库"


# ──────────── ④ 过旧 → 先返旧值 + stale 标注 + 后台回源 ────────────
def test_stale_db_snapshot_marked_stale_and_revalidated(_isolate, monkeypatch):
    age = mm._CACHE_TTL_S + 120.0
    monkeypatch.setattr(mm, "_read_daily_snapshot", lambda: _snap_payload("库过旧", age_s=age))

    started = threading.Event()

    def slow_fetch():
        started.set()
        time.sleep(0.05)
        return _snap_payload("回源新值", age_s=0.0)

    monkeypatch.setattr(mm, "_fetch_mainline", slow_fetch)

    t0 = time.perf_counter()
    out = mm.get_market_mainline()
    elapsed = time.perf_counter() - t0

    assert elapsed < 0.2, f"过旧快照不该阻塞请求(实测 {elapsed:.2f}s)"
    assert out["note"] == "库过旧", "过旧时应先返回旧值"
    assert out["stale"] is True, "必须标注 stale, 不能假装是新的"
    assert out["age_s"] >= mm._CACHE_TTL_S, "必须给出数据年龄"

    assert started.wait(2), "过旧快照必须触发后台回源"

    # 后台刷新完成后: 新值落库 + 缓存换新
    for _ in range(60):
        if _isolate and _isolate[-1]["note"] == "回源新值":
            break
        time.sleep(0.05)
    assert _isolate and _isolate[-1]["note"] == "回源新值", "后台回源结果没落库"
    hit = biz_cache.get_json(mm._CACHE_KEY)
    assert hit is not None and hit["note"] == "回源新值"


def test_too_old_db_snapshot_still_served_not_blocking(_isolate, monkeypatch):
    """超过陈旧上限也仍然先返旧值 + 标注(不把用户挂在 16~37s 的同步回源上)。"""
    age = mm._STALE_MAX_S + 600.0
    monkeypatch.setattr(mm, "_read_daily_snapshot", lambda: _snap_payload("昨日榜", age_s=age))

    release = threading.Event()

    def hanging_fetch():
        release.wait(5)
        return _snap_payload("新值")

    monkeypatch.setattr(mm, "_fetch_mainline", hanging_fetch)

    t0 = time.perf_counter()
    out = mm.get_market_mainline()
    elapsed = time.perf_counter() - t0
    release.set()

    assert elapsed < 0.2
    assert out["note"] == "昨日榜" and out["stale"] is True
    assert out["age_s"] >= mm._STALE_MAX_S


# ──────────── ⑤ 两层都空 → 同步取一次并回填 ────────────
def test_cold_path_no_cache_no_snapshot_syncs_once_then_caches(_isolate, monkeypatch):
    monkeypatch.setattr(mm, "_read_daily_snapshot", lambda: None)
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return _snap_payload("冷启动", age_s=0.0)

    monkeypatch.setattr(mm, "_fetch_mainline", fetch)

    first = mm.get_market_mainline()
    assert first["note"] == "冷启动" and first["stale"] is False and first["age_s"] == 0.0
    assert calls["n"] == 1

    # 回填后第二次不再回源
    second = mm.get_market_mainline()
    assert second["note"] == "冷启动" and calls["n"] == 1
    # 成功聚合 → 触发日频快照落库
    assert _isolate and _isolate[-1]["note"] == "冷启动"


def test_successful_aggregation_triggers_snapshot_write(_isolate, monkeypatch):
    """每次成功聚合都必须把当日快照写入(收盘后那次即定稿)。"""
    monkeypatch.setattr(mm, "_read_daily_snapshot", lambda: None)
    monkeypatch.setattr(mm, "_fetch_mainline", lambda: _snap_payload("聚合成功"))

    mm.get_market_mainline()
    assert [w["note"] for w in _isolate] == ["聚合成功"]
