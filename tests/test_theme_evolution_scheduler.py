"""题材演化 agent 的 SIDA 调度注册适配器契约(TDD: 先红后绿)。

被测模块: `src/core/theme_evolution_scheduler.py`

这一层只做一件事: 把**已有的** `theme_evolution_agent.run_once` 注册成一个
SIDA 内 APScheduler 的定时作业。它不抓数、不建库、不迁移、不接启动、不发消息。

硬约束(违反即视为 bug, 每条都有断言):

1. **默认关闭, 开关只认显式 "1"**: 未设/`0`/`true`/`yes`/空串一律不注册任何 job;
   且注册后再把开关置回 `0`, 已注册的 job 也不许跑。
2. **依赖全部显式注入**: 只有 engine + deps + 上游就绪回调三样东西, 没有默认取数实现;
   缺任何一样都不运行, 并把原因记成未知, 绝不回退到模块内联网实现。
3. **上游 ready 与目标日都要肯定证据**: `ready is True`(不是 truthy) **且** 上游给出的
   交易日 == 时钟当日(合法 YYYYMMDD)才运行; 否则不运行并记 unknown。
4. **锁忙 / 失败不许变成成功**: 进程内锁拿不到 → `skipped`; agent 返回 refused/partial/
   异常 → `failed`/`refused`/`partial`, `ok is False`; 只有 agent 自己 `ok is True`
   且核对通过才算 `completed`。
5. **不建表 / 不迁移 / 不接启动**: 模块源码里没有任何建引擎/建表/读配置/迁移/启动
   接线路径; 对着没有表的库跑一轮也不会凭空建表。
6. **不声称跨进程安全**: 锁与 `max_instances` 都只在单进程内有效, 输出里固定
   `cross_process_safe=False`; 多 worker 部署下的互斥不由本层负责。
7. **无交易 / 无外发**: 结果不含方向/收益/目标/仓位字段, 不生成任何对外消息或报告。

夹具全部是合成数据(`SYN` 前缀) + 内存 SQLite, 零网络、零真实历史。
"""

from __future__ import annotations

import datetime as dt
import inspect
import json
import pathlib
import subprocess
import sys
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, func, select

from src.core import theme_evolution_agent as agent
from src.core import theme_evolution_capture as cap
from src.core import theme_evolution_scheduler as sched
from src.core import theme_evolution_store as store

CST = ZoneInfo("Asia/Shanghai")
NOW = dt.datetime(2026, 11, 16, 15, 16, tzinfo=CST)  # 周一
DAY = "20261116"
PREV_DAY = "20261113"
SECRET = "SECRET1234567"

BOARDS = (
    {"code": "880001", "name": "半导体", "board_type": "concept"},
    {"code": "880002", "name": "光伏", "board_type": "concept"},
)


# ── 合成取数依赖(零网络, 记账调用次数) ──────────────────────────────────
class Harness:
    def __init__(self, *, boards=BOARDS, pool=None, fresh: bool = True,
                 trade_date=DAY, members=None):
        self.boards = boards
        self.pool = pool if pool is not None else [
            {"code": "000001", "name": "合成标的甲", "pool_coverage": "complete",
             "as_of": "2026-11-16", "as_of_basis": "kline"},
        ]
        self.fresh = fresh
        self.trade_date = trade_date
        self.members = members if members is not None else {
            "*": {"members": ["000001", "000002"], "data_day": DAY,
                  "date_basis": "source_reported"},
        }
        self.board_calls = 0
        self.pool_calls = 0

    def fetch_boards(self):
        self.board_calls += 1
        if isinstance(self.boards, BaseException):
            raise self.boards
        return self.boards

    def fetch_members(self, code: str):
        result = self.members.get(code, self.members.get("*"))
        if isinstance(result, BaseException):
            raise result
        return result

    def fetch_pool(self, day: str):
        self.pool_calls += 1
        if isinstance(self.pool, BaseException):
            raise self.pool
        return self.pool

    def fetch_trade_date(self):
        if isinstance(self.trade_date, BaseException):
            raise self.trade_date
        return self.trade_date

    def deps(self, **over) -> dict:
        base = dict(
            fetch_boards=self.fetch_boards,
            fetch_members=self.fetch_members,
            fetch_pool=self.fetch_pool,
            fetch_trade_date=self.fetch_trade_date,
            source_fresh=self.fresh,
        )
        base.update(over)
        return base


