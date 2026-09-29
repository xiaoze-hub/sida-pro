"""预测历史「到期对照」应用侧 enrichment 测试(2026-09-29)。

覆盖 `/api/forecast/history` 新增 outcome 字段的 4 类验收场景:
  ① 有引擎数据但未到期 → pending
  ② 到期且行情可取 → hit/miss 与 outcome_return_pct 正确
  ③ 到期但行情缺失 → no_data 且**不返回 0**(硬约束: 缺数据禁编造)
  ④ 引擎报错 → 仍降级(不可达 503 / enrichment 失败返回引擎原始数据)

另含 2026-09-29 新规则用例(⑤): 目标日=今天时按收盘线(15:00)判定 —— 未过收盘仍
pending, >= 15:00 用当日 K 线评估, 当日 K 线缺失则 no_data。时间经 `now` 注入, 不改系统时钟。

全部 mock 引擎响应与 K 线, **不触真实网络**。
"""
from __future__ import annotations

import asyncio
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.forecast_outcome import (  # noqa: E402
    enrich_history_outcomes,
    evaluate_history_item,
)

_TODAY = date(2026, 9, 29)


def _bars(*pairs: tuple[str, float]) -> list[tuple[date, float]]:
    """('YYYY-MM-DD', close) 序列 → 已解析的 (date, close) 升序序列(评估函数的入参口径)。"""
    return sorted((date.fromisoformat(d), float(c)) for d, c in pairs)


def _raw(*pairs: tuple[str, float]) -> list:
    """('YYYY-MM-DD', close) 序列 → K 线对象列表(行情 loader / 端点链路的入参口径)。"""
    return [SimpleNamespace(date=d, close=c) for d, c in pairs]


def _item(**overrides: Any) -> dict:
    row: dict[str, Any] = {
        "id": 1,
        "symbol": "600519",
        "stock_name": "贵州茅台",
        "last_close": 10.0,
        "last_date": "2026-09-01",
        "target_date": "2026-09-08",
        "pred_days": 5,
        "direction": "up",
        "expected_pct": 8.0,
        "summary": "模型看多",
    }
    row.update(overrides)
    return row


def _enrich(payload: Any, loader, **kwargs: Any) -> Any:
    return asyncio.run(
        enrich_history_outcomes(payload, today=_TODAY, klines_loader=loader, **kwargs)
    )


# ---------- ① 未到期 ----------

def test_pending_when_target_date_in_future_and_no_kline_fetch():
    """目标日在未来 → pending, 且不该为此去取行情(不浪费数据源配额)。"""
    calls: list = []

    def _loader(symbol, days):
        calls.append((symbol, days))
        return _raw(("2026-09-01", 10.0), ("2026-09-08", 11.0))

    out = _enrich({"items": [_item(target_date="2026-10-20")]}, _loader)
    row = out["items"][0]
    assert row["outcome_status"] == "pending"
    assert row["outcome_return_pct"] is None
    assert calls == [], f"未到期不应取 K 线, 实际: {calls}"


def test_pending_when_target_is_today_even_with_today_bar():
    """目标日=今天且未过收盘(<15:00) → pending: T 日收盘价未落定, 盘中实时柱不能当到期收盘(禁编造)。"""
    bars = _bars(("2026-09-25", 10.0), ("2026-09-29", 10.5))  # 含当日实时柱
    assert evaluate_history_item(
        _item(last_date="2026-09-25", target_date="2026-09-29"),
        bars,
        today=_TODAY,
        now=datetime(2026, 9, 29, 11, 30),
    ) == {"outcome_status": "pending"}
    # 且不该为此去取行情
    calls: list = []
    out = _enrich(
        {"items": [_item(last_date="2026-09-25", target_date="2026-09-29")]},
        lambda symbol, days: calls.append(symbol) or [],
        now=datetime(2026, 9, 29, 11, 30),
    )
    assert out["items"][0]["outcome_status"] == "pending"
    assert calls == []


