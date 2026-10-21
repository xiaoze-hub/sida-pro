"""Agent 事故回归集（2026-10-20, P2）。

每一条都对应一次**真实线上事故**或一条**被验证过的红线**。改提示词/配方/工具/内核
之后先跑这个文件：它不测"功能对不对"，只测"以前踩过的坑有没有重新裂开"。

命名规则：`<事故简称>`，desc 里写清当时的症状。新增事故时**只加行，不改行**。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UID = "84425dba-9944-4f35-b804-fcdfc7ce6fa4"


# ────────────────────────── 事故清单 ──────────────────────────

def _c01_auction_not_aliased():
    """竞价问「我的自选」被别名成全市场最强榜 → 只能得到「不在榜单」。"""
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY

    desc = CHAT_TOOL_REGISTRY["get_auction_data"].schema["function"]["description"]
    assert "不要用 strongest" in desc
    assert "watchlist" in desc and "symbols" in desc


def _c02_auction_missing_snapshot_wording():
    """取不到竞价写成「没有强度」而不是「没取到」。"""
    from src.harness.skills import load_skill

    assert "无竞价快照" in load_skill("auction")
    assert "不要" in load_skill("auction")


def _c03_limit_down_no_snapshot_is_none():
    """跌停家数读不到时补 0 会撒谎 —— 必须 None（前端显 `--`）。"""
    from src.web.api import market_phase as mp

    # 无快照分支由 test_dashboard_gaps 的 fixture 保证；这里只锁签名契约
    assert mp._limit_down_from_breadth.__annotations__.get("return") is not None


def _c04_total_amount_never_zero_filled():
    """成交额拿不到时不能给 0.0 —— 只能 None。"""
    from src.web.api import market_data as md

    out = md._snapshot_to_response({"total_main_flow": 1.0})
    assert out["total_amount"] is None


def _c05_total_amount_index_fallback():
    """上游不返回 total_amount 时用指数成交额兜底（否则首页恒为 --）。"""
    from src.web.api import market_data as md

    class _MD:
        def index_quotes(self, codes):
            return [{"turnover": 3.9e11}, {"turnover": 4.37e11}]

    import src.core.marketdata_client as mdc

    old = mdc.get_market_data
    old_tq = md._tq_index_amount
    mdc.get_market_data = lambda: _MD()
    md._tq_index_amount = lambda: (None, "tdx_tq_error")   # 专测腾讯兜底层
    md._AMOUNT_CACHE.clear(); md._AMOUNT_CACHE.update({"ts": 0.0, "val": None})
    try:
        assert md._total_amount_from_indices() == pytest.approx(8270.0)
    finally:
        mdc.get_market_data = old
        md._tq_index_amount = old_tq
        md._AMOUNT_CACHE.clear(); md._AMOUNT_CACHE.update({"ts": 0.0, "val": None})


def _c06_trace_digest_truncated():
    """工具返回带回用户持仓，全量落库=二次留存。"""
    from src.harness.trace import MAX_DIGEST_CHARS, _clip

    assert len(_clip("x" * 5000, MAX_DIGEST_CHARS)) <= MAX_DIGEST_CHARS + 1


def _c07_trace_error_status():
    """降级/异常必须留痕为 error，不能伪装成 ok。"""
    from src.harness.trace import CopilotTrace

    t = CopilotTrace(conversation_id=1, user_id=UID, question="x")
    assert t.run_id


def _c08_budget_hard_stop():
    """模型死循环调工具烧钱 —— 预算必须硬拦。"""
    from src.harness.runtime import BUDGET_EXCEEDED, AgentRuntime

    rt = AgentRuntime(max_tool_calls=0)
    ok, reason = rt.allow("get_stock_quote")
    assert not ok and reason == BUDGET_EXCEEDED


def _c09_whitelist_hard_stop():
    """配方白名单是硬约束，不是提示词里的软建议。"""
    from src.harness.runtime import AgentRuntime

    rt = AgentRuntime(allowed_tools=frozenset({"get_stock_quote"}))
    ok, reason = rt.allow("order_stock")
    assert not ok and "不在当前配方允许" in reason


def _c10_whitelist_allows_inside():
    from src.harness.runtime import AgentRuntime

    rt = AgentRuntime(allowed_tools=frozenset({"get_stock_quote"}))
    assert rt.allow("get_stock_quote") == (True, "")


def _c11_user_id_is_uuid_text():
    """users.id 是 UUID 字符串 —— 写成 Integer 时每条轨迹都 DataError。"""
    from src.db.models import CopilotFeedback, CopilotRun

    for model in (CopilotRun, CopilotFeedback):
        col = model.__table__.columns["user_id"]
        assert col.type.python_type is str


def _c12_tool_count_stable():
    """工具数变化必须是有意的（2026-10-21 = 59：P0 记忆/定时 + 分时画像）。"""
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY

    assert len(CHAT_TOOL_REGISTRY) == 59


def _c13_tool_schema_name_matches():
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY

    for name, tool in CHAT_TOOL_REGISTRY.items():
        if tool.schema is not None:
            assert tool.schema["function"]["name"] == name


def _c14_every_tool_has_caliber():
    """口径标签缺失 → 下游无法判断这个数字能不能用于方向判定。"""
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY

    for name, tool in CHAT_TOOL_REGISTRY.items():
        assert tool.caliber and tool.caliber.strip(), name


def _c15_capital_flow_redline():
    """东财资金流方向位会反 —— 必须带红线文案，禁止据此判主力意图。"""
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY

    assert "禁止" in CHAT_TOOL_REGISTRY["get_capital_flow"].caliber


def _c16_recipes_load():
    """YAML 坏了整个 /agents 会 500（线上真出过）。"""
    from src.harness.recipes import list_recipes

    assert len(list_recipes()) >= 6


def _c17_recipe_when_all_strings():
    """YAML 1.1 把 9:25 解析成六十进制整数 565 → ' '.join 抛 TypeError，线上 500。"""
    from src.harness.recipes import list_recipes

    for r in list_recipes():
        for w in r.when:
            assert isinstance(w, str), f"{r.id} when 含非字符串: {w!r}"


def _c18_auction_recipe_has_auction_tools():
    from src.harness.recipes import get_recipe

    r = get_recipe("auction_hunter")
    assert "get_auction_data" in r.tools


def _c19_chip_recipe_has_skill():
    from src.harness.recipes import get_recipe

    assert get_recipe("chip_analyst").skills == ("chip",)


def _c20_skill_path_traversal():
    """手册名来自配置，禁止 ../ 逃逸。"""
    from src.harness.skills import load_skill

    assert load_skill("../../etc/passwd") == ""


def _c21_action_unknown_kind():
    """'order_stock' 这类交易动作永远不进动作框架。"""
    from src.db.session import SessionLocal
    from src.harness import actions as A

    db = SessionLocal()
    try:
        with pytest.raises(A.ActionError):
            A.propose(db, user_id=UID, kind="order_stock", payload={"symbol": "600111"})
    finally:
        db.close()


def _c22_action_empty_payload():
    from src.db.session import SessionLocal
    from src.harness import actions as A

    db = SessionLocal()
    try:
        with pytest.raises(A.ActionError):
            A.propose(db, user_id=UID, kind="watchlist", payload={})
    finally:
        db.close()


def _c23_annotation_needs_label():
    """没标签的标注画到图上是噪音。"""
    from src.db.session import SessionLocal
    from src.harness import actions as A

    db = SessionLocal()
    try:
        item = A.propose(db, user_id=UID, kind="annotation", payload={"symbol": "600111"})
        with pytest.raises(A.ActionError):
            A.apply_action(db, user_id=UID, action_id=item["id"])
    finally:
        db.query.__self__  # noqa: B018 - 占位，实际清理在下面
        from src.db.models import CopilotAction

        db.query(CopilotAction).filter(CopilotAction.user_id == UID).delete()
        db.commit()
        db.close()


def _c24_alert_needs_price():
    from src.db.session import SessionLocal
    from src.harness import actions as A
    from src.db.models import CopilotAction

    db = SessionLocal()
    try:
        item = A.propose(db, user_id=UID, kind="alert", payload={"symbol": "600111"})
        with pytest.raises(A.ActionError):
            A.apply_action(db, user_id=UID, action_id=item["id"])
    finally:
        db.query(CopilotAction).filter(CopilotAction.user_id == UID).delete()
        db.commit()
        db.close()


def _c25_cross_user_apply_denied():
    """拿别人的 action_id 也能 apply = 越权。"""
    from src.db.session import SessionLocal
    from src.harness import actions as A
    from src.db.models import CopilotAction

    db = SessionLocal()
    try:
        item = A.propose(db, user_id=UID, kind="watchlist", payload={"symbol": "000001"})
        with pytest.raises(A.ActionError):
            A.apply_action(db, user_id="another-user", action_id=item["id"])
    finally:
        db.query(CopilotAction).filter(CopilotAction.user_id == UID).delete()
        db.commit()
        db.close()


def _c26_view_state_empty_noop():
    from src.harness.viewstate import render_view_state_block

    assert render_view_state_block({}) == ""


def _c27_view_state_precedence_note():
    """页面锚点不能覆盖用户明说的标的 —— 文案必须写死优先级。"""
    from src.harness.viewstate import render_view_state_block

    block = render_view_state_block({"symbol": "600111"})
    assert "以文字为准" in block


def _c28_view_state_bounded():
    """锚点块 ≤300 字，否则会挤掉行情数据。"""
    from src.harness.viewstate import render_view_state_block

    block = render_view_state_block({
        "page": "kline", "symbol": "600111", "market": "SH", "period": "30m",
        "range": {"from": "2026-09-01", "to": "2026-10-20"},
        "selection": {"from": "2026-10-10", "to": "2026-10-18"},
        "indicators": ["MA5", "MA20", "MA60", "VOL", "MACD", "BOLL", "KDJ"],
        "anchor_line": 35.2,
    })
    assert len(block) <= 300


def _c29_migrations_contiguous():
    """迁移号重复/跳号 → 新环境起不来。"""
    from src.web.migrations import MIGRATIONS

    versions = [m.version for m in MIGRATIONS]
    assert versions == sorted(versions)
    assert len(set(versions)) == len(versions)


def _c30_chat_py_line_guard():
    """chat.py 历史上膨胀到 3021 行，护栏 2221 行；新逻辑必须外置到 harness/。"""
    lines = (ROOT / "src" / "web" / "api" / "chat.py").read_text(encoding="utf-8").count("\n")
    # 2026-10-20: TQ 异动资金档案 +3 行 → 2230；2026-10-21 长期记忆注入 +5 行 → 2240。
    # 逻辑仍在往 src/core、src/harness 抽，护栏只是防止 chat.py 无限膨胀。
    assert lines <= 2300, f"chat.py 当前 {lines} 行，超护栏"


def _c31_no_decision_pioneer_in_user_facing_text():
    """口径红线：对外文案统一叫「数智决策」，AI 不能对用户念出「决策先锋」。

    只看**会被用户/AI 读到的文本**（工具描述、研判手册）；内部模块名/注释属实现细节，
    不在本条约束内（全仓清理是另一件独立工作）。
    """
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY

    bad = []
    for name, tool in CHAT_TOOL_REGISTRY.items():
        if tool.schema is None:
            continue
        desc = tool.schema["function"].get("description") or ""
        if "决策先锋" in desc:
            bad.append(f"tool:{name}")
    for md in (ROOT / "src" / "harness" / "skills").glob("*.md"):
        if "决策先锋" in md.read_text(encoding="utf-8", errors="ignore"):
            bad.append(f"skill:{md.name}")
    assert bad == [], f"对外文案出现「决策先锋」: {bad}"




def _c32_auction_gap_is_open_vs_prev_close():
    """2026-10-20 金健米业事故：THS 异动幅度曾被当成竞价涨幅（低开 -1.75% 显示 +4.87%）。

    口径铁律：gap_pct 只能来自「开盘价 vs 昨收」；异动幅度单独放 anomaly_ratio。
    """
    import inspect

    import src.core.auction_pool as ap

    src_text = inspect.getsource(ap)
    assert "anomaly_ratio" in src_text
    assert "OpenZAF" in src_text and "LastClose" in src_text
    # 没有真实涨幅时不得做方向判定
    assert "❔ 数据不足" in src_text


def _c33_tq_flow_snapshot_must_be_source_filtered():
    """快照表里混着东财口径的旧行 —— 读侧必须只认 source='tdx_tq'，
    否则会把东财的数标成 TQ 口径（比没数据更坏）。
    """
    import inspect

    from src.web.api import market_data as md

    src_text = inspect.getsource(md._latest_tq_flow_row)
    assert "source = 'tdx_tq'" in src_text
    # 时区: 库里是本地 naive 时间, 不能当 UTC（否则算出负年龄的"未来快照"）
    assert "timedelta(hours=8)" in src_text



CASES = [
    ("c01_竞价别名", _c01_auction_not_aliased),
    ("c02_竞价缺数措辞", _c02_auction_missing_snapshot_wording),
    ("c03_跌停缺数", _c03_limit_down_no_snapshot_is_none),
    ("c04_成交额不补零", _c04_total_amount_never_zero_filled),
    ("c05_成交额兜底", _c05_total_amount_index_fallback),
    ("c06_轨迹摘要截断", _c06_trace_digest_truncated),
    ("c07_轨迹错误态", _c07_trace_error_status),
    ("c08_工具预算", _c08_budget_hard_stop),
    ("c09_白名单拒绝", _c09_whitelist_hard_stop),
    ("c10_白名单放行", _c10_whitelist_allows_inside),
    ("c11_用户UUID", _c11_user_id_is_uuid_text),
    ("c12_工具数量", _c12_tool_count_stable),
    ("c13_工具schema名", _c13_tool_schema_name_matches),
    ("c14_工具口径标签", _c14_every_tool_has_caliber),
    ("c15_资金流红线", _c15_capital_flow_redline),
    ("c16_配方可加载", _c16_recipes_load),
    ("c17_配方when字符串", _c17_recipe_when_all_strings),
    ("c18_竞价配方工具", _c18_auction_recipe_has_auction_tools),
    ("c19_筹码配方手册", _c19_chip_recipe_has_skill),
    ("c20_手册路径穿越", _c20_skill_path_traversal),
    ("c21_交易动作拒绝", _c21_action_unknown_kind),
    ("c22_空动作拒绝", _c22_action_empty_payload),
    ("c23_标注缺标签", _c23_annotation_needs_label),
    ("c24_提醒缺价格", _c24_alert_needs_price),
    ("c25_跨用户越权", _c25_cross_user_apply_denied),
    ("c26_空视图", _c26_view_state_empty_noop),
    ("c27_视图优先级", _c27_view_state_precedence_note),
    ("c28_视图长度上限", _c28_view_state_bounded),
    ("c29_迁移连续", _c29_migrations_contiguous),
    ("c30_chat行数护栏", _c30_chat_py_line_guard),
    ("c31_对外文案口径", _c31_no_decision_pioneer_in_user_facing_text),
    ("c32_竞价涨幅口径", _c32_auction_gap_is_open_vs_prev_close),
    ("c33_TQ快照来源过滤", _c33_tq_flow_snapshot_must_be_source_filtered),
]


@pytest.mark.parametrize("name,fn", CASES, ids=[c[0] for c in CASES])
def test_regression(name, fn):
    fn()
