"""盘中三态状态机(v0.5.87): classify/merge_state/build_live_day/scan_tick 时段与失败降级。"""
from datetime import datetime

import src.core.limit_ladder_live as live


def test_sealed_within_one_tick():
    assert live.classify(10.00, 10.00, False, 0) == live.STATE_SEALED
    assert live.classify(10.004, 10.00, False, 0) == live.STATE_SEALED  # 1 分容差内


def test_blown_requires_ever_sealed():
    assert live.classify(9.50, 10.00, True, 2) == live.STATE_BLOWN
    assert live.classify(9.50, 10.00, False, 0) == live.STATE_IDLE  # 没封过不叫炸板


def test_broken_excludes_yesterday_first_board():
    assert live.classify(9.50, 10.00, False, 2) == live.STATE_BROKEN
    assert live.classify(9.50, 10.00, False, 1) == live.STATE_IDLE  # 昨首板今未触≠断板


def test_none_price_is_idle_not_zero():
    assert live.classify(None, 10.00, False, 2) == live.STATE_IDLE
    assert live.classify(9.5, None, False, 2) == live.STATE_IDLE


def test_merge_state_keeps_first_at_and_ever_sealed():
    s1 = live.merge_state({}, {"A": live.STATE_SEALED}, "09:31")
    assert s1["A"]["ever_sealed"] is True and s1["A"]["first_at"] == "09:31"
    s2 = live.merge_state(s1, {"A": live.STATE_BLOWN}, "09:32")
    assert s2["A"]["first_at"] == "09:31" and s2["A"]["opened_at"] == "09:32"
    s3 = live.merge_state(s2, {"A": live.STATE_SEALED}, "09:33")
    assert s3["A"]["opened_at"] is None  # 回封清 opened


def test_build_live_day_groups_and_marks():
    state = {"A": {"ever_sealed": True, "first_at": "09:31",
                   "last_sealed_at": "09:31", "opened_at": "09:40"}}
    quotes = {"A": {"price": 9.5, "limit_px": 10.0, "change_pct": -4.8}}
    day = live.build_live_day(state, quotes, {"A": 3}, {"A": "AA"}, "20260912")
    assert day["provisional"] is True
    assert day["date"] == "20260912"
    assert [m["symbol"] for m in day["blown"]] == ["A"]
    assert day["blown"][0]["prev_boards"] == 3
    assert day["rows"] == []  # 炸板的不进板数组


def test_build_live_day_sealed_groups_by_prev_plus_one_and_candle():
    state = {"A": {"ever_sealed": True, "first_at": "09:30",
                   "last_sealed_at": "09:30", "opened_at": None}}
    quotes = {"A": {"price": 11.0, "limit_px": 11.0, "change_pct": 10.0,
                    "open": 10.0, "high": 11.0, "low": 9.9}}
    day = live.build_live_day(state, quotes, {"A": 2}, {"A": "AA"}, "20260912")
    row = day["rows"][0]
    assert row["boards"] == 3 and row["tag"] is None  # 昨2板→今3板, 非首板
    assert row["stocks"][0]["candle"] == {"o": 10.0, "h": 11.0, "l": 9.9, "c": 11.0}


def test_build_live_day_no_ohlc_candle_none():
    state = {"A": {"ever_sealed": True, "first_at": "09:30",
                   "last_sealed_at": "09:30", "opened_at": None}}
    quotes = {"A": {"price": 11.0, "limit_px": 11.0}}  # 无 O/H/L
    day = live.build_live_day(state, quotes, {"A": 0}, {"A": "AA"}, "20260912")
    assert day["rows"][0]["stocks"][0]["candle"] is None  # 不编影线
    assert day["rows"][0]["tag"] == "首板"


class _Deps:
    def __init__(self, quotes=None, exc=0, cli=None):
        self.calls = 0
        self.saved = []
        self._quotes = quotes or {}
        self._exc = exc
        self.cli = cli
        self._meta = {"last_ok": None, "stale": False, "rounds_failed": 0}

    def quotes_fn(self, codes):
        self.calls += 1
        if self._exc:
            self._exc -= 1
            raise RuntimeError("tdx down")
        return dict(self._quotes)

    def universe_fn(self):
        return ["000001"]

    def names_fn(self):
        return {"000001": "平安银行"}

    def yesterday_boards_fn(self):
        return {"000001": 2}

    def save_fn(self, cli, date, state, meta):
        self.saved.append((date, state, meta))
        self._meta = dict(meta)  # 模拟 Redis 持久化, 下轮 meta_fn 读回
        return True

    def load_fn(self, cli, date):
        return {}

    def meta_fn(self, cli, date):
        return dict(self._meta)

    def save_day_fn(self, cli, date, day):
        self.day = day
        return True

    def load_day_fn(self, cli, date):
        return getattr(self, "day", None)


def test_scan_tick_writes_state_in_trading_hours():
    deps = _Deps(quotes={"000001": {"price": 11.0, "last_close": 10.0}})
    out = live.scan_tick(now=datetime(2026, 9, 11, 10, 0, 0), deps=deps)  # 周五盘中
    assert out is not None
    date, state, meta = deps.saved[0]
    assert date == "20260911"
    assert state["000001"]["ever_sealed"] is True  # 10.0*1.1=11.0 在板
    assert meta["rounds_failed"] == 0 and meta["stale"] is False
    day = out["live_day"]
    assert day["provisional"] is True and day["date"] == "20260911"
    assert day["rows"][0]["boards"] == 3  # 昨2板+1
    assert deps.day == day  # 已持久化供 /ladder 读


def test_scan_tick_three_failures_flip_stale():
    deps = _Deps(exc=3)
    for _ in range(3):
        live.scan_tick(now=datetime(2026, 9, 11, 10, 0, 0), deps=deps)
    assert deps.saved[-1][2]["stale"] is True
    assert deps.saved[-1][2]["rounds_failed"] == 3


def test_scan_tick_off_hours_noop():
    deps = _Deps()
    assert live.scan_tick(now=datetime(2026, 9, 12, 10, 0, 0), deps=deps) is None  # 周六
    assert deps.saved == [] and deps.calls == 0


def test_scan_tick_empty_quotes_no_write():
    deps = _Deps(quotes={})
    assert live.scan_tick(now=datetime(2026, 9, 11, 10, 0, 0), deps=deps) is None
    assert deps.saved == []  # TDX 空(非交易/无数据)不写态
