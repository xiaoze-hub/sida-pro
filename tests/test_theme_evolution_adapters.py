"""题材演化观察层: **真实数据源 payload 适配器**契约(TDD 先红后绿)。

被测模块: `src/core/theme_evolution_adapters.py`

适配器只做一件事: 把**真实采集源返回的原始 payload** 映射成
`theme_evolution_capture.plan_capture` 的注入契约。它不取数、不读时钟、不发请求、
不建库、不写文件; 没注入的依赖一律 fail-closed。

payload 形状照抄真实实现(**全程未联网**, 手工转录自源码):
- `MarketSentimentCollector._limit_up_pool_tq` → 每行 `as_of='YYYY-MM-DD'`,
  `as_of_basis='kline'`, `pool_coverage={status,requested_codes,fetched_codes,batch_failures}`
- `..._limit_up_pool_wudao` / 东财兜底 → `as_of_basis='source_request'`, coverage
  `status='unknown'/'partial'`, 数值字段可为 None
- `limit_pool_zdt.zdt_fallback_pool`(快照备源, 经 `_maybe_enrich_fallback`) →
  **没有 `as_of` / `as_of_basis`**, 只有 coverage
- `tdx_boards.sector_items()` → `[{'code','name','board_type'}]`(code 带 .SH/.SZ 后缀)
- `tdx_boards.constituents(code)` → `['600519.SH', ...]` 或 `None`(**无成员有效日**)
- `tdx_boards.latest_trade_date()` → `'YYYY-MM-DD'`

硬约束(违反即 bug, 每条都有断言):

1. **依赖注入/fail-closed**: 未注入 → 采集结构化 refused(`deps_not_injected`),
   一次取数都不发; 传了非 callable → 适配层直接拒(`raw_source_not_callable`)。
2. **签名即证据**: 适配函数只有 `raw` 一个参数, 没有任何 day/trade_date/now 入口
   ⇒ 结构上不可能拿请求日或行情日补日期。
3. **逐行 as_of/basis**: 涨停池逐行保留 `as_of`/`as_of_basis`(不统一、不推一个
   公共日期), `pool_coverage` 字典原样保留(含 None, 不补 0, 不改 status)。
4. **unknown 不伪造**: 空池 `[]`、`None`、缺 `as_of` 都是 unknown, 不是 0。
5. **成员有效日只能来自可信源显式字段**: TQ 成分只有代码列表 ⇒ 日期必须留空;
   行情日/请求日不得冒充; 源给了 `as_of` 之类的行情字段也不算成员有效日。
6. **原码保留**: 后缀/大小写/空格一律不修补(非法值交给上游拒绝)。
7. **错误不泄露**: 异常只带结构化 code, 不含 payload/URL/token/异常消息。
8. **不默认 source_fresh=True、不宣称 PIT**: 真实 TQ payload 下不可能判 complete。
9. **完全隔离**: 无网络、无 DB、无真实时钟。
"""

from __future__ import annotations

import datetime as dt
import inspect
import subprocess
import sys
from zoneinfo import ZoneInfo

import pytest

from src.core import theme_evolution_adapters as ad
from src.core import theme_evolution_capture as cap

CST = ZoneInfo("Asia/Shanghai")
NOW = dt.datetime(2026, 11, 16, 15, 16, tzinfo=CST)  # 周一 15:16
DAY = "20261116"
SECRET = "token=SECRET-ab12cd34"


# ── 真实 payload 形状(照抄源码字段, 未联网) ──────────────────────────────
def tq_pool_rows() -> list[dict]:
    """`_limit_up_pool_tq` 的返回形状(build_pool_items + as_of/basis/coverage)。"""
    return [
        {
            "code": "600519",
            "name": "贵州茅台",
            "price": 1500.0,
            "pct": 10.0,
            "amount": 1.2e9,
            "ltsz": 0.0,
            "first_time": "",
            "last_time": "",
            "days": 2,
            "limit_pct": 10.0,
            "sector": "",
            "theme": "",
            "reason": "",
            "turnover_rate": 0.0,
            "order_amount": 0.0,
            "source": "tq",
            "as_of": "2026-11-16",
            "as_of_basis": "kline",
            "pool_coverage": {
                "status": "complete",
                "requested_codes": 5569,
                "fetched_codes": 5590,
                "batch_failures": 0,
            },
        },
        {
            "code": "000001",
            "name": "平安银行",
            "price": 12.3,
            "pct": 9.98,
            "amount": 3.1e8,
            "ltsz": 0.0,
            "first_time": "",
            "last_time": "",
            "days": 1,
            "limit_pct": 10.0,
            "sector": "",
            "theme": "",
            "reason": "",
            "turnover_rate": 0.0,
            "order_amount": 0.0,
            "source": "tq",
            "as_of": "2026-11-16",
            "as_of_basis": "kline",
            "pool_coverage": {
                "status": "complete",
                "requested_codes": 5569,
                "fetched_codes": 5590,
                "batch_failures": 0,
            },
        },
    ]


