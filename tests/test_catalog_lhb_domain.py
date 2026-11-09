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
import re
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
            "lhb_top_buy": [
                {"symbol": "002361", "name": "闽东电力", "reason": "日涨幅偏离值达7%",
                 "net_buy": 1.23e8, "buy_amt": 2.1e8, "sell_amt": 0.87e8,
                 "change_pct": 9.98, "turnover_pct": 12.3},
            ],
            "lhb_seats_top": [
                {"side": "buy", "symbol": "002361", "operate_dept_name": "机构专用",
                 "buy_amt": 8.2e7, "sell_amt": 0.0, "net_amt": 8.2e7},
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
            "lhb_top_buy": [
                {"symbol": "002361", "name": "闽东电力", "net_buy": 1e7,
                 "buy_amt": 2e7, "sell_amt": 1e7, "change_pct": 3.0, "reason": "涨幅偏离"},
            ],
        },
    )
    out = _query({"domain": "lhb.daily", "params_json": '{"date": "2026-10-16"}'})
    assert "时点 20261016" in out
    assert "20261110" not in out
    assert "lhb_date" not in rec.keys()          # 指定日期时不该去读最新日期
    assert rec.params_of("lhb_top_buy")[0]["d"] == "20261016"


def test_latest_date_is_labelled_when_date_omitted(monkeypatch):
    _patch(
        monkeypatch,
        {
            "lhb_date": [{"d": "20261110"}],
            "lhb_top_buy": [{"symbol": "002361", "name": "闽东电力", "net_buy": 1e7,
                             "buy_amt": 2e7, "sell_amt": 1e7, "change_pct": 3.0}],
        },
    )
    out = _query({"domain": "lhb.daily"})
    assert "时点 20261110" in out
    assert "库内最新交易日" in out          # 不能冒充当日/实时数据


def test_unparseable_date_refuses_instead_of_latest(monkeypatch):
    rec = _patch(monkeypatch, {"lhb_date": [{"d": "20261110"}],
                               "lhb_top_buy": [{"symbol": "002361", "net_buy": 1e7}]})
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
    assert rec.params_of("lhb_top_buy")[0]["d"] == "20261020"   # 不回退


# ──────────────────────────── 3. 席位：归属代码 + 不编身份 ────────────────────────────

def test_market_seat_rows_carry_stock_code_and_no_identity_claim(monkeypatch):
    _patch(
        monkeypatch,
        {
            "lhb_date": [{"d": "20261020"}],
            "lhb_top_buy": [
                {"symbol": "002361", "name": "闽东电力", "net_buy": 1e7,
                 "buy_amt": 2e7, "sell_amt": 1e7, "change_pct": 9.98},
            ],
            "lhb_top_sell": [
                {"symbol": "600111", "name": "北方稀土", "net_buy": -5e6,
                 "buy_amt": 1e7, "sell_amt": 1.5e7, "change_pct": -7.1},
            ],
            "lhb_seats_top": [
                {"side": "buy", "symbol": "002361", "name": "",
                 "operate_dept_name": "华泰证券股份有限公司深圳益田路荣超商务中心证券营业部",
                 "buy_amt": 8.2e7, "sell_amt": 0.0, "net_amt": 8.2e7},
                {"side": "sell", "symbol": "600111", "name": "",
                 "operate_dept_name": "机构专用",
                 "buy_amt": 0.0, "sell_amt": 4.1e7, "net_amt": -4.1e7},
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
                           "buy_amt": 8.2e7, "sell_amt": 0.0, "net_amt": 8.2e7}],
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
            "lhb_top_buy": [{"symbol": "002361", "name": "闽东电力", "net_buy": 1e7,
                             "buy_amt": 2e7, "sell_amt": 1e7, "change_pct": 9.98}],
            "lhb_seats_top": [],
        },
    )
    out = _query({"domain": "lhb.daily"})
    assert "无席位明细" in out
    assert "未知营业部" not in out          # 不拿占位名冒充席位
    assert "游资" not in out and "量化" not in out


def test_same_name_institution_seats_are_not_collapsed(monkeypatch):
    """「机构专用」是东财原文占位名，多家机构同名 → 金额不同就是不同席位，不能按名字去重。"""
    _patch(
        monkeypatch,
        {
            "lhb_date": [{"d": "20261020"}],
            "lhb_events_symbol": [{"symbol": "002361", "name": "闽东电力", "net_buy": 1e7,
                                   "buy_amt": 2e7, "sell_amt": 1e7, "change_pct": 9.98}],
            "lhb_seats": [
                {"side": "buy", "symbol": "002361", "operate_dept_name": "机构专用",
                 "operate_dept_code": "A01", "buy_amt": 8.2e7, "sell_amt": 0.0, "net_amt": 8.2e7},
                {"side": "buy", "symbol": "002361", "operate_dept_name": "机构专用",
                 "operate_dept_code": "A02", "buy_amt": 3.0e7, "sell_amt": 0.0, "net_amt": 3.0e7},
            ],
        },
    )
    out = _query({"domain": "lhb.daily", "params_json": '{"symbol": "002361"}'})
    # 两个不同机构席位（同名、不同营业部代码、不同金额）都必须列出来
    assert "8200万" in out and "3000万" in out, out
    assert out.count("机构专用") >= 3, out        # 2 条席位 + 口径说明里的原文举例
    assert "仅列一次" not in out                   # 不同席位不该被当成重复


