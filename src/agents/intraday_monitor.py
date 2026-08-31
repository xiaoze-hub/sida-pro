"""盘中监测 Agent - 实时监控持仓，AI 判断是否需要提醒"""

import json
import logging
import re
import asyncio
from datetime import datetime, timedelta, date, timezone
from pathlib import Path

from src.agents.base import BaseAgent, AgentContext, AnalysisResult, apply_scene_binding
from src.core.analysis_history import get_latest_analysis, get_analysis
from src.core.context_builder import ContextBuilder
from src.core.context_store import (
    save_agent_context_run,
    save_agent_prediction_outcome,
)
from src.core.suggestion_pool import save_suggestion
from src.core.signals import SignalPackBuilder
from src.core.signals.structured_output import try_parse_action_json
from src.models.market import MarketCode, StockData, MARKETS

logger = logging.getLogger(__name__)

# 腾讯实时量比缓存: {symbol -> (ts, volume_ratio)}。盘中 TTL 30s, 避免每轮重复请求。
_realtime_volume_ratio_cache: dict[str, tuple[float, float | None]] = {}


def _main_intent_both(symbol: str) -> tuple[str, dict | None]:
    """主力意图字符串+结构化一次计算(2026-08-12 性能优化)。

    summary 接口原本 `_main_intent_summary` + `_main_intent_structured` 各调一次
    compute_dark_flow(逐笔翻页/分价表/5日资金流各跑一遍 → 接口 1s+)。合并后
    compute_dark_flow 只跑一次, 两个产出共享同一份 dark 结果。

    修复 2026-08-21: 整体加 12s 硬超时保护(在线程池跑), 防止数据源慢/逐笔翻页死循环
    导致 summary 接口拖到 30s 超时。
    """
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(_main_intent_both_inner, symbol)
        try:
            return future.result(timeout=12.0)
        except concurrent.futures.TimeoutError:
            logger.warning(f"_main_intent_both({symbol}) 超时 12s, 返回空")
            return "", None
        except Exception as e:
            logger.warning(f"_main_intent_both({symbol}) 失败: {e}")
            return "", None


def _main_intent_both_inner(symbol: str) -> tuple[str, dict | None]:
    try:
        from src.core.dark_flow import compute_dark_flow
        from marketdata.symbol import Symbol as MDSymbol
        mdsym = MDSymbol.parse(symbol, "CN")
        dark = compute_dark_flow(mdsym)
        if not dark:
            return "", None

        # ---- 字符串摘要(与原 _main_intent_summary 同格式) ----
        parts = []
        main_net = dark.get("main_net", 0) or 0
        big_net = dark.get("big_net", 0) or 0
        mid_net = dark.get("mid_net", 0) or 0
        tag = "净流入" if main_net > 500e4 else ("净流出" if main_net < -500e4 else "平衡")
        parts.append(f"主力{tag}{main_net / 1e4:+.0f}万(超大单{big_net / 1e4:+.0f}/大单{mid_net / 1e4:+.0f})")
        if dark.get("main_intensity") is not None:
            parts.append(f"参与度{dark['main_intensity']:.0f}%买占{dark.get('main_buy_ratio') or 0:.0f}%")
        if dark.get("phase"):
            parts.append(f"阶段[{dark['phase']}]")
        if dark.get("auction_amt"):
            parts.append(f"竞价{dark['auction_amt'] / 1e4:.0f}万")
        chips = None
        try:
            from src.core.chip_distribution import compute_near_term_chips
            tc = f"{'sh' if symbol.startswith('6') else 'sz'}{symbol}"
            chips = compute_near_term_chips(tc, days=10)
            if chips:
                band = chips.get("cost_band")
                bstr = f" 成本带{band['low']}-{band['high']}" if band else ""
                parts.append(f"筹码峰{chips['peak_price']} 获利{chips['profit_ratio'] * 100:.0f}%{bstr}")
        except Exception:
            pass
        summary_str = " | ".join(parts)

        # ---- 结构化(与原 _main_intent_structured 同逻辑) ----
        intensity = dark.get("main_intensity")
        buy_ratio = dark.get("main_buy_ratio")
        seg = dark.get("segments") or {}
        tail = seg.get("tail", 0)
        if dark.get("data_status") in ("insufficient", "suspect"):
            structured = {
                "direction": "neutral",
                "main_net": main_net,
                "big_net": big_net,
                "mid_net": mid_net,
                "participation": intensity,
                "buy_ratio": buy_ratio,
                "auction_amt": dark.get("auction_amt", 0) or 0,
                "phase": dark.get("phase"),
                "signal": dark.get("signal"),
                "tail_net": tail,
                "data_status": "insufficient",
                "tick_count": dark.get("tick_count", 0),
                "ai_verdict": None,
                "board": _board_snapshot(symbol),
            }
        else:
            strong_absorb = (intensity or 0) >= 35 and (buy_ratio or 0) >= 48
            if main_net > 500e4:
                direction = "buy"
            elif main_net < -500e4:
                direction = "wash" if strong_absorb else "sell"
            else:
                direction = "absorb" if strong_absorb else "neutral"
            structured = {
                "direction": direction,
                "main_net": main_net,
                "big_net": big_net,
                "mid_net": mid_net,
                "participation": intensity,
                "buy_ratio": buy_ratio,
                "auction_amt": dark.get("auction_amt", 0) or 0,
                "phase": dark.get("phase"),
                "signal": dark.get("signal"),
                "tail_net": tail,
                "data_status": dark.get("data_status", "ok"),
                "tick_count": dark.get("tick_count", 0),
            }
            if chips:
                structured["chip_peak"] = chips.get("peak_price")
                band = chips.get("cost_band")
                if band:
                    structured["chip_band"] = {"low": band["low"], "high": band["high"]}
                structured["profit_ratio"] = chips.get("profit_ratio")
            structured["ai_verdict"] = _ai_counter_check(symbol, dark)
            structured["board"] = _board_snapshot(symbol)
        return summary_str, structured
    except Exception:
        return "", None


def _main_intent_summary(symbol: str) -> str:
    """主力意图结构化摘要(2026-08-11): 供通知/卡片展示, 不依赖 LLM 复述。

    Returns: 如 "主力净流出-2466万(超大单+5967/大单-8433) 参与度88%/买占49%
              筹码峰11.41 成本带10.84-11.98 获利74%"
    """
    try:
        from marketdata import Symbol as MDSymbol
        from src.core.dark_flow import compute_dark_flow
        mdsym = MDSymbol.parse(symbol, "CN")
        dark = compute_dark_flow(mdsym)
        if not dark:
            return ""
        parts = []
        main_net = dark.get("main_net", 0) or 0
        big_net = dark.get("big_net", 0) or 0
        mid_net = dark.get("mid_net", 0) or 0
        tag = "净流入" if main_net > 500e4 else ("净流出" if main_net < -500e4 else "平衡")
        parts.append(f"主力{tag}{main_net / 1e4:+.0f}万(超大单{big_net / 1e4:+.0f}/大单{mid_net / 1e4:+.0f})")
        if dark.get("main_intensity") is not None:
            parts.append(f"参与度{dark['main_intensity']:.0f}%买占{dark.get('main_buy_ratio') or 0:.0f}%")
        if dark.get("phase"):
            parts.append(f"阶段[{dark['phase']}]")
        if dark.get("auction_amt"):
            parts.append(f"竞价{dark['auction_amt'] / 1e4:.0f}万")
        # 筹码(新浪真实分布优先)
        try:
            from src.core.chip_distribution import compute_near_term_chips
            tc = f"{'sh' if symbol.startswith('6') else 'sz'}{symbol}"
            chips = compute_near_term_chips(tc, days=10)
            if chips:
                band = chips.get("cost_band")
                bstr = f" 成本带{band['low']}-{band['high']}" if band else ""
                parts.append(f"筹码峰{chips['peak_price']} 获利{chips['profit_ratio'] * 100:.0f}%{bstr}")
        except Exception:
            pass
        return " | ".join(parts)
    except Exception:
        return ""


def _main_intent_structured(symbol: str) -> dict | None:
    """主力意图结构化数据(2026-08-12): 供前端K线 markers/筹码叠加, 不依赖字符串解析。

    Returns: {
        direction: "buy" | "sell" | "neutral" | "wash" | "absorb"
            (2026-08-12 修正: 与 v14 判据对齐, 不止看净额)
            buy    = 主力净流入(>500万) → 吸筹
            wash   = 主力净流出但参与度高/买占高 → 洗盘吸筹(对倒换手, 意图仍是吸)
            absorb = 主力净额平衡但参与度高 → 疑似吸筹
            sell   = 主力净流出且买入强度不足 → 派发
            neutral= 平衡
        main_net: 主力净额(元)
        big_net: 超大单净额(元)
        mid_net: 大单净额(元)
        participation: 参与度(%)
        buy_ratio: 主力买占比(%)
        auction_amt: 竞价额(元)
        phase: 5日阶段
        signal: 综合信号文本
        chip_peak: 筹码峰价
        chip_band: {"low": 成本带下沿, "high": 成本带上沿}
        profit_ratio: 获利盘比例(0-1)
    }
    """
    try:
        from marketdata import Symbol as MDSymbol
        from src.core.dark_flow import compute_dark_flow
        mdsym = MDSymbol.parse(symbol, "CN")
        dark = compute_dark_flow(mdsym)
        if not dark:
            return None
        main_net = dark.get("main_net", 0) or 0
        big_net = dark.get("big_net", 0) or 0
        mid_net = dark.get("mid_net", 0) or 0
        intensity = dark.get("main_intensity")
        buy_ratio = dark.get("main_buy_ratio")
        seg = dark.get("segments") or {}
        tail = seg.get("tail", 0)
        # 2026-08-12: 竞价/开盘初期数据不足(<30笔非竞价成交) → 标记 insufficient,
        # 前端显示"数据不足"而非误导性结论
        if dark.get("data_status") in ("insufficient", "suspect"):
            return {
                "direction": "neutral",
                "main_net": main_net,
                "big_net": big_net,
                "mid_net": mid_net,
                "participation": intensity,
                "buy_ratio": buy_ratio,
                "auction_amt": dark.get("auction_amt", 0) or 0,
                "phase": dark.get("phase"),
                "signal": dark.get("signal"),
                "tail_net": tail,
                "data_status": "insufficient",
                "tick_count": dark.get("tick_count", 0),
                # AI 反证层(算法5): 数据不足 → 方向强制 neutral, 无算法结论可反证,
                # 跳过 LLM 调用(ai_verdict=None); 板块快照仍可附带(失败→None)
                "ai_verdict": None,
                "board": _board_snapshot(symbol),
            }
        # v14 判据(与 _judge_signal 对齐): 参与度≥35% 且 买占≥48% = 强吸筹力度
        strong_absorb = (intensity or 0) >= 35 and (buy_ratio or 0) >= 48
        if main_net > 500e4:
            direction = "buy"
        elif main_net < -500e4:
            direction = "wash" if strong_absorb else "sell"
        else:
            direction = "absorb" if strong_absorb else "neutral"
        out: dict = {
            "direction": direction,
            "main_net": main_net,
            "big_net": big_net,
            "mid_net": mid_net,
            "participation": intensity,
            "buy_ratio": buy_ratio,
            "auction_amt": dark.get("auction_amt", 0) or 0,
            "phase": dark.get("phase"),
            "signal": dark.get("signal"),
            "tail_net": tail,
            "data_status": dark.get("data_status", "ok"),
            "tick_count": dark.get("tick_count", 0),
        }
        # 筹码(新浪真实分布优先)
        try:
            from src.core.chip_distribution import compute_near_term_chips
            tc = f"{'sh' if symbol.startswith('6') else 'sz'}{symbol}"
            chips = compute_near_term_chips(tc, days=10)
            if chips:
                out["chip_peak"] = chips.get("peak_price")
                band = chips.get("cost_band")
                if band:
                    out["chip_band"] = {"low": band["low"], "high": band["high"]}
                out["profit_ratio"] = chips.get("profit_ratio")
        except Exception:
            pass
        # ---- AI 反证层(算法5, 2026-08-14): 算法结论 + 当日事件 → LLM 综合评级+置信度 ----
        # 防对倒/拆单骗过纯算法; LLM 失败/超时/解析失败 → None(静默降级, 不影响算法结论)
        out["ai_verdict"] = _ai_counter_check(symbol, dark)
        # 板块异动快照(src.core.board_snapshot 缺失/采样不足/失败 → None 容错)
        out["board"] = _board_snapshot(symbol)
        return out
    except Exception:
        return None


