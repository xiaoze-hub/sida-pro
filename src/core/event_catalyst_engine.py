"""事件驱动预期差引擎(2026-08-22)。

把个股当日公告/事件原文交给 LLM(DeepSeek/场景绑定), 推理成结构化催化信号:
催化题材名 + 方向(利好/利空/中性) + 置信度 + 受益链(beneficiary_pool) +
预期差(expectation_gap, 判断"股价是否已反应事件"), 供题材潜伏策略使用。

设计原则:
- 纯函数(parse_catalyst_reply / build_catalyst_prompt)与 LLM 层
  (analyze_event_catalyst)分离, 纯函数可独立单测;
- 所有 LLM / 数据源失败(异常/超时/非法 JSON/非法字段)一律静默降级返回 None,
  绝不影响主流程;
- LLM 调用模式复用 src.agents.intraday_monitor 的反证层: 场景绑定回落
  Settings/env、asyncio.wait_for 8s 超时、协程同步化 _run_coro。
"""

import json
import logging
import re

from datetime import datetime

logger = logging.getLogger(__name__)

_LLM_TIMEOUT = 8  # 催化推理 LLM 超时秒数(超时 → 静默降级 None)

_VALID_DIRECTIONS = {"利好", "利空", "中性"}
_VALID_CONFIDENCE = {"高", "中", "低"}
_VALID_GAP_LEVELS = {"高", "中", "低"}
_MAX_BENEFICIARIES = 5
_MAX_REASON_LEN = 80

_CATALYST_SYSTEM_PROMPT = (
    "你是一名A股事件驱动预期差分析师。给定某只股票当日公告/事件(标题列表), "
    "做因果链推理, 输出该事件的催化信号与预期差。\n"
    "推理要求:\n"
    "1. 因果链推理: 从事件本身推导\"事件 → 中间传导环节 → 受益环节/受益标的\"。"
    "例如\"某上游厂商停产\" → \"供给收缩\" → \"产品涨价\" → \"同类上游/替代材料厂商受益\"。\n"
    "2. 严禁编造事件: 只基于给定的事件标题推理, 不得补充不存在的事件、数据或公告。\n"
    "3. 严禁从股票代码反查公司: 事件标题里已包含公司与业务信息, 不得用代码去猜"
    "\"这是哪家公司、主营什么\"。受益标的也只能由事件标题中的产业链信息推导, "
    "不能凭空列举。\n"
    "4. 预期差判断(写入 expectation_gap):\n"
    "   - 利好但股价尚未反应(事件刚出/尚未发酵) → 预期差\"高\", 潜伏价值大;\n"
    "   - 利好但股价已大涨/已充分反应 → 利好兑现, 预期差\"低\", 追高风险;\n"
    "   - 利空但主力逆势承接(恐慌中有人接盘) → 预期差\"高\", 借恐慌低吸机会;\n"
    "   - 中性或影响微小 → 预期差\"低\"。\n"
    "5. 只输出 JSON, 不要任何其他文字。\n"
    "6. 有行情快照时, expectation_gap.note 必须引用快照数字(如涨跌幅/量比); "
    "无快照时 level 只许给中或低, 不许断言尚未反应。\n"
    "输出格式(严格 JSON):\n"
    '{"catalyst": "催化题材名", "direction": "利好"|"利空"|"中性", '
    '"confidence": "高"|"中"|"低", '
    '"beneficiary_pool": ["受益股/受益产业链名称", ...](最多5个, 不含标的自身), '
    '"expectation_gap": {"level": "高"|"中"|"低", "note": "一句话说明"}, '
    '"reason": "因果链一句话理由(≤80字)"}'
)


