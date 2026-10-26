# -*- coding: utf-8 -*-
"""工作台日级图层派生块预落库(workbench_layer_daily)测试 —— **禁真网络**。

覆盖(对应改造验收点):
  ① 落库/读库往返 + 幂等(同 (symbol, market, asof) 只留一行);
  ② 读库命中 ⇒ 日级块**零现算**(把现算函数换成会炸的桩, 仍能正常返回);
  ③ 未命中(盘中当日块未定型 / 无行 / 行过旧 / 读库失败) ⇒ 现算, 且与"纯现算"逐字段
     一致(**口径不变**), 算完定型就落库;
  ④ 降级诚实: 读库失败/落库失败都不阻塞 summary(回现算 / 照样返回);
  ⑤ 定型门禁: 未定型的当日行永不被读侧使用(不拿旧块冒充当日状态);
  ⑥ 收盘批算: 预热集 = 自选 ∪ 持仓 ∪ 最近访问; 单票失败不拖垮整批;
  ⑦ 全空(数据源整体不可用)载荷不落库。

时钟一律注入(now=), 不依赖用例运行时刻。
"""
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.core import layer_precompute as lp  # noqa: E402
from src.models.market import MarketCode  # noqa: E402
from src.web.api import klines as kapi  # noqa: E402

_SYM = "LAYERPREC1"
_LATEST = "2020-01-03"  # 历史日(恒已定型), 不依赖用例运行时刻
_BARS = [
    {"date": "2020-01-02", "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2, "volume": 1000},
    {"date": _LATEST, "open": 10.2, "high": 10.6, "low": 10.0, "close": 10.4, "volume": 1200},
]


def _blocks(marker: str = "computed") -> dict:
    """一个"像真的"日级块载荷(非空: payload_is_empty 会挡全空)。"""
    return {
        "gs_signals": [{"date": _LATEST, "side": "G", "confirmed": False, "price": 10.4}],
        "fund_flow": [{"date": _LATEST, "ming_net": None, "dark_net": 12.3}],
        "activity_series": [{"date": _LATEST, "activity": 55.0, "level": "中"}],
        "events": [{"date": _LATEST, "kind": "limit_up", "label": "涨停"}],
        "unlock_levels": None,
        "chips": "sz000001",
        "resonance": {"available": True, "row": 1, "phase": "发酵", "marker": marker},
        "main_accumulation_chips": None,
    }


def _ensure_table() -> None:
    """保证本表存在(SQLite 测试库可能没跑过迁移)。用真实迁移 DDL, 不另写建表语句。"""
    from src.web.migrations import _m191_workbench_layer_daily_table

    with lp._engine().begin() as conn:
        _m191_workbench_layer_daily_table(conn)


@pytest.fixture(autouse=True)
def _clean_layer_rows():
    """隔离: 进出都清空本表(用例间/文件间不互相喂缓存)。"""
    _ensure_table()
    lp.clear_layer_daily()
    yield
    lp.clear_layer_daily()


def _no_network(monkeypatch) -> None:
    """把日级块现算里的网络/外部件换成确定性桩(不真联网)。"""
    monkeypatch.setattr(kapi, "_build_orderbook", lambda s: {"stub": True})
    monkeypatch.setattr(kapi, "_build_events", lambda s, b: [])
    monkeypatch.setattr(
        kapi, "_build_resonance",
        lambda s, b: {"available": True, "row": 1, "phase": "发酵", "marker": "live"},
    )
    monkeypatch.setattr(kapi, "_tencent_code", lambda s: None)  # 跳过 big_order_flow


def _patch_bars(monkeypatch, bars=_BARS) -> None:
    from src.core import decision_pioneer as dp

    monkeypatch.setattr(dp, "fetch_bars", lambda s, m="CN", days=120: list(bars))


def _trading_day_near(d: date) -> date:
    """找一个交易日(含 d 起向后 30 天), 供"盘中窗口"用例注入 now。"""
    from src.core.trading_calendar import is_trading_day

    cur = d
    for _ in range(30):
        try:
            if is_trading_day(cur):
                return cur
        except Exception:  # noqa: BLE001 — 日历缺年: 退回周一~周五判定
            break
        cur += timedelta(days=1)
    while cur.weekday() >= 5:
        cur += timedelta(days=1)
    return cur


def _off_session_now() -> datetime:
    """收盘定型之后的时刻(读库窗口) —— 不依赖用例实际运行时刻。"""
    return datetime.combine(_trading_day_near(date(2026, 10, 1)), time(16, 0))


# ───────────────────────────── ① 落库/读库往返 ─────────────────────────────


def test_put_get_roundtrip_and_idempotent():
    blocks = _blocks()
    assert lp.put_layer_daily(_SYM, "CN", _LATEST, blocks, source=lp.SOURCE_PRECOMPUTE,
                              settled=True) is True
    row = lp.get_layer_daily(_SYM, "CN")
    assert row is not None
    assert row["asof"] == _LATEST and row["settled"] is True
    assert row["source"] == lp.SOURCE_PRECOMPUTE
    assert row["blocks"] == blocks

    # 幂等: 同 (symbol, market, asof) 再写只覆盖, 不新增行
    blocks2 = _blocks(marker="v2")
    lp.put_layer_daily(_SYM, "CN", _LATEST, blocks2, source=lp.SOURCE_REQUEST, settled=True)
    assert lp.get_layer_daily(_SYM, "CN")["blocks"] == blocks2
    rows = lp._engine().connect().execute(
        lp.text("SELECT COUNT(*) FROM workbench_layer_daily WHERE symbol = :s"), {"s": _SYM}
    ).scalar()
    assert rows == 1


def test_get_missing_returns_none():
    assert lp.get_layer_daily("NO_SUCH_SYMBOL_XYZ", "CN") is None


def test_empty_payload_not_stored():
    """全空(数据源整体不可用)不落库 —— 免得把降级态钉成"该日定稿"。"""
    empty = {"gs_signals": None, "fund_flow": None, "activity_series": None, "events": None,
             "unlock_levels": None, "chips": None, "resonance": {"available": False}}
    assert lp.payload_is_empty(empty) is True
    assert lp.put_layer_daily(_SYM, "CN", _LATEST, empty, settled=True) is False
    assert lp.get_layer_daily(_SYM, "CN") is None
    assert lp.payload_is_empty(_blocks()) is False


# ───────────────────────────── ⑤ 定型 / 窗口门禁 ─────────────────────────────


def test_is_settled_boundaries():
    now = datetime(2026, 10, 14, 10, 0)
    assert lp.is_settled("2026-10-13", now) is True   # 历史日
    assert lp.is_settled("2026-10-14", now) is False  # 今日盘中
    assert lp.is_settled("2026-10-14", datetime(2026, 10, 14, 15, 5)) is True   # 收盘定型时刻
    assert lp.is_settled("2026-10-14", datetime(2026, 10, 14, 15, 4)) is False
    assert lp.is_settled("2026-10-15", now) is False  # 未来日绝不称定型
    assert lp.is_settled("", now) is False
    assert lp.is_settled("20260104", now) is True     # 8 位数字格式归一


def test_live_window_and_usable_gates():
    td = _trading_day_near(date(2026, 10, 1))
    live = datetime.combine(td, time(14, 0))
    after = datetime.combine(td, time(16, 0))
    before = datetime.combine(td, time(8, 0))
    assert lp.in_live_window(live) is True
    assert lp.in_live_window(after) is False
    assert lp.in_live_window(before) is False

    row = {"asof": td.isoformat(), "settled": True}
    assert lp.usable(row, td.isoformat(), after) is True
    assert lp.usable(row, td.isoformat(), before) is True
    assert lp.usable(row, td.isoformat(), live) is False          # 盘中一律现算
    assert lp.usable({**row, "settled": False}, td.isoformat(), after) is False
    assert lp.usable(row, "2099-01-01", after) is False           # 行落后于最近定型日
    assert lp.usable(None, td.isoformat(), after) is False


def test_settled_flag_forced_by_clock():
    """声明 settled=True 但当日未定型 ⇒ 落库行仍 settled=0(读侧永不用)。"""
    td = _trading_day_near(date(2026, 10, 1))
    intraday = datetime.combine(td, time(10, 0))
    assert lp.put_layer_daily(_SYM, "CN", td.isoformat(), _blocks(),
                              settled=True, now=intraday) is True
    row = lp.get_layer_daily(_SYM, "CN")
    assert row["settled"] is False
    assert lp.usable(row, td.isoformat(), datetime.combine(td, time(16, 0))) is False


# ────────────────── ② / ③ 请求期: 命中零现算 · 未命中口径不变 ──────────────────


def test_build_layer_data_hit_zero_recompute(monkeypatch):
    """读库命中 ⇒ 日级块零现算(现算桩直接炸, 仍返回预落库载荷)。"""
    _no_network(monkeypatch)
    _patch_bars(monkeypatch)
    stored = _blocks(marker="stored")
    lp.put_layer_daily(_SYM, "CN", _LATEST, stored, source=lp.SOURCE_PRECOMPUTE, settled=True)

    def _boom(*a, **kw):
        raise AssertionError("命中预落库时不得现算日级块")

    monkeypatch.setattr(kapi, "_compute_daily_blocks", _boom)

    out = kapi._build_layer_data(_SYM, MarketCode.CN, now=_off_session_now())
    for k in lp.DAILY_BLOCK_KEYS:
        assert out[k] == stored[k], k
    assert out["orderbook"] == {"stub": True}  # 实时块仍每次现算


def test_build_layer_data_miss_matches_direct_compute_and_stores(monkeypatch):
    """未命中 ⇒ 现算, 且与直接现算逐字段一致(口径不变); 定型后落库供下次命中。"""
    _no_network(monkeypatch)
    _patch_bars(monkeypatch)
    expected = kapi._compute_daily_blocks(_SYM, _BARS)

    out = kapi._build_layer_data(_SYM, MarketCode.CN, now=_off_session_now())
    for k in lp.DAILY_BLOCK_KEYS:
        assert out[k] == expected[k], k
    assert out["orderbook"] == {"stub": True}

    row = lp.get_layer_daily(_SYM, "CN")
    assert row is not None and row["asof"] == _LATEST
    assert row["settled"] is True and row["source"] == lp.SOURCE_REQUEST
    assert row["blocks"] == expected


def test_build_layer_data_live_window_recomputes_not_reads_cache(monkeypatch):
    """盘中(交易日 09:15–15:05): 即使有定型行也**现算** —— 不拿旧块冒充当日状态。"""
    _no_network(monkeypatch)
    _patch_bars(monkeypatch)
    td = _trading_day_near(date(2026, 10, 1))
    lp.put_layer_daily(_SYM, "CN", _LATEST, _blocks(marker="stale"),
                       source=lp.SOURCE_PRECOMPUTE, settled=True)
    calls = {"n": 0}
    real = kapi._compute_daily_blocks

    def _spy(symbol, bars):
        calls["n"] += 1
        return real(symbol, bars)

    monkeypatch.setattr(kapi, "_compute_daily_blocks", _spy)
    out = kapi._build_layer_data(_SYM, MarketCode.CN,
                                 now=datetime.combine(td, time(14, 0)))
    assert calls["n"] == 1
    assert out["resonance"]["marker"] == "live"  # 现算结果, 不是库里的 "stale"


def test_build_layer_data_row_older_than_latest_settled_recomputes(monkeypatch):
    """库里行落后于最近已定型日(批算漏票) ⇒ 回现算补齐, 不返回缺日的序列。"""
    _no_network(monkeypatch)
    _patch_bars(monkeypatch)
    lp.put_layer_daily(_SYM, "CN", "2020-01-02", _blocks(marker="old"),
                       source=lp.SOURCE_PRECOMPUTE, settled=True)
    calls = {"n": 0}
    real = kapi._compute_daily_blocks

    def _spy(symbol, bars):
        calls["n"] += 1
        return real(symbol, bars)

    monkeypatch.setattr(kapi, "_compute_daily_blocks", _spy)
    out = kapi._build_layer_data(_SYM, MarketCode.CN, now=_off_session_now())
    assert calls["n"] == 1
    assert out["resonance"]["marker"] == "live"


def test_build_layer_data_no_bars_skips_cache(monkeypatch):
    """bars 全空 ⇒ 不读库(无法核对新鲜度), 保持显式"无数据"降级(不编造)。"""
    _no_network(monkeypatch)
    _patch_bars(monkeypatch, [])
    lp.put_layer_daily(_SYM, "CN", _LATEST, _blocks(marker="stored"),
                       source=lp.SOURCE_PRECOMPUTE, settled=True)
    out = kapi._build_layer_data(_SYM, MarketCode.CN, now=_off_session_now())
    assert out["gs_signals"] is None and out["fund_flow"] is None
    assert out["resonance"]["available"] is False


# ────────────────────────── ④ 降级诚实: 读写失败不阻塞 ──────────────────────────


def test_build_layer_data_read_failure_falls_back(monkeypatch):
    _no_network(monkeypatch)
    _patch_bars(monkeypatch)

    def _boom(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(lp, "get_layer_daily", _boom)
    real = kapi._compute_daily_blocks
    calls = {"n": 0}

    def _spy(symbol, bars):
        calls["n"] += 1
        return real(symbol, bars)

    monkeypatch.setattr(kapi, "_compute_daily_blocks", _spy)
    out = kapi._build_layer_data(_SYM, MarketCode.CN, now=_off_session_now())
    assert calls["n"] == 1
    assert out["gs_signals"] == real(_SYM, _BARS)["gs_signals"]


def test_build_layer_data_write_failure_does_not_block(monkeypatch):
    _no_network(monkeypatch)
    _patch_bars(monkeypatch)

    def _boom(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(lp, "put_layer_daily", _boom)
    out = kapi._build_layer_data(_SYM, MarketCode.CN, now=_off_session_now())
    expected = kapi._compute_daily_blocks(_SYM, _BARS)
    for k in lp.DAILY_BLOCK_KEYS:
        assert out[k] == expected[k], k


def test_build_layer_data_compute_failure_yields_degraded(monkeypatch):
    """现算整体炸掉 → 保持显式"无数据"(不抛, 不编造), orderbook 照常 join。"""
    _no_network(monkeypatch)
    _patch_bars(monkeypatch)

    def _boom(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(kapi, "_compute_daily_blocks", _boom)
    out = kapi._build_layer_data(_SYM, MarketCode.CN, now=_off_session_now())
    assert out["gs_signals"] is None and out["fund_flow"] is None
    assert out["resonance"]["available"] is False
    assert out["orderbook"] == {"stub": True}


# ───────────────────────────── ⑥ 收盘批算 (预热集) ─────────────────────────────


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def join(self, *a, **kw):
        return self


class _FakeDB:
    """最小 db 桩: query(...).all() / join(...).all() + execute(text).all()。"""

    def __init__(self, stock_rows, position_rows=(), recent_rows=()):
        self._stock = stock_rows
        self._pos = position_rows
        self._recent = recent_rows
        self._n = 0

    def query(self, *a, **kw):
        self._n += 1
        return _FakeQuery(self._stock if self._n == 1 else self._pos)

    def execute(self, *a, **kw):
        return _FakeResult(self._recent)


def test_collect_targets_union():
    db = _FakeDB(
        stock_rows=[("000001", "CN"), ("600519", "CN"), ("00700", "HK")],
        position_rows=[("000001", "CN"), ("300750", "CN")],
        recent_rows=[("688981", "CN"), ("00700", "HK")],
    )
    got = lp.collect_targets(db)
    assert got == sorted({("000001", "CN"), ("600519", "CN"), ("00700", "HK"),
                          ("300750", "CN"), ("688981", "CN")})


def test_collect_targets_survives_db_failure():
    class _BrokenDB:
        def query(self, *a, **kw):
            raise RuntimeError("no table")

        def execute(self, *a, **kw):
            raise RuntimeError("no table")

    assert lp.collect_targets(_BrokenDB()) == []


def test_precompute_batch_stats_and_isolated_failure(monkeypatch):
    from src.web.api import klines as _k

    def _stub(symbol, market_code, days=120):
        if symbol == "BOOM":
            raise RuntimeError("vendor down")
        return _LATEST, _blocks(marker=symbol)

    monkeypatch.setattr(_k, "build_layer_daily_blocks", _stub)
    out = lp.precompute(symbols=[(_SYM, "CN"), ("BOOM", "CN"), ("00700", "HK")], builder=_stub)
    assert out["total"] == 3 and out["computed"] == 1
    assert out["failed"] == 1 and "BOOM:RuntimeError" in out["errors"][0]
    assert out["skipped"] == 1  # 非 CN(港美股无日级块)
    row = lp.get_layer_daily(_SYM, "CN")
    assert row["blocks"] == _blocks(marker=_SYM) and row["settled"] is True
    assert row["source"] == lp.SOURCE_PRECOMPUTE


def test_precompute_uses_registered_builder():
    """依赖倒置: web 层 import 期注册了现算入口(core 不得反向 import web)。"""
    from src.web.api import klines as _k

    assert lp.get_daily_block_builder() is _k.build_layer_daily_blocks


def test_precompute_without_builder_skips_all(monkeypatch):
    """未注册入口(web 层未加载): 全部 skipped + 告警, 绝不编造。"""
    monkeypatch.setattr(lp, "_daily_block_builder", None)
    out = lp.precompute(symbols=[(_SYM, "CN")])
    assert out["computed"] == 0 and out["skipped"] == 1 and out["errors"] == ["no_builder"]
    assert lp.get_layer_daily(_SYM, "CN") is None


def test_precompute_intraday_does_not_pin_unsettled(monkeypatch):
    """盘中手动跑批: 当日未定型 ⇒ 行写出但不置 settled(读侧不可用)。"""
    td = _trading_day_near(date(2026, 10, 1))
    intraday = datetime.combine(td, time(10, 0))
    stub = lambda symbol, market_code, days=120: (td.isoformat(), _blocks())  # noqa: E731
    out = lp.precompute(symbols=[(_SYM, "CN")], now=intraday, builder=stub)
    assert out["computed"] == 1
    row = lp.get_layer_daily(_SYM, "CN")
    assert row is not None and row["settled"] is False


def test_scheduler_registers_cron_job():
    """调度器: 工作日 15:40 一个 job(不真起后台线程)。"""
    sched = lp.LayerPrecomputeScheduler()
    sched.scheduler.add_job(
        lambda: None, "cron", day_of_week="mon-fri",
        hour=lp.RUN_HOUR, minute=lp.RUN_MINUTE, id="workbench_layer_precompute",
    )
    job = sched.scheduler.get_job("workbench_layer_precompute")
    assert job is not None
    assert (lp.RUN_HOUR, lp.RUN_MINUTE) == (15, 40)
    sched.shutdown()