# ──────────────── AI 反证层(算法5, 2026-08-14) ────────────────
# 纯算法(dark_flow)会被对倒/拆单骗: 算法给出方向后, 结合当日个股事件(公告/新闻)
# 用 LLM 做最终评级+置信度。LLM 失败/超时(8s)/解析失败一律静默降级返回 None,
# 绝不影响算法结论。事件源失败返回空 → prompt 明示"无当日事件"。

_AI_LLM_TIMEOUT = 8  # 反证层 LLM 超时秒数(超时 → 静默降级)

# ── v0.4.9: 反证层限速/冷却/缓存(修生产 429 rpm exhausted 风暴) ──
_AI_RATE_MAX_PER_MIN = 10      # 全局令牌桶: 每分钟最多 N 次 LLM 反证
_AI_429_COOLDOWN_S = 600       # 撞 429 后全局面板冷却 10 分钟, 期间反证直接降级 None
_ai_rate_lock = __import__("threading").Lock()
_ai_rate_window_start = [0.0]
_ai_rate_count = [0]
_ai_429_until = [0.0]


def _ai_rate_allow() -> bool:
    """全局令牌桶: 每分钟最多 _AI_RATE_MAX_PER_MIN 次; 429 冷却期内一律 False。"""
    import time as _t

    now = _t.time()
    with _ai_rate_lock:
        if now < _ai_429_until[0]:
            return False
        if now - _ai_rate_window_start[0] >= 60.0:
            _ai_rate_window_start[0] = now
            _ai_rate_count[0] = 0
        if _ai_rate_count[0] >= _AI_RATE_MAX_PER_MIN:
            return False
        _ai_rate_count[0] += 1
        return True


def _ai_rate_mark_429():
    """撞 429: 全局进入冷却窗口。"""
    import time as _t

    with _ai_rate_lock:
        _ai_429_until[0] = _t.time() + _AI_429_COOLDOWN_S

_AI_COUNTER_SYSTEM_PROMPT = (
    "你是一名A股主力资金行为反证分析师。给定算法(逐笔主力净额/参与度/买占比)给出的"
    "主力意图结论, 以及该股当日公告/新闻事件, 判断算法结论是否可信。\n"
    "规则:\n"
    "1. 事件与算法结论矛盾时必须给\"算法存疑\"或\"算法结论错误\"。例如: 算法说吸筹"
    "(buy/absorb/wash)但当日有减持/利空/立案/大额解禁等公告 → \"算法存疑\"或"
    "\"算法结论错误\"; 算法说派发(sell)但当日有大股东增持/回购 → 同样存疑。\n"
    "2. 无事件或事件中性(例行公告、与主力行为无关) → \"支持算法结论\"。\n"
    "3. 严禁编造事件; 事件为空就按\"无事件\"处理。\n"
    "4. 只输出 JSON, 不要任何其他文字。\n"
    "输出格式(严格 JSON):\n"
    '{"verdict": "支持算法结论"|"算法存疑"|"算法结论错误", '
    '"confidence": "高"|"中"|"低", "reason": "一句话理由(≤60字)"}'
)


# v0.4.9: 进程级复用的后台事件循环 — 修 "no running event loop"/"Event loop is closed"
# (旧实现每次新建+close loop, 而 AsyncOpenAI 的 HTTP 客户端进程级复用, 持有旧 loop 引用)
_bg_loop = None
_bg_loop_lock = __import__("threading").Lock()


def _get_bg_loop():
    global _bg_loop
    with _bg_loop_lock:
        if _bg_loop is None or _bg_loop.is_closed():
            _bg_loop = __import__("asyncio").new_event_loop()
            __import__("threading").Thread(target=_bg_loop.run_forever, daemon=True, name="ai-counter-loop").start()
        return _bg_loop


def _run_coro(coro):
    """同步上下文执行异步协程(v0.4.9): 统一投递到进程级后台 loop(run_coroutine_threadsafe),
    不再新建/关闭事件循环。若当前线程已有运行中 loop(不可能在 sync fn 里, 防御性保留),
    也走后台 loop。"""
    import asyncio
    import concurrent.futures

    loop = _get_bg_loop()
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        return fut.result(timeout=_AI_LLM_TIMEOUT + 5)
    except concurrent.futures.TimeoutError:
        fut.cancel()
        raise TimeoutError(f"_run_coro timeout {_AI_LLM_TIMEOUT + 5}s")


def _build_counter_client(db=None):
    """构造反证层 LLM 客户端: 优先 db 场景绑定(intraday_monitor/chat), 回落
    Settings/env(AI_BASE_URL/AI_API_KEY/AI_MODEL)。最简可靠路径。"""
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
            logger.debug(f"反证层场景绑定不可用(回落 Settings/env): {e}")
    from src.config import Settings

    settings = Settings()
    return AIClient(
        base_url=settings.ai_base_url,
        api_key=settings.ai_api_key,
        model=settings.ai_model,
    )


async def _ai_counter_llm_chat(dark_feat: str, events_summary: str, db=None) -> str:
    """单次 LLM 调用(8s 超时, wait_for 保证)。独立函数便于测试 monkeypatch
    AIClient.chat。客户端在协程内构造, 避免跨事件循环复用 httpx client。"""
    import asyncio

    client = _build_counter_client(db)
    user_content = (
        f"算法结论:\n{dark_feat}\n\n当日个股事件:\n{events_summary}\n\n请输出 JSON 评级。"
    )
    return await asyncio.wait_for(
        client.chat(_AI_COUNTER_SYSTEM_PROMPT, user_content, temperature=0.2),
        timeout=_AI_LLM_TIMEOUT,
    )


def _fetch_today_events(symbol: str) -> list[str]:
    """当日个股公告/新闻摘要(东财公告单源, 最多3条, 每条≤80字)。

    失败(网络/解析/无事件)一律返回空列表 → prompt 明示"无当日事件",
    严禁让 LLM 编造事件。"""
    try:
        from marketdata import Symbol as MDSymbol
        from marketdata.vendors.events import EventsVendor

        mdsym = MDSymbol.parse(symbol, "CN")
        items = EventsVendor().fetch([mdsym], {"since_days": 1})
        today = datetime.now().date()
        items = [ev for ev in items if getattr(ev, "publish_time", None)
                 and ev.publish_time.date() == today]
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


def _derive_direction(dark: dict) -> str:
    """由 dark 字段推导算法方向(与 _main_intent_structured 内联 v14 判据完全一致,
    供反证层复用, 避免两处逻辑漂移)。"""
    main_net = dark.get("main_net", 0) or 0
    intensity = dark.get("main_intensity")
    if intensity is None:
        intensity = dark.get("participation")
    buy_ratio = dark.get("main_buy_ratio")
    if buy_ratio is None:
        buy_ratio = dark.get("buy_ratio")
    if dark.get("data_status") in ("insufficient", "suspect"):
        return "neutral"
    strong_absorb = (intensity or 0) >= 35 and (buy_ratio or 0) >= 48
    if main_net > 500e4:
        return "buy"
    if main_net < -500e4:
        return "wash" if strong_absorb else "sell"
    return "absorb" if strong_absorb else "neutral"


def _ai_counter_check(symbol: str, dark: dict, db=None) -> dict | None:
    """AI 反证层(算法5): 算法结论 + 当日事件 → LLM 综合评级 + 置信度。

    防止纯算法被对倒/拆单骗: 算法方向与当日事件矛盾时(如算法说吸筹但当日有
    减持/利空公告), LLM 必须给"算法存疑"或"算法结论错误"。

    Args:
        symbol: 6位A股代码
        dark: compute_dark_flow 的原始结果 dict(或同构 dict)
        db: 可选 db session(用于场景模型绑定; None 回落 Settings/env)。
            2026-08-14 热修: None 时内部自建 SessionLocal —— 否则生产容器
            env 无 AI_* 配置, 反证层永远走空配置 → 永远 None。
    """
    # v0.4.9: 当日缓存 — 同股一天只评一次(biz_cache, TTL 到收盘), 省 token 且防风暴
    try:
        from datetime import datetime as _dt
        from src.web.cache.biz_cache import biz_cache

        _cache_key = f"ai_counter:{symbol}:{_dt.now().strftime('%Y%m%d')}"
        _cached = biz_cache.get_json(_cache_key)
        if _cached is not None:
            return _cached
    except Exception:
        _cache_key = None

    # v0.4.9: 全局限速 + 429 冷却 — 超额直接降级 None(不影响算法结论)
    if not _ai_rate_allow():
        return None

    _close_db = False
    if db is None:
        try:
            from src.web.database import SessionLocal
            db = SessionLocal()
            _close_db = True
        except Exception:
            db = None
    try:
        # ---- 算法特征摘要 ----
        main_net = dark.get("main_net", 0) or 0
        big_net = dark.get("big_net", 0) or 0
        mid_net = dark.get("mid_net", 0) or 0
        intensity = dark.get("main_intensity")
        if intensity is None:
            intensity = dark.get("participation")
        buy_ratio = dark.get("main_buy_ratio")
        if buy_ratio is None:
            buy_ratio = dark.get("buy_ratio")
        feat = [
            f"方向={_derive_direction(dark)}",
            f"主力净额={main_net / 1e4:+.0f}万(超大单{big_net / 1e4:+.0f}万/"
            f"大单{mid_net / 1e4:+.0f}万)",
        ]
        if intensity is not None:
            feat.append(f"参与度={intensity:.0f}%")
        if buy_ratio is not None:
            feat.append(f"买占比={buy_ratio:.0f}%")
        if dark.get("phase"):
            feat.append(f"5日阶段={str(dark['phase'])[:40]}")
        if dark.get("signal"):
            feat.append(f"综合信号={str(dark['signal'])[:60]}")
        dark_feat = "\n".join(feat)

        # ---- 当日事件(最多3条; 失败 → 无事件, 严禁编造) ----
        events = _fetch_today_events(symbol)
        events_summary = "\n".join(f"- {e}" for e in events) if events else "无当日事件"

        # ---- 单次 LLM 调用(超时/异常由外层兜住) ----
        raw = _run_coro(_ai_counter_llm_chat(dark_feat, events_summary, db))
        if not raw or not raw.strip():
            return None
        if "429" in raw or "rate" in raw.lower()[:50]:
            _ai_rate_mark_429()

        # ---- 解析 JSON(容忍 ```json 围栏) ----
        text = raw.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
        data = json.loads(text)
        if not isinstance(data, dict):
            return None
        verdict = str(data.get("verdict") or "").strip()
        confidence = str(data.get("confidence") or "").strip()
        reason = str(data.get("reason") or "").strip()
        if verdict not in {"支持算法结论", "算法存疑", "算法结论错误"}:
            return None
        if confidence not in {"高", "中", "低"}:
            return None
        if len(reason) > 60:
            reason = reason[:60] + "…"
        result = {"verdict": verdict, "confidence": confidence, "reason": reason}
        # v0.4.9: 成功结果写当日缓存(TTL 到收盘, 简化: 6h)
        if _cache_key:
            try:
                biz_cache.set_json(_cache_key, result, ttl=6 * 3600)
            except Exception:
                pass
        return result
    except Exception as e:
        # 429/限流类异常 → 全局冷却
        if "429" in repr(e) or "quota" in repr(e).lower() or "rate" in repr(e).lower():
            _ai_rate_mark_429()
            logger.warning(f"[ai-counter] 撞限流, 全局冷却 {_AI_429_COOLDOWN_S}s: {symbol}")
        else:
            logger.debug(f"AI 反证层失败(静默降级, 不影响算法结论): {symbol}: {e}")
        return None
    finally:
        if _close_db and db is not None:
            try:
                db.close()
            except Exception:
                pass


