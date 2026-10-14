# -*- coding: utf-8 -*-
"""连板梯队定型 payload 收盘预渲染落库(2026-10-14)。

背景(审计定性): `/api/theme-mood/ladder` 的 `_ladder_payload` **每次请求重算** —— 20 天
全市场 limit_up_events + 1100+ 只个股 OHLC 批量查询(自注释实测 6.5s), 而 TTL 盘中仅 30s,
到期即再吃一次 6.5s。但梯队**历史交易日的连板数不可变**(只随 limit_up_events 落库而定),
盘中唯一会变的只有"当日行"(既有 ladder_live_state 盘中状态机)。修法: 收盘 cron 预渲染
当日定型 payload 落 `ladder_daily_snapshot`(迁移 190) → 请求期读库为主(毫秒级) + 盘中只
叠加 live 快照的当日行; 快照缺行/落后回退 `_ladder_payload` 现算并**后台**补写。

本文件钉(禁真网络; 落库回环 = 测试 SQLite, 其余全 mock):
  ① 快照 upsert 幂等 —— 同日同 (window_days, mode) 覆盖, 只留一行;
  ② 收盘预渲染作业幂等 + 事件表落后时不落"落后快照";
  ③ 读库命中 → **零重算**(`_ladder_payload` 一次都不被调用);
  ④ 盘中 live 只叠加**当日行**(历史日原封不动来自快照), 且同一天不重复叠两列;
  ⑤ mode=finalized 不叠 live; 只请求 live/finalized 也能回退读 'auto' 行的历史部分;
  ⑥ 快照缺行/落后 → 回退现算 + 后台补写(不阻塞请求);
  ⑦ 读库失败静默(不 500, 回退现算);
  ⑧ 响应字段集与 `_ladder_payload` 完全一致(前端零改动)。
"""
from __future__ import annotations

import threading
import time

import pytest
from sqlalchemy import text

from src.web.api import theme_mood as tm
from src.web.cache.biz_cache import biz_cache

D1 = "20261013"
D2 = "20261014"
LIVE_D = "20261015"
_WINDOWS = (7, 10, 20, 21, 30)


def _engine():
    from src.db.session import engine

    return engine


def _wipe() -> None:
    try:
        with _engine().begin() as conn:
            conn.execute(text("DELETE FROM ladder_daily_snapshot"))
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """关 L2(不连真 Redis) + 清 biz_cache L1 + 清补写节流 + 清快照表。"""
    monkeypatch.setattr(biz_cache, "_enabled", False, raising=False)
    biz_cache._l1.clear()
    tm._snapshot_write_ts.clear()
    _wipe()
    yield
    biz_cache._l1.clear()
    tm._snapshot_write_ts.clear()
    _wipe()


# ──────────── 夹具: payload / live 快照 ────────────
def _day(date: str, boards=None) -> dict:
    boards = boards or {1: ["600001"], 2: ["600002", "600003"]}
    rows = [
        {"boards": b, "codes": list(cs), "names": list(cs), "seal_n": len(cs),
         "tag": "首板" if b == 1 else None,
         "stocks": [{"symbol": s, "name": s, "candle": None, "strength_label": None}
                    for s in cs]}
        for b, cs in sorted(boards.items(), reverse=True)
    ]
    return {"date": date, "rows": rows, "blown": [], "broken": []}


def _payload(dates=(D1, D2), mode="finalized", live_day=None, live_as_of=None,
             stale=False, note_closing=None) -> dict:
    """与 `_ladder_payload` **同字段集**的定型 payload。"""
    ladder = [_day(d) for d in dates]
    return {"dates": list(dates), "ladder": ladder, "note": "连板数=沿事件表日期序列",
            "mode": mode, "live_day": live_day, "live_as_of": live_as_of,
            "stale": stale, "degraded": None, "note_closing": note_closing,
            "stats": tm._ladder_stats(ladder, live_day)}


def _live_day(date: str = LIVE_D) -> dict:
    return {"date": date,
            "rows": [{"boards": 1, "codes": ["600009"], "names": ["600009"], "seal_n": 1,
                      "tag": "首板", "stocks": []}],
            "blown": [], "broken": [], "charging": [], "provisional": True}