def wudao_pool_rows() -> list[dict]:
    """`_limit_up_pool_wudao` 形状: 按请求日返回 ⇒ basis=source_request, coverage=unknown。"""
    return [
        {
            "code": "300750",
            "name": "宁德时代",
            "price": 210.0,
            "pct": 20.0,
            "amount": 5.0e8,
            "ltsz": 0.0,
            "first_time": "09:31:00",
            "last_time": "09:31:00",
            "days": 1,
            "sector": "电池",
            "theme": "固态电池",
            "reason": "订单",
            "source": "wudao",
            "as_of": "2026-11-16",
            "as_of_basis": "source_request",
            "pool_coverage": {
                "status": "unknown",
                "requested_codes": None,
                "fetched_codes": 1,
                "batch_failures": None,
            },
        }
    ]


def em_pool_rows() -> list[dict]:
    """东财兜底形状: 有 tc 才有完整性, 没取全 ⇒ partial, batch_failures=None。"""
    return [
        {
            "code": "002230",
            "name": "科大讯飞",
            "days": 3,
            "source": "eastmoney",
            "as_of": "2026-11-16",
            "as_of_basis": "source_request",
            "pool_coverage": {
                "status": "partial",
                "requested_codes": 103,
                "fetched_codes": 60,
                "batch_failures": None,
            },
        }
    ]


def zdt_pool_rows() -> list[dict]:
    """TQ 快照备源(经 `_maybe_enrich_fallback`)形状: **完全没有 as_of/basis**。"""
    return [
        {
            "code": "600000",
            "name": "浦发银行",
            "days": 1,
            "source": "tq_zdt",
            "first_time": "09:45:00",
            "open_times": 0,
            "order_amount": 1200.0,
            "pool_coverage": {
                "status": "unknown",
                "requested_codes": None,
                "fetched_codes": 1,
                "batch_failures": None,
            },
        }
    ]


BOARD_CATALOG = (
    {"code": "881101.SH", "name": "半导体", "board_type": "concept"},
    {"code": "880491.SH", "name": "AI算力", "board_type": "concept"},
)


def members_by_board() -> dict[str, list[str]]:
    """`tdx_boards.constituents` 形状: 带交易所后缀的大写代码, **没有任何日期**。"""
    return {
        "881101.SH": ["600519.SH", "000001.SZ"],
        "880491.SH": ["300750.SZ"],
    }


def make_deps(**over) -> dict:
    """用真实形状的假源组成注入依赖(不联网、不读时钟)。"""
    sources = {
        "board_catalog": lambda: [dict(x) for x in BOARD_CATALOG],
        "members": lambda code: list(members_by_board()[code]),
        "limit_up_pool": lambda day: [dict(r) for r in tq_pool_rows()],
        "trade_date": lambda: "2026-11-16",
    }
    sources.update(over)
    return ad.build_capture_deps(**sources)


def plan(deps: dict | None = None, **over):
    base: dict[str, object] = dict(make_deps() if deps is None else deps)
    base.update(over)
    base["now"] = NOW
    return cap.plan_capture(**base)  # type: ignore[arg-type]