def _board_snapshot(symbol: str):
    """板块异动快照(算法5集成): src.core.board_snapshot 模块缺失/调用失败
    一律返回 None(容错, 前端不展示)。code 格式: sh/sz + 6位代码。"""
    try:
        from src.core.board_snapshot import get_board_manipulation

        code = f"{'sh' if symbol.startswith('6') else 'sz'}{symbol}"
        return get_board_manipulation(code)
    except Exception as e:
        logger.debug(f"板块快照获取失败(降级 None): {symbol}: {e}")
        return None


def _append_main_intent(lines: list, symbol: str) -> None:
    """主力意图段(2026-08-11 独立段): 逐笔主力 + 筹码分布 + 股东户数。

    与"资金面"口径隔离: 资金面=东财/腾讯静态资金流, 主力意图=逐笔实时+
    筹码面+股东户数交叉验证。任何数据源失败均静默, 不阻塞整段。
    """
    try:
        from marketdata import Symbol as MDSymbol
        from src.core.dark_flow import compute_dark_flow

        mdsym = MDSymbol.parse(symbol, "CN")
        dark = compute_dark_flow(mdsym)
        if not dark:
            return
        lines.append("\n## 主力意图(腾讯逐笔实时口径, 与资金面段不同源)")
        main_net = dark.get("main_net", 0) or 0
        big_net = dark.get("big_net", 0) or 0
        mid_net = dark.get("mid_net", 0) or 0
        small_net = dark.get("small_net", 0) or 0
        main_tag = "主力净流入" if main_net > 500e4 else ("主力净流出" if main_net < -500e4 else "主力平衡")
        line = (f"- 主力方向：{main_tag}(主力{main_net / 1e4:+.0f}万="
                f"超大单{big_net / 1e4:+.0f}+大单{mid_net / 1e4:+.0f}，"
                f"散户{small_net / 1e4:+.0f}万)")
        if dark.get("main_intensity") is not None:
            line += (f"，参与度{dark['main_intensity']:.0f}%"
                     f"/买占{dark.get('main_buy_ratio') or 0:.0f}%")
        if dark.get("phase"):
            line += f"，阶段[{dark['phase']}]"
        if dark.get("auction_amt"):
            line += f"，竞价{dark['auction_amt'] / 1e4:.0f}万"
        tail = dark.get("segments", {}).get("tail", 0) or 0
        if abs(tail) > 300e4:
            line += f"，尾盘{tail / 1e4:+.0f}万"
        lines.append(line)
        zones = dark.get("absorb_zones") or []
        if zones:
            zs = "、".join(f"{z['price']:.2f}(大单{z['big_net'] / 1e4:+.0f}万)" for z in zones[:3])
            lines.append(f"- 主力吸筹位：{zs}")
        split = dark.get("split_order") or {}
        if split.get("net") is not None and abs(split["net"]) >= 200e4:
            dir_s = "买入" if split["net"] > 0 else "卖出"
            lines.append(
                f"- 拆单识别：疑似主力{dir_s}{abs(split['net']) / 1e4:.0f}万"
                f"(逆势{len([g for g in split.get('groups', []) if g.get('contrarian')])}组，"
                f"散户顺势{abs(split.get('herd_sell', 0) - split.get('herd_buy', 0)) / 1e4:.0f}万)"
            )
        # 筹码分布(2026-08-11): 主力成本区 + 套牢盘
        # 优先: 新浪历史分价表近期真实分布(精确); 降级: 三角分布估算
        try:
            from src.core.chip_distribution import compute_near_term_chips, compute_chips
            from marketdata.vendors.kline import fetch_tencent_kline_raw
            tc = f"{'sh' if symbol.startswith('6') else 'sz'}{symbol}"
            chips = compute_near_term_chips(tc, days=10)
            src_tag = "近期真实"
            if chips is None:
                kl = fetch_tencent_kline_raw(tc, 300)
                chips = compute_chips(kl) if len(kl) >= 50 else None
                src_tag = "估算"
            if chips:
                profit = chips["profit_ratio"] * 100
                pos = "上方(套牢)" if chips["peak_price"] > chips["last_close"] else ("下方(获利)" if chips["peak_price"] < chips["last_close"] else "持平")
                band = chips.get("cost_band")
                band_str = f" 成本带{band['low']}-{band['high']}({band['ratio']}%)" if band else ""
                lines.append(
                    f"- 筹码面({src_tag}): 峰{chips['peak_price']}({pos}) 获利盘{profit:.0f}% "
                    f"COST50={chips['cost_50']}{band_str}"
                )
        except Exception:
            pass
        # 股东户数(2026-08-11): 筹码集中度交叉验证
        try:
            from marketdata.vendors.tencent_info import fetch_stock_brief
            brief = fetch_stock_brief(mdsym)
            if brief and brief.get("gdgb"):
                g = brief["gdgb"]
                if g.get("gdrshb"):
                    chg = g["gdrshb"]
                    tag = "集中(吸筹)" if chg < 0 else ("分散(派发)" if chg > 0 else "持平")
                    lines.append(f"- 股东户数：{g.get('gdrs', '--')}户，变化{chg}%({tag})")
        except Exception:
            pass
    except Exception as e:
        logger.debug(f"主力意图段获取失败(不影响其他段): {e}")


def _append_decision_pioneer(lines: list, symbol: str) -> None:
    """决策先锋三指标段(2026-08-30): GS策略 + AI机构活跃度 + L2主力净流入。

    与主力意图段(逐笔, 真暗盘)不同源: 本段=GS趋势(日线均线交叉) + 机构活跃度(纯K线波动)
    + L2主力净流入(TQ get_more_info.Zjl_HB, 明盘口径, 同花顺"主力资金"对齐)。数据源失败静默。
    """
    try:
        from src.core.decision_pioneer import fetch_decision_pioneer

        d = fetch_decision_pioneer(symbol, "CN")
        if not d:
            return
        lines.append("\n## 决策先锋三指标(GS趋势 × 机构活跃度 × L2资金)")
        act = d.get("institution_activity")
        if act:
            ma5 = f"，5日均{act['ma5']}" if act.get("ma5") is not None else ""
            lines.append(
                f"- AI机构活跃度：{act['activity']:.2f}({act['level']})，"
                f"连强{act['streak_days']}日{ma5}〔生命线1.56/强势线3/大牛线6〕"
            )
        else:
            lines.append("- AI机构活跃度：无数据")
        gs = d.get("gs")
        if gs:
            sig = {"G": "G买(上穿)", "S": "S卖(下穿)"}.get(gs.get("signal"), "无")
            lines.append(f"- GS趋势：{gs['state']}(方向过滤, 买卖点滞后仅参考, 最近{sig})")
        else:
            lines.append("- GS趋势：无数据")
        l2 = d.get("l2") or {}
        if l2.get("available") and isinstance(l2.get("zjl_hb"), (int, float)):
            lines.append(
                f"- 主力净流入(L2·TQ)：{l2['zjl_hb'] / 1e4:+.0f}万"
                f"({l2.get('direction') or '平衡'})，"
                f"逐笔{l2.get('l2_tick_num') or 0}笔/委托{l2.get('l2_order_num') or 0}笔"
            )
        else:
            lines.append("- 主力净流入(L2·TQ)：无数据(TQ未连接或休市)")
    except Exception as e:  # noqa: BLE001
        logger.debug(f"决策先锋段获取失败(不影响其他段): {e}")


def get_realtime_volume_ratio(symbol: str, market: str = "CN") -> float | None:
    """取腾讯实时量比(独立于 ths quote 源, 后者无此字段)。

    盘中量比以腾讯口径为准(今日每分钟均量 / 5日每分钟均量),
    与 K 线口径(今日总量/5日均量)不同——开盘初期 K 线口径严重失真。
    """
    import time
    now = time.time()
    cached = _realtime_volume_ratio_cache.get(symbol)
    if cached and (now - cached[0]) < 30.0:
        return cached[1]
    try:
        from marketdata.vendors.tencent import TencentQuoteVendor
        from marketdata import Symbol

        vendor = TencentQuoteVendor()
        quotes = vendor.fetch([Symbol.parse(symbol, market)], {})
        ratio = None
        for q in quotes:
            ratio = getattr(q, "volume_ratio", None)
            if ratio:
                break
        _realtime_volume_ratio_cache[symbol] = (now, ratio)
        return ratio
    except Exception as e:
        logger.debug(f"腾讯实时量比获取失败 {symbol}: {e}")
        return None


def is_market_trading(market: MarketCode) -> bool:
    """按市场判断是否在交易时段。"""
    market_def = MARKETS.get(market)
    if not market_def:
        return False
    return market_def.is_trading_time()


def market_label(market: MarketCode) -> str:
    if market == MarketCode.CN:
        return "A股"
    if market == MarketCode.HK:
        return "港股"
    if market == MarketCode.US:
        return "美股"
    return market.value


def build_ma_critical_warnings(current_price: float | None, ma_values: dict[str, float | None]) -> list[str]:
    """均线临界保护: 现价与 MA 距离 <1% 时生成警告行, 禁止 AI 断言"站上/跌破"。

    Args:
        current_price: 现价
        ma_values: {"MA5": 11.90, "MA10": 10.79, ...}(None 跳过)

    Returns:
        警告行列表; 距离足够远时返回空(不干扰正常判断)。
    """
    warnings: list[str] = []
    if current_price is None:
        return warnings
    try:
        price = float(current_price)
    except (TypeError, ValueError):
        return warnings
    for ma_name, ma_val in ma_values.items():
        if ma_val is None:
            continue
        try:
            ma_f = float(ma_val)
        except (TypeError, ValueError):
            continue
        dist_pct = abs(price - ma_f) / ma_f * 100
        pos = "上方" if price > ma_f else "下方"
        if dist_pct < 1.0:
            def _fmt(v: float) -> str:
                return f"{v:.2f}".rstrip("0").rstrip(".")
            warnings.append(
                f"- ⚠️ 现价{_fmt(price)}与{ma_name}={_fmt(ma_f)}距离仅{dist_pct:.2f}%"
                f"(现价在{ma_name}{pos})：处于临界区，禁止说'站上/跌破{ma_name}'，"
                f"只能说'贴近/在{ma_name}{pos}'"
            )
    return warnings


