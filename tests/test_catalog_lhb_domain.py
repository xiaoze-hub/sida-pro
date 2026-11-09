# -*- coding: utf-8 -*-
"""龙虎榜（lhb.daily）进参谋的回归：执行路径 / 指定日期 / 空数据 / 权限 / 不编身份。

背景：`lhb.daily` 域在 tools_catalog 里已有实现（render_lhb + 只读 SQL），本文件不新造
agent/工具，只锁死「参谋真的调得到」以及 four 条诚实性红线：

1. **执行路径**：LLM 发起的 args（domain + params_json）经 registry 注册的
   `query_data_domain` handler 能走通到渲染，不是只有内部函数能用；
2. **数据日期**：指定日期必须按指定日期查；日期认不出时明确拒绝，**绝不悄悄回退到
   库内最新交易日**（否则用户问 10-20 却拿到 11-10 的数据，等于编日期）；
   未指定日期时，时点必须标注「库内最新交易日」，不能让模型误认成实时/今日；
3. **空数据**：指定日无上榜/无席位 → 写「未取到（无记录）」，不补 0、不换别的日期；
4. **席位身份**：只展示东财原文营业部名称 + 所属代码，不做游资/机构/量化身份推断；
5. **权限**：龙虎榜是公共行情域（requires=""），未登录（user=None）可查，且全程只读。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents.chat import tools_catalog as tc  # noqa: E402

REPO = ROOT


def _run(coro):
    return asyncio.run(coro)


class _Recorder:
    """按 key 返回预置行，并记录每次查询（含绑定参数）。"""

    def __init__(self, table: dict[str, list[dict]]):
        self.table = table
        self.calls: list[tuple[str, dict]] = []

    def read(self, db, key, params=None, fmt=None):
        self.calls.append((key, dict(params or {})))
        return [dict(r) for r in self.table.get(key, [])]

    def read_one(self, db, key, params=None):
        rows = self.read(db, key, params)
        return rows[0] if rows else None

    def keys(self) -> list[str]:
        return [k for k, _ in self.calls]

    def params_of(self, key: str) -> list[dict]:
        return [p for k, p in self.calls if k == key]


def _patch(monkeypatch, table):
    rec = _Recorder(table)
    monkeypatch.setattr(tc, "_read", rec.read)
    monkeypatch.setattr(tc, "_read_one", rec.read_one)
    return rec


def _query(args: dict) -> str:
    return _run(tc.query_data_domain(None, args))


# ──────────────────────────── 1. 执行路径（registry → handler → 渲染） ────────────────────────────

def test_lhb_tool_registered_and_public():
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY

    tool = CHAT_TOOL_REGISTRY["query_data_domain"]
    schema = tool.schema["function"]
    assert schema["name"] == "query_data_domain"
    assert set(schema["parameters"]["properties"]) == {"domain", "params_json"}
    assert schema["parameters"]["required"] == ["domain"]
    # 公共行情域：不要求登录态
    assert tool.requires == ""
    assert tool.caliber
    # 龙虎榜入口写在描述里，模型知道该用哪个域
    assert "lhb.daily" in schema["description"]


def test_lhb_callable_through_registered_handler(monkeypatch):
    """模拟 LLM 的实际调用：handler(db, args, user) —— 不是直接调内部函数。"""
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY

    _patch(
        monkeypatch,
        {
            "lhb_date": [{"d": "20261020"}],
            "lhb_events": [
                {"symbol": "002361", "name": "闽东电力", "reason": "日涨幅偏离值达7%",
                 "net_buy": 1.23e8, "buy_amt": 2.1e8, "sell_amt": 0.87e8,
                 "change_pct": 9.98, "turnover_pct": 12.3},
            ],
            "lhb_seats_top": [
                {"side": "buy", "symbol": "002361", "operate_dept_name": "机构专用",
                 "accum_amount": 8.2e7},
            ],
        },
    )
    handler = CHAT_TOOL_REGISTRY["query_data_domain"].handler
    out = _run(handler(None, {"domain": "lhb.daily", "params_json": "{}"}, None))
    assert "时点 20261020" in out
    assert "龙虎榜" in out and "002361" in out
    assert "未取到" not in out


# ──────────────────────────── 2. 指定日期 / 数据日期诚实 ────────────────────────────

def test_explicit_date_wins_over_latest(monkeypatch):
    rec = _patch(
        monkeypatch,
        {
            # 库内最新是 11-10，但用户指定 10-16 → 必须按 10-16 查
            "lhb_date": [{"d": "20261110"}],
            "lhb_events": [
                {"symbol": "002361", "name": "闽东电力", "net_buy": 1e7,
                 "buy_amt": 2e7, "sell_amt": 1e7, "change_pct": 3.0, "reason": "涨幅偏离"},
            ],
        },
    )
    out = _query({"domain": "lhb.daily", "params_json": '{"date": "2026-10-16"}'})
    assert "时点 20261016" in out
    assert "20261110" not in out
    assert "lhb_date" not in rec.keys()          # 指定日期时不该去读最新日期
    assert rec.params_of("lhb_events")[0]["d"] == "20261016"


def test_latest_date_is_labelled_when_date_omitted(monkeypatch):
    _patch(
        monkeypatch,
        {
            "lhb_date": [{"d": "20261110"}],
            "lhb_events": [{"symbol": "002361", "name": "闽东电力", "net_buy": 1e7,
                            "buy_amt": 2e7, "sell_amt": 1e7, "change_pct": 3.0}],
        },
    )
    out = _query({"domain": "lhb.daily"})
    assert "时点 20261110" in out
    assert "库内最新交易日" in out          # 不能冒充当日/实时数据


def test_unparseable_date_refuses_instead_of_latest(monkeypatch):
    rec = _patch(monkeypatch, {"lhb_date": [{"d": "20261110"}],
                               "lhb_events": [{"symbol": "002361", "net_buy": 1e7}]})
    out = _query({"domain": "lhb.daily", "params_json": '{"date": "上周五"}'})
    assert "日期无法识别" in out or "日期参数无法识别" in out
    assert "未取到" in out
    assert "20261110" not in out
    assert rec.calls == []                   # 拒绝取数，不做任何日期替换


def test_impossible_date_refused():
    out = _query({"domain": "lhb.daily", "params_json": '{"date": "2026-02-31"}'})
    assert "日期" in out and ("无法识别" in out or "不合法" in out)
    assert "未取到" in out


def test_explicit_empty_date_says_no_records(monkeypatch):
    rec = _patch(monkeypatch, {"lhb_date": [{"d": "20261110"}]})
    out = _query({"domain": "lhb.daily", "params_json": '{"date": "20261020"}'})
    assert "时点 20261020" in out
    assert "未取到" in out and "无记录" in out
    assert "20261110" not in out
    assert rec.params_of("lhb_events")[0]["d"] == "20261020"   # 不回退


# ──────────────────────────── 3. 席位：归属代码 + 不编身份 ────────────────────────────

def test_market_seat_rows_carry_stock_code_and_no_identity_claim(monkeypatch):
    _patch(
        monkeypatch,
        {
            "lhb_date": [{"d": "20261020"}],
            "lhb_events": [
                {"symbol": "002361", "name": "闽东电力", "net_buy": 1e7,
                 "buy_amt": 2e7, "sell_amt": 1e7, "change_pct": 9.98},
                {"symbol": "600111", "name": "北方稀土", "net_buy": -5e6,
                 "buy_amt": 1e7, "sell_amt": 1.5e7, "change_pct": -7.1},
            ],
            "lhb_seats_top": [
                {"side": "buy", "symbol": "002361", "name": "",
                 "operate_dept_name": "华泰证券股份有限公司深圳益田路荣超商务中心证券营业部",
                 "accum_amount": 8.2e7},
                {"side": "sell", "symbol": "600111", "name": "",
                 "operate_dept_name": "机构专用", "accum_amount": 4.1e7},
            ],
        },
    )
    out = _query({"domain": "lhb.daily"})
    # 全市场席位必须带所属代码，否则「谁家的席位」无从判断
    assert "（002361）" in out and "（600111）" in out
    assert "买入席位" in out and "卖出席位" in out
    # 只做原文披露，不推断身份
    assert "不推断" in out and "游资/机构/量化" in out


def test_symbol_path_seats_are_scoped_and_normalized(monkeypatch):
    rec = _patch(
        monkeypatch,
        {
            "lhb_date": [{"d": "20261020"}],
            "lhb_events_symbol": [{"symbol": "002361", "name": "闽东电力", "net_buy": 1e7,
                                   "buy_amt": 2e7, "sell_amt": 1e7, "change_pct": 9.98}],
            "lhb_seats": [{"side": "buy", "symbol": "002361", "operate_dept_name": "机构专用",
                           "accum_amount": 8.2e7}],
        },
    )
    out = _query({"domain": "lhb.daily",
                  "params_json": json.dumps({"symbol": "sz002361", "date": "20261020"})})
    assert "闽东电力" in out and "002361" in out
    assert rec.params_of("lhb_events_symbol")[0]["s"] == "002361"
    assert rec.params_of("lhb_seats")[0]["s"] == "002361"


def test_invalid_symbol_refused_not_silently_empty(monkeypatch):
    rec = _patch(monkeypatch, {"lhb_date": [{"d": "20261020"}]})
    out = _query({"domain": "lhb.daily", "params_json": '{"symbol": "6001"}'})
    assert "6 位" in out
    assert rec.calls == []


def test_empty_seats_report_missing_without_invented_names(monkeypatch):
    _patch(
        monkeypatch,
        {
            "lhb_date": [{"d": "20261020"}],
            "lhb_events": [{"symbol": "002361", "name": "闽东电力", "net_buy": 1e7,
                            "buy_amt": 2e7, "sell_amt": 1e7, "change_pct": 9.98}],
            "lhb_seats_top": [],
        },
    )
    out = _query({"domain": "lhb.daily"})
    assert "无席位明细" in out
    assert "未知营业部" not in out          # 不拿占位名冒充席位
    assert "游资" not in out and "量化" not in out


def test_duplicate_dept_rows_listed_once(monkeypatch):
    """同一营业部同日同股多个上榜原因 → 只列一次，不重复堆榜。"""
    _patch(
        monkeypatch,
        {
            "lhb_date": [{"d": "20261020"}],
            "lhb_events": [{"symbol": "002361", "name": "闽东电力", "net_buy": 1e7,
                            "buy_amt": 2e7, "sell_amt": 1e7, "change_pct": 9.98}],
            "lhb_seats": [
                {"side": "buy", "symbol": "002361", "operate_dept_name": "机构专用",
                 "accum_amount": 8.2e7},
                {"side": "buy", "symbol": "002361", "operate_dept_name": "机构专用",
                 "accum_amount": 3.0e7},
            ],
        },
    )
    out = _query({"domain": "lhb.daily", "params_json": '{"symbol": "002361"}'})
    assert out.count("机构专用") == 2       # 席位行 1 次 + 口径说明里的原文举例 1 次
    assert "仅列一次" in out


def test_plain_lhb_question_keeps_domain_tool_visible():
    """纯龙虎榜问题不能被某份配方吞掉后又拿不到取数工具。

    现状：`龙虎榜` 不是任何配方的 when 关键词 → 走「无配方 → 全量工具兜底」，
    `query_data_domain`（fallback=True）可见。若以后有人把「龙虎榜」加进某份
    **pipeline** 配方的 when，模型会走确定性管线、根本没有机会调这个工具，
    本断言在那一刻就会红——提醒必须给该配方显式挂 query_data_domain（且不能是
    pipeline，否则仍调不到）。
    """
    from src.agents.chat.registry import chat_tool_schemas
    from src.harness.routing import route_for_request

    route = route_for_request(
        "看下2026年10月20日龙虎榜榜单和席位", chat_tool_schemas(),
        forced=None, persona_recipe=None,
    )
    if route.recipe is None:
        names = {(s.get("function") or {}).get("name") for s in route.tools}
        assert "query_data_domain" in names
    else:
        rid = getattr(route.recipe, "id", "?")
        assert not getattr(route.recipe, "pipeline", False), rid
        assert "query_data_domain" in getattr(route.recipe, "tools", ()), rid


# ──────────────────────────── 4. 权限 + 只读 ────────────────────────────

def test_lhb_is_public_readonly_domain():
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY

    assert CHAT_TOOL_REGISTRY["query_data_domain"].requires == ""
    assert "登录" not in tc.render_domain_catalog().split("lhb.daily")[0][-80:]
    lhb_sql = {k: v for k, v in tc._SQL.items() if k.startswith("lhb")}
    assert lhb_sql
    for key, sql in lhb_sql.items():
        assert sql.upper().lstrip().startswith("SELECT"), key
        assert "LIMIT" in sql.upper(), key


def test_lhb_no_login_required(monkeypatch):
    """user=None（未登录）也能查公共龙虎榜，不返回登录/权限提示。"""
    _patch(
        monkeypatch,
        {
            "lhb_date": [{"d": "20261020"}],
            "lhb_events": [{"symbol": "002361", "name": "闽东电力", "net_buy": 1e7,
                            "buy_amt": 2e7, "sell_amt": 1e7, "change_pct": 9.98}],
        },
    )
    out = _run(tc.query_data_domain(None, {"domain": "lhb.daily"}, None))
    assert "请先登录" not in out and "无权限" not in out
    assert "002361" in out


# ──────────────────────────── 5. 真 SQL 冒烟（sqlite 临时库，不碰生产） ────────────────────────────

def test_lhb_real_sql_roundtrip_on_sqlite(tmp_path):
    """不打 _read 桩：用真实 SELECT 跑一遍，锁住列名/绑定参数/空日行为。

    只建临时 sqlite（隔离环境，不连生产库）；表结构照 src/web/migrations.py 的
    lhb 两张表裁剪，能照出 SELECT 列名漂移与 :d/:s/:n 绑定错误。
    """
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    engine = create_engine(f"sqlite:///{tmp_path / 'lhb.db'}")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE dragon_tiger_events (trade_date TEXT, symbol TEXT, market TEXT, "
            "name TEXT, reason TEXT, close REAL, change_pct REAL, net_buy REAL, buy_amt REAL, "
            "sell_amt REAL, deal_amt REAL, turnover_pct REAL)"
        ))
        conn.execute(text(
            "CREATE TABLE lhb_seat_details (row_uid TEXT PRIMARY KEY, trade_date TEXT, "
            "side TEXT, symbol TEXT, name TEXT, reason TEXT, operate_dept_code TEXT, "
            "operate_dept_name TEXT, close REAL, change_pct REAL, accum_amount REAL, "
            "accum_volume REAL)"
        ))
        conn.execute(
            text(
                "INSERT INTO dragon_tiger_events VALUES "
                "('20261020','002361','CN','闽东电力','日涨幅偏离值达7%',12.5,9.98,1.23e8,"
                "2.1e8,0.87e8,2.97e8,12.3)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO lhb_seat_details VALUES "
                "('u1','20261020','buy','002361','','涨幅偏离','D001','机构专用',12.5,9.98,8.2e7,100)"
            )
        )
    with Session(engine) as s:
        out = _run(tc.query_data_domain(
            s, {"domain": "lhb.daily", "params_json": '{"date": "20261020", "symbol": "002361"}'}
        ))
        assert "时点 20261020" in out
        assert "闽东电力" in out and "+1.23亿" in out
        assert "机构专用" in out and "不推断" in out
        assert "未取到" not in out

        # 同库换一个没有数据的交易日 → 只报无记录，不回退到 20261020
        empty = _run(tc.query_data_domain(
            s, {"domain": "lhb.daily", "params_json": '{"date": "20261021"}'}
        ))
        assert "时点 20261021" in empty and "无记录" in empty and "未取到" in empty
        assert "闽东电力" not in empty

