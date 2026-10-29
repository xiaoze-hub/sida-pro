"""分析历史记录管理

## 读取归属(2026-10-29 五审: NULL 归属收口)

`AnalysisHistory.user_id IS NULL` 是**多用户改造前的历史行**: 里面可能是某个具体
用户的持仓成本/自选/AI 正文。旧读路径统一写成 `or_(user_id == uid, user_id IS NULL)`,
等于把这段历史当成"公共数据"发给所有登录用户 —— member 能直接读到别人的成本。

现在唯一口径见 `analysis_history_scope_clause`:

- member: 只读 `user_id == 本人`;
- owner : 本人 + NULL(数据责任人排查历史存量);
- 无会话: 一条都不读(fail-closed)。

**没有"NULL 即公开"**。要公开某份分析, 必须是显式 `scope=shared` 报告文件那条链路
(见 `src/core/report_scope.py`), 不能靠 AnalysisHistory 的 NULL 语义猜。
"""
import logging
import re
from datetime import date, datetime, timedelta

from sqlalchemy import false, or_

from src.core.agent_catalog import infer_agent_kind
from src.db.session import SessionLocal
from src.db.models import AnalysisHistory
from src.core.json_safe import to_jsonable

logger = logging.getLogger(__name__)

# TradingAgents 深度分析在 AnalysisHistory 里的 agent_name(见 agent.py: name = "tradingagents")
TA_AGENT_NAME = "tradingagents"


def analysis_history_scope_clause(user_id: str | None, role: str | None = None):
    """AnalysisHistory 读作用的唯一实现(member=本人, owner=本人+NULL, 无会话=空)。

    `user_id=None` **不再**表示"共享桶可读": 历史 NULL 行来源不明, 只给 owner。
    返回 SQLAlchemy 条件, 调用方直接 `.filter(...)`。
    """
    uid = str(user_id) if user_id else ""
    if not uid:
        return false()
    if str(role or "").strip().lower() == "owner":
        return or_(AnalysisHistory.user_id == uid, AnalysisHistory.user_id.is_(None))
    return AnalysisHistory.user_id == uid


def analysis_history_visible(record: AnalysisHistory | None, user_id: str | None, role: str | None = None) -> bool:
    """对象级同口径判定(已取到行时用, 避免再发一次 SQL)。"""
    if record is None:
        return False
    uid = str(user_id) if user_id else ""
    if not uid:
        return False
    rid = getattr(record, "user_id", None)
    if rid is None:
        return str(role or "").strip().lower() == "owner"
    return str(rid) == uid


def save_analysis(
    agent_name: str,
    stock_symbol: str,
    content: str,
    title: str = "",
    raw_data: dict | None = None,
    analysis_date: date | None = None,
    user_id: str | None = None,
    status: str = "success",
    error: str | None = None,
) -> bool:
    """
    保存分析结果

    - 同一天可以覆盖
    - 历史记录不可覆盖（通过数据库约束保证）
    - M3(2026-08-23): 增加 user_id 维度, 调度入口传入触发用户。
      多账号下报告归属当前用户, history 端点按 user 过滤(S1)。
    - 0.3(2026-09-08): 增加 status/error —— 降级报告显式落库,
      前端据此显示「未生成」而非把降级提示当分析渲染。

    Args:
        agent_name: Agent 名称，如 "daily_report"
        stock_symbol: 股票代码，"*" 表示全局分析
        content: AI 分析内容
        title: 分析标题
        raw_data: 原始数据快照
        analysis_date: 分析日期，默认今天
        user_id: 触发用户 UUID(系统调度/批量任务传 None, 走存量 NULL 共享)
        status: success / degraded / failed
        error: 降级或失败原因

    Returns:
        是否保存成功
    """
    if analysis_date is None:
        analysis_date = date.today()

    date_str = analysis_date.strftime("%Y-%m-%d")

    db = SessionLocal()
    try:
        payload = to_jsonable(raw_data or {})
        agent_kind = infer_agent_kind(agent_name)

        # 查找是否已存在(同用户同日覆盖;不同用户/系统调度各自独立一行)
        existing = db.query(AnalysisHistory).filter(
            AnalysisHistory.agent_name == agent_name,
            AnalysisHistory.stock_symbol == stock_symbol,
            AnalysisHistory.analysis_date == date_str,
            AnalysisHistory.user_id.is_(None)
            if user_id is None
            else AnalysisHistory.user_id == user_id,
        ).first()

        if existing:
            # 更新（同一天可覆盖, 但保持原 user_id 不变 —— 避免覆盖过程中丢失归属）
            existing.title = title
            existing.content = content
            existing.raw_data = payload
            existing.agent_kind_snapshot = agent_kind
            existing.status = status
            existing.error = error
            logger.info(f"更新分析记录: {agent_name}/{stock_symbol}/{date_str}")
        else:
            # 新增
            record = AnalysisHistory(
                user_id=user_id,
                agent_name=agent_name,
                stock_symbol=stock_symbol,
                analysis_date=date_str,
                title=title,
                content=content,
                raw_data=payload,
                agent_kind_snapshot=agent_kind,
                status=status,
                error=error,
            )
            db.add(record)
            logger.info(f"新增分析记录: {agent_name}/{stock_symbol}/{date_str}")

        db.commit()
        return True

    except Exception as e:
        logger.error(f"保存分析记录失败: {e}")
        db.rollback()
        return False
    finally:
        db.close()


