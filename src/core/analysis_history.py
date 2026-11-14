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
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import false, or_

from src.core.agent_catalog import infer_agent_kind
from src.core.timezone import beijing_now
from src.db.session import SessionLocal
from src.db.models import AnalysisHistory
from src.core.json_safe import to_jsonable

logger = logging.getLogger(__name__)

# TradingAgents 深度分析在 AnalysisHistory 里的 agent_name(见 agent.py: name = "tradingagents")
TA_AGENT_NAME = "tradingagents"


@dataclass
class SaveResult:
    """一次落库请求的真实结果(2026-11-12 P0: 不再用 bool 掩盖三种状态)。

    为什么需要: 原来返回 False 同时代表「报告没存」「报告存了但证据没存」「盘后重跑
    被拒(正文按规则保留)」—— 调用方只能记一句「保存失败」, 等于把真实状态吞掉。
    """

    body_saved: bool = False
    # None = 本次没有要求写凭证; True/False = 真实写入结果
    evidence_saved: bool | None = None
    # True = 本次正文**按规则没有覆盖**(当日已有一份可证明盘前的成功正文)
    content_skipped: bool = False
    reason: str | None = None
    run_trace_id: str | None = None
    # 生成完成时刻(调用方在报告真正生成完成时取的带时区时刻); None = 不可证明
    report_completed_at: str | None = None
    # 保存时刻(落库这一刻); **不是**报告完成时刻, 不能拿它判断盘前
    report_saved_at: str | None = None
    # 这份报告自己的盘前门槛结论: True/False/None(unknown)。非盘前 agent → None
    preopen: bool | None = None
    # 本次请求的正文状态(success/degraded/failed), 供调用方决定要不要写建议池
    body_status: str | None = None
    record_id: int | None = None

    @property
    def ok(self) -> bool:
        """整条链路没有发生「该存的数据丢了」: 正文存了或按规则保留, 证据没写失败。"""
        return (self.body_saved or self.content_skipped) and self.evidence_saved is not False

    def __bool__(self) -> bool:
        # 兼容历史 `if save_analysis(...)` 调用方: 只有正文真的没落库才是 False。
        # 盘后重跑被拒(正文按规则保留)不算失败 —— 但也绝不能被读成「这次正文存进去了」,
        # 需要区分时请用 `body_saved` / `content_skipped`。
        return bool(self.body_saved or self.content_skipped)


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
    *,
    premarket_evidence: dict | None = None,
    report_completed_at: str | None = None,
) -> SaveResult:
    """
    保存分析结果

    - 同一天同一用户**会覆盖**报告行（upsert）—— 这不是不可变存储。
      盘前报告额外传 `premarket_evidence` 时，凭证单独追加到
      `premarket_factor_evidence`（只 INSERT），重跑不会销毁已采样的快照；
      但正文历史版本仍会被覆盖，见该表 docstring 的诚实边界。
    - 盘前保护(2026-11-12 P0 修订): 当天已有「可证明盘前」的**成功**记录时, 盘后重跑
      **不覆盖正文**, 但会:
      ① 把这次采样的凭证照样追加进凭证表(它属于这一次 run, 不会被误当成盘前);
      ② 返回 `SaveResult(content_skipped=True, reason=...)`, 由调用方**显式**记警告/告警
      —— 不再用一句 “保存失败” 把真实状态吞掉。
      若当日旧记录是 degraded/failed(没成功过), 新的成功报告**必须**能覆盖它
      （「成功恢复报告」不能被旧失败记录挡住）。
    - 落库时写入 `raw_data.report_completed_at`（**整份报告生成完成时刻**, 由生成侧在报告
      真正生成完成时取带时区时刻传入)与 `raw_data.report_saved_at`（保存时刻, 两者分开,
      保存慢不能把盘前报告改成盘后、保存早也不能把盘后报告冒充盘前）与
      `raw_data.premarket_report_preopen`（由完成时刻 + 采集结束时刻推导）。
      盘前账本据此判定「这一期是否盘前」, 而不是只看因子采样时点。
      调用方不传完成时刻 → 落 NULL, 门槛按 unknown(不拿保存时刻兜底)。
    - 证据写失败**不再回滚正文**: 正文先提交, 凭证单独一个事务; 凭证失败时
      `SaveResult.evidence_saved=False` + `reason` 显式告知调用方（报告不丢, 也不冒充完整）。
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
        premarket_evidence: 盘前股性因子凭证(生成时真实采样 + 带时区时点)。
            由 `premarket_factor_evidence.build_snapshot_evidence()` 生成;
            传了就追加一条(只 INSERT), 不参与覆盖。
        report_completed_at: 整份报告**生成完成**时刻(带时区 ISO)。生成侧必须在报告真的
            写完那一刻取; 传 None = 时点不可证明(落 NULL, 门槛 unknown)。**绝不**用保存
            时刻兜底 —— 那是另一个事实。

    Returns:
        SaveResult(真值 = 正文已落库或按规则保留; 证据结果见 evidence_saved)
    """
    if analysis_date is None:
        analysis_date = date.today()

    date_str = analysis_date.strftime("%Y-%m-%d")
    is_premarket_agent = agent_name == "premarket_outlook"
    body_status = str(status or "success").strip() or "success"

    from src.core.premarket_factor_evidence import (
        normalize_generated_at,
        preopen_flag,
        report_preopen_state,
    )

    # 生成完成时刻只认调用方传进来的那个带时区时刻; 保存时刻单独记, 二者不互相冒充。
    completed_at = normalize_generated_at(report_completed_at)
    saved_at = normalize_generated_at(beijing_now())
    result = SaveResult(
        run_trace_id=None,
        report_completed_at=completed_at,
        report_saved_at=saved_at,
        body_status=body_status,
    )

    db = SessionLocal()
    try:
        payload = to_jsonable(raw_data or {})
        if not isinstance(payload, dict):
            payload = {}
        result.run_trace_id = str(payload.get("run_trace_id") or "").strip() or None
        if is_premarket_agent:
            # 生成完成时刻: 盘前账本用它判定「整份报告是否在 09:30 前完成」, 不能缺。
            # 缺就是 unknown —— 不拿保存时刻(report_saved_at)兜底冒充生成时刻。
            # 只写盘前报告 —— 别的 agent 的 raw_data 形状不因这次 P0 改动而变化。
            payload["report_completed_at"] = completed_at
            payload["report_saved_at"] = saved_at
            gate = report_preopen_state(
                report_completed_at=completed_at,
                collection_finished_at=payload.get("timestamp"),
                report_day=date_str,
            )
            result.preopen = gate["preopen"]
            # 落库时把同一个门槛的结论一并存下(账本会重算, 两边冲突按 unknown 处理)
            payload["premarket_report_preopen"] = gate["preopen"]
            payload["premarket_gate_reason"] = gate["reason"]
        agent_kind = infer_agent_kind(agent_name)
        record: AnalysisHistory | None = None

        # 查找是否已存在(同用户同日覆盖;不同用户/系统调度各自独立一行)
        existing = db.query(AnalysisHistory).filter(
            AnalysisHistory.agent_name == agent_name,
            AnalysisHistory.stock_symbol == stock_symbol,
            AnalysisHistory.analysis_date == date_str,
            AnalysisHistory.user_id.is_(None)
            if user_id is None
            else AnalysisHistory.user_id == user_id,
        ).first()

        # ── 盘前保护: 只有「当日已有一份可证明盘前的**成功**正文」才保留不被覆盖 ──
        # 2026-11-12(审计补充): 被保护的不只「盘后重跑」——
        #   · 降级/失败正文(事实快报/AI 不可用)即便在 09:30 前生成, 也**不许**顶掉
        #     当日已经成功生成的盘前正文;
        #   · 盘后/时点不可证明的重跑同样不许覆盖。
        # 反过来, 旧记录是 degraded/failed(当天没成功过)时, 新成功报告必须能覆盖它。
        if existing and is_premarket_agent:
            old_raw = existing.raw_data if isinstance(existing.raw_data, dict) else {}
            old_gate = report_preopen_state(
                report_completed_at=old_raw.get("report_completed_at"),
                collection_finished_at=old_raw.get("timestamp"),
                report_day=date_str,
            )
            old_looks_preopen = (
                old_gate["preopen"] is True
                # 旧记录缺 report_completed_at 时退回到「因子采样时点」判定来保护它:
                # 保护一条可能正确的盘前正文, 比让它被盘后重跑覆盖更安全。
                or preopen_flag(old_raw.get("demon_factors_frozen_at"), date_str) is True
            )
            new_gate = report_preopen_state(
                report_completed_at=completed_at,
                collection_finished_at=payload.get("timestamp"),
                report_day=date_str,
            )
            old_status = str(getattr(existing, "status", "") or "success")
            is_downgrade = body_status != "success"
            # 只有「本次同样是成功正文, 且整份报告被证明在 09:30 前完成」才允许替换。
            # unknown 也不行 —— 证明不了的版本没有资格顶掉已经证明过的盘前成功正文。
            if (
                old_looks_preopen
                and old_status == "success"
                and (is_downgrade or new_gate["preopen"] is not True)
            ):
                if is_downgrade:
                    why = (
                        f"本次是 {body_status} 正文(非成功报告), 不能顶掉当日成功生成的盘前报告; "
                        f"本次完成于 {completed_at or '时点不可证明'}"
                    )
                else:
                    why = (
                        f"本次报告完成于 {completed_at or '时点不可证明'}, "
                        f"按 09:30 门槛不是盘前(preopen={new_gate['preopen']})"
                    )
                result.content_skipped = True
                result.record_id = getattr(existing, "id", None)
                result.reason = (
                    f"当日已有一份可证明盘前的成功报告(record_id={getattr(existing, 'id', None)}), "
                    f"本次不覆盖正文: {why}。"
                    "本次采样凭证仍会追加留档(绑定本次 run_trace_id)。"
                )
                logger.warning(
                    "盘前正文未覆盖(按规则保留当日成功盘前版, 已显式告知调用方): "
                    "%s/%s/%s run=%s status=%s",
                    agent_name,
                    stock_symbol,
                    date_str,
                    result.run_trace_id,
                    body_status,
                )
                db.rollback()
                body_saved = False
            else:
                body_saved = True
        else:
            body_saved = True

        if body_saved and existing:
            # 更新（同一天可覆盖, 但保持原 user_id 不变 —— 避免覆盖过程中丢失归属）
            existing.title = title
            existing.content = content
            existing.raw_data = payload
            existing.agent_kind_snapshot = agent_kind
            existing.status = status
            existing.error = error
            logger.info(f"更新分析记录: {agent_name}/{stock_symbol}/{date_str}")
        elif body_saved:
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

        if body_saved:
            db.commit()
            result.body_saved = True
            if existing is not None:
                result.record_id = getattr(existing, "id", None)
            elif record is not None:
                result.record_id = getattr(record, "id", None)

    except Exception as e:
        logger.error(f"保存分析记录失败: {e}")
        db.rollback()
        result.body_saved = False
        result.reason = f"报告正文保存失败: {e}"
        return result
    finally:
        db.close()

    # ── 凭证单独一个事务: 写失败也不丢正文, 但必须显式告知调用方(不冒充完整) ──
    if premarket_evidence is not None:
        from src.core.premarket_factor_evidence import append_evidence

        ev_db = SessionLocal()
        try:
            ok = append_evidence(ev_db, premarket_evidence, user_id=user_id)
            if ok:
                ev_db.commit()
                result.evidence_saved = True
            else:
                ev_db.rollback()
                result.evidence_saved = False
                result.reason = (
                    (result.reason + "；" if result.reason else "")
                    + "盘前因子凭证写入失败(报告正文已保存, 账本将回落到该报告的 raw_data 凭证)"
                )
                logger.error(
                    "盘前因子凭证写入失败但正文已保存: %s/%s/%s run=%s",
                    agent_name,
                    stock_symbol,
                    date_str,
                    result.run_trace_id,
                )
        except Exception as e:  # noqa: BLE001 — 显式失败, 不吞
            try:
                ev_db.rollback()
            except Exception:  # noqa: BLE001
                pass
            result.evidence_saved = False
            result.reason = (
                (result.reason + "；" if result.reason else "")
                + f"盘前因子凭证写入异常: {e}(报告正文已保存)"
            )
            logger.error("盘前因子凭证写入异常但正文已保存: %s", e)
        finally:
            ev_db.close()

    return result


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
