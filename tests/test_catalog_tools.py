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


@pytest.mark.parametrize("domain", ["stock.chip", "stock.flow", "stock.intraday", "stock.factor"])
def test_missing_symbol_rejected(domain):
    out = _run(tc.query_data_domain(None, {"domain": domain, "params_json": "{}"}))
    assert "缺少必填参数" in out and "symbol" in out
    # 拒绝时不能返回任何数字读数
    assert "现价" not in out and "净额" not in out


def test_bad_params_json_rejected():
    out = _run(tc.query_data_domain(None, {"domain": "stock.chip", "params_json": "{oops"}))
    assert "params_json" in out and "不是合法 JSON" in out


def test_p1_domain_is_honest():
    out = _run(tc.query_data_domain(None, {"domain": "market.index"}))
    assert "尚未接入实现" in out or "P1 待接" in out


def test_all_eight_p0_domains_are_wired(monkeypatch):
    """P0 八个域（yaml 里 source 非空）必须都有实现，不能漏一个去报「未接入」。"""
    import yaml

    _canned_reads(monkeypatch, {})   # 不碰真库：全部当空表处理

    async def _fake(name, db, args, user):   # 不碰网络：转发层直接给占位
        return f"[{name}] 占位"

    monkeypatch.setattr(tc, "_delegate", _fake)
    raw = yaml.safe_load(_CATALOG_YAML.read_text(encoding="utf-8"))
    p0 = {str(d["id"]) for d in raw["domains"] if str(d.get("source") or "").strip()}
    assert len(p0) == 8
    for domain in sorted(p0):
        out = _run(tc.query_data_domain(None, {"domain": domain, "params_json": '{"symbol": "002361"}'}))
        assert "尚未接入实现" not in out, f"{domain} 未接线"


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