# ---------- ② 到期且行情可取 ----------

def test_due_item_hit_and_miss_with_same_source_baseline():
    """同源 K 线算相对涨跌: up +10% → hit; 同数据 down → miss。"""
    bars = _bars(("2026-09-01", 10.0), ("2026-09-08", 11.0))
    assert evaluate_history_item(_item(direction="up"), bars, today=_TODAY) == {
        "outcome_return_pct": 10.0,
        "outcome_status": "hit",
    }
    assert evaluate_history_item(_item(direction="down"), bars, today=_TODAY) == {
        "outcome_return_pct": 10.0,
        "outcome_status": "miss",
    }


def test_due_item_uses_close_on_or_before_target_date():
    """到期价取"目标日或之前最近一根", 目标日之后的行情不算进来。"""
    bars = _bars(
        ("2026-09-01", 10.0),
        ("2026-09-08", 9.5),
        ("2026-09-20", 12.0),  # 目标日之后的行情
    )
    assert evaluate_history_item(_item(direction="down"), bars, today=_TODAY) == {
        "outcome_return_pct": -5.0,
        "outcome_status": "hit",
    }


def test_zero_return_is_miss_for_up_but_hit_for_flat():
    """涨幅恰为 0: up/down 方向未兑现 → miss; flat 预测 → hit。"""
    bars = _bars(("2026-09-01", 10.0), ("2026-09-08", 10.0))
    assert evaluate_history_item(_item(direction="up"), bars, today=_TODAY) == {
        "outcome_return_pct": 0.0,
        "outcome_status": "miss",
    }
    assert evaluate_history_item(_item(direction="flat"), bars, today=_TODAY) == {
        "outcome_return_pct": 0.0,
        "outcome_status": "hit",
    }


def test_target_date_derived_from_pred_days_when_engine_left_it_empty():
    """老记录 target_date 为空: 按 last_date + pred_days 个交易日推算(走交易日历)。"""
    bars = _bars(("2026-09-10", 10.0), ("2026-09-17", 11.0))
    computed = evaluate_history_item(
        _item(last_date="2026-09-10", target_date="", pred_days=5, direction="up"),
        bars,
        today=_TODAY,
    )
    # 2026-09-10 + 5 个交易日 = 2026-09-17
    assert computed == {"outcome_return_pct": 10.0, "outcome_status": "hit"}


# ---------- ③ 到期但行情缺失: no_data, 不许填 0 ----------

def test_due_item_without_klines_is_no_data_and_not_zero():
    """到期但取不到 K 线 → no_data, outcome_return_pct 必须是 null 而不是 0。"""
    assert evaluate_history_item(_item(), [], today=_TODAY) == {"outcome_status": "no_data"}

    out = _enrich({"items": [_item()]}, lambda symbol, days: [])
    row = out["items"][0]
    assert row["outcome_status"] == "no_data"
    assert row["outcome_return_pct"] is None
    assert row["outcome_return_pct"] != 0


def test_no_data_when_klines_precede_the_prediction_window():
    """K 线只覆盖基准日之前(到不了预测窗口) → no_data, 不倒着算。"""
    bars = _bars(("2026-08-20", 9.0))
    assert evaluate_history_item(_item(last_date="2026-09-01"), bars, today=_TODAY) == {
        "outcome_status": "no_data"
    }


def test_missing_direction_returns_pct_but_no_hitmiss_judgement():
    """方向字段缺失: 涨跌幅真实可算 → 返回; 但不做 hit/miss 判定(标 no_data)。"""
    bars = _bars(("2026-09-01", 10.0), ("2026-09-08", 11.0))
    assert evaluate_history_item(_item(direction=""), bars, today=_TODAY) == {
        "outcome_return_pct": 10.0,
        "outcome_status": "no_data",
    }