def get_analysis(
    agent_name: str,
    stock_symbol: str,
    analysis_date: date | None = None,
    *,
    user_id: str | None = None,
    role: str | None = None,
) -> AnalysisHistory | None:
    """
    获取分析结果

    Args:
        agent_name: Agent 名称
        stock_symbol: 股票代码
        analysis_date: 分析日期，默认今天
        user_id: 触发用户(缺会话/None → 按 fail-closed 返回 None, 不读 NULL 历史桶)
        role: 用户角色(owner 额外可读 NULL 历史行)

    Returns:
        分析记录，或 None
    """
    if analysis_date is None:
        analysis_date = date.today()

    date_str = analysis_date.strftime("%Y-%m-%d")

    db = SessionLocal()
    try:
        return db.query(AnalysisHistory).filter(
            AnalysisHistory.agent_name == agent_name,
            AnalysisHistory.stock_symbol == stock_symbol,
            AnalysisHistory.analysis_date == date_str,
            analysis_history_scope_clause(user_id, role),
        ).first()
    finally:
        db.close()


def get_latest_analysis(
    agent_name: str,
    stock_symbol: str,
    before_date: date | None = None,
    *,
    user_id: str | None = None,
    role: str | None = None,
) -> AnalysisHistory | None:
    """
    获取最近的分析结果（用于获取昨日/历史分析）

    Args:
        agent_name: Agent 名称
        stock_symbol: 股票代码
        before_date: 在此日期之前的最近记录，默认今天
        user_id: 触发用户; None = 不可读(历史 NULL 行只给 owner)
        role: 用户角色(owner 额外可读 NULL 历史行)

    Returns:
        分析记录，或 None
    """
    if before_date is None:
        before_date = date.today()

    date_str = before_date.strftime("%Y-%m-%d")

    db = SessionLocal()
    try:
        return db.query(AnalysisHistory).filter(
            AnalysisHistory.agent_name == agent_name,
            AnalysisHistory.stock_symbol == stock_symbol,
            AnalysisHistory.analysis_date < date_str,
            analysis_history_scope_clause(user_id, role),
        ).order_by(AnalysisHistory.analysis_date.desc()).first()
    finally:
        db.close()


def get_analysis_history(
    agent_name: str,
    stock_symbol: str | None = None,
    limit: int = 30,
    *,
    user_id: str | None = None,
    role: str | None = None,
) -> list[AnalysisHistory]:
    """
    获取分析历史列表

    Args:
        agent_name: Agent 名称
        stock_symbol: 股票代码，None 表示所有
        limit: 返回数量限制
        user_id: 触发用户; None = 不可读(历史 NULL 行只给 owner)

    Returns:
        分析记录列表，按日期倒序
    """
    db = SessionLocal()
    try:
        query = db.query(AnalysisHistory).filter(
            AnalysisHistory.agent_name == agent_name,
            analysis_history_scope_clause(user_id, role),
        )

        if stock_symbol:
            query = query.filter(AnalysisHistory.stock_symbol == stock_symbol)

        return query.order_by(AnalysisHistory.analysis_date.desc()).limit(limit).all()
    finally:
        db.close()


