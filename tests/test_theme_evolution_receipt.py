"""上游运行回执(上游是否跑过/跑成)原型: **契约测试**(TDD 先红后绿)。

被测模块: `src/core/theme_evolution_receipt.py`

模块只做一件事: 把调用方**显式声明**的一次上游运行(started/finished UTC 时间、
目标日、状态、来源/依据/收盘完成/覆盖)原样、只追加、可区分未知地记下来, 并给出
**只读 readiness**。它不联网、不读时钟、不调度、不建生产库, 也**不判断声明真伪**。

硬约束(每条都有对应断言, 违反即 bug):

1. **显式 bind**: 只接受 Engine / Connection; Session / None / URL 字符串一律拒绝,
   且拒绝时一行都不写。模块不构造引擎、不建生产库。
2. **显式时点**: started_at 必须是 UTC aware(naive / +08:00 都拒绝); 模块内部没有
   `datetime.now()` / `time.time()` / sleep。
3. **只记声明, 不验真伪**: 状态与收盘完成/覆盖都是调用方声明; readiness 状态叫
   `complete_declared`, 并永久携带 `claims_verified=False`。
4. **未知保留**: 缺 finished_at / close_complete / coverage / source_data_day 时
   落库为 NULL、读回仍是 None, 绝不填 False / 当日 / "full"。
5. **只读 readiness 区分**: not_run / failed / refused / wrong_day / time_unknown /
   completion_unknown / incomplete / complete_declared。
6. **15:00 不是收盘信号**: 同日 15:30 完成且缺 close_complete 声明 ⇒
   `completion_unknown`, 不得判 complete。
7. **不拿旧行冒充**: 当天没有回执 ⇒ not_run(即使前一天有 complete_declared);
   当天最新一次失败 ⇒ failed(不倒回去拿更早的 complete_declared)。
8. **异步错序**: 回执可能乱序到达; 顺序基准是追加序号(attempt_seq), 不是墙钟;
   完成时刻乱序时 readiness 显式标记 `finished_out_of_order`。
9. **拒绝敏感异常内容**: BaseException 对象、含 token/password/bearer/DSN 凭据的
   文本一律拒绝, 且错误消息里不回显原文; 拒绝时 run 与声明行都不落库。
10. **独立 MetaData**: 表名统一 `theme_upstream_receipt_` 前缀, 不碰生产迁移。

本文件只使用注入的内存 SQLite(`sqlite://`), 不碰 `data/sida.db`、不读连接串。
"""

from __future__ import annotations

import ast
import datetime as dt
import itertools
import subprocess
import sys
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, func, inspect as sa_inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from src.core import theme_evolution_receipt as rc

CST = ZoneInfo("Asia/Shanghai")
DAY = "20261116"
PREV_DAY = "20261113"

# 09:30 CST / 15:30 CST(同一天, 已过 15:00 —— 但时间本身不是收盘证据)
UTC_START = dt.datetime(2026, 11, 16, 1, 30, tzinfo=dt.timezone.utc)
UTC_FINISH = dt.datetime(2026, 11, 16, 7, 30, tzinfo=dt.timezone.utc)
SECRET = "SECRET1234567"

_RUN_KEYS = itertools.count(1)


def make_receipt(**over) -> rc.UpstreamReceipt:
    base: dict = dict(
        day=DAY,
        run_key=f"up-{next(_RUN_KEYS)}",
        status=rc.STATUS_OK,
        source="kline_gateway",
        started_at=UTC_START,
        finished_at=UTC_FINISH,
        source_data_day=DAY,
        date_basis="source_reported_trade_date",
        close_complete=True,
        coverage="complete",
        declarations=(rc.ReceiptDeclaration("upstream", "sync_phase synced=True"),),
    )
    base.update(over)
    return rc.UpstreamReceipt(**base)


