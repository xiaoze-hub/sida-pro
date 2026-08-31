"""数据导出 API(CSV) — 2026-08-15 完整度评估 P1/P2。

三个导出接口, 均返回 text/csv + Content-Disposition attachment;
中文列头带 UTF-8 BOM(Excel 直接打开不乱码); 空数据返回空表头 CSV(不报错)。

挂载(由主模型在 app.py 统一加):
    app.include_router(export.router, prefix="/api", tags=["export"], dependencies=protected)
"""
import csv
import io
import logging

from fastapi import APIRouter, Depends, Response
from sqlalchemy import or_
from sqlalchemy.orm import Session

from src.web.database import get_db
from src.web.api.auth import get_current_user
from src.web.models import (
    AgentPredictionOutcome,
    EntryCandidate,
    Position,
    Stock,
    User,
)
from src.web.api.accounts import (
    _fetch_quotes_for_stocks,
    get_hkd_cny_rate,
    get_usd_cny_rate,
)

logger = logging.getLogger(__name__)
router = APIRouter()

# UTF-8 BOM: Excel 识别中文列头必须
_BOM = "\ufeff"


def _fmt(value, nd: int = 2) -> str:
    """数值格式化: None/非有限数 → 空串, 否则保留 nd 位小数。"""
    if value is None:
        return ""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return ""
    if num != num or num in (float("inf"), float("-inf")):  # NaN / inf
        return ""
    return f"{num:.{nd}f}"


def _csv_safe(value) -> str:
    """CSV 公式注入防护(2026-08-15 评审 B): 以 = + - @ 开头的字段前置单引号,
    防止 Excel/WPS 打开时执行公式。"""
    s = "" if value is None else str(value)
    if s and s[0] in ("=", "+", "-", "@"):
        return "'" + s
    return s


def _csv_response(filename: str, headers: list[str], rows: list[list]) -> Response:
    """构造带 BOM 的 CSV 下载响应(空 rows 也输出表头, 不报错)。"""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    for row in rows:
        writer.writerow([_csv_safe(c) for c in row])
    content = (_BOM + buf.getvalue()).encode("utf-8")
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


# ========== 持仓导出 ==========

@router.get("/export/portfolio")
def export_portfolio(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """当前用户持仓 CSV: 代码/名称/数量/成本价/现价/市值/盈亏(市值/盈亏折人民币)。"""
    # 审计(2026-08-15 评审 B 补覆盖)
    try:
        from src.web.api.audit import log_audit
        log_audit(db, user, "export", detail="导出持仓", ip="")
    except Exception:
        pass
    positions = (
        db.query(Position)
        .filter(or_(Position.user_id == user.id, Position.user_id.is_(None)))
        .order_by(Position.account_id.asc(), Position.sort_order.asc(), Position.id.asc())
        .all()
    )
    headers = ["代码", "名称", "数量", "成本价", "现价", "市值", "盈亏"]

    if not positions:
        return _csv_response("portfolio.csv", headers, [])

    stock_ids = {p.stock_id for p in positions if p.stock_id}
    stocks = (
        db.query(Stock).filter(Stock.id.in_(stock_ids)).all() if stock_ids else []
    )
    stock_map = {s.id: s for s in stocks}
    quotes = _fetch_quotes_for_stocks(stocks) if stocks else {}
    hkd_rate = get_hkd_cny_rate()
    usd_rate = get_usd_cny_rate()

    rows: list[list] = []
    for pos in positions:
        stock = stock_map.get(pos.stock_id)
        if not stock:
            continue
        rate = (
            hkd_rate
            if stock.market == "HK"
            else usd_rate
            if stock.market == "US"
            else 1.0
        )
        quote = quotes.get(stock.symbol)
        current_price = quote.get("current_price") if quote else None

        cost_cny = pos.cost_price * pos.quantity * rate
        if current_price is not None:
            market_value_cny = current_price * pos.quantity * rate
            pnl_cny = market_value_cny - cost_cny
        else:
            market_value_cny = None
            pnl_cny = None

        rows.append([
            stock.symbol,
            stock.name,
            pos.quantity,
            _fmt(pos.cost_price),
            _fmt(current_price),
            _fmt(market_value_cny),
            _fmt(pnl_cny),
        ])
    return _csv_response("portfolio.csv", headers, rows)


# ========== 预测记录导出 ==========

def _prediction_result_label(rec: AgentPredictionOutcome) -> str:
    """结果列: pending(待验证) / hit(命中) / miss(未中) / no_base_price / evaluated。

    DB 原始 outcome_status 为 pending/evaluated/no_base_price;
    evaluated 时按动作方向 × 实际涨跌幅判定 hit/miss(对齐任务口径)。
    """
    status = (rec.outcome_status or "pending").lower()
    if status == "pending":
        return "pending"
    if status == "no_base_price":
        return "no_base_price"
    ret = rec.outcome_return_pct
    if ret is None:
        return "evaluated"
    action = (rec.action or "").lower()
    if action in ("buy", "add"):
        return "hit" if ret > 0 else "miss"
    if action in ("sell", "reduce", "avoid"):
        return "hit" if ret < 0 else "miss"
    return "evaluated"


@router.get("/export/predictions")
def export_predictions(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """预测记录 CSV: 代码/方向/目标价/日期/结果(hit/miss/pending)。"""
    # 审计(2026-08-15 评审 B 补覆盖)
    try:
        from src.web.api.audit import log_audit
        log_audit(db, user, "export", detail="导出预测", ip="")
    except Exception:
        pass
    records = (
        db.query(AgentPredictionOutcome)
        .order_by(
            AgentPredictionOutcome.prediction_date.desc(),
            AgentPredictionOutcome.id.desc(),
        )
        .all()
    )
    headers = ["代码", "方向", "目标价", "日期", "结果"]

    rows: list[list] = []
    for rec in records:
        direction = rec.action_label or rec.action or ""
        rows.append([
            rec.stock_symbol,
            direction,
            _fmt(rec.trigger_price),
            rec.prediction_date,
            _prediction_result_label(rec),
        ])
    return _csv_response("predictions.csv", headers, rows)


# ========== 机会候选导出 ==========

@router.get("/export/opportunities")
def export_opportunities(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """机会候选 CSV(entry_candidates 表): 代码/名称/日期/评分/方向/信号/理由/来源/目标价/止损。"""
    # 审计(2026-08-15 评审 B 补覆盖)
    try:
        from src.web.api.audit import log_audit
        log_audit(db, user, "export", detail="导出机会", ip="")
    except Exception:
        pass
    candidates = (
        db.query(EntryCandidate)
        .order_by(EntryCandidate.snapshot_date.desc(), EntryCandidate.score.desc())
        .all()
    )
    headers = [
        "代码", "名称", "市场", "日期", "状态", "评分", "置信度",
        "方向", "信号", "理由", "来源", "目标价", "止损", "计划质量",
    ]

    market_label = {"CN": "A股", "HK": "港股", "US": "美股"}
    rows: list[list] = []
    for c in candidates:
        rows.append([
            c.stock_symbol,
            c.stock_name,
            market_label.get(c.stock_market, c.stock_market),
            c.snapshot_date,
            c.status,
            _fmt(c.score, 1),
            _fmt(c.confidence),
            c.action_label or c.action or "",
            c.signal or "",
            c.reason or "",
            c.candidate_source or "",
            _fmt(c.target_price),
            _fmt(c.stop_loss),
            c.plan_quality,
        ])
    return _csv_response("opportunities.csv", headers, rows)
