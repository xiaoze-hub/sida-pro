# -*- coding: utf-8 -*-
"""数据域架构 P0 验收（2026-10-21）：list_data_domains + query_data_domain。

覆盖 7 条硬要求（缺一条就是这套架构跑偏）：
1. 目录返回 26 个域且 ≤2500 字（模型上下文预算）；
2. 域不在白名单 → 明确拒绝，并指回 list_data_domains；
3. 必填参数（symbol）缺省 → 拒绝并说明，不猜代码；
4. 渲染函数对「空结果」写「未取到」，**不写 0、不编造**；
5. 域白名单与 `src/harness/data_catalog.yaml` 的 id 集合**完全一致**（防漂移）；
6. 本模块**不依赖 Web 层**（源码级断言，杜绝反向依赖把 core 拖回 web）；
7. 只读 SQL 必带 LIMIT（静态断言，防全表扫回归）。

另加：已有工具的域必须**转发到原 handler**（同一口径），不能在这里重写取数。
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents.chat import tools_catalog as tc  # noqa: E402

_SOURCE = (ROOT / "src" / "agents" / "chat" / "tools_catalog.py").read_text(encoding="utf-8")
_CATALOG_YAML = ROOT / "src" / "harness" / "data_catalog.yaml"


def _run(coro):
    return asyncio.run(coro)


# ──────────────────────────── 1. 目录 ────────────────────────────

def test_catalog_lists_26_domains_within_budget():
    out = tc.render_domain_catalog()
    assert len(out) <= tc.MAX_CATALOG_CHARS <= 2500
    ids = [line.split(" | ")[0] for line in out.splitlines() if " | " in line]
    assert len(ids) == 26
    assert set(ids) == tc.DOMAIN_IDS
    # 一行摘要必须带「必填参数 + 覆盖什么」，否则模型无法选域
    assert all(line.count(" | ") >= 3 for line in out.splitlines()[1:] if " | " in line)
    # 首行是一句话说明
    assert out.splitlines()[0].startswith("平台可查数据域")


def test_catalog_tool_returns_same_catalog():
    out = _run(tc._tool_list_data_domains(None, {}))
    assert out == tc.render_domain_catalog()


# ──────────────────────────── 2/3. 拒绝路径 ────────────────────────────

def test_unknown_domain_rejected():
    out = _run(tc.query_data_domain(None, {"domain": "stock.nonexistent"}))
    assert "未知数据域" in out
    assert "list_data_domains" in out
    # 拒绝时不得泄露/猜测表名
    assert "SELECT" not in out.upper()


def test_missing_domain_name_rejected():
    out = _run(tc.query_data_domain(None, {}))
    assert "请提供数据域" in out and "list_data_domains" in out


@pytest.mark.parametrize(
    "domain",
    ["stock.chip", "stock.flow", "stock.intraday", "stock.factor", "stock.quote", "stock.fundamental"],
)
def test_missing_symbol_rejected(domain):
    out = _run(tc.query_data_domain(None, {"domain": domain, "params_json": "{}"}))
    assert "缺少必填参数" in out and "symbol" in out
    # 拒绝时不能返回任何数字读数
    assert "现价" not in out and "净额" not in out


def test_bad_params_json_rejected():
    out = _run(tc.query_data_domain(None, {"domain": "stock.chip", "params_json": "{oops"}))
    assert "params_json" in out and "不是合法 JSON" in out


def test_no_domain_is_left_unimplemented():
    """26 个域全部接线：没有域再报「尚未接入实现 / P1 待接」。"""
    import yaml

    raw = yaml.safe_load(_CATALOG_YAML.read_text(encoding="utf-8"))
    assert len(raw["domains"]) == 26
    for d in raw["domains"]:
        domain = str(d["id"])
        assert str(d.get("source") or "").strip(), f"{domain} 的 source 不能为空"
        assert "P1" not in str(d.get("desc") or ""), f"{domain} 的 desc 还写着 P1"
    out = _run(tc.query_data_domain(None, {"domain": "market.mainline"}))
    assert "尚未接入实现" not in out
    assert "P1 待接" not in out


def test_all_twenty_six_domains_are_wired(monkeypatch):
    """全部 26 个域都能路由到实现（转发层给占位，读库层全当空表）。"""
    import yaml

    _canned_reads(monkeypatch, {})   # 不碰真库：全部当空表处理

    async def _fake(name, db, args, user):   # 不碰网络：转发层直接给占位
        return f"[{name}] 占位"

    monkeypatch.setattr(tc, "_delegate", _fake)
    raw = yaml.safe_load(_CATALOG_YAML.read_text(encoding="utf-8"))
    domains = [str(d["id"]) for d in raw["domains"]]
    assert len(domains) == 26
    for domain in sorted(domains):
        out = _run(tc.query_data_domain(None, {"domain": domain, "params_json": '{"symbol": "002361"}'}))
        assert "尚未接入实现" not in out, f"{domain} 未接线"
        assert "未知数据域" not in out, f"{domain} 未识别"
        assert len(out) <= tc.MAX_DOMAIN_CHARS


# ──────────────────────────── 4. 空结果不补 0 ────────────────────────────

def test_empty_render_says_missing_not_zero():
    out = tc.render_domain_sections(
        "stock.chip", as_of="2026-10-20", source="chip_distribution", sections=[], missing=[]
    )
    assert "未取到" in out
    assert re.search(r"(?<![\d.])0(?![\d.])", out) is None, out


def test_empty_limit_pool_says_missing():
    out = tc.render_limit_pool(as_of="未知", counts={}, ladder_rows=[], strength_rows=[], blown=[])
    assert "未取到" in out
    assert "0 家" not in out and "封板率" not in out


def test_empty_lhb_says_missing():
    out = tc.render_lhb(as_of="未知", events=[], seats=[])
    assert "未取到" in out
    assert "0 条" not in out


def test_render_caps_at_1200_chars():
    out = tc.render_domain_sections(
        "limit.pool", as_of="2026-10-20", source="t", sections=["- " + "很长的内容" * 400], missing=[]
    )
    assert len(out) <= tc.MAX_DOMAIN_CHARS
    assert "已截断" in out


def test_lhb_missing_amount_is_not_zero():
    """净买额缺值必须写「未披露」，不能拿 0 冒充。"""
    out = tc.render_lhb(
        as_of="20261020",
        events=[{"symbol": "002361", "name": "闽东电力", "net_buy": None, "buy_amt": 1.2e8,
                 "sell_amt": None, "change_pct": 9.98, "turnover_pct": None, "reason": "涨幅偏离"}],
        seats=[],
    )
    assert "未披露" in out
    assert "+0.00亿" not in out


# ──────────────────────────── 5. 白名单 == yaml ────────────────────────────

def test_whitelist_matches_yaml_exactly():
    import yaml

    raw = yaml.safe_load(_CATALOG_YAML.read_text(encoding="utf-8"))
    yaml_ids = {str(d["id"]) for d in raw["domains"]}
    assert yaml_ids == tc.DOMAIN_IDS
    assert yaml_ids == {d["id"] for d in tc.DOMAINS}
    assert len(yaml_ids) == 26


def test_required_params_come_from_yaml():
    assert tc.required_params("stock.chip") == ("symbol",)
    assert tc.required_params("limit.pool") == ()
    assert tc.required_params("lhb.daily") == ()  # symbol 可选（全市场榜单也有意义）


# ──────────────────────────── 6/7. 静态约束 ────────────────────────────

def test_no_web_layer_import():
    assert "from src.web" not in _SOURCE
    assert "import src.web" not in _SOURCE
    # 只允许 ORM 中立层的会话入口
    assert "from src.db.session import SessionLocal" in _SOURCE


def test_all_read_sql_has_limit_and_binding():
    assert tc._SQL, "只读语句表不应为空"
    for key, sql in tc._SQL.items():
        upper = sql.upper()
        assert upper.lstrip().startswith("SELECT"), f"{key} 必须是 SELECT"
        assert "LIMIT" in upper, f"{key} 缺 LIMIT（禁止全表扫）"
        assert ";" not in sql, f"{key} 不应带分号拼接多条语句"
    # 禁止 f-string 拼 SQL：语句必须是常量，参数走绑定
    assert 'f"SELECT' not in _SOURCE and "f'SELECT'" not in _SOURCE
    assert "text(" in _SOURCE and "session.execute(text(" in _SOURCE


# ──────────────────────────── 8. 已有工具优先转发（同一口径） ────────────────────────────

@pytest.mark.parametrize(
    "domain,expect",
    [
        ("stock.chip", "get_chip_accumulation"),
        ("stock.intraday", "get_intraday_profile"),
        ("stock.factor", "get_stock_factors"),
        ("market.sentiment", "get_sentiment_cycle"),
    ],
)
def test_delegates_to_existing_handler(monkeypatch, domain, expect):
    calls: list[str] = []

    async def _fake(name, db, args, user):
        calls.append(name)
        return f"[{name}] 占位读数"

    monkeypatch.setattr(tc, "_delegate", _fake)
    out = _run(tc.query_data_domain(None, {"domain": domain, "params_json": json.dumps({"symbol": "002361"})}))
    assert calls == [expect]
    assert expect in out and "未取到" not in out


def test_stock_flow_merges_intent_and_cancel(monkeypatch):
    calls: list[str] = []

    async def _fake(name, db, args, user):
        calls.append(name)
        return f"[{name}] 占位读数"

    monkeypatch.setattr(tc, "_delegate", _fake)
    out = _run(tc.query_data_domain(None, {"domain": "stock.flow", "params_json": '{"symbol": "002361"}'}))
    assert calls == ["get_main_intent", "get_cancel_order_intent"]
    assert "get_main_intent" in out and "get_cancel_order_intent" in out


def test_delegate_failure_is_reported_not_faked(monkeypatch):
    async def _empty(name, db, args, user):
        return ""

    monkeypatch.setattr(tc, "_delegate", _empty)
    out = _run(tc.query_data_domain(None, {"domain": "stock.flow", "params_json": '{"symbol": "002361"}'}))
    assert "未取到" in out


# ──────────────────────────── 9. 自写域（limit.pool / lhb.daily）取数 → 渲染 ────────────────────────────

def _canned_reads(monkeypatch, table: dict[str, list[dict]]):
    monkeypatch.setattr(tc, "_read", lambda db, key, params=None: list(table.get(key, [])))
    monkeypatch.setattr(
        tc, "_read_one", lambda db, key, params=None: (list(table.get(key, [])) or [None])[0]
    )


def test_limit_pool_renders_ladder_and_strength(monkeypatch):
    _canned_reads(
        monkeypatch,
        {
            "limit_date": [{"d": "20261020"}],
            "limit_counts": [{"total": 3, "sealed": 2}],
            "limit_dates": [{"trade_date": "20261016"}, {"trade_date": "20261017"},
                            {"trade_date": "20261020"}],
            "limit_history": [
                {"trade_date": "20261016", "symbol": "002361", "name": "闽东电力", "is_sealed_close": 1},
                {"trade_date": "20261017", "symbol": "002361", "name": "闽东电力", "is_sealed_close": 1},
                {"trade_date": "20261020", "symbol": "002361", "name": "闽东电力", "is_sealed_close": 1},
                {"trade_date": "20261020", "symbol": "600111", "name": "北方稀土", "is_sealed_close": 0},
            ],
            "limit_events": [
                {"symbol": "002361", "name": "闽东电力", "is_sealed_close": 1},
                {"symbol": "600111", "name": "北方稀土", "is_sealed_close": 0},
            ],
            "limit_strength": [
                {"symbol": "002361", "name": "闽东电力", "strength_label": "S",
                 "seal_ratio": 0.0123, "first_time": "09:31", "open_times": 0},
            ],
        },
    )
    out = _run(tc.query_data_domain(None, {"domain": "limit.pool", "params_json": '{"date": "20261020"}'}))
    assert "时点 20261020" in out
    assert "触板 3 家" in out and "封住 2 家" in out
    assert "3板" in out and "闽东电力" in out          # 连板数由事件表序列推出（未用 limit_days）
    assert "强度 S" in out and "封成比 1.23%" in out
    assert "未取到" not in out


def test_limit_pool_min_streak_filters(monkeypatch):
    _canned_reads(
        monkeypatch,
        {
            "limit_date": [{"d": "20261020"}],
            "limit_counts": [{"total": 2, "sealed": 2}],
            "limit_dates": [{"trade_date": "20261019"}, {"trade_date": "20261020"}],
            "limit_history": [
                {"trade_date": "20261019", "symbol": "002361", "name": "闽东电力", "is_sealed_close": 1},
                {"trade_date": "20261020", "symbol": "002361", "name": "闽东电力", "is_sealed_close": 1},
                {"trade_date": "20261020", "symbol": "600111", "name": "北方稀土", "is_sealed_close": 1},
            ],
            "limit_events": [{"symbol": "002361", "is_sealed_close": 1}],
            "limit_strength": [],
        },
    )
    out = _run(tc.query_data_domain(
        None, {"domain": "limit.pool", "params_json": '{"min_streak": 2}'}
    ))
    assert "2板 1 只（闽东电力）" in out
    assert "北方稀土" not in out            # 首板被 min_streak 过滤
    assert "未取到" in out                  # 强度影子表无行 → 如实说


def test_lhb_renders_board_and_seats(monkeypatch):
    _canned_reads(
        monkeypatch,
        {
            "lhb_date": [{"d": "20261020"}],
            "lhb_events": [
                {"symbol": "002361", "name": "闽东电力", "reason": "日涨幅偏离值达7%", "net_buy": 1.23e8,
                 "buy_amt": 2.1e8, "sell_amt": 0.87e8, "change_pct": 9.98, "turnover_pct": 12.3},
                {"symbol": "600111", "name": "北方稀土", "reason": "日跌幅偏离值达7%", "net_buy": -0.5e8,
                 "buy_amt": 0.3e8, "sell_amt": 0.8e8, "change_pct": -7.1, "turnover_pct": 5.2},
            ],
            "lhb_seats": [],
            "lhb_seats_top": [
                {"side": "buy", "symbol": "002361", "operate_dept_name": "机构专用", "accum_amount": 8.2e7},
                {"side": "sell", "symbol": "600111", "operate_dept_name": "某游资营业部", "accum_amount": 4.1e7},
            ],
        },
    )
    out = _run(tc.query_data_domain(None, {"domain": "lhb.daily"}))
    assert "时点 20261020" in out
    assert "上榜记录 2 条" in out
    assert "净买入前列" in out and "+1.23亿" in out
    assert "净卖出前列" in out and "-5000万" in out
    assert "机构专用" in out and "买入席位" in out
    assert "未取到" not in out            # 榜单与席位都取到了 → 不该出现缺数据标记


def test_lhb_symbol_path(monkeypatch):
    _canned_reads(
        monkeypatch,
        {
            "lhb_date": [{"d": "20261020"}],
            "lhb_events_symbol": [
                {"symbol": "002361", "name": "闽东电力", "net_buy": 1e7, "change_pct": 9.98,
                 "buy_amt": 2e7, "sell_amt": 1e7, "turnover_pct": 3.0, "reason": "涨幅偏离"},
            ],
            "lhb_seats": [
                {"side": "buy", "symbol": "002361", "operate_dept_name": "机构专用", "accum_amount": 8.2e7},
            ],
        },
    )
    out = _run(tc.query_data_domain(
        None, {"domain": "lhb.daily", "params_json": '{"symbol": "002361"}'}
    ))
    assert "闽东电力" in out and "买入席位" in out
    assert "未取到" not in out


def test_theme_mood_renders_five_dims(monkeypatch):
    _canned_reads(
        monkeypatch,
        {
            "theme_date": [{"d": "20261020"}],
            "theme_rows": [
                {"trade_date": "20261020", "block_code": "880001", "block_name": "半导体",
                 "score": 82.3, "s1": 88, "s2": 76, "s3": 71, "s4": 64, "s5": 59,
                 "confidence": 80, "core": 1, "limit_up_cnt": 12, "touched_cnt": 15,
                 "max_boards": 4, "ge2_cnt": 5,
                 "core_stocks": json.dumps([{"symbol": "002361", "name": "闽东电力", "boards": 3}])},
                {"trade_date": "20261020", "block_code": "880002", "block_name": "稀土",
                 "score": 55.0, "s1": 50, "s2": 51, "s3": None, "s4": 40, "s5": 45,
                 "confidence": 60, "core": 0, "limit_up_cnt": 3, "touched_cnt": 4,
                 "max_boards": 1, "ge2_cnt": 0, "core_stocks": "[]"},
            ],
            "theme_funds": [
                {"block_code": "880001", "date": "2026-10-20", "change_pct": 2.31, "fund_net": 1.2e8},
            ],
        },
    )
    out = _run(tc.query_data_domain(None, {"domain": "theme.mood", "params_json": '{"top_n": 5}'}))
    assert "时点 20261020" in out
    assert "半导体" in out and "总分 82.3" in out
    assert "结构 88/扩散 76/核心 71/接力 64/持续 59" in out
    assert "★核心" in out
    assert "龙头 002361 闽东电力(3板)" in out
    assert "板块资金 涨跌 +2.31% 净流入 +1.20亿" in out
    assert "未披露" in out                       # s3 缺值不补 0
    assert "board_daily 在 2026-10-20 无板块资金行" not in out   # 有资金行 → 不该报缺
    assert len(out) <= tc.MAX_DOMAIN_CHARS


def test_sql_failure_degrades_to_missing(monkeypatch):
    def _boom(db, key, params=None):
        raise RuntimeError("table missing")

    monkeypatch.setattr(tc, "_read", _boom)
    monkeypatch.setattr(tc, "_read_one", _boom)
    out = _run(tc.query_data_domain(None, {"domain": "limit.pool"}))
    assert "未取到" in out and "RuntimeError" in out
    assert "触板" not in out


# ──────────────────────────── 10. P1 六域（2026-10-22）：market.index / market.breadth /
#                                  board.daily / stock.fundamental / auction.flow / stock.quote

P1_SIX = ("market.index", "market.breadth", "board.daily", "stock.fundamental", "auction.flow", "stock.quote")


@pytest.mark.parametrize("domain", P1_SIX)
def test_p1_six_domains_recognised_and_header(monkeypatch, domain):
    """六个新域都能被识别、渲染非空文案，且首行带「时点 / 来源」。"""
    _canned_reads(monkeypatch, {})          # 空库

    async def _empty(name, db, args, user):
        return ""

    monkeypatch.setattr(tc, "_delegate", _empty)
    out = _run(tc.query_data_domain(None, {"domain": domain, "params_json": '{"symbol": "002361"}'}))
    assert domain in out
    assert "时点" in out and "来源" in out
    assert "尚未接入实现" not in out and "未知数据域" not in out
    assert "未取到" in out                  # 空库 + 空转发 → 必须说未取到，不能编数
    assert len(out) <= tc.MAX_DOMAIN_CHARS


@pytest.mark.parametrize("domain", P1_SIX)
def test_p1_six_empty_render_has_no_fake_zero(monkeypatch, domain):
    """空结果里不能出现冒充数值的 0（「未取到」而不是 0）。"""
    _canned_reads(monkeypatch, {})

    async def _empty(name, db, args, user):
        return ""

    monkeypatch.setattr(tc, "_delegate", _empty)
    out = _run(tc.query_data_domain(None, {"domain": domain, "params_json": '{"symbol": "002361"}'}))
    assert re.search(r"(?<![\d.])0(?![\d.])", out) is None, out


def test_market_index_renders_points_and_turnover(monkeypatch):
    _canned_reads(
        monkeypatch,
        {
            "idx_snapshot_ts": [{"d": "20261020", "ts": "2026-10-20 09:35:00"}],
            "idx_snapshot": [
                {"symbol": "sh000001", "name": "上证指数", "last": 3123.45, "prev_close": 3096.5,
                 "change_pct": 0.87, "turnover": 4.52e11, "source": "tencent"},
                {"symbol": "sz399001", "name": "深证成指", "last": 9876.5, "prev_close": 9800.0,
                 "change_pct": 0.78, "turnover": 5.31e11, "source": "tencent"},
                {"symbol": "sz399006", "name": "创业板指", "last": 1980.2, "prev_close": 2005.0,
                 "change_pct": -1.24, "turnover": 2.1e11, "source": "tencent"},
            ],
        },
    )
    out = _run(tc.query_data_domain(None, {"domain": "market.index"}))
    assert "时点 20261020 09:35:00" in out
    assert "上证指数（000001）" in out and "3123.45" in out and "+0.87%" in out
    assert "创业板指（399006）" in out and "-1.24%" in out
    assert "两市成交额 9830.00亿" in out       # 4.52e11 + 5.31e11 = 9.83e11
    assert "未取到" not in out


def test_market_index_falls_back_to_daily(monkeypatch):
    _canned_reads(
        monkeypatch,
        {
            "idx_snapshot_ts": [],
            "idx_daily_date": [{"d": "2026-10-20"}],
            "idx_daily": [
                {"code": "000001", "name": "上证指数", "close": 3100.0, "prev_close": 3070.0,
                 "amount": 4.0e11, "source": "marketdata"},
            ],
        },
    )
    out = _run(tc.query_data_domain(None, {"domain": "market.index"}))
    assert "时点 20261020" in out
    assert "来源 index_daily" in out
    assert "+0.98%" in out                    # 由 close/prev_close 反算
    assert "两市成交额：缺上证或深证成指成交额" in out     # 只有沪市 → 不补 0


def test_market_breadth_renders_series_dist_and_regime(monkeypatch):
    import src.collectors.market_breadth_series as mbs

    monkeypatch.setattr(
        mbs, "latest_with_percentile", lambda db, days=260: {"ok": True, "trade_date": "2026-10-20"}
    )
    monkeypatch.setattr(
        mbs, "render_text", lambda s: "· 涨跌家数：上涨 3200 / 下跌 1800 / 平盘 200（共 5200 只）"
    )
    _canned_reads(
        monkeypatch,
        {
            "breadth_dist": [{
                "trade_date": "2026-10-20", "total": 5200, "valid": 5200, "source": "sina",
                "items": json.dumps([
                    {"bucket": "跌停", "count": 3}, {"bucket": "<-5%", "count": 41},
                    {"bucket": "涨停", "count": 62},
                ]),
            }],
            "regime": [{
                "snapshot_date": "2026-10-20", "market": "CN", "regime": "bullish",
                "regime_score": 0.42, "confidence": 0.71, "breadth_up_pct": 61.5,
                "avg_change_pct": 0.83, "volatility_pct": 1.9, "active_ratio": 0.77,
                "sample_size": 5200,
            }],
        },
    )
    out = _run(tc.query_data_domain(None, {"domain": "market.breadth"}))
    assert "时点 2026-10-20" in out and "来源" in out
    assert "上涨 3200 / 下跌 1800" in out
    assert "跌停 3" in out and "涨停 62" in out
    assert "状态 偏多" in out and "+0.42" in out and "样本 5200" in out
    assert "未取到" not in out


def test_market_breadth_zero_valid_is_missing_not_zero(monkeypatch):
    import src.collectors.market_breadth_series as mbs

    monkeypatch.setattr(mbs, "latest_with_percentile", lambda db, days=260: {"ok": False, "reason": "no_data"})
    monkeypatch.setattr(mbs, "render_text", lambda s: "")
    _canned_reads(
        monkeypatch,
        {
            "breadth_dist": [{"trade_date": "2026-10-20", "total": 0, "valid": 0,
                              "items": json.dumps([{"bucket": "涨停", "count": 0}]), "source": "sina"}],
            "regime": [],
        },
    )
    out = _run(tc.query_data_domain(None, {"domain": "market.breadth"}))
    assert "未取到" in out
    assert "有效样本为 0" in out              # 如实说源不可用
    assert "涨停 0" not in out                # 不把 0 当读数展示


def test_board_daily_renders_rank_and_intraday_snapshot(monkeypatch):
    _canned_reads(
        monkeypatch,
        {
            "board_date": [{"d": "2026-10-20"}],
            "board_inflow": [
                {"block_code": "880001", "name": "半导体", "board_type": "industry",
                 "change_pct": 2.31, "fund_net": 1.23e9, "volume": 8.8e10},
                {"block_code": "880002", "name": "稀土永磁", "board_type": "concept",
                 "change_pct": 3.12, "fund_net": 6.6e8, "volume": None},
            ],
            "board_outflow": [
                {"block_code": "880003", "name": "白酒", "board_type": "industry",
                 "change_pct": -1.8, "fund_net": -9.1e8, "volume": 3.2e10},
            ],
            "board_flow": [{
                "ts": "2026-10-20 10:31:00", "board_type": "industry", "item_count": 2,
                "payload": json.dumps([
                    {"board_name": "半导体", "change_pct": 2.5, "net_inflow": 12.3,
                     "leader_name": "北方华创", "leader_change_pct": 5.6},
                ]),
            }],
        },
    )
    out = _run(tc.query_data_domain(None, {"domain": "board.daily"}))
    assert "时点 20261020" in out
    assert "半导体（行业）" in out and "净流入 +12.30亿" in out
    assert "白酒（行业）" in out and "净流入 -9.10亿" in out
    assert "盘中板块资金快照（2026-10-20 10:31:00" in out and "龙头 北方华创" in out
    assert "未取到" not in out


def test_stock_fundamental_forwards_existing_handler(monkeypatch):
    calls: list[tuple[str, dict]] = []

    async def _fake(name, db, args, user):
        calls.append((name, dict(args)))
        return "龙虎榜：002361 闽东电力 净买 +1.23亿；两融余额 12.3亿；分红 10派1.5元"

    monkeypatch.setattr(tc, "_delegate", _fake)
    out = _run(tc.query_data_domain(
        None, {"domain": "stock.fundamental", "params_json": '{"symbol": "002361"}'}
    ))
    assert calls == [("get_fundamentals_detail", {"symbol": "002361", "market": "CN"})]
    assert "get_fundamentals_detail" in out and "两融余额" in out
    assert "未取到" not in out


def test_auction_flow_symbol_reads_snapshot_and_ticks(monkeypatch):
    calls: list[str] = []

    async def _fake(name, db, args, user):
        calls.append(name)
        return "竞价池占位"

    monkeypatch.setattr(tc, "_delegate", _fake)
    _canned_reads(
        monkeypatch,
        {
            "auction_snapshot": [{
                "trade_date": "2026-10-20", "source": "tq", "auction_price": 14.62,
                "open_price": 14.62, "open_pct": 9.98, "open_amount": 2.31e8,
                "open_limit_buy": 1.2e7, "direction": "一字", "withdraw_rate_pre0920": 0.082,
            }],
            "auction_ticks": [{
                "tick_ts": "09:25:00", "ref_price": 14.62, "match_vol": 158000,
                "unmatched_vol": 420000, "withdraw_rate": 0.082, "verdict": "强",
            }],
        },
    )
    out = _run(tc.query_data_domain(
        None, {"domain": "auction.flow", "params_json": '{"symbol": "002361"}'}
    ))
    assert "时点 2026-10-20" in out
    assert "竞价价 14.62" in out and "开盘涨幅 +9.98%" in out
    assert "一字买量 12000000" in out and "方向 一字" in out
    assert "09:25:00 虚拟价 14.62" in out and "→ 强" in out
    assert calls == []                        # 库内有快照 → 不外呼
    assert "未取到" not in out


def test_auction_flow_market_reads_anomaly_pool(monkeypatch):
    _canned_reads(
        monkeypatch,
        {
            "auction_anomalies": [
                {"symbol": "002361", "name": "闽东电力", "gap_pct": 9.98, "withdraw_rate": 0.05,
                 "volume_ratio": 3.2, "note": "涨停抢筹", "created_at": "2026-10-20 09:25:03"},
                {"symbol": "600111", "name": "北方稀土", "gap_pct": -4.2, "withdraw_rate": 0.41,
                 "volume_ratio": 2.1, "note": "撤单砸盘", "created_at": "2026-10-20 09:25:01"},
            ],
        },
    )
    out = _run(tc.query_data_domain(None, {"domain": "auction.flow"}))
    assert "时点 2026-10-20 09:25" in out
    assert "竞价异动池（2 条）" in out
    assert "闽东电力" in out and "高开 +9.98%" in out and "北方稀土" in out
    assert "未取到" not in out


def test_auction_flow_symbol_falls_back_to_tool(monkeypatch):
    calls: list[str] = []

    async def _fake(name, db, args, user):
        calls.append(name)
        return "002361 竞价：开盘 +9.98%，竞价成交额 2.31亿"

    monkeypatch.setattr(tc, "_delegate", _fake)
    _canned_reads(monkeypatch, {"auction_snapshot": []})
    out = _run(tc.query_data_domain(
        None, {"domain": "auction.flow", "params_json": '{"symbol": "002361"}'}
    ))
    assert calls == ["get_auction_data"]
    assert "竞价成交额 2.31亿" in out
    assert "未取到" not in out


def test_stock_quote_prefers_fresh_db_row(monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    calls: list[str] = []

    async def _fake(name, db, args, user):
        calls.append(name)
        return "实时占位"

    monkeypatch.setattr(tc, "_delegate", _fake)
    ts = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    _canned_reads(
        monkeypatch,
        {"quote_latest": [{
            "symbol": "002361", "name": "闽东电力", "ts": ts, "last": 14.62, "change_pct": 9.98,
            "open": 14.0, "high": 14.62, "low": 13.9, "prev_close": 13.29, "volume": 1.2e6,
            "turnover": 1.75e8, "turnover_rate": 12.3, "volume_ratio": 2.4,
        }]},
    )
    out = _run(tc.query_data_domain(
        None, {"domain": "stock.quote", "params_json": '{"symbol": "002361"}'}
    ))
    assert "时点 " in out and "来源 quote_snapshots" in out
    assert "最新 14.62" in out and "+9.98%" in out and "换手 +12.30%" in out
    assert "行龄 ≤60s" in out
    assert calls == []                        # 库内够新 → 不转实时工具
    assert "未取到" not in out


def test_stock_quote_stale_row_forwards_tool(monkeypatch):
    calls: list[str] = []

    async def _fake(name, db, args, user):
        calls.append(name)
        return "002361 实时：14.70 (+10.02%)"

    monkeypatch.setattr(tc, "_delegate", _fake)
    _canned_reads(
        monkeypatch,
        {"quote_latest": [{
            "symbol": "002361", "name": "闽东电力", "ts": "2026-10-20 09:31:00",
            "last": 14.0, "change_pct": 5.0, "turnover": 1e8,
        }]},
    )
    out = _run(tc.query_data_domain(
        None, {"domain": "stock.quote", "params_json": '{"symbol": "002361"}'}
    ))
    assert calls == ["get_stock_quote"]
    assert "14.70 (+10.02%)" in out
    assert "超过 60s" in out and "转实时报价" in out
    assert "未取到" not in out


def test_stock_quote_stale_and_tool_dead_marks_expired(monkeypatch):
    async def _empty(name, db, args, user):
        return ""

    monkeypatch.setattr(tc, "_delegate", _empty)
    _canned_reads(
        monkeypatch,
        {"quote_latest": [{
            "symbol": "002361", "name": "闽东电力", "ts": "2026-10-20 09:31:00",
            "last": 14.0, "change_pct": 5.0,
        }]},
    )
    out = _run(tc.query_data_domain(
        None, {"domain": "stock.quote", "params_json": '{"symbol": "002361"}'}
    ))
    assert "过期快照" in out and "未取到" in out
    assert "14.00" in out                     # 过期值仍展示，但明确标注不可当实时


# ──────────────────────────── 11. P2 十二域（2026-10-22） ────────────────────────────
# 覆盖：26 域全部路由到实现 / 用户域 user=None 拒绝 / 空结果写「未取到」不补 0 /
#       每域输出含「时点」「来源」/ 用户域按 user_id 过滤 / 只读 SQL 全带 LIMIT。

#: 2026-10-22 新接的 12 个域。
P2_TWELVE = (
    "market.mainline",
    "strategy.signal",
    "stock.kline",
    "stock.tech",
    "stock.dossier",
    "stock.scenario",
    "portfolio.risk",
    "watchlist",
    "paper.trading",
    "notify.task",
    "backtest",
    "ops.config",
)

#: 用户私域（必须按 user_id 隔离；无登录态直接拒绝）。
USER_DOMAINS = ("portfolio.risk", "watchlist", "paper.trading", "notify.task")


class _FakeUser:
    """最小 user 替身（只带 id，够 _user_id 用）。"""

    def __init__(self, uid: str = "user-a"):
        self.id = uid


def _no_delegate(monkeypatch):
    """转发层全部返回空（不碰网络、不碰真库）。"""

    async def _empty(name, db, args, user):
        return ""

    monkeypatch.setattr(tc, "_delegate", _empty)


def _stub_reads(monkeypatch, table: dict[str, list[dict]]):
    """读库层喂假数据（同时覆盖 _read / _read_one / _read_safe / _read_one_safe）。"""

    def _read(db, key, params=None):
        return list(table.get(key, []))

    def _read_one(db, key, params=None):
        return (list(table.get(key, [])) or [None])[0]

    monkeypatch.setattr(tc, "_read", _read)
    monkeypatch.setattr(tc, "_read_one", _read_one)
    monkeypatch.setattr(tc, "_read_safe", lambda db, key, params=None, missing=None, label="": _read(db, key, params))
    monkeypatch.setattr(tc, "_read_one_safe", lambda db, key, params=None, missing=None, label="": _read_one(db, key, params))


# ── 11.1 全部 26 域都能路由到实现（不允许「P1 待接」） ────────────────────────────

def test_all_26_domains_route_to_an_implementation(monkeypatch):
    """目录里 26 个域每一个都能路由到实现 —— 没有域再报「尚未接入」。"""
    import yaml

    raw = yaml.safe_load(_CATALOG_YAML.read_text(encoding="utf-8"))
    domains = [str(d["id"]) for d in raw["domains"]]
    assert len(domains) == 26
    _stub_reads(monkeypatch, {})
    _no_delegate(monkeypatch)
    for domain in sorted(domains):
        out = _run(tc.query_data_domain(
            None,
            {"domain": domain, "params_json": '{"symbol": "002361"}'},
            _FakeUser(),
        ))
        assert "尚未接入实现" not in out, f"{domain} 报未接入"
        assert "P1 待接" not in out, f"{domain} 报 P1"
        assert "未知数据域" not in out, f"{domain} 未识别"
        assert out.strip(), f"{domain} 渲染为空"
        assert len(out) <= tc.MAX_DOMAIN_CHARS, f"{domain} 超 1200 字"


def test_every_domain_output_has_asof_and_source(monkeypatch):
    """每个域的首行都必须带「时点」与「来源」—— 缺了就不知道数据多旧、从哪来。"""
    import yaml

    raw = yaml.safe_load(_CATALOG_YAML.read_text(encoding="utf-8"))
    _stub_reads(monkeypatch, {})
    _no_delegate(monkeypatch)
    for d in raw["domains"]:
        domain = str(d["id"])
        out = _run(tc.query_data_domain(
            None,
            {"domain": domain, "params_json": '{"symbol": "002361"}'},
            _FakeUser(),
        ))
        head = out.splitlines()[0]
        assert domain in head, f"{domain} 首行没写域 id"
        assert "时点" in head, f"{domain} 首行缺时点"
        assert "来源" in head, f"{domain} 首行缺来源"


def test_p2_twelve_all_present_in_catalog():
    """这 12 个域必须在目录里且 source 非空（source 非空 = --check 能解析出真实表）。"""
    import yaml

    raw = yaml.safe_load(_CATALOG_YAML.read_text(encoding="utf-8"))
    by_id = {str(d["id"]): d for d in raw["domains"]}
    for domain in P2_TWELVE:
        assert domain in by_id, f"{domain} 不在目录里"
        assert str(by_id[domain].get("source") or "").strip(), f"{domain} 的 source 为空"
        assert "P1" not in str(by_id[domain].get("desc") or "")


# ── 11.2 用户私域：user=None 一律拒绝 ────────────────────────────

@pytest.mark.parametrize("domain", USER_DOMAINS)
def test_user_domains_reject_without_login(monkeypatch, domain):
    """无登录态（user=None）必须说「需要登录」，绝不返回任何数据。"""
    _stub_reads(monkeypatch, {
        "portfolio_positions": [{"symbol": "002361", "name": "闽东电力", "market": "CN",
                                 "quantity": 100, "cost_price": 14.0, "trading_style": "swing"}],
        "watchlist_rows": [{"symbol": "002361", "name": "闽东电力", "market": "CN",
                            "sort_order": 0, "updated_at": None}],
        "paper_account": [{"id": 1, "initial_capital": 1000000.0, "current_capital": 1000000.0,
                           "total_pnl": 0.0, "total_trades": 0, "winning_trades": 0,
                           "max_drawdown_pct": 0.0, "peak_capital": 1000000.0,
                           "enabled": True, "updated_at": None}],
        "paper_positions": [{"stock_symbol": "002361", "stock_market": "CN", "stock_name": "闽东电力",
                             "quantity": 100, "entry_price": 14.0, "current_price": 15.0,
                             "unrealized_pnl": 100.0, "status": "open"}],
        "copilot_schedules": [{"id": 1, "kind": "daily_brief", "at_hhmm": "15:30",
                               "weekdays": "mon-fri", "enabled": True, "last_run_date": "20261021",
                               "created_at": None}],
        "notify_rows": [{"title": "策略刷新完成", "category": "strategy", "level": "info",
                         "created_at": None, "read_at": None}],
    })
    _no_delegate(monkeypatch)
    out = _run(tc.query_data_domain(None, {"domain": domain}))
    assert "需要登录" in out, f"{domain} 未拒绝无登录态"
    assert "未取到" in out
    # 关键：不能泄露任何一条用户数据
    assert "闽东电力" not in out
    assert "002361" not in out
    assert "1000000" not in out and "1000000.00" not in out
    assert "daily_brief" not in out and "收盘小结" not in out


@pytest.mark.parametrize("domain", USER_DOMAINS)
def test_user_domains_only_query_own_user_id(monkeypatch, domain):
    """用户域的每条只读 SQL 都必须带 user_id 绑定参数（:uid），不能漏成全表。"""
    calls: list[tuple[str, dict]] = []

    def _read(db, key, params=None):
        calls.append((key, dict(params or {})))
        return []

    monkeypatch.setattr(tc, "_read", _read)
    monkeypatch.setattr(tc, "_read_one", lambda db, key, params=None: None)
    monkeypatch.setattr(tc, "_read_safe",
                        lambda db, key, params=None, missing=None, label="": _read(db, key, params))
    monkeypatch.setattr(tc, "_read_one_safe",
                        lambda db, key, params=None, missing=None, label="": None)

    async def _empty(name, db, args, user):
        return ""

    monkeypatch.setattr(tc, "_delegate", _empty)
    _run(tc.query_data_domain(None, {"domain": domain}, _FakeUser("uid-42")))

    user_scoped = [k for k in ("portfolio_positions", "watchlist_rows", "paper_account",
                               "paper_positions", "paper_trades", "copilot_schedules",
                               "notify_rows", "notify_channel_rows")]
    checked = [k for k, _ in calls if k in user_scoped]
    assert checked, f"{domain} 没读到任何用户表"
    for key, params in calls:
        if key in user_scoped:
            assert params.get("uid") == "uid-42", f"{key} 没按 user_id 过滤: {params}"


def test_paper_trading_and_watchlist_do_not_leak_other_users(monkeypatch):
    """模拟盘/自选的 SQL 里必须是 `user_id = :uid`（严格等值），不是全局行。"""
    sql = tc._SQL
    for key in ("paper_account", "paper_positions", "paper_trades", "watchlist_rows",
                "portfolio_positions", "copilot_schedules"):
        text = sql[key].upper()
        assert "USER_ID = :UID" in text, f"{key} 未按 user_id 严格过滤"
        assert "USER_ID IS NULL" not in text, f"{key} 不得混入全局行"


# ── 11.3 各域渲染：空结果写「未取到」，不补 0 ────────────────────────────

@pytest.mark.parametrize("domain", P2_TWELVE)
def test_p2_domains_empty_result_says_missing_not_zero(monkeypatch, domain):
    """空库 + 空转发 → 必须写「未取到」，且不得出现冒充数值的 0。"""
    _stub_reads(monkeypatch, {})
    _no_delegate(monkeypatch)
    out = _run(tc.query_data_domain(
        None, {"domain": domain, "params_json": '{"symbol": "002361"}'}, _FakeUser()
    ))
    assert "未取到" in out, f"{domain} 空结果没写未取到"
    # 数字 0 只能出现在「第 0 名」这类结构性表述里，不能是读数
    assert re.search(r"(?<![.\d])0(?![.\d%])", out) is None, out


@pytest.mark.parametrize("domain", P2_TWELVE)
def test_p2_domains_missing_value_is_not_zero(monkeypatch, domain):
    """字段缺值必须写「未披露」，不能拿 0 冒充（backtest 的口径行不算读数）。"""
    _stub_reads(monkeypatch, {})
    _no_delegate(monkeypatch)
    out = _run(tc.query_data_domain(
        None, {"domain": domain, "params_json": '{"symbol": "002361"}'}, _FakeUser()
    ))
    assert "未披露" in out or "未取到" in out, out


# ── 11.4 各域有数据时的渲染 ────────────────────────────

def test_mainline_renders_rank_and_constituents(monkeypatch):
    _stub_reads(monkeypatch, {
        "mainline_rank_date": [{"d": "2026-10-20"}],
        "mainline_rank_rows": [
            {"date": "2026-10-20", "name": "半导体", "rank": 1, "score": 82.5},
            {"date": "2026-10-20", "name": "稀土永磁", "rank": 2, "score": 71.2},
        ],
        "mainline_snapshot": [{
            "date": "2026-10-20", "computed_ts": 1.0,
            "payload": json.dumps({"ranked_groups": [{
                "name": "半导体", "score": 82.5, "limit_up_count": 12, "max_boards": 4,
                "leader": {"code": "600111", "name": "北方稀土", "days": 3},
                "constituents": [{"code": "600111", "name": "北方稀土"}],
            }]}),
        }],
    })
    out = _run(tc.query_data_domain(None, {"domain": "market.mainline"}))
    assert "时点 2026-10-20" in out and "来源" in out
    assert "第 1 名" in out and "半导体" in out and "主线分 82.50" in out
    assert "稀土永磁" in out and "主线分 71.20" in out
    assert "龙头 600111 北方稀土（3板）" in out
    assert "涨停 12 家" in out and "最高 4 板" in out
    assert "未取到" not in out


def test_strategy_signal_renders_factor_and_emit(monkeypatch):
    _stub_reads(monkeypatch, {
        "signal_factor_date": [{"d": "2026-10-20"}],
        "signal_factor_rows": [{
            "snapshot_date": "2026-10-20", "stock_symbol": "002361", "stock_market": "CN",
            "strategy_code": "demon", "alpha_score": 82.0, "catalyst_score": 70.0,
            "quality_score": 60.0, "risk_penalty": 5.0, "crowd_penalty": 3.0,
            "final_score": 71.4,
        }],
        "signal_emit_date": [{"d": "2026-10-20"}],
        "signal_emit_rows": [{
            "emit_ts": "2026-10-20T09:35:00", "emit_date": "2026-10-20", "signal_type": "resonance",
            "symbol": "002361", "market": "CN", "direction": "long", "strength": 0.82,
            "outcome_t1": 3.2, "outcome_t5": -1.1, "checked_t1": 1, "checked_t5": 1,
        }],
    })
    out = _run(tc.query_data_domain(None, {"domain": "strategy.signal"}))
    assert "时点 2026-10-20" in out
    assert "002361" in out and "策略 demon" in out and "总分 71.40" in out
    assert "alpha 82.0" in out and "quality 60.0" in out
    assert "risk +5.0" in out and "crowd +3.0" in out
    assert "resonance" in out and "long" in out and "强度 0.82" in out
    assert "T+1 +3.20%" in out and "T+5 -1.10%" in out
    assert "未取到" not in out


def test_strategy_signal_missing_outcome_not_zero(monkeypatch):
    """T+1/T+5 没回填 → 写「未回填」，不写 0.00%。"""
    _stub_reads(monkeypatch, {
        "signal_factor_date": [{"d": "2026-10-20"}],
        "signal_factor_rows": [],
        "signal_emit_date": [{"d": "2026-10-20"}],
        "signal_emit_rows": [{
            "emit_ts": "2026-10-20T09:35:00", "emit_date": "2026-10-20", "signal_type": "resonance",
            "symbol": "002361", "market": "CN", "direction": "long", "strength": None,
            "outcome_t1": None, "outcome_t5": None, "checked_t1": 0, "checked_t5": 0,
        }],
    })
    out = _run(tc.query_data_domain(None, {"domain": "strategy.signal"}))
    assert "T+1 未回填" in out and "T+5 未回填" in out
    assert "+0.00%" not in out


def test_kline_renders_bars_and_moving_averages(monkeypatch):
    _stub_reads(monkeypatch, {
        "kline_daily_bars": [
            {"ts": "2026-10-19 15:00:00+00:00", "open": 9.8, "high": 10.2, "low": 9.7,
             "close": 10.1, "volume": 1.2e6, "source": "tq"},
            {"ts": "2026-10-20 15:00:00+00:00", "open": 10.1, "high": 10.6, "low": 10.0,
             "close": 10.5, "volume": 1.5e6, "source": "tq"},
        ],
    })
    out = _run(tc.query_data_domain(
        None, {"domain": "stock.kline", "params_json": '{"symbol": "002361"}'}
    ))
    assert "时点 2026-10-20" in out and "来源 klines" in out
    assert "收 10.50" in out and "高 10.60" in out
    assert "区间涨跌" in out
    assert "未取到" not in out


def test_kline_empty_says_missing(monkeypatch):
    _stub_reads(monkeypatch, {"kline_daily_bars": []})
    out = _run(tc.query_data_domain(
        None, {"domain": "stock.kline", "params_json": '{"symbol": "002361"}'}
    ))
    assert "未取到" in out
    assert "0 根" not in out


def test_stock_tech_forwards_three_handlers(monkeypatch):
    calls: list[str] = []

    async def _fake(name, db, args, user):
        calls.append(name)
        return f"[{name}] 占位读数"

    monkeypatch.setattr(tc, "_delegate", _fake)
    out = _run(tc.query_data_domain(
        None, {"domain": "stock.tech", "params_json": '{"symbol": "002361"}'}
    ))
    assert calls == ["get_technical_analysis", "get_wyckoff_structure", "get_kline_patterns"]
    assert "get_technical_analysis" in out and "get_wyckoff_structure" in out
    assert "get_kline_patterns" in out
    assert "未取到" not in out


def test_stock_dossier_forwards_handler(monkeypatch):
    calls: list[tuple[str, dict]] = []

    async def _fake(name, db, args, user):
        calls.append((name, dict(args)))
        return "研报：某某证券 买入｜公告：近15日 2 条"

    monkeypatch.setattr(tc, "_delegate", _fake)
    out = _run(tc.query_data_domain(
        None, {"domain": "stock.dossier", "params_json": '{"symbol": "002361"}'}
    ))
    assert calls == [("get_stock_dossier", {"symbol": "002361", "market": "CN"})]
    assert "研报" in out and "公告" in out
    assert "未取到" not in out


def test_stock_scenario_forwards_handler(monkeypatch):
    calls: list[tuple[str, dict]] = []

    async def _fake(name, db, args, user):
        calls.append((name, dict(args)))
        return "【走强】触发：站上 MA20｜目标：12.00"

    monkeypatch.setattr(tc, "_delegate", _fake)
    out = _run(tc.query_data_domain(
        None, {"domain": "stock.scenario", "params_json": '{"symbol": "002361"}'}
    ))
    assert calls == [("get_scenario_matrix", {"symbol": "002361", "market": "CN"})]
    assert "走强" in out
    assert "未取到" not in out


def test_portfolio_risk_forwards_and_shows_snapshot(monkeypatch):
    calls: list[str] = []

    async def _fake(name, db, args, user):
        calls.append(name)
        return "实盘持仓：002361 闽东电力 100股 成本14.0"

    monkeypatch.setattr(tc, "_delegate", _fake)
    _stub_reads(monkeypatch, {
        "portfolio_positions": [{
            "symbol": "002361", "name": "闽东电力", "market": "CN",
            "quantity": 100, "cost_price": 14.0, "trading_style": "swing",
        }],
        "portfolio_risk_snapshot": [{
            "snapshot_date": "2026-10-20", "market": "CN", "total_signals": 12,
            "active_signals": 8, "held_signals": 3, "high_risk_ratio": 0.25,
            "concentration_top5": 0.62, "avg_rank_score": 71.5, "risk_level": "medium",
        }],
    })
    out = _run(tc.query_data_domain(None, {"domain": "portfolio.risk"}, _FakeUser()))
    assert calls == ["get_portfolio"]
    assert "闽东电力" in out
    assert "组合风险画像" in out and "高风险占比 +25.00%" in out
    assert "前 5 集中度 +62.00%" in out and "风险档 medium" in out
    assert "未取到" not in out


def test_watchlist_forwards_and_lists_rows(monkeypatch):
    calls: list[str] = []

    async def _fake(name, db, args, user):
        calls.append(name)
        return "自选股列表：\n- 闽东电力(CN:002361)"

    monkeypatch.setattr(tc, "_delegate", _fake)
    _stub_reads(monkeypatch, {
        "watchlist_rows": [{"symbol": "002361", "name": "闽东电力", "market": "CN",
                            "sort_order": 0, "updated_at": None}],
    })
    out = _run(tc.query_data_domain(None, {"domain": "watchlist"}, _FakeUser()))
    assert calls == ["get_watchlist"]
    assert "自选明细" in out and "002361" in out
    assert "未取到" not in out


def test_paper_trading_renders_account_positions_trades(monkeypatch):
    _stub_reads(monkeypatch, {
        "paper_account": [{
            "id": 1, "initial_capital": 1000000.0, "current_capital": 1012000.0,
            "total_pnl": 12000.0, "total_trades": 5, "winning_trades": 3,
            "max_drawdown_pct": 4.2, "peak_capital": 1050000.0, "enabled": True,
            "updated_at": "2026-10-20 15:30:00",
        }],
        "paper_positions": [{
            "stock_symbol": "002361", "stock_market": "CN", "stock_name": "闽东电力",
            "quantity": 100, "entry_price": 14.0, "current_price": 15.0,
            "unrealized_pnl": 100.0, "status": "open",
        }],
        "paper_trades": [{
            "stock_symbol": "600111", "stock_market": "CN", "stock_name": "北方稀土",
            "quantity": 200, "entry_price": 20.0, "exit_price": 21.5, "pnl": 300.0,
            "pnl_pct": 7.5, "exit_reason": "target_price", "closed_at": "2026-10-20 14:00:00",
        }],
    })
    out = _run(tc.query_data_domain(None, {"domain": "paper.trading"}, _FakeUser()))
    assert "模拟盘账户" in out and "初始资金 +100.00万" in out
    assert "当前资金 +101.20万" in out
    assert "累计盈亏 +1.20万" in out
    assert "模拟持仓" in out and "002361" in out and "浮盈 +100.00元" in out
    assert "已平仓" in out and "600111" in out and "+7.50%" in out
    assert "未取到" not in out


def test_notify_task_renders_schedules_channels_notifications(monkeypatch):
    calls: list[str] = []

    async def _fake(name, db, args, user):
        calls.append(name)
        return "【系统通知】最近1条: - [2026-10-20 15:30] 策略刷新完成"

    monkeypatch.setattr(tc, "_delegate", _fake)
    _stub_reads(monkeypatch, {
        "copilot_schedules": [{
            "id": 1, "kind": "daily_brief", "at_hhmm": "15:30", "weekdays": "mon-fri",
            "enabled": True, "last_run_date": "20261021", "created_at": None,
        }],
        "notify_channel_rows": [{"name": "企业微信", "type": "wecom", "enabled": True, "is_default": True}],
    })
    out = _run(tc.query_data_domain(None, {"domain": "notify.task"}, _FakeUser()))
    assert calls == ["get_notifications"]
    assert "收盘小结" in out and "每天 15:30" in out and "已启用" in out
    assert "推送渠道" in out and "企业微信（wecom）" in out
    assert "站内通知" in out and "策略刷新完成" in out
    assert "未取到" not in out


def test_notify_task_is_read_only(monkeypatch):
    """notify.task 只读：绝不能调用 create_daily_task 这类写动作。"""
    calls: list[str] = []

    async def _fake(name, db, args, user):
        calls.append(name)
        return "通知占位"

    monkeypatch.setattr(tc, "_delegate", _fake)
    _stub_reads(monkeypatch, {})
    _run(tc.query_data_domain(None, {"domain": "notify.task"}, _FakeUser()))
    assert "create_daily_task" not in calls
    assert calls == ["get_notifications"]


def test_backtest_without_results_says_no_result(monkeypatch):
    """回测引擎结果不落库 → 无样本时如实说「无结果」，不编收益数字。"""
    _stub_reads(monkeypatch, {
        "backtest_outcome_stats": [],
        "backtest_outcome_rows": [],
        "backtest_factor_days": [],
    })
    out = _run(tc.query_data_domain(None, {"domain": "backtest"}, _FakeUser()))
    assert "未取到" in out
    assert "不落库" in out
    assert "无已结算样本" in out
    assert re.search(r"(?<![.\d])0(?![.\d%])", out) is None, out


def test_backtest_renders_outcome_stats(monkeypatch):
    _stub_reads(monkeypatch, {
        "backtest_outcome_stats": [{"horizon_days": 5, "n": 12, "avg_ret": 2.35,
                                    "hit_t": 4, "hit_s": 2}],
        "backtest_outcome_rows": [{
            "snapshot_date": "2026-10-20", "strategy_code": "demon", "stock_symbol": "002361",
            "horizon_days": 5, "base_price": 14.0, "outcome_price": 14.33,
            "outcome_return_pct": 2.35, "hit_target": False, "hit_stop": False,
            "outcome_status": "evaluated",
        }],
        "backtest_factor_days": [{"snapshot_date": "2026-10-20", "n": 8, "avg_score": 71.2}],
    })
    out = _run(tc.query_data_domain(None, {"domain": "backtest"}, _FakeUser()))
    assert "事后收益汇总" in out and "持有 5 日" in out and "样本 12" in out
    assert "平均 +2.35%" in out
    assert "最近样本" in out and "002361" in out
    assert "未取到" not in out


def test_ops_config_renders_datasources_and_trust(monkeypatch):
    _stub_reads(monkeypatch, {
        "ops_datasources": [{
            "id": 1, "name": "雪球资讯", "type": "news", "provider": "xueqiu",
            "enabled": True, "priority": 1, "supports_batch": True, "last_status": "ok",
            "success_count": 100, "error_count": 2, "last_used_at": None, "last_error_at": None,
        }],
        "ops_settings": [{"key": "llm_price_per_mtoken", "description": "价格"}],
        "ops_ds_failures": [{"provider": "eastmoney", "kind": "timeout", "symbol": "002361",
                             "detail": "读超时", "latency_ms": 5000, "created_at": "2026-10-20 10:00:00"}],
    })
    # 信任分来自 marketdata 健康快照（非库表），这里替身喂一份
    import src.core.marketdata_client as mdc

    class _MD:
        def health(self):
            return {"eastmoney": {"success_rate": 0.95, "p50_latency_ms": 500, "count": 200}}

    orig = mdc.get_market_data
    mdc.get_market_data = lambda: _MD()
    try:
        out = _run(tc.query_data_domain(None, {"domain": "ops.config"}, _FakeUser()))
    finally:
        mdc.get_market_data = orig
    assert "数据源配置" in out and "雪球资讯（news/xueqiu）" in out
    assert "优先级 1" in out and "已启用" in out
    assert "数据源信任分" in out and "信任分 95" in out
    assert "平台设置项" in out and "llm_price_per_mtoken" in out
    assert "数据源失败记录" in out
    assert "未取到" not in out


def test_ops_config_is_read_only(monkeypatch):
    """ops.config L2 只读：不写入任何表（没有 INSERT/UPDATE/DELETE 语句）。"""
    for key, sql in tc._SQL.items():
        up = sql.upper()
        assert not any(w in up for w in ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "ALTER ")), key


# ── 11.5 目录/工具契约 ────────────────────────────

def test_catalog_source_tables_are_real_tables():
    """P2 十二域 source 里写的表必须都真实存在（models ∪ migrations）。"""
    import importlib.util
    import sys as _sys

    spec = importlib.util.spec_from_file_location(
        "gen_data_catalog", ROOT / "scripts" / "gen_data_catalog.py"
    )
    assert spec is not None and spec.loader is not None
    gen = importlib.util.module_from_spec(spec)
    # 必须先注册到 sys.modules：生成器里的 dataclass 装饰器要拿模块 __dict__
    _sys.modules["gen_data_catalog"] = gen
    try:
        spec.loader.exec_module(gen)
        tables = set(
            gen.parse_tables(ROOT / "src" / "db" / "models.py", ROOT / "src" / "web" / "migrations.py")
        )

        import yaml

        raw = yaml.safe_load(_CATALOG_YAML.read_text(encoding="utf-8"))
        by_id = {str(d["id"]): d for d in raw["domains"]}
        for domain in P2_TWELVE:
            src = str(by_id[domain].get("source") or "")
            assert src.strip(), f"{domain} source 为空"
            tokens = gen.parse_source_tokens(src)
            assert tokens, f"{domain} source 解析不出 token"
            for tok in tokens:
                if "/" in tok:
                    continue  # 路由提示
                assert tok.lower() in {t.lower() for t in tables}, (
                    f"{domain} 引用了不存在的表 {tok}"
                )
    finally:
        _sys.modules.pop("gen_data_catalog", None)


def test_new_sql_statements_have_limit_and_bindings():
    """新增只读语句：必须是 SELECT + LIMIT + 绑定参数（禁 f-string 拼 SQL）。"""
    for key in ("mainline_rank_rows", "signal_factor_rows", "signal_emit_rows",
                "kline_daily_bars", "portfolio_positions", "watchlist_rows",
                "paper_account", "paper_positions", "paper_trades",
                "copilot_schedules", "notify_rows", "backtest_outcome_rows",
                "ops_datasources", "ops_settings", "ops_ds_failures"):
        sql = tc._SQL[key]
        up = sql.upper()
        assert up.lstrip().startswith("SELECT"), key
        assert "LIMIT" in up, f"{key} 缺 LIMIT"
        assert ";" not in sql, key
        if key in ("portfolio_positions", "watchlist_rows", "paper_account", "paper_positions",
                   "paper_trades", "copilot_schedules", "notify_rows"):
            assert ":UID" in sql.upper(), f"{key} 缺 user_id 绑定参数"


def test_tool_count_still_73():
    """工具总数 = 73（2026-10-24 新增 search_news / web_search_fallback）。"""
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY

    assert len(CHAT_TOOL_REGISTRY) == 76