def test_no_uid_rows_with_different_reasons_are_not_merged(monkeypatch):
    """没有 row_uid 的桩/旧数据：同侧同股同营业部同金额但**上榜原因不同** → 两条源行，不合并。

    2026-11-09 二轮审查：按「名称+编码+金额」判重会把不同机构/不同上榜原因的记录吞掉。
    现在只认 row_uid（真源行身份），没有 row_uid 时至少要把上榜原因计入键，并显式提示
    「同名同额」可能不是同一家。

    @spec LHB-002
    """
    row = {"side": "buy", "symbol": "002361", "operate_dept_name": "机构专用",
           "operate_dept_code": "A01", "reason": "涨幅偏离", "buy_amt": 8.2e7,
           "sell_amt": 0.0, "net_amt": 8.2e7}
    _patch(
        monkeypatch,
        {
            "lhb_date": [{"d": "20261020"}],
            "lhb_events_symbol": [{"symbol": "002361", "name": "闽东电力", "net_buy": 1e7,
                                   "buy_amt": 2e7, "sell_amt": 1e7, "change_pct": 9.98}],
            "lhb_seats": [row, dict(row, reason="连续三日涨幅偏离")],
        },
    )
    out = _query({"domain": "lhb.daily", "params_json": '{"symbol": "002361"}'})
    assert out.count("8200万") >= 2, out
    assert "同名同额" in out and "不合并" in out, out