@pytest.fixture()
def engine() -> Engine:
    """完全隔离的内存 SQLite: 每个用例独立, 用完销毁。"""
    eng = create_engine("sqlite://")
    rc.create_schema(eng)
    try:
        yield eng
    finally:
        eng.dispose()


def _run_rows(engine: Engine) -> list[dict]:
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(
            select(rc.THEME_UPSTREAM_RECEIPT_RUN).order_by(
                rc.THEME_UPSTREAM_RECEIPT_RUN.c.attempt_seq)
        ).mappings()]


def _decl_rows(engine: Engine) -> list[dict]:
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(
            select(rc.THEME_UPSTREAM_RECEIPT_DECL).order_by(
                rc.THEME_UPSTREAM_RECEIPT_DECL.c.run_id,
                rc.THEME_UPSTREAM_RECEIPT_DECL.c.seq)
        ).mappings()]


def _count(engine: Engine, table) -> int:
    with engine.connect() as conn:
        return int(conn.execute(select(func.count()).select_from(table)).scalar_one())


def _assert_nothing_written(engine: Engine) -> None:
    assert _count(engine, rc.THEME_UPSTREAM_RECEIPT_RUN) == 0
    assert _count(engine, rc.THEME_UPSTREAM_RECEIPT_DECL) == 0