def test_baseline_falls_back_to_last_close_when_series_misses_base_day():
    """同源 K 线覆盖不到基准日 → 回落该条预测自带的 last_close。"""
    bars = _bars(("2026-09-08", 12.0))
    assert evaluate_history_item(
        _item(last_close=10.0, direction="up"), bars, today=_TODAY
    ) == {"outcome_return_pct": 20.0, "outcome_status": "hit"}


# ---------- 向后兼容 / 引擎既有值保护 ----------

def test_engine_provided_outcome_kept_when_app_side_has_no_data():
    """应用侧取不到行情时不得"擦掉"引擎已给的评估结果。"""
    item = _item(outcome_return_pct=3.5, outcome_status="hit")
    out = _enrich({"items": [item]}, lambda symbol, days: [])
    row = out["items"][0]
    assert row["outcome_return_pct"] == 3.5
    assert row["outcome_status"] == "hit"
    assert item["outcome_status"] == "hit"  # 入参对象未被原地改写


def test_app_side_result_overrides_engine_value_and_keeps_other_fields():
    """算得出结果时以应用侧为准; 引擎其余字段与包裹结构原样保留。"""
    payload: dict = {
        "items": [_item(outcome_return_pct=99.0, outcome_status="miss")],
        "engine": "forecast_server",
    }
    loader = lambda symbol, days: _raw(  # noqa: E731
        ("2026-09-01", 10.0), ("2026-09-08", 11.0)
    )
    out = _enrich(payload, loader)
    row = out["items"][0]
    assert row["outcome_return_pct"] == 10.0
    assert row["outcome_status"] == "hit"
    assert row["expected_pct"] == 8.0 and row["direction"] == "up"
    assert out["engine"] == "forecast_server"
    assert payload["items"][0]["outcome_return_pct"] == 99.0  # 入参未被改写


def test_list_payload_and_symbol_suffix_normalized():
    """裸 list 入参同样支持; 带 `.SH` 后缀的 symbol 归一后再取行情。"""
    seen: list[str] = []

    def _loader(symbol, days):
        seen.append(symbol)
        return _raw(("2026-09-01", 10.0), ("2026-09-08", 11.0))

    out = _enrich([_item(symbol="600519.SH")], _loader)
    assert isinstance(out, list)
    assert seen == ["600519"]
    assert out[0]["outcome_status"] == "hit"


def test_symbol_cap_leaves_extra_items_untouched():
    """超过单次评估上限的条目保持原样(不误标 no_data), 列表仍可用。"""
    items = [_item(id=1, symbol="600519"), _item(id=2, symbol="000001")]
    out = _enrich(
        {"items": items},
        lambda symbol, days: _raw(("2026-09-01", 10.0), ("2026-09-08", 11.0)),
        max_symbols=1,
    )
    assert out["items"][0]["outcome_status"] == "hit"
    assert "outcome_status" not in out["items"][1]
    assert "outcome_return_pct" not in out["items"][1]


def test_unexpected_payload_shape_returned_unchanged():
    """引擎返回结构异常(无 items 列表)时原样透传, 不抛错。"""
    payload = {"items": "oops"}
    assert _enrich(payload, lambda symbol, days: []) is payload


# ---------- ⑤ 目标日=今天 的收盘线判定(2026-09-29 新规则) ----------

_CLOSE_LINE = datetime(2026, 9, 29, 15, 0)  # _TODAY 的收盘时刻


def test_target_today_before_close_is_pending_and_skips_kline_fetch():
    """① 目标日=今天, now=14:59(未过收盘) → pending, 且不取行情。"""
    calls: list = []
    bars = _bars(("2026-09-25", 10.0), ("2026-09-29", 10.5))
    assert evaluate_history_item(
        _item(last_date="2026-09-25", target_date="2026-09-29"),
        bars,
        today=_TODAY,
        now=datetime(2026, 9, 29, 14, 59),
    ) == {"outcome_status": "pending"}

    out = _enrich(
        {"items": [_item(last_date="2026-09-25", target_date="2026-09-29")]},
        lambda symbol, days: calls.append(symbol) or _raw(("2026-09-29", 10.5)),
        now=datetime(2026, 9, 29, 14, 59),
    )
    row = out["items"][0]
    assert row["outcome_status"] == "pending"
    assert row["outcome_return_pct"] is None
    assert calls == [], f"未过收盘不应取 K 线, 实际: {calls}"


