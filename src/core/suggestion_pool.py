"""建议池管理 - 汇总各 Agent 建议"""

import logging
from datetime import datetime, timedelta
from typing import Optional
from sqlalchemy import and_, func, or_

from src.web.database import SessionLocal
from src.web.models import StockSuggestion
from src.core.timezone import utc_now, to_iso_with_tz, to_utc
from src.core.json_safe import to_jsonable

logger = logging.getLogger(__name__)


def _norm_text(s: str) -> str:
    return " ".join((s or "").strip().split())


def _dedupe_window_minutes(agent_name: str) -> int:
    # Default: keep the suggestion list stable and avoid repeated rows.
    # Intraday runs frequently; other agents run a few times a day.
    if agent_name == "intraday_monitor":
        return 30
    if agent_name == "news_digest":
        return 60
    return 180


# Agent 有效期配置（小时）
AGENT_EXPIRY_HOURS = {
    "premarket_outlook": 12,  # 盘前建议当日有效（约12小时）
    "intraday_monitor": 6,  # 盘中建议6小时有效
    "daily_report": 16,  # 盘后建议隔夜有效（到次日开盘，约16小时）
    "news_digest": 12,  # 新闻速递建议半天有效
}

# Agent 中文名称映射
AGENT_LABELS = {
    "premarket_outlook": "盘前分析",
    "intraday_monitor": "盘中监测",
    "daily_report": "收盘复盘",
    "news_digest": "新闻速递",
}

def save_suggestion(
    stock_symbol: str,
    stock_name: str,
    action: str,
    action_label: str,
    agent_name: str,
    signal: str = "",
    reason: str = "",
    agent_label: str = "",
    expires_hours: Optional[int] = None,
    prompt_context: str = "",
    ai_response: str = "",
    stock_market: str = "CN",
    meta: dict | None = None,
    user_id: str | None = None,
) -> bool:
    """
    M2(2026-08-23): 增加 user_id 维度。
    - 同一标的在不同账号下可分别存建议(不同账号决策互不干扰)
    - dedupe key 加 user_id: (user_id, stock_symbol, stock_market, agent_name)
    - 旧数据 user_id=NULL 与新建 user_id=某值 视为不同账号 → 不会触发误 dedupe
    """
    """
    保存 Agent 建议到建议池

    Args:
        stock_symbol: 股票代码
        stock_name: 股票名称
        action: 操作类型 (buy/add/reduce/sell/hold/watch/alert/avoid)
        action_label: 操作中文标签
        agent_name: Agent 名称
        signal: 信号描述
        reason: 建议理由
        agent_label: Agent 中文名称（可选，自动推断）
        expires_hours: 过期时间（小时），不指定则使用默认配置
        prompt_context: Prompt 上下文摘要
        ai_response: AI 原始响应

    Returns:
        是否保存成功
    """
    db = SessionLocal()
    try:
        market = (stock_market or "CN").strip().upper() or "CN"

        # 计算过期时间（使用 UTC）
        if expires_hours is None:
            expires_hours = AGENT_EXPIRY_HOURS.get(agent_name, 8)

        now = utc_now()
        expires_at = now + timedelta(hours=expires_hours)

        # Agent 标签
        if not agent_label:
            agent_label = AGENT_LABELS.get(agent_name, agent_name)

        # Dedupe(2026-08-23 M2): key 加 user_id 维度, 不同账号互不吞建议。
        try:
            latest = (
                db.query(StockSuggestion)
                .filter(
                    StockSuggestion.stock_symbol == stock_symbol,
                    StockSuggestion.stock_market == market,
                    StockSuggestion.agent_name == agent_name,
                    # user_id 维度过滤:None 与具体值严格区分(避免旧数据被新账号 dedupe 掉)
                    StockSuggestion.user_id.is_(None)
                    if user_id is None
                    else StockSuggestion.user_id == user_id,
                )
                .order_by(StockSuggestion.created_at.desc(), StockSuggestion.id.desc())
                .first()
            )

            if latest and latest.created_at:
                latest_created = to_utc(latest.created_at)

                window = timedelta(minutes=_dedupe_window_minutes(agent_name))
                same_key = (
                    _norm_text(latest.action) == _norm_text(action)
                    and _norm_text(latest.action_label) == _norm_text(action_label)
                    and _norm_text(latest.signal or "") == _norm_text(signal)
                )

                if same_key and (now - latest_created) <= window:
                    # Extend expiry (keep the first message to avoid churn).
                    # 注意(2026-08-23 bug fix): latest.expires_at 可能 naive(SQLite 默认无 tz),
                    # expires_at 是 tz-aware(utc_now()), 直接比较抛 TypeError 被 except 吞掉,
                    # 反而走到"创建新建议"路径导致 dedupe 失效。统一转为 naive UTC 再比较。
                    cur_exp = latest.expires_at
                    if cur_exp is not None and cur_exp.tzinfo is not None:
                        cur_exp_naive = cur_exp.replace(tzinfo=None)
                    else:
                        cur_exp_naive = cur_exp
                    new_exp_naive = expires_at.replace(tzinfo=None) if expires_at.tzinfo else expires_at
                    if not cur_exp_naive or cur_exp_naive < new_exp_naive:
                        latest.expires_at = new_exp_naive
                    if not (latest.stock_name or "") and stock_name:
                        latest.stock_name = stock_name
                    db.commit()
                    logger.info(
                        f"建议去重: {stock_symbol} {action_label} (来源: {agent_label})"
                    )
                    return True

                # Stability: avoid flip-flopping to a less severe action within a short window.
                try:
                    action_rank = {
                        "alert": 4,
                        "avoid": 4,
                        "sell": 4,
                        "reduce": 3,
                        "buy": 2,
                        "add": 2,
                        "hold": 1,
                        "watch": 0,
                    }
                    old_r = action_rank.get((latest.action or "").strip(), 0)
                    new_r = action_rank.get((action or "").strip(), 0)
                    change_window = timedelta(
                        minutes=_dedupe_window_minutes(agent_name)
                    )
                    if (now - latest_created) <= change_window and new_r < old_r:
                        # Keep the previous (more severe) action; extend expiry.
                        cur_exp = latest.expires_at
                        if cur_exp is not None and cur_exp.tzinfo is not None:
                            cur_exp_naive = cur_exp.replace(tzinfo=None)
                        else:
                            cur_exp_naive = cur_exp
                        new_exp_naive = expires_at.replace(tzinfo=None) if expires_at.tzinfo else expires_at
                        if not cur_exp_naive or cur_exp_naive < new_exp_naive:
                            latest.expires_at = new_exp_naive
                        if not (latest.stock_name or "") and stock_name:
                            latest.stock_name = stock_name
                        db.commit()
                        logger.info(
                            f"建议稳定: {stock_symbol} 新建议降级({action_label})，保持上一条({latest.action_label})"
                        )
                        return True
                except Exception:
                    db.rollback()
        except Exception:
            # Best-effort only; never block saving.
            db.rollback()

        # 创建新建议
        suggestion = StockSuggestion(
            user_id=user_id,
            stock_symbol=stock_symbol,
            stock_market=market,
            stock_name=stock_name,
            action=action,
            action_label=action_label,
            signal=signal,
            reason=reason,
            agent_name=agent_name,
            agent_label=agent_label,
            expires_at=expires_at,
            prompt_context=prompt_context[:2000] if prompt_context else "",  # 限制长度
            ai_response=ai_response[:2000] if ai_response else "",  # 限制长度
            meta=to_jsonable(meta or {}),
        )
        db.add(suggestion)
        db.commit()

        logger.info(f"保存建议: {stock_symbol} {action_label} (来源: {agent_label})")
        return True

    except Exception as e:
        logger.error(f"保存建议失败: {e}")
        db.rollback()
        return False
    finally:
        db.close()


