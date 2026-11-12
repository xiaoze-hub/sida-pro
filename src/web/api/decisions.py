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


@router.get("/watchlist-signal-quality")
def decisions_watchlist_signal_quality(
    days: int = Query(180, ge=7, le=730, description="回看天数(按信号产生日)"),
    min_sample: int = Query(30, ge=1, le=500, description="低于此样本量不给命中率"),
    user: User = Depends(get_current_user),
):
    """用户自选池专属信号质量报告(2026-10-11 胜率证据层 ②): 纯账本聚合, 零预测。

    **按 user_id 隔离**(多用户铁律): 只在**当前登录用户自选池标的**的决策账本行上聚合
    (signal_kind × symbol), 给出 T+1/3/5 实测命中率 + 样本量 + regime 分桶。
    样本 < min_sample 一律 `insufficient=true`(不给数字)。**只陈述实测, 不给建议**。
    无自选/无法隔离 → `available=false` + 原因(不退化到全局池、不编造)。
    """
    from src.core.signal_quality import watchlist_signal_quality
    from src.db.session import get_read_engine

    try:
        return watchlist_signal_quality(
            get_read_engine(), user_id=user.id, days=days, min_sample=min_sample
        )
    except Exception as e:  # noqa: BLE001 —— 永不 500, 显式失败
        logger.warning("自选池信号质量查询失败: %s", e)
        return {
            "available": False,
            "user_id": getattr(user, "id", ""),
            "days": days,
            "min_sample": min_sample,
            "symbols_count": 0,
            "signals": [],
            "error": f"查询失败: {e}",
            "note": "查询失败, 不推算; 请稍后重试。",
        }


