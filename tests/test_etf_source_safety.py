"""ETF 分钟备源安全：日级锁 / 同日单源 / 残缺日失败 / 完整 240 分钟 / 可证实对账。

背景（2026-11-10）：TQ 的 1m 自 11-05 晚停更，东财 `trends2` 兜底补 11/06、11/09。
兜底上线后暴露四个会**静默出错**的洞，本文件逐个用真实隔离 SQLite 复现并钉死：

1. **并发混源**：旧 `write_day` 是「事务内无锁 SELECT 有行吗 → 没有就 INSERT」。
   两个写者（TQ 恢复 + 东财补数）在同一日同时跑，都可能读到 0 行，然后各写一部分；
   TQ 的 upsert 还会把东财的 `source` 改成 `tdx_tq`。现在写前取数据库日级锁
   （PG advisory xact lock / SQLite BEGIN IMMEDIATE），TQ 遇到异源直接拒绝（-2）。
2. **空源**：取数全空时必须是 `fetch_failed` + 退出码 1，不能返回「成功 0 行」。
3. **残缺日**：表里有行 ≠ 已补完。只有 1 只、或某只只有半天，旧逻辑当作
   `date_already_ingested` 静默成功（退出码 0）；现在是 `date_partially_ingested` → 1。
4. **对账不可证实**：对账函数只要没抛异常就标 `reconciled=True`，哪怕一只都没比对。
   现在 `reconciled/verified` 必须逐只对过且无缺席；CLI 默认 `require_verified=True`，
   不可证实就不写并退出 1。

数据库全部是 tmp_path 下的独立 SQLite（真实跑 m243 + m245 建表），不碰
`src.web.database`，也不碰生产库。
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from src.collectors import em_etf_minute as em
from src.collectors import tq_etf_watch as ew
from src.web.migrations import _m243_etf_minute_flow, _m245_etf_minute_source

DAY = "20261106"
NOW = datetime(2026, 11, 10, 9, 0)
CODES = [c for c, _n, _cat in ew.ETF_BASKET]


# ── 夹具 ────────────────────────────────────────────────────────────────

@pytest.fixture()
def db(tmp_path):
    """同一个 SQLite 文件上按需造多个 engine（每个线程/写者一条独立连接）。

    用生产同款 `build_engine`（WAL + busy_timeout + NullPool），确保测的是真实加锁路径，
    而不是一个没开 WAL、语义不同的临时 engine。
    """
    from src.db.dialect import build_engine

    url = f"sqlite:///{tmp_path / 'etf_flow.db'}"
    created = []

    def make():
        eng = build_engine(url)
        created.append(eng)
        return eng

    eng = make()
    with eng.begin() as conn:
        _m243_etf_minute_flow(conn)
        _m245_etf_minute_source(conn)
    yield SimpleNamespace(engine=eng, make=make)
    for e in created:
        e.dispose()


def _bars(amount_wan: float = 0.417, volume: int = 1000, close: float = 4.17):
    """一天完整 240 根（已归一：无 09:30）。单位自洽：0.417 万 / (1000 股 × 4.17)。"""
    return [em.MinuteBar(m, amount_wan, volume, close) for m in em.SESSION_MINUTES]


def _full_day(codes=CODES):
    return {c: _bars() for c in codes}


def _rows(eng, day: str = DAY):
    with eng.connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                text(
                    "SELECT trade_date, minute, etf_code, amount_wan, volume, source "
                    "FROM etf_minute_flow WHERE trade_date = :d"
                ),
                {"d": day},
            ).mappings()
        ]


def _seed(eng, rows: list[tuple[str, str, float, str]]):
    """rows = [(minute, code, amount_wan, source), ...]"""
    with eng.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO etf_minute_flow "
                "(trade_date, minute, etf_code, amount_wan, volume, source) "
                "VALUES (:d, :m, :c, :a, 1000, :s)"
            ),
            [{"d": DAY, "m": m, "c": c, "a": a, "s": s} for m, c, a, s in rows],
        )


def _fake_fetch(payload_for):
    calls: list[str] = []

    def _inner(code, *, ndays=5, timeout=8.0):
        calls.append(code)
        return payload_for(code)

    _inner.calls = calls  # type: ignore[attr-defined]
    return _inner


def _payload(code: str, day: str = "2026-11-06", *, price: float = 4.17, lots: int = 1000):
    """东财报文：09:30 竞价 + 240 根，共 241 行（与真实端点同形态）。"""
    lines = []
    minutes = ["09:30"] + [f"{m[:2]}:{m[2:]}" for m in em.SESSION_MINUTES]
    for hm in minutes:
        yuan = lots * 100 * price
        lines.append(f"{day} {hm},0.000,{price:.3f},{price:.3f},{price:.3f},{lots},{yuan:.3f},{price:.4f}")
    return {"rc": 0, "data": {"code": code, "market": 1, "trends": lines}}


def _all_payload(code):
    return _payload(code.split(".")[0])


# ── 1. 完整 240 分钟：严格集合相等 ──────────────────────────────────────

def test_validate_requires_exactly_240_session_minutes():
    ok, problems = em.validate_day(DAY, _bars())
    assert ok, problems

    # 239 根（缺 10:00）→ 不是完整日
    short = [b for b in _bars() if b.minute != "1000"]
    ok, problems = em.validate_day(DAY, short)
    assert not ok
    assert any("根数" in p or "缺" in p for p in problems)

    # 240 根但重复一根 + 缺一根 → 根数对、集合不对，同样拒绝
    swapped = list(_bars()[:-1]) + [em.MinuteBar("1000", 41.7, 1000, 4.17)]
    ok, problems = em.validate_day(DAY, swapped)
    assert not ok
    assert any("重复" in p or "缺" in p for p in problems)

    # 混进 09:30（未归一）或 13:00（TQ 没有）→ 拒绝
    for extra in ("0930", "1300"):
        bars = _bars() + [em.MinuteBar(extra, 1.0, 1000, 4.1)]
        ok, problems = em.validate_day(DAY, bars)
        assert not ok
        assert any("时段" in p or "多余" in p for p in problems), (extra, problems)


# ── 2. 残缺日：CLI 必须失败，不许当「已补过」 ──────────────────────────

def test_partial_existing_day_is_failure_not_success(db, monkeypatch):
    """只有 1 只标的的一行数据 → 不能算已采完；且不许联网取数、不许补写。"""
    _seed(db.engine, [("0931", CODES[0], 1.0, "tdx_tq")])
    fetch = _fake_fetch(_all_payload)
    monkeypatch.setattr(em, "_get_json", fetch)

    rep = em.backfill([DAY], engine=db.engine, now=NOW, dry_run=False)
    day = rep["days"][0]
    assert day["action"] == "skipped"
    assert day["reason"] == "date_partially_ingested"
    assert day["existing_codes"] == 1
    assert day["existing_sources"] == ["tdx_tq"]
    assert fetch.calls == [], "残缺日不该再联网取数"
    assert em.exit_code(rep) == 1, "残缺日必须让运维脚本报红"
    assert len(_rows(db.engine)) == 1


def test_day_with_one_code_only_half_day_is_partial(db, monkeypatch):
    """一只标的 240 根、其余没有 → 覆盖率不足；一只标的只有 120 根也算残缺。"""
    _seed(db.engine, [(m, CODES[0], 1.0, "tdx_tq") for m in em.SESSION_MINUTES])
    rep = em.backfill([DAY], engine=db.engine, now=NOW, dry_run=True)
    day = rep["days"][0]
    assert day["reason"] == "date_partially_ingested"
    assert day["existing_codes"] == 1
    assert day["existing_minutes"] == 240
    assert em.exit_code(rep) == 1

    _seed(db.engine, [(m, CODES[1], 1.0, "tdx_tq") for m in em.SESSION_MINUTES[:120]])
    rep2 = em.backfill([DAY], engine=db.engine, now=NOW, dry_run=True)
    assert rep2["days"][0]["reason"] == "date_partially_ingested"
    assert rep2["days"][0]["existing_short_codes"] == [CODES[1]]


def test_complete_existing_day_is_benign_skip(db, monkeypatch):
    """20 只 × 240 根、单来源 → 真正的「已采完」，退出码 0，且不再取数。"""
    rows = [
        (m, c, 1.0, "tdx_tq")
        for c in CODES
        for m in em.SESSION_MINUTES
    ]
    _seed(db.engine, rows)
    fetch = _fake_fetch(_all_payload)
    monkeypatch.setattr(em, "_get_json", fetch)

    rep = em.backfill([DAY], engine=db.engine, now=NOW, dry_run=False)
    day = rep["days"][0]
    assert day["reason"] == "date_already_ingested"
    assert day["existing_codes"] == len(CODES)
    assert day["existing_minutes"] == 240
    assert em.exit_code(rep) == 0
    assert getattr(fetch, "calls", []) == []


def test_mixed_source_existing_day_is_never_benign(db):
    """一天两个来源本身就该报红（哪怕行数看起来够），不能当成功。"""
    rows = [(m, c, 1.0, "tdx_tq") for c in CODES for m in em.SESSION_MINUTES]
    rows += [(m, "510999.SH", 2.0, "em_trends2") for m in em.SESSION_MINUTES]
    _seed(db.engine, rows)
    rep = em.backfill([DAY], engine=db.engine, now=NOW, dry_run=True)
    day = rep["days"][0]
    assert day["reason"] == "date_partially_ingested"
    assert sorted(day["existing_sources"]) == ["em_trends2", "tdx_tq"]
    assert em.exit_code(rep) == 1


# ── 3. 空源：明确失败，不写 0 行冒充 ───────────────────────────────────

def test_empty_source_fails_loudly(db, monkeypatch):
    monkeypatch.setattr(em, "_get_json", _fake_fetch(lambda code: {"rc": 0, "data": None}))
    rep = em.backfill([DAY], engine=db.engine, now=NOW, dry_run=False)
    day = rep["days"][0]
    assert day["action"] == "skipped"
    assert day["reason"] == "fetch_failed"
    assert day["rows"] == 0
    assert em.exit_code(rep) == 1
    assert _rows(db.engine) == []


def test_write_day_with_empty_mapping_writes_nothing(db):
    assert em.write_day(DAY, {}, engine=db.engine) == 0
    assert _rows(db.engine) == []


# ── 4. 并发：日级锁，不是无锁 SELECT ────────────────────────────────────

def test_write_day_blocks_until_other_writer_commits(db):
    """A 持锁写 1 行、0.5s 后提交；B 必须**等锁**，然后看到已有行返回 0。

    旧实现（无锁 SELECT）会立刻读到 0 行并尝试写 240 行（1 行冲突、239 行插入），
    要么写出半天数据，要么因 rowcount 不符返回 -1；两条都不允许出现。
    """
    holder = db.make()
    started = threading.Event()

    def hold():
        with holder.begin() as conn:
            em.lock_day(conn, DAY)
            conn.execute(
                text(
                    "INSERT INTO etf_minute_flow "
                    "(trade_date, minute, etf_code, amount_wan, volume, source) "
                    "VALUES (:d, '0931', :c, 1.0, 1000, 'tdx_tq')"
                ),
                {"d": DAY, "c": CODES[0]},
            )
            started.set()
            time.sleep(0.5)
        # 退出 with 才提交并释放锁

    with ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(hold)
        assert started.wait(5), "持锁线程未启动"
        t0 = time.monotonic()
        written = em.write_day(DAY, _full_day([CODES[0]]), engine=db.engine)
        elapsed = time.monotonic() - t0
        fut.result(timeout=10)

    assert written == 0, "锁内看到已有行 → 整日跳过"
    assert elapsed >= 0.3, f"没有等锁就返回了（{elapsed:.3f}s），说明判重没在锁内"
    assert len(_rows(db.engine)) == 1


def test_two_concurrent_writers_never_mix_sources(db):
    """两个进程级写者同时抢同一天：只能有一个写成功，且整日单源。"""
    a, b = db.make(), db.make()
    barrier = threading.Barrier(2)

    def writer(eng, amount):
        barrier.wait(timeout=10)
        return em.write_day(DAY, _full_day([CODES[0]]), engine=eng)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = [pool.submit(writer, a, 1.0), pool.submit(writer, b, 2.0)]
        results = sorted(f.result(timeout=30) for f in futs)

    assert results == [0, 240], results
    rows = _rows(db.engine)
    assert len(rows) == 240
    assert {r["source"] for r in rows} == {"em_trends2"}
    assert len({r["amount_wan"] for r in rows}) == 1, "不许两个写者各写一半"


def test_tq_and_em_race_keeps_single_source(db):
    """TQ 写 2 分钟、东财写 240 分钟同时抢同一天 → 只能一方落库，绝不混源。"""
    a, b = db.make(), db.make()
    barrier = threading.Barrier(2)

    def em_writer():
        barrier.wait(timeout=10)
        return em.write_day(DAY, _full_day([CODES[0]]), engine=a)

    def tq_writer():
        barrier.wait(timeout=10)
        return ew.write_minute(DAY, {CODES[0]: [("0931", 45.0, 1000), ("1500", 45.0, 1000)]}, engine=b)

    with ThreadPoolExecutor(max_workers=2) as pool:
        em_f = pool.submit(em_writer)
        tq_f = pool.submit(tq_writer)
        em_result, tq_result = em_f.result(timeout=30), tq_f.result(timeout=30)

    assert (em_result, tq_result) in {(240, -2), (0, 2)}, (em_result, tq_result)
    rows = _rows(db.engine)
    assert {r["source"] for r in rows} == {"em_trends2" if em_result == 240 else "tdx_tq"}
    assert len(rows) == (240 if em_result == 240 else 2)


# ── 5. TQ 恢复：遇到东财补过的日子必须拒绝，不许覆盖/混源 ─────────────

def test_tq_refuses_to_touch_day_owned_by_em(db):
    _seed(db.engine, [(m, CODES[0], 41.7, "em_trends2") for m in em.SESSION_MINUTES])

    rc = ew.write_minute(
        DAY,
        {CODES[0]: [("0931", 45.0, 1000), ("0932", 46.0, 1100)]},
        engine=db.engine,
    )
    assert rc == -2, "异源日子必须拒绝写入"
    rows = _rows(db.engine)
    assert len(rows) == 240
    assert {r["source"] for r in rows} == {"em_trends2"}
    assert all(r["amount_wan"] == 41.7 for r in rows), "东财数据不许被 TQ 改动"


def test_tq_recovery_is_explicit_failure_in_ingest_day(db, monkeypatch):
    _seed(db.engine, [(m, CODES[0], 41.7, "em_trends2") for m in em.SESSION_MINUTES])
    monkeypatch.setattr(
        ew, "fetch_minute",
        lambda *a, **k: {CODES[0]: [("0931", 45.0, 1000)]},
    )
    out = ew.ingest_day(DAY, engine=db.engine)
    assert out["ok"] is False
    assert out["error"] == "source_conflict"
    assert {r["source"] for r in _rows(db.engine)} == {"em_trends2"}


def test_tq_writes_empty_day_then_em_must_skip(db):
    rc = ew.write_minute(
        DAY,
        {CODES[0]: [("0931", 45.0, 1000), ("1500", 45.0, 1000)]},
        engine=db.engine,
    )
    assert rc == 2
    assert em.write_day(DAY, _full_day([CODES[0]]), engine=db.engine) == 0
    rows = _rows(db.engine)
    assert len(rows) == 2
    assert {r["source"] for r in rows} == {"tdx_tq"}


def test_tq_write_is_still_idempotent_on_own_day(db):
    data = {CODES[0]: [("0931", 45.0, 1000), ("1500", 45.0, 1000)]}
    assert ew.write_minute(DAY, data, engine=db.engine) == 2
    assert ew.write_minute(DAY, data, engine=db.engine) == 2
    rows = _rows(db.engine)
    assert len(rows) == 2
    assert {r["source"] for r in rows} == {"tdx_tq"}


# ── 6. 对账：可证实才算 verified；CLI 默认拒绝不可证实的写入 ─────────────

def _totals(codes=CODES, lots=1000, price=4.17):
    # 日线含竞价 → 241 根合计（09:30 并入 09:31，金额一分不丢）
    return {c: 241 * lots * 100 * price / 1e4 for c in codes}


def test_verified_requires_every_candidate_reconciled(db, monkeypatch):
    monkeypatch.setattr(em, "_get_json", _fake_fetch(_all_payload))
    ref = _totals()
    missing = CODES[5]
    ref.pop(missing)  # 这只没参照 → 不可证实

    rep = em.backfill(
        [DAY], engine=db.engine, now=NOW, dry_run=False,
        daily_ref=lambda d: ref, require_verified=True,
    )
    day = rep["days"][0]
    assert day["action"] == "skipped"
    assert day["reason"] == "unverified"
    assert day["verified"] is False
    assert missing in day["reconcile_missing"]
    assert day["rows_planned"] > 0
    assert em.exit_code(rep) == 1
    assert _rows(db.engine) == [], "不可证实 → 一行都不许写"


def test_unverified_when_reference_unavailable(db, monkeypatch):
    monkeypatch.setattr(em, "_get_json", _fake_fetch(_all_payload))

    def _boom(_day):
        raise RuntimeError("TQ 网关不可用")

    rep = em.backfill(
        [DAY], engine=db.engine, now=NOW, dry_run=False,
        daily_ref=_boom, require_verified=True,
    )
    day = rep["days"][0]
    assert day["reason"] == "unverified"
    assert day["reconciled"] is False
    assert "TQ" in day["reconcile_error"]
    assert _rows(db.engine) == []
    assert em.exit_code(rep) == 1


def test_no_reconcile_is_honest_and_requires_explicit_opt_out(db, monkeypatch):
    """require_verified=False（--no-reconcile）可写，但绝不声称对过账。"""
    monkeypatch.setattr(em, "_get_json", _fake_fetch(_all_payload))
    rep = em.backfill(
        [DAY], engine=db.engine, now=NOW, dry_run=False,
        daily_ref=None, require_verified=False,
    )
    day = rep["days"][0]
    assert day["action"] == "written"
    assert day["reconciled"] is False
    assert day["verified"] is False
    assert "未提供日线参照" in day["reconcile_error"]
    assert len(_rows(db.engine)) == len(CODES) * 240


def test_window_copy_matches_real_endpoint_window():
    """窗口文案不能再暗示「5 个交易日都能补」：端点含当日，已收盘实际只到 4 个。"""
    note = em.calibration_note()
    window = note["window"]
    assert "含当日" in window
    assert "4" in window
    assert str(em.MAX_NDAYS) in window


# ── 7. 无锁语义的后端：安全禁用写入 ─────────────────────────────────────

def test_unknown_dialect_disables_writes():
    fake = SimpleNamespace(dialect=SimpleNamespace(name="mysql"))
    with pytest.raises(RuntimeError, match="不支持"):
        em.lock_day(fake, DAY)