class FakeAPScheduler:
    """只实现适配器用到的那几个方法; 记录 add_job 的全部关键字。"""

    def __init__(self) -> None:
        self.jobs: dict[str, object] = {}
        self.calls: list[dict] = []
        self.started = False

    def add_job(self, func, trigger=None, *, id, **kwargs):  # noqa: A002 — 对齐 APScheduler
        self.calls.append({"func": func, "trigger": trigger, "id": id, **kwargs})
        self.jobs[id] = func
        return object()

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def get_jobs(self):
        return [type("J", (), {"id": jid})() for jid in self.jobs]

    def start(self):
        self.started = True


@pytest.fixture(autouse=True)
def _switch_on(monkeypatch):
    """除开关用例自己显式覆盖外, 本文件的运行用例都在"开关已开"前提下验证。

    默认关闭本身由 TestEnableSwitch 里删掉环境变量的用例守护。
    """
    monkeypatch.setenv(sched.ENV_FLAG, "1")


@pytest.fixture()
def engine():
    """完全隔离的内存 SQLite; schema 由**测试**建, 不是被测模块建的。"""
    eng = create_engine("sqlite://")
    store.create_schema(eng)
    try:
        yield eng
    finally:
        eng.dispose()


def _run_count(engine) -> int:
    with engine.connect() as conn:
        return int(conn.execute(select(func.count()).select_from(store.THEME_OBS_RUN)).scalar_one())


def _upstream(**over):
    state = {"ready": True, "day": DAY}
    state.update(over)
    return lambda now: dict(state)


def _make(engine, deps=None, upstream=None, *, fake=None, slot=cap.DEFAULT_SLOT):
    return sched.ThemeEvolutionScheduler(
        engine=engine,
        deps=Harness().deps() if deps is None else deps,
        upstream_ready=_upstream() if upstream is None else upstream,
        scheduler=fake if fake is not None else FakeAPScheduler(),
        now=lambda: NOW,
        slot=slot,
    )


# ────────────────────────────────────────────────────────────────────────
# 1. 开关: 只认显式 "1", 其它一律不注册
# ────────────────────────────────────────────────────────────────────────
class TestEnableSwitch:
    def test_flag_name_is_explicit(self):
        assert sched.ENV_FLAG == "SIDA_THEME_EVOLUTION_AGENT"

    @pytest.mark.parametrize(
        "value,expect",
        [
            ("1", True),
            (" 1 ", True),
            (None, False),
            ("", False),
            ("0", False),
            ("true", False),
            ("True", False),
            ("yes", False),
            ("on", False),
            ("2", False),
            ("01", False),
        ],
    )
    def test_only_exact_one_enables(self, value, expect, monkeypatch):
        if value is None:
            monkeypatch.delenv(sched.ENV_FLAG, raising=False)
        else:
            monkeypatch.setenv(sched.ENV_FLAG, value)
        assert sched.is_enabled() is expect

    def test_default_disabled_registers_nothing(self, engine, monkeypatch):
        monkeypatch.delenv(sched.ENV_FLAG, raising=False)
        fake = FakeAPScheduler()
        out = _make(engine, fake=fake).register()
        assert out.registered is False
        assert out.job_id is None
        assert out.reason == sched.REASON_FLAG_OFF
        assert fake.calls == [] and fake.jobs == {}

    @pytest.mark.parametrize("value", ["0", "true", "yes", "", "2"])
    def test_non_one_flag_registers_nothing(self, engine, monkeypatch, value):
        monkeypatch.setenv(sched.ENV_FLAG, value)
        fake = FakeAPScheduler()
        out = _make(engine, fake=fake).register()
        assert out.registered is False and fake.jobs == {}
        assert out.reason == sched.REASON_FLAG_OFF

    def test_enabled_registers_exactly_one_job(self, engine, monkeypatch):
        monkeypatch.setenv(sched.ENV_FLAG, "1")
        fake = FakeAPScheduler()
        out = _make(engine, fake=fake).register()
        assert out.registered is True
        assert out.job_id == sched.JOB_ID
        assert list(fake.jobs) == [sched.JOB_ID], "只许注册一个 job"
        call = fake.calls[0]
        assert call["coalesce"] is True
        assert call["max_instances"] == 1
        assert call["replace_existing"] is True
        assert fake.started is False, "注册不等于启动进程"

    def test_duplicate_registration_is_idempotent(self, engine, monkeypatch):
        monkeypatch.setenv(sched.ENV_FLAG, "1")
        fake = FakeAPScheduler()
        first = _make(engine, fake=fake).register()
        second = _make(engine, fake=fake).register()  # 另一个实例, 同一个 APScheduler
        third = _make(engine, fake=fake).register()
        assert first.registered is True
        assert second.registered is False and second.reason == sched.REASON_ALREADY_REGISTERED
        assert third.registered is False
        assert list(fake.jobs) == [sched.JOB_ID], "重复注册不能堆出第二个 job"

    def test_registration_leaves_global_registry_alone(self, engine, monkeypatch):
        """启动/自检接线由产品侧负责: 本适配器不写全局注册表。"""
        from src.core import scheduler_registry

        before = scheduler_registry.get_all()
        monkeypatch.setenv(sched.ENV_FLAG, "1")
        _make(engine).register()
        assert scheduler_registry.get_all() == before

    def test_flag_flipped_off_after_registration_blocks_run(self, engine, monkeypatch):
        monkeypatch.setenv(sched.ENV_FLAG, "1")
        fake = FakeAPScheduler()
        s = _make(engine, fake=fake)
        s.register()
        monkeypatch.setenv(sched.ENV_FLAG, "0")
        out = fake.jobs[sched.JOB_ID]()  # 已注册的 job 被触发
        assert out["status"] == sched.STATUS_DISABLED
        assert out["ok"] is False
        assert _run_count(engine) == 0, "开关关了就不许落任何证据"