# ── 1. 依赖注入: 缺就拒, 不偷偷联网 ─────────────────────────────────────
class TestDependencyInjection:
    def test_no_sources_yields_no_keys(self):
        assert ad.build_capture_deps() == {}

    def test_none_sources_are_not_injected(self):
        deps = ad.build_capture_deps(board_catalog=None, members=None, limit_up_pool=None, trade_date=None)
        assert deps == {}

    def test_no_deps_capture_refuses_without_calling_anything(self):
        calls: list[str] = []
        deps = ad.build_capture_deps()
        out = plan(deps=deps)
        assert out.status == cap.STATUS_REFUSED
        assert out.reason == cap.REASON_DEPS_MISSING
        assert calls == []
        assert out.boards == ()

    def test_partial_injection_still_refuses_and_fetches_nothing(self):
        calls: list[str] = []

        def boards():
            calls.append("boards")
            return [dict(x) for x in BOARD_CATALOG]

        out = plan(deps=ad.build_capture_deps(board_catalog=boards))
        assert out.status == cap.STATUS_REFUSED
        assert out.reason == cap.REASON_DEPS_MISSING
        assert calls == []  # 缺 fetch_members ⇒ 一次取数都不发

    def test_non_callable_source_fails_closed_with_sanitized_code(self):
        with pytest.raises(ad.AdapterDependencyMissing) as err:
            ad.build_capture_deps(members=[SECRET])
        assert err.value.code == ad.CODE_RAW_SOURCE_NOT_CALLABLE
        assert SECRET not in str(err.value)
        assert SECRET not in repr(err.value.args)

    def test_adapters_have_no_clock_or_day_parameter(self):
        """签名里没有 day/trade_date/now ⇒ 结构上无法拿请求日/行情日补日期。"""
        for fn in (ad.adapt_board_catalog, ad.adapt_members, ad.adapt_limit_up_pool, ad.adapt_trade_date):
            assert list(inspect.signature(fn).parameters) == ["raw"], fn.__name__
        for fn in (
            ad.board_catalog_fetcher,
            ad.member_fetcher,
            ad.limit_up_pool_fetcher,
            ad.trade_date_fetcher,
        ):
            assert list(inspect.signature(fn).parameters) == ["fetch_raw"], fn.__name__
        assert "now" not in inspect.signature(ad.build_capture_deps).parameters

    def test_deps_never_carry_freshness_or_trade_date_values(self):
        deps = make_deps()
        assert set(deps) == {"fetch_boards", "fetch_members", "fetch_pool", "fetch_trade_date"}
        assert "source_fresh" not in deps
        assert "source_trade_date" not in deps
        assert "source_fresh" not in inspect.signature(ad.build_capture_deps).parameters

    def test_import_pulls_no_network_db_or_market_module(self):
        code = (
            "import sys;"
            "import src.core.theme_evolution_adapters;"
            "bad=[m for m in sys.modules if m.startswith(('marketdata','requests','httpx','aiohttp',"
            "'sqlalchemy','psycopg2')) or m in ('src.core.tdx_boards',"
            "'src.collectors.market_sentiment_collector','src.collectors.wudao_mcp_client')];"
            "print(','.join(bad))"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(__import__("pathlib").Path(__file__).resolve().parent.parent),
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "", f"import 拉进了这些模块: {proc.stdout.strip()}"

    def test_module_source_reads_no_clock_and_opens_no_socket(self):
        src = inspect.getsource(ad)
        for token in (
            "datetime.now",
            "utcnow",
            "time.time",
            "monotonic",
            "requests.",
            "httpx.",
            "urlopen",
            "socket.",
            "tq_rpc",
            "SessionLocal",
        ):
            assert token not in src, f"适配层不应出现 {token}"


# ── 2. 涨停池: 逐行 as_of/basis + coverage 原样 ──────────────────────────
class TestPoolPayload:
    def test_per_row_as_of_and_basis_preserved(self):
        rows = tq_pool_rows()
        rows[1]["as_of"] = "2026-11-13"  # 源回退到更早交易日
        rows[1]["as_of_basis"] = "kline"
        out = ad.adapt_limit_up_pool(rows)
        assert [r["as_of"] for r in out] == ["2026-11-16", "2026-11-13"]
        assert [r["as_of_basis"] for r in out] == ["kline", "kline"]

    def test_rows_are_not_collapsed_to_one_date(self):
        rows = wudao_pool_rows() + tq_pool_rows()
        out = ad.adapt_limit_up_pool(rows)
        assert len(out) == 3
        assert [r["as_of_basis"] for r in out] == ["source_request", "kline", "kline"]

    @pytest.mark.parametrize("factory", [tq_pool_rows, wudao_pool_rows, em_pool_rows])
    def test_coverage_dict_preserved_verbatim_including_none(self, factory):
        rows = factory()
        out = ad.adapt_limit_up_pool(rows)
        assert out[0]["pool_coverage"] == rows[0]["pool_coverage"]
        assert isinstance(out[0]["pool_coverage"], dict)
        cov = out[0]["pool_coverage"]
        assert cov["status"] == rows[0]["pool_coverage"]["status"]
        # None 不得被当成 0 补上
        if rows[0]["pool_coverage"]["requested_codes"] is None:
            assert cov["requested_codes"] is None

    def test_legacy_string_coverage_is_not_reshaped(self):
        out = ad.adapt_limit_up_pool([{"code": "600519", "as_of": "2026-11-16", "pool_coverage": "complete"}])
        assert out[0]["pool_coverage"] == "complete"

    def test_only_contract_fields_survive(self):
        out = ad.adapt_limit_up_pool(tq_pool_rows())
        allowed = {
            "code", "stock_code", "name", "source", "as_of", "as_of_basis",
            "pool_coverage",
            # 同源同行的题材归属(只搬运, 不解释)
            "sector", "concepts", "block_as_of", "block_source",
        }
        assert set(out[0]) <= allowed
        for dropped in ("price", "pct", "days", "theme", "reason", "limit_pct"):
            assert dropped not in out[0]

    def test_code_is_kept_verbatim_without_suffix_rewrite(self):
        out = ad.adapt_limit_up_pool([{"code": "600519.SH", "as_of": "2026-11-16"}])
        assert out[0]["code"] == "600519.SH"

    def test_empty_pool_stays_empty_and_capture_says_unknown(self):
        assert ad.adapt_limit_up_pool([]) == []
        out = plan(deps=make_deps(limit_up_pool=lambda day: []))
        assert out.pool_state == cap.POOL_UNKNOWN
        assert out.pool_count is None
        assert out._pool_codes is None
        assert "pool_empty_unverifiable" in out.notes

    def test_none_payload_stays_none_and_is_unknown(self):
        assert ad.adapt_limit_up_pool(None) is None
        out = plan(deps=make_deps(limit_up_pool=lambda day: None))
        assert out.pool_state == cap.POOL_UNKNOWN
        assert out.pool_count is None

    def test_missing_as_of_is_not_filled(self):
        """TQ 快照备源没有 as_of: 适配层不许拿请求日补上。"""
        out_rows = ad.adapt_limit_up_pool(zdt_pool_rows())
        assert "as_of" not in out_rows[0]
        assert "as_of_basis" not in out_rows[0]
        out = plan(deps=make_deps(limit_up_pool=lambda day: zdt_pool_rows()))
        assert out.pool_state == cap.POOL_UNKNOWN
        assert out.pool_count is None
        assert out.pool_data_day is None
        assert "pool_as_of_missing" in out.notes

    def test_requested_day_is_forwarded_but_never_used_as_as_of(self):
        seen: list[str] = []

        def pool(day: str):
            seen.append(day)
            return zdt_pool_rows()

        deps = make_deps(limit_up_pool=pool)
        out = plan(deps=deps)
        assert seen == [DAY]  # 日期确实透传给真实源(它按日取数)
        assert out.pool_data_day is None  # 但没被当成数据日
        assert out.pool_state == cap.POOL_UNKNOWN

    def test_mixed_as_of_becomes_unknown(self):
        rows = tq_pool_rows()
        rows[1]["as_of"] = "2026-11-13"
        out = plan(deps=make_deps(limit_up_pool=lambda day: rows))
        assert out.pool_state == cap.POOL_UNKNOWN
        assert "pool_as_of_mixed" in out.notes

    def test_non_mapping_row_passes_through_and_is_unknown(self):
        out_rows = ad.adapt_limit_up_pool([tq_pool_rows()[0], "garbage"])
        assert out_rows[1] == "garbage"
        out = plan(deps=make_deps(limit_up_pool=lambda day: [tq_pool_rows()[0], "garbage"]))
        assert out.pool_state == cap.POOL_UNKNOWN
        assert "pool_row_not_mapping" in out.notes

    def test_unexpected_payload_type_raises_sanitized_code(self):
        with pytest.raises(ad.AdapterPayloadInvalid) as err:
            ad.adapt_limit_up_pool({"token": SECRET})
        assert err.value.code == ad.CODE_POOL_NOT_A_SEQUENCE
        assert SECRET not in str(err.value)

    def test_pool_rows_carry_no_pit_or_close_claim(self):
        for row in ad.adapt_limit_up_pool(tq_pool_rows() + wudao_pool_rows()):
            keys = " ".join(row).lower()
            for banned in ("pit", "point_in_time", "closed", "final", "settled"):
                assert banned not in keys


# ── 3. 板块: 原码保留 + 成员有效日只能源自报 ──────────────────────────────
class TestBoardPayload:
    def test_catalog_codes_kept_verbatim(self):
        out = ad.adapt_board_catalog(list(BOARD_CATALOG))
        assert [b["code"] for b in out] == ["881101.SH", "880491.SH"]
        assert set(out[0]) == {"code", "name", "board_type"}

    def test_members_kept_verbatim_without_any_date(self):
        out = ad.adapt_members(["600519.SH", "000001.SZ"])
        assert out == ["600519.SH", "000001.SZ"]

    def test_tq_members_yield_unknown_member_effective_day(self):
        out = plan()
        assert out.boards_ok == 2
        for board in out.boards:
            assert board.members is not None
            assert board.data_day is None
            assert board.date_basis is None
            assert "board_date_unknown" in board.notes
        # 关键: 没有任何板块的 data_day 被写成目标日
        assert all(b.data_day != DAY for b in out.boards)

    def test_trade_date_never_substitutes_member_effective_day(self):
        """注入的行情最新日 = 目标日, 也不得变成成员有效日。"""
        out = plan(deps=make_deps(trade_date=lambda: DAY))
        assert out.source_trade_date == DAY
        assert all(b.data_day is None for b in out.boards)

    def test_quote_style_date_field_is_not_a_member_effective_day(self):
        """源给了 as_of(行情/池口径)也不算成员有效日 —— 只认 data_day/date_basis。"""
        out_map = ad.adapt_members({"members": ["600519.SH"], "as_of": "2026-11-16"})
        assert "data_day" not in out_map
        assert "date_basis" not in out_map
        out = plan(deps=make_deps(members=lambda code: {"members": ["600519.SH"], "as_of": "2026-11-16"}))
        assert out.boards[0].data_day is None
        assert out.boards[0].date_basis is None

    def test_trusted_source_explicit_dates_pass_through(self):
        payload = {"members": ["600519.SH"], "data_day": "20261116", "date_basis": "official_release"}
        out = plan(deps=make_deps(members=lambda code: dict(payload)))
        assert out.boards[0].data_day == DAY
        assert out.boards[0].date_basis == "official_release"

    def test_half_date_is_not_completed_by_the_adapter(self):
        out_map = ad.adapt_members({"members": ["600519.SH"], "data_day": "20261116"})
        assert out_map.get("date_basis") is None
        out = plan(deps=make_deps(members=lambda code: {"members": ["600519.SH"], "data_day": "20261116"}))
        assert out.boards[0].data_day is None
        assert out.boards[0].date_basis is None
        assert "board_date_incomplete" in out.boards[0].notes

    def test_members_none_is_fetch_failed_not_empty(self):
        assert ad.adapt_members(None) is None
        out = plan(deps=make_deps(members=lambda code: None))
        assert out.boards_empty_ok == 0
        assert out.boards_fetch_failed == 2
        assert out.boards[0].members is None

    def test_whitespace_code_not_repaired(self):
        out_rows = ad.adapt_members([" 600519.SH"])
        assert out_rows == [" 600519.SH"]
        out = plan(deps=make_deps(members=lambda code: [" 600519.SH"]))
        assert out.boards[0].state == cap.STATE_FAILED
        assert out.boards[0].error == "invalid_member_identifier"

    def test_member_board_code_is_forwarded_verbatim(self):
        seen: list[str] = []

        def members(code: str):
            seen.append(code)
            return ["600519.SH"]

        plan(deps=make_deps(members=members))
        assert seen == ["881101.SH", "880491.SH"]

    def test_catalog_not_a_sequence_raises(self):
        with pytest.raises(ad.AdapterPayloadInvalid) as err:
            ad.adapt_board_catalog({"code": "881101.SH", "token": SECRET})
        assert err.value.code == ad.CODE_CATALOG_NOT_A_SEQUENCE
        assert SECRET not in str(err.value)

    def test_members_payload_unsupported_raises(self):
        with pytest.raises(ad.AdapterPayloadInvalid) as err:
            ad.adapt_members(12345)
        assert err.value.code == ad.CODE_MEMBERS_UNSUPPORTED

    def test_members_mapping_without_members_key_raises(self):
        with pytest.raises(ad.AdapterPayloadInvalid) as err:
            ad.adapt_members({"data_day": "20261116"})
        assert err.value.code == ad.CODE_MEMBERS_MISSING

    def test_trade_date_normalized_to_yyyymmdd(self):
        assert ad.adapt_trade_date("2026-11-16") == DAY
        assert ad.adapt_trade_date("20261116") == DAY
        assert ad.adapt_trade_date(None) is None
        assert ad.adapt_trade_date("2026-13-45") is None
        assert ad.adapt_trade_date("yesterday") is None


# ── 4. 错误不泄露 ───────────────────────────────────────────────────────
class TestErrorLeakage:
    def test_pool_fetch_exception_message_not_in_outcome(self):
        def boom(day: str):
            raise RuntimeError(SECRET)

        out = plan(deps=make_deps(limit_up_pool=boom))
        assert out.pool_state == cap.POOL_FAILED
        assert out.pool_error == "pool_fetch_failed:RuntimeError"
        blob = repr(out.to_dict())
        assert SECRET not in blob

    def test_member_fetch_exception_message_not_in_outcome(self):
        def boom(code: str):
            raise ValueError(SECRET)

        out = plan(deps=make_deps(members=boom))
        assert out.boards_fetch_failed == 2
        assert out.boards[0].error == "member_fetch_error:ValueError"
        assert SECRET not in repr(out.to_dict())

    def test_catalog_exception_message_not_in_outcome(self):
        def boom():
            raise ValueError(SECRET)

        out = plan(deps=make_deps(board_catalog=boom))
        assert out.status == cap.STATUS_REFUSED
        assert out.reason == cap.REASON_CATALOG_FAILED
        assert SECRET not in repr(out.to_dict())

    def test_adapter_errors_carry_code_only(self):
        for payload, code in (
            ({"token": SECRET}, ad.CODE_POOL_NOT_A_SEQUENCE),
            (12345, ad.CODE_POOL_NOT_A_SEQUENCE),
        ):
            with pytest.raises(ad.AdapterPayloadInvalid) as err:
                ad.adapt_limit_up_pool(payload)
            assert err.value.code == code
            assert str(err.value) == code
            assert SECRET not in str(err.value)


# ── 5. 诚实性: 真实 payload 达不到 complete, 缺口写清 ────────────────────
class TestHonesty:
    def test_real_tq_payload_cannot_be_complete(self):
        """最有利条件(池 complete/源新鲜/交易日对齐)下, 真实成分无有效日 ⇒ 只能 partial。"""
        out = plan(source_fresh=True, source_trade_date=DAY)
        assert out.pool_state == cap.POOL_OK
        assert out.boards_ok == 2
        assert out.status == cap.STATUS_PARTIAL
        assert out.status != cap.STATUS_COMPLETE
        assert any("board_date_unknown" in b.notes for b in out.boards)

    def test_source_fresh_is_never_defaulted_to_true(self):
        out = plan()
        assert out.source_fresh is None
        assert out.status == cap.STATUS_PARTIAL

    def test_complete_is_reachable_when_source_really_declares_everything(self):
        """反证: 适配层不是无脑判 partial —— 源显式给出全部日期/覆盖时才可能 complete。"""
        deps = ad.build_capture_deps(
            board_catalog=lambda: [{"code": "881101.SH", "name": "半导体", "board_type": "concept"}],
            members=lambda code: {
                "members": ["600519.SH"],
                "data_day": DAY,
                "date_basis": "official_release",
            },
            limit_up_pool=lambda day: [
                {
                    "code": "600519",
                    "name": "贵州茅台",
                    "source": "tq",
                    "as_of": "2026-11-16",
                    "as_of_basis": "kline",
                    "pool_coverage": {
                        "status": "complete",
                        "requested_codes": 10,
                        "fetched_codes": 10,
                        "batch_failures": 0,
                    },
                }
            ],
            trade_date=lambda: "2026-11-16",
        )
        out = plan(deps=deps, source_fresh=True, source_trade_date=DAY)
        assert out.status == cap.STATUS_COMPLETE
        assert out.coverage == 1.0

    def test_known_gaps_are_declared(self):
        gaps = " ".join(ad.KNOWN_GAPS).lower()
        assert "member" in gaps and ("effective day" in gaps or "effective_day" in gaps)
        assert "as_of" in gaps and ("close" in gaps or "final" in gaps or "定稿" in gaps)
        assert "coverage" in gaps

    def test_module_declares_no_point_in_time_capability(self):
        names = [n.lower() for n in vars(ad) if callable(getattr(ad, n))]
        assert not any("pit" in n or "point_in_time" in n for n in names)
        assert ad.ADAPTER_RULE_VERSION

    def test_rule_version_is_stable(self):
        assert ad.ADAPTER_RULE_VERSION == "theme_evo_adapter_v1"