def _live_snap(date: str = LIVE_D, last_ok="14:50", stale=False, closing=False) -> dict:
    return {"live_day": _live_day(date), "meta": {"last_ok": last_ok, "stale": stale},
            "date": date, "closing": closing}


def _boom(*_a, **_k):
    raise AssertionError("命中快照时不得重算 _ladder_payload")


def _rows(window: int, mode: str = "auto") -> int:
    with _engine().connect() as conn:
        return conn.execute(
            text("SELECT COUNT(*) FROM ladder_daily_snapshot WHERE window_days = :w AND mode = :m"),
            {"w": window, "m": mode},
        ).scalar()


# ──────────── ① 快照落库回环 / 幂等 ────────────
def test_snapshot_upsert_roundtrip_and_same_day_overwrite():
    """同日同 (window_days, mode) 再写 = 覆盖(收盘后那次即当日定稿), 只留一行。"""
    p1 = _payload()
    assert tm._write_snapshot_row(20, "auto", "2026-10-13", p1) is True
    got = tm._read_snapshot_row(20, "auto")
    assert got is not None and got[1] == "2026-10-13"
    assert got[0]["dates"] == [D1, D2]
    assert got[0]["ladder"][0]["rows"][0]["codes"] == ["600002", "600003"]

    p2 = _payload(dates=(D1, D2, LIVE_D))
    assert tm._write_snapshot_row(20, "auto", "2026-10-13", p2) is True
    again = tm._read_snapshot_row(20, "auto")
    assert again is not None and again[0]["dates"] == [D1, D2, LIVE_D]
    assert _rows(20) == 1, "同日同 (window_days, mode) 必须只留一行"

    # 不同日 → 各留一行, 读最近
    assert tm._write_snapshot_row(20, "auto", "2026-10-14", _payload(dates=(D2,))) is True
    latest = tm._read_snapshot_row(20, "auto")
    assert latest is not None and latest[1] == "2026-10-14"
    assert latest[0]["dates"] == [D2]


def test_read_snapshot_row_empty_and_silent_on_broken_db(monkeypatch):
    """无行 / 读库炸了 → 一律 None(调用方现算兜底, 不向上抛)。"""
    assert tm._read_snapshot_row(20, "auto") is None

    from src.db import session as db_session

    class _Broken:
        def begin(self):
            raise RuntimeError("db down")

    monkeypatch.setattr(db_session, "engine", _Broken())
    assert tm._read_snapshot_row(20, "auto") is None
    assert tm._snapshot_base(20, "auto") is None


# ──────────── ② 收盘预渲染作业 ────────────
def test_render_snapshots_writes_each_window_and_is_idempotent(monkeypatch):
    calls = {"n": 0}

    def _fake_payload(window, mode):
        calls["n"] += 1
        return _payload()

    monkeypatch.setattr(tm, "_ladder_payload", _fake_payload)
    monkeypatch.setattr(tm, "_latest_event_date", lambda: D2)

    first = tm._render_ladder_snapshots()
    assert first["ok"] is True
    assert first["wrote"] == len(tm.SNAPSHOT_WINDOWS) == calls["n"]
    assert set(first["windows"]) == set(tm.SNAPSHOT_WINDOWS)
    for w in tm.SNAPSHOT_WINDOWS:
        assert _rows(w) == 1

    second = tm._render_ladder_snapshots()
    assert second["ok"] is True and second["wrote"] == len(tm.SNAPSHOT_WINDOWS)
    for w in tm.SNAPSHOT_WINDOWS:
        assert _rows(w) == 1, "重跑必须幂等(同日覆盖, 不新增行)"
    got = tm._read_snapshot_row(20, "auto")
    assert got is not None and got[0]["mode"] == "finalized" and got[0]["live_day"] is None


def test_render_skips_when_events_table_not_caught_up(monkeypatch):
    """事件表最新日 != 梯队末日期(15:35 管线/16:05 自检未成) → 不落落后快照, 作业判失败。"""
    monkeypatch.setattr(tm, "_ladder_payload", lambda w, m: _payload())
    monkeypatch.setattr(tm, "_latest_event_date", lambda: "20260930")

    out = tm._render_ladder_snapshots()
    assert out["ok"] is False and out["wrote"] == 0
    assert out["skipped"] == len(tm.SNAPSHOT_WINDOWS)
    for w in tm.SNAPSHOT_WINDOWS:
        assert _rows(w) == 0