def test_seat_amount_uses_seat_buy_sell_not_stock_turnover(monkeypatch):
    """席位金额必须是席位自身的 BUY/SELL/NET，不是 ACCUM_AMOUNT（该股当日累计成交额）。

    ACCUM_AMOUNT 与榜单里的股票总成交额同值：拿它当席位金额，等于每一家营业部都
    显示 12 亿，席位强弱比较彻底失真（test_lhb_detail_backfill 的样本里同一只票的
    机构表与席位表 ACCUM_AMOUNT 都是 1.2e9，而席位 BUY 只有 2.88e8）。
    """
    _patch(
        monkeypatch,
        {
            "lhb_date": [{"d": "20261020"}],
            "lhb_events_symbol": [{"symbol": "002636", "name": "金安国纪", "net_buy": 1.4e8,
                                   "buy_amt": 2.88e8, "sell_amt": 1.48e8, "change_pct": 10.02}],
            "lhb_seats": [
                {"side": "buy", "symbol": "002636", "operate_dept_name": "深股通专用",
                 "operate_dept_code": "10456710", "accum_amount": 1.2e9,
                 "buy_amt": 2.88e8, "sell_amt": 1.48e8, "net_amt": 1.4e8},
            ],
        },
    )
    out = _query({"domain": "lhb.daily", "params_json": '{"symbol": "002636", "date": "20261020"}'})
    assert "2.88亿" in out, out          # 席位买入额
    assert "1.40亿" in out, out          # 席位净额
    assert "12.00亿" not in out and "1.20亿" not in out, out   # 股票总成交额不得冒充席位金额


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
            "lhb_top_buy": [{"symbol": "002361", "name": "闽东电力", "net_buy": 1e7,
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
            "accum_volume REAL, buy_amt REAL, sell_amt REAL, net_amt REAL)"
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
                "('u1','20261020','buy','002361','','涨幅偏离','D001','机构专用',12.5,9.98,"
                "2.97e8,100,8.2e7,0.0,8.2e7)"
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


# ──────────────────────────── 6. 全市场榜单：先排序后截断，去重口径明示 ────────────────────────────

def test_market_board_ranks_in_sql_before_limit(monkeypatch):
    """全市场净买/净卖前列必须是「全表按净额排序后取前 N」，不能按代码顺序抽样后再排序。

    旧实现：`SELECT … ORDER BY symbol LIMIT 60` 再在 Python 里挑净买额最大的 5 条 ——
    样本是「代码最小的 60 条」，榜单却被写成「净买入前列」，等于把抽样冒充全市场排名。
    """
    rec = _patch(
        monkeypatch,
        {
            "lhb_date": [{"d": "20261020"}],
            "lhb_rank_count": [{"n": 92, "syms": 41}],
            "lhb_top_buy": [
                {"symbol": "002361", "name": "闽东电力", "net_buy": 3.0e8, "buy_amt": 4.0e8,
                 "sell_amt": 1.0e8, "change_pct": 9.98, "reason": "日涨幅偏离值达7%"},
                # 同股第二个上榜原因 → 榜单只算一只
                {"symbol": "002361", "name": "闽东电力", "net_buy": 3.0e8, "buy_amt": 4.0e8,
                 "sell_amt": 1.0e8, "change_pct": 9.98, "reason": "连续三日涨幅偏离"},
                {"symbol": "600111", "name": "北方稀土", "net_buy": 1.1e8, "buy_amt": 2.0e8,
                 "sell_amt": 0.9e8, "change_pct": 6.1, "reason": "涨幅偏离"},
            ],
            "lhb_top_sell": [
                {"symbol": "300750", "name": "宁德时代", "net_buy": -4.2e8, "buy_amt": 1.0e8,
                 "sell_amt": 5.2e8, "change_pct": -8.4, "reason": "跌幅偏离"},
            ],
            "lhb_seats_top": [],
            "lhb_seat_total": [{"n": 0}],
        },
    )
    out = _query({"domain": "lhb.daily", "params_json": '{"date": "20261020"}'})
    # 榜单条数用库内真实计数，不是本轮取到的行数
    assert "92 条" in out and "41" in out, out
    assert "净买入前列" in out and "闽东电力" in out and "北方稀土" in out
    assert out.count("闽东电力") == 1, f"同股多上榜原因在榜单里只该出现一次: {out}"
    assert "净卖出前列" in out and "宁德时代" in out
    # 取数走的是排序语句（ORDER BY net_buy），不是 symbol 抽样语句
    assert "lhb_top_buy" in rec.keys() and "lhb_top_sell" in rec.keys()
    assert rec.params_of("lhb_top_buy")[0]["d"] == "20261020"
    assert "lhb_events" not in rec.keys()
    assert "lhb_rank_count" in rec.keys()        # 榜单规模来自库内 COUNT，不是取到的行数


def test_board_truncation_is_disclosed_when_more_than_five(monkeypatch):
    """净买额超过 5 只时必须写明「已截断/只列前 5」，不能让人以为榜单只有 5 只。"""
    rows = [
        {"symbol": f"00{i:04d}", "name": f"票{i}", "net_buy": 1e8 - i * 1e7,
         "buy_amt": 2e8, "sell_amt": 1e8, "change_pct": 5.0}
        for i in range(7)
    ]
    _patch(
        monkeypatch,
        {
            "lhb_date": [{"d": "20261020"}],
            "lhb_rank_count": [{"n": 7, "syms": 7}],
            "lhb_top_buy": rows,
            "lhb_top_sell": [],
            "lhb_seats_top": [],
            "lhb_seat_total": [{"n": 0}],
        },
    )
    out = _query({"domain": "lhb.daily", "params_json": '{"date": "20261020"}'})
    assert "已截断" in out and "5 只" in out, out
    assert "000005" not in out and "000006" not in out     # 第 6/7 只确实没列


def test_rank_sql_orders_by_amount_not_symbol():
    """SQL 层就必须按净额排序（幂等/无桩时也一样），否则「前 5」只是抽样前 5。"""
    buy = tc._SQL["lhb_top_buy"].upper()
    sell = tc._SQL["lhb_top_sell"].upper()
    assert "NET_BUY > 0" in buy and "ORDER BY NET_BUY DESC" in buy
    assert "NET_BUY < 0" in sell and "ORDER BY NET_BUY ASC" in sell
    for sql in (buy, sell):
        assert "ORDER BY SYMBOL" not in sql
    seats = tc._SQL["lhb_seats_top"].upper()
    assert "ORDER BY SYMBOL" not in seats and "NET_AMT" in seats


def test_seat_listing_discloses_total_and_truncation(monkeypatch):
    """全市场席位按净额排序后取前几条，必须写明「共 N 条 / 只列前 5」，不冒充全量。"""
    _patch(
        monkeypatch,
        {
            "lhb_date": [{"d": "20261020"}],
            "lhb_rank_count": [{"n": 10, "syms": 4}],
            "lhb_top_buy": [{"symbol": "002361", "name": "闽东电力", "net_buy": 1e8,
                             "buy_amt": 2e8, "sell_amt": 1e8, "change_pct": 9.0}],
            "lhb_top_sell": [],
            "lhb_seats_top": [
                {"side": "buy", "symbol": "002361", "operate_dept_name": "机构专用",
                 "operate_dept_code": "A01", "buy_amt": 8.2e7, "sell_amt": 0.0, "net_amt": 8.2e7},
            ],
            "lhb_seat_total": [{"n": 137}],
        },
    )
    out = _query({"domain": "lhb.daily", "params_json": '{"date": "20261020"}'})
    assert "137" in out and "前 5" in out, out
    assert "非全量" in out or "已截断" in out, out
    assert "8200万" in out


# ──────────────────────────── 7. 复盘 + 龙虎榜：路由 → 执行 → 返回 全链路 ────────────────────────────

def test_lhb_review_question_routes_to_non_pipeline_recipe_that_can_fetch():
    """「复盘 + 龙虎榜」必须有一条真能取数到 lhb.daily 的路径。

    旧行为：`复盘` 命中 `postmarket_reviewer`（pipeline: true），其工具白名单里没有
    `query_data_domain` —— 用户问「复盘今天的龙虎榜」，管线自己跑完 8 个工具就写结论，
    模型**没有任何机会**调到龙虎榜域，只能凭印象编席位。修法不是把域工具塞进 pipeline
    白名单（pipeline 按 {} 调用，query_data_domain 必填 domain，会被判 unsatisfiable
    静默跳过），而是给龙虎榜一份专门的非 pipeline 配方。

    @spec LHB-001
    """
    from src.agents.chat.registry import chat_tool_schemas
    from src.harness.routing import route_for_request

    for q in ("复盘今天的龙虎榜", "今天龙虎榜席位谁在买", "看下龙虎榜净买入和营业部"):
        route = route_for_request(q, chat_tool_schemas())
        assert route.recipe is not None, f"{q} 落到了无配方兜底，取数路径不受控"
        assert route.recipe.id == "lhb_reviewer", f"{q} → {route.recipe.id}"
        assert not route.recipe.pipeline, "pipeline 模式按 {} 调工具，query_data_domain 会被静默跳过"
        names = {(s.get("function") or {}).get("name") for s in route.tools}
        assert "query_data_domain" in names, f"{q} 路由后模型看不到取数入口"

    # 纯复盘仍走原管线（不被新配方抢走）
    plain = route_for_request("帮我复盘今天", chat_tool_schemas())
    assert plain.recipe is not None and plain.recipe.id == "postmarket_reviewer"


def test_pipeline_recipe_would_silently_skip_domain_tool():
    """反面证据：把域工具塞进 pipeline 白名单救不了复盘 —— 必填 domain 被判 unsatisfiable 直接跳过。

    这条锁的是「为什么必须另建非 pipeline 配方」这个判断本身：管线只按 schema 的 required
    拼参数（{} / {symbol}），`query_data_domain` 的 required=['domain'] 永远填不上，
    `build_steps` 会生成一个 skip 步骤，工具根本没被调用过。
    """
    from dataclasses import replace

    from src.harness.pipeline_market import build_steps
    from src.harness.recipes import get_recipe

    recipe = get_recipe("postmarket_reviewer")
    assert recipe is not None
    assert recipe.pipeline is True
    assert "query_data_domain" not in recipe.tools     # 现状：管线里没有它
    fake = replace(recipe, tools=tuple(recipe.tools) + ("query_data_domain",))
    steps = {s.tool: s for s in build_steps(fake, watchlist=["002361"])}
    step = steps["query_data_domain"]
    assert step.skip, "pipeline 里必填 domain 的工具不会被调用"
    assert step.skip.startswith("unsatisfiable_args"), step.skip


def test_lhb_review_executes_through_chat_tool_layer(tmp_path):
    """执行层证据：路由产出的配方白名单下，chat._execute_tool 真的放行并返回龙虎榜数据。

    注册表 handler 能跑通只说明「函数可调用」；配方白名单是**运行时硬闸**
    (`_REQ_TOOLS`)，名字不在 `recipe.tools` 里会被直接拒绝。这里走完整链路：
    route → recipe.tools → _REQ_TOOLS → _execute_tool → registry handler → 渲染。
    """
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from src.agents.chat.registry import chat_tool_schemas
    from src.harness.routing import route_for_request
    from src.web.api import chat as chat_api

    route = route_for_request("复盘今天的龙虎榜", chat_tool_schemas())
    recipe = route.recipe
    assert recipe is not None and getattr(recipe, "id", "") == "lhb_reviewer"
    tools = tuple(getattr(recipe, "tools", ()))
    assert "query_data_domain" in tools

    engine = create_engine(f"sqlite:///{tmp_path / 'lhb_exec.db'}")
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
            "accum_volume REAL, buy_amt REAL, sell_amt REAL, net_amt REAL)"
        ))
        conn.execute(text(
            "INSERT INTO dragon_tiger_events VALUES "
            "('20261020','002361','CN','闽东电力','日涨幅偏离值达7%',12.5,9.98,1.23e8,"
            "2.1e8,0.87e8,2.97e8,12.3)"
        ))
        conn.execute(text(
            "INSERT INTO lhb_seat_details VALUES "
            "('u1','20261020','buy','002361','','涨幅偏离','D001','机构专用',12.5,9.98,"
            "2.97e8,100,8.2e7,0.0,8.2e7)"
        ))

    token = chat_api._REQ_TOOLS.set(frozenset(tools))
    try:
        with Session(engine) as s:
            out = _run(chat_api._execute_tool(
                s, "query_data_domain",
                {"domain": "lhb.daily", "params_json": '{"date": "20261020"}'}, user=None,
            ))
    finally:
        chat_api._REQ_TOOLS.reset(token)
    assert "不使用工具" not in out, out[:200]
    assert "时点 20261020" in out and "闽东电力" in out
    assert "8200万" in out and "买入席位" in out


