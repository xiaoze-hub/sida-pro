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
    # stale 走真实时钟会 flaky: 固定它, staleness 另有纯函数单测
    monkeypatch.setattr(api, "_tick_staleness", lambda *a, **k: {"stale": False, "lag_sec": 5})
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
        "stale": False,
        "tick_lag_sec": 5,
        "source": "tencent_ticks",
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


def _noon(day="2026-09-04"):
    import datetime as _dt
    return _dt.datetime(2026, 9, 4, 12, 0, 0)


def test_staleness_fresh():
    import src.web.api.darkflow as api
    # 2026-09-04 是周五; 末笔 11:50 vs 12:00 → lag 600s, 阈值外(>600才 stale)
    assert api._tick_staleness("11:50:00", "2026-09-04", now=_noon()) == {"stale": False, "lag_sec": 600}


def test_staleness_stale():
    import src.web.api.darkflow as api
    assert api._tick_staleness("11:40:00", "2026-09-04", now=_noon()) == {"stale": True, "lag_sec": 1200}


def test_staleness_no_false_alarm():
    import datetime as _dt
    import src.web.api.darkflow as api
    # 盘后 / 跨日 / 无数据 → 一律不 stale
    assert api._tick_staleness("11:40:00", "2026-09-04", now=_dt.datetime(2026, 9, 4, 16, 0, 0))["stale"] is False
    assert api._tick_staleness("11:40:00", "2026-09-03", now=_noon())["stale"] is False
    assert api._tick_staleness(None, "2026-09-04", now=_noon())["stale"] is False


def test_refetch_diff_math(monkeypatch):
    import src.web.api.darkflow as api

    def _resp(ticks, net):
        return {
            "diag": {"tick_count": ticks, "last_tick_t": "12:00:00", "trade_date": "2026-09-04",
                     "tick_pages": 10, "stale": False, "tick_lag_sec": 0},
            "main_intent": {"main_net": net, "data_status": "ok", "verdict_note": None},
        }

    calls = {"n": 0}

    def _fake(symbol):
        calls["n"] += 1
        return _resp(4717, -125035724) if calls["n"] == 1 else _resp(3958, 34904070)

    monkeypatch.setattr(api, "build_darkflow_response", _fake)
    out = api.refetch_darkflow(symbol="002361", owner=object())
    assert out["before"] == {"tick_count": 4717, "main_net": -125035724}
    assert out["after"]["tick_count"] == 3958
    assert out["dedup_removed"] == 759
    assert out["verdict_changed"] is True


def test_alert_throttle_once_per_day():
    """同股同日同类只报一次; 换类/换日重报。"""
    from src.core import darkflow_alerts as al

    class _Mem:
        def __init__(self): self.d = {}
        def get(self, k): return self.d.get(k)
        def set(self, k, v): self.d[k] = v

    m = _Mem()
    assert al.should_alert("sz002361", "2026-09-04", "suspect", cache=m) is True
    assert al.should_alert("sz002361", "2026-09-04", "suspect", cache=m) is False
    assert al.should_alert("sz002361", "2026-09-04", "stale", cache=m) is True
    assert al.should_alert("sz002361", "2026-09-05", "suspect", cache=m) is True


def test_source_ctx_default_and_override():
    """默认走环境变量源; ctx 覆盖隔离可重置。"""
    from src.core import dark_flow as _df
    assert _df._active_source() == _df.DARK_SOURCE
    tok = _df._DARK_SOURCE_CTX.set("thsdk")
    try:
        assert _df._active_source() == "thsdk"
    finally:
        _df._DARK_SOURCE_CTX.reset(tok)
    assert _df._active_source() == _df.DARK_SOURCE


def test_gray_bypasses_shared_cache(monkeypatch):
    """灰度源直拉直返, 不写共享缓存(零残留可回滚)。"""
    from src.core import dark_flow as _df

    canned = [{"d": "B", "amt": 5.0, "vol": 1.0, "price": 10.0, "t": "10:00:00"}]
    import src.core.dark_l2 as _l2
    monkeypatch.setattr(_l2, "fetch_l2_ticks", lambda code, src: list(canned))
    tok = _df._DARK_SOURCE_CTX.set("thsdk")
    try:
        assert "sz000002" not in _df._TICKS_CACHE
        out = _df._fetch_all_ticks("sz000002")
        assert out == canned
        assert "sz000002" not in _df._TICKS_CACHE
    finally:
        _df._DARK_SOURCE_CTX.reset(tok)


def test_build_rejects_bad_source():
    import pytest as _pt
    from fastapi import HTTPException as _HE
    import src.web.api.darkflow as api
    with _pt.raises(_HE) as e:
        api.build_darkflow_response("002361", source="nope")
    assert e.value.status_code == 400