def test_render_no_data_is_failure_and_never_raises(monkeypatch):
    monkeypatch.setattr(tm, "_ladder_payload",
                        lambda w, m: {"dates": [], "ladder": [], "mode": "finalized"})
    monkeypatch.setattr(tm, "_latest_event_date", lambda: None)
    out = tm._render_ladder_snapshots()
    assert out["ok"] is False and out["wrote"] == 0

    def _boom_payload(w, m):
        raise RuntimeError("DB 挂了")

    monkeypatch.setattr(tm, "_ladder_payload", _boom_payload)
    out2 = tm._render_ladder_snapshots()
    assert out2["ok"] is False and all("error" in v for v in out2["windows"].values())


def test_ladder_snapshot_job_guard_and_spawn(monkeypatch):
    """非交易日/未到点 → 跳过不起作业; 到点 → 单飞起一次(作业框架可见)。"""
    from src.core import theme_mood as core_tm

    spawned: list[str] = []
    monkeypatch.setattr(tm, "spawn_ladder_snapshot",
                        lambda reason="scheduled": spawned.append(reason) or {"started": True})

    monkeypatch.setattr(core_tm, "is_settle_due_now", lambda now=None: False)
    out = tm.ladder_snapshot_job()
    assert out["skipped"] is True and spawned == []

    monkeypatch.setattr(core_tm, "is_settle_due_now", lambda now=None: True)
    out2 = tm.ladder_snapshot_job()
    assert out2["started"] is True and spawned == ["scheduled"]


# ──────────── ③ 读库命中 → 零重算 ────────────
def test_snapshot_hit_serves_from_db_without_recompute(monkeypatch):
    base = _payload()
    assert tm._write_snapshot_row(20, "auto", "2026-10-14", base) is True
    monkeypatch.setattr(tm, "_latest_event_date", lambda: D2)
    monkeypatch.setattr(tm, "_cache_ttl", lambda: 1800)
    monkeypatch.setattr(tm, "_ladder_payload", _boom)          # 命中快照 → 一次都不该调
    monkeypatch.setattr(tm, "_live_snapshot", lambda: None)    # 盘外无 live

    out = tm.get_ladder(window=20, mode="auto")
    assert out["dates"] == [D1, D2]
    assert [d["date"] for d in out["ladder"]] == [D1, D2]
    assert out["mode"] == "finalized" and out["live_day"] is None
    assert out["cached"] is False and out["cache_ttl_s"] == 1800
    assert "as_of" in out

    out2 = tm.get_ladder(window=20, mode="auto")               # 第二次命中响应缓存
    assert out2["cached"] is True


def test_snapshot_hit_is_millisecond_even_if_recompute_is_slow(monkeypatch):
    """命中快照时响应时间**与现算成本无关**(把 `_ladder_payload` 拖慢 0.6s 也不等它)。

    这正是本改造的目标: 每次请求现算 6.5s(20 天事件 + 1100+ 只 OHLC) → 读库秒开。
    """
    assert tm._write_snapshot_row(20, "auto", "2026-10-14", _payload()) is True
    monkeypatch.setattr(tm, "_latest_event_date", lambda: D2)
    monkeypatch.setattr(tm, "_live_snapshot", lambda: None)
    monkeypatch.setattr(tm, "_cache_ttl", lambda: 1800)

    def _slow(*_a, **_k):
        time.sleep(0.6)
        raise AssertionError("命中快照时不得重算 _ladder_payload")

    monkeypatch.setattr(tm, "_ladder_payload", _slow)
    t0 = time.perf_counter()
    out = tm.get_ladder(window=20, mode="auto")
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.2, f"快照命中应为毫秒级(实测 {elapsed:.2f}s)"
    assert [d["date"] for d in out["ladder"]] == [D1, D2]