def test_lhb_review_route_executes_and_returns_seat_evidence(tmp_path):
    """路由命中后，真的按 schema 调 registry handler，从临时 sqlite 取到席位证据。"""
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from src.agents.chat.registry import CHAT_TOOL_REGISTRY, chat_tool_schemas
    from src.harness.routing import route_for_request

    route = route_for_request("复盘今天的龙虎榜", chat_tool_schemas())
    schema = next(
        s for s in route.tools if (s.get("function") or {}).get("name") == "query_data_domain"
    )
    props = schema["function"]["parameters"]["properties"]
    assert {"domain", "params_json"} <= set(props)     # 模型能构造出合法参数

    engine = create_engine(f"sqlite:///{tmp_path / 'lhb_route.db'}")
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
            "accum_volume REAL, buy_amt REAL, sell_amt REAL, net_amt REAL)"
        ))
        conn.execute(text(
            "INSERT INTO dragon_tiger_events VALUES "
            "('20261020','002361','CN','闽东电力','日涨幅偏离值达7%',12.5,9.98,1.23e8,"
            "2.1e8,0.87e8,2.97e8,12.3)"
        ))
        conn.execute(text(
            "INSERT INTO lhb_seat_details VALUES "
            "('u1','20261020','buy','002361','','涨幅偏离','D001','机构专用',12.5,9.98,"
            "2.97e8,100,8.2e7,0.0,8.2e7)"
        ))
    handler = CHAT_TOOL_REGISTRY["query_data_domain"].handler
    with Session(engine) as s:
        out = _run(handler(s, {"domain": "lhb.daily", "params_json": '{"date": "20261020"}'}, None))
    assert "时点 20261020" in out
    assert "闽东电力" in out
    # 席位证据来自 buy_amt/net_amt（8.2e7 / 8.2e7），不是 accum_amount（2.97e8）
    assert "8200万" in out and "2.97亿" not in out
    assert "买入席位" in out and "机构专用" in out


# ──────────────────────────── 8. 模型可见的身份描述不得撒谎 ────────────────────────────

