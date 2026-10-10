"""决策日志 API(B6, 2026-09-18) —— "信号 → 结果"的账, 只读。

两个端点:
- `GET /api/decisions/stats` —— 按信号类型统计 T+1/3/5 命中率;
  **样本不足 (`n < min_sample`) 时不给命中率**, 返回 `insufficient=true`(页面显示"样本不足")。
- `GET /api/decisions/log`    —— 最近的信号明细(含当时价格/上下文快照与回填状态)。

口径: 缺价格 / 未回填一律 NULL(不补 0), 平盘记未命中 —— 本端点只透传, 不在 API 层做任何推算。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query

from src.web.api.auth import get_current_user
from src.web.models import User

logger = logging.getLogger(__name__)

router = APIRouter(tags=["decisions"])


@router.get("/stats")
def decisions_stats(
    days: int = Query(180, ge=7, le=730, description="回看天数(按信号产生日)"),
    min_sample: int = Query(30, ge=1, le=500, description="低于此样本量不给命中率"),
    _: User = Depends(get_current_user),
):
    """按信号类型统计命中率(样本不足则如实说"样本不足", 不拿小样本算百分比)。"""
    from src.core.decision_log import stats
    from src.db.session import get_read_engine

    return stats(get_read_engine(), days=days, min_sample=min_sample)


@router.post("/backfill")
def decisions_backfill(
    limit: int = Query(500, ge=1, le=5000, description="单次最多回填的待处理信号数"),
    _: User = Depends(get_current_user),
):
    """手动触发决策账本回填(T+1/3/5 收益与命中), 作业框架单飞, 返回 job_id。

    P0-1(2026-10-10) 运维口子: 定时在交易日 18:35, 这里供补跑/追单。同类活跃作业
    已存在时复用其 job_id(`started=false`), 不并发起第二个。真实回填在后台线程执行,
    进度/结果落作业框架(app_jobs), 可从 `/api/jobs` 查看。
    """
    from src.core.decision_log import spawn_backfill

    return spawn_backfill(limit=limit, reason="manual")


@router.get("/log")
def decisions_log(
    kind: str | None = Query(None, description="按信号类型过滤, 如 resonance3"),
    limit: int = Query(100, ge=1, le=500, description="单页条数(后端再钳到 [1,500])"),
    offset: int = Query(0, ge=0, description="分页偏移(越界只返回空页, 不报错)"),
    start_date: str | None = Query(None, description="起始日 YYYY-MM-DD 或 YYYYMMDD(含)"),
    end_date: str | None = Query(None, description="结束日 YYYY-MM-DD 或 YYYYMMDD(含)"),
    _: User = Depends(get_current_user),
):
    """信号明细(分页): 当时价格 + 上下文快照 + T+1/3/5 回填状态(未回填=None)。

    审计 P2-4: 原端点只有 kind+limit, 无法翻页/按日期收窄 —— 账本上万行只能看头 100 条。
    本端点补齐 `offset`/`start_date`/`end_date`, 并回 `total`/`has_more` 供前端翻页。
    """
    from src.core.decision_log import query_log
    from src.db.session import get_read_engine

    return query_log(
        get_read_engine(),
        kind=kind,
        limit=limit,
        offset=offset,
        start_date=start_date,
        end_date=end_date,
    )


@router.get("/backtest")
def decisions_backtest(
    symbols: str = Query("", description="逗号分隔的股票池(6 位代码), 如 002361,600519"),
    start_date: str | None = Query(None, description="信号日区间起(含)"),
    end_date: str | None = Query(None, description="信号日区间止(含)"),
    hold_days: int = Query(5, ge=1, le=30, description="持有/观察窗口(交易日)"),
    success_pct: float = Query(3.0, ge=0.1, le=50, description="收益超过该百分比算成功(官方 3%)"),
    fund_source: str | None = Query(
        None, description="资金口径: 空=双指标(诚实, 缺资金维) / ohlc=OHLC 暗盘对照项"
    ),
    activity_line: float = Query(3.0, ge=0, le=20, description="活跃度门槛(3 强势线 / 6 大牛线)"),
    max_symbols: int = Query(50, ge=1, le=200, description="单次回测股票池上限(逐日重算 O(n²))"),
    _: User = Depends(get_current_user),
):
    """三指标共振回测(P2-1): 暴露 `backtest_resonance` —— 原能力全仓零调用, 后端有术无入口。

    ⚠️ 诚实口径: 明盘历史无源, 默认 `fund_source=空` 走**双指标**(缺资金维), **结果带 `basis`
    标记透传前端**; 与官方"三指标四态"不可直接比较。缺符号/计算失败一律显式降级(不 500)。
    """
    from src.core.decision_backtest import backtest_resonance

    sym_list = [s.strip() for s in (symbols or "").split(",") if s.strip()][:max_symbols]
    params = {
        "symbols": sym_list,
        "start_date": start_date,
        "end_date": end_date,
        "hold_days": hold_days,
        "success_pct": success_pct,
        "fund_source": fund_source,
        "activity_line": activity_line,
    }
    if not sym_list:
        return {
            "available": False,
            "error": "未提供股票池(symbols 为空) —— 无样本可回测",
            "params": params,
            "basis": "双指标(缺资金维)" if fund_source is None else "三指标(资金=OHLC对照项)",
            "sample": {"symbols": 0, "signals": 0},
            "note": "需至少一个 6 位股票代码; 逐日滚动重算, 建议池 ≤ 50 只、区间 ≤ 2 年。",
        }
    try:
        result = backtest_resonance(
            symbols=sym_list,
            start_date=start_date,
            end_date=end_date,
            hold_days=hold_days,
            success_pct=success_pct,
            fund_source=fund_source,
            activity_line=activity_line,
        )
        result["available"] = True
        return result
    except Exception as e:  # noqa: BLE001
        logger.warning("共振回测失败: %s", e)
        return {
            "available": False,
            "error": f"回测计算失败: {e}",
            "params": params,
            "basis": "双指标(缺资金维)" if fund_source is None else "三指标(资金=OHLC对照项)",
            "sample": {"symbols": 0, "signals": 0},
            "note": "回测取 K 线或指标计算失败, 结果不可用(未推算、未填充)。",
        }


@router.get("/entry-outcomes")
def decisions_entry_outcomes(
    days: int = Query(30, ge=1, le=365, description="回看天数(按后验记录创建时间)"),
    min_sample: int = Query(20, ge=1, le=500, description="低于此样本量不给胜率"),
    _: User = Depends(get_current_user),
):
    """入场候选后验(P2-3): 按 horizon×来源给胜率/均收益; 样本不足显式 `insufficient`。

    暴露 `evaluate_entry_candidate_outcomes` 落库结果的**只读查询**(原仅 cron 写入无查询口)。
    """
    from src.core.entry_candidates import entry_outcomes_summary

    try:
        return entry_outcomes_summary(days=days, min_sample=min_sample)
    except Exception as e:  # noqa: BLE001
        logger.warning("入场后验查询失败: %s", e)
        return {
            "window_days": days,
            "min_sample": min_sample,
            "available": False,
            "error": f"入场后验查询失败: {e}",
            "rows": [],
            "note": "查询失败, 不推算; 请稍后重试或检查数据库。",
        }


@router.get("/thresholds")
def decisions_thresholds(
    _: User = Depends(get_current_user),
):
    """决策阈值只读快照(数智决策 P1-3): 当前生效值 + 来源(default/env)。

    阈值统一配置层见 `src.core.thresholds`(默认 1.56/3.00/6.00, 支持 env
    `SIDA_THRESHOLD_*` 覆盖)。**只读** —— 运行时改阈值需要权限设计, 本端点不提供写入口。
    非法/缺失配置在配置层回默认 + warn(不 crash), 故此处永远返回可用的生效值。
    """
    from src.core import thresholds

    items = [
        {
            "key": key,
            "label": spec["label"],
            "value": spec["value"],
            "source": spec["source"],
            "default": spec["default"],
            "env": spec["env"],
        }
        for key, spec in thresholds.snapshot().items()
    ]
    return {
        "items": items,
        "note": "只读快照; 来源 default=内置默认, env=环境变量覆盖。",
    }
