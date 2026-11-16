"""题材演化 agent 单次运行编排契约(TDD: 先红后绿)。

被测模块: `src/core/theme_evolution_agent.py`

这一层是**产品内定时 agent 的一次运行**: 它把已有三块拼成一次可追责的运行, 自己不
抓数、不建库、不出交易结论:

1. 用调用方注入的取数依赖调用 `capture_day` —— 落库的是**同一个批次**;
2. 按该批次的 `run_id` **读回**并逐项核对(行数/日期/状态/归属), 不拿别的批次凑证据;
3. 把调用方显式注入的标签事件与 K 线交给 timeline; 没注入就是 `unknown`, 绝不编造。

硬约束(违反即视为 bug, 每条都有断言):

1. **默认不联网**: 不注入取数函数 ⇒ 一次外部调用都不发; import 本模块不得拉进行情/
   HTTP 客户端。
2. **不自建库**: 只接受调用方显式给的 Engine/Connection; 模块里没有建引擎/读配置/
   建表的路径; 不碰注册与迁移模块。
3. **拒绝 ≠ 成功**: 采集拒绝(依赖缺失/源不新鲜/目标日错配)与采集异常一律
   `ok is False`, 且能说清为什么。
4. **只认一次 run**: 读回的证据里出现别的 run 的行、行数不符、缺行 ⇒ 整份证据作废
   (`evidence is None`), 状态 `failed`, 绝不把"部分行"当批次。
5. **未知就是 unknown**: 时间线输入缺失/非法 ⇒ `timeline is None` + 显式状态与原因;
   池子未知 ⇒ 计数 `None`(不是 0)。
6. **无交易语义**: 输出不含方向/收益/目标/仓位字段, 叙述里没有仓位买卖措辞。
7. **可追溯**: 输出带 `run_id` / 来源 / 日期 / 采集时刻 / 生成时刻, 并标明生成时刻
   是显式给的还是取自本次 `now`。

夹具全部是**合成数据**(代码前缀 `SYN`), 只建 `sqlite://` 内存库: 不读生产库、
不联网、不构造真实历史。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import inspect
import json
import pathlib
import subprocess
import sys
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine

from src.core import theme_evolution_agent as agent
from src.core import theme_evolution_capture as cap
from src.core import theme_evolution_store as store
from src.core import theme_evolution_timeline as tl

CST = ZoneInfo("Asia/Shanghai")
NOW = dt.datetime(2026, 11, 16, 15, 16, tzinfo=CST)  # 周一 15:16
DAY = "20261116"
PREV_DAY = "20261113"
SECRET = "token=SECRET1234567"

BOARDS = (
    {"code": "880001", "name": "半导体", "board_type": "concept"},
    {"code": "880002", "name": "光伏", "board_type": "concept"},
)


# ── 采集侧夹具(全内存 Fake, 零网络) ─────────────────────────────────────
def _pool_row(code: str = "000001", as_of: str | None = "2026-11-16",
              basis: str | None = "kline", name: str = "合成标的甲") -> dict:
    row: dict = {"code": code, "name": name, "pool_coverage": "complete"}
    if as_of is not None:
        row["as_of"] = as_of
    if basis is not None:
        row["as_of_basis"] = basis
    return row


def _members(codes=("000001", "000002"), data_day: str | None = DAY,
             basis: str | None = "source_reported") -> dict:
    return {"members": list(codes), "data_day": data_day, "date_basis": basis}


class Harness:
    """注入式取数依赖: 记录调用次数, 便于断言"拒绝时一次都没发"。"""

    def __init__(self, *, boards=BOARDS, members=None, pool=None,
                 fresh: bool | None = True, trade_date: str | Exception | None = DAY):
        self.boards = boards
        self.members = members if members is not None else {"*": _members()}
        self.pool = pool if pool is not None else [_pool_row()]
        self.fresh = fresh
        self.trade_date = trade_date
        self.board_calls = 0
        self.member_calls: list[str] = []
        self.pool_calls: list[str] = []

    def fetch_boards(self):
        self.board_calls += 1
        if isinstance(self.boards, BaseException):
            raise self.boards
        return self.boards

    def fetch_members(self, code: str):
        self.member_calls.append(code)
        result = self.members.get(code, self.members.get("*"))
        if isinstance(result, BaseException):
            raise result
        return result

    def fetch_pool(self, day: str):
        self.pool_calls.append(day)
        if isinstance(self.pool, BaseException):
            raise self.pool
        return self.pool

    def fetch_trade_date(self):
        if isinstance(self.trade_date, BaseException):
            raise self.trade_date
        return self.trade_date

    def kwargs(self, **over) -> dict:
        base = dict(
            now=NOW,
            day=DAY,
            fetch_boards=self.fetch_boards,
            fetch_members=self.fetch_members,
            fetch_pool=self.fetch_pool,
            fetch_trade_date=self.fetch_trade_date,
            source_fresh=self.fresh,
        )
        base.update(over)
        return base

    def run(self, bind, **over):
        return agent.run_once(bind=bind, **self.kwargs(**over))


# ── 时间线侧夹具(合成代码 + 真实引用形状, 不是真实历史) ────────────────
SHA = "0f" * 32


def _event(date: str, code: str, labels=("合成题材甲",), *, line: int = 1,
           source: str = "fixture-vendor") -> dict:
    return {"date": date, "code": code, "labels": list(labels), "source": source,
            "reference": {"file": f"fixture/{date}.json", "line": line, "sha256": SHA}}


def _bar(code: str, date: str, *, o=10.0, h=10.5, lo=9.8, c=10.2, v=1000) -> dict:
    return {"code": code, "date": date, "open": o, "high": h, "low": lo,
            "close": c, "volume": v}


EVENTS = (
    _event("20261012", "SYN001", ("合成题材甲", "合成题材乙"), line=3),
    _event("20261012", "SYN002", ("合成题材甲",), line=4),
    _event("20261013", "SYN001", ("合成题材甲",), line=5),
)

KLINES = (
    _bar("SYN001", "20261012"),
    _bar("SYN001", "20261013"),
    _bar("SYN002", "20261012"),
)


@pytest.fixture()
def engine() -> Engine:
    """完全隔离的内存 SQLite: 每个用例独立, 用完销毁。"""
    eng = create_engine("sqlite://")
    store.create_schema(eng)
    try:
        yield eng
    finally:
        eng.dispose()


def _runs(engine: Engine) -> list[dict]:
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(
            select(store.THEME_OBS_RUN).order_by(store.THEME_OBS_RUN.c.attempt_seq)
        ).mappings()]


def _count(engine: Engine, table) -> int:
    with engine.connect() as conn:
        return int(conn.execute(select(func.count()).select_from(table)).scalar_one())


# ────────────────────────────────────────────────────────────────────────
# 1. 契约: 纯函数式编排、显式时间、不联网、不自建库、不给建议
# ────────────────────────────────────────────────────────────────────────
class TestContract:
    def test_run_once_signature(self):
        params = inspect.signature(agent.run_once).parameters
        assert params["now"].default is inspect.Parameter.empty  # 时间必须显式
        assert params["bind"].default is None                   # 库必须显式, 缺了就拒绝
        assert "events" in params and "klines" in params

    def test_now_is_required(self):
        with pytest.raises(TypeError):
            agent.run_once(bind=None)  # type: ignore[call-arg]

    def test_missing_bind_is_structured_failure_and_never_calls_capture(self):
        calls: list[int] = []

        def capture(*a, **kw):
            calls.append(1)
            raise AssertionError("不应在没有 bind 时调用采集")

        out = agent.run_once(now=NOW, bind=None, capture=capture)
        assert out.outcome == agent.OUTCOME_FAILED
        assert out.ok is False
        assert out.run_id is None
        assert agent.REASON_BIND_MISSING in out.reason
        assert calls == []

    def test_bad_day_raises_instead_of_silently_running(self):
        with pytest.raises(ValueError):
            agent.run_once(bind=None, **Harness().kwargs(day="2026-11-16"))
        with pytest.raises(ValueError):
            agent.run_once(bind=None, **Harness().kwargs(day="20261332"))

    def test_now_must_be_datetime(self):
        with pytest.raises(ValueError):
            agent.run_once(bind=None, **Harness().kwargs(now="2026-11-16 15:16"))

    def test_module_never_builds_or_discovers_a_database(self):
        """只接受显式 Engine/Connection: 模块里没有建引擎/读配置/建表的路径。"""
        source = inspect.getsource(agent)
        for token in ("create_engine", "DATA_DIR", "SIDA_DB_URL", "SessionLocal",
                      "create_schema", "migrations", "startup"):
            assert token not in source, f"模块源码不应出现 {token}"

    def test_module_does_not_touch_registration_or_migration_modules(self):
        source = inspect.getsource(agent)
        assert "src.web.migrations" not in source
        assert "src.bootstrap" not in source
        assert "importlib" not in source

    def test_import_pulls_no_network_module(self):
        code = (
            "import sys;"
            "import src.core.theme_evolution_agent;"
            "bad=[m for m in sys.modules if m.startswith(('marketdata','requests','httpx','aiohttp'))"
            " or m in ('src.core.tdx_boards','src.collectors.market_sentiment_collector',"
            "'src.collectors.wudao_mcp_client')];"
            "print(','.join(bad))"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
            cwd=str(pathlib.Path(__file__).resolve().parent.parent),
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "", f"import 拉进了这些模块: {proc.stdout.strip()}"

    def test_module_has_no_advice_or_report_builder(self):
        banned = ("advice", "suggest", "recommend", "signal", "rank", "target_price")
        offenders = [
            name for name, obj in vars(agent).items()
            if callable(obj) and not name.startswith("_")
            and any(word in name.lower() for word in banned)
        ]
        assert offenders == []

    def test_engine_fixture_is_isolated_memory_sqlite(self, engine):
        assert str(engine.url).startswith("sqlite")


# ────────────────────────────────────────────────────────────────────────
# 2. 正常路径: 同一批次落库 + 按 run_id 读回核对 + 可追溯头部
# ────────────────────────────────────────────────────────────────────────
class TestHappyPath:
    def test_complete_run_is_verified_and_traceable(self, engine):
        out = Harness().run(engine)

        assert out.outcome == agent.OUTCOME_COMPLETE
        assert out.ok is True
        assert out.verified is True
        assert out.verification_failures == ()
        assert isinstance(out.run_id, int) and out.run_id > 0
        assert out.attempt_seq == 1
        assert out.inserted is True and out.replayed is False

        # 头部: run_id / 来源 / 日期 / 采集时刻 / 生成时刻都在
        assert out.day == DAY and out.run_key == cap.run_key_for(DAY)
        assert out.source == cap.SOURCE
        assert out.rule_version == cap.RULE_VERSION
        assert out.captured_at == dt.datetime(2026, 11, 16, 15, 16)
        assert out.generated_at is None
        assert out.generated_at_source == "unknown"

        # 读回的是**这一次** run 的证据
        assert out.evidence is not None
        assert out.evidence["run"]["run_id"] == out.run_id
        assert out.evidence["run"]["run_key"] == out.run_key
        assert len(out.evidence["boards"]) == 2
        assert len(out.evidence["limit_up"]) == 1
        assert {r["run_id"] for r in out.evidence["boards"]} == {out.run_id}
        assert {r["run_id"] for r in out.evidence["limit_up"]} == {out.run_id}

        # 库里也只有这一批
        runs = _runs(engine)
        assert len(runs) == 1 and runs[0]["id"] == out.run_id
        assert _count(engine, store.THEME_OBS_BOARD) == 2
        assert _count(engine, store.THEME_OBS_LIMITUP) == 1

    def test_payload_carries_documented_header_fields(self, engine):
        payload = Harness().run(engine).to_dict()
        assert payload["schema_version"] == agent.AGENT_RULE_VERSION
        assert set(payload) >= {
            "outcome", "ok", "run_id", "attempt_seq", "inserted", "replayed",
            "day", "run_key", "slot", "source", "rule_version", "captured_at",
            "generated_at", "generated_at_source", "timezone", "batch_status",
            "verified", "verification", "evidence", "timeline", "unknowns", "notes",
            "error", "reason", "contains_trading_advice", "limitations",
        }
        assert payload["timezone"] == "Asia/Shanghai"
        assert payload["contains_trading_advice"] is False
        assert payload["evidence"] == {"boards": 2, "limit_up": 1,
                                       "pool_state": cap.POOL_OK, "pool_count": 1}
        assert payload["limitations"]

    def test_explicit_generated_at_is_honoured_and_labelled(self, engine):
        later = NOW + dt.timedelta(minutes=5)
        out = Harness().run(engine, generated_at=later)
        assert out.generated_at == dt.datetime(2026, 11, 16, 15, 21)
        assert out.generated_at_source == agent.GENERATED_AT_EXPLICIT
        assert out.generated_at != out.captured_at

    def test_generated_at_must_be_datetime(self, engine):
        with pytest.raises(ValueError):
            Harness().run(engine, generated_at="2026-11-16T15:20:00")

    def test_naive_now_is_beijing_wall_clock(self, engine):
        naive = dt.datetime(2026, 11, 16, 15, 16)
        out = Harness().run(engine, now=naive)
        assert out.day == DAY
        assert out.captured_at == naive and out.generated_at is None

    def test_utc_now_converts_to_beijing_day(self, engine):
        utc_now = dt.datetime(2026, 11, 15, 17, 30, tzinfo=dt.timezone.utc)
        out = Harness().run(engine, now=utc_now)
        assert out.day == DAY
        assert out.captured_at == dt.datetime(2026, 11, 16, 1, 30)

    def test_connection_bind_is_supported(self, engine):
        """bind 是 Connection 也能跑(仍是调用方显式给的连接)。"""
        with engine.connect() as conn:
            out = agent.run_once(bind=conn, **Harness().kwargs())
        assert out.outcome == agent.OUTCOME_COMPLETE
        assert out.ok is True and out.verified is True
        assert isinstance(out.run_id, int)


# ────────────────────────────────────────────────────────────────────────
# 3. 读回核对: 只认本批次, 缺行/串批次/查无此 run 一律作废
# ────────────────────────────────────────────────────────────────────────
class TestReadbackVerification:
    def test_reader_sees_our_run_id(self, engine):
        seen: list[int] = []

        def reader(bind, run_id):
            seen.append(run_id)
            with engine.connect() as conn:
                return store.evidence_for_run(conn, run_id)

        out = Harness().run(engine, reader=reader)
        assert seen == [out.run_id]
        assert out.verified is True

    def test_reader_returning_none_fails_without_claiming_success(self, engine):
        out = Harness().run(engine, reader=lambda bind, run_id: None)
        assert out.outcome == agent.OUTCOME_FAILED
        assert out.ok is False
        assert out.verified is False
        assert out.evidence is None
        assert agent.VERIFY_RUN_NOT_FOUND in out.verification_failures

    def test_foreign_run_rows_are_rejected_not_stitched(self, engine):
        """读回的行属于别的 run ⇒ 整份证据作废(不跨批次拼证据)。"""
        first = Harness().run(engine, slot="daily")
        assert first.run_id is not None
        other_id = first.run_id

        def reader(bind, run_id):
            with engine.connect() as conn:
                return store.evidence_for_run(conn, other_id)  # 故意返回别的批次

        second = Harness().run(engine, slot="recover1", reader=reader)
        assert second.run_id != other_id
        assert second.outcome == agent.OUTCOME_FAILED
        assert second.ok is False
        assert second.evidence is None
        assert agent.VERIFY_RUN_ID_MISMATCH in second.verification_failures

    def test_subset_of_rows_is_rejected(self, engine):
        def reader(bind, run_id):
            with engine.connect() as conn:
                full = store.evidence_for_run(conn, run_id)
            full["boards"] = full["boards"][:1]  # 只留一行
            return full

        out = Harness().run(engine, reader=reader)
        assert out.outcome == agent.OUTCOME_FAILED
        assert out.ok is False
        assert out.evidence is None
        assert agent.VERIFY_BOARDS_COUNT_MISMATCH in out.verification_failures

    def test_missing_limit_up_rows_is_rejected(self, engine):
        def reader(bind, run_id):
            with engine.connect() as conn:
                full = store.evidence_for_run(conn, run_id)
            full["limit_up"] = []
            return full

        out = Harness().run(engine, reader=reader)
        assert out.outcome == agent.OUTCOME_FAILED
        assert agent.VERIFY_LIMIT_UP_COUNT_MISMATCH in out.verification_failures

    def test_status_mismatch_is_rejected(self, engine):
        def reader(bind, run_id):
            with engine.connect() as conn:
                full = store.evidence_for_run(conn, run_id)
            full["run"]["status"] = cap.STATUS_PARTIAL
            return full

        out = Harness().run(engine, reader=reader)
        assert out.outcome == agent.OUTCOME_FAILED
        assert agent.VERIFY_STATUS_MISMATCH in out.verification_failures

    def test_day_mismatch_is_rejected(self, engine):
        def reader(bind, run_id):
            with engine.connect() as conn:
                full = store.evidence_for_run(conn, run_id)
            full["run"]["day"] = PREV_DAY
            return full

        out = Harness().run(engine, reader=reader)
        assert out.outcome == agent.OUTCOME_FAILED
        assert agent.VERIFY_DAY_MISMATCH in out.verification_failures

    def test_captured_at_mismatch_is_rejected(self, engine):
        def reader(bind, run_id):
            with engine.connect() as conn:
                full = store.evidence_for_run(conn, run_id)
            full["run"]["captured_at"] = dt.datetime(2026, 11, 16, 14, 0)
            return full

        out = Harness().run(engine, reader=reader)
        assert out.outcome == agent.OUTCOME_FAILED
        assert agent.VERIFY_CAPTURED_AT_MISMATCH in out.verification_failures

    def test_source_flag_mismatch_is_rejected(self, engine):
        def reader(bind, run_id):
            with engine.connect() as conn:
                full = store.evidence_for_run(conn, run_id)
            full["run"]["source_fresh"] = False
            return full

        out = Harness().run(engine, reader=reader)
        assert out.outcome == agent.OUTCOME_FAILED
        assert agent.VERIFY_SOURCE_FLAG_MISMATCH in out.verification_failures

    def test_source_trade_date_mismatch_is_rejected(self, engine):
        def reader(bind, run_id):
            with engine.connect() as conn:
                full = store.evidence_for_run(conn, run_id)
            full["run"]["source_trade_date"] = PREV_DAY
            return full

        out = Harness().run(engine, reader=reader)
        assert out.outcome == agent.OUTCOME_FAILED
        assert agent.VERIFY_SOURCE_TRADE_DATE_MISMATCH in out.verification_failures

    def test_foreign_rows_inside_evidence_are_rejected(self, engine):
        def reader(bind, run_id):
            with engine.connect() as conn:
                full = store.evidence_for_run(conn, run_id)
            full["boards"][0]["run_id"] = run_id + 999
            return full

        out = Harness().run(engine, reader=reader)
        assert out.outcome == agent.OUTCOME_FAILED
        assert agent.VERIFY_FOREIGN_ROWS in out.verification_failures

    def test_refused_run_that_carries_evidence_rows_is_rejected(self, engine):
        def reader(bind, run_id):
            with engine.connect() as conn:
                full = store.evidence_for_run(conn, run_id)
            full["run"]["status"] = cap.STATUS_REFUSED
            full["boards"] = [{"run_id": run_id, "day": DAY, "board_code": "880001"}]
            return full

        out = Harness().run(engine, reader=reader)
        assert out.outcome == agent.OUTCOME_FAILED
        assert agent.VERIFY_REFUSED_WITH_EVIDENCE in out.verification_failures

    def test_reader_exception_is_a_failure_not_success(self, engine):
        def reader(bind, run_id):
            raise RuntimeError(f"db down?{SECRET}")

        out = Harness().run(engine, reader=reader)
        assert out.outcome == agent.OUTCOME_FAILED
        assert out.ok is False
        assert out.evidence is None
        assert agent.VERIFY_READ_EXCEPTION in out.verification_failures
        assert "SECRET1234567" not in json.dumps(out.to_dict(), ensure_ascii=False)


# ────────────────────────────────────────────────────────────────────────
# 4. 拒绝 ≠ 成功(采集侧拒绝仍会被落成 refused run, 但 outcome 不许是成功)
# ────────────────────────────────────────────────────────────────────────
class TestRefusalIsNotSuccess:
    def test_missing_fetch_deps_is_refused_and_not_success(self, engine):
        out = agent.run_once(bind=engine, now=NOW, day=DAY)
        assert out.outcome == agent.OUTCOME_REFUSED
        assert out.ok is False
        assert out.reason == agent.REASON_CAPTURE_REFUSED
        assert out.batch_status == cap.STATUS_REFUSED
        # 拒绝也留痕(库里一条 refused run), 但一行证据都没有
        assert isinstance(out.run_id, int)
        assert out.verified is True  # 核对了"零行"这件事
        assert out.evidence is not None
        assert out.evidence["boards"] == [] and out.evidence["limit_up"] == []
        runs = _runs(engine)
        assert [r["status"] for r in runs] == [store.STATUS_REFUSED]
        assert runs[0]["refused_reason"] == cap.REASON_DEPS_MISSING
        assert _count(engine, store.THEME_OBS_BOARD) == 0

    def test_refused_payload_says_refused_and_has_no_evidence_counts(self, engine):
        payload = agent.run_once(bind=engine, now=NOW, day=DAY).to_dict()
        assert payload["outcome"] == agent.OUTCOME_REFUSED
        assert payload["ok"] is False
        assert payload["batch_status"] == cap.STATUS_REFUSED
        assert payload["evidence"] == {"boards": 0, "limit_up": 0,
                                       "pool_state": cap.POOL_UNKNOWN, "pool_count": None}
        assert payload["unknowns"]

    def test_stale_source_refused_without_fetching(self, engine):
        h = Harness(fresh=False)
        out = h.run(engine)
        assert out.outcome == agent.OUTCOME_REFUSED
        assert out.ok is False
        assert (h.board_calls, h.member_calls, h.pool_calls) == (0, [], [])
        assert _runs(engine)[0]["refused_reason"] == cap.REASON_SOURCE_STALE

    def test_wrong_day_refused_without_fetching(self, engine):
        h = Harness()
        out = h.run(engine, day=PREV_DAY)
        assert out.outcome == agent.OUTCOME_REFUSED
        assert out.ok is False
        assert (h.board_calls, h.member_calls, h.pool_calls) == (0, [], [])

    def test_attempt_cap_is_refused_without_run_id(self, engine):
        for i in range(store.MAX_ATTEMPTS_PER_DAY):
            assert Harness().run(engine, slot=f"s{i}").ok is True
        over = Harness().run(engine, slot="overflow")
        assert over.outcome == agent.OUTCOME_REFUSED
        assert over.ok is False
        assert over.run_id is None
        assert over.verified is None  # 没有可核对的写入
        assert "attempt_cap" in over.reason


# ────────────────────────────────────────────────────────────────────────
# 5. 采集失败: 真实异常 / 注入异常, 都不许报告成功, 也不许泄漏凭据
# ────────────────────────────────────────────────────────────────────────
class TestCaptureFailure:
    def test_missing_schema_is_a_real_failure(self):
        """真实 SQLite 引擎但没建表 ⇒ 采集抛异常 ⇒ failed, 不冒充成功。"""
        eng = create_engine("sqlite://")
        try:
            out = Harness().run(eng)
        finally:
            eng.dispose()
        assert out.outcome == agent.OUTCOME_FAILED
        assert out.ok is False
        assert out.run_id is None
        assert out.evidence is None
        assert out.reason.startswith(agent.REASON_CAPTURE_EXCEPTION)
        assert "OperationalError" in out.reason

    def test_capture_exception_does_not_leak_secret(self, engine):
        def capture(*a, **kw):
            raise RuntimeError(f"POST https://gw/x?{SECRET}")

        out = Harness().run(engine, capture=capture)
        assert out.outcome == agent.OUTCOME_FAILED
        assert out.ok is False
        text = json.dumps(out.to_dict(), ensure_ascii=False)
        assert "SECRET1234567" not in text and "https://" not in text

    def test_capture_without_append_is_a_failure(self, engine):
        class Bare:
            status = cap.STATUS_COMPLETE

        out = Harness().run(engine, capture=lambda *a, **kw: Bare())
        assert out.outcome == agent.OUTCOME_FAILED
        assert out.ok is False
        assert agent.REASON_CAPTURE_NO_RESULT in out.reason

    def test_capture_returning_none_is_a_failure(self, engine):
        out = Harness().run(engine, capture=lambda *a, **kw: None)
        assert out.outcome == agent.OUTCOME_FAILED
        assert out.ok is False


# ────────────────────────────────────────────────────────────────────────
# 6. 部分数据: partial 有证据但绝不是成功; unknown 落 NULL 不是 0
# ────────────────────────────────────────────────────────────────────────
class TestPartialData:
    def test_partial_batch_is_not_success_but_keeps_evidence(self, engine):
        out = Harness(members={"880001": _members(), "880002": None}).run(engine)
        assert out.outcome == agent.OUTCOME_PARTIAL
        assert out.ok is False
        assert out.verified is True
        assert out.batch_status == cap.STATUS_PARTIAL
        states = {r["board_code"]: r for r in out.evidence["boards"]}
        assert states["880001"]["state"] == cap.STATE_OK
        assert states["880002"]["state"] == cap.STATE_FAILED
        assert states["880002"]["member_count"] is None  # 失败不是 0
        assert states["880002"]["members_json"] is None
        assert any("not_complete" in u for u in out.unknowns)

    def test_unknown_pool_is_null_count_and_no_rows(self, engine):
        out = Harness(pool=[]).run(engine)  # 空列表无法区分失败与"真的 0 只"
        assert out.outcome == agent.OUTCOME_PARTIAL
        assert out.ok is False
        assert out.evidence["run"]["pool_state"] == cap.POOL_UNKNOWN
        assert out.evidence["run"]["limit_up_count"] is None
        assert out.evidence["limit_up"] == []
        assert "pool_count_unknown" in out.unknowns

    def test_unknown_source_freshness_is_reported_as_unknown(self, engine):
        out = Harness(fresh=None).run(engine)
        assert out.outcome == agent.OUTCOME_PARTIAL
        assert "source_fresh_unknown" in out.unknowns
        assert any("source_trade_date" in u for u in out.unknowns) is False  # 交易日已对齐

    def test_unknown_trade_date_is_reported_as_unknown(self, engine):
        out = Harness(trade_date=None).run(engine)
        assert out.outcome == agent.OUTCOME_PARTIAL
        assert "source_trade_date_unknown" in out.unknowns

    def test_board_with_unknown_date_is_counted(self, engine):
        out = Harness(members={"*": ["000001", "000002"]}).run(engine)
        assert out.outcome == agent.OUTCOME_PARTIAL
        assert any(u.startswith("boards_with_unknown_date:") for u in out.unknowns)


# ────────────────────────────────────────────────────────────────────────
# 7. 重复运行: 同槽位幂等, 换槽位是新批次, 两者都不许重复写行
# ────────────────────────────────────────────────────────────────────────
class TestDuplicates:
    def test_same_slot_replay_is_idempotent(self, engine):
        h = Harness()
        first = h.run(engine)
        second = h.run(engine)

        assert first.inserted is True and first.replayed is False
        assert second.inserted is False and second.replayed is True
        assert second.run_id == first.run_id
        assert second.outcome == agent.OUTCOME_COMPLETE
        assert second.ok is True  # 证据仍然是完整且已核对的那一批
        assert agent.NOTE_REPLAYED in second.notes
        assert len(_runs(engine)) == 1
        assert _count(engine, store.THEME_OBS_BOARD) == 2
        assert second.evidence["run"]["run_id"] == second.run_id

    def test_new_slot_is_a_new_attempt_without_touching_the_old_one(self, engine):
        first = Harness().run(engine, slot="daily")
        later = NOW + dt.timedelta(hours=2)
        second = Harness().run(engine, slot="recover1", now=later)

        assert second.run_id != first.run_id
        assert second.attempt_seq == 2
        assert second.outcome == agent.OUTCOME_COMPLETE
        runs = _runs(engine)
        assert [r["attempt_seq"] for r in runs] == [1, 2]
        assert _count(engine, store.THEME_OBS_BOARD) == 4  # 两批各 2 行, 旧批次没被抹
        # 读回的仍只是第二批自己的 2 行(不跨批次拼)
        assert {r["run_id"] for r in second.evidence["boards"]} == {second.run_id}
        assert len(second.evidence["boards"]) == 2

    def test_second_attempt_partial_does_not_borrow_first_attempt(self, engine):
        Harness().run(engine, slot="daily")
        second = Harness(pool=[]).run(engine, slot="recover1",
                                      now=NOW + dt.timedelta(hours=2))
        assert second.outcome == agent.OUTCOME_PARTIAL
        assert second.evidence["run"]["run_key"] == cap.run_key_for(DAY, "recover1")
        assert second.evidence["run"]["pool_state"] == cap.POOL_UNKNOWN
        assert second.evidence["limit_up"] == []

    def test_partial_replay_is_idempotent_too(self, engine):
        kwargs = {"members": {"880001": _members(), "880002": None}}
        first = Harness(**kwargs).run(engine, slot="part")
        second = Harness(**kwargs).run(engine, slot="part")
        assert first.outcome == agent.OUTCOME_PARTIAL
        assert second.inserted is False and second.replayed is True
        assert second.run_id == first.run_id
        assert second.outcome == agent.OUTCOME_PARTIAL
        assert second.ok is False  # 重放不把 partial 升级成成功
        assert len(_runs(engine)) == 1
        assert _count(engine, store.THEME_OBS_BOARD) == 2

    def test_same_slot_with_different_inputs_conflicts_and_keeps_old_batch(self, engine):
        """同槽位不同输入: store 拒绝当成重试 ⇒ agent 报失败, 旧批次一行不动。"""
        first = Harness().run(engine, slot="daily")
        conflicting = Harness(pool=[]).run(engine, slot="daily")
        assert conflicting.outcome == agent.OUTCOME_FAILED
        assert conflicting.ok is False
        assert conflicting.evidence is None
        assert "EvidenceConflictError" in (conflicting.reason or "")
        assert len(_runs(engine)) == 1
        assert _count(engine, store.THEME_OBS_BOARD) == 2
        with engine.connect() as conn:
            kept = store.evidence_for_run(conn, first.run_id)
        assert len(kept["boards"]) == 2 and len(kept["limit_up"]) == 1


# ────────────────────────────────────────────────────────────────────────
# 8. 时间线: 显式注入才用, 缺失/非法一律 unknown+, 拒绝轮次不附带
# ────────────────────────────────────────────────────────────────────────
class TestTimelineInjection:
    def test_no_timeline_inputs_yields_explicit_unknown(self, engine):
        out = Harness().run(engine)
        assert out.timeline is None
        assert out.timeline_state == agent.TIMELINE_UNKNOWN
        assert out.timeline_reason == agent.REASON_TIMELINE_INPUTS_MISSING
        assert "timeline_events_not_injected" in out.unknowns
        assert "timeline_klines_not_injected" in out.unknowns
        assert out.ok is True  # 采集/核对成功与否与时间线是否可用分开报告
        payload = out.to_dict()
        assert payload["timeline"] == {"state": agent.TIMELINE_UNKNOWN,
                                       "reason": agent.REASON_TIMELINE_INPUTS_MISSING,
                                       "run_id": None, "association": "unverified_independent_input",
                                       "dates": 0, "events": 0}

    def test_only_events_injected_is_still_unknown(self, engine):
        out = Harness().run(engine, events=EVENTS)
        assert out.timeline is None
        assert out.timeline_state == agent.TIMELINE_UNKNOWN
        assert "timeline_klines_not_injected" in out.unknowns
        assert "timeline_events_not_injected" not in out.unknowns

    def test_valid_timeline_is_explicitly_unlinked_to_capture(self, engine):
        out = Harness().run(engine, events=EVENTS, klines=KLINES)
        assert out.timeline_state == agent.TIMELINE_OK
        assert out.timeline_reason is None
        assert out.timeline is not None
        assert [d["date"] for d in out.timeline["timeline"]] == ["2026-10-12", "2026-10-13"]
        assert out.timeline["landings"] == {"total": 3, "exact": 3, "mapped": 0,
                                            "unresolved": 0}
        # 没有显式生成时刻时不能挪用采集起点。
        assert out.timeline["provenance"]["created_at"] is None
        # 时间线是事后标签: 必须如实标未证实时点
        assert out.timeline["provenance"]["point_in_time_verified"] is False
        assert "timeline_point_in_time_unverified" in out.unknowns
        payload = out.to_dict()
        assert payload["timeline"] == {"state": agent.TIMELINE_OK, "reason": None,
                                       "run_id": None, "association": "unverified_independent_input",
                                       "dates": 2, "events": 3}

    def test_explicit_generated_at_flows_into_timeline(self, engine):
        later = NOW + dt.timedelta(minutes=5)
        out = Harness().run(engine, events=EVENTS, klines=KLINES, generated_at=later)
        assert out.timeline["provenance"]["created_at"] == "2026-11-16T15:21:00"

    def test_verified_code_map_is_used_and_reported(self, engine):
        events = (_event("20261012", "SYNX001", ("合成题材甲",)),)
        out = Harness().run(engine, events=events, klines=KLINES,
                            verified_code_map={"SYNX001": "SYN001"})
        assert out.timeline["landings"] == {"total": 1, "exact": 0, "mapped": 1,
                                            "unresolved": 0}
        assert out.timeline["provenance"]["verified_code_map"]["used"] == 1
        assert out.timeline["provenance"]["identity_mapping_verification"] == \
            "caller_asserted_unverified"

    def test_invalid_events_are_reported_not_silently_dropped(self, engine):
        broken = ({"date": "20261012", "code": "SYN001", "labels": ["合成题材甲"],
                   "source": "fixture-vendor"},)  # 缺 reference
        out = Harness().run(engine, events=broken, klines=KLINES)
        assert out.timeline is None
        assert out.timeline_state == agent.TIMELINE_INVALID
        assert out.timeline_reason == "missing_reference"
        assert any(u.startswith("timeline_invalid:") for u in out.unknowns)
        # 时间线坏了不影响"这次采集已落库且核对通过"这件事, 但也不会说时间线可用
        assert out.ok is True and out.verified is True

    def test_empty_events_are_invalid_not_an_empty_timeline(self, engine):
        out = Harness().run(engine, events=(), klines=KLINES)
        assert out.timeline is None
        assert out.timeline_state == agent.TIMELINE_INVALID
        assert out.timeline_reason == "events_empty"

    def test_unparsable_timeline_dates_are_invalid(self, engine):
        bad = (_event("2026-10-10", "SYN001"),)  # 周六: 无交易日
        out = Harness().run(engine, events=bad, klines=KLINES)
        assert out.timeline_state == agent.TIMELINE_INVALID
        assert out.timeline_reason == "weekend_date"

    def test_refused_batch_does_not_get_a_timeline(self, engine):
        out = agent.run_once(bind=engine, now=NOW, day=DAY, events=EVENTS, klines=KLINES)
        assert out.outcome == agent.OUTCOME_REFUSED
        assert out.timeline is None
        assert out.timeline_state == agent.TIMELINE_SKIPPED
        assert out.timeline_reason == agent.REASON_TIMELINE_NO_BATCH
        assert "timeline_skipped_no_batch" in out.unknowns

    def test_failed_readback_does_not_get_a_timeline(self, engine):
        out = Harness().run(engine, events=EVENTS, klines=KLINES,
                            reader=lambda bind, run_id: None)
        assert out.outcome == agent.OUTCOME_FAILED
        assert out.timeline is None
        assert out.timeline_state == agent.TIMELINE_SKIPPED


# ────────────────────────────────────────────────────────────────────────
# 9. 输出安全: 无方向/收益/仓位字段, 无仓位买卖措辞, 可 JSON 序列化
# ────────────────────────────────────────────────────────────────────────
def _walk_keys(node):
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _walk_keys(value)
    elif isinstance(node, (list, tuple)):
        for value in node:
            yield from _walk_keys(value)


class TestOutputSafety:
    @pytest.mark.parametrize("ctor,run_kwargs,expected", [
        ({}, {}, agent.OUTCOME_COMPLETE),
        ({}, {"events": EVENTS, "klines": KLINES}, agent.OUTCOME_COMPLETE),
        ({"pool": []}, {}, agent.OUTCOME_PARTIAL),
        ({"fresh": False}, {}, agent.OUTCOME_REFUSED),
    ])
    def test_no_forbidden_trading_keys(self, engine, ctor, run_kwargs, expected):
        out = Harness(**ctor).run(engine, **run_kwargs)
        assert out.outcome == expected
        leaked = [k for k in _walk_keys(out.to_dict()) if k in tl.FORBIDDEN_OUTPUT_KEYS]
        assert leaked == []

    def test_payload_is_json_serializable(self, engine):
        out = Harness().run(engine, events=EVENTS, klines=KLINES)
        text = json.dumps(out.to_dict(), ensure_ascii=False)
        assert len(text) > 100

    def test_no_position_or_direction_wording(self, engine):
        out = Harness().run(engine, events=EVENTS, klines=KLINES)
        text = json.dumps(out.to_dict(), ensure_ascii=False)
        tl.assert_no_trading_wording(text)
        for word in ("建议买入", "建议卖出", "加仓", "减仓", "目标价", "仓位"):
            assert word not in text

    def test_limitations_are_declared_with_each_result(self, engine):
        payload = Harness().run(engine).to_dict()
        assert len(payload["limitations"]) >= 4
        joined = " ".join(payload["limitations"])
        assert "不构成投资建议" in joined or "不构成" in joined
        assert "未" in joined  # 至少一条明说"未验证/未覆盖"

    def test_result_is_immutable(self, engine):
        out = Harness().run(engine)
        with pytest.raises(dataclasses.FrozenInstanceError):
            out.ok = True  # type: ignore[misc]
