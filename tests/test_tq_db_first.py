# -*- coding: utf-8 -*-
"""库优先 + 口径/时点标注验收(2026-10-22)。

背景: 数据大多已落库, 但有几个 chat 工具请求时直连 TQ 客户端(网关 tdx-gateway), 导致同一
数字在页面(落库 = 某时点)与对话(实时 RPC = 此刻)口径不同。口径改造的硬约束:

1. `pricevol` 实时报价**库优先**: `quote_snapshots` 该标的最近一行行龄 ≤60s 直接用(不调 RPC);
   过期 / 无行 / 无表 → 回落 RPC, 且末行同时标出库内时点与 RPC 时点(过期快照不许充作实时)。
2. 库无对应表的(`relation` 板块归属 / `kzz_info` 转债条款 / `ipo_info` 申购日历 /
   `trackzs_etf` 跟踪 ETF)保持 RPC, 末行标「口径：实时 RPC HH:MM:SS（tdx-gateway）」。
3. `search_symbols` / 标的核对**优先本地全市场名称缓存**(`stock_list.get_stock_list()`),
   缓存无命中才回落 `match_stkinfo`。
4. 只读 SQL(`text()` + 绑定参数 + LIMIT), 不新增表; 取不到如实写「取不到(…)」, 不猜数。
5. 新增取数路径不 import `src.web`(KI-039 棘轮: 反向依赖会把 web 层拖进 agent 层)。
"""

import inspect
import json
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents.chat import registry  # noqa: E402

_CST = ZoneInfo("Asia/Shanghai")
_HMS = re.compile(r"\d{2}:\d{2}:\d{2}")

_QUOTE_DDL = """
CREATE TABLE quote_snapshots (
    trade_date TEXT NOT NULL,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    market TEXT NOT NULL DEFAULT 'CN',
    name TEXT,
    last REAL,
    open REAL,
    high REAL,
    low REAL,
    prev_close REAL,
    change_pct REAL,
    volume REAL,
    turnover REAL,
    source TEXT
)
"""


# ──────────────────────────── 夹具 ────────────────────────────

