"""dark-flow 运维杠杆单测 (2026-09-04: main_net 钉死事故)。

- clear_ticks_cache: 纯函数, 不联网。
- diag 转发: monkeypatch 掉 compute_dark_flow/quote/tck, 不联网。
"""
import datetime
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packages/marketdata/src"))

from src.core import dark_flow as df


@pytest.fixture(autouse=True)
def _isolated_cache():
    df._TICKS_CACHE.clear()
    yield
    df._TICKS_CACHE.clear()


def _seed(code="sz002361"):
    df._TICKS_CACHE[code] = (1.0, [{"d": "B", "amt": 1.0, "t": "09:25:00"}], 0, 1, df._cache_day())


def test_clear_single():
    _seed()
    assert df.clear_ticks_cache("sz002361") == 1
    assert "sz002361" not in df._TICKS_CACHE
    assert df.clear_ticks_cache("sz002361") == 0  # 重复清=0, 不报错


def test_clear_all():
    _seed("sz002361")
    _seed("sh600519")
    n = df.clear_ticks_cache(None)
    assert n == 2
    assert df._TICKS_CACHE == {}


def test_diag_forwarded(monkeypatch):
    import src.web.api.darkflow as api

    fake = {
        "main_net": -1, "big_net": -1, "mid_net": 0, "small_net": 1,
        "main_intensity": 80.0, "main_buy_ratio": 44.0, "signal": "s",
        "data_status": "ok", "inner_outer": {},
        "split_order": {"buy_amt": 1, "sell_amt": 0, "net": 1, "groups": []},
        "tick_count": 1234, "last_tick_t": "10:30:00", "tick_pages": 18,
    }
    monkeypatch.setattr(api, "compute_dark_flow", lambda symbol: fake)
    monkeypatch.setattr(api, "_fetch_quote_dict", lambda symbol: None)
    monkeypatch.setattr(api, "compute_tck_active_ratio", lambda code: None)
    # L2 走真实 fetch 会联网: 直接让 decision_pioneer 抛错 → l2=None
    try:
        import src.core.decision_pioneer as dp
        monkeypatch.setattr(dp, "fetch_tq_l2", lambda code: (_ for _ in ()).throw(RuntimeError("no net in test")))
    except ImportError:
        pass

    resp = api.build_darkflow_response("002361")
    assert resp["diag"] == {
        "tick_count": 1234,
        "last_tick_t": "10:30:00",
        "trade_date": datetime.date.today().isoformat(),
        "tick_pages": 18,
    }
    assert resp["dark_order"]["trade_date"] == datetime.date.today().isoformat()


def test_clear_endpoint_single(monkeypatch):
    import src.web.api.darkflow as api

    _seed()
    out = api.clear_darkflow_ticks_cache(symbol="002361", owner=object())
    assert out["cleared"] == 1
    assert out["tcode"] == "sz002361"
    assert out["refetch_next"] is True


def test_drop_future_ticks(monkeypatch):
    """未来 tick 被丢, 正常保留, 空列表原样(冻结时钟, 跑在任何时段都稳)。"""
    import datetime as _dt_module

    real_dt = _dt_module.datetime

    class _Frozen(real_dt):
        @classmethod
        def now(cls, tz=None):
            return real_dt(2026, 9, 4, 12, 0, 0)

    monkeypatch.setattr(_dt_module, "datetime", _Frozen)
    ticks = [
        {"d": "B", "amt": 1.0, "t": "09:25:00"},
        {"d": "B", "amt": 1.0, "t": "12:00:00"},
        {"d": "S", "amt": 1.0, "t": "15:00:00"},  # 未来 → 丢
    ]
    out = df._drop_future_ticks(ticks)
    assert [t["t"] for t in out] == ["09:25:00", "12:00:00"]
    assert df._drop_future_ticks([]) == []


def test_disk_key_per_day():
    assert df._ticks_disk_key() == f"all:{df._cache_day()}"
    assert df._ticks_disk_key("2026-01-01") == "all:2026-01-01"


def test_last_fetch_filled_on_ttl_hit():
    """TTL 命中(不联网)时 wrapper 照样回填 _LAST_FETCH。"""
    import time
    df._TICKS_CACHE["sz000001"] = (
        time.time(), [{"d": "B", "amt": 1.0, "t": "09:25:00"}], 5, 100, df._cache_day(),
    )
    ticks = df._fetch_all_ticks("sz000001")
    assert len(ticks) == 1
    assert df._LAST_FETCH["sz000001"] == {"pages": 6, "ticks": 1}
