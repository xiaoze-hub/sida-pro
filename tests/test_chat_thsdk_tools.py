"""thsdk 工具注册到对话助手 — 单元测试(2026-08-20, 选项 C)。

覆盖:
1. mock thsdk_l2 返回 DataFrame → 验证工具返回统一 dict 结构
   {"available": True, "data": [records], "note": ""}
2. mock thsdk 失败/超时 → 验证 available=False 降级(不抛异常到用户层)
3. 验证 11 个工具注册到对话助手 CHAT_TOOLS(schema list)
4. 验证 system prompt 包含 thsdk 数据源指引

全部工具通过 data_source.thsdk_l2 间接调用, 不直接 import thsdk。
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.agents.chat import tools_thsdk as tt


def _mk_df(rows, cols=None):
    """构造一个简单 DataFrame(带 thsdk 常见中文字段)。"""
    if rows is None:
        return pd.DataFrame()
    return pd.DataFrame(rows, columns=cols)


class FakeL2:
    """mock thsdk_l2 模块: 各方法返回可控结果, 支持注入异常。"""

    def __init__(self, fail_methods=None):
        self.fail_methods = fail_methods or set()

    def _maybe_fail(self, name):
        if name in self.fail_methods:
            raise TimeoutError(f"{name} 超时")

    def get_news(self, symbol=None):
        self._maybe_fail("get_news")
        return _mk_df(
            [
                {"标题": "神剑股份公告", "时间": "2026-08-20", "内容": "xxx", "来源": "同花顺"},
                {"标题": "大盘快评", "时间": "2026-08-20", "内容": "yyy", "来源": "同花顺"},
            ]
        )

    def get_corporate_action(self, symbol="USZA002361"):
        self._maybe_fail("get_corporate_action")
        return _mk_df([{"名称": "神剑股份", "类型": "分红", "方案": "10派1"}])

    def get_dde(self, symbol="USZA002361"):
        self._maybe_fail("get_dde")
        return _mk_df([{"代码": "002361", "DDX": 0.5, "主力净量": 1000}])

    def get_main_flow_official(self, symbol="002361"):
        # 2026-08-21: get_thsdk_dde 改走 get_main_flow_official(THS 无 dde 方法)
        self._maybe_fail("get_dde")
        return {
            "symbol": symbol,
            "ths_code": "USZA" + str(symbol)[-6:],
            "price": 10.46,
            "total_amount_wan": 116836.13,
            "main_net_amount_wan": -5647.64,
            "main_net_ratio": -0.6774,
            "summary": {"代码": symbol, "主力净流入": -56476402},
            "detail": {"主动买入特大单金额": 100},
        }

    def get_hs300_constituents(self):
        self._maybe_fail("get_hs300_constituents")
        return _mk_df([{"代码": "600519", "名称": "贵州茅台"}])

    def get_market_data_cn_extended(self, symbol="USZA002361", extended="扩展1"):
        self._maybe_fail("get_market_data_cn_extended")
        return _mk_df([{"代码": "002361", "最新价": 12.5, "主力净流入": 5.2e7}])

    def get_market_data_index(self, symbol="USHI000001"):
        self._maybe_fail("get_market_data_index")
        return _mk_df([{"名称": "上证指数", "最新价": 3200.0, "涨跌幅": 0.5}])

    def get_market_data_hk(self, symbol="UHKG00700", extended="基础数据"):
        self._maybe_fail("get_market_data_hk")
        return _mk_df([{"代码": "00700", "最新价": 380.0, "涨跌幅": 1.2}])

    def get_market_data_us(self, symbol="UNQQAAPL", extended="基础数据"):
        self._maybe_fail("get_market_data_us")
        return _mk_df([{"代码": "AAPL", "最新价": 220.0, "涨跌幅": 0.8}])

    def get_market_data_bond(self, symbol="USBK113550"):
        self._maybe_fail("get_market_data_bond")
        return _mk_df([{"代码": "113550", "最新价": 118.0, "涨跌幅": 1.5}])

    def get_market_data_fund(self, symbol="USZF510300"):
        self._maybe_fail("get_market_data_fund")
        return _mk_df([{"代码": "510300", "最新价": 4.1, "涨跌幅": 0.6}])

    def get_wencai_enhanced(self, query, use_cache=True):
        self._maybe_fail("get_wencai_enhanced")
        return _mk_df([{"代码": "300033.SZ", "名称": "同花顺"}])


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """默认把 thsdk_l2 换成 FakeL2(不触网); 失败用例通过 fail_methods 注入异常。"""
    monkeypatch.setattr(tt, "_l2", lambda: FakeL2())


# ──────────────────────────────────────────────────────────────────────────
# 1) DataFrame → 统一 dict 结构
# ──────────────────────────────────────────────────────────────────────────


def test_news_returns_dict_structure():
    res = tt.get_thsdk_news("002361")
    assert isinstance(res, dict)
    assert res["available"] is True
    assert isinstance(res["data"], list) and len(res["data"]) > 0
    assert "note" in res
    # 规范化字段: 每条含 title/time/content/source
    first = res["data"][0]
    assert "title" in first and "time" in first and "source" in first


def test_news_limit_respected():
    res = tt.get_thsdk_news("002361", limit=1)
    assert len(res["data"]) <= 1


def test_corporate_action_dict_structure():
    res = tt.get_thsdk_corporate_action("002361")
    assert res["available"] is True
    assert isinstance(res["data"], list) and res["data"]


def test_dde_dict_structure():
    res = tt.get_thsdk_dde("002361")
    assert res["available"] is True
    assert res["data"]


def test_hs300_dict_structure():
    res = tt.get_thsdk_hs300_constituents()
    assert res["available"] is True
    assert res["data"]


def test_market_data_variants_dict_structure():
    for fn, args in [
        (tt.get_thsdk_market_data_cn_extended, ("002361",)),
        (tt.get_thsdk_market_data_index, ("000001",)),
        (tt.get_thsdk_market_data_hk, ("00700",)),
        (tt.get_thsdk_market_data_us, ("AAPL",)),
        (tt.get_thsdk_market_data_bond, ("113550",)),
        (tt.get_thsdk_market_data_fund, ("510300",)),
    ]:
        res = fn(*args)
        assert isinstance(res, dict), f"{fn.__name__} 返回非 dict"
        assert res["available"] is True, f"{fn.__name__} available=False: {res}"
        assert res["data"], f"{fn.__name__} 无数据"


def test_wencai_enhanced_dict_structure():
    res = tt.get_wencai_enhanced("均线多头,非ST")
    assert res["available"] is True
    assert res["data"]


# ──────────────────────────────────────────────────────────────────────────
# 2) thsdk 失败 / 超时 → available=False 降级(不抛异常)
# ──────────────────────────────────────────────────────────────────────────


def test_failure_degrades_to_unavailable(monkeypatch):
    monkeypatch.setattr(tt, "_l2", lambda: FakeL2(fail_methods={"get_news"}))
    res = tt.get_thsdk_news("002361")  # 不应抛异常
    assert res["available"] is False
    assert res["data"] == []
    assert "thsdk" in res["note"]


def test_failure_all_variants_degrades(monkeypatch):
    m = {"get_corporate_action", "get_dde", "get_hs300_constituents",
         "get_market_data_cn_extended", "get_market_data_index",
         "get_market_data_hk", "get_market_data_us", "get_market_data_bond",
         "get_market_data_fund", "get_wencai_enhanced"}
    monkeypatch.setattr(tt, "_l2", lambda: FakeL2(fail_methods=m))
    for fn, args in [
        (tt.get_thsdk_corporate_action, ("002361",)),
        (tt.get_thsdk_dde, ("002361",)),
        (tt.get_thsdk_hs300_constituents, ()),
        (tt.get_thsdk_market_data_cn_extended, ("002361",)),
        (tt.get_thsdk_market_data_index, ("000001",)),
        (tt.get_thsdk_market_data_hk, ("00700",)),
        (tt.get_thsdk_market_data_us, ("AAPL",)),
        (tt.get_thsdk_market_data_bond, ("113550",)),
        (tt.get_thsdk_market_data_fund, ("510300",)),
        (tt.get_wencai_enhanced, ("均线多头",)),
    ]:
        res = fn(*args)
        assert res["available"] is False, f"{fn.__name__} 未降级"
        assert res["data"] == []


def test_empty_data_degrades(monkeypatch):
    class EmptyL2:
        def get_news(self, symbol=None):
            return _mk_df(None)

    monkeypatch.setattr(tt, "_l2", lambda: EmptyL2())
    res = tt.get_thsdk_news("002361")
    assert res["available"] is False
    assert res["data"] == []


def test_invalid_symbol_degrades():
    res = tt.get_thsdk_corporate_action("")   # 缺 symbol → 降级, 不抛异常
    assert res["available"] is False
    res2 = tt.get_wencai_enhanced("")          # 缺 query → 降级
    assert res2["available"] is False


# ──────────────────────────────────────────────────────────────────────────
# 3) 注册到对话助手 CHAT_TOOLS
# ──────────────────────────────────────────────────────────────────────────


def test_tools_registered_in_chat(monkeypatch):
    """11 个工具都出现在 CHAT_TOOLS 里, 且 schema 完整。"""
    from src.web.api import chat as chat_api

    names = {t["function"]["name"] for t in chat_api.CHAT_TOOLS}
    for n in tt.THSDK_TOOL_NAMES:
        assert n in names, f"{n} 未注册到 CHAT_TOOLS"

    # schema 结构: type=function + 中文描述 + 数据源标注 + JSON Schema 参数
    thsdk_schemas = [
        t for t in chat_api.CHAT_TOOLS
        if t["function"]["name"] in tt.THSDK_TOOL_NAMES
    ]
    assert len(thsdk_schemas) == 11
    for t in thsdk_schemas:
        fn = t["function"]
        assert t["type"] == "function"
        assert fn["name"] in tt.THSDK_TOOL_NAMES
        assert "数据源" in fn["description"]          # 数据源标注
        assert fn["parameters"]["type"] == "object"   # JSON Schema 风格


# ──────────────────────────────────────────────────────────────────────────
# 4) system prompt 包含 thsdk 工具说明
# ──────────────────────────────────────────────────────────────────────────


def test_system_prompt_mentions_thsdk():
    from src.web.api import chat as chat_api

    assert "thsdk 数据源" in chat_api.SYSTEM_PROMPT
    # 提示词明确指引: 用户问个股新闻/公司行动/DDE/沪深300/可转债/基金/增强版问财时优先用 thsdk 工具
    assert "个股新闻" in chat_api.SYSTEM_PROMPT
    assert "公司行动" in chat_api.SYSTEM_PROMPT
    assert "DDE" in chat_api.SYSTEM_PROMPT
    assert "沪深300" in chat_api.SYSTEM_PROMPT
    assert "可转债" in chat_api.SYSTEM_PROMPT


# ──────────────────────────────────────────────────────────────────────────
# 5) 端到端: _execute_tool 路由到 thsdk 工具并返回可读文本
# ──────────────────────────────────────────────────────────────────────────


def test_execute_tool_routes_thsdk(monkeypatch):
    import asyncio
    from src.web.api import chat as chat_api

    # 同步 thsdk_l2 mock(不触网)
    monkeypatch.setattr(tt, "_l2", lambda: FakeL2())

    out = asyncio.run(chat_api._exec_thsdk_tool("get_thsdk_news", {"symbol": "002361", "limit": 5}))
    assert isinstance(out, str)
    assert "同花顺" in out or "共" in out
    assert "thsdk" in out.lower()

    # 失败 → 降级文本, 不抛异常
    monkeypatch.setattr(tt, "_l2", lambda: FakeL2(fail_methods={"get_news"}))
    out2 = asyncio.run(chat_api._exec_thsdk_tool("get_thsdk_news", {"symbol": "002361"}))
    assert isinstance(out2, str)
    assert "可用" in out2 or "thsdk" in out2.lower()


def test_execute_tool_unknown_thskd_degrades():
    import asyncio
    from src.web.api import chat as chat_api

    out = asyncio.run(chat_api._exec_thsdk_tool("get_thsdk_not_real", {}))
    assert isinstance(out, str)