def _ts(seconds_ago: float) -> str:
    return (datetime.now(_CST) - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%d %H:%M:%S")


def _make_db(tmp_path, *, with_table: bool = True):
    """独立 sqlite 会话: 只建 quote_snapshots(与迁移 157 同列), 不碰真实库。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'db_first.db'}")
    if with_table:
        with engine.begin() as conn:
            conn.execute(text(_QUOTE_DDL))
    return sessionmaker(bind=engine)()


def _insert_quote(db, *, symbol: str = "600519", market: str = "CN", last: float = 1712.5,
                  ts: str | None = None) -> None:
    db.execute(
        text(
            "INSERT INTO quote_snapshots (trade_date, ts, symbol, market, name, last, "
            "prev_close, change_pct, source) VALUES (:d, :ts, :s, :m, :n, :last, :pc, :cp, :src)"
        ),
        {
            "d": str(ts or _ts(0))[:10].replace("-", ""),
            "ts": ts or _ts(5),
            "s": symbol,
            "m": market,
            "n": "贵州茅台",
            "last": last,
            "pc": 1700.0,
            "cp": 0.7,
            "src": "tencent",
        },
    )
    db.commit()


def _no_rpc(*_a, **_kw):
    raise AssertionError("库内快照新鲜时不该调实时 RPC(pricevol)")


# ────────────────── ① pricevol 库优先: 新鲜快照不调 RPC ──────────────────

def test_fresh_snapshot_used_without_rpc(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    _insert_quote(db, ts=_ts(10), last=1712.5)
    monkeypatch.setattr(registry, "_pricevol_rpc", _no_rpc)

    quote, caliber = registry._db_first_price(db, "600519.SH")

    assert quote["close"] == pytest.approx(1712.5)
    assert caliber.startswith("口径：库内快照 ")
    assert "quote_snapshots" in caliber
    assert "实时 RPC" not in caliber          # 没有回落痕迹
    assert quote["trade_date"] == datetime.now(_CST).strftime("%Y%m%d")


def test_fresh_snapshot_wins_in_chips_tool(tmp_path, monkeypatch):
    """工具层同样不调 RPC, 且末行给出口径(给用户看的时点)。"""
    db = _make_db(tmp_path)
    _insert_quote(db, ts=_ts(5), last=1712.5)
    monkeypatch.setattr(registry, "_pricevol_rpc", _no_rpc)

    seen: dict = {}

    def _fake_build(code, *, count=250, quote=None):
        seen.update({"code": code, "quote": quote})
        return {"ok": True}

    monkeypatch.setattr("src.core.tq_chips.build_chips", _fake_build)
    monkeypatch.setattr("src.core.tq_chips.render_text", lambda payload: "🧪 筹码体检")

    out = __import__("asyncio").run(registry._tool_get_stock_chips(db, {"symbol": "600519"}, None))

    assert seen["quote"]["close"] == pytest.approx(1712.5)   # 现价来自库内快照
    assert out.splitlines()[-1].startswith("口径：库内快照 ")
    assert "tdx-gateway" not in out


# ────────────────── ② 过期快照回落 RPC(不许充作实时) ──────────────────

def test_stale_snapshot_falls_back_to_rpc(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    stale_ts = _ts(60 * 30)
    _insert_quote(db, ts=stale_ts, last=1700.0)
    calls: list[list[str]] = []

    def _rpc(codes):
        calls.append(list(codes))
        return True, {"600519.SH": {"Now": "1718.88"}}

    monkeypatch.setattr(registry, "_pricevol_rpc", _rpc)

    quote, caliber = registry._db_first_price(db, "600519.SH")

    assert calls == [["600519.SH"]]
    assert quote["close"] == "1718.88"                 # 用了 RPC, 不是过期的 1700
    assert "库内快照" in caliber and "／实时 RPC" in caliber
    assert "不作实时" in caliber
    assert stale_ts[:16] in caliber and "tdx-gateway" in caliber


def test_stale_snapshot_never_passed_off_as_realtime(tmp_path, monkeypatch):
    """库过期 + RPC 也不可用 → 如实说取不到, 不把过期价当现价返回。"""
    db = _make_db(tmp_path)
    _insert_quote(db, ts=_ts(60 * 60), last=1700.0)
    monkeypatch.setattr(registry, "_pricevol_rpc", lambda codes: (False, "conn refused"))

    quote, caliber = registry._db_first_price(db, "600519.SH")

    assert quote is None
    assert caliber.startswith("口径：取不到（")
    assert "过期" in caliber and "RPC 不可用" in caliber


# ────────────── ③ 无表 / 无行 / 无 db 也回落, 绝不抛 ──────────────

def test_missing_table_falls_back_without_raise(tmp_path, monkeypatch):
    db = _make_db(tmp_path, with_table=False)          # 旧库没跑迁移 157
    monkeypatch.setattr(registry, "_pricevol_rpc",
                        lambda codes: (True, {"600519.SH": {"Now": 1719.0}}))

    quote, caliber = registry._db_first_price(db, "600519.SH")

    assert quote["close"] == 1719.0
    assert "取不到（库无最新快照）" in caliber and "实时 RPC" in caliber


def test_empty_table_and_rpc_down_is_honest(tmp_path, monkeypatch):
    db = _make_db(tmp_path)                            # 有表无行
    monkeypatch.setattr(registry, "_pricevol_rpc", lambda codes: (False, "boom"))

    quote, caliber = registry._db_first_price(db, "600519.SH")

    assert quote is None
    assert caliber == "口径：取不到（库无最新快照 / RPC 不可用）"


def test_db_none_falls_back_without_raise(monkeypatch):
    monkeypatch.setattr(registry, "_pricevol_rpc", lambda codes: (False, "no db"))
    quote, caliber = registry._db_first_price(None, "600519.SH")
    assert quote is None and "取不到" in caliber


def test_no_snapshot_row_is_never_fatal_for_chips(monkeypatch):
    """筹码工具在库无快照 + RPC 不可用时仍返回文本(不抛), 且写明取不到。"""
    monkeypatch.setattr(registry, "_pricevol_rpc", lambda codes: (False, "boom"))
    monkeypatch.setattr("src.core.tq_chips.build_chips",
                        lambda code, count=250, quote=None: {"ok": False, "detail": "公式引擎不可用"})

    out = __import__("asyncio").run(registry._tool_get_stock_chips(None, {"symbol": "600519"}, None))

    assert "取不到" in out and "口径：取不到（" in out


# ────────── ④ 4 个仍走 RPC 的工具: 末行含「口径」+ 时点 ──────────

def _tq_stub(fn, *a, _quiet=False, **kw):
    if fn == "relation":
        return True, [
            {"BlockCode": "881044.SH", "BlockName": "塑料", "BlockType": "行业"},
            {"BlockCode": "880507.SH", "BlockName": "国防军工", "BlockType": "概念"},
        ]
    if fn == "kzz_info":
        return True, {"KZZName": "立讯转债", "KZZCode": "128136", "ZGPrice": "52.00",
                      "ForceRedeem": "72.370", "HSScore": "AA+"}
    if fn == "ipo_info":
        return True, [{"name": "通则康威", "code": "301234", "sg_date": "20261023",
                       "sg_price": 0, "sg_code": "301234", "max_sg": 10000, "pe_issue": 22.5}]
    if fn == "trackzs_etf":
        return True, [
            {"Code": "510300.SH", "Name": "沪深300ETF华泰", "Sz": "1200.00",
             "NowPrice": "4.6000", "IOPV": "4.6000"},
            {"Code": "159925.SZ", "Name": "沪深300ETF南方", "Sz": "28.61",
             "NowPrice": "4.591", "IOPV": "4.5889"},
        ]
    raise AssertionError(f"未预期的 RPC 调用: {fn}")


@pytest.mark.parametrize(
    "tool, args, must_have",
    [
        (registry._tool_get_stock_sectors, {"symbol": "002361"}, "行业: 塑料"),
        (registry._tool_get_kzz_terms, {"code": "128136"}, "强赎触发价 72.370"),
        (registry._tool_get_ipo_calendar, {"kind": "new"}, "通则康威"),
        (registry._tool_get_index_etfs, {"index_code": "000300.SH"}, "折溢价"),
    ],
)
def test_rpc_tools_carry_caliber_and_timestamp(monkeypatch, tool, args, must_have):
    monkeypatch.setattr(registry, "_tq_vendor_call", _tq_stub)

    out = __import__("asyncio").run(tool(None, args, None))

    assert must_have in out
    last = out.splitlines()[-1]
    assert last.startswith("口径：实时 RPC "), last        # 末行 = 口径(不许写在开头糊弄)
    assert "tdx-gateway" in last
    assert _HMS.search(last), last                        # 时点精确到秒


# ────────── ⑤ search_symbols: 本地缓存优先, 无命中才回落 ──────────

def _write_stock_cache(tmp_path, monkeypatch, stocks: list[dict]) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "stock_list_cache.json").write_text(
        json.dumps({"ts": time.time(), "stocks": stocks}, ensure_ascii=False), encoding="utf-8"
    )


def test_search_symbols_prefers_local_cache(tmp_path, monkeypatch):
    _write_stock_cache(tmp_path, monkeypatch, [
        {"symbol": "300750", "name": "宁德时代", "market": "CN"},
        {"symbol": "00700", "name": "腾讯控股", "market": "HK"},
    ])
    monkeypatch.setattr(registry, "_tq_vendor_call", _no_rpc)

    out = __import__("asyncio").run(registry._tool_search_symbols(None, {"keyword": "宁德时代"}, None))

    assert "宁德时代 300750.SZ(深市)" in out
    assert out.splitlines()[-1].startswith("口径：库内快照 ")
    assert "stock_list_cache.json" in out and "tdx-gateway" not in out


def test_search_symbols_falls_back_when_cache_misses(tmp_path, monkeypatch):
    _write_stock_cache(tmp_path, monkeypatch, [{"symbol": "300750", "name": "宁德时代", "market": "CN"}])

    def _rpc(fn, *a, _quiet=False, **kw):
        assert fn == "match_stkinfo"
        return True, [{"Code": "128136.SZ", "Name": "立讯转债"}]

    monkeypatch.setattr(registry, "_tq_vendor_call", _rpc)

    out = __import__("asyncio").run(registry._tool_search_symbols(None, {"keyword": "立讯转债"}, None))

    assert "128136.SZ(深市)" in out                       # 缓存没有的品种仍走客户端
    assert out.splitlines()[-1].startswith("口径：实时 RPC ") and "tdx-gateway" in out


def test_search_symbols_without_cache_does_not_fetch(tmp_path, monkeypatch):
    """缓存文件不存在时不许触发全市场刷新(会打外部源/阻塞数秒) → 直接回落 RPC。"""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert registry._local_match("宁德时代") == []


# ────────────── ⑥ 静态约束: 只读 SQL + 不 import src.web ──────────────

def test_snapshot_sql_is_read_only_with_bind_and_limit():
    sql = registry._QUOTE_SNAPSHOT_SQL
    upper = sql.upper()
    assert upper.startswith("SELECT")
    assert " LIMIT " in upper
    assert ":s" in sql and ":m" in sql                     # 绑定参数, 不拼串
    for forbidden in ("INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER"):
        assert forbidden not in upper


def test_new_helpers_do_not_import_web_layer():
    """新增取数路径不得反向依赖 src.web(KI-039 棘轮)。"""
    helpers = [
        registry._db_first_price, registry._db_quote_snapshot, registry._snapshot_symbol,
        registry._quote_age_sec, registry._pricevol_rpc, registry._caliber_db,
        registry._caliber_rpc, registry._caliber_db_then_rpc, registry._caliber_missing,
        registry._local_stock_rows, registry._local_match, registry._cache_stamp,
        registry._code_for_display,
    ]
    for fn in helpers:
        assert "src.web" not in inspect.getsource(fn), fn.__name__

    module_level = [
        ln for ln in (ROOT / "src/agents/chat/registry.py").read_text(encoding="utf-8").splitlines()
        if ln.startswith(("import ", "from ")) and "src.web" in ln
    ]
    assert module_level == []