@router.get("/premarket")
def decisions_premarket(
    days: int = Query(5, ge=1, le=20, description="回看最近 N 期盘前分析"),
    max_rows: int = Query(30, ge=1, le=200, description="潜伏表最多返回行数"),
    user: User = Depends(get_current_user),
):
    """盘前分析账：最近 N 期「事件驱动前瞻（双轨制 · 潜伏表）」的标的 + 真实信号 + 事后表现。

    为什么在决策账本里：盘前分析给出的潜伏标的此前只在报告正文里，事后无从对账。
    本端点把那张表解析成结构化行，挂上**真实**信号（股性因子、收盘价位置/连板）与
    事后表现（当日 / T+1 / T+3，收盘价口径），让「盘前说了什么、后来怎样」可查。

    诚实边界：
    - 表格由 LLM 撰写，**解析不到就 `parsed=false` 并返回空行**，不凭空补标的；
    - 信号取不到 → 该项 `null`（前端显示「无数据」）；
    - 事后表现只用已有日线收盘价；该交易日无 K 线（停牌/休市）→ `null`，不推算；
    - 「止损是否触及」只在止损列写了**具体价位**时才算，且是**收盘价**口径（盘中路径没有数据）。

    时点（PIT）边界 —— 盘前报告写于报告日 D 开盘前：
    - 「位置/连板」只用 **trade_date < D** 的收盘（`as_of=D` 的 `<=` 语义会把 D 日当天收盘
      当成盘前信号，那是收盘后才知道的事），并用 `signals.price.close_date` 标出真实收盘日；
    - 「股性因子」只在**快照日 < D** 时展示（`demon_factors` 是每股一行的当前快照，
      表里没有历史版本；D 日当天/之后重算的行含 D 日封板事件，盘前不可得）→ 否则 `null`
      并在 `signals.demon_note` 写明原因，**不伪造历史快照、不拿今天的表冒充当时**；
    - 「事后表现」（当日/T+1/T+3）本身是 post-hoc 字段，与上面的盘前信号严格分开。
    """
    from src.core import premarket_ambush as pa
    from src.core.analysis_history import analysis_history_scope_clause
    from src.core.timezone import beijing_now
    from src.db.models import AnalysisHistory
    from src.db.session import SessionLocal, get_read_engine

    try:
        db = SessionLocal()
    except Exception as e:  # noqa: BLE001
        return {"available": False, "error": f"数据库不可用: {e}", "items": []}
    try:
        q = (
            db.query(AnalysisHistory)
            .filter(
                AnalysisHistory.agent_name == "premarket_outlook",
                AnalysisHistory.status == "success",
                analysis_history_scope_clause(user.id, getattr(user, "role", None)),
            )
            .order_by(AnalysisHistory.analysis_date.desc(), AnalysisHistory.created_at.desc())
        )
        # 同一交易日可能有多条（重跑），只保留最新一条
        by_date: dict[str, AnalysisHistory] = {}
        for rec in q.limit(200).all():
            d = str(rec.analysis_date)
            if d not in by_date:
                by_date[d] = rec
            if len(by_date) >= days:
                break
        records = [by_date[d] for d in sorted(by_date, reverse=True)]
    except Exception as e:  # noqa: BLE001
        logger.warning("盘前账查询失败: %s", e)
        return {"available": False, "error": f"查询失败: {e}", "items": []}
    finally:
        db.close()

    read_engine = get_read_engine()
    close_cache: dict[str, list] = {}
    items: list[dict] = []
    for rec in records:
        date = str(rec.analysis_date)
        rows = pa.parse_ambush_table(rec.content or "")[:max_rows]
        codes = [r["code"] for r in rows]
        signals = pa.collect_signals(
            codes,
            engine=read_engine,
            start_day=_shift_day(date, -25),
            report_day=date,
        )
        out_rows: list[dict] = []
        for r in rows:
            code = r["code"]
            sig = signals.get(code) or {}
            detail = pa.build_signal_detail(r, demon=sig.get("demon"),
                                            price=sig.get("price"),
                                            demon_note=sig.get("demon_note"))
            seq = close_cache.get(code)
            if seq is None:
                from src.core.decision_log import _close_series_pg

                try:
                    seq = _close_series_pg(read_engine, code, _shift_day(date, -25))
                except Exception:  # noqa: BLE001
                    seq = []
                close_cache[code] = seq
            detail["outcome"] = _outcome_for(seq, date, detail.get("stop_price"))
            out_rows.append(detail)

        scored = [o for o in out_rows if o["outcome"]["t0"] is not None]
        t1 = [o for o in out_rows if o["outcome"]["t1"] is not None]
        price_dates = sorted(
            {
                d
                for d in (
                    ((o.get("signals") or {}).get("price") or {}).get("close_date")
                    for o in out_rows
                )
                if d
            }
        )
        demon_n = sum(1 for o in out_rows if (o.get("signals") or {}).get("demon"))
        items.append(
            {
                "date": date,
                "record_id": getattr(rec, "id", None),
                "title": rec.title or "",
                "quality": "ok" if (rec.content or "").strip() else "suspect",
                "parsed": bool(out_rows),
                "row_count": len(out_rows),
                "rows": out_rows,
                # 盘前时点证据: 信号用的到底是哪一天的收盘 / 因子快照日上限
                "pit": {
                    "price_before": date,
                    "price_close_dates": sorted(set(price_dates)),
                    "demon_before": date,
                    "demon_available": demon_n,
                    "demon_unavailable": len(out_rows) - demon_n,
                },
                "summary": {
                    "rows": len(out_rows),
                    "t0_scored": len(scored),
                    "t0_up": sum(1 for o in scored if (o["outcome"]["t0"] or 0) > 0),
                    "t1_scored": len(t1),
                    "t1_up": sum(1 for o in t1 if (o["outcome"]["t1"] or 0) > 0),
                },
            }
        )

    return {
        "available": bool(items),
        "days": days,
        "as_of": beijing_now().isoformat(),
        "items": items,
        "note": (
            "潜伏表由 AI 撰写，本页只解析并用真实行情对账；解析不到表格即 parsed=false，不补标的。"
            "信号按盘前时点取数：位置/连板只用报告日之前的收盘（close_date 标出真实收盘日），"
            "股性因子只在快照日早于报告日时展示，否则留空并注明「盘前不可得」。"
            "事后表现（当日/T+1/T+3）按收盘价计算，未使用盘中最低/最高，与盘前信号分开看。"
        ),
    }


def _shift_day(day: str, delta: int) -> str:
    from datetime import date as _date, timedelta as _td

    try:
        d = _date.fromisoformat(str(day)[:10])
    except ValueError:
        return str(day)
    return (d + _td(days=delta)).isoformat()


def _outcome_for(seq: list[tuple[str, float]], date: str, stop_price: float | None) -> dict:
    """当日 / T+1 / T+3（相对收盘），以及收盘价是否跌破止损。缺数据一律 None。"""
    out = {"t0": None, "t1": None, "t3": None, "stop_breached": None, "note": None}
    if not seq:
        out["note"] = "无日线数据"
        return out
    days = [d for d, _ in seq]
    if date not in days:
        out["note"] = "该交易日无K线(休市/停牌)"
        return out
    i = days.index(date)
    closes = [c for _, c in seq]
    if i >= 1 and closes[i - 1]:
        out["t0"] = round((closes[i] / closes[i - 1] - 1.0) * 100.0, 2)
    if i + 1 < len(closes) and closes[i]:
        out["t1"] = round((closes[i + 1] / closes[i] - 1.0) * 100.0, 2)
    if i + 3 < len(closes) and closes[i]:
        out["t3"] = round((closes[i + 3] / closes[i] - 1.0) * 100.0, 2)
    if stop_price is None:
        out["note"] = "止损为条件式(非价位)，未判定"
    else:
        window = closes[i : i + 4]
        out["stop_breached"] = any(c < stop_price for c in window) if window else None
        out["note"] = "止损按收盘价判定"
    return out


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