def test_snapshot_isolated_per_window_and_mode_fallback(monkeypatch):
    """窗口不串用; 只请求 live/finalized 时回退读同一窗口的 'auto' 行。"""
    assert tm._write_snapshot_row(20, "auto", "2026-10-14", _payload()) is True
    monkeypatch.setattr(tm, "_latest_event_date", lambda: D2)
    monkeypatch.setattr(tm, "_live_snapshot", lambda: None)

    assert tm._read_snapshot_row(10, "auto") is None, "窗口不同不得串用 20 日快照"
    got = tm._read_snapshot_row(20, "finalized")
    assert got is not None and got[0]["dates"] == [D1, D2]
    got_live = tm._read_snapshot_row(20, "live")
    assert got_live is not None and got_live[0]["dates"] == [D1, D2]

    # mode=finalized: 即使盘中也不叠 live(live_as_of 保持 None)
    monkeypatch.setattr(tm, "_live_snapshot", _boom)
    row = tm._read_snapshot_row(20, "finalized")
    assert row is not None
    out = tm._overlay_live(row[0], "finalized")
    assert out["mode"] == "finalized" and out["live_day"] is None and out["live_as_of"] is None


def test_snapshot_base_rejects_behind_snapshot(monkeypatch):
    """库里已有比快照更新的定型交易日 → 快照不得服务(否则静默吞掉一个交易日)。"""
    assert tm._write_snapshot_row(20, "auto", "2026-10-13", _payload(dates=(D1,))) is True
    monkeypatch.setattr(tm, "_latest_event_date", lambda: D2)
    assert tm._snapshot_base(20, "auto") is None

    monkeypatch.setattr(tm, "_latest_event_date", lambda: D1)
    assert tm._snapshot_base(20, "auto") is not None


def test_date_format_normalization(monkeypatch):
    """快照落库日 'YYYY-MM-DD' 与事件表 MAX 'YYYYMMDD' 格式不同也算同一交易日。"""
    assert tm._norm_date("2026-10-14") == tm._norm_date("20261014") == "20261014"
    assert tm._norm_date(None) == "" and tm._norm_date("") == ""

    assert tm._write_snapshot_row(20, "auto", "2026-10-14", _payload()) is True
    monkeypatch.setattr(tm, "_latest_event_date", lambda: "2026-10-14")   # 带横杠
    assert tm._snapshot_base(20, "auto") is not None, "同一天不同格式不得误判为落后"


# ──────────── ④ 盘中 live 只叠加当日行 ────────────
def test_intraday_overlay_adds_only_today_and_keeps_history_untouched(monkeypatch):
    base = _payload(dates=(D1, D2))
    assert tm._write_snapshot_row(20, "auto", "2026-10-14", base) is True
    monkeypatch.setattr(tm, "_latest_event_date", lambda: D2)
    monkeypatch.setattr(tm, "_ladder_payload", _boom)                  # 历史日不得重算
    monkeypatch.setattr(tm, "_live_snapshot", lambda: _live_snap(LIVE_D, last_ok="14:50"))
    monkeypatch.setattr(tm, "_cache_ttl", lambda: 30)

    out = tm.get_ladder(window=20, mode="auto")
    assert out["mode"] == "live"
    assert out["live_day"]["date"] == LIVE_D
    assert out["live_as_of"] == "14:50"
    assert out["dates"] == [D1, D2], "历史 dates 必须原封不动来自快照"
    assert [d["date"] for d in out["ladder"]] == [D1, D2], "live 日不进 ladder(由 live_day 单独给)"
    assert out["stats"]["first"] == 1, "stats 应用 live 当日行重算"
    assert out["stats"]["blown"] == 0


def test_intraday_closing_window_flags_note(monkeypatch):
    assert tm._write_snapshot_row(20, "auto", "2026-10-14", _payload()) is True
    monkeypatch.setattr(tm, "_latest_event_date", lambda: D2)
    monkeypatch.setattr(tm, "_live_snapshot", lambda: _live_snap(LIVE_D, closing=True))
    out = tm._ladder_serve(20, "auto")
    assert out["note_closing"] == "收盘撮合中, 稍后定型"
    assert out["live_day"] is not None


def test_intraday_overlay_not_duplicated_when_live_day_already_in_snapshot(monkeypatch):
    """live 日已落进 base(或 Redis 隔日残留) → 不叠, 免得同一天出两列。"""
    base = _payload(dates=(D1, LIVE_D))
    monkeypatch.setattr(tm, "_live_snapshot", lambda: _live_snap(LIVE_D))
    out = tm._overlay_live(base, "auto")
    assert out["mode"] == "finalized" and out["live_day"] is None
    assert out["stale"] is False and out["live_as_of"] is None and out["note_closing"] is None

    monkeypatch.setattr(tm, "_live_snapshot", lambda: _live_snap(D2))
    out2 = tm._overlay_live(base, "auto")
    assert out2["mode"] == "finalized" and out2["live_day"] is None