def build_catalyst_prompt(
    symbol: str, events: list[str], market: dict | None = None
) -> tuple[str, str]:
    """构造催化推理提示词, 返回 (system_prompt, user_content)。

    system 要求 LLM 做因果链推理(停产→涨价→受益链), 输出严格 JSON, 不得编造
    事件、不得从 ticker 反查公司(事件列表里已有标题), 并按预期差逻辑给 level。
    market 为行情快照(现价/涨跌幅/量比/换手, 可 None)→ 拼进 user_content,
    预期差必须引用快照数据, 无快照不得断言"尚未反应"。
    """
    lines = [f"股票代码: {symbol}", "当日事件(标题列表, 最多3条):"]
    for i, ev in enumerate(events, 1):
        lines.append(f"{i}. {ev}")
    if market:
        lines.append("")
        lines.append("最新行情快照(事实, 预期差判断必须引用):")
        for k in ("current_price", "change_pct", "volume_ratio", "turnover_rate", "quote_date"):
            if market.get(k) is not None:
                lines.append(f"- {k}: {market[k]}")
    lines.append("")
    lines.append("请基于以上事件做因果链推理, 输出催化信号 JSON。")
    user_content = "\n".join(lines)
    return _CATALYST_SYSTEM_PROMPT, user_content


def parse_catalyst_reply(
    symbol: str, events: list[str], llm_reply: str | None
) -> dict | None:
    """解析 LLM JSON 输出为结构化催化信号, 字段非法/空返回 None。

    Args:
        symbol: 6位A股代码(用于把标的自身从受益池剔除)。
        events: 当日事件标题列表(签名占位, 便于与上游对齐; 校验不依赖其内容)。
        llm_reply: LLM 原始回复。

    Returns:
        {
            "catalyst": str,                 # 催化题材名
            "direction": "利好"|"利空"|"中性",
            "confidence": "高"|"中"|"低",
            "beneficiary_pool": list[str],   # 受益股/受益产业链, ≤5, 不含标的自身
            "expectation_gap": {"level": "高"|"中"|"低", "note": str},
            "reason": str,                   # ≤80字
        }
    """
    if not llm_reply or not llm_reply.strip():
        return None
    text = llm_reply.strip()
    # 容忍 ```json 围栏
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    catalyst = str(data.get("catalyst") or "").strip()
    direction = str(data.get("direction") or "").strip()
    confidence = str(data.get("confidence") or "").strip()
    reason = str(data.get("reason") or "").strip()

    if not catalyst:
        return None
    if direction not in _VALID_DIRECTIONS:
        return None
    if confidence not in _VALID_CONFIDENCE:
        return None
    if not reason:
        return None
    if len(reason) > _MAX_REASON_LEN:
        reason = reason[:_MAX_REASON_LEN] + "…"

    raw_pool = data.get("beneficiary_pool")
    if not isinstance(raw_pool, list):
        return None
    pool: list[str] = []
    self_aliases = {symbol, f"sh{symbol}", f"sz{symbol}"}
    for name in raw_pool:
        if not isinstance(name, str):
            continue
        n = name.strip()
        if not n or n in self_aliases:
            continue
        pool.append(n)
    if not pool:
        return None
    pool = pool[:_MAX_BENEFICIARIES]

    gap = data.get("expectation_gap")
    if not isinstance(gap, dict):
        return None
    level = str(gap.get("level") or "").strip()
    note = str(gap.get("note") or "").strip()
    if level not in _VALID_GAP_LEVELS:
        return None

    return {
        "catalyst": catalyst,
        "direction": direction,
        "confidence": confidence,
        "beneficiary_pool": pool,
        "expectation_gap": {"level": level, "note": note},
        "reason": reason,
    }


def _run_coro(coro):
    """同步上下文执行异步协程: 无运行中事件循环 → asyncio.run; 有(如 FastAPI
    请求上下文)→ 新建独立事件循环执行, 避免 asyncio.run 抛
    "cannot be called from a running event loop"。"""
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    new_loop = asyncio.new_event_loop()
    try:
        return new_loop.run_until_complete(coro)
    finally:
        new_loop.close()


def _build_catalyst_client(db=None):
    """构造催化引擎 LLM 客户端: 优先 db 场景绑定(intraday_monitor/chat), 回落
    Settings/env(AI_BASE_URL/AI_API_KEY/AI_MODEL)。与 intraday_monitor 反证层
    同构的最简可靠路径。"""
    from src.core.ai_client import AIClient

    if db is not None:
        try:
            from src.core.ai_client import get_model_for_scene
            from src.web.models import AIService

            m = get_model_for_scene(db, "intraday_monitor") or get_model_for_scene(db, "chat")
            if m is not None:
                s = db.query(AIService).filter(AIService.id == m.service_id).first()
                if s is not None and s.base_url and s.api_key:
                    return AIClient(base_url=s.base_url, api_key=s.api_key, model=m.model)
        except Exception as e:
            logger.debug(f"催化引擎场景绑定不可用(回落 Settings/env): {e}")
    from src.config import Settings

    settings = Settings()
    return AIClient(
        base_url=settings.ai_base_url,
        api_key=settings.ai_api_key,
        model=settings.ai_model,
    )