def test_catalog_description_does_not_claim_identity_labels():
    """`list_data_domains` 的描述会原样喂给模型 → 不能写目录里没有的「游资/机构/量化标签」。

    `lhb_seat_details` 没有身份标签列（只有东财原文营业部名称）。目录写成「游资/机构/量化
    标签」，模型就会把这个描述当成能力说明，回头把「机构专用」当成身份判定来引用。
    """
    lhb = next(d for d in tc.DOMAINS if d["id"] == "lhb.daily")
    desc = lhb["desc"]
    assert "标签" not in desc, f"目录描述不能声称有身份标签: {desc}"
    for word in ("游资", "量化"):
        assert word not in desc, f"目录描述不能声称能区分 {word}: {desc}"
    # 真正该说的：东财原文营业部名 + 无身份标签
    assert "席位" in desc
    catalog = tc.render_domain_catalog()
    line = next(ln for ln in catalog.splitlines() if ln.startswith("lhb.daily"))
    assert "标签" not in line and "游资" not in line, line
    # 取数说明里的身份红线仍在（不推断）
    assert "不推断" in tc._QUERY_SCHEMA["function"]["description"]


# ─────────── 9. 二轮审查复现：同名同额 / 整行截断 / 混合问句路由 / 排名口径 ───────────


def test_same_name_same_amount_seats_are_kept_with_reason_hint(monkeypatch):
    """同名 + 同额也不能推定是同一家席位 —— 必须保留源行，并给出原因/身份提示。

    东财把一堆不同机构都写成「机构专用」，营业部代码可能同为空：若按「名称+金额」去重，
    两家金额恰好相同的机构会被吞成一条，席位家数与买卖力量直接失真。真实的重复只有
    「同一条源记录」（同 row_uid）被重复返回，按内容猜重复不可靠。

    @spec LHB-002
    """
    _patch(
        monkeypatch,
        {
            "lhb_date": [{"d": "20261020"}],
            "lhb_events_symbol": [{"symbol": "002361", "name": "闽东电力", "net_buy": 1e7,
                                   "buy_amt": 2e7, "sell_amt": 1e7, "change_pct": 9.98}],
            "lhb_seats": [
                {"row_uid": "u1", "side": "buy", "symbol": "002361",
                 "operate_dept_name": "机构专用", "operate_dept_code": "",
                 "reason": "日涨幅偏离值达7%",
                 "buy_amt": 8.2e7, "sell_amt": 0.0, "net_amt": 8.2e7},
                {"row_uid": "u2", "side": "buy", "symbol": "002361",
                 "operate_dept_name": "机构专用", "operate_dept_code": "",
                 "reason": "连续三个交易日收盘价涨幅偏离值累计20%",
                 "buy_amt": 8.2e7, "sell_amt": 0.0, "net_amt": 8.2e7},
            ],
        },
    )
    out = _query({"domain": "lhb.daily", "params_json": '{"symbol": "002361"}'})
    # 两条源行都要出现（不是按名字+金额合成一条）
    assert out.count("8200万") >= 2, out
    assert "同名同额" in out and "不合并" in out, out
    # 同名同额的提示要带上各自的上榜原因，别让模型以为是一条记录
    assert "连续三个交易日收盘价涨幅偏离值累计20%" in out, out


def test_duplicate_source_row_uid_still_listed_once(monkeypatch):
    """同一条源记录（同 row_uid）被重复返回时才合并 —— 这是唯一可证明的重复。"""
    row = {"row_uid": "u1", "side": "buy", "symbol": "002361",
           "operate_dept_name": "机构专用", "operate_dept_code": "A01",
           "reason": "涨幅偏离", "buy_amt": 8.2e7, "sell_amt": 0.0, "net_amt": 8.2e7}
    _patch(
        monkeypatch,
        {
            "lhb_date": [{"d": "20261020"}],
            "lhb_events_symbol": [{"symbol": "002361", "name": "闽东电力", "net_buy": 1e7,
                                   "buy_amt": 2e7, "sell_amt": 1e7, "change_pct": 9.98}],
            "lhb_seats": [row, dict(row, reason="连续三日涨幅偏离")],
        },
    )
    out = _query({"domain": "lhb.daily", "params_json": '{"symbol": "002361"}'})
    assert out.count("机构专用") == 2       # 席位行 1 次 + 口径说明里的原文举例 1 次
    assert "同一源记录" in out or "仅列一次" in out, out