def test_intraday_stale_live_marked(monkeypatch):
    assert tm._write_snapshot_row(20, "auto", "2026-10-14", _payload()) is True
    monkeypatch.setattr(tm, "_latest_event_date", lambda: D2)
    monkeypatch.setattr(tm, "_live_snapshot", lambda: _live_snap(LIVE_D, stale=True))
    out = tm._ladder_serve(20, "auto")
    assert out["mode"] == "live" and out["stale"] is True


# ──────────── ⑤ 开盘前(9:30 前)live_day 日期落后也不叠 ────────────
def test_no_overlay_when_no_live_snapshot(monkeypatch):
    assert tm._write_snapshot_row(20, "auto", "2026-10-14", _payload()) is True
    monkeypatch.setattr(tm, "_latest_event_date", lambda: D2)
    monkeypatch.setattr(tm, "_live_snapshot", lambda: None)
    out = tm._ladder_serve(20, "auto")
    assert out["mode"] == "finalized" and out["live_day"] is None
    assert out["dates"] == [D1, D2]


# ──────────── ⑥ 快照缺行/落后 → 回退现算 + 后台补写 ────────────
def _record_writes(monkeypatch) -> list:
    writes: list = []
    monkeypatch.setattr(tm, "_write_snapshot_async",
                        lambda w, m, d, p: writes.append((w, m, d, p)))
    return writes


def test_missing_snapshot_falls_back_and_backfills_candidate(monkeypatch):
    computed = _payload(mode="live", live_day=_live_day(LIVE_D), live_as_of="14:50")
    monkeypatch.setattr(tm, "_ladder_payload", lambda w, m: computed)
    monkeypatch.setattr(tm, "_latest_event_date", lambda: D2)
    writes = _record_writes(monkeypatch)

    out = tm._ladder_serve(20, "auto")
    assert out is computed, "缺行时必须原样返回现算结果(行为与改造前一致)"
    assert len(writes) == 1
    w, m, _date, cand = writes[0]
    assert (w, m) == (20, "auto")
    assert cand["mode"] == "finalized" and cand["live_day"] is None, "落库的是定型视图"
    assert cand["dates"] == [D1, D2] and cand["ladder"] == computed["ladder"]
    assert cand["live_as_of"] is None and cand["stale"] is False


def test_behind_snapshot_falls_back_and_backfills(monkeypatch):
    assert tm._write_snapshot_row(20, "auto", "2026-10-13", _payload(dates=(D1,))) is True
    computed = _payload(dates=(D1, D2))
    monkeypatch.setattr(tm, "_ladder_payload", lambda w, m: computed)
    monkeypatch.setattr(tm, "_latest_event_date", lambda: D2)
    writes = _record_writes(monkeypatch)

    out = tm._ladder_serve(20, "auto")
    assert out["dates"] == [D1, D2]
    assert len(writes) == 1 and writes[0][3]["dates"] == [D1, D2]


def test_no_backfill_when_payload_not_at_latest_event_day(monkeypatch):
    """现算结果自身就落后(数据断档) → 不落库(落后快照会污染后续读)。"""
    monkeypatch.setattr(tm, "_ladder_payload", lambda w, m: _payload(dates=(D1,)))
    monkeypatch.setattr(tm, "_latest_event_date", lambda: D2)
    writes = _record_writes(monkeypatch)

    out = tm._ladder_serve(20, "auto")
    assert out["dates"] == [D1]
    assert writes == []


def test_no_backfill_when_events_latest_unknown(monkeypatch):
    """事件表最新日读不到(空表/查询异常) → 无从确认覆盖, 不落库(不确定就不写)。"""
    monkeypatch.setattr(tm, "_ladder_payload", lambda w, m: _payload())
    monkeypatch.setattr(tm, "_latest_event_date", lambda: None)
    writes = _record_writes(monkeypatch)

    tm._ladder_serve(20, "auto")
    assert writes == []

    out = tm._render_ladder_snapshots()
    assert out["ok"] is False and out["wrote"] == 0
    assert all("skipped" in v for v in out["windows"].values())


