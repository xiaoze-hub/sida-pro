"""题材演化观察层 A 通道: **采集编排**契约(TDD 先红后绿)。

被测模块: `src/core/theme_evolution_capture.py`

这个模块只做一件事: 把「注入的取数函数 + 显式当前时间」编排成一份**可追责的
证据批次**(按板块保存结果 + 每个板块自己的源有效日期/依据, 取不到就是 unknown),
再(可选)交给 `theme_evolution_store` 只追加地落库。它不联网、不开库、不写报告、
不做分类、不给交易建议。

硬约束(违反即视为 bug, 每条都有对应断言):

1. **默认不联网**: 不注入取数函数时返回结构化 refused(`deps_not_injected`),
   一次外部调用都不发; import 本模块不得拉进任何行情/采集器模块。
2. **显式当前时间**: `now` 是必填关键字参数, 模块内部不读 `datetime.now()`。
3. **当前成分不能冒充历史日**: 目标日 ≠ `now` 所在日 → refused
   `current_members_wrong_day`, 而且**连一次取数都不做**。
4. **一行一验 as_of**: 涨停池任一行缺 `as_of` / 多行 `as_of` 不一致 / `as_of`
   不是目标日 → 只把池状态记成 `unknown`, 不写任何代码, 不把旧池当今日的池。
5. **失败不是 0**: 取数失败/空列表 → 计数 `None`(落库为 NULL), 不是 0。
6. **不造分类、不生成建议**: 输出字段里没有 rank/score/advice/kind/theme 之类
   派生判断, 模块里不存在报告渲染/建议构造函数。
7. **同日恢复**: 同一天可以追加第二个 attempt(第一次 partial, 第二次 complete),
   旧批次不被覆盖; 同一个 `run_key` 重跑是幂等的, 不重复写行。
8. **完全隔离**: 测试只建 `sqlite://` 内存库, 不碰 `data/sida.db`、不读生产连接串。
"""

from __future__ import annotations

import datetime as dt
import inspect
import subprocess
import sys
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine

from src.core import theme_evolution_capture as cap
from src.core import theme_evolution_store as store

CST = ZoneInfo("Asia/Shanghai")
NOW = dt.datetime(2026, 11, 16, 15, 16, tzinfo=CST)  # 周一 15:16
DAY = "20261116"
PREV_DAY = "20261113"
SECRET = "token=SECRET1234567"

BOARDS = (
    {"code": "880001", "name": "半导体", "board_type": "concept"},
    {"code": "880002", "name": "光伏", "board_type": "concept"},
)


# ── 构造 helper ─────────────────────────────────────────────────────────
def _pool_row(code: str = "000001", as_of: str | None = "2026-11-16",
              basis: str | None = "kline", name: str = "平安银行") -> dict:
    row: dict = {"code": code, "name": name, "pool_coverage": "complete"}
    if as_of is not None:
        row["as_of"] = as_of
    if basis is not None:
        row["as_of_basis"] = basis
    return row


def _members(codes=("000001", "000002"), data_day: str | None = DAY,
             basis: str | None = "source_reported") -> dict:
    """富结果: 成员 + 源自报的有效日期/依据(都可为 None = unknown)。"""
    return {"members": list(codes), "data_day": data_day, "date_basis": basis}


class Harness:
    """注入式取数依赖 + 调用计数器(Fake, 全内存, 零网络)。"""

    def __init__(self, *, boards=BOARDS, members=None, pool=None, fresh: bool | None = True,
                 trade_date: str | Exception | None = DAY):
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
            fetch_boards=self.fetch_boards,
            fetch_members=self.fetch_members,
            fetch_pool=self.fetch_pool,
            fetch_trade_date=self.fetch_trade_date,
            source_fresh=self.fresh,
            now=NOW,
            day=DAY,
        )
        base.update(over)
        return base

    def plan(self, **over):
        return cap.plan_capture(**self.kwargs(**over))


class FakeClock:
    def __init__(self, start: float = 0.0):
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


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


def _board_rows(engine: Engine) -> list[dict]:
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(
            select(store.THEME_OBS_BOARD).order_by(store.THEME_OBS_BOARD.c.board_code)
        ).mappings()]