def test_truncation_cuts_whole_lines_only_and_keeps_caliber(monkeypatch):
    """单域 1200 字上限必须按整行截断：不能把金额截成「+1.2」半截，也必须保住口径行。

    旧实现 `text[:1200]` 在字符上硬切：正文一长，正好切在「+1.2」/「8.2」中间，
    模型读到半个金额；末行的「席位口径」被整段吞掉，于是它不知道金额是席位自身买卖额。

    @spec LHB-003
    """
    long_reason = (
        "日涨幅偏离值达7%、连续三个交易日收盘价涨幅偏离值累计20%、换手率异常放大且"
        "成交量较前五日均值放大三倍以上（交易所公开信息口径，含盘中异动提示与收盘后复核说明）"
    )
    rows = [
        {"symbol": f"00{i:04d}", "name": f"样本股{i}", "net_buy": 1.2345e8 - i * 1e6,
         "buy_amt": 2.3456e8, "sell_amt": 1.1111e8, "change_pct": 5.55,
         "turnover_pct": 12.34, "reason": long_reason}
        for i in range(60)
    ]
    sell_rows = [
        {"symbol": f"60{i:04d}", "name": f"样本卖出股{i}", "net_buy": -(1.3456e8 - i * 1e6),
         "buy_amt": 1.1111e8, "sell_amt": 2.4567e8, "change_pct": -6.66,
         "turnover_pct": 15.43, "reason": long_reason}
        for i in range(20)
    ]
    seats = [
        {"side": "buy", "symbol": f"00{i:04d}", "operate_dept_name": f"某某证券营业部{i}",
         "operate_dept_code": f"D{i:03d}", "buy_amt": 9.8765e7 - i * 1e5,
         "sell_amt": 0.0, "net_amt": 9.8765e7 - i * 1e5}
        for i in range(60)
    ]
    _patch(
        monkeypatch,
        {
            "lhb_date": [{"d": "20261020"}],
            "lhb_rank_count": [{"n": 60, "syms": 60}],
            "lhb_top_buy": rows,
            "lhb_top_sell": sell_rows,
            "lhb_seats_top": seats,
            "lhb_seat_total": [{"n": 60}],
        },
    )
    monkeypatch.setattr(tc, "MAX_DOMAIN_CHARS", 20000)
    full = _query({"domain": "lhb.daily", "params_json": '{"date": "20261020"}'})
    assert len(full) > 1200, f"样本不够长，测不到截断: {len(full)}"

    monkeypatch.setattr(tc, "MAX_DOMAIN_CHARS", 1200)
    out = _query({"domain": "lhb.daily", "params_json": '{"date": "20261020"}'})
    assert len(out) <= 1200, len(out)
    assert "已截断" in out
    # 必需口径行必须活过截断（否则模型不知道席位金额是什么）
    assert "席位口径" in out, out
    # 除截断提示行外，每一行都必须是原文里的完整一行；被截的那一行只能按「；」断条目。
    full_lines = full.splitlines()
    for line in out.splitlines():
        if "已截断" in line:
            continue
        assert line in full_lines or (
            line.endswith("；") and any(fl.startswith(line) for fl in full_lines)
        ), f"截断切断了整行/半条记录: {line!r}"
    # 被边界截断的那一行以「；」收尾，不留半截数字
    for line in out.splitlines():
        assert not re.search(r"\d\.?\d*$", line), f"行尾是半截数字: {line!r}"
    assert "+1.23亿" in out or "+1.23亿" in full


def test_mixed_review_question_routes_to_lhb_capable_recipe():
    """「收盘复盘，龙虎榜和涨停梯队」不能只落进盘后管线：管线取不到龙虎榜。

    postmarket_reviewer 命中「收盘/复盘/涨停梯队」得分 24，lhb_reviewer 只命中「龙虎榜」
    得 9 —— 旧实现直接判给管线，而 pipeline 按 {} 调工具，query_data_domain 必填 domain
    会被判 unsatisfiable 静默跳过：龙虎榜在这类问句里永远取不到。混合问句必须落在一份
    既能调 query_data_domain(domain=lhb.daily)、也能查涨停梯队的非 pipeline 配方上。

    @spec LHB-001
    """
    from src.agents.chat.registry import chat_tool_schemas
    from src.harness.routing import route_for_request

    q = "收盘复盘，龙虎榜和涨停梯队"
    route = route_for_request(q, chat_tool_schemas())
    assert route.recipe is not None, f"{q} 落到无配方兜底"
    assert not route.recipe.pipeline, f"{q} → {route.recipe.id}（pipeline 取不到龙虎榜）"
    names = {(s.get("function") or {}).get("name") for s in route.tools}
    assert "query_data_domain" in names, f"{q} 路由后看不到取数入口"
    frag = route.recipe.prompt_fragment
    assert "lhb.daily" in frag, "提示词必须指明龙虎榜域"
    assert "limit.pool" in frag, "同句问涨停梯队时也必须指明 limit.pool，不能丢掉这一半"

    # 不含龙虎榜的纯盘后复盘仍走原管线
    plain = route_for_request("收盘复盘和涨停梯队", chat_tool_schemas())
    assert plain.recipe is not None and plain.recipe.id == "postmarket_reviewer"


def test_board_ranking_scope_and_multi_reason_are_spelled_out(monkeypatch):
    """榜单必须写清「按个股排名」与同股多原因的处理方式，不能只写一句「合并为一条」。

    @spec LHB-004
    """
    _patch(
        monkeypatch,
        {
            "lhb_date": [{"d": "20261020"}],
            "lhb_rank_count": [{"n": 92, "syms": 41}],
            "lhb_top_buy": [
                {"symbol": "002361", "name": "闽东电力", "net_buy": 3.0e8, "buy_amt": 4.0e8,
                 "sell_amt": 1.0e8, "change_pct": 9.98, "reason": "日涨幅偏离值达7%"},
                {"symbol": "002361", "name": "闽东电力", "net_buy": 2.1e8, "buy_amt": 3.0e8,
                 "sell_amt": 0.9e8, "change_pct": 9.98, "reason": "连续三个交易日涨幅偏离值累计20%"},
                {"symbol": "600111", "name": "北方稀土", "net_buy": 1.1e8, "buy_amt": 2.0e8,
                 "sell_amt": 0.9e8, "change_pct": 6.1, "reason": "涨幅偏离"},
                {"symbol": "600222", "name": "太龙药业", "net_buy": 9e7, "buy_amt": 1.5e8,
                 "sell_amt": 0.6e8, "change_pct": 5.0, "reason": "涨幅偏离"},
                {"symbol": "600333", "name": "长春燃气", "net_buy": 8e7, "buy_amt": 1.4e8,
                 "sell_amt": 0.6e8, "change_pct": 4.0, "reason": "涨幅偏离"},
                {"symbol": "600444", "name": "国机通用", "net_buy": 7e7, "buy_amt": 1.3e8,
                 "sell_amt": 0.6e8, "change_pct": 3.0, "reason": "涨幅偏离"},
                {"symbol": "600555", "name": "海立股份", "net_buy": 6e7, "buy_amt": 1.2e8,
                 "sell_amt": 0.6e8, "change_pct": 2.0, "reason": "涨幅偏离"},
            ],
            "lhb_top_sell": [],
            "lhb_seats_top": [],
            "lhb_seat_total": [{"n": 0}],
        },
    )
    out = _query({"domain": "lhb.daily", "params_json": '{"date": "20261020"}'})
    assert "按个股" in out, out                       # 排名单位是个股，不是上榜记录
    assert "同股" in out and "净额" in out, out        # 同股多原因取哪条、怎么合并
    assert "92" in out and "41" in out, out
    # 代表行要带上「另有 N 个上榜原因」，不能把多原因悄悄丢掉
    assert "原因" in out
    # 第 6/7 只确实没列（截断披露与内容一致）
    assert "600555" not in out


