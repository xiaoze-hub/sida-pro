"""代码 → 名字解析(2026-10-21)钉子: `src/core/stock_names.py` + 账本 `name` 字段 + `/api/stocks/names`。

钉的四件事:
  ① `names_for` 命中 / 带前缀后缀也能命中 / **取不到的符号不出现在结果里**(不编造、不回空串);
  ② 采集器抛异常 → 返回 `{}` 不抛(离线环境不许把 API 拖炸);
  ③ `query_log` 每行都带 `name`, 取不到时是 `None`(不是 `""`、不是代码本身);
  ④ core 模块不许 import src.web(B4.1 棘轮门禁)。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import sqlalchemy as sa

from src.core import decision_log as dl
from src.core import stock_names as sn
from src.web.migrations import _m175_decision_log

ROOT = Path(__file__).resolve().parents[1]

FAKE_LIST = [
    {"symbol": "600111", "name": "北方稀土", "market": "CN"},
    {"symbol": "002050", "name": "三花智控", "market": "CN"},
    {"symbol": "300033", "name": "同花顺", "market": "CN"},
    {"symbol": "00700", "name": "腾讯控股", "market": "HK"},
    {"symbol": "BADROW", "name": "", "market": "CN"},  # 无名字的行: 不许进索引
]


@pytest.fixture(autouse=True)
def _reset_names_cache(monkeypatch):
    """每个用例前后清掉进程内索引, 免得 TTL 缓存把上一个用例的假数据带过来。"""
    sn.reset_cache()
    yield
    sn.reset_cache()


def _fake_provider(calls: list[str] | None = None):
    def _get() -> list[dict]:
        if calls is not None:
            calls.append("hit")
        return [dict(r) for r in FAKE_LIST]

    return _get


# ── ① names_for ────────────────────────────────────────────────────────────
def test_hit_returns_name(monkeypatch):
    monkeypatch.setattr(sn, "get_stock_list", _fake_provider())
    assert sn.names_for(["600111"]) == {"600111": "北方稀土"}
    # 一次多个符号: 全部命中
    assert sn.names_for(["600111", "002050"]) == {"600111": "北方稀土", "002050": "三花智控"}


def test_prefix_suffix_forms_hit_and_keep_original_key(monkeypatch):
    monkeypatch.setattr(sn, "get_stock_list", _fake_provider())
    got = sn.names_for(["sz002050", "002050.SZ", "USZA300033", "600111 "])
    # 输出 key 用调用方原写法(strip 后的), 前端可直接 names[symbol]
    assert got == {
        "sz002050": "三花智控",
        "002050.SZ": "三花智控",
        "USZA300033": "同花顺",
        "600111": "北方稀土",
    }


def test_missing_symbols_absent_not_empty(monkeypatch):
    monkeypatch.setattr(sn, "get_stock_list", _fake_provider())
    got = sn.names_for(["600111", "999999", "BADROW"])
    assert got == {"600111": "北方稀土"}
    # 关键口径: 缺的是**键不存在**, 不是空串、不是代码冒充名字
    assert "999999" not in got and "BADROW" not in got
    assert "" not in got.values()


def test_short_or_alpha_codes_are_not_guessed(monkeypatch):
    monkeypatch.setattr(sn, "get_stock_list", _fake_provider())
    # CN 市场: 3 位数字不许补零猜成 6 位码; 纯字母(美股 ticker)也无解
    assert sn.names_for(["1", "AAPL"]) == {}


def test_empty_input_does_not_touch_provider(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(sn, "get_stock_list", _fake_provider(calls))
    assert sn.names_for([]) == {}
    assert sn.names_for(["", None]) == {}  # type: ignore[list-item]  # None 入参也要容错
    assert calls == []


def test_index_is_cached_across_calls(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(sn, "get_stock_list", _fake_provider(calls))
    sn.names_for(["600111"])
    sn.names_for(["002050"])
    sn.names_for(["300033"])
    assert calls == ["hit"], f"索引应复用(只抓一次), 实际抓了 {len(calls)} 次"


# ── ② 失败不抛 ─────────────────────────────────────────────────────────────
def test_provider_exception_returns_empty(monkeypatch):
    def _boom() -> list[dict]:
        raise RuntimeError("network down")

    monkeypatch.setattr(sn, "get_stock_list", _boom)
    assert sn.names_for(["600111"]) == {}  # 不抛
    assert sn.names_for(["600111"]) == {}  # 失败也进短 TTL 缓存, 仍不抛


def test_failed_refresh_keeps_old_index(monkeypatch):
    """刷新失败不该让页面从"有名字"退回"只有代码": 沿用旧索引, 只缩短重试间隔。"""
    monkeypatch.setattr(sn, "get_stock_list", _fake_provider())
    assert sn.names_for(["600111"]) == {"600111": "北方稀土"}

    def _boom() -> list[dict]:
        raise RuntimeError("network down")

    monkeypatch.setattr(sn, "get_stock_list", _boom)
    sn._built_at -= sn.CACHE_TTL_S + 1  # 模拟 TTL 过期(强制重建)
    assert sn.names_for(["600111"]) == {"600111": "北方稀土"}
    assert sn._ok is False  # 但已记为"失败", 下次重试间隔走 FAILURE_TTL_S


def test_normalize_symbol_rules():
    assert sn.normalize_symbol("sz002050") == "002050"
    assert sn.normalize_symbol("002050.SZ") == "002050"
    assert sn.normalize_symbol("USZA300033") == "300033"
    assert sn.normalize_symbol("600111") == "600111"
    assert sn.normalize_symbol("") == ""
    assert sn.normalize_symbol("AAPL") == ""


# ── ③ query_log 每行带 name ────────────────────────────────────────────────
def _engine():
    eng = sa.create_engine("sqlite://")
    with eng.connect() as conn:
        _m175_decision_log(conn)
    return eng


def test_query_log_rows_carry_name(monkeypatch):
    eng = _engine()
    dl.record_signal(eng, signal_kind="resonance3", symbol="600111", trade_date="2026-09-01", price=10.0)
    dl.record_signal(eng, signal_kind="resonance3", symbol="999999", trade_date="2026-09-02", price=8.0)
    monkeypatch.setattr(sn, "get_stock_list", _fake_provider())

    page = dl.query_log(eng, limit=10)
    assert page["total"] == 2
    by_symbol = {it["symbol"]: it for it in page["items"]}
    assert all("name" in it for it in page["items"])
    assert by_symbol["600111"]["name"] == "北方稀土"
    # 取不到 → None(不是空串, 也不是拿 symbol 顶)
    assert by_symbol["999999"]["name"] is None


def test_query_log_name_lookup_failure_is_none(monkeypatch):
    eng = _engine()
    dl.record_signal(eng, signal_kind="gs_buy", symbol="600111", trade_date="2026-09-01", price=10.0)

    def _boom(symbols, market="CN"):
        raise RuntimeError("boom")

    monkeypatch.setattr(dl, "names_for", _boom)
    page = dl.query_log(eng, limit=10)
    assert page["count"] == 1  # 名字失败不影响明细
    assert page["items"][0]["name"] is None
    assert page["total"] == 1 and page["has_more"] is False


def test_query_log_pagination_untouched(monkeypatch):
    eng = _engine()
    for i in range(3):
        dl.record_signal(eng, signal_kind="resonance3", symbol=f"60000{i}",
                         trade_date=f"2026-09-0{i + 1}", price=10.0 + i)
    monkeypatch.setattr(sn, "get_stock_list", _fake_provider())
    p1 = dl.query_log(eng, limit=2, offset=0)
    assert p1["count"] == 2 and p1["total"] == 3 and p1["has_more"] is True
    assert all(it["name"] is None for it in p1["items"])  # 600000~600002 都不在假列表里


# ── ④ 反向依赖门禁 ─────────────────────────────────────────────────────────
def test_core_module_does_not_import_web():
    """与 tests/test_w41_core_web_dependency.py 同一正则(只认"行首的 import 语句"), 不误伤
    docstring 里提到 `src.web` 的说明文字。"""
    src = (ROOT / "src" / "core" / "stock_names.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(from\s+src\.web|import\s+src\.web)", src, re.M)
    # 也不许走 `from src import web` 这类绕法
    assert not re.search(r"^\s*from\s+src\s+import\s+web", src, re.M)


# ── 备: 端点形状(直接调函数, 不起 TestClient) ────────────────────────────
def test_names_endpoint_shape(monkeypatch):
    from src.web.api.stocks import get_stock_names

    monkeypatch.setattr(sn, "get_stock_list", _fake_provider())
    out = get_stock_names(symbols="600111,999999,600111", market="CN", _=None)  # type: ignore[arg-type]
    assert out == {"names": {"600111": "北方稀土"}, "missing": ["999999"]}


def test_names_endpoint_rejects_over_limit():
    from fastapi import HTTPException

    from src.web.api.stocks import STOCK_NAMES_MAX_SYMBOLS, get_stock_names

    too_many = ",".join(str(600000 + i) for i in range(STOCK_NAMES_MAX_SYMBOLS + 1))
    with pytest.raises(HTTPException) as ei:
        get_stock_names(symbols=too_many, market="CN", _=None)  # type: ignore[arg-type]
    assert ei.value.status_code == 400