def test_target_today_at_close_evaluates_with_today_bar():
    """② 目标日=今天, now=15:00(收盘线, 含边界) 且当日 K 线已在 → 正常评出 hit/miss。"""
    bars = _bars(("2026-09-25", 10.0), ("2026-09-29", 11.0))  # 当日已是终值
    assert evaluate_history_item(
        _item(last_date="2026-09-25", target_date="2026-09-29", direction="up"),
        bars,
        today=_TODAY,
        now=_CLOSE_LINE,
    ) == {"outcome_return_pct": 10.0, "outcome_status": "hit"}
    assert evaluate_history_item(
        _item(last_date="2026-09-25", target_date="2026-09-29", direction="down"),
        bars,
        today=_TODAY,
        now=datetime(2026, 9, 29, 15, 30),
    ) == {"outcome_return_pct": 10.0, "outcome_status": "miss"}

    seen: list[str] = []
    out = _enrich(
        {"items": [_item(last_date="2026-09-25", target_date="2026-09-29")]},
        lambda symbol, days: seen.append(symbol)
        or _raw(("2026-09-25", 10.0), ("2026-09-29", 11.0)),
        now=_CLOSE_LINE,
    )
    row = out["items"][0]
    assert seen == ["600519"]  # 过了收盘才取行情
    assert row["outcome_return_pct"] == 10.0
    assert row["outcome_status"] == "hit"


def test_target_today_after_close_without_today_bar_is_no_data_not_zero():
    """③ 目标日=今天, now>=15:00, 但当日 K 线缺失 → no_data 且 pct 显式为 None(禁填 0)。"""
    bars_missing_today = _bars(("2026-09-25", 10.0))  # 只有昨收, 没有当日终值
    assert evaluate_history_item(
        _item(last_date="2026-09-25", target_date="2026-09-29"),
        bars_missing_today,
        today=_TODAY,
        now=datetime(2026, 9, 29, 15, 30),
    ) == {"outcome_status": "no_data"}
    # 完全取不到行情同样 no_data
    assert evaluate_history_item(
        _item(last_date="2026-09-25", target_date="2026-09-29"),
        [],
        today=_TODAY,
        now=datetime(2026, 9, 29, 15, 30),
    ) == {"outcome_status": "no_data"}

    out = _enrich(
        {"items": [_item(last_date="2026-09-25", target_date="2026-09-29")]},
        lambda symbol, days: _raw(("2026-09-25", 10.0)),
        now=datetime(2026, 9, 29, 15, 30),
    )
    row = out["items"][0]
    assert row["outcome_status"] == "no_data"
    assert row["outcome_return_pct"] is None
    assert row["outcome_return_pct"] != 0


def test_target_yesterday_forced_evaluated_even_before_close_regression():
    """④ 目标日=昨天(即使 now 还没到今天收盘) → 行为与改动前一致: 照旧评估。"""
    bars = _bars(("2026-09-25", 10.0), ("2026-09-28", 11.0))
    assert evaluate_history_item(
        _item(last_date="2026-09-25", target_date="2026-09-28", direction="up"),
        bars,
        today=_TODAY,
        now=datetime(2026, 9, 29, 9, 30),  # 早于收盘, 但目标日不是今天
    ) == {"outcome_return_pct": 10.0, "outcome_status": "hit"}

    out = _enrich(
        {"items": [_item(last_date="2026-09-25", target_date="2026-09-28")]},
        lambda symbol, days: _raw(("2026-09-25", 10.0), ("2026-09-28", 11.0)),
        now=datetime(2026, 9, 29, 9, 30),
    )
    row = out["items"][0]
    assert row["outcome_return_pct"] == 10.0
    assert row["outcome_status"] == "hit"