def test_symbol_path_lists_every_listing_reason(monkeypatch):
    """单标的路径要把该股当日**全部**上榜原因列出来，不能只留净额最大那条。

    @spec LHB-004
    """
    ev = {"symbol": "002361", "name": "闽东电力", "net_buy": 1.2e8, "buy_amt": 2.0e8,
          "sell_amt": 0.8e8, "change_pct": 9.98}
    _patch(
        monkeypatch,
        {
            "lhb_date": [{"d": "20261020"}],
            "lhb_events_symbol": [
                dict(ev, reason="日涨幅偏离值达7%"),
                dict(ev, net_buy=1.19e8, reason="连续三个交易日收盘价涨幅偏离值累计20%"),
            ],
            "lhb_seats": [],
        },
    )
    out = _query({"domain": "lhb.daily", "params_json": '{"symbol": "002361"}'})
    assert "日涨幅偏离值达7%" in out, out
    assert "连续三个交易日收盘价涨幅偏离值累计20%" in out, out
    assert "2 条" in out or "2 个" in out, out




def test_mixed_review_route_executes_both_domains_through_chat_tool_layer(tmp_path):
    """混合问句全链路：路由 → 配方白名单 → _REQ_TOOLS → _execute_tool → 真 SQL。

    「收盘复盘，龙虎榜和涨停梯队」必须同时能取到 lhb.daily 与 limit.pool —— 只落进
    pipeline 配方时两个域都调不到（管线按 {} 拼参数）。这里用临时 sqlite 真跑一遍，
    证明模型在这条路由下**真的**能拿到龙虎榜席位与涨停梯队两段数据。

    @spec LHB-001
    """
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from src.agents.chat.registry import chat_tool_schemas
    from src.harness.routing import route_for_request
    from src.web.api import chat as chat_api

    q = "收盘复盘，龙虎榜和涨停梯队"
    route = route_for_request(q, chat_tool_schemas())
    recipe = route.recipe
    assert recipe is not None and recipe.id == "lhb_reviewer"
    tools = tuple(getattr(recipe, "tools", ()))
    assert {"query_data_domain", "get_market_replay"} <= set(tools)

    engine = create_engine(f"sqlite:///{tmp_path / 'lhb_mixed.db'}")
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
            "accum_volume REAL, buy_amt REAL, sell_amt REAL, net_amt REAL)"
        ))
        # limit.pool 所需三表（空表即可：本条断言的是「能查、且如实说无记录」）
        conn.execute(text(
            "CREATE TABLE limit_up_events (trade_date TEXT, symbol TEXT, name TEXT, "
            "sealed INTEGER, boards INTEGER, first_time TEXT, open_times INTEGER)"
        ))
        conn.execute(text(
            "INSERT INTO dragon_tiger_events VALUES "
            "('20261020','002361','CN','闽东电力','日涨幅偏离值达7%',12.5,9.98,1.23e8,"
            "2.1e8,0.87e8,2.97e8,12.3)"
        ))
        conn.execute(text(
            "INSERT INTO lhb_seat_details VALUES "
            "('u1','20261020','buy','002361','','涨幅偏离','D001','机构专用',12.5,9.98,"
            "2.97e8,100,8.2e7,0.0,8.2e7)"
        ))

    token = chat_api._REQ_TOOLS.set(frozenset(tools))
    try:
        with Session(engine) as s:
            lhb = _run(chat_api._execute_tool(
                s, "query_data_domain",
                {"domain": "lhb.daily", "params_json": '{"date": "20261020"}'}, user=None,
            ))
            pool = _run(chat_api._execute_tool(
                s, "query_data_domain",
                {"domain": "limit.pool", "params_json": '{"date": "20261020"}'}, user=None,
            ))
    finally:
        chat_api._REQ_TOOLS.reset(token)
    assert "不使用工具" not in lhb, lhb[:200]
    assert "时点 20261020" in lhb and "闽东电力" in lhb and "8200万" in lhb
    assert "limit.pool" in pool, pool[:200]


def test_symbol_level_pipeline_is_not_hijacked_by_lhb_keyword():
    """反例：「深度诊断 002361 龙虎榜」不能被改路由到市场级龙虎榜配方。

    单标的管线（deep_diagnosis）有自己的 14 个维度证据链，龙虎榜只是其中一句提到的
    维度；为补一个域而丢掉其余维度，是另一种失真。改路由只对 scope=market 的
    pipeline 配方生效。
    """
    from src.agents.chat.registry import chat_tool_schemas
    from src.harness.routing import route_for_request

    route = route_for_request("深度诊断 002361 的龙虎榜和筹码", chat_tool_schemas())
    assert route.recipe is not None and route.recipe.id == "deep_diagnosis", route.recipe


