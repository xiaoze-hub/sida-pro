"""L2 引擎注册为 AI 对话工具 — 单元测试(2026-08-22)。

覆盖:
1. get_main_flow_compare: mock compare_main_flow → 验证返回文本含口径标注和一致性
2. get_delta_series: mock fetch_l2_ticks + compute_delta_series → 验证返回文本含 Delta 摘要
3. get_orderbook: mock _to_thsdk_symbol + run → 验证返回文本含盘口事件
4. market != 'CN' => 拒绝
5. 工具失败/异常 => 友好降级不抛异常
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.asyncio

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.web.api.chat import _execute_tool


# ──────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────
@pytest.fixture
def mock_db():
    """Mock SQLAlchemy Session (unused by the 3 tools, but required by signature)."""
    return MagicMock()


# ──────────────────────────────────────────────────────────
# get_main_flow_compare
# ──────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_main_flow_compare_success(mock_db):
    """mock compare_main_flow 返回双源数据 → 验证文本含口径标注和一致性评分。"""
    fake_result = {
        "symbol": "002361",
        "tencent": {"available": True, "main_net": 5000000},
        "thsdk": {"available": True, "main_net": 4800000},
        "consistency": 85.0,
        "delta_pct": 15.0,
        "note": "双源一致性比对(腾讯逐笔 vs thsdk L2)",
        "notes": ["tencent: 可用", "thsdk: 可用"],
    }
    # 函数内使用 from src.core.main_flow_compare import compare_main_flow → 在定义模块上 patch
    with patch("src.core.main_flow_compare.compare_main_flow", return_value=fake_result):
        text = await _execute_tool(mock_db, "get_main_flow_compare", {"symbol": "002361"})

    assert "[数据源: 腾讯逐笔/同花顺L2]" in text
    assert "002361" in text
    assert "85.0" in text or "85/100" in text
    assert "腾讯逐笔" in text
    assert "同花顺L2" in text
    assert "一致性" in text


@pytest.mark.asyncio
async def test_main_flow_compare_market_not_cn(mock_db):
    """market != 'CN' => 明确拒绝。"""
    text = await _execute_tool(mock_db, "get_main_flow_compare", {"symbol": "AAPL", "market": "US"})
    assert "仅支持 A 股" in text


@pytest.mark.asyncio
async def test_main_flow_compare_all_fail(mock_db):
    """双源全部失败 => 友好降级。"""
    fake_result = {
        "symbol": "002361",
        "tencent": None,
        "thsdk": None,
        "consistency": None,
        "delta_pct": None,
        "note": "双源一致性比对; tencent 数据暂不可用; thsdk 数据暂不可用",
        "notes": ["tencent: 数据暂不可用", "thsdk: 数据暂不可用"],
    }
    with patch("src.core.main_flow_compare.compare_main_flow", return_value=fake_result):
        text = await _execute_tool(mock_db, "get_main_flow_compare", {"symbol": "002361"})
    assert "数据均不可用" in text


@pytest.mark.asyncio
async def test_main_flow_compare_exception(mock_db):
    """引擎抛异常 => 不崩, 返回友好文案。"""
    with patch("src.core.main_flow_compare.compare_main_flow", side_effect=ConnectionError("超时")):
        text = await _execute_tool(mock_db, "get_main_flow_compare", {"symbol": "002361"})
    assert "失败" in text or "超时" in text


# ──────────────────────────────────────────────────────────
# get_delta_series
# ──────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_delta_series_success(mock_db):
    """mock fetch_l2_ticks + compute_delta_series → 验证返回文本含 Delta 摘要。"""
    fake_ticks = [
        {"d": "B", "amt": 100000, "vol": 10, "price": 12.5, "t": "09:30:00"},
        {"d": "S", "amt": 50000, "vol": 5, "price": 12.48, "t": "09:30:01"},
        {"d": "B", "amt": 200000, "vol": 20, "price": 12.52, "t": "09:30:02"},
    ]
    fake_delta = {
        "ticks": 3,
        "first_t": "09:30:00",
        "last_t": "09:30:02",
        "seconds": [],
        "signals": [],
        "stats": {
            "seconds": 3,
            "total_buy_yuan": 300000.0,
            "total_sell_yuan": 50000.0,
            "total_neutral_yuan": 0.0,
            "net_yuan": 250000.0,
            "cum_net_last": 250000.0,
            "peak_delta30": 250000.0,
            "trough_delta30": 50000.0,
            "hi_price": 12.52,
            "lo_price": 12.48,
            "signals": 0,
        },
    }
    # 函数内使用 from src.core.dark_l2 import fetch_l2_ticks 和 from src.core.delta_engine import compute_delta_series
    with patch("src.core.dark_l2.fetch_l2_ticks", return_value=fake_ticks), \
         patch("src.core.delta_engine.compute_delta_series", return_value=fake_delta):
        text = await _execute_tool(mock_db, "get_delta_series", {"symbol": "002361"})

    assert "[数据源: THS L2 逐笔]" in text
    assert "002361" in text
    assert "Delta" in text
    assert "250,000" in text


@pytest.mark.asyncio
async def test_delta_series_market_not_cn(mock_db):
    """market != 'CN' => 明确拒绝。"""
    text = await _execute_tool(mock_db, "get_delta_series", {"symbol": "AAPL", "market": "US"})
    assert "仅支持 A 股" in text


@pytest.mark.asyncio
async def test_delta_series_empty_ticks(mock_db):
    """fetch_l2_ticks 返回空列表 => 友好降级。"""
    with patch("src.core.dark_l2.fetch_l2_ticks", return_value=[]):
        text = await _execute_tool(mock_db, "get_delta_series", {"symbol": "002361"})
    assert "无逐笔数据" in text or "失败" in text


@pytest.mark.asyncio
async def test_delta_series_with_signals(mock_db):
    """带背离信号 => 验证信号行被渲染。"""
    fake_ticks = [
        {"d": "B", "amt": 100000, "vol": 10, "price": 12.5, "t": "09:30:00"},
        {"d": "S", "amt": 50000, "vol": 5, "price": 12.48, "t": "09:30:01"},
    ]
    fake_delta = {
        "ticks": 2,
        "first_t": "09:30:00",
        "last_t": "09:30:01",
        "seconds": [],
        "signals": [
            {"type": "底背离", "t": "09:30:01", "price": 12.48, "delta30": -50000.0,
             "cum_net": 50000.0, "since": "09:30:00", "streak": 120},
        ],
        "stats": {
            "seconds": 2,
            "total_buy_yuan": 100000.0,
            "total_sell_yuan": 50000.0,
            "total_neutral_yuan": 0.0,
            "net_yuan": 50000.0,
            "cum_net_last": 50000.0,
            "peak_delta30": 100000.0,
            "trough_delta30": -50000.0,
            "hi_price": 12.5,
            "lo_price": 12.48,
            "signals": 1,
        },
    }
    with patch("src.core.dark_l2.fetch_l2_ticks", return_value=fake_ticks), \
         patch("src.core.delta_engine.compute_delta_series", return_value=fake_delta):
        text = await _execute_tool(mock_db, "get_delta_series", {"symbol": "002361"})
    assert "底背离" in text
    assert "09:30:01" in text


@pytest.mark.asyncio
async def test_delta_series_exception(mock_db):
    """引擎抛异常 => 不崩, 返回友好文案。"""
    with patch("src.core.dark_l2.fetch_l2_ticks", side_effect=RuntimeError("数据源异常")):
        text = await _execute_tool(mock_db, "get_delta_series", {"symbol": "002361"})
    assert "失败" in text or "异常" in text


# ──────────────────────────────────────────────────────────
# get_orderbook
# ──────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_orderbook_success(mock_db):
    """mock run → 验证返回文本含盘口事件和口径标注。"""
    fake_result = {
        "events": [
            {"type": "托单", "side": "bid", "price_level": 2, "price": 11.26,
             "delta_hands": 5000, "duration_s": 3.0, "ts": 1000.0, "note": ""},
            {"type": "压单", "side": "ask", "price_level": 3, "price": 11.30,
             "delta_hands": 6000, "duration_s": 3.0, "ts": 1001.0, "note": ""},
        ],
        "ob_series": [
            {"ts": 1000.0, "dt": "2026-08-22T09:30:00", "bid_amt10": 1e8, "ask_amt10": 8e7, "ob": 0.1111, "label": "中性"},
            {"ts": 1001.0, "dt": "2026-08-22T09:30:01", "bid_amt10": 1.2e8, "ask_amt10": 7e7, "ob": 0.2632, "label": "中性"},
        ],
        "ghost_ratio": 0.0,
        "summary": "symbol=USZA002361 快照数=8 用时=12.0s | 交易时段 | 事件数=2 | OB均值=0.1871 | 幽灵比=0.0",
    }
    # 函数内使用 from src.core.main_flow_compare import _to_thsdk_symbol 和 from src.core.orderbook_engine import run
    with patch("src.core.main_flow_compare._to_thsdk_symbol", return_value="USZA002361"), \
         patch("src.core.orderbook_engine.run", return_value=fake_result):
        text = await _execute_tool(mock_db, "get_orderbook", {"symbol": "002361"})

    assert "[数据源: THS L2 盘口]" in text
    assert "002361" in text
    assert "USZA002361" in text
    assert "托单" in text
    assert "压单" in text
    assert "订单簿失衡" in text


@pytest.mark.asyncio
async def test_orderbook_market_not_cn(mock_db):
    """market != 'CN' => 明确拒绝。"""
    text = await _execute_tool(mock_db, "get_orderbook", {"symbol": "AAPL", "market": "US"})
    assert "仅支持 A 股" in text


@pytest.mark.asyncio
async def test_orderbook_invalid_symbol(mock_db):
    """6位代码转换失败(非6位数字) => 友好降级。"""
    with patch("src.core.main_flow_compare._to_thsdk_symbol", return_value=None):
        text = await _execute_tool(mock_db, "get_orderbook", {"symbol": "invalid"})
    assert "无法将" in text


@pytest.mark.asyncio
async def test_orderbook_exception(mock_db):
    """引擎抛异常 => 不崩, 返回友好文案。"""
    with patch("src.core.main_flow_compare._to_thsdk_symbol", return_value="USZA002361"), \
         patch("src.core.orderbook_engine.run", side_effect=TimeoutError("THS 超时")):
        text = await _execute_tool(mock_db, "get_orderbook", {"symbol": "002361"})
    assert "失败" in text or "超时" in text