def _pool_rows(engine: Engine) -> list[dict]:
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(
            select(store.THEME_OBS_LIMITUP).order_by(store.THEME_OBS_LIMITUP.c.stock_code)
        ).mappings()]


def _count(engine: Engine, table) -> int:
    with engine.connect() as conn:
        return int(conn.execute(select(func.count()).select_from(table)).scalar_one())


# ────────────────────────────────────────────────────────────────────────
# 1. 纯性与依赖注入
# ────────────────────────────────────────────────────────────────────────
class TestPurityAndDeps:
    def test_no_deps_returns_structured_refusal(self):
        """不注入依赖 ⇒ 结构化 refused, 一次外部调用都不发(默认不联网)。"""
        out = cap.plan_capture(now=NOW, day=DAY)
        assert out.status == cap.STATUS_REFUSED
        assert out.reason == cap.REASON_DEPS_MISSING
        assert out.boards == ()
        assert out.pool_state == cap.POOL_UNKNOWN
        assert out.pool_count is None
        assert cap.REASON_DEPS_MISSING in " ".join(out.notes)

    def test_plan_capture_has_no_bind_parameter(self):
        """规划函数不接库: 它没有任何落库能力。"""
        params = inspect.signature(cap.plan_capture).parameters
        assert "bind" not in params
        assert "now" in params

    def test_now_is_required(self):
        assert inspect.signature(cap.plan_capture).parameters["now"].default is inspect.Parameter.empty
        with pytest.raises(TypeError):
            cap.plan_capture(day=DAY)  # type: ignore[call-arg]

    def test_import_pulls_no_network_module(self):
        """import 本模块不得拉进行情/采集器/HTTP 客户端。"""
        code = (
            "import sys;"
            "import src.core.theme_evolution_capture;"
            "bad=[m for m in sys.modules if m.startswith(('marketdata','requests','httpx','aiohttp'))"
            " or m in ('src.core.tdx_boards','src.collectors.market_sentiment_collector',"
            "'src.collectors.wudao_mcp_client')];"
            "print(','.join(bad))"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
            cwd=str(__import__("pathlib").Path(__file__).resolve().parent.parent),
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "", f"import 拉进了这些模块: {proc.stdout.strip()}"

    def test_module_has_no_report_or_advice_builder(self):
        """不生成交易建议/不渲染报告: 模块里不存在这类构造函数。"""
        banned = ("render", "suggest", "advice", "recommend", "signal", "rank")
        offenders = [
            name for name, obj in vars(cap).items()
            if callable(obj) and not name.startswith("_")
            and any(word in name.lower() for word in banned)
        ]
        assert offenders == []

    def test_bad_day_is_rejected(self):
        h = Harness()
        with pytest.raises(ValueError):
            h.plan(day="2026-11-16")
        with pytest.raises(ValueError):
            h.plan(day="20261332")

    def test_day_defaults_to_now_date(self):
        h = Harness()
        out = h.plan(day=None)
        assert out.day == DAY
        assert out.status == cap.STATUS_COMPLETE

    def test_now_must_be_datetime(self):
        h = Harness()
        with pytest.raises(ValueError):
            h.plan(now="2026-11-16 15:16")

    def test_naive_now_is_taken_as_beijing_wall_clock(self):
        """naive 时间不搬到 UTC: 原样当北京时间被接受, 日期不会被挪一天。"""
        naive = dt.datetime(2026, 11, 16, 15, 16)
        out = Harness().plan(now=naive)
        assert out.day == DAY
        assert out.captured_at == naive


# ────────────────────────────────────────────────────────────────────────
# 2. 当前成分不能当作历史日
# ────────────────────────────────────────────────────────────────────────
class TestWrongDay:
    def test_past_day_refused_without_any_fetch(self):
        """目标日≠今天 ⇒ refused, 且一次取数都不做(当前成分不是历史成分)。"""
        h = Harness()
        out = h.plan(day=PREV_DAY)
        assert out.status == cap.STATUS_REFUSED
        assert out.reason == cap.REASON_WRONG_DAY
        assert out.boards == ()
        assert (h.board_calls, h.member_calls, h.pool_calls) == (0, [], [])

    def test_future_day_also_refused(self):
        h = Harness()
        assert h.plan(day="20261117").reason == cap.REASON_WRONG_DAY

    def test_day_uses_beijing_date_not_utc(self):
        """UTC 时间 2026-11-15T17:30Z = 北京 11-16 01:30, 目标日应是 11-16。"""
        utc_now = dt.datetime(2026, 11, 15, 17, 30, tzinfo=dt.timezone.utc)
        h = Harness()
        out = h.plan(day=None, now=utc_now)
        assert out.day == DAY
        assert out.captured_at == dt.datetime(2026, 11, 16, 1, 30)


# ────────────────────────────────────────────────────────────────────────
# 3. 板块目录
# ────────────────────────────────────────────────────────────────────────
class TestBoardCatalog:
    def test_empty_catalog_refused(self):
        h = Harness(boards=[])
        out = h.plan()
        assert (out.status, out.reason) == (cap.STATUS_REFUSED, cap.REASON_CATALOG_EMPTY)
        assert out.boards == ()

    def test_catalog_exception_refused_without_leaking_secret(self):
        h = Harness(boards=RuntimeError(f"GET https://x/y?{SECRET}"))
        out = h.plan()
        assert (out.status, out.reason) == (cap.STATUS_REFUSED, cap.REASON_CATALOG_FAILED)
        text = repr(out.to_dict())
        assert "SECRET1234567" not in text and "token=" not in text and "http" not in text.lower()

    def test_invalid_board_entries_skipped_and_noted(self):
        h = Harness(boards=[{"code": ""}, "880003", {"code": "880004", "name": "AI"}],
                    members={"*": _members()})
        out = h.plan()
        assert [b.board_code for b in out.boards] == ["880004"]
        assert any("board_entry_invalid" in n for n in out.notes)
        assert out.status == cap.STATUS_PARTIAL

    def test_all_entries_invalid_refused(self):
        h = Harness(boards=[{"code": ""}, {"name": "无代码"}], members={"*": _members()})
        out = h.plan()
        assert (out.status, out.reason) == (cap.STATUS_REFUSED, cap.REASON_CATALOG_EMPTY)
        assert out.boards == ()


# ────────────────────────────────────────────────────────────────────────
# 4. 逐板块结果: ok / empty_ok / fetch_failed, 失败不是 0
# ────────────────────────────────────────────────────────────────────────
class TestBoardStates:
    def test_ok_empty_and_failed_are_three_distinct_states(self):
        h = Harness(
            boards=BOARDS + ({"code": "880003", "name": "AI"},),
            members={
                "880001": _members(("000001", "000002")),
                "880002": [],                       # 源确认为空
                "880003": None,                     # 取不到
            },
        )
        out = h.plan()
        by_code = {b.board_code: b for b in out.boards}
        assert by_code["880001"].state == cap.STATE_OK
        assert len(by_code["880001"].members or ()) == 2
        assert by_code["880002"].state == cap.STATE_EMPTY
        assert by_code["880002"].members == ()
        assert by_code["880003"].state == cap.STATE_FAILED
        assert by_code["880003"].members is None
        assert (out.boards_ok, out.boards_empty_ok, out.boards_fetch_failed) == (1, 1, 1)

    def test_failed_board_member_count_is_none_not_zero(self):
        h = Harness(boards=BOARDS, members={"*": None})
        out = h.plan()
        payload = out.to_dict()
        for board in payload["boards"]:
            assert board["member_count"] is None
        assert out.coverage == 0.0
        assert out.status == cap.STATUS_PARTIAL

    def test_member_exception_marked_failed_without_secret(self):
        h = Harness(boards=BOARDS,
                    members={"880001": RuntimeError(f"mcp://gw?{SECRET}"), "880002": _members()})
        out = h.plan()
        failed = next(b for b in out.boards if b.board_code == "880001")
        assert failed.state == cap.STATE_FAILED
        assert failed.members is None
        text = repr(out.to_dict())
        assert "SECRET1234567" not in text and "token=" not in text and "mcp://" not in text

    def test_budget_exhausted_marks_remaining_boards_failed(self):
        clock = FakeClock()

        def fetch_members(code):
            clock.advance(200.0)
            return _members()

        out = cap.plan_capture(
            day=DAY, now=NOW, fetch_boards=lambda: list(BOARDS),
            fetch_members=fetch_members, fetch_pool=lambda day: [_pool_row()],
            source_fresh=True, fetch_trade_date=lambda: DAY,
            budget_s=100.0, monotonic=clock,
        )
        assert out.boards[0].state == cap.STATE_OK
        assert out.boards[1].state == cap.STATE_FAILED
        assert out.boards[1].error == "budget_exhausted"
        assert any("budget_exhausted" in n for n in out.notes)

    def test_duplicate_board_code_kept_once_and_run_not_complete(self):
        h = Harness(boards=(BOARDS[0], {"code": "880001", "name": "半导体(重复)"}))
        out = h.plan()
        assert [b.board_code for b in out.boards] == ["880001"]
        assert any("duplicate_board_code" in n for n in out.notes)
        assert out.status == cap.STATUS_PARTIAL

    def test_max_boards_truncation_is_not_complete(self):
        """只采了目录的一部分 ⇒ 不算 complete(不许把局部当全部)。"""
        h = Harness(boards=BOARDS + ({"code": "880003", "name": "AI"},))
        out = h.plan(max_boards=2)
        assert [b.board_code for b in out.boards] == ["880001", "880002"]
        assert any("scope_truncated:2/3" in n for n in out.notes)
        assert out.status == cap.STATUS_PARTIAL


# ────────────────────────────────────────────────────────────────────────
# 5. 每个板块自己的源有效日期/依据, 未知就是未知
# ────────────────────────────────────────────────────────────────────────
class TestBoardDates:
    def test_raw_sequence_members_have_unknown_date(self):
        """只给成员列表、不给日期 ⇒ data_day/date_basis 都是 None, 绝不填成今天。"""
        h = Harness(members={"*": ["000001", "000002"]})
        out = h.plan()
        board = out.boards[0]
        assert board.data_day is None and board.date_basis is None
        assert any("board_date_unknown" in n for n in board.notes)
        assert out.status == cap.STATUS_PARTIAL

    def test_reported_date_is_preserved_verbatim(self):
        h = Harness(members={"*": _members(data_day=PREV_DAY, basis="source_reported")})
        out = h.plan()
        board = out.boards[0]
        assert (board.data_day, board.date_basis) == (PREV_DAY, "source_reported")
        # 该板块的成员属于上一交易日 ⇒ 不能算当日证据
        assert out.status == cap.STATUS_PARTIAL
        assert board.state == cap.STATE_OK

    def test_half_date_info_becomes_unknown(self):
        h = Harness(members={"*": _members(data_day=DAY, basis=None)})
        out = h.plan()
        board = out.boards[0]
        assert (board.data_day, board.date_basis) == (None, None)
        assert any("board_date_incomplete" in n for n in board.notes)

    def test_unparsable_date_becomes_unknown(self):
        h = Harness(members={"*": _members(data_day="16/11/2026", basis="source_reported")})
        out = h.plan()
        board = out.boards[0]
        assert (board.data_day, board.date_basis) == (None, None)
        assert any("board_date_unparsable" in n for n in board.notes)

    def test_future_date_becomes_unknown(self):
        h = Harness(members={"*": _members(data_day="20261117", basis="source_reported")})
        out = h.plan()
        board = out.boards[0]
        assert (board.data_day, board.date_basis) == (None, None)
        assert any("board_date_after_target" in n for n in board.notes)

    def test_failed_board_never_claims_a_date(self):
        h = Harness(members={"*": None})
        out = h.plan()
        board = out.boards[0]
        assert (board.data_day, board.date_basis) == (None, None)
        assert board.members is None

    def test_overlong_basis_becomes_unknown(self):
        h = Harness(members={"*": _members(basis="x" * 40)})
        board = h.plan().boards[0]
        assert (board.data_day, board.date_basis) == (None, None)
        assert any("board_date_incomplete" in n for n in board.notes)

    def test_duplicate_codes_before_dedupe_are_noted(self):
        h = Harness(members={"*": ["000001", "000001", "000001.SZ", "000002"]})
        board = h.plan().boards[0]
        assert board.members == ("000001", "000001.SZ", "000002")
        assert any("member_duplicates_removed" in n for n in board.notes)


# ────────────────────────────────────────────────────────────────────────
# 6. 涨停池: 逐行核 as_of, 缺/混杂/错日 ⇒ 仅 unknown
# ────────────────────────────────────────────────────────────────────────
class TestPool:
    def test_all_rows_same_day_is_ok(self):
        h = Harness(pool=[_pool_row("000001"), _pool_row("000002", name="万科A")])
        out = h.plan()
        assert out.pool_state == cap.POOL_OK
        assert out.pool_count == 2
        assert out.pool_data_day == DAY
        assert out.pool_date_basis == "kline"

    def test_missing_as_of_on_one_row_is_unknown(self):
        h = Harness(pool=[_pool_row("000001"), _pool_row("000002", as_of=None)])
        out = h.plan()
        assert out.pool_state == cap.POOL_UNKNOWN
        assert out.pool_count is None
        assert out.pool_data_day is None
        assert any("pool_as_of_missing" in n for n in out.notes)
        # 旧池/部分池都不能当今日的池
        assert out.to_dict()["pool_state"] == cap.POOL_UNKNOWN

    def test_mixed_as_of_is_unknown(self):
        h = Harness(pool=[_pool_row("000001", as_of="2026-11-16"),
                          _pool_row("000002", as_of="2026-11-13")])
        out = h.plan()
        assert out.pool_state == cap.POOL_UNKNOWN
        assert out.pool_count is None
        assert any("pool_as_of_mixed" in n for n in out.notes)

    def test_earlier_day_pool_is_unknown_not_today(self):
        """回退到更早日期的池子(A 源会这样)不能顶到目标日。"""
        h = Harness(pool=[_pool_row("000001", as_of="2026-11-13")])
        out = h.plan()
        assert out.pool_state == cap.POOL_UNKNOWN
        assert out.pool_count is None
        assert out.pool_data_day is None
        assert any("pool_as_of_wrong_day" in n for n in out.notes)
        assert out.as_store_batch().limit_up == ()

    def test_unparsable_as_of_is_unknown(self):
        h = Harness(pool=[_pool_row("000001", as_of="16/11/2026")])
        out = h.plan()
        assert out.pool_state == cap.POOL_UNKNOWN
        assert any("pool_as_of_unparsable" in n for n in out.notes)

    def test_empty_list_is_unknown_not_zero(self):
        """空列表无法区分"取数失败"与"真的 0 只涨停" ⇒ unknown, 不是 0。"""
        h = Harness(pool=[])
        out = h.plan()
        assert out.pool_state == cap.POOL_UNKNOWN
        assert out.pool_count is None
        assert any("pool_empty_unverifiable" in n for n in out.notes)

    def test_none_pool_is_unknown(self):
        h = Harness(pool=None)
        h.pool = None
        out = h.plan()
        assert out.pool_state == cap.POOL_UNKNOWN
        assert out.pool_count is None

    def test_pool_exception_is_failed_without_secret(self):
        h = Harness(pool=RuntimeError(f"https://push2ex?{SECRET}"))
        out = h.plan()
        assert out.pool_state == cap.POOL_FAILED
        assert out.pool_count is None
        assert "SECRET1234567" not in repr(out.to_dict())

    def test_row_without_code_is_unknown(self):
        h = Harness(pool=[_pool_row("000001"), {"name": "无代码", "as_of": "2026-11-16"}])
        out = h.plan()
        assert out.pool_state == cap.POOL_UNKNOWN
        assert any("pool_row_without_code" in n for n in out.notes)

    def test_non_mapping_row_is_unknown(self):
        h = Harness(pool=[_pool_row("000001"), "000002"])
        out = h.plan()
        assert out.pool_state == cap.POOL_UNKNOWN
        assert any("pool_row_not_mapping" in n for n in out.notes)

    def test_duplicate_codes_are_deduped(self):
        h = Harness(pool=[_pool_row("000001"), _pool_row("000001", name="平安银行")])
        out = h.plan()
        assert out.pool_state == cap.POOL_OK
        assert out.pool_count == 1
        assert [r.stock_code for r in out.as_store_batch().limit_up] == ["000001"]

    def test_codes_are_preserved(self):
        h = Harness(pool=[_pool_row("000001.SZ"), _pool_row("600000.sh", name="浦发银行")])
        out = h.plan()
        assert sorted(r.stock_code for r in out.as_store_batch().limit_up) == ["000001.SZ", "600000.sh"]

    def test_mixed_basis_recorded_as_unknown_source(self):
        h = Harness(pool=[_pool_row("000001", basis="kline"),
                          _pool_row("000002", basis="source_request")])
        out = h.plan()
        assert out.pool_state == cap.POOL_UNKNOWN
        assert out.pool_date_basis is None
        assert any("pool_date_basis_unverifiable" in n for n in out.notes)
        assert out.status == cap.STATUS_PARTIAL

    def test_missing_pool_dep_yields_unknown_not_refusal(self):
        """只缺涨停池依赖: 板块照采, 池子 unknown(不是整轮 refused)。"""
        h = Harness()
        out = h.plan(fetch_pool=None)
        assert out.status == cap.STATUS_PARTIAL
        assert out.pool_state == cap.POOL_UNKNOWN
        assert any("pool_fetch_not_injected" in n for n in out.notes)

    def test_pool_rows_carry_no_derived_fields(self):
        """池行只保留代码/名称; 不做分类、不加派生判断字段。"""
        h = Harness(pool=[{**_pool_row("000001"), "theme": "半导体", "days": 3}])
        out = h.plan()
        row = out.as_store_batch().limit_up[0]
        assert row.stock_code == "000001"
        assert not hasattr(row, "theme")
        assert not hasattr(row, "kind")

    def test_pool_dict_instead_of_list_is_unknown(self):
        h = Harness(pool={"rows": [_pool_row()]})
        out = h.plan()
        assert out.pool_state == cap.POOL_UNKNOWN
        assert any("pool_not_a_sequence" in n for n in out.notes)

    def test_calendar_invalid_as_of_is_unknown(self):
        """格式对但日历不存在(2026-13-32) ⇒ 解析失败, 不能当日期用。"""
        h = Harness(pool=[_pool_row("000001", as_of="2026-13-32")])
        out = h.plan()
        assert out.pool_state == cap.POOL_UNKNOWN
        assert any("pool_as_of_unparsable" in n for n in out.notes)

    def test_one_row_without_basis_marks_basis_unknown(self):
        h = Harness(pool=[_pool_row("000001", basis="kline"),
                          _pool_row("000002", basis=None)])
        out = h.plan()
        assert out.pool_state == cap.POOL_UNKNOWN
        assert out.pool_date_basis is None
        assert any("pool_date_basis_unverifiable" in n for n in out.notes)

    def test_mapping_result_without_members_is_failed(self):
        h = Harness(members={"880001": {"data_day": DAY}, "880002": _members()})
        board = next(b for b in h.plan().boards if b.board_code == "880001")
        assert board.state == cap.STATE_FAILED
        assert board.error == "members_missing_in_result"

    def test_unsupported_member_result_is_failed(self):
        h = Harness(members={"880001": 42, "880002": _members()})
        board = next(b for b in h.plan().boards if b.board_code == "880001")
        assert board.state == cap.STATE_FAILED
        assert board.error == "unsupported_member_result"


# ────────────────────────────────────────────────────────────────────────
# 7. 批次状态
# ────────────────────────────────────────────────────────────────────────
class TestStatus:
    def test_complete_requires_full_alignment(self):
        out = Harness().plan()
        assert out.status == cap.STATUS_COMPLETE
        assert out.reason is None

    def test_unknown_freshness_is_partial(self):
        out = Harness(fresh=None).plan()
        assert out.status == cap.STATUS_PARTIAL

    def test_unknown_trade_date_is_partial(self):
        out = Harness(trade_date=None).plan()
        assert out.status == cap.STATUS_PARTIAL

    def test_stale_source_refused_without_fetching(self):
        h = Harness(fresh=False)
        out = h.plan()
        assert (out.status, out.reason) == (cap.STATUS_REFUSED, cap.REASON_SOURCE_STALE)
        assert (h.board_calls, h.pool_calls) == (0, [])

    def test_trade_date_behind_target_refused_without_fetching(self):
        h = Harness(trade_date=PREV_DAY)
        out = h.plan()
        assert (out.status, out.reason) == (cap.STATUS_REFUSED, cap.REASON_SOURCE_NOT_UPDATED)
        assert (h.board_calls, h.member_calls, h.pool_calls) == (0, [], [])

    def test_one_failed_board_makes_partial(self):
        out = Harness(members={"880001": _members(), "880002": None}).plan()
        assert out.status == cap.STATUS_PARTIAL

    def test_pool_unknown_makes_partial(self):
        out = Harness(pool=[]).plan()
        assert out.status == cap.STATUS_PARTIAL

    def test_explicit_trade_date_wins_over_probe(self):
        """显式给 source_trade_date 就不再探测(避免两次取数口径打架)。"""
        h = Harness()
        out = h.plan(source_trade_date="2026-11-16")
        assert out.source_trade_date == DAY
        assert out.status == cap.STATUS_COMPLETE

    def test_trade_date_probe_failure_is_unknown_not_guess(self):
        h = Harness(trade_date=RuntimeError(f"tq://x?{SECRET}"))
        out = h.plan()
        assert out.source_trade_date is None
        assert out.status == cap.STATUS_PARTIAL
        assert "SECRET1234567" not in repr(out.to_dict())


# ────────────────────────────────────────────────────────────────────────
# 8. 输出形状: 不带分类/建议字段, 不可变
# ────────────────────────────────────────────────────────────────────────
class TestOutcomeShape:
    def test_to_dict_has_no_ranking_or_advice_keys(self):
        payload = Harness().plan().to_dict()
        banned = ("rank", "score", "advice", "suggest", "signal", "kind", "theme", "recommend")
        flat_keys = set(payload) | {k for b in payload["boards"] for k in b}
        offenders = [k for k in flat_keys if any(w in k.lower() for w in banned)]
        assert offenders == []

    def test_documented_top_level_keys(self):
        payload = Harness().plan().to_dict()
        assert set(payload) >= {
            "day", "run_key", "status", "reason", "captured_at", "source_fresh",
            "source_trade_date", "boards_total", "boards_ok", "boards_empty_ok",
            "boards_fetch_failed", "coverage", "pool_state", "pool_count",
            "pool_data_day", "pool_date_basis", "notes", "boards",
        }

    def test_outcome_is_frozen(self):
        out = Harness().plan()
        with pytest.raises(Exception):
            out.status = cap.STATUS_PARTIAL  # type: ignore[misc]

    def test_run_key_is_deterministic_per_day_and_slot(self):
        assert cap.run_key_for(DAY, "daily") == cap.run_key_for(DAY, "daily")
        assert cap.run_key_for(DAY, "daily") != cap.run_key_for(DAY, "recover1")
        assert cap.run_key_for(DAY, "daily").startswith(cap.RULE_VERSION)
        with pytest.raises(ValueError):
            cap.run_key_for(DAY, "bad slot!")


# ────────────────────────────────────────────────────────────────────────
# 9. 隔离 SQLite: 落库、unknown 落 NULL、同日恢复、幂等
# ────────────────────────────────────────────────────────────────────────
class TestSQLitePersistence:
    def test_persists_board_rows_and_pool(self, engine):
        assert str(engine.url).startswith("sqlite")
        out = cap.capture_day(engine, **Harness().kwargs())
        assert out.append is not None and out.append.inserted is True
        assert out.append.status == out.status == cap.STATUS_COMPLETE
        assert out.append.boards_written == 2
        assert out.append.limit_up_written == 1
        assert out.to_dict()["append"]["inserted"] is True
        assert out.to_dict()["append"]["status"] == cap.STATUS_COMPLETE

        runs = _runs(engine)
        assert len(runs) == 1
        assert runs[0]["status"] == cap.STATUS_COMPLETE
        assert runs[0]["boards_total"] == 2
        assert runs[0]["limit_up_count"] == 1
        assert runs[0]["pool_data_day"] == DAY
        assert _count(engine, store.THEME_OBS_BOARD) == 2
        assert _count(engine, store.THEME_OBS_LIMITUP) == 1

    def test_board_dates_persisted_as_given(self, engine):
        h = Harness(boards=(BOARDS[0], BOARDS[1]),
                    members={"880001": _members(data_day=PREV_DAY, basis="source_reported"),
                             "880002": ["000003"]})
        cap.capture_day(engine, **h.kwargs())
        rows = {r["board_code"]: r for r in _board_rows(engine)}
        assert (rows["880001"]["data_day"], rows["880001"]["date_basis"]) == (PREV_DAY, "source_reported")
        assert (rows["880002"]["data_day"], rows["880002"]["date_basis"]) == (None, None)

    def test_pool_unknown_stores_null_count_and_no_codes(self, engine):
        cap.capture_day(engine, **Harness(pool=[_pool_row("000001", as_of=PREV_DAY)]).kwargs())
        runs = _runs(engine)
        assert runs[0]["pool_state"] == cap.POOL_UNKNOWN
        assert runs[0]["limit_up_count"] is None  # NULL, 不是 0
        assert _count(engine, store.THEME_OBS_LIMITUP) == 0

    def test_failed_board_is_not_zero_in_db(self, engine):
        cap.capture_day(engine, **Harness(members={"880001": None, "880002": _members()}).kwargs())
        rows = {r["board_code"]: r for r in _board_rows(engine)}
        assert rows["880001"]["state"] == cap.STATE_FAILED
        assert rows["880001"]["member_count"] is None
        assert rows["880001"]["members_json"] is None

    def test_refused_batch_recorded_without_evidence(self, engine):
        out = cap.capture_day(engine, **Harness().kwargs(day=PREV_DAY))
        assert out.status == cap.STATUS_REFUSED
        assert out.append is not None and out.append.inserted is True
        runs = _runs(engine)
        assert len(runs) == 1
        assert runs[0]["status"] == store.STATUS_REFUSED
        assert runs[0]["refused_reason"] == cap.REASON_WRONG_DAY
        assert runs[0]["limit_up_count"] is None
        assert _count(engine, store.THEME_OBS_BOARD) == 0
        assert _count(engine, store.THEME_OBS_LIMITUP) == 0

    def test_same_day_recovery_appends_second_attempt(self, engine):
        """第一次部分失败(池 unknown), 同日晚些时候补采成功 ⇒ 追加, 不覆盖。"""
        first = cap.capture_day(engine, **Harness(pool=[]).kwargs(slot="daily"))
        assert first.status == cap.STATUS_PARTIAL

        later = NOW + dt.timedelta(hours=2)
        second = cap.capture_day(engine, **Harness().kwargs(now=later, slot="recover1"))
        assert second.status == cap.STATUS_COMPLETE
        assert second.append is not None
        assert second.append.inserted is True

        runs = _runs(engine)
        assert [r["attempt_seq"] for r in runs] == [1, 2]
        assert [r["status"] for r in runs] == [cap.STATUS_PARTIAL, cap.STATUS_COMPLETE]
        assert _count(engine, store.THEME_OBS_BOARD) == 4  # 两批各 2 个板块, 旧批次没被抹掉
        with engine.connect() as conn:
            latest = store.latest_evidence(conn, DAY)
        assert latest is not None
        assert latest["run"]["attempt_seq"] == 2
        assert latest["run"]["pool_state"] == cap.POOL_OK

    def test_same_slot_retry_is_idempotent(self, engine):
        kw = Harness().kwargs()
        first = cap.capture_day(engine, **kw)
        second = cap.capture_day(engine, **kw)
        assert first.append is not None and second.append is not None
        assert first.append.inserted is True
        assert second.append.inserted is False
        assert second.append.reason == "run_key_exists"
        assert _count(engine, store.THEME_OBS_RUN) == 1
        assert _count(engine, store.THEME_OBS_BOARD) == 2

    def test_catalog_anomaly_remains_partial_in_store(self, engine):
        """目录有异常时，落库状态不可升级为 complete。"""
        h = Harness(boards=(BOARDS[0], {"code": "880001", "name": "重复"}))
        out = cap.capture_day(engine, **h.kwargs())
        assert out.status == cap.STATUS_PARTIAL
        assert out.append is not None
        assert out.append.status == cap.STATUS_PARTIAL

    def test_capture_day_requires_explicit_bind(self):
        with pytest.raises((TypeError, ValueError)):
            cap.capture_day(None, **Harness().kwargs())  # type: ignore[arg-type]

    def test_capture_never_touches_data_dir(self):
        """模块自身不解析 DATA_DIR / 不建引擎: 只接受显式 Engine/Connection。"""
        source = inspect.getsource(cap)
        assert "DATA_DIR" not in source
        assert "create_engine" not in source
        assert "SessionLocal" not in source