# ─────────── 10. 三轮审查复现：covers_domains 改路由只限盘后复盘 / 记录数与只数口径 ───────────


def test_seat_keyword_does_not_hijack_position_or_intraday_recipes():
    """「席位」只是问句里附带的一句，不能把持仓/盘中/竞价工具集换掉。

    旧实现：只要「pipeline + scope=market」胜出且另有配方声明了 covers_domains 就改路由。
    「持仓要不要减，看看席位」里持仓(4分)与席位(4分)打平 → intraday_watcher 先命中取胜，
    随后被改跳到 lhb_reviewer，`get_portfolio` 与持仓/异动/破位工具全被丢掉 —— 用户问的是
    **要不要减仓**，拿到的却是一份龙虎榜复盘。改路由只允许在盘后复盘场景发生
    （lhb_reviewer 显式声明 `rescues_pipelines: [postmarket_reviewer]`）。

    @spec LHB-001
    """
    from src.agents.chat.registry import chat_tool_schemas
    from src.harness.routing import route_for_request

    cases = [
        ("持仓要不要减，看看席位", "intraday_watcher", "get_portfolio"),
        ("盘中持仓破位，顺便看下席位", "intraday_watcher", "get_portfolio"),
        ("竞价高开要不要看席位", "auction_hunter", None),
    ]
    for q, rid, tool in cases:
        route = route_for_request(q, chat_tool_schemas())
        assert route.recipe is not None, q
        assert route.recipe.id == rid, f"{q} → {route.recipe.id}（应保留原配方）"
        names = {(s.get("function") or {}).get("name") for s in route.tools}
        if tool:
            assert tool in names, f"{q} 的工具集被换掉了: {sorted(names)[:10]}"
        assert "query_data_domain" not in names, f"{q} 不该改走取数域配方"


def test_lhb_recipe_rescue_is_declared_and_scoped_to_postmarket():
    """改路由的授权写在配方里，而不是「任何市场级管线都能被抢」—— 只点名盘后复盘。"""
    from src.harness.recipes import get_recipe

    lhb = get_recipe("lhb_reviewer")
    assert lhb is not None
    assert tuple(lhb.rescues_pipelines) == ("postmarket_reviewer",)
    for rid in ("intraday_watcher", "auction_hunter", "premarket_strategist", "deep_diagnosis"):
        assert rid not in lhb.rescues_pipelines, f"{rid} 不在盘后复盘场景，不许被改路由"


def test_truncation_reports_record_count_and_stock_count_separately(monkeypatch):
    """截断说明里的「净买记录 N 条」必须是真的记录条数，不是按个股去重后的只数。

    旧实现写 `净买记录 {len(all_buys)} 条`，而 all_buys 是 `_rank_board` 按个股去重后的
    结果：8 条上榜记录（6 只个股）会被写成「净买记录 6 条」，读者据此判断当日上榜规模会
    少算。记录数与只数必须分开写。

    @spec LHB-004
    """
    buy_rows = [
        {"symbol": "002361", "name": "闽东电力", "net_buy": 3.0e8, "buy_amt": 4.0e8,
         "sell_amt": 1.0e8, "change_pct": 9.98, "reason": "日涨幅偏离值达7%"},
        {"symbol": "002361", "name": "闽东电力", "net_buy": 2.1e8, "buy_amt": 3.0e8,
         "sell_amt": 0.9e8, "change_pct": 9.98, "reason": "连续三个交易日涨幅偏离值累计20%"},
        {"symbol": "600111", "name": "北方稀土", "net_buy": 1.1e8, "buy_amt": 2.0e8,
         "sell_amt": 0.9e8, "change_pct": 6.1, "reason": "涨幅偏离"},
        {"symbol": "600111", "name": "北方稀土", "net_buy": 9e7, "buy_amt": 1.8e8,
         "sell_amt": 0.9e8, "change_pct": 6.1, "reason": "连续三日涨幅偏离"},
        *[
            {"symbol": f"60022{i}", "name": f"样本{i}", "net_buy": 8e7 - i * 1e7,
             "buy_amt": 1.4e8, "sell_amt": 6e7, "change_pct": 4.0, "reason": "涨幅偏离"}
            for i in range(4)
        ],
    ]
    sell_rows = [
        {"symbol": "300750", "name": "宁德时代", "net_buy": -4.2e8, "buy_amt": 1.0e8,
         "sell_amt": 5.2e8, "change_pct": -8.4, "reason": "跌幅偏离"},
        {"symbol": "300750", "name": "宁德时代", "net_buy": -3.0e8, "buy_amt": 1.0e8,
         "sell_amt": 4.0e8, "change_pct": -8.4, "reason": "连续三日跌幅偏离"},
    ]
    _patch(
        monkeypatch,
        {
            "lhb_date": [{"d": "20261020"}],
            "lhb_rank_count": [{"n": 9, "syms": 6}],
            "lhb_top_buy": buy_rows,
            "lhb_top_sell": sell_rows,
            "lhb_seats_top": [],
            "lhb_seat_total": [{"n": 0}],
        },
    )
    out = _query({"domain": "lhb.daily", "params_json": '{"date": "20261020"}'})
    assert "已截断" in out, out
    # 8 条净买记录、按个股 6 只；净卖 2 条记录、1 只
    assert "净买 8 条记录 / 6 只" in out, out
    assert "净卖 2 条记录 / 1 只" in out, out
    # 不是把去重后的 6 只误写成 6 条记录
    assert "净买 6 条记录" not in out, out
