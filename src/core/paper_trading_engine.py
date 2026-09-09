"""模拟盘引擎：自动按策略信号建仓/平仓，跟踪虚拟账户收益。

B5(2026-09-09) 结算分界: 平仓结算/可用现金/账户净值等**金额运算**走
src/core/money.py Decimal; 行情展示与 DB 列(模型 Float)留 float —— 入库值
均为 Decimal 精确结算后量化的结果, 数值迁移另行评估。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.core.marketdata_client import md_quote_rows
from src.core.money import q2, q4, to_dec
from src.models.market import MarketCode, MARKETS
from src.web.database import SessionLocal
from src.web.models import (
    PaperTradingAccount,
    PaperTradingPosition,
    PaperTradingTrade,
    StrategySignalRun,
)
from src.core.backtest.cost_model import CostModel
from src.core.risk_limits import check_entry, load_risk_limits
from src.core.timezone import to_utc

logger = logging.getLogger(__name__)

# 模拟盘交易成本(A股口径,Phase 1)。与回测共用同一成本模型。
COST_MODEL = CostModel()

# 建仓股数下限(A股一手)
FIXED_QUANTITY = 100

# 移动止损:浮盈超过 MIN_PROFIT_FOR_TRAILING 后启用,从持仓最高价回撤超 TRAILING_STOP_PCT 即离场
MIN_PROFIT_FOR_TRAILING = 0.05
TRAILING_STOP_PCT = 0.10
# 时间止损:无 signal.holding_days 时的默认最大持有自然日
DEFAULT_TIME_STOP_DAYS = 20


def _position_weight(rank_score: float) -> float:
    """按信号强度分配单笔资金占该市场预算的比例(rank_score 越高投越多)。"""
    s = float(rank_score or 0.0)
    if s >= 85:
        return 0.25
    if s >= 75:
        return 0.18
    if s >= 65:
        return 0.12
    return 0.08


def _compute_quantity(
    *,
    rank_score: float,
    market_budget: float,
    price: float,
    available_cash: float,
    cost_model: CostModel,
    lot: int = FIXED_QUANTITY,
) -> int:
    """按信号强度 + 市场预算计算建仓股数(lot 整数倍),受可用现金(含买入费)约束。

    返回 0 表示连最小一手都买不起,应跳过。
    """
    if price <= 0:
        return 0
    target_cash = max(0.0, market_budget) * _position_weight(rank_score)
    qty = int((target_cash / price) // lot) * lot
    if qty < lot:
        qty = lot  # 至少一手
    while qty >= lot:
        outlay = -cost_model.fill("buy", price, qty).cash_delta
        if outlay <= available_cash:
            return qty
        qty -= lot
    return 0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _notify_risk_block(account, reason: str, equity: float) -> None:
    """风控拦截开仓时留痕: 日志 + 站内通知(W3/B3.1)。"""
    labels = {
        "drawdown_halt": "回撤熔断",
        "max_positions": "持仓只数上限",
        "no_equity": "账户净值为零",
    }
    label = labels.get(reason, reason)
    mdd = float(getattr(account, "max_drawdown_pct", 0.0) or 0.0)
    logger.warning(
        "[模拟盘风控] 账户 %s 暂停开仓: %s (净值=%.2f, 回撤=%.2f%%)",
        getattr(account, "id", "?"), label, equity, mdd * 100.0,
    )
    try:
        from src.core.notify_center import push_notification

        push_notification(
            title=f"模拟盘风控: {label}",
            body=(
                f"账户回撤 {mdd * 100:.1f}%（净值 {equity:.0f} 元）已触发「{label}」，"
                "本轮起暂停开仓；检查持仓后可在模拟盘页重置，"
                "或调整阈值环境变量 SIDA_RISK_HALT_DRAWDOWN / SIDA_RISK_MAX_POSITIONS。"
            ),
            category="risk",
            level="warning",
            source="paper_trading_risk",
            user_id=getattr(account, "user_id", None),
        )
    except Exception as e:  # 通知失败不得影响主流程
        logger.debug("[模拟盘风控] 熔断通知发送失败: %s", e)


def _to_market(market: str) -> MarketCode:
    try:
        return MarketCode(market)
    except Exception:
        return MarketCode.CN


def _is_trading_time(market: str) -> bool:
    mc = _to_market(market)
    market_def = MARKETS.get(mc)
    if not market_def:
        return False
    return market_def.is_trading_time()


def _safe_float(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 分市场资金配置（投资比例 → 子池现金）
# ---------------------------------------------------------------------------

ALL_MARKETS: tuple[str, ...] = ("CN", "HK", "US")
DEFAULT_ALLOCATIONS: dict[str, float] = {"CN": 0.5, "HK": 0.3, "US": 0.2}


def normalize_allocations(raw: dict | None) -> dict[str, float]:
    """补齐三市场、clamp 到 [0,1]，返回 {market: ratio}。"""
    raw = raw or {}
    out: dict[str, float] = {}
    for m in ALL_MARKETS:
        try:
            v = float(raw.get(m, 0.0) or 0.0)
        except Exception:
            v = 0.0
        out[m] = min(1.0, max(0.0, v))
    return out


def market_allocations_or_default(account: Any) -> dict[str, float]:
    """账户未配置比例时回退默认配置，否则归一化已配置的比例。"""
    raw = getattr(account, "market_allocations", None) or {}
    if not raw:
        return dict(DEFAULT_ALLOCATIONS)
    return normalize_allocations(raw)


def allocations_from_excluded(excluded: list[str] | None) -> dict[str, float]:
    """迁移用：被排除市场比例置 0，其余市场按默认权重归一化到合计 1.0。"""
    excluded_set = {str(m).upper() for m in (excluded or [])}
    weights = {m: DEFAULT_ALLOCATIONS[m] for m in ALL_MARKETS if m not in excluded_set}
    total = sum(weights.values())
    if total <= 0:
        # 全部被排除：兜底投 A 股
        return {"CN": 1.0, "HK": 0.0, "US": 0.0}
    return {m: round(weights.get(m, 0.0) / total, 6) for m in ALL_MARKETS}


def compute_market_cash(
    initial_capital: float, ratio: float, realized_pnl: float, open_cost: float
) -> float:
    """某市场可用现金 = 总资金×比例 + 该市场已实现盈亏 − 该市场持仓成本（纯函数，可单测）。

    B5: Decimal 结算(量化到分); ratio 经 str 往返, 10000×0.3 类 float 漂移被消除。"""
    return float(q2(to_dec(initial_capital) * to_dec(ratio) + to_dec(realized_pnl) - to_dec(open_cost)))


def _user_scope(model: Any, user_id: str | None) -> tuple:
    """按归属用户过滤的查询条件; user_id=None 匹配 NULL 行(冷启动遗留)。"""
    if user_id is None:
        return (model.user_id.is_(None),)
    return (model.user_id == user_id,)


def market_realized_open(db: Session, market: str, user_id: str | None = None) -> tuple[float, float]:
    """返回 (该市场已实现盈亏合计, 该市场未平仓持仓成本合计)。user_id 限定归属用户。

    B5 分界: SQL 侧聚合返回 float(小数列运算在库内), 下游 compute_market_cash
    经 to_dec 进入 Decimal 结算。"""
    realized = (
        db.query(func.coalesce(func.sum(PaperTradingTrade.pnl), 0.0))
        .filter(
            PaperTradingTrade.stock_market == market,
            *_user_scope(PaperTradingTrade, user_id),
        )
        .scalar()
    ) or 0.0
    open_cost = (
        db.query(
            func.coalesce(
                func.sum(PaperTradingPosition.entry_price * PaperTradingPosition.quantity),
                0.0,
            )
        )
        .filter(
            PaperTradingPosition.status == "open",
            PaperTradingPosition.stock_market == market,
            *_user_scope(PaperTradingPosition, user_id),
        )
        .scalar()
    ) or 0.0
    return float(realized), float(open_cost)


def market_available_cash(
    db: Session, account: PaperTradingAccount, market: str, alloc: dict | None = None
) -> float:
    """某市场当前可用现金（用于建仓门槛与展示）。"""
    alloc = alloc or market_allocations_or_default(account)
    ratio = alloc.get(market, 0.0)
    realized, open_cost = market_realized_open(db, market, user_id=account.user_id)
    return compute_market_cash(account.initial_capital, ratio, realized, open_cost)


def _serialize_position(pos: PaperTradingPosition) -> dict:
    """将 ORM Position 提取为 plain dict，避免 detached 问题。"""
    return {
        "id": pos.id,
        "stock_symbol": pos.stock_symbol,
        "stock_market": pos.stock_market,
        "stock_name": pos.stock_name or "",
        "quantity": pos.quantity,
        "entry_price": pos.entry_price,
        "stop_loss": pos.stop_loss,
        "target_price": pos.target_price,
        "current_price": pos.current_price,
        "unrealized_pnl": pos.unrealized_pnl,
        "status": pos.status,
        "strategy_code": pos.strategy_code or "",
    }


def _serialize_trade(trade: PaperTradingTrade) -> dict:
    """将 ORM Trade 提取为 plain dict。"""
    return {
        "id": trade.id,
        "stock_symbol": trade.stock_symbol,
        "stock_market": trade.stock_market,
        "stock_name": trade.stock_name or "",
        "quantity": trade.quantity,
        "entry_price": trade.entry_price,
        "exit_price": trade.exit_price,
        "pnl": trade.pnl,
        "pnl_pct": trade.pnl_pct,
        "exit_reason": trade.exit_reason,
        "holding_days": trade.holding_days,
        "strategy_code": trade.strategy_code or "",
    }


def _serialize_signal(sig: StrategySignalRun) -> dict:
    """将 ORM Signal 提取为 plain dict。"""
    return {
        "id": sig.id,
        "stock_symbol": sig.stock_symbol,
        "stock_market": sig.stock_market,
        "stock_name": sig.stock_name or "",
        "strategy_code": sig.strategy_code or "",
        "rank_score": sig.rank_score,
        "entry_low": sig.entry_low,
        "entry_high": sig.entry_high,
        "action": sig.action,
    }


class PaperTradingEngine:
    """模拟盘扫描引擎(2026-09-08 T6: 多用户隔离, 每用户一份账户/持仓/交易)。"""

    @staticmethod
    def _owner_user_id(db: Session) -> str | None:
        """最早创建的 owner 用户 id(冷启动无 owner 时返回 None)。"""
        from src.web.models import User

        owner = (
            db.query(User)
            .filter(User.role == "owner", User.is_active.is_(True))
            .order_by(User.created_at.asc(), User.id.asc())
            .first()
        )
        if owner:
            return owner.id
        fallback = db.query(User).order_by(User.id.asc()).first()
        return fallback.id if fallback else None

    def _get_or_create_account(self, db: Session, user_id: str | None = None) -> PaperTradingAccount:
        """取(或建)指定用户的模拟盘账户; user_id=None 时归属 owner(调度/系统路径)。"""
        if user_id is None:
            user_id = self._owner_user_id(db)
        query = db.query(PaperTradingAccount)
        if user_id is None:
            query = query.filter(PaperTradingAccount.user_id.is_(None))
        else:
            query = query.filter(PaperTradingAccount.user_id == user_id)
        account = query.first()
        if not account:
            account = PaperTradingAccount(
                user_id=user_id,
                initial_capital=1000000.0,
                current_capital=1000000.0,
                peak_capital=1000000.0,
            )
            db.add(account)
            db.commit()
            db.refresh(account)
        return account

    def _fetch_quotes_map(self, symbols_markets: list[tuple[str, str]]) -> dict[tuple[str, str], dict]:
        """批量获取报价，返回 {(market, symbol): quote_dict}

        通过 QuoteOrchestrator 调度,支持多 provider 主备故障转移。
        """
        grouped: dict[MarketCode, list[str]] = {}
        for symbol, market in symbols_markets:
            mc = _to_market(market)
            grouped.setdefault(mc, []).append(symbol)

        out: dict[tuple[str, str], dict] = {}
        for market, symbols in grouped.items():
            if not symbols:
                continue
            rows = md_quote_rows(symbols, market.value)
            by_symbol = {str(r.get("symbol")): r for r in rows}
            for sym in symbols:
                q = by_symbol.get(sym)
                if q:
                    out[(market.value, sym)] = q
        return out

    def _check_entries(
        self, db: Session, account: PaperTradingAccount,
    ) -> tuple[int, set[tuple[str, str]], list[tuple[PaperTradingPosition, StrategySignalRun | None]]]:
        """检查可入场的策略信号，自动建仓。返回 (建仓数, 新建仓股票key集合, 建仓事件列表)。"""
        # 查询最新活跃买入信号
        query = (
            db.query(StrategySignalRun)
            .filter(
                StrategySignalRun.status == "active",
                StrategySignalRun.action.in_(["buy", "add"]),
                StrategySignalRun.entry_low.isnot(None),
                StrategySignalRun.entry_high.isnot(None),
            )
        )
        # 按投资比例排除不投入（比例为 0）的市场
        alloc = market_allocations_or_default(account)
        excluded = [m for m in ALL_MARKETS if alloc.get(m, 0.0) <= 0]
        if excluded:
            query = query.filter(StrategySignalRun.stock_market.notin_(excluded))
        signals = query.order_by(StrategySignalRun.rank_score.desc()).limit(50).all()
        entry_events: list[tuple[PaperTradingPosition, StrategySignalRun | None]] = []
        new_keys: set[tuple[str, str]] = set()
        if not signals:
            return 0, new_keys, entry_events

        # 已有 open position 的股票
        open_keys = set()
        open_positions = (
            db.query(PaperTradingPosition)
            .filter(PaperTradingPosition.status == "open")
            .all()
        )
        for p in open_positions:
            open_keys.add((p.stock_symbol, p.stock_market))

        # ── W3/B3.1-B3.2 组合级风控闸门 ────────────────────────────────
        limits = load_risk_limits()
        exposure_value = 0.0
        for p in open_positions:
            px = _safe_float(getattr(p, "current_price", None)) or float(p.entry_price or 0.0)
            exposure_value += float(p.quantity or 0) * float(px or 0.0)
        equity = float(account.current_capital or 0.0) + exposure_value
        pos_count = len(open_positions)
        block = check_entry(
            limits=limits,
            account_drawdown_pct=account.max_drawdown_pct,
            equity=equity,
            position_value=exposure_value,
            new_position_value=0.0,
            position_count=pos_count,
        )
        if block in ("no_equity", "drawdown_halt", "max_positions"):
            _notify_risk_block(account, block, equity)
            return 0, new_keys, entry_events

        # 收集需要报价的信号（去重：同股票只取 rank_score 最高的一条）
        candidates = []
        seen = set()
        for sig in signals:
            key = (sig.stock_symbol, sig.stock_market)
            if key in open_keys:
                continue
            if key in seen:
                continue
            seen.add(key)
            candidates.append(sig)

        if not candidates:
            return 0, new_keys, entry_events

        # 批量获取报价
        syms = [(s.stock_symbol, s.stock_market) for s in candidates]
        quotes = self._fetch_quotes_map(syms)

        # 预算各市场可用现金（建仓时按市场子池逐笔扣减）
        market_cash = {m: market_available_cash(db, account, m, alloc) for m in ALL_MARKETS}

        opened = 0
        for sig in candidates:
            key = (sig.stock_market, sig.stock_symbol)
            quote = quotes.get(key)
            if not quote:
                continue
            current_price = _safe_float(quote.get("current_price"))
            if current_price is None or current_price <= 0:
                continue

            # 用当前市价入场
            entry_price = current_price
            mkt = sig.stock_market
            if alloc.get(mkt, 0.0) <= 0:
                continue  # 该市场比例为 0，不投入
            avail = market_cash.get(mkt, 0.0)

            # 仓位管理:按信号强度分配该市场预算(替换原固定 100 股)
            market_budget = account.initial_capital * alloc.get(mkt, 0.0)
            quantity = _compute_quantity(
                rank_score=float(sig.rank_score or 0.0),
                market_budget=market_budget,
                price=entry_price,
                available_cash=avail,
                cost_model=COST_MODEL,
            )
            if quantity <= 0:
                continue  # 子池额度不足以买入最小一手

            # W3/B3.2: 单票上限 + 总敞口上限(按当前净值口径, 不只看市场子池)
            new_value = float(quantity) * float(entry_price)
            gate = check_entry(
                limits=limits,
                account_drawdown_pct=account.max_drawdown_pct,
                equity=equity,
                position_value=exposure_value,
                new_position_value=new_value,
                position_count=pos_count,
            )
            if gate:
                logger.info(
                    "[模拟盘] 风控拦截 %s %s: %s (仓位 %.0f / 净值 %.0f)",
                    sig.stock_symbol, sig.stock_market, gate, new_value, equity,
                )
                continue

            # 含交易成本的实际买入流出(B5: Decimal 结算)
            buy_fill = COST_MODEL.fill("buy", entry_price, quantity)
            buy_outlay = -to_dec(buy_fill.cash_delta)

            # 基于入场价计算止损/止盈
            # 优先用信号的止损/止盈比例，否则用默认 -8%/+15%
            stop_loss = sig.stop_loss
            target_price = sig.target_price
            if stop_loss and sig.entry_low and sig.entry_low > 0:
                # 保留信号的止损比例，映射到实际入场价
                orig_mid = (sig.entry_low + (sig.entry_high or sig.entry_low)) / 2
                if orig_mid > 0:
                    stop_ratio = (stop_loss - orig_mid) / orig_mid
                    target_ratio = ((target_price - orig_mid) / orig_mid) if target_price else 0.15
                    stop_loss = round(entry_price * (1 + stop_ratio), 4)
                    target_price = round(entry_price * (1 + target_ratio), 4) if target_price else None
            # 兜底：止损不合理时用默认 -8%
            if not stop_loss or stop_loss <= 0 or stop_loss >= entry_price:
                stop_loss = round(entry_price * 0.92, 4)
            # 兜底：止盈不合理时用默认 +15%
            if not target_price or target_price <= 0 or target_price <= entry_price:
                target_price = round(entry_price * 1.15, 4)

            pos = PaperTradingPosition(
                stock_symbol=sig.stock_symbol,
                stock_market=sig.stock_market,
                stock_name=sig.stock_name or "",
                quantity=quantity,
                entry_price=entry_price,
                stop_loss=stop_loss,
                target_price=target_price,
                current_price=current_price,
                highest_price=entry_price,
                unrealized_pnl=0.0,
                status="open",
                signal_run_id=sig.id,
                signal_snapshot_date=sig.snapshot_date or "",
                signal_action=sig.action or "",
                strategy_code=sig.strategy_code or "",
            )
            db.add(pos)
            account.current_capital -= float(buy_outlay)
            market_cash[mkt] = float(to_dec(avail) - buy_outlay)
            open_keys.add((sig.stock_symbol, sig.stock_market))
            new_keys.add((sig.stock_symbol, sig.stock_market))
            entry_events.append((pos, sig))
            opened += 1
            # 风控口径随建仓同步累加(同一轮内后续候选按最新敞口判定)
            exposure_value += new_value
            pos_count += 1
            logger.info(
                "[模拟盘] 建仓: %s %s @ %.2f x%d, 止损=%.2f, 止盈=%s, 买入费=%.2f, 策略=%s",
                sig.stock_name or sig.stock_symbol,
                sig.stock_market,
                entry_price,
                quantity,
                stop_loss or 0,
                target_price or "无",
                buy_fill.explicit_fees + buy_fill.slippage_cost,
                sig.strategy_code,
            )

        if opened > 0:
            db.commit()
        return opened, new_keys, entry_events

    def _close_position(
        self,
        db: Session,
        account: PaperTradingAccount,
        pos: PaperTradingPosition,
        exit_price: float,
        exit_reason: str,
    ) -> PaperTradingTrade:
        """平仓单个持仓，返回交易记录。"""
        now = _utc_now()
        # 含交易成本的净盈亏:卖出净回收 − 建仓含费投入(与建仓口径一致,资金守恒)
        # B5: 结算走 Decimal(fill 内部已 Decimal 化, float 值经 str 往返精确还原)
        buy_cost = -to_dec(COST_MODEL.fill("buy", pos.entry_price, pos.quantity).cash_delta)
        sell_fill = COST_MODEL.fill("sell", exit_price, pos.quantity)
        sell_proceeds = to_dec(sell_fill.cash_delta)
        pnl_d = q4(sell_proceeds - buy_cost)
        pnl = float(pnl_d)
        pnl_pct = float(q2(pnl_d / buy_cost * to_dec(100))) if buy_cost > 0 else 0.0

        holding_days = 0
        if pos.opened_at:
            opened = to_utc(pos.opened_at)
            holding_days = max(0, (now - opened).days)

        trade = PaperTradingTrade(
            user_id=getattr(pos, "user_id", None),
            stock_symbol=pos.stock_symbol,
            stock_market=pos.stock_market,
            stock_name=pos.stock_name or "",
            quantity=pos.quantity,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            pnl=pnl,
            pnl_pct=round(pnl_pct, 2),
            exit_reason=exit_reason,
            signal_run_id=pos.signal_run_id,
            signal_snapshot_date=pos.signal_snapshot_date or "",
            strategy_code=pos.strategy_code or "",
            holding_days=holding_days,
            opened_at=pos.opened_at,
            closed_at=now,
        )
        db.add(trade)

        pos.status = "closed"
        pos.closed_at = now
        pos.current_price = exit_price
        pos.unrealized_pnl = pnl

        # 回收资金(卖出净回收,已扣卖出费; DB 列 Float 分界: 值已量化)
        account.current_capital += float(sell_proceeds)
        account.total_pnl += pnl
        account.total_trades += 1
        if pnl > 0:
            account.winning_trades += 1

        logger.info(
            "[模拟盘] 平仓: %s %s @ %.2f, 盈亏=%.2f (%.2f%%), 原因=%s",
            pos.stock_name or pos.stock_symbol,
            pos.stock_market,
            exit_price,
            pnl,
            pnl_pct,
            exit_reason,
        )
        return trade

    def _check_exits(
        self, db: Session, account: PaperTradingAccount, skip_keys: set[tuple[str, str]] | None = None,
    ) -> tuple[int, list[tuple[PaperTradingPosition, PaperTradingTrade]]]:
        """检查持仓止损/止盈/信号反转，自动平仓。skip_keys 中的股票跳过（本轮新建仓）。"""
        exit_events: list[tuple[PaperTradingPosition, PaperTradingTrade]] = []
        positions = (
            db.query(PaperTradingPosition)
            .filter(
                PaperTradingPosition.status == "open",
                *_user_scope(PaperTradingPosition, account.user_id),
            )
            .all()
        )
        if not positions:
            return 0, exit_events

        # 批量获取报价
        syms = [(p.stock_symbol, p.stock_market) for p in positions]
        quotes = self._fetch_quotes_map(syms)

        closed = 0
        for pos in positions:
            # 跳过本轮刚建仓的持仓
            if skip_keys and (pos.stock_symbol, pos.stock_market) in skip_keys:
                continue
            key = (pos.stock_market, pos.stock_symbol)
            quote = quotes.get(key)
            current_price = _safe_float(quote.get("current_price")) if quote else None

            if current_price is None or current_price <= 0:
                continue

            # W3/B3.3: A股 T+1 —— 当日买入当日不可卖(含止损), 与回测内核口径一致
            if pos.opened_at:
                opened_cst = (
                    to_utc(pos.opened_at).astimezone(ZoneInfo("Asia/Shanghai")).date()
                )
                if opened_cst == datetime.now(ZoneInfo("Asia/Shanghai")).date():
                    continue

            # 更新现价、净浮动盈亏(含若此刻平仓的双边成本)、持仓期最高价
            pos.current_price = current_price
            _buy_cost_u = -COST_MODEL.fill("buy", pos.entry_price, pos.quantity).cash_delta
            _sell_u = COST_MODEL.fill("sell", current_price, pos.quantity).cash_delta
            pos.unrealized_pnl = round(_sell_u - _buy_cost_u, 4)
            if pos.highest_price is None or current_price > pos.highest_price:
                pos.highest_price = current_price

            # 检查止损
            if pos.stop_loss and current_price <= pos.stop_loss:
                trade = self._close_position(db, account, pos, current_price, "stop_loss")
                exit_events.append((pos, trade))
                closed += 1
                continue

            # 检查止盈
            if pos.target_price and current_price >= pos.target_price:
                trade = self._close_position(db, account, pos, current_price, "target_price")
                exit_events.append((pos, trade))
                closed += 1
                continue

            # 移动止损:浮盈达标后,从持仓最高价回撤超阈值则离场
            if pos.highest_price and pos.entry_price > 0:
                profit_ratio = (pos.highest_price - pos.entry_price) / pos.entry_price
                if profit_ratio >= MIN_PROFIT_FOR_TRAILING:
                    trail_line = pos.highest_price * (1 - TRAILING_STOP_PCT)
                    if current_price <= trail_line:
                        trade = self._close_position(db, account, pos, current_price, "trailing_stop")
                        exit_events.append((pos, trade))
                        closed += 1
                        continue

            # 检查信号反转
            if pos.signal_run_id:
                # no_autoflush: 信号查询是只读的,不要把本轮累积的持仓现价更新提前 flush——
                # 否则扫描中途会反复抢 SQLite 写锁,与其它调度器并发写时触发 "database is locked"。
                # 所有写入统一在本方法末尾 db.commit() 时一次性落盘。
                with db.no_autoflush:
                    latest = (
                        db.query(StrategySignalRun)
                        .filter(
                            StrategySignalRun.stock_symbol == pos.stock_symbol,
                            StrategySignalRun.stock_market == pos.stock_market,
                            StrategySignalRun.status == "active",
                        )
                        .order_by(StrategySignalRun.created_at.desc())
                        .first()
                    )
                if latest and latest.action in ("sell", "reduce"):
                    trade = self._close_position(db, account, pos, current_price, "signal_reversal")
                    exit_events.append((pos, trade))
                    closed += 1
                    continue

            # 时间止损:持有超过最大自然日离场(优先用 signal 的 holding_days)
            max_days = DEFAULT_TIME_STOP_DAYS
            if pos.signal_run_id:
                sig_hold = (
                    db.query(StrategySignalRun.holding_days)
                    .filter(StrategySignalRun.id == pos.signal_run_id)
                    .scalar()
                )
                if sig_hold and int(sig_hold) > 0:
                    max_days = int(sig_hold)
            if pos.opened_at:
                opened_dt = to_utc(pos.opened_at)
                if (_utc_now() - opened_dt).days >= max_days:
                    trade = self._close_position(db, account, pos, current_price, "time_stop")
                    exit_events.append((pos, trade))
                    closed += 1
                    continue

        self._update_account_metrics(db, account)
        db.commit()
        return closed, exit_events

    def _update_account_metrics(self, db: Session, account: PaperTradingAccount) -> None:
        """更新账户峰值和最大回撤。

        B5: 净值/回撤为金额结算 → Decimal; 入库值量化(账户列 Float 分界)。"""
        # 计算包含浮动盈亏的总资产
        open_positions = (
            db.query(PaperTradingPosition)
            .filter(PaperTradingPosition.status == "open")
            .all()
        )
        unrealized_total = sum(p.unrealized_pnl or 0 for p in open_positions)
        total_equity = to_dec(account.current_capital) + sum(
            to_dec(p.current_price or p.entry_price) * to_dec(p.quantity)
            for p in open_positions
        )
        total_equity_f = float(q2(total_equity))

        if total_equity_f > account.peak_capital:
            account.peak_capital = total_equity_f

        if account.peak_capital > 0:
            drawdown = (to_dec(account.peak_capital) - total_equity) / to_dec(account.peak_capital) * to_dec(100)
            if drawdown > to_dec(account.max_drawdown_pct):
                account.max_drawdown_pct = float(q2(drawdown))

    def _scan_sync(self) -> dict:
        """同步扫描（在线程中执行）。2026-09-08 T6: 遍历所有用户的账户逐一扫描。"""
        db = SessionLocal()
        try:
            # 需要扫描的账户集合: 已存在的全部账户(每用户一行) + owner 兜底账户
            accounts = db.query(PaperTradingAccount).all()
            if not accounts:
                accounts = [self._get_or_create_account(db)]

            total_opened = 0
            total_closed = 0
            entry_events: list[tuple[PaperTradingPosition, StrategySignalRun | None]] = []
            exit_events: list[tuple[PaperTradingPosition, PaperTradingTrade]] = []
            scanned_any = False
            for account in accounts:
                if not account.enabled:
                    continue
                scanned_any = True
                opened, new_keys, acc_entries = self._check_entries(db, account)
                closed, acc_exits = self._check_exits(db, account, skip_keys=new_keys)
                total_opened += opened
                total_closed += closed
                entry_events.extend(acc_entries)
                exit_events.extend(acc_exits)

            if not scanned_any:
                # 无账户或全部账户被停用
                return {"status": "disabled"}

            # 在 db.close() 前将 ORM 对象序列化为 dict，避免 detached 问题
            serialized_entries = [
                {"pos_data": _serialize_position(pos), "sig_data": _serialize_signal(sig) if sig else None}
                for pos, sig in entry_events
            ]
            serialized_exits = [
                {"pos_data": _serialize_position(pos), "trade_data": _serialize_trade(trade)}
                for pos, trade in exit_events
            ]

            return {
                "status": "ok",
                "opened": total_opened,
                "closed": total_closed,
                "entry_events": serialized_entries,
                "exit_events": serialized_exits,
            }
        except Exception as e:
            logger.exception(f"[模拟盘] 扫描异常: {e}")
            return {"status": "error", "error": str(e)}
        finally:
            db.close()

    async def scan_once(self) -> dict:
        """异步扫描入口。"""
        result = await asyncio.to_thread(self._scan_sync)
        # 发送通知（异步，失败不影响交易）
        await self._send_notifications(result)
        return result

    def close_position_manual(self, position_id: int, user_id: str | None = None) -> dict:
        """手动平仓。user_id 非空时校验持仓归属(2026-09-08 T6 防跨账号平仓)。"""
        db = SessionLocal()
        try:
            account = self._get_or_create_account(db, user_id)
            pos = (
                db.query(PaperTradingPosition)
                .filter(
                    PaperTradingPosition.id == position_id,
                    PaperTradingPosition.status == "open",
                    *_user_scope(PaperTradingPosition, account.user_id),
                )
                .first()
            )
            if not pos:
                return {"ok": False, "error": "持仓不存在或已平仓"}

            # 获取最新报价(走 flag 门控的 md_quote_rows,支持故障转移)
            mc = _to_market(pos.stock_market)
            rows = md_quote_rows([pos.stock_symbol], mc.value)

            exit_price = pos.current_price or pos.entry_price
            if rows:
                p = _safe_float(rows[0].get("current_price"))
                if p and p > 0:
                    exit_price = p

            trade = self._close_position(db, account, pos, exit_price, "manual")
            self._update_account_metrics(db, account)
            db.commit()
            # 序列化后返回，避免 db.close() 后 ORM 对象 detached
            return {
                "ok": True,
                "pos_data": _serialize_position(pos),
                "trade_data": _serialize_trade(trade),
            }
        finally:
            db.close()

    async def close_position_manual_async(self, position_id: int) -> dict:
        """异步手动平仓，含通知。"""
        result = await asyncio.to_thread(self.close_position_manual, position_id)
        if result.get("ok"):
            try:
                from src.core.paper_trading_notifier import notify_exit
                pos_data = result.pop("pos_data", None)
                trade_data = result.pop("trade_data", None)
                if pos_data and trade_data:
                    await notify_exit(pos_data, trade_data)
            except Exception:
                logger.exception("[模拟盘] 手动平仓通知失败")
        return result

    async def _send_notifications(self, result: dict) -> None:
        """从扫描结果中取出序列化事件，发送通知。"""
        try:
            from src.core.paper_trading_notifier import notify_entry, notify_exit

            for evt in result.pop("entry_events", []):
                await notify_entry(evt["pos_data"], evt.get("sig_data"))
            for evt in result.pop("exit_events", []):
                await notify_exit(evt["pos_data"], evt["trade_data"])
        except Exception:
            logger.exception("[模拟盘] 通知发送失败")

    def reset_account(self, user_id: str | None = None) -> dict:
        """重置模拟盘（清空该用户所有数据; user_id=None 归属 owner）。"""
        db = SessionLocal()
        try:
            account = self._get_or_create_account(db, user_id)
            scope = _user_scope(PaperTradingPosition, account.user_id)
            db.query(PaperTradingPosition).filter(*scope).delete(synchronize_session=False)
            db.query(PaperTradingTrade).filter(*_user_scope(PaperTradingTrade, account.user_id)).delete(
                synchronize_session=False
            )
            account.current_capital = account.initial_capital
            account.total_pnl = 0.0
            account.total_trades = 0
            account.winning_trades = 0
            account.max_drawdown_pct = 0.0
            account.peak_capital = account.initial_capital
            account.enabled = True
            db.commit()
            return {"ok": True}
        finally:
            db.close()


ENGINE = PaperTradingEngine()