def get_latest_ta_verdict_row(
    symbol: str,
    within_days: int = 14,
    today: date | None = None,
    *,
    user_id: str | None = None,
    role: str | None = None,
) -> AnalysisHistory | None:
    """获取某标的最近一次 TradingAgents 深度分析记录(含当日)。

    get_latest_analysis 用 ``analysis_date < before_date`` 语义会排除当天,
    这里传 ``before_date = today + 1 天`` 把当天也纳入。

    Args:
        symbol: 股票代码
        within_days: 仅在此天数内有效(超出视为过期,由调用方判定)
        today: 测试可注入,默认 date.today()

    Returns:
        最近的 AnalysisHistory 行,或 None。
    """
    if today is None:
        today = date.today()
    # +1 天以包含今天(get_latest_analysis 是严格小于)
    return get_latest_analysis(
        TA_AGENT_NAME,
        symbol,
        before_date=today + timedelta(days=1),
        user_id=user_id,
        role=role,
    )


def _clean_one_liner(text: str, max_chars: int = 120) -> str:
    """从结论正文里清洗出一句话摘要并截断到 ~max_chars。

    - 去掉 Markdown 标记 / 多余空白 / 控制字符
    - 取首段(到第一个句号/换行)
    - 超长截断并补省略号
    """
    if not text:
        return ""
    s = str(text)
    # 去 markdown 强调符、标题井号、链接残留
    s = re.sub(r"[#*`>\-]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return ""
    # 取首句(中英文句号 / 换行)
    m = re.split(r"[。\.!！\n]", s, maxsplit=1)
    head = (m[0] or s).strip()
    candidate = head if len(head) >= 8 else s
    if len(candidate) > max_chars:
        candidate = candidate[:max_chars].rstrip() + "…"
    return candidate


def get_latest_ta_verdict(
    symbol: str,
    within_days: int = 14,
    today: date | None = None,
    *,
    user_id: str | None = None,
    role: str | None = None,
) -> dict | None:
    """抽取某标的最近一次 TA 深度结论的紧凑版本(供盘前/盘后做高权重先验)。

    只返回 ``{rating, action_label, one_liner, date, age_days}`` —— 绝不返回全文,
    控制 token 预算。任何缺数据 / 解析异常 → None(fail-soft,不抛)。

    2026-10-29 五审: 必须带触发用户; 无用户 → None(不读 NULL 历史桶)。
    """
    if today is None:
        today = date.today()
    try:
        row = get_latest_ta_verdict_row(
            symbol,
            within_days=within_days,
            today=today,
            user_id=user_id,
            role=role,
        )
        if row is None:
            return None

        date_str = str(getattr(row, "analysis_date", "") or "")
        try:
            row_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return None

        age_days = (today - row_date).days
        if age_days < 0 or age_days > max(1, int(within_days)):
            return None

        raw = getattr(row, "raw_data", None) or {}
        if not isinstance(raw, dict):
            return None
        sug = raw.get("suggestion") or {}
        if not isinstance(sug, dict):
            sug = {}

        rating = sug.get("rating_raw") or raw.get("rating") or sug.get("action") or "hold"
        action_label = sug.get("action_label") or ""
        reason = sug.get("reason") or getattr(row, "content", "") or ""
        one_liner = _clean_one_liner(reason)

        return {
            "rating": str(rating),
            "action_label": str(action_label),
            "one_liner": one_liner,
            "date": date_str,
            "age_days": int(age_days),
        }
    except Exception as e:  # 任何意外都 fail-soft
        logger.debug(f"提取 TA 深度结论失败: {symbol} - {e}")
        return None