# ────────────────────────────────────────────────────────────────────────
# 1. 纯性 / 显式 bind / 独立 metadata
# ────────────────────────────────────────────────────────────────────────
class TestPurityAndBinding:
    def test_create_schema_rejects_non_bind(self, engine):
        for bad in (None, "sqlite://", object(), Session(engine)):
            with pytest.raises(rc.ReceiptError):
                rc.create_schema(bad)  # type: ignore[arg-type]

    def test_append_rejects_non_bind_without_writing(self, engine):
        for bad in (None, "sqlite://", object(), Session(engine)):
            with pytest.raises(rc.ReceiptBindError):
                rc.append_receipt(bad, make_receipt())  # type: ignore[arg-type]
        _assert_nothing_written(engine)

    def test_read_rejects_non_bind(self):
        for bad in (None, "sqlite://", object()):
            with pytest.raises(rc.ReceiptBindError):
                rc.read_readiness(bad, DAY)  # type: ignore[arg-type]

    def test_append_refuses_connection_with_open_transaction(self, engine):
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
            with pytest.raises(rc.ReceiptError):
                rc.append_receipt(conn, make_receipt())
            conn.rollback()
        _assert_nothing_written(engine)

    def test_schema_is_independent_metadata_only(self, engine):
        names = set(rc.THEME_UPSTREAM_RECEIPT_METADATA.tables)
        assert names == {
            "theme_upstream_receipt_run",
            "theme_upstream_receipt_decl",
        }
        assert all(n.startswith("theme_upstream_receipt_") for n in names)
        assert "theme_board_daily" not in names
        with engine.connect() as conn:
            created = set(sa_inspect(conn).get_table_names())
        assert created == names

    def test_append_does_not_auto_create_schema(self):
        fresh = create_engine("sqlite://")
        try:
            with pytest.raises(OperationalError):
                rc.append_receipt(fresh, make_receipt())
            assert sa_inspect(fresh).get_table_names() == []
        finally:
            fresh.dispose()

    def test_module_imports_no_network_or_product_modules(self):
        # 先 import sqlalchemy 做基线(socket 等由它自己带进来), 只检查本模块新增的。
        code = (
            "import sys, sqlalchemy;"
            "before=set(sys.modules);"
            "import src.core.theme_evolution_receipt;"
            "added=set(sys.modules)-before;"
            "bad=[m for m in added if m.startswith("
            "('requests','httpx','aiohttp','urllib3','marketdata','socket'))"
            " or m in ('src.web.migrations','src.bootstrap.startup',"
            "'src.core.theme_evolution','src.core.market_phase')];"
            "print(','.join(bad))"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
            cwd=str(__import__("pathlib").Path(__file__).resolve().parent.parent),
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "", f"import 拉进了: {proc.stdout.strip()}"

    def test_module_source_has_no_clock_or_network_calls(self):
        tree = ast.parse(__import__("inspect").getsource(rc))
        calls = {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
        banned = {
            "datetime.now", "datetime.utcnow", "datetime.today", "time.time",
            "time.monotonic", "time.sleep", "asyncio.sleep", "requests.get",
            "requests.post", "httpx.get", "httpx.post", "urlopen", "socket.socket",
        }
        assert calls & banned == set()

    def test_public_api_has_no_now_clock_or_scheduler_argument(self):
        for fn in (rc.append_receipt, rc.read_readiness, rc.receipts_for_day):
            params = set(__import__("inspect").signature(fn).parameters)
            assert params & {"now", "clock", "scheduler", "sleep"} == set()
        assert set(__import__("inspect").signature(rc.read_readiness).parameters) == {
            "bind", "day",
        }

    def test_module_never_claims_to_verify_caller_declarations(self):
        assert rc.CLAIMS_VERIFIED is False
        banned = ("verify", "confirm", "attest", "market_closed", "is_closed", "truth")
        offenders = [
            name for name in dir(rc)
            if callable(getattr(rc, name)) and not name.startswith("_")
            and any(word in name.lower() for word in banned)
        ]
        assert offenders == []


# ────────────────────────────────────────────────────────────────────────
# 2. 三种状态都真实落库
# ────────────────────────────────────────────────────────────────────────
class TestRecordStatuses:
    def test_ok_receipt_round_trips_every_declaration(self, engine):
        receipt = make_receipt(
            declarations=(
                rc.ReceiptDeclaration("upstream", "sync_phase synced=True"),
                rc.ReceiptDeclaration("coverage", "concept boards 60/60"),
            )
        )
        out = rc.append_receipt(engine, receipt)
        assert out.inserted is True
        assert out.reason is None
        assert (out.day, out.run_key, out.attempt_seq, out.status) == (
            DAY, receipt.run_key, 1, rc.STATUS_OK)
        assert out.declarations_written == 2
        assert out.claims_verified is False
        assert out.run_id is not None

        rows = _run_rows(engine)
        assert len(rows) == 1
        row = rows[0]
        assert row["status"] == rc.STATUS_OK
        assert row["source"] == "kline_gateway"
        assert row["source_data_day"] == DAY
        assert row["date_basis"] == "source_reported_trade_date"
        assert row["close_complete"] is True
        assert row["coverage"] == "complete"
        assert rc.parse_utc(row["started_at_utc"]) == UTC_START
        assert rc.parse_utc(row["finished_at_utc"]) == UTC_FINISH

        decls = rc.declarations_for_run(engine, out.run_id)
        assert [d["seq"] for d in decls] == [1, 2]
        assert [d["value"] for d in decls] == [
            "sync_phase synced=True", "concept boards 60/60"]

    def test_refused_receipt_is_recorded_and_read_as_refused(self, engine):
        out = rc.append_receipt(engine, make_receipt(
            status=rc.STATUS_REFUSED, reason="source_not_fresh",
            close_complete=None, coverage=None))
        assert out.inserted is True
        assert _run_rows(engine)[0]["status"] == rc.STATUS_REFUSED
        ready = rc.read_readiness(engine, DAY)
        assert ready.state == rc.READINESS_REFUSED
        assert ready.reason == "source_not_fresh"

    def test_failed_receipt_is_recorded_and_read_as_failed(self, engine):
        out = rc.append_receipt(engine, make_receipt(
            status=rc.STATUS_FAILED, reason="upstream_timeout",
            detail="gateway read timeout after 30s", close_complete=None, coverage=None))
        assert out.inserted is True
        assert _run_rows(engine)[0]["detail"] == "gateway read timeout after 30s"
        ready = rc.read_readiness(engine, DAY)
        assert ready.state == rc.READINESS_FAILED
        assert ready.reason == "upstream_timeout"
        assert ready.claims_verified is False

    @pytest.mark.parametrize("status", ["complete", "success", "", None, 1])
    def test_unknown_status_is_rejected(self, engine, status):
        with pytest.raises(rc.ReceiptValidationError):
            rc.append_receipt(engine, make_receipt(status=status))
        _assert_nothing_written(engine)

    def test_refused_or_failed_requires_reason(self, engine):
        for status in (rc.STATUS_REFUSED, rc.STATUS_FAILED):
            with pytest.raises(rc.ReceiptValidationError) as ei:
                rc.append_receipt(engine, make_receipt(status=status, reason=None))
            assert ei.value.reason == "reason_required"
        _assert_nothing_written(engine)


# ────────────────────────────────────────────────────────────────────────
# 3. 显式 UTC 时点 / 基本字段校验
# ────────────────────────────────────────────────────────────────────────
class TestExplicitUtcTime:
    def test_naive_timestamp_rejected(self, engine):
        with pytest.raises(rc.ReceiptValidationError) as ei:
            rc.append_receipt(engine, make_receipt(
                started_at=dt.datetime(2026, 11, 16, 9, 30)))
        assert ei.value.reason == "naive_timestamp"
        _assert_nothing_written(engine)

    def test_non_utc_offset_rejected(self, engine):
        cst_start = dt.datetime(2026, 11, 16, 9, 30, tzinfo=CST)
        with pytest.raises(rc.ReceiptValidationError) as ei:
            rc.append_receipt(engine, make_receipt(started_at=cst_start))
        assert ei.value.reason == "not_utc"
        _assert_nothing_written(engine)

    def test_zulu_timezone_is_accepted(self, engine):
        out = rc.append_receipt(engine, make_receipt(
            started_at=dt.datetime(2026, 11, 16, 1, 30, tzinfo=dt.timezone.utc)))
        assert out.inserted is True
        assert _run_rows(engine)[0]["started_at_utc"].endswith("Z")

    def test_finished_before_started_rejected(self, engine):
        with pytest.raises(rc.ReceiptValidationError) as ei:
            rc.append_receipt(engine, make_receipt(
                finished_at=dt.datetime(2026, 11, 16, 1, 0, tzinfo=dt.timezone.utc)))
        assert ei.value.reason == "finished_before_started"
        _assert_nothing_written(engine)

    @pytest.mark.parametrize("day", ["2026-11-16", "20261332", "", None, 20261116])
    def test_bad_day_rejected(self, engine, day):
        with pytest.raises(rc.ReceiptValidationError) as ei:
            rc.append_receipt(engine, make_receipt(day=day))
        assert ei.value.reason == "bad_day"
        _assert_nothing_written(engine)

    def test_close_complete_only_bool_or_none(self, engine):
        for bad in ("yes", 1, 0, "true"):
            with pytest.raises(rc.ReceiptValidationError) as ei:
                rc.append_receipt(engine, make_receipt(close_complete=bad))
            assert ei.value.reason == "bad_close_complete"
        _assert_nothing_written(engine)

    def test_bad_run_key_rejected(self, engine):
        for bad in ("", "has space", "x" * 65, "http://x"):
            with pytest.raises(rc.ReceiptValidationError) as ei:
                rc.append_receipt(engine, make_receipt(run_key=bad))
            assert ei.value.reason == "bad_run_key"
        _assert_nothing_written(engine)


# ────────────────────────────────────────────────────────────────────────
# 4. 敏感异常内容: 拒绝且不回显
# ────────────────────────────────────────────────────────────────────────
class TestSensitiveContent:
    def test_exception_object_is_not_accepted_as_detail(self, engine):
        exc = RuntimeError(f"GET https://gw/x?token={SECRET}")
        with pytest.raises(rc.ReceiptValidationError) as ei:
            rc.append_receipt(engine, make_receipt(
                status=rc.STATUS_FAILED, reason="upstream_error", detail=exc))
        assert ei.value.reason == "exception_object"
        assert SECRET not in str(ei.value)
        _assert_nothing_written(engine)

    @pytest.mark.parametrize("detail", [
        f"token={SECRET}",
        "password=hunter2",
        f"Authorization: Bearer {SECRET}",
        "postgresql://app:hunter2@db:5432/sida",
        'api_key="abcdef12345678"',
        "密钥: abcd1234",
    ])
    def test_secret_bearing_text_is_refused(self, engine, detail):
        with pytest.raises(rc.ReceiptSensitiveContentError) as ei:
            rc.append_receipt(engine, make_receipt(
                status=rc.STATUS_FAILED, reason="upstream_error", detail=detail))
        assert ei.value.reason == "sensitive_content"
        assert "hunter2" not in str(ei.value)
        assert SECRET not in str(ei.value)
        _assert_nothing_written(engine)

    def test_secret_in_declaration_is_refused(self, engine):
        receipt = make_receipt(declarations=(
            rc.ReceiptDeclaration("probe", f"token={SECRET}"),))
        with pytest.raises(rc.ReceiptSensitiveContentError):
            rc.append_receipt(engine, receipt)
        _assert_nothing_written(engine)

    def test_plain_failure_text_is_accepted(self, engine):
        out = rc.append_receipt(engine, make_receipt(
            status=rc.STATUS_FAILED, reason="upstream_timeout",
            detail="upstream returned HTTP 500 after 30s timeout"))
        assert out.inserted is True


# ────────────────────────────────────────────────────────────────────────
# 5. 幂等 / 冲突 / 失败回滚
# ────────────────────────────────────────────────────────────────────────
class TestIdempotencyAndAtomicity:
    def test_same_run_key_same_payload_is_idempotent(self, engine):
        receipt = make_receipt(run_key="up-fixed")
        first = rc.append_receipt(engine, receipt)
        second = rc.append_receipt(engine, receipt)
        assert first.inserted is True
        assert second.inserted is False
        assert second.reason == "duplicate_run_key"
        assert second.run_id == first.run_id
        assert second.status == rc.STATUS_OK
        assert len(_run_rows(engine)) == 1
        assert len(_decl_rows(engine)) == 1

    def test_same_run_key_different_payload_is_conflict(self, engine):
        rc.append_receipt(engine, make_receipt(run_key="up-fixed"))
        with pytest.raises(rc.ReceiptConflictError):
            rc.append_receipt(engine, make_receipt(
                run_key="up-fixed", status=rc.STATUS_FAILED,
                reason="upstream_timeout", detail="boom"))
        rows = _run_rows(engine)
        assert len(rows) == 1
        assert rows[0]["status"] == rc.STATUS_OK
        assert len(_decl_rows(engine)) == 1

    def test_failure_after_run_insert_rolls_back_everything(self, engine, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("injected mid-write failure")

        monkeypatch.setattr(rc, "_insert_declarations", boom)
        with pytest.raises(RuntimeError):
            rc.append_receipt(engine, make_receipt())
        _assert_nothing_written(engine)

    def test_attempt_seq_unique_conflict_rolls_back(self, engine, monkeypatch):
        rc.append_receipt(engine, make_receipt(run_key="up-first"))
        monkeypatch.setattr(rc, "_next_attempt_seq", lambda conn, day: 1)
        with pytest.raises(rc.ReceiptConflictError):
            rc.append_receipt(engine, make_receipt(run_key="up-second"))
        assert len(_run_rows(engine)) == 1
        assert len(_decl_rows(engine)) == 1


# ────────────────────────────────────────────────────────────────────────
# 6. readiness: 未运行 / 失败不倒退 / 错日 / 时间未知 / 完成未知
# ────────────────────────────────────────────────────────────────────────
class TestReadinessStates:
    def test_no_receipt_for_day_is_not_run_and_never_borrows_old_rows(self, engine):
        rc.append_receipt(engine, make_receipt(day=PREV_DAY, run_key="up-prev"))
        ready = rc.read_readiness(engine, DAY)
        assert ready.state == rc.READINESS_NOT_RUN
        assert ready.reason == "no_receipt_for_day"
        assert ready.run_key is None
        assert ready.attempt_count == 0
        assert ready.claims_verified is False
        assert "no_fallback_to_other_days" in ready.notes

    def test_latest_failure_does_not_fall_back_to_earlier_success(self, engine):
        rc.append_receipt(engine, make_receipt(run_key="up-ok"))
        rc.append_receipt(engine, make_receipt(
            run_key="up-failed", status=rc.STATUS_FAILED,
            reason="upstream_timeout", detail="boom", close_complete=None))
        ready = rc.read_readiness(engine, DAY)
        assert ready.state == rc.READINESS_FAILED
        assert ready.run_key == "up-failed"
        assert ready.attempt_seq == 2

    def test_latest_refusal_does_not_fall_back(self, engine):
        rc.append_receipt(engine, make_receipt(run_key="up-ok"))
        rc.append_receipt(engine, make_receipt(
            run_key="up-refused", status=rc.STATUS_REFUSED,
            reason="source_not_fresh", close_complete=None))
        ready = rc.read_readiness(engine, DAY)
        assert ready.state == rc.READINESS_REFUSED
        assert ready.reason == "source_not_fresh"

    def test_declared_source_day_mismatch_is_wrong_day(self, engine):
        rc.append_receipt(engine, make_receipt(source_data_day=PREV_DAY))
        ready = rc.read_readiness(engine, DAY)
        assert ready.state == rc.READINESS_WRONG_DAY
        assert ready.reason == "source_day_mismatch"
        assert ready.source_data_day == PREV_DAY
        assert ready.claims_verified is False

    def test_missing_finished_at_is_time_unknown_even_after_15_cst(self, engine):
        rc.append_receipt(engine, make_receipt(finished_at=None))
        ready = rc.read_readiness(engine, DAY)
        assert ready.state == rc.READINESS_TIME_UNKNOWN
        assert ready.reason == "finished_at_unknown"
        assert ready.finished_at is None

    def test_unreadable_stored_timestamp_is_time_unknown(self, engine):
        out = rc.append_receipt(engine, make_receipt())
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "UPDATE theme_upstream_receipt_run SET finished_at_utc='not-a-time' "
                "WHERE id=?", (out.run_id,))
        ready = rc.read_readiness(engine, DAY)
        assert ready.state == rc.READINESS_TIME_UNKNOWN
        assert ready.reason == "timestamp_unreadable"

    def test_1530_same_day_without_close_declaration_is_still_unknown(self, engine):
        """15:30 CST 完成 ≠ 收盘定稿: 没有明示 close_complete 就是 unknown。"""
        rc.append_receipt(engine, make_receipt(
            finished_at=dt.datetime(2026, 11, 16, 7, 30, tzinfo=dt.timezone.utc),
            close_complete=None))
        ready = rc.read_readiness(engine, DAY)
        assert ready.state == rc.READINESS_COMPLETION_UNKNOWN
        assert ready.reason == "close_complete_undeclared"
        assert ready.state != rc.READINESS_COMPLETE_DECLARED
        assert ready.declared_close_complete is None

    def test_complete_without_coverage_declaration_is_unknown(self, engine):
        rc.append_receipt(engine, make_receipt(coverage=None))
        ready = rc.read_readiness(engine, DAY)
        assert ready.state == rc.READINESS_COMPLETION_UNKNOWN
        assert ready.reason == "coverage_undeclared"

    def test_missing_source_day_is_completion_unknown_not_wrong_day(self, engine):
        rc.append_receipt(engine, make_receipt(source_data_day=None))
        ready = rc.read_readiness(engine, DAY)
        assert ready.state == rc.READINESS_COMPLETION_UNKNOWN
        assert ready.reason == "source_day_undeclared"
        assert ready.source_data_day is None

    def test_missing_date_basis_is_completion_unknown(self, engine):
        rc.append_receipt(engine, make_receipt(date_basis=None))
        ready = rc.read_readiness(engine, DAY)
        assert ready.state == rc.READINESS_COMPLETION_UNKNOWN
        assert ready.reason == "date_basis_undeclared"

    def test_explicit_not_closed_is_incomplete_not_unknown(self, engine):
        rc.append_receipt(engine, make_receipt(close_complete=False))
        ready = rc.read_readiness(engine, DAY)
        assert ready.state == rc.READINESS_INCOMPLETE
        assert ready.declared_close_complete is False

    def test_all_declarations_present_is_complete_declared_only(self, engine):
        rc.append_receipt(engine, make_receipt())
        ready = rc.read_readiness(engine, DAY)
        assert ready.state == rc.READINESS_COMPLETE_DECLARED
        assert ready.reason is None
        assert ready.claims_verified is False
        assert ready.order_basis == "append_sequence"
        assert "caller_declarations_not_verified" in ready.notes

    def test_unknown_values_are_preserved_as_null(self, engine):
        rc.append_receipt(engine, make_receipt(
            source_data_day=None, date_basis=None,
            close_complete=None, coverage=None, finished_at=None))
        row = _run_rows(engine)[0]
        assert row["source_data_day"] is None
        assert row["date_basis"] is None
        assert row["close_complete"] is None
        assert row["coverage"] is None
        assert row["finished_at_utc"] is None
        ready = rc.read_readiness(engine, DAY)
        assert ready.declared_close_complete is None
        assert ready.declared_coverage is None
        assert ready.finished_at is None

    def test_readiness_is_read_only(self, engine):
        rc.append_receipt(engine, make_receipt())
        before_runs = _run_rows(engine)
        before_decls = _decl_rows(engine)
        rc.read_readiness(engine, DAY)
        with engine.connect() as conn:
            rc.read_readiness(conn, DAY)
        assert _run_rows(engine) == before_runs
        assert _decl_rows(engine) == before_decls

    def test_async_out_of_order_uses_append_sequence(self, engine):
        """后追加的回执完成得更早: 仍按追加序号取最新一次, 并标记乱序。"""
        rc.append_receipt(engine, make_receipt(
            run_key="up-late-finish", finished_at=dt.datetime(
                2026, 11, 16, 7, 40, tzinfo=dt.timezone.utc)))
        rc.append_receipt(engine, make_receipt(
            run_key="up-early-finish", finished_at=dt.datetime(
                2026, 11, 16, 7, 20, tzinfo=dt.timezone.utc)))
        ready = rc.read_readiness(engine, DAY)
        assert ready.attempt_seq == 2
        assert ready.run_key == "up-early-finish"
        assert ready.state == rc.READINESS_COMPLETION_UNKNOWN
        assert ready.finished_out_of_order is True
        assert ready.order_basis == "append_sequence"
        assert [r["run_key"] for r in rc.receipts_for_day(engine, DAY)] == [
            "up-late-finish", "up-early-finish"]

    def test_no_order_anomaly_when_finishes_are_monotonic(self, engine):
        rc.append_receipt(engine, make_receipt(
            run_key="up-a", finished_at=dt.datetime(
                2026, 11, 16, 7, 10, tzinfo=dt.timezone.utc)))
        rc.append_receipt(engine, make_receipt(
            run_key="up-b", finished_at=dt.datetime(
                2026, 11, 16, 7, 20, tzinfo=dt.timezone.utc)))
        assert rc.read_readiness(engine, DAY).finished_out_of_order is False