def test_backfill_skipped_when_no_ladder(monkeypatch):
    monkeypatch.setattr(tm, "_ladder_payload",
                        lambda w, m: {"dates": [], "ladder": [], "mode": "finalized"})
    monkeypatch.setattr(tm, "_latest_event_date", lambda: None)
    writes = _record_writes(monkeypatch)
    tm._ladder_serve(20, "auto")
    assert writes == []


def test_async_backfill_does_not_block_and_throttles(monkeypatch):
    """补写走后台线程(请求不等它); 同一 (window, mode) 30s 内只补写一次。"""
    done = threading.Event()
    calls: list = []

    def _slow_write(w, m, d, p):
        calls.append((w, m))
        time.sleep(0.3)
        done.set()
        return True

    monkeypatch.setattr(tm, "_write_snapshot_row", _slow_write)

    t0 = time.perf_counter()
    tm._write_snapshot_async(20, "auto", "2026-10-14", _payload())
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.15, f"补写不得阻塞请求(实测 {elapsed:.2f}s)"

    tm._write_snapshot_async(20, "auto", "2026-10-14", _payload())   # 节流内 → 不再起线程
    assert done.wait(3), "后台线程应最终写库"
    time.sleep(0.1)
    assert calls == [(20, "auto")], f"节流应只放行一次, 实际 {calls}"


def test_fallback_end_to_end_then_hit(monkeypatch):
    """冷启动现算并补写 → 补写落库后第二个请求(缓存已过期/换 worker)直接命中, 零重算。"""
    calls = {"n": 0}

    def _payload_and_count(w, m):
        calls["n"] += 1
        return _payload()

    monkeypatch.setattr(tm, "_ladder_payload", _payload_and_count)
    monkeypatch.setattr(tm, "_latest_event_date", lambda: D2)
    monkeypatch.setattr(tm, "_cache_ttl", lambda: 0)      # 立刻过期 → 每请求都走 _ladder_serve
    monkeypatch.setattr(tm, "_live_snapshot", lambda: None)

    first = tm.get_ladder(window=20, mode="auto")
    assert first["dates"] == [D1, D2] and calls["n"] == 1

    for _ in range(60):                                    # 等后台补写落库
        if _rows(20) == 1:
            break
        time.sleep(0.05)

    monkeypatch.setattr(tm, "_ladder_payload", _boom)
    second = tm.get_ladder(window=20, mode="auto")
    assert second["dates"] == [D1, D2], "第二个请求应命中快照(此后零重算)"


# ──────────── ⑧ 响应字段集不变(前端零改动) ────────────
def test_served_field_set_matches_computed_payload(monkeypatch):
    """快照路径(含 live 叠加)与现算路径的响应字段集必须逐键一致 —— 前端零改动。"""
    computed = _payload()
    monkeypatch.setattr(tm, "_live_snapshot", lambda: None)
    assert set(tm._overlay_live(computed, "auto")) == set(computed)

    monkeypatch.setattr(tm, "_live_snapshot", lambda: _live_snap(LIVE_D, last_ok="14:50"))
    base = _payload(dates=(D1, D2))                      # 快照只到 D2, live 日 = D3
    cand = tm._snapshot_candidate(_payload(mode="live", live_day=_live_day(LIVE_D)))
    assert cand is not None
    served_live = tm._overlay_live(cand, "auto")
    assert set(served_live) == set(computed)
    assert set(tm._overlay_live(base, "live")) == set(computed)
    assert set(tm._overlay_live(base, "finalized")) == set(computed)


def test_snapshot_candidate_strips_live_fields():
    live = _payload(mode="live", live_day=_live_day(LIVE_D), live_as_of="14:50",
                    stale=True, note_closing="收盘撮合中, 稍后定型")
    cand = tm._snapshot_candidate(live)
    assert cand is not None
    assert cand["mode"] == "finalized" and cand["live_day"] is None
    assert cand["live_as_of"] is None and cand["stale"] is False
    assert cand["note_closing"] is None
    assert cand["dates"] == live["dates"] and cand["ladder"] == live["ladder"]
    assert cand["stats"] == tm._ladder_stats(live["ladder"], None)
    assert tm._snapshot_candidate({"dates": [], "ladder": []}) is None
    assert tm._snapshot_candidate(_payload()) is not None
