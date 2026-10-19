"""对话标的绑定（2026-10-19）：前端"正在看 XXX"必须绑到会话上。

事故背景：用户在 600111 工作台点「全景数智深度诊断」，请求体里没有标的，模型从持仓里
"猜"了用户主仓 002361（神剑股份），把两只票的分析混在一起；另一种表现是直接回
「没有股票标的」。修法 = 前端带 stock_symbol + 后端绑定到对话 + 提示词禁止替换标的。
"""
from __future__ import annotations

from types import SimpleNamespace

from src.web.api.chat import SendMessageBody, _bind_conv_symbol


class _DB:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _conv(symbol=None, market=None):
    return SimpleNamespace(stock_symbol=symbol, stock_market=market)


def test_binds_symbol_and_market():
    conv, db = _conv(), _DB()
    _bind_conv_symbol(db, conv, SendMessageBody(content="x", stock_symbol="600111"))
    assert (conv.stock_symbol, conv.stock_market) == ("600111", "SH")
    assert db.commits == 1


def test_market_inference_covers_sz_and_bj():
    for code, market in (("002361", "SZ"), ("300750", "SZ"), ("920001", "BJ"), ("430047", "BJ")):
        conv, db = _conv(), _DB()
        _bind_conv_symbol(db, conv, SendMessageBody(content="x", stock_symbol=code))
        assert conv.stock_market == market, code


def test_existing_binding_is_never_overwritten():
    conv, db = _conv("600111", "SH"), _DB()
    _bind_conv_symbol(db, conv, SendMessageBody(content="x", stock_symbol="002361"))
    assert conv.stock_symbol == "600111"
    assert db.commits == 0


def test_invalid_code_is_ignored():
    conv, db = _conv(), _DB()
    _bind_conv_symbol(db, conv, SendMessageBody(content="x", stock_symbol="北方稀土"))
    assert conv.stock_symbol is None
    assert db.commits == 0


def test_missing_symbol_keeps_conversation_unbound():
    conv, db = _conv(), _DB()
    _bind_conv_symbol(db, conv, SendMessageBody(content="今天大盘怎么样"))
    assert conv.stock_symbol is None