# ────────────────────────────────────────────────────────────────────────
# 2. 上游 ready + 目标日: 缺肯定证据一律不运行, 并记 unknown
# ────────────────────────────────────────────────────────────────────────
class TestUpstreamGate:
    def test_not_ready_does_not_run(self, engine):
        harness = Harness()
        s = _make(engine, deps=harness.deps(), upstream=_upstream(ready=False))
        out = s.run_now()
        assert out.status == sched.STATUS_SKIPPED
        assert out.ok is False
        assert out.reason == sched.REASON_UPSTREAM_NOT_READY
        assert "upstream_state_unknown" in out.unknowns
        assert out.run_id is None
        assert harness.board_calls == 0 and harness.pool_calls == 0
        assert _run_count(engine) == 0

    @pytest.mark.parametrize("ready", [1, "true", "1", None, 0, "yes"])
    def test_ready_must_be_true_not_truthy(self, engine, ready):
        harness = Harness()
        s = _make(engine, deps=harness.deps(), upstream=_upstream(ready=ready))
        out = s.run_now()
        assert out.ok is False and out.status == sched.STATUS_SKIPPED
        assert harness.board_calls == 0 and _run_count(engine) == 0

    def test_wrong_day_does_not_run(self, engine):
        harness = Harness()
        s = _make(engine, deps=harness.deps(), upstream=_upstream(day=PREV_DAY))
        out = s.run_now()
        assert out.status == sched.STATUS_SKIPPED
        assert out.ok is False
        assert out.reason == sched.REASON_UPSTREAM_DAY_MISMATCH
        assert out.day == PREV_DAY, "结果里要保留上游报的那一天, 便于排查"
        assert "upstream_day_mismatch" in out.unknowns
        assert harness.board_calls == 0 and _run_count(engine) == 0

    @pytest.mark.parametrize("day", [None, "", "2026111", "2026-11-16", "20261332", 20261116])
    def test_missing_or_malformed_day_does_not_run(self, engine, day):
        harness = Harness()
        s = _make(engine, deps=harness.deps(), upstream=_upstream(day=day))
        out = s.run_now()
        assert out.ok is False
        assert out.status == sched.STATUS_SKIPPED
        assert out.reason == sched.REASON_UPSTREAM_DAY_UNKNOWN
        assert "upstream_day_unknown" in out.unknowns
        assert harness.board_calls == 0 and _run_count(engine) == 0
        # 未知不能写成 0/空串之类"看起来已知"的值
        assert out.day is None

    def test_upstream_exception_does_not_run_or_leak_message(self, engine):
        def boom(now):
            raise RuntimeError(f"token={SECRET}")

        harness = Harness()
        out = _make(engine, deps=harness.deps(), upstream=boom).run_now()
        assert out.status in (sched.STATUS_SKIPPED, sched.STATUS_FAILED)
        assert out.ok is False
        assert out.reason == sched.REASON_UPSTREAM_ERROR
        assert "upstream_error" in out.unknowns
        assert SECRET not in json.dumps(out.to_dict(), ensure_ascii=False)
        assert harness.board_calls == 0 and _run_count(engine) == 0

    def test_upstream_missing_does_not_run(self, engine):
        harness = Harness()
        s = sched.ThemeEvolutionScheduler(
            engine=engine, deps=harness.deps(), upstream_ready=None,
            scheduler=FakeAPScheduler(), now=lambda: NOW,
        )
        out = s.run_now()
        assert out.ok is False
        assert out.reason == sched.REASON_UPSTREAM_MISSING
        assert "upstream_not_injected" in out.unknowns
        assert harness.board_calls == 0 and _run_count(engine) == 0

    def test_missing_engine_or_deps_does_not_run(self):
        harness = Harness()
        out = sched.ThemeEvolutionScheduler(
            engine=None, deps=harness.deps(), upstream_ready=_upstream(),
            scheduler=FakeAPScheduler(), now=lambda: NOW,
        ).run_now()
        assert out.ok is False and out.reason == sched.REASON_DEPS_MISSING
        assert "engine_not_injected" in out.unknowns

        out2 = sched.ThemeEvolutionScheduler(
            engine=object(), deps={}, upstream_ready=_upstream(),
            scheduler=FakeAPScheduler(), now=lambda: NOW,
        ).run_now()
        assert out2.ok is False and out2.reason == sched.REASON_DEPS_MISSING
        assert harness.board_calls == 0

    def test_deps_may_not_shadow_now_bind_day_slot(self):
        for key in ("now", "bind", "day", "slot"):
            with pytest.raises(ValueError):
                sched.ThemeEvolutionScheduler(
                    engine=object(), deps={key: "x"}, upstream_ready=_upstream(),
                    scheduler=FakeAPScheduler(), now=lambda: NOW,
                )