def get_suggestions_for_stock(
    stock_symbol: str,
    stock_market: str | None = None,
    include_expired: bool = False,
    limit: int = 10,
    user_id: str | None = None,
) -> list[dict]:
    """
    获取某只股票的建议列表

    Args:
        stock_symbol: 股票代码
        include_expired: 是否包含已过期建议
        limit: 返回数量限制
        user_id: S5(2026-08-26) 归属过滤;None(内部调用/未登录场景)保持旧行为

    Returns:
        建议列表，按时间倒序
    """
    db = SessionLocal()
    try:
        query = db.query(StockSuggestion).filter(StockSuggestion.stock_symbol == stock_symbol)
        if stock_market:
            query = query.filter(
                StockSuggestion.stock_market == (stock_market or "CN").strip().upper()
            )
        # S5: 只返回本人建议 + user_id=NULL 的旧数据共享行
        if user_id is not None:
            query = query.filter(
                or_(
                    StockSuggestion.user_id == user_id,
                    StockSuggestion.user_id.is_(None),
                )
            )

        now = utc_now()
        if not include_expired:
            query = query.filter(
                (StockSuggestion.expires_at == None)
                | (StockSuggestion.expires_at > now)
            )

        suggestions = (
            query.order_by(StockSuggestion.created_at.desc()).limit(limit).all()
        )

        return [_to_dict(s, now) for s in suggestions]

    finally:
        db.close()