# 标准化操作建议
SUGGESTION_TYPES = {
    "建仓": "buy",  # 新开仓位
    "加仓": "add",  # 增加现有仓位
    "减仓": "reduce",  # 减少仓位
    "清仓": "sell",  # 全部卖出
    "持有": "hold",  # 维持现状
    "观望": "watch",  # 暂不操作
}

PROMPT_PATH = Path(__file__).parent.parent.parent / "prompts" / "intraday_monitor.txt"


class IntradayMonitorAgent(BaseAgent):
    """
    盘中监测 Agent

    特点：
    - 单只模式 (single): 逐只股票分析，每只单独发送通知
    - AI 智能判断: 把股票数据发给 AI，由 AI 决定是否值得提醒
    - 通知节流: 同一股票短时间内不重复通知
    - 技术分析: 包含 K 线和技术指标
    """

    name = "intraday_monitor"
    display_name = "盘中监测"
    description = "交易时段实时监控持仓，AI 判断是否有值得关注的信号"

    def __init__(
        self,
        throttle_minutes: int = 30,
        bypass_throttle: bool = False,
        bypass_market_hours: bool = False,
        event_only: bool = True,
        price_alert_threshold: float = 3.0,
        volume_alert_ratio: float = 2.0,
        stop_loss_warning: float = -5.0,
        take_profit_warning: float = 10.0,
    ):
        """
        Args:
            throttle_minutes: 同一股票通知间隔（分钟）
            bypass_throttle: 是否跳过节流（测试用）
            bypass_market_hours: 是否跳过交易时段门禁（仅手动分析场景）
            price_alert_threshold: 涨跌幅超过阈值视为价格异动（%）
            volume_alert_ratio: 量比超过阈值视为放量异动
            stop_loss_warning: 浮亏超过阈值触发止损预警（%）
            take_profit_warning: 浮盈超过阈值触发止盈提醒（%）
        """
        self.throttle_minutes = throttle_minutes
        self.bypass_throttle = bypass_throttle
        self.bypass_market_hours = bypass_market_hours
        self.event_only = event_only
        self.price_alert_threshold = price_alert_threshold
        self.volume_alert_ratio = volume_alert_ratio
        self.stop_loss_warning = stop_loss_warning
        self.take_profit_warning = take_profit_warning

    async def collect(self, context: AgentContext) -> dict:
        """采集实时行情 + K线 + 历史分析"""
        if not context.watchlist:
            logger.warning("自选股列表为空，跳过盘中监测")
            return {"stocks": [], "stock_data": None}

        # SignalPack: 统一结构化输入（quote/technical/position）
        stock_config = context.watchlist[0] if context.watchlist else None
        market = stock_config.market if stock_config else MarketCode.CN
        symbol = stock_config.symbol if stock_config else ""
        name = stock_config.name if stock_config else symbol

        # 按股票所属市场做交易时段门禁（而非全局任一市场开盘）
        if not self.bypass_market_hours and not is_market_trading(market):
            msg = f"当前{market_label(market)}非交易时段，已跳过执行"
            logger.info(f"{msg}: {symbol}")
            return {
                "stocks": [],
                "stock_data": None,
                "skip_reason": msg,
            }

        builder = SignalPackBuilder()
        packs = await builder.build_for_symbols(
            symbols=[(symbol, market, name)],
            include_news=True,
            news_hours=24,
            portfolio=context.portfolio,
            include_technical=True,
            include_capital_flow=True,
            include_events=True,
            events_days=3,
        )
        pack = packs.get(symbol)

        context_builder = ContextBuilder()
        context_pack = await context_builder.build_symbol_contexts(
            agent_name=self.name,
            context=context,
            packs=packs,
            realtime_hours=6,
            extended_hours=24,
            history_days=7,
            kline_days=60,
            persist_snapshot=True,
        )
        symbol_context = (context_pack.get("symbols", {}) or {}).get(symbol, {})
        quality_overview = context_pack.get("quality_overview", {}) or {}

        stock_data = pack.quote if pack and pack.quote else None

        kline_summary = pack.technical if pack else None

        # 获取历史分析（为 AI 提供更多上下文）
        daily_analysis = get_latest_analysis(
            agent_name="daily_report",
            stock_symbol="*",
            before_date=date.today(),
        )
        premarket_analysis = get_analysis(
            agent_name="premarket_outlook",
            stock_symbol="*",
            analysis_date=date.today(),
        )

        # 市场情绪 + 板块面 + 实时大盘指数(盘中情绪判断)
        market_sentiment = {}
        try:
            from src.collectors.market_sentiment_collector import (
                MarketSentimentCollector,
            )

            senti_c = MarketSentimentCollector()
            # 修复 2026-08-21: 同步 collector 调用改用 to_thread, 避免阻塞 asyncio 事件循环
            # 根因: 之前 sync get_* 调 requests.get → 整事件循环卡死, 26 并发 API 全部超时
            summary, sector_rotation, indices = await asyncio.gather(
                asyncio.to_thread(senti_c.get_sentiment_summary),
                asyncio.to_thread(senti_c.get_sector_rotation, top_n=12),
                asyncio.to_thread(senti_c.get_index_snapshot),
            )
            # 实时指数(腾讯接口,盘中实时): 大盘涨跌直接决定情绪强弱
            index_summary = []
            for idx in indices:
                pct = idx.get("pct") or 0
                tone = "🟢涨" if pct > 0 else ("🔴跌" if pct < 0 else "平")
                index_summary.append(
                    f"{idx.get('name')} {idx.get('price')} ({pct:+.2f}%) {tone}"
                )
            market_sentiment = {
                "sentiment": summary,
                "sectors": sector_rotation,
                "indices": indices,
                "index_summary": index_summary,
            }
        except Exception as e:
            logger.warning(f"市场情绪采集失败: {e}")
            market_sentiment = {}

        return {
            "stocks": [stock_data] if stock_data else [],
            "stock_data": stock_data,
            "kline_summary": kline_summary,
            "signal_pack": pack,
            "market_sentiment": market_sentiment,
            "daily_analysis": daily_analysis.content if daily_analysis else None,
            "premarket_analysis": premarket_analysis.content
            if premarket_analysis
            else None,
            "symbol_context": symbol_context,
            "quality_overview": quality_overview,
            "timestamp": datetime.now().isoformat(),
        }

    def build_prompt(self, data: dict, context: AgentContext) -> tuple[str, str]:
        """构建盘中分析 Prompt"""
        system_prompt = PROMPT_PATH.read_text(encoding="utf-8")

        # 辅助函数(2026-08-23 M6 升级): None/NaN/Inf/字符串/异常 → 返回 default,
        # 非法值不再原样透传给后续 /1e4 等运算触发 TypeError。default 默认 0 与旧实现一致。
        import math
        def safe_num(value, default=0):
            if value is None:
                return default
            try:
                f = float(value)
            except (TypeError, ValueError):
                return default
            if math.isnan(f) or math.isinf(f):
                return default
            return f

        def format_num(value, precision=2):
            if value is None:
                return "N/A"
            return f"{value:.{precision}f}"

        stock: StockData | None = data.get("stock_data")
        if not stock:
            return system_prompt, "无股票数据"

        # 获取所有账户的持仓信息
        positions = context.portfolio.get_positions_for_stock(stock.symbol)
        style_labels = {"short": "短线", "swing": "波段", "long": "长线"}

        lines = []
        lines.append(f"## 时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
        lines.append("> ⚠️ 数据时刻提醒：以下行情/技术指标为采集时刻快照。开盘初期(9:30-10:00)指标波动大，"
                     "KDJ/MA/量比可能随后续成交快速变化；描述时必须使用「当前/截至采集时刻」口径，"
                     "不得把快照数据说成确定事实，也不得用快照推断盘面方向。\n")

        # 实时大盘指数(最先给出,情绪判断首要依据)
        idx_summary = (data.get("market_sentiment") or {}).get("index_summary") or []
        if idx_summary:
            lines.append("## 实时大盘指数(当前,情绪判断首要依据)")
            for s in idx_summary:
                lines.append(f"- {s}")
            lines.append("")
            lines.append("> 规则: 指数红盘(涨)=情绪偏暖,指数绿盘(跌)=情绪偏冷。个股建议必须结合大盘方向。")
            lines.append("")

        # 股票行情
        current_price = safe_num(stock.current_price)
        change_pct = safe_num(stock.change_pct)
        change_amount = safe_num(stock.change_amount)
        open_price = safe_num(stock.open_price)
        high_price = safe_num(stock.high_price)
        low_price = safe_num(stock.low_price)
        prev_close = safe_num(stock.prev_close)
        volume = safe_num(stock.volume)
        turnover = safe_num(stock.turnover)

        lines.append("## 股票行情")
        lines.append(f"- 股票：{stock.name}（{stock.symbol}）")
        lines.append(f"- 现价：{current_price:.2f}")
        lines.append(f"- 涨跌幅：{change_pct:+.2f}%")
        lines.append(f"- 涨跌额：{change_amount:+.2f}")
        lines.append(f"- 今开：{open_price:.2f}")
        lines.append(f"- 最高：{high_price:.2f}")
        lines.append(f"- 最低：{low_price:.2f}")
        lines.append(f"- 昨收：{prev_close:.2f}")
        if volume > 0:
            lines.append(f"- 成交量：{volume:.0f} 手")
        if turnover > 0:
            lines.append(f"- 成交额：{turnover / 10000:.0f} 万")

        # 系统阈值（帮助 AI 做出更稳定的“提醒/不提醒”判断）
        # 价格异动改为相对个股自身波动率(ATR%)的自适应阈值,固定阈值作为下限/兜底。
        from src.core.intraday_event_gate import (
            DEFAULT_ATR_K,
            adaptive_price_threshold,
            is_abnormal_move,
        )

        kline_for_atr = data.get("kline_summary") or {}
        atr_pct = kline_for_atr.get("atr_pct")
        adaptive_threshold = adaptive_price_threshold(
            atr_pct, self.price_alert_threshold, DEFAULT_ATR_K
        )

        lines.append("\n## 系统阈值")
        if atr_pct is not None and atr_pct > 0:
            lines.append(
                f"- 价格异动：|涨跌幅| ≥ max(固定阈值 {self.price_alert_threshold:.1f}%, "
                f"{DEFAULT_ATR_K:g}×ATR%={atr_pct:.2f}%)={adaptive_threshold:.2f}%"
                f"（相对个股自身波动率自适应，固定阈值为下限）"
            )
        else:
            lines.append(
                f"- 价格异动：|涨跌幅| ≥ {self.price_alert_threshold:.1f}%"
                f"（ATR 不可用，回退固定阈值）"
            )
        lines.append(f"- 量能异动：量比 ≥ {self.volume_alert_ratio:.1f}")
        lines.append(f"- 止损预警：浮亏 ≤ {self.stop_loss_warning:.1f}%")
        lines.append(f"- 止盈提醒：浮盈 ≥ {self.take_profit_warning:.1f}%")
        price_hit = (
            "触发"
            if is_abnormal_move(
                change_pct,
                atr_pct,
                k=DEFAULT_ATR_K,
                fixed_threshold=self.price_alert_threshold,
            )
            else "未触发"
        )
        lines.append(f"- 当前涨跌幅：{change_pct:+.2f}%（{price_hit}）")

        symbol_ctx = data.get("symbol_context") or {}
        quality = (symbol_ctx.get("data_quality") or {})
        if quality:
            lines.append(
                f"- 上下文质量：{quality.get('score', 0)}（实时新闻 {quality.get('realtime_news_count', 0)} 条，扩展新闻 {quality.get('extended_news_count', 0)} 条，历史新闻 {quality.get('history_news_count', 0)} 条）"
            )

        layered_news = symbol_ctx.get("news") or {}
        realtime_news = layered_news.get("realtime") or []
        extended_news = layered_news.get("extended") or []
        history_news = layered_news.get("history") or []
        if realtime_news or extended_news or history_news:
            lines.append("\n## 新闻与事件上下文")
            chosen = realtime_news or extended_news or history_news
            for item in chosen[:5]:
                lines.append(
                    f"- [{item.get('time')}] {item.get('title')}（{item.get('source')}）"
                )
            hist_topic = (layered_news.get("history_topic") or {}).get("summary")
            if hist_topic:
                lines.append(f"- 历史新闻主题：{hist_topic}")

            # 题材关联研判(2026-08-11): 新闻→题材→个股 关联提示, 让 LLM 综合事件研判
            try:
                from marketdata import Symbol as MDSymbol
                from marketdata.vendors.tencent_info import fetch_stock_brief
                mdsym_t = MDSymbol.parse(stock.symbol, "CN")
                brief = fetch_stock_brief(mdsym_t)
                concepts_t = []
                if brief and brief.get("gsjj") and brief["gsjj"].get("concept"):
                    concepts_t = [c["name"] for c in brief["gsjj"]["concept"]][:12]
                if concepts_t:
                    lines.append(
                        f"- 该股题材：{'、'.join(concepts_t)}"
                    )
                # 事件溯源: 用题材关键词反查市场级事件新闻(2026-08-11 v2 修复)
                # v1 缺陷: 题材词精确匹配失败(如"机器人概念"≠"机器人")时 kw_pool 只剩
                # 通用事件词(火箭/发射/航天), 导致紫光股份(云计算)误报火箭新闻!
                # v2 修复: ①题材词包含匹配 ②命中条件=标题必须含该股题材词,
                #   通用事件词仅用于发现(搜索), 不直接判定命中。
                _EVENT_KW = ("军工", "航天", "低空", "无人机", "卫星", "芯片", "机器人", "AI", "算力",
                             "储能", "光伏", "新能源", "半导体", "信创", "数据要素", "量子", "生物")
                _NOISE_KW = ("融资融券", "转融券", "深股通", "沪股通", "国企改革", "政府控股", "股权转让",
                             "昨日高振幅", "昨日高换手", "中报预减", "标普", "富时")
                _TRIGGER_KW = ("火箭", "发射", "卫星", "航天", "获批", "涨价", "召回", "事故", "处罚",
                               "中标", "签约", "量产", "投产", "重组", "定增")
                _ABNORMAL_KW = ("失利", "推迟", "延期", "取消", "爆炸", "故障", "召回", "处罚",
                                "下修", "亏损", "违约", "爆雷", "立案")
                # 利好事件词(2026-08-11): 获批/中标/涨价/签约/量产等, 与利空对称识别
                _POSITIVE_KW = ("获批", "核准", "中标", "签约", "涨价", "提价", "量产", "投产",
                                "突破", "首飞", "成功", "交付", "增持", "回购", "重组获批", "定增落地",
                                "创新高", "订单", "合作", "入股", "预增", "扭亏")
                # 该股事件驱动型题材(包含匹配): 如"机器人概念"含"机器人"
                event_concepts = [c for c in concepts_t if any(k in c for k in _EVENT_KW)]
                # 搜索词 = 事件型题材词 + 通用事件词(仅发现用)
                search_kws = list(event_concepts[:4])
                search_kws += [w for w in _TRIGGER_KW if w not in search_kws][:4]
                # 判定用题材词(命中必须含该股题材, 防跨题材误报)
                judge_concepts = event_concepts or [c for c in concepts_t if c not in _NOISE_KW]
                event_hits: list = []
                try:
                    from src.core.marketdata_client import get_market_data
                    md_g = get_market_data()
                    for kw in search_kws:
                        if len(event_hits) >= 3:
                            break
                        try:
                            arts = md_g.news_by_keyword(kw)
                            for a in arts or []:
                                title = getattr(a, "title", "") or ""
                                # 关键: 标题必须含该股题材词才算"题材相关事件"
                                if not any(c in title for c in judge_concepts):
                                    continue
                                # 标记: ⚠️=利空异常 / ✅=利好 / 无标记=中性
                                is_abnormal = any(n in title for n in _ABNORMAL_KW)
                                is_positive = any(n in title for n in _POSITIVE_KW)
                                tag = "⚠️" if is_abnormal else ("✅" if is_positive else "")
                                if not any(n in title for n in ("涨超", "跌超", "涨幅", "跌幅", "涨停", "跌停")):
                                    event_hits.append(
                                        f"[{str(getattr(a, 'publish_time', ''))[:16]}] {title[:60]}{tag}"
                                    )
                                    if len(event_hits) >= 3:
                                        break
                        except Exception:
                            continue
                except Exception:
                    pass
                # 事件优先级: ⚠️利空异常 > ✅利好 > 中性
                def _ev_rank(h: str) -> int:
                    return 0 if "⚠️" in h else (1 if "✅" in h else 2)
                event_hits.sort(key=_ev_rank)
                if event_hits:
                    lines.append("- 题材相关事件：")
                    for eh in event_hits[:3]:
                        lines.append(f"  · {eh}")
                lines.append(
                    "> 研判指引: 判断上方「题材相关事件」与个股的关系, ⚠️=利空异常(事故/推迟/处罚等)"
                    " ✅=利好(获批/中标/涨价/成功等) 无标记=中性。命中需说明事件性质及其对主力意图"
                    "(利空低吸/利好派发等)的可能影响, 结合「主力意图」段综合研判, "
                    "无相关事件则说明'新闻与题材无明显关联'"
                )
            except Exception:
                pass

        kline_history = symbol_ctx.get("kline_history") or {}
        if kline_history.get("available"):
            lines.append("\n## 历史K线背景")
            lines.append(
                f"- 历史涨跌：5日{format_num(kline_history.get('ret_5d'), 1)}% / 20日{format_num(kline_history.get('ret_20d'), 1)}% / 60日{format_num(kline_history.get('ret_60d'), 1)}%"
            )
            if kline_history.get("volatility_20d") is not None:
                lines.append(
                    f"- 波动(20日标准差)：{format_num(kline_history.get('volatility_20d'), 2)}%"
                )
            if kline_history.get("breakout_state") and kline_history.get("breakout_state") != "none":
                lines.append(f"- 突破状态：{kline_history.get('breakout_state')}")

        # K 线和技术指标
        kline = data.get("kline_summary")
        if kline and not kline.get("error"):
            lines.append("\n## 技术分析")

            # 基础趋势
            lines.append(f"- 趋势：{kline.get('trend', 'N/A')}")
            lines.append(
                f"- 近5日：{kline.get('recent_5_up', 0)}涨{5 - kline.get('recent_5_up', 0)}跌"
            )
            lines.append(
                f"- 5日涨幅：{format_num(kline.get('change_5d'))}% | 20日涨幅：{format_num(kline.get('change_20d'))}%"
            )

            # MACD
            macd_info = f"MACD：{kline.get('macd_status', 'N/A')}"
            if kline.get("macd_cross_days"):
                macd_info += f"（{kline.get('macd_cross_days')}日前）"
            lines.append(f"- {macd_info}")

            # RSI
            rsi_status = kline.get("rsi_status")
            rsi6 = kline.get("rsi6")
            if rsi_status and rsi6 is not None:
                lines.append(f"- RSI(6)：{rsi6:.1f}（{rsi_status}）")

            # KDJ
            kdj_status = kline.get("kdj_status")
            kdj_k, kdj_d, kdj_j = (
                kline.get("kdj_k"),
                kline.get("kdj_d"),
                kline.get("kdj_j"),
            )
            if kdj_status and kdj_k is not None:
                lines.append(
                    f"- KDJ：K={kdj_k:.1f} D={kdj_d:.1f} J={kdj_j:.1f}（{kdj_status}）"
                )
                # 临界保护: K≈D 时状态易翻转, 明确提示 AI 不得据此断言方向
                if kdj_d is not None and abs(kdj_k - kdj_d) < 1.0:
                    lines.append("- ⚠️ KDJ 处于临界(K≈D)，金叉/死叉随时可能翻转，禁止据此单独判断买卖方向")

            # 布林带
            boll_status = kline.get("boll_status")
            boll_upper, boll_lower = kline.get("boll_upper"), kline.get("boll_lower")
            if boll_status and boll_upper is not None:
                lines.append(
                    f"- 布林带：上轨={format_num(boll_upper)} 下轨={format_num(boll_lower)}（{boll_status}）"
                )

            # 量能
            volume_trend = kline.get("volume_trend")
            kline_volume_ratio = kline.get("volume_ratio")
            # 实时量比(腾讯行情口径)优先: K线口径=今日总量/5日均量, 盘中会系统性偏低
            # (尤其开盘初期), 实时口径=今日每分钟均量/5日每分钟均量, 才是标准量比。
            realtime_volume_ratio = (
                getattr(stock, "volume_ratio", None)
                or get_realtime_volume_ratio(stock.symbol, stock.market.value)
            )
            use_realtime = realtime_volume_ratio is not None and realtime_volume_ratio > 0
            if use_realtime:
                assert realtime_volume_ratio is not None
                vol_info = f"量能：量比={realtime_volume_ratio:.2f}(实时口径)"
                vol_hit = "触发" if realtime_volume_ratio >= self.volume_alert_ratio else "未触发"
            elif volume_trend:
                vol_info = f"量能：{volume_trend}"
                if kline_volume_ratio:
                    vol_info += f"（量比={kline_volume_ratio:.2f}）"
                vol_hit = "触发" if kline_volume_ratio and kline_volume_ratio >= self.volume_alert_ratio else "未触发"
            else:
                vol_info = "量能：无数据"
                vol_hit = "未触发"
            lines.append(f"- {vol_info}")
            lines.append(f"- 量比阈值判断：{vol_hit}")
            # 开盘初期标注: K线口径量比在交易前 30 分钟不可信
            now_min = datetime.now().hour * 60 + datetime.now().minute
            market_open = 9 * 60 + 30
            if market_open <= now_min <= market_open + 30 and not use_realtime and kline_volume_ratio is not None and kline_volume_ratio < 0.5:
                lines.append("- ⚠️ 开盘初期(9:30-10:00)：K线口径量比偏低不代表缩量，请以实时量比或盘中走势为准")

            # 波动率（ATR）：个股自身波动基准，用于判断"异动 vs 正常波动"
            atr_val = kline.get("atr")
            atr_pct_val = kline.get("atr_pct")
            if atr_pct_val is not None:
                atr_line = f"波动率：ATR={format_num(atr_val)}（ATR%={format_num(atr_pct_val)}%）"
                atr_line += (
                    f"，今日涨跌幅{change_pct:+.2f}% "
                    + (
                        "超出"
                        if abs(change_pct) >= adaptive_threshold
                        else "处于"
                    )
                    + f"自适应异动阈值{adaptive_threshold:.2f}%"
                )
                lines.append(f"- {atr_line}")

            # 均线
            ma5_val = kline.get('ma5')
            ma10_val = kline.get('ma10')
            ma20_val = kline.get('ma20')
            ma60_val = kline.get('ma60')
            lines.append(
                f"- MA5：{format_num(ma5_val)} | MA10：{format_num(ma10_val)} | MA20：{format_num(ma20_val)} | MA60：{format_num(ma60_val)}"
            )
            # 均线临界保护: 现价与 MA 距离 < 1% 时禁止断言"站上/跌破"(避免 11.95 vs 11.90 被说成跌破)
            try:
                price_now = float(current_price) if current_price is not None else None
            except (TypeError, ValueError):
                price_now = None
            lines.extend(build_ma_critical_warnings(price_now, {"MA5": ma5_val, "MA10": ma10_val}))

            # K线形态(自研同花顺形态 + TA-Lib 标准形态,2026-08-10 接入)
            kline_patterns = kline.get("kline_patterns") or []
            if kline_patterns:
                lines.append("\n- K线形态:")
                for p in kline_patterns[:6]:
                    pname = p.get("cn_name") or p.get("name") or ""
                    psig = p.get("signal") or ""
                    if p.get("source") == "talib":
                        lines.append(
                            f"  - {pname}(TA-Lib标准形态) {psig} 强度{p.get('strength', '')}"
                        )
                    else:
                        lines.append(
                            f"  - {pname}(同花顺形态) {psig} 位置:{p.get('position', '--')}"
                        )
                lines.append("")
                lines.append(
                    "> 形态规则: 看涨形态(锤子线/红三兵/早晨之星/金针探底等)在低位更有意义, 看跌形态(射击之星/三只乌鸦/黄昏之星等)在高位更危险。形态需与趋势/量能/资金面交叉确认, 不单独构成买卖依据。"
                )

        # 市场情绪面 + 板块面(涨停池/情绪周期/板块涨跌资金)
        ms = data.get("market_sentiment", {}) or {}
        senti = ms.get("sentiment") or {}
        if senti and not senti.get("error"):
            lines.append("\n## 涨停池参考(隔日数据,仅参考)")
            lines.append(
                f"- 最近交易日涨停家数：{senti.get('limit_up_count', '-')}，最高连板：{senti.get('max_streak', '-')} 板"
            )
            ladder = senti.get("ladder", {}) or {}
            if ladder:
                ladder_str = "，".join(
                    f"{k}板×{v}家" for k, v in list(ladder.items())[:5]
                )
                lines.append(f"- 连板梯队：{ladder_str}")
            lines.append("- ⚠️ 这是隔日(最近交易日)数据,不是今日实时。今日情绪强弱以「实时大盘指数」为准,禁止用此数据推断今日情绪周期")
            lines.append("")

        # 板块面(行业+概念涨幅榜,判断个股所处板块强弱)
        sectors = ms.get("sectors") or {}
        indus = (sectors.get("industries") or [])[:5]
        concepts = (sectors.get("concepts") or [])[:5]
        if indus or concepts:
            lines.append("## 板块面(行业/概念涨跌与资金)")
            if indus:
                parts = [
                    f"{s.get('name')}{s.get('pct', 0):+.1f}%"
                    for s in indus
                    if s.get("name")
                ]
                lines.append(f"- 领涨行业：{'、'.join(parts)}")
            if concepts:
                parts = [
                    f"{s.get('name')}{s.get('pct', 0):+.1f}%"
                    for s in concepts
                    if s.get("name")
                ]
                lines.append(f"- 领涨概念：{'、'.join(parts)}")
            # 个股所属板块强弱(2026-08-12 修复): 从概念涨幅榜反查该股题材,
            # 之前只显示市场领涨(彩票/中药等), LLM 无从判断个股板块强弱
            try:
                from marketdata import Symbol as MDSymbol2
                from marketdata.vendors.tencent_info import fetch_plate_list
                plates = fetch_plate_list(MDSymbol2.parse(stock.symbol, "CN"))
                stock_concepts = []
                if plates and plates.get("concept"):
                    stock_concepts = [c.get("name", "") for c in plates["concept"] if c.get("name")]
                # 在涨幅榜里找个股所属概念
                own = [
                    f"{s.get('name')}{s.get('pct', 0):+.1f}%"
                    for s in (sectors.get("concepts") or [])
                    if s.get("name") in stock_concepts
                ][:3]
                if own:
                    lines.append(f"- 个股所属概念：{'、'.join(own)}")
                else:
                    lines.append("- 个股所属概念：未在涨幅榜(板块偏弱或不在领涨)")
            except Exception:
                pass
            lines.append("")
            lines.append("> 判断个股所属板块是否处于当日强势方向: 板块强+个股强=顺势, 板块弱+个股异动=独立行情(谨慎)")

        # 资金流向（仅A股，若可用）
        pack = data.get("signal_pack")
        flow = getattr(pack, "capital_flow", None) if pack else None
        lines.append("\n## 资金面(东财四档口径, 与主力意图段不同源)")
        if (
            isinstance(flow, dict)
            and flow
            and not flow.get("error")
            and flow.get("status")
        ):
            try:
                inflow = float(flow.get("main_net_inflow") or 0)
                inflow_pct = float(flow.get("main_net_inflow_pct") or 0)
                inflow_str = (
                    f"{inflow / 1e8:+.2f}亿"
                    if abs(inflow) >= 1e8
                    else f"{inflow / 1e4:+.0f}万"
                )
                # 主力净流入(方向明确: +流入 / -流出)
                direction = "流入" if inflow > 0 else ("流出" if inflow < 0 else "平衡")
                # 数据基准日: 新浪/东财 daykline 是 T-1 收盘数据(盘中无当日实时)
                flow_date = flow.get("date") or flow.get("opendate") or "最近交易日"
                lines.append(
                    f"- 资金：主力净{direction} {inflow_str}（占比{inflow_pct:+.1f}%，数据基准日 {flow_date}）"
                )
                # 分项净流入(超大单/大单, 有值才显示)
                super_net = flow.get("super_net_inflow")
                big_net = flow.get("big_net_inflow")
                if super_net is not None or big_net is not None:
                    parts = []
                    if super_net is not None:
                        sn = float(super_net)
                        parts.append(
                            f"超大单净{('流入' if sn > 0 else '流出' if sn < 0 else '平衡')} "
                            f"{sn / 1e8:+.2f}亿" if abs(sn) >= 1e8 else
                            f"超大单净{('流入' if sn > 0 else '流出' if sn < 0 else '平衡')} {sn / 1e4:+.0f}万"
                        )
                    if big_net is not None:
                        bn = float(big_net)
                        if abs(bn) >= 1e8:
                            parts.append(
                                f"大单净{('流入' if bn > 0 else '流出' if bn < 0 else '平衡')} {bn / 1e8:+.2f}亿"
                            )
                        else:
                            parts.append(
                                f"大单净{('流入' if bn > 0 else '流出' if bn < 0 else '平衡')} {bn / 1e4:+.0f}万"
                            )
                    lines.append(f"  - 分项：{'，'.join(parts)}")
                if flow.get("trend_5d") and flow.get("trend_5d") != "无数据":
                    lines.append(f"- 5日资金：{flow.get('trend_5d')}")
                # 判断规则: 主力净流入+分项同向 = 真流入; 主力净流入但超大单流出 = 分歧
                if inflow > 0 and super_net is not None and float(super_net) < 0:
                    lines.append(
                        "  - ⚠️ 主力净流入但超大单净流出(分歧): 可能是大单拉抬、超大单出货,谨慎追涨"
                    )
                # 口径提醒(2026-08-11): 本段为东财四档(按单笔金额分档), 与「主力意图」段腾讯逐笔不同源。
                # 两段方向冲突时, 以「主力意图」段(逐笔, 已验证与同花顺暗盘对齐)为准。
                lines.append(
                    "> 口径提醒: 本段「资金面」为东财四档口径(按单笔金额分档统计主动买卖)。"
                    "下方「主力意图」段为腾讯逐笔实时口径(≥20万或600手), 两段可能方向不同, "
                    "判断主力吸筹/派发一律以「主力意图」段为准, 本段仅作资金面参考。"
                )
                # 盘口大单面板(腾讯, 2026-08-11 接入): 大单占比 + 大单分档统计, 失败静默
                mdsym = None
                try:
                    from marketdata.vendors.tencent_panel import (
                        fetch_pan_analysis,
                        fetch_big_order_stats,
                    )
                    from marketdata import Symbol as MDSymbol

                    mdsym = MDSymbol.parse(stock.symbol, "CN")
                    pan = fetch_pan_analysis(mdsym)
                    if pan:
                        net_big = pan["buy_big"] - pan["sell_big"]
                        side = "买盘大单占优" if net_big > 0 else ("卖盘大单占优" if net_big < 0 else "大单平衡")
                        lines.append(
                            f"- 盘口大单占比：买大{pan['buy_big']:.1f}%/买小{pan['buy_small']:.1f}%/"
                            f"卖大{pan['sell_big']:.1f}%/卖小{pan['sell_small']:.1f}% ({side})"
                        )
                    stats = fetch_big_order_stats(mdsym)
                    if stats:
                        # 大单分档: 前3档净买入 + 档位1(最大单)买卖方向
                        t1 = stats[0]
                        t1_net = t1["buy"] - t1["sell"]
                        t1_dir = "净买入" if t1_net > 0 else ("净卖出" if t1_net < 0 else "平衡")
                        total_buy = sum(s["buy"] for s in stats)
                        total_sell = sum(s["sell"] for s in stats)
                        net = total_buy - total_sell
                        net_dir = "净买入" if net > 0 else ("净卖出" if net < 0 else "平衡")
                        lines.append(
                            f"- 大单分档统计：{len(stats)}档，最大单(档1)单数{t1['count']}笔/金额{t1['amount_wan']:.0f}万({t1_dir})，"
                            f"全档合计{net_dir}{abs(net) / 1e4:.0f}万元"
                        )
                except Exception as e:
                    logger.debug(f"盘口大单面板获取失败(不影响资金面): {e}")
            except Exception:
                lines.append("- ⚠️ 资金数据解析失败(数据源返回异常),资金面留空")
        else:
            # 显式标"无数据",AI 看到这条不会瞎编;同时给出失败原因(供调试)
            if isinstance(flow, dict) and flow.get("error"):
                lines.append(f"- ⚠️ 资金数据源异常({flow.get('error')[:80]}),资金面留空,禁止编造")
            else:
                lines.append("- ⚠️ 暂无资金数据(数据源未返回),资金面留空,禁止编造")

            # 多级支撑压力
            support_m, resistance_m = kline.get("support_m"), kline.get("resistance_m")
            if support_m and resistance_m:
                lines.append(
                    f"- 中期支撑：{format_num(support_m)} | 中期压力：{format_num(resistance_m)}"
                )

            support_s, resistance_s = kline.get("support_s"), kline.get("resistance_s")
            if support_s and resistance_s:
                lines.append(
                    f"- 短期支撑：{format_num(support_s)} | 短期压力：{format_num(resistance_s)}"
                )

            # K线形态
            kline_pattern = kline.get("kline_pattern")
            if kline_pattern:
                lines.append(f"- K线形态：{kline_pattern}")

            # 振幅
            amplitude = kline.get("amplitude")
            amplitude_avg5 = kline.get("amplitude_avg5")
            if amplitude is not None:
                amp_info = f"今日振幅：{amplitude:.2f}%"
                if amplitude_avg5 is not None:
                    amp_info += f"（5日平均：{amplitude_avg5:.2f}%）"
                lines.append(f"- {amp_info}")

        # ============ 主力意图(独立段, 2026-08-11) ============
        # 逐笔实时口径 + 筹码面 + 股东户数, 资金面之外的决策核心
        _append_main_intent(lines, stock.symbol)

        # ============ 决策先锋三指标(2026-08-30) ============
        # GS趋势 + 机构活跃度 + L2主力净流入(对齐同花顺暗盘)
        _append_decision_pioneer(lines, stock.symbol)

        # 账户资金情况
        lines.append("\n## 账户资金")
        lines.append(f"- 总可用资金：{context.portfolio.total_available_funds:.0f} 元")
        for acc in context.portfolio.accounts:
            lines.append(f"  - {acc.name}：{acc.available_funds:.0f} 元")
        constraints = symbol_ctx.get("constraints") or {}
        if constraints:
            lines.append(
                f"- 单票仓位占比：{safe_num(constraints.get('single_position_ratio'), 0) * 100:.1f}%（{constraints.get('risk_budget_hint', 'normal')}）"
            )
        memory = symbol_ctx.get("memory") or {}
        if memory:
            lines.append(
                f"- 历史上下文记忆：近{memory.get('window_days', 30)}天质量均值{safe_num(memory.get('avg_quality_score'), 0):.1f}，趋势{memory.get('quality_trend', 'flat')}"
            )
            if memory.get("latest_history_topic"):
                lines.append(f"- 历史记忆主题：{memory.get('latest_history_topic')}")

        # 各账户持仓信息
        if positions:
            lines.append(f"\n## 持仓情况（共 {len(positions)} 个账户）")
            for i, pos in enumerate(positions, 1):
                cost_price = safe_num(pos.cost_price, 1)
                pnl_pct = (
                    (current_price - cost_price) / cost_price * 100
                    if cost_price > 0
                    else 0
                )
                style_label = style_labels.get(pos.trading_style, "波段")
                market_value = current_price * pos.quantity
                # 找到对应账户的可用资金
                acc_funds = 0
                for acc in context.portfolio.accounts:
                    if acc.id == pos.account_id:
                        acc_funds = acc.available_funds
                        break

                lines.append(f"\n### 持仓 {i}：{pos.account_name}")
                lines.append(f"- 交易风格：{style_label}")
                lines.append(f"- 成本价：{cost_price:.2f}")
                lines.append(f"- 持仓量：{pos.quantity} 股")
                lines.append(f"- 持仓市值：{market_value:.0f} 元")
                pnl_note = ""
                if pnl_pct <= self.stop_loss_warning:
                    pnl_note = "（触发止损预警）"
                elif pnl_pct >= self.take_profit_warning:
                    pnl_note = "（触发止盈提醒）"
                lines.append(f"- 浮动盈亏：{pnl_pct:+.1f}%{pnl_note}")
                lines.append(f"- 账户可用：{acc_funds:.0f} 元")
        else:
            lines.append("\n## 未持仓（仅关注）")
            lines.append("- 可用资金充足，可考虑建仓")

        # 历史分析上下文（帮助 AI 做出更好的判断）
        daily_analysis = data.get("daily_analysis")
        premarket_analysis = data.get("premarket_analysis")

        if daily_analysis or premarket_analysis:
            lines.append("\n## 历史分析参考")

            if daily_analysis:
                # 截取与当前股票相关的部分（最多 300 字）
                content = (
                    daily_analysis[:300] + "..."
                    if len(daily_analysis) > 300
                    else daily_analysis
                )
                lines.append("\n### 昨日盘后分析摘要")
                lines.append(content)

            if premarket_analysis:
                content = (
                    premarket_analysis[:300] + "..."
                    if len(premarket_analysis) > 300
                    else premarket_analysis
                )
                lines.append("\n### 今日盘前分析摘要")
                lines.append(content)

        lines.append("\n请结合技术分析、资金情况和历史分析，给出明确的操作建议。")

        user_content = "\n".join(lines)
        return system_prompt, user_content

    def _parse_suggestion(self, content: str) -> dict:
        """
        从 AI 响应中解析操作建议

        Returns:
            {
                "action": "hold",  # buy/add/reduce/sell/hold/watch
                "action_label": "持有",
                "signal": "...",
                "reason": "...",
                "should_alert": True
            }
        """
        result = {
            "action": "watch",
            "action_label": "观望",
            "signal": "",
            "reason": "",
            "should_alert": False,
        }

        # 1) Prefer JSON output (structured mode)
        obj = try_parse_action_json(content) or self._try_parse_loose_json(content)
        if obj:
            action = (obj.get("action") or "watch").strip()
            result["action"] = action
            result["action_label"] = (
                obj.get("action_label") or result["action_label"]
            ).strip()[:20]
            result["signal"] = (obj.get("signal") or "").strip()[:60]
            result["reason"] = (obj.get("reason") or "").strip()[:160]
            result["should_alert"] = action in {
                "buy",
                "add",
                "reduce",
                "sell",
                "alert",
                "avoid",
            }
            result["triggers"] = (
                obj.get("triggers") if isinstance(obj.get("triggers"), list) else []
            )
            result["invalidations"] = (
                obj.get("invalidations")
                if isinstance(obj.get("invalidations"), list)
                else []
            )
            result["risks"] = (
                obj.get("risks") if isinstance(obj.get("risks"), list) else []
            )
            return result

        # 检查是否无需提醒
        if "[无需提醒]" in content:
            result["should_alert"] = False
            result["action"] = "hold"
            result["action_label"] = "持有"
            # 提取"无需提醒"之后的原因作为 reason(否则建议池里只有 action 没有分析内容)
            after = content.split("[无需提醒]", 1)[1] if "[无需提醒]" in content else ""
            clean_after = re.sub(r"\*\*|##|#|「|」", "", after).strip()
            if clean_after:
                result["reason"] = clean_after[:100]
            else:
                result["reason"] = "AI 判断无需提醒"
            if not result["signal"]:
                result["signal"] = "无异常"
            return result

        # 提取建议类型（从全文搜索）
        for label, action in SUGGESTION_TYPES.items():
            if label in content:
                result["action"] = action
                result["action_label"] = label
                break

        # 提取信号（支持多种格式）
        signal_patterns = [
            r"「信号」\s*[:：]?\s*(.+?)(?=「|$|\n\n)",
            r"\*\*信号\*\*\s*[:：]?\s*(.+?)(?=\*\*|$|\n\n)",
            r"信号\s*[:：]\s*(.+?)(?=\n|$)",
        ]
        for pattern in signal_patterns:
            match = re.search(pattern, content, re.DOTALL)
            if match:
                result["signal"] = match.group(1).strip()[:50]
                break

        # 提取建议内容（支持多种格式）
        suggest_patterns = [
            r"「建议」\s*[:：]?\s*(.+?)(?=「|$|\n\n)",
            r"\*\*建议\*\*\s*[:：]?\s*(.+?)(?=\*\*|$|\n\n)",
            r"建议\s*[:：]\s*(.+?)(?=\n|$)",
        ]
        for pattern in suggest_patterns:
            match = re.search(pattern, content, re.DOTALL)
            if match:
                suggest_text = match.group(1).strip()
                # 从建议中提取操作类型
                for label, action in SUGGESTION_TYPES.items():
                    if label in suggest_text:
                        result["action"] = action
                        result["action_label"] = label
                        break
                # 如果信号为空，使用建议内容作为信号
                if not result["signal"]:
                    result["signal"] = suggest_text[:50]
                break

        # 提取理由（支持多种格式）
        reason_patterns = [
            r"「理由」\s*[:：]?\s*(.+?)(?=「|$|\n\n)",
            r"\*\*理由\*\*\s*[:：]?\s*(.+?)(?=\*\*|$|\n\n)",
            r"理由\s*[:：]\s*(.+?)(?=\n|$)",
        ]
        for pattern in reason_patterns:
            match = re.search(pattern, content, re.DOTALL)
            if match:
                result["reason"] = match.group(1).strip()[:100]
                break

        # 如果没有提取到信号和理由，尝试使用整段内容的前部分
        if not result["signal"] and not result["reason"]:
            # 清理 markdown 格式后取前 100 字符
            clean_content = re.sub(r"\*\*|##|#", "", content).strip()
            # 跳过无需提醒的情况
            if not clean_content.startswith("[无需提醒]"):
                result["reason"] = clean_content[:100]

        # 最终 should_alert 判定：只在明确“建仓/加仓/减仓/清仓”时提醒
        result["should_alert"] = result["action"] in {"buy", "add", "reduce", "sell"}
        return result

    def _try_parse_loose_json(self, text: str) -> dict | None:
        """宽松解析 JSON 输出，兜底兼容模型异常格式。"""
        raw = (text or "").strip()
        if not raw:
            return None

        # 兼容首行 "json"
        lines = raw.splitlines()
        if lines and lines[0].strip().lower() == "json":
            raw = "\n".join(lines[1:]).strip()

        # 去掉 fenced code block
        if raw.startswith("```"):
            block_lines = raw.splitlines()
            if len(block_lines) >= 3 and block_lines[-1].strip().startswith("```"):
                raw = "\n".join(block_lines[1:-1]).strip()
                if raw.lower().startswith("json\n"):
                    raw = raw[5:].strip()

        # 优先直接解析，失败则提取首个 JSON 对象片段
        try:
            obj = json.loads(raw)
        except Exception:
            m = re.search(r"\{[\s\S]*\}", raw)
            if not m:
                return None
            try:
                obj = json.loads(m.group(0))
            except Exception:
                return None

        if not isinstance(obj, dict):
            return None

        # 没有关键字段时不认为是建议 JSON
        keys = {"action", "action_label", "signal", "reason", "triggers", "invalidations", "risks"}
        if not any(k in obj for k in keys):
            return None
        return obj

    def _format_human_readable_content(
        self, stock: StockData, suggestion: dict, raw_content: str
    ) -> str:
        """当模型返回 JSON 时，生成可读通知内容。"""
        action_label = suggestion.get("action_label") or "观望"
        signal = suggestion.get("signal") or "无明显新信号"
        reason = suggestion.get("reason") or "请结合盘面与风控策略审慎判断。"
        triggers = (
            suggestion.get("triggers")
            if isinstance(suggestion.get("triggers"), list)
            else []
        )
        invalidations = (
            suggestion.get("invalidations")
            if isinstance(suggestion.get("invalidations"), list)
            else []
        )
        risks = (
            suggestion.get("risks") if isinstance(suggestion.get("risks"), list) else []
        )
        price = (
            f"{stock.current_price:.2f}" if getattr(stock, "current_price", None) else "N/A"
        )
        chg = f"{(stock.change_pct or 0):+.2f}%"
        lines = [
            f"{stock.name}（{stock.symbol}）",
            f"现价：{price}  涨跌：{chg}",
            f"建议：{action_label}",
            f"信号：{signal}",
            f"理由：{reason}",
        ]
        # 主力意图(2026-08-11): 结构化数据直接展示, 不依赖 LLM 复述
        intent = _main_intent_summary(stock.symbol)
        if intent:
            lines.append(f"主力意图：{intent}")
        # 决策先锋三指标(2026-08-30): 精简一行(GS+机构活跃度+L2)
        try:
            from src.core.decision_pioneer import decision_pioneer_text
            dp = decision_pioneer_text(stock.symbol, "CN")
            if dp:
                lines.append(f"决策先锋：{dp}")
        except Exception:  # noqa: BLE001
            pass
        if triggers:
            lines.append("触发条件：")
            lines.extend([f"- {str(x)}" for x in triggers[:3]])
        if invalidations:
            lines.append("失效条件：")
            lines.extend([f"- {str(x)}" for x in invalidations[:3]])
        if risks:
            lines.append("风险提示：")
            lines.extend([f"- {str(x)}" for x in risks[:3]])
        # 若本次并非纯 JSON，附上简短原文摘要便于核对
        if not (try_parse_action_json(raw_content) or self._try_parse_loose_json(raw_content)):
            brief = re.sub(r"\s+", " ", (raw_content or "").strip())[:200]
            if brief:
                lines.append(f"备注：{brief}")
        return "\n".join(lines)

    async def analyze(self, context: AgentContext, data: dict) -> AnalysisResult:
        """AI 分析并判断是否需要提醒"""
        # 非交易时段跳过
        if data.get("skip_reason"):
            return AnalysisResult(
                agent_name=self.name,
                title=f"【{self.display_name}】跳过",
                content=data.get("skip_reason", "跳过执行"),
                raw_data={"skipped": True, **data},
            )

        stock: StockData | None = data.get("stock_data")

        if not stock:
            return AnalysisResult(
                agent_name=self.name,
                title=f"【{self.display_name}】无数据",
                content="未获取到股票数据",
                raw_data=data,
            )

        system_prompt, user_content = self.build_prompt(data, context)

        # 统一 LLM 配置中心: reports 场景模型绑定 + 画像注入(无 db/绑定失败则原样)
        system_prompt = apply_scene_binding(context, "reports", system_prompt)

        # 打印完整 prompt 用于调试
        logger.info(f"=== Prompt for {stock.symbol} ===\n{user_content}")

        raw_content = await context.ai_client.chat(system_prompt, user_content)

        # 打印 AI 返回结果
        logger.info(f"=== AI Response for {stock.symbol} ===\n{raw_content}")

        # 解析操作建议
        suggestion = self._parse_suggestion(raw_content)
        content = raw_content
        analysis_date = (data.get("timestamp") or "")[:10] or datetime.now().strftime(
            "%Y-%m-%d"
        )
        quality_score = (
            (data.get("symbol_context") or {}).get("data_quality", {}).get("score")
        )
        # JSON/类 JSON 输出时，统一转换为可读通知文本，避免渠道直接推送原始 JSON
        if try_parse_action_json(raw_content) or self._try_parse_loose_json(raw_content):
            content = self._format_human_readable_content(stock, suggestion, raw_content)

        # M3(2026-08-23): 提取触发用户 UUID 喂给建议池, 多账号各自决策互不干扰
        _user_id = None
        try:
            _ctx_user = getattr(context, "user", None)
            if _ctx_user is not None:
                _user_id = getattr(_ctx_user, "id", None)
        except Exception:
            _user_id = None

        # 保存到建议池（包含 prompt 上下文）
        save_suggestion(
            stock_symbol=stock.symbol,
            stock_name=stock.name,
            action=suggestion["action"],
            action_label=suggestion["action_label"],
            signal=suggestion.get("signal", ""),
            reason=suggestion.get("reason", ""),
            agent_name=self.name,
            agent_label=self.display_name,
            expires_hours=6,  # 盘中建议 6 小时有效
            prompt_context=user_content,  # 保存 prompt 上下文
            ai_response=raw_content,  # 保存 AI 原始响应
            stock_market=stock.market.value,
            user_id=_user_id,
            meta={
                "quote": {
                    "current_price": stock.current_price,
                    "change_pct": stock.change_pct,
                },
                "kline_meta": {
                    "computed_at": (data.get("kline_summary") or {}).get("computed_at"),
                    "asof": (data.get("kline_summary") or {}).get("asof"),
                },
                "event_gate": data.get("event_gate"),
                "analysis_date": analysis_date,
                "context_quality_score": quality_score,
                "plan": {
                    "triggers": suggestion.get("triggers")
                    if isinstance(suggestion, dict)
                    else [],
                    "invalidations": suggestion.get("invalidations")
                    if isinstance(suggestion, dict)
                    else [],
                    "risks": suggestion.get("risks")
                    if isinstance(suggestion, dict)
                    else [],
                },
            },
        )
        for horizon in (1, 5):
            save_agent_prediction_outcome(
                agent_name=self.name,
                stock_symbol=stock.symbol,
                stock_market=stock.market.value,
                prediction_date=analysis_date,
                horizon_days=horizon,
                action=suggestion.get("action") or "watch",
                action_label=suggestion.get("action_label") or "观望",
                confidence=(float(quality_score) / 100.0)
                if quality_score is not None
                else None,
                trigger_price=getattr(stock, "current_price", None),
                meta={
                    "source": "intraday_monitor",
                    "reason": suggestion.get("reason", ""),
                    "signal": suggestion.get("signal", ""),
                },
            )

        save_agent_context_run(
            agent_name=self.name,
            stock_symbol=stock.symbol,
            analysis_date=analysis_date,
            context_payload={
                "symbol_context": data.get("symbol_context") or {},
                "quality_overview": data.get("quality_overview") or {},
            },
            quality={"score": quality_score or 0},
        )

        # 构建标题
        title = f"【{self.display_name}】{stock.name} {stock.change_pct:+.2f}%"

        # 附 AI 模型信息
        if context.model_label:
            content = content.rstrip() + f"\n\n---\nAI: {context.model_label}"

        # 急涨/急跌联动:满足阈值时异步触发 TradingAgents 深度分析(默认关闭)
        try:
            from src.agents.tradingagents.auto_trigger import try_auto_trigger
            try_auto_trigger(stock, source_agent=self.name)
        except Exception:
            logger.exception("TA 联动触发失败,继续返回 intraday 结果")

        return AnalysisResult(
            agent_name=self.name,
            title=title,
            content=content,
            raw_data={
                "stock": {
                    "symbol": stock.symbol,
                    "name": stock.name,
                    "current_price": stock.current_price,
                    "change_pct": stock.change_pct,
                },
                "suggestion": suggestion,
                "should_alert": suggestion["should_alert"],
                "kline_summary": data.get("kline_summary"),
                "symbol_context": data.get("symbol_context") or {},
                "quality_overview": data.get("quality_overview") or {},
                **data,
            },
        )

    async def should_notify(self, result: AnalysisResult) -> bool:
        """检查是否需要通知"""
        # 跳过的结果不通知
        if result.raw_data.get("skipped"):
            return False

        # AI 判断不需要提醒
        if not result.raw_data.get("should_alert", True):
            logger.info(
                f"AI 判断无需提醒: {result.raw_data.get('stock', {}).get('symbol')}"
            )
            return False

        stock_data = result.raw_data.get("stock")
        if not stock_data:
            return False

        symbol = stock_data.get("symbol")
        if not symbol:
            return False

        # 检查节流（测试模式可跳过）
        if not self.bypass_throttle:
            if not self._check_throttle(symbol):
                logger.info(
                    f"通知节流: {symbol} 在 {self.throttle_minutes} 分钟内已通知"
                )
                return False
        else:
            logger.info(f"跳过节流检查（测试模式）: {symbol}")

        return True

    def _check_throttle(self, symbol: str) -> bool:
        """检查是否可以发送通知（未被节流）"""
        from src.web.database import SessionLocal
        from src.web.models import NotifyThrottle

        db = SessionLocal()
        try:
            record = (
                db.query(NotifyThrottle)
                .filter(
                    NotifyThrottle.agent_name == self.name,
                    NotifyThrottle.stock_symbol == symbol,
                )
                .first()
            )

            if not record:
                return True

            # 以 UTC 进行比较，避免容器/部署时区变化导致异常
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            threshold = now - timedelta(minutes=self.throttle_minutes)
            last = record.last_notify_at
            if last and last.tzinfo is not None:
                last = last.astimezone(timezone.utc).replace(tzinfo=None)
            return (last or datetime.fromtimestamp(0)) < threshold
        finally:
            db.close()

    def _update_throttle(self, symbol: str):
        """更新节流记录"""
        from src.web.database import SessionLocal
        from src.web.models import NotifyThrottle

        db = SessionLocal()
        try:
            record = (
                db.query(NotifyThrottle)
                .filter(
                    NotifyThrottle.agent_name == self.name,
                    NotifyThrottle.stock_symbol == symbol,
                )
                .first()
            )

            now = datetime.now(timezone.utc).replace(tzinfo=None)
            if record:
                # 检查是否是新的一天
                if record.last_notify_at.date() < now.date():
                    record.notify_count = 1
                else:
                    record.notify_count += 1
                record.last_notify_at = now
            else:
                db.add(
                    NotifyThrottle(
                        agent_name=self.name,
                        stock_symbol=symbol,
                        last_notify_at=now,
                        notify_count=1,
                    )
                )

            db.commit()
        finally:
            db.close()

    async def run_single(
        self, context: AgentContext, stock_symbol: str
    ) -> AnalysisResult | None:
        """
        单只模式执行：只分析指定的一只股票

        用于实时监控场景，每只股票独立分析和通知
        """
        # 过滤只保留指定股票
        original_watchlist = context.config.watchlist
        context.config.watchlist = [
            s for s in original_watchlist if s.symbol == stock_symbol
        ]

        if not context.config.watchlist:
            return None

        try:
            data = await self.collect(context)
            if not data.get("stock_data"):
                return None

            # 事件门禁仅作为上下文信号，不阻断 AI 分析。
            # 产品策略：建议持续刷新，通知再由 should_alert + throttle 控制降噪。
            if self.event_only:
                try:
                    from src.core.intraday_event_gate import check_and_update

                    stock = data.get("stock_data")
                    kline_summary = data.get("kline_summary")
                    decision = check_and_update(
                        symbol=stock_symbol,
                        change_pct=getattr(stock, "change_pct", None),
                        volume_ratio=(kline_summary or {}).get("volume_ratio"),
                        kline_summary=kline_summary,
                        price_threshold=self.price_alert_threshold,
                        volume_threshold=self.volume_alert_ratio,
                    )
                    data["event_gate"] = {
                        "reasons": decision.reasons,
                        "should_analyze": bool(decision.should_analyze),
                    }
                except Exception as e:
                    logger.debug(f"事件门禁异常，继续分析: {e}")

            result = await self.analyze(context, data)

            if getattr(context, "suppress_notify", False):
                result.raw_data["notified"] = False
                result.raw_data["notify_skipped"] = "suppressed"
                return result

            if await self.should_notify(result):
                notify_result = await context.notifier.notify_with_result(
                    result.title,
                    result.content,
                    result.images,
                )
                notified = bool(notify_result.get("success"))
                result.raw_data["notified"] = notified
                if notified:
                    logger.info(
                        f"Agent [{self.display_name}] 通知已发送: {stock_symbol}"
                    )
                    if not self.bypass_throttle:
                        self._update_throttle(stock_symbol)
                else:
                    notify_error = notify_result.get("error") or "未知错误"
                    result.raw_data["notify_error"] = notify_error
                    logger.error(
                        f"Agent [{self.display_name}] 通知发送失败: {stock_symbol} - {notify_error}"
                    )
            else:
                result.raw_data["notified"] = False

            return result
        finally:
            context.config.watchlist = original_watchlist