async def _catalyst_llm_chat(system_prompt: str, user_content: str, db=None) -> str:
    """单次 LLM 调用(8s 超时, asyncio.wait_for 保证)。独立函数便于测试
    monkeypatch AIClient.chat。客户端在协程内构造, 避免跨事件循环复用 httpx
    client。"""
    import asyncio

    client = _build_catalyst_client(db)
    return await asyncio.wait_for(
        client.chat(system_prompt, user_content, temperature=0.2),
        timeout=_LLM_TIMEOUT,
    )


def _fetch_today_events(symbol: str) -> list[str]:
    """当日个股公告/新闻标题(东财公告单源, 最多3条, 每条≤80字)。

    失败(网络/解析/无事件)一律返回空列表 → 不调 LLM, 严禁让 LLM 编造事件。
    """
    try:
        from marketdata import Symbol as MDSymbol
        from marketdata.vendors.events import EventsVendor

        mdsym = MDSymbol.parse(symbol, "CN")
        items = EventsVendor().fetch([mdsym], {"since_days": 1})
        today = datetime.now().date()
        items = [
            ev for ev in items
            if getattr(ev, "publish_time", None) and ev.publish_time.date() == today
        ]
        items.sort(key=lambda ev: getattr(ev, "importance", 0) or 0, reverse=True)
        out: list[str] = []
        for ev in items[:3]:
            title = (getattr(ev, "title", "") or "").strip()
            if not title:
                continue
            if len(title) > 80:
                title = title[:80] + "…"
            out.append(title)
        return out
    except Exception as e:
        logger.debug(f"当日事件获取失败(降级为空): {symbol}: {e}")
        return []


def _fetch_market_snapshot(symbol: str) -> dict | None:
    """单股行情快照(现价/涨跌幅/量比/换手/日期)。失败 → None(老 prompt 照跑)。

    数据源: md_quote_rows(多源聚合, 与 /quotes 同口径)。
    """
    try:
        from src.core.marketdata_client import md_quote_rows

        rows = md_quote_rows([symbol], "CN") or []
        q = next((r for r in rows if r.get("symbol") == symbol), None)
        if not q:
            return None
        return {
            "current_price": q.get("current_price"),
            "change_pct": q.get("change_pct"),
            "volume_ratio": q.get("volume_ratio"),
            "turnover_rate": q.get("turnover_rate"),
            "quote_date": q.get("quote_date"),
        }
    except Exception as e:
        logger.debug(f"行情快照获取失败(降级无快照): {symbol}: {e}")
        return None


def analyze_event_catalyst(
    symbol: str, db=None, market: dict | None = None, extra_events: list[str] | None = None
) -> dict | None:
    """事件驱动预期差主入口: 拉当日事件(+真实日历条目) → 拉行情快照 → 推理 → 解析。

    market 为调用方自带快照(可 None, None 则内部拉, 拉不到就无快照跑)。
    extra_events 为已验证的真实未来催化文本(如解禁/会议日历条目), 与当日
    公告合并输入; 严禁传入推测性文本。
    空事件直接返回 None(不调 LLM); 任何异常/超时/非法 JSON/非法字段一律
    静默降级返回 None, 不影响主流程。
    """
    events = _fetch_today_events(symbol)
    for e in extra_events or []:
        e = (e or "").strip()
        if e and e not in events:
            events.append(e[:80])
    events = events[:5]
    if not events:
        return None
    if market is None:
        market = _fetch_market_snapshot(symbol)
    try:
        system_prompt, user_content = build_catalyst_prompt(symbol, events, market)
        raw = _run_coro(_catalyst_llm_chat(system_prompt, user_content, db))
        if not raw or not raw.strip():
            return None
        return parse_catalyst_reply(symbol, events, raw)
    except Exception as e:
        logger.debug(f"事件催化推理失败(静默降级): {symbol}: {e}")
        return None