# ────────────────────────────────────────────────────────────────────────
# 3. 触发成功: 走真实 agent.run_once, 真实落库, 读回核对通过
# ────────────────────────────────────────────────────────────────────────
class TestTriggerSuccess:
    def test_trigger_runs_real_agent_and_persists_one_batch(self, engine):
        harness = Harness()
        s = _make(engine, deps=harness.deps())
        out = s.run_now()
        assert out.status == sched.STATUS_COMPLETED
        assert out.ok is True
        assert out.reason is None
        assert out.day == DAY
        assert out.agent_outcome == agent.OUTCOME_COMPLETE
        assert isinstance(out.run_id, int)
        assert harness.board_calls == 1
        assert _run_count(engine) == 1
        with engine.connect() as conn:
            row = conn.execute(select(store.THEME_OBS_RUN)).mappings().one()
        assert row["day"] == DAY and row["status"] == store.STATUS_COMPLETE
        assert row["run_key"] == cap.run_key_for(DAY, cap.DEFAULT_SLOT)

    def test_registered_job_is_callable_without_args(self, engine, monkeypatch):
        monkeypatch.setenv(sched.ENV_FLAG, "1")
        fake = FakeAPScheduler()
        harness = Harness()
        s = _make(engine, deps=harness.deps(), fake=fake)
        s.register()
        payload = fake.jobs[sched.JOB_ID]()  # APScheduler 调用时不传参
        assert payload["status"] == sched.STATUS_COMPLETED
        assert payload["ok"] is True
        assert _run_count(engine) == 1

    def test_capture_only_injection_is_accepted(self, engine):
        """`capture` 是 agent 的正式注入点: 只给替换实现(不给 fetch_*)也算有数据来源。"""
        harness = Harness()

        def custom_capture(bind, **kw):
            return cap.capture_day(bind, **{**kw, **harness.deps()})

        out = _make(engine, deps={"capture": custom_capture}).run_now()
        assert out.status == sched.STATUS_COMPLETED
        assert out.ok is True
        assert harness.board_calls == 1

    def test_second_trigger_same_slot_is_replay_not_new_evidence(self, engine):
        harness = Harness()
        s = _make(engine, deps=harness.deps())
        first = s.run_now()
        second = s.run_now()
        assert first.ok is True and second.ok is True
        assert second.replayed is True
        assert second.run_id == first.run_id
        assert _run_count(engine) == 1, "同槽位重跑是幂等重放, 不许再落一个批次"

    def test_result_is_json_safe_and_has_no_trading_fields(self, engine):
        out = _make(engine).run_now()
        payload = out.to_dict()
        assert json.loads(json.dumps(payload, ensure_ascii=False)) == payload
        banned = ("direction", "position", "target_price", "expected_return",
                  "buy", "sell", "signal", "advice_recommendation")
        for key in payload:
            assert not any(b in key.lower() for b in banned), f"结果不许带交易字段: {key}"
        assert payload["contains_trading_advice"] is False
        assert payload["limitations"], "边界声明不能为空"
        assert payload["timezone"] == "Asia/Shanghai"

    def test_never_claims_cross_process_safety(self, engine, monkeypatch):
        monkeypatch.setenv(sched.ENV_FLAG, "1")
        out = _make(engine).register()
        payload = out.to_dict()
        assert payload["cross_process_safe"] is False
        assert payload["lock_scope"] == sched.LOCK_SCOPE == "process_local"
        assert any("跨进程" in line for line in payload["limitations"])
        assert any("max_instances" in line for line in payload["limitations"])
        assert any("跨进程" in line for line in sched.SCHEDULER_LIMITATIONS)