def get_latest_suggestions(
    stock_symbols: Optional[list[str]] = None,
    stock_keys: Optional[list[tuple[str, str]]] = None,
    include_expired: bool = False,
    user_id: str | None = None,
) -> dict[str, dict]:
    """
    获取所有股票的最新建议（每只股票只返回最新的一条）

    Args:
        stock_symbols: 股票代码列表，None 表示所有
        include_expired: 是否包含已过期建议
        user_id: S5(2026-08-26) 归属过滤;None(内部调用/调度)保持旧行为

    Returns:
        {symbol: suggestion_dict}
    """
    db = SessionLocal()
    try:
        subquery = (
            db.query(
                StockSuggestion.stock_symbol,
                StockSuggestion.stock_market,
                func.max(StockSuggestion.id).label("max_id"),
            )
            .group_by(StockSuggestion.stock_symbol, StockSuggestion.stock_market)
        )
        # S5: 子查询先按用户圈定候选行(NULL 视为旧数据共享), 再取每标的最新一条
        if user_id is not None:
            subquery = subquery.filter(
                or_(
                    StockSuggestion.user_id == user_id,
                    StockSuggestion.user_id.is_(None),
                )
            )
        subquery = subquery.subquery()

        query = db.query(StockSuggestion).join(
            subquery,
            and_(
                StockSuggestion.stock_symbol == subquery.c.stock_symbol,
                StockSuggestion.stock_market == subquery.c.stock_market,
                StockSuggestion.id == subquery.c.max_id,
            ),
        )

        if stock_keys:
            norm_keys = []
            for symbol, market in stock_keys:
                sym = (symbol or "").strip().upper()
                mkt = (market or "CN").strip().upper()
                if sym:
                    norm_keys.append((sym, mkt))
            if norm_keys:
                query = query.filter(
                    or_(
                        *[
                            and_(
                                StockSuggestion.stock_symbol == sym,
                                StockSuggestion.stock_market == mkt,
                            )
                            for sym, mkt in norm_keys
                        ]
                    )
                )
            else:
                return {}
        elif stock_symbols:
            query = query.filter(StockSuggestion.stock_symbol.in_(stock_symbols))

        now = utc_now()
        if not include_expired:
            query = query.filter(
                (StockSuggestion.expires_at == None)
                | (StockSuggestion.expires_at > now)
            )

        suggestions = query.all()

        result: dict[str, dict] = {}
        for s in suggestions:
            key = f"{(s.stock_market or 'CN').upper()}:{s.stock_symbol}"
            result[key] = _to_dict(s, now)
        return result

    finally:
        db.close()


def _to_dict(
    suggestion: StockSuggestion,
    now: Optional[datetime] = None,
    user_id: str | None = None,
    include_private: bool = True,
) -> dict:
    """将 StockSuggestion 转换为字典（时间使用 ISO 格式带时区）

    S5(2026-08-26) 隐私收敛: prompt_context/ai_response 仅在调用方声明
    include_private 且建议归属本人(或 user_id=NULL 共享行, 或内部调用未传 user_id)时下发;
    非本人建议一律置空, 防止跨账号泄露 Prompt/AI 原文。
    """
    if now is None:
        now = utc_now()

    is_expired = False
    if suggestion.expires_at:
        # naive 时间按 app 时区(北京)解读后再转 UTC 比较(2026-08-24 修复 +8 偏移)
        is_expired = to_utc(suggestion.expires_at) < now

    row_user_id = getattr(suggestion, "user_id", None)
    if include_private and user_id is not None:
        can_read_private = row_user_id == user_id or row_user_id is None
    else:
        can_read_private = True

    # 转换时间为带时区的 ISO 格式(naive 按 app 时区解读, 见 timezone.to_iso_with_tz)
    created_at_str = to_iso_with_tz(suggestion.created_at) if suggestion.created_at else None
    expires_at_str = to_iso_with_tz(suggestion.expires_at) if suggestion.expires_at else None

    return {
        "id": suggestion.id,
        "stock_symbol": suggestion.stock_symbol,
        "stock_market": suggestion.stock_market or "CN",
        "stock_name": suggestion.stock_name,
        "action": suggestion.action,
        "action_label": suggestion.action_label,
        "signal": suggestion.signal,
        "reason": suggestion.reason,
        "agent_name": suggestion.agent_name,
        "agent_label": suggestion.agent_label,
        "created_at": created_at_str,
        "expires_at": expires_at_str,
        "is_expired": is_expired,
        "prompt_context": (suggestion.prompt_context or "") if can_read_private else "",
        "ai_response": (suggestion.ai_response or "") if can_read_private else "",
        "meta": suggestion.meta or {},
        "should_alert": (suggestion.action or "")
        in ("alert", "avoid", "sell", "reduce"),
    }


def cleanup_expired_suggestions(days: int = 7, user_id: str | None = None) -> int:
    """
    清理过期的建议记录

    Args:
        days: 清理多少天前的记录
        user_id: S5(2026-08-26) 归属范围;None 保持旧行为(系统清理全量)

    Returns:
        删除的记录数
    """
    db = SessionLocal()
    try:
        cutoff = utc_now() - timedelta(days=days)
        query = db.query(StockSuggestion).filter(
            StockSuggestion.created_at < cutoff
        )
        # S5: 端点限本人范围 — 只删自己创建的建议, 不碰 NULL/旧数据与他人数据
        if user_id is not None:
            query = query.filter(StockSuggestion.user_id == user_id)
        result = query.delete()
        db.commit()
        logger.info(f"清理了 {result} 条过期建议")
        return result
    except Exception as e:
        logger.error(f"清理过期建议失败: {e}")
        db.rollback()
        return 0
    finally:
        db.close()