def test_target_tomorrow_still_pending_regression():
    """④ 目标日 > 今天 → 仍 pending(不改动的分支)。"""
    assert evaluate_history_item(
        _item(last_date="2026-09-25", target_date="2026-09-30"),
        _bars(("2026-09-25", 10.0), ("2026-09-30", 11.0)),
        today=_TODAY,
        now=datetime(2026, 9, 29, 16, 0),
    ) == {"outcome_status": "pending"}


# ---------- ④ 端点级: 引擎降级 ----------

def _history_payload() -> tuple[dict, date, date]:
    """构造引擎返回体 + 两条用于 mock K 线的日期(相对今天, 保证"已到期")。"""
    last_day = date.today() - timedelta(days=30)
    target_day = date.today() - timedelta(days=5)
    payload = {
        "items": [
            {
                "id": 7,
                "symbol": "600519",
                "stock_name": "贵州茅台",
                "last_close": 10.0,
                "last_date": last_day.isoformat(),
                "target_date": target_day.isoformat(),
                "pred_days": 5,
                "direction": "up",
                "expected_pct": 8.0,
                "summary": "模型看多",
            }
        ]
    }
    return payload, last_day, target_day


@pytest.fixture
def main_client():
    """8000 forecast router 隔离 app(不 import 整个 src.web.app)。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.web.api import forecast as forecast_api
    from src.web.database import get_db

    app = FastAPI()
    app.include_router(forecast_api.router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: object()
    return TestClient(app)


def _patch_engine(monkeypatch, payload: dict) -> None:
    from src.web.api import forecast as forecast_api

    class _Resp:
        def __init__(self, data: dict):
            self._data = data

        def raise_for_status(self):
            return None

        def json(self):
            return self._data

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            return _Resp(payload)

    monkeypatch.setattr(forecast_api.httpx, "AsyncClient", _Client)


def test_endpoint_enriches_history_items(monkeypatch, main_client):
    """端到端: 引擎列表 → 应用侧补 outcome_*(mock 引擎 + mock K 线, 无真实网络)。"""
    payload, last_day, target_day = _history_payload()
    _patch_engine(monkeypatch, payload)

    def _loader(symbol, days):
        assert symbol == "600519"
        return _raw((last_day.isoformat(), 10.0), (target_day.isoformat(), 11.0))

    monkeypatch.setattr("src.core.forecast_outcome._load_klines", _loader)

    resp = main_client.get("/api/forecast/history", params={"limit": 30})
    assert resp.status_code == 200
    row = resp.json()["items"][0]
    assert row["outcome_return_pct"] == 10.0
    assert row["outcome_status"] == "hit"
    assert row["summary"] == "模型看多"  # 引擎既有字段原样保留


def test_endpoint_engine_unreachable_still_503(monkeypatch, main_client):
    """④ 引擎不可达 → 仍返回原 503 文案(enrichment 未介入)。"""
    from src.web.api import forecast as forecast_api

    class _Exploding:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            raise httpx.ConnectError("connection refused")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(forecast_api.httpx, "AsyncClient", _Exploding)

    resp = main_client.get("/api/forecast/history")
    assert resp.status_code == 503
    assert "预测引擎不可用(需在主机运行 forecast_server.py)" in resp.json()["detail"]


def test_endpoint_degrades_to_raw_engine_payload_when_enrichment_fails(monkeypatch, main_client):
    """④ enrichment 整体失败 → 200 + 引擎原始数据, 不把接口搞挂。"""
    payload, _, _ = _history_payload()
    _patch_engine(monkeypatch, payload)

    async def _boom(*a, **k):
        raise RuntimeError("klines down")

    monkeypatch.setattr("src.core.forecast_outcome.enrich_history_outcomes", _boom)

    resp = main_client.get("/api/forecast/history")
    assert resp.status_code == 200
    assert resp.json() == payload
    assert "outcome_status" not in resp.json()["items"][0]