# ────────────────────────────────────────────────────────────────────────
# 4. 锁忙 / 失败 / 异常: 一律不许变成成功
# ────────────────────────────────────────────────────────────────────────
class TestFailureHandling:
    def test_lock_busy_is_skipped_and_unknown(self, engine):
        harness = Harness()
        s = _make(engine, deps=harness.deps())
        assert s._lock.acquire(blocking=False) is True
        try:
            out = s.run_now()
        finally:
            s._lock.release()
        assert out.status == sched.STATUS_SKIPPED
        assert out.ok is False
        assert out.reason == sched.REASON_LOCK_BUSY
        assert "run_lock_busy" in out.unknowns
        assert harness.board_calls == 0 and _run_count(engine) == 0

    def test_source_fetch_failure_is_refused_and_writes_nothing(self, engine):
        """板块目录取失败 → 采集拒绝: 一行不写, 且绝不算成功。"""
        harness = Harness(boards=RuntimeError("目录不可达"))
        out = _make(engine, deps=harness.deps()).run_now()
        assert out.status == sched.STATUS_REFUSED
        assert out.ok is False
        assert out.unknowns, "拒绝必须留下未知项, 不能装作一切正常"
        # 拒绝会留一条 status=refused 的运行行(凭据是"没采到"), 但板块明细一行不写。
        with engine.connect() as conn:
            rows = [dict(r) for r in conn.execute(select(store.THEME_OBS_RUN)).mappings()]
            boards = int(conn.execute(
                select(func.count()).select_from(store.THEME_OBS_BOARD)).scalar_one())
        assert len(rows) == 1 and rows[0]["status"] == store.STATUS_REFUSED
        assert boards == 0, "拒绝时不许写出空快照冒充证据"

    def test_unverified_readback_is_failed_not_success(self, engine):
        """落库有了、但按 run_id 读回核对不过 → agent failed, 这里也是 failed。"""
        def broken_reader(bind, run_id):
            raise RuntimeError(f"readback broken token={SECRET}")

        out = _make(engine, deps=Harness().deps(reader=broken_reader)).run_now()
        assert out.status == sched.STATUS_FAILED
        assert out.ok is False
        assert out.agent_outcome == agent.OUTCOME_FAILED
        assert out.run_id is not None, "批次确实写了, 只是没核对过 —— 不许报成没跑"
        assert out.unknowns, "失败必须留下未知项"
        assert SECRET not in json.dumps(out.to_dict(), ensure_ascii=False)

    def test_agent_refused_is_refused_not_completed(self, engine):
        """上游说 ready, 但源不新鲜 → 采集拒绝: 状态必须是 refused, 不是 completed。"""
        harness = Harness(fresh=False)
        out = _make(engine, deps=harness.deps()).run_now()
        assert out.status == sched.STATUS_REFUSED
        assert out.ok is False
        assert out.agent_outcome == agent.OUTCOME_REFUSED
        with engine.connect() as conn:
            status = conn.execute(select(store.THEME_OBS_RUN.c.status)).scalar_one()
        assert status == store.STATUS_REFUSED, "源不新鲜只许落 refused 行, 不许落 complete"

    def test_capture_exception_is_failed_and_lock_released(self, engine):
        """采集内部抛异常: agent 已把它归成 failed; 适配器照样不许说成功, 且必须放锁。"""
        def capture(*a, **kw):
            raise RuntimeError(f"token={SECRET}")

        s = _make(engine, deps=Harness().deps())
        s.deps = dict(s.deps, capture=capture)
        bad = s.run_now()
        assert bad.status == sched.STATUS_FAILED
        assert bad.ok is False
        assert bad.agent_outcome == agent.OUTCOME_FAILED
        assert bad.reason, "失败必须说清原因"
        assert bad.unknowns
        assert SECRET not in json.dumps(bad.to_dict(), ensure_ascii=False)
        assert s._lock.locked() is False, "异常路径必须放锁"

        # 放锁之后下一轮还能正常跑
        s.deps = Harness().deps()
        again = s.run_now()
        assert again.ok is True

    def test_unexpected_agent_exception_becomes_structured_failure(self, engine, monkeypatch):
        """agent 自己抛异常(不该发生)时, 适配器也要结构化上报并放锁, 不向调度器抛。"""
        def boom(**kw):
            raise RuntimeError(f"token={SECRET}")

        monkeypatch.setattr(sched.agent, "run_once", boom)
        s = _make(engine, deps=Harness().deps())
        out = s.run_now()
        assert out.status == sched.STATUS_FAILED
        assert out.ok is False
        assert out.reason == sched.REASON_AGENT_EXCEPTION
        assert "agent_exception" in out.unknowns
        assert SECRET not in json.dumps(out.to_dict(), ensure_ascii=False)
        assert s._lock.locked() is False
        # 作业入口也不许把异常抛给 APScheduler
        payload = s._job()
        assert payload["status"] == sched.STATUS_FAILED and payload["ok"] is False

    def test_invalid_slot_is_failed_not_success(self, engine):
        out = _make(engine, slot="bad slot!/..").run_now()
        assert out.status == sched.STATUS_FAILED
        assert out.ok is False
        assert out.reason == sched.REASON_AGENT_INVALID_ARGUMENT
        assert _run_count(engine) == 0

    def test_partial_batch_is_not_success(self, engine):
        """池子取失败 → partial: 有证据但结论不完整, ok 必须 False。"""
        harness = Harness(pool=RuntimeError("池子不可达"))
        out = _make(engine, deps=harness.deps()).run_now()
        assert out.agent_outcome == agent.OUTCOME_PARTIAL
        assert out.status == sched.STATUS_PARTIAL
        assert out.ok is False
        assert out.run_id is not None


