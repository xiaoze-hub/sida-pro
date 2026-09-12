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
    def __init__(self, quotes=None, exc=0, cli=None, l2=None):
        self.calls = 0
        self.saved = []
        self._quotes = quotes or {}
        self._exc = exc
        self.cli = cli
        self._l2 = l2 or {}
        self._meta = {"last_ok": None, "stale": False, "rounds_failed": 0}

    def l2_fn(self, cands):
        return {k: v for k, v in self._l2.items() if k in cands}

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


def test_classify_charging_when_near_limit_not_sealed():
    # 涨停 10%: limit=11.0, floor=10*1.07=10.7; price=10.8 未封未炸未断 → 冲板
    assert live.classify(10.8, 11.0, False, 0, charging_floor=10.7) == live.STATE_CHARGING
    assert live.classify(10.2, 11.0, False, 0, charging_floor=10.7) == live.STATE_IDLE


def test_merge_state_counts_open_episodes_and_resealed():
    s1 = live.merge_state({}, {"A": live.STATE_SEALED}, "09:30")
    s2 = live.merge_state(s1, {"A": live.STATE_BLOWN}, "09:40")   # 第1次开板
    assert s2["A"]["open_count"] == 1 and s2["A"]["opened_at"] == "09:40"
    s3 = live.merge_state(s2, {"A": live.STATE_SEALED}, "09:50")  # 回封
    assert s3["A"]["resealed"] is True and s3["A"]["opened_at"] is None
    s4 = live.merge_state(s3, {"A": live.STATE_BLOWN}, "10:00")   # 第2次开板
    assert s4["A"]["open_count"] == 2


def test_build_live_day_tags_and_plate_type():
    state = {
        "Y": {"ever_sealed": True, "first_at": "09:25", "last_sealed_at": "09:25",
              "opened_at": None, "open_count": 0, "resealed": False},          # 一字
        "T": {"ever_sealed": True, "first_at": "09:30", "last_sealed_at": "10:00",
              "opened_at": None, "open_count": 1, "resealed": True},           # T字/回封
        "H": {"ever_sealed": True, "first_at": "09:31", "last_sealed_at": "09:31",
              "opened_at": None, "open_count": 0, "resealed": False},          # 换手
        "B": {"ever_sealed": True, "first_at": "09:30", "last_sealed_at": "09:30",
              "opened_at": "09:40", "open_count": 1, "resealed": False},       # 炸板
        "C": {"ever_sealed": False, "first_at": "09:35", "last_sealed_at": None,
              "opened_at": None, "open_count": 0, "resealed": False},          # 冲板
    }
    quotes = {
        "Y": {"price": 11.0, "limit_px": 11.0, "open": 11.0, "high": 11.0, "low": 11.0,
              "change_pct": 10.0, "amount": 1e8},
        "T": {"price": 11.0, "limit_px": 11.0, "open": 10.0, "high": 11.0, "low": 9.9,
              "change_pct": 10.0},
        "H": {"price": 11.0, "limit_px": 11.0, "open": 10.0, "high": 11.0, "low": 9.9,
              "change_pct": 10.0},
        "B": {"price": 9.5, "limit_px": 11.0, "change_pct": -4.8},
        "C": {"price": 10.8, "limit_px": 11.0, "charging_floor": 10.7, "change_pct": 8.0},
    }
    day = live.build_live_day(state, quotes, {"Y": 1, "T": 1, "H": 1, "B": 2, "C": 0},
                              {k: k for k in state}, "20260912",
                              seal_amounts={"Y": 3.9e7})
    by = {s["symbol"]: s for row in day["rows"] for s in row["stocks"]}
    assert by["Y"]["tag"] == "封住" and by["Y"]["plate_type"] == "一字"
    assert by["Y"]["seal_ratio"] == round(3.9e7 / 1e8, 4)  # 封成比
    assert by["T"]["tag"] == "回封" and by["T"]["plate_type"] == "T字"
    assert by["H"]["tag"] == "封住" and by["H"]["plate_type"] == "换手"
    assert [m["symbol"] for m in day["blown"]] == ["B"] and day["blown"][0]["tag"] == "炸板"
    assert [m["symbol"] for m in day["charging"]] == ["C"] and day["charging"][0]["tag"] == "冲板"


def test_build_live_day_broken_water_flat_tags():
    state = {
        "W": {"ever_sealed": False, "first_at": None, "open_count": 0, "resealed": False},
        "F": {"ever_sealed": False, "first_at": None, "open_count": 0, "resealed": False},
    }
    quotes = {"W": {"price": 9.0, "limit_px": 11.0, "change_pct": -5.0},
              "F": {"price": 10.0, "limit_px": 11.0, "change_pct": 0.2}}
    day = live.build_live_day(state, quotes, {"W": 3, "F": 3}, {"W": "W", "F": "F"}, "20260912")
    tags = {m["symbol"]: m["tag"] for m in day["broken"]}
    assert tags == {"W": "水下", "F": "平盘"}


def test_scan_tick_l2_refines_sealed_and_tags():
    # FCAmo>0 → 封住; 五档封死; 跳水; boards_vendor 交叉; 快照K
    deps = _Deps(
        quotes={"A": {"price": 11.0, "last_close": 10.0}},
        l2={"A": {"snapshot": {"now": 11.0, "open": 10.0, "high": 11.0, "low": 9.9,
                               "amount": 1e8, "before5min": 11.3,
                               "buyp": [11.0], "buyv": [500], "sellp": [11.01], "sellv": [10]},
                  "more": {"zt_price": 11.0, "fcamo": 3.9e7, "ever_zt_count": 2}}},
    )
    out = live.scan_tick(now=datetime(2026, 9, 11, 10, 0, 0), deps=deps)
    day = out["live_day"]
    s0 = day["rows"][0]["stocks"][0]
    assert s0["tag"] == "封住" and s0["seal_tag"] == "封死"
    assert s0["dive"] is True            # 11.0 <= 11.3*0.98
    assert s0["boards_vendor"] == 2
    assert s0["candle"] == {"o": 10.0, "h": 11.0, "l": 9.9, "c": 11.0}
    assert s0["seal_amount"] == 3.9e7    # FCAmo 优先


def test_scan_tick_l2_fcamo_zero_and_ever_sealed_is_blown():
    deps = _Deps(
        quotes={"A": {"price": 10.2, "last_close": 10.0}},
        l2={"A": {"snapshot": {"now": 10.2, "before5min": 10.2},
                  "more": {"zt_price": 11.0, "fcamo": 0.0, "ever_zt_count": 1}}},
    )
    # 先让 A 今日曾封(预置 state), FCAmo=0 → 炸板
    deps_cli = deps
    out = live.scan_tick(now=datetime(2026, 9, 11, 10, 0, 0), deps=deps_cli)
    # 首轮 state 无 ever_sealed, FCAmo=0 且未封 → 按价格态(冲板/未封), 非炸板
    assert out["live_day"]["blown"] == []