# ────────────────────────────────────────────────────────────────────────
# 5. 不自建库 / 不迁移 / 不接启动 / 不发消息
# ────────────────────────────────────────────────────────────────────────
class TestNoSideEffects:
    def test_module_source_has_no_db_or_wiring_paths(self):
        source = inspect.getsource(sched)
        for token in ("create_engine", "create_schema", "SessionLocal", "SIDA_DB_URL",
                      "migrations", "src.bootstrap", "startup", "report_generator",
                      "write_report_document", "wecom", "alerting", "importlib",
                      "requests", "httpx", "aiohttp"):
            assert token not in source, f"模块源码不应出现 {token}"

    def test_import_pulls_no_network_module(self):
        code = (
            "import sys;"
            "import src.core.theme_evolution_scheduler;"
            "bad=[m for m in sys.modules if m.startswith(('requests','httpx','aiohttp',"
            "'marketdata','src.collectors'))];"
            "print(','.join(bad))"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=180,
            cwd=str(pathlib.Path(__file__).resolve().parent.parent),
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "", f"import 拉进了这些模块: {proc.stdout.strip()}"

    def test_run_against_empty_database_creates_no_table(self):
        bare = create_engine("sqlite://")
        try:
            out = sched.ThemeEvolutionScheduler(
                engine=bare, deps=Harness().deps(), upstream_ready=_upstream(),
                scheduler=FakeAPScheduler(), now=lambda: NOW,
            ).run_now()
            assert out.ok is False, "没有表就不该报成功"
            assert out.status == sched.STATUS_FAILED
            from sqlalchemy import inspect as sa_inspect

            assert "theme_obs_run" not in sa_inspect(bare).get_table_names(), \
                "本层不许自己建表(建表属于迁移流程)"
        finally:
            bare.dispose()

    def test_registration_is_not_started_by_import(self, monkeypatch):
        """import 本模块不得注册任何 job: 接线是产品侧显式动作。"""
        monkeypatch.setenv(sched.ENV_FLAG, "1")
        code = (
            "import src.core.theme_evolution_scheduler as m;"
            "print(m.ThemeEvolutionScheduler.__name__)"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=180,
            cwd=str(pathlib.Path(__file__).resolve().parent.parent),
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "ThemeEvolutionScheduler"

    def test_no_outbound_message_builder(self):
        names = [n for n, o in vars(sched).items() if callable(o) and not n.startswith("_")]
        for banned in ("send", "notify", "push", "report", "mail", "webhook"):
            offenders = [n for n in names if banned in n.lower()]
            assert offenders == [], f"本层不许有外发出口: {offenders}"
