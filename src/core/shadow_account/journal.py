"""交割单分析: FIFO 配对 + 行为画像(移植自 HKUDS/Vibe-Trading, MIT)。

提供:
- pair_trades_fifo: 每股按 FIFO 配对买卖,计算每回合 PnL。
- compute_profile: 持仓天数/胜率/盈亏比/回撤/频率等画像。
- compute_behavior: 处置效应/过度交易/追涨/锚定 4 项行为诊断。
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


def pair_trades_fifo(df: pd.DataFrame) -> list[dict[str, Any]]:
    """按 FIFO 配对买入/卖出,计算每回合 PnL。

    Args:
        df: 标准化 DataFrame(datetime 已解析排序)。

    Returns:
        dict 列表: symbol, buy_dt, sell_dt, qty, buy_price, sell_price,
        hold_days, pnl, pnl_pct。未匹配仓位忽略。
    """
    queues: dict[str, deque] = defaultdict(deque)
    roundtrips: list[dict[str, Any]] = []

    for row in df.itertuples(index=False):
        if row.side == "buy":
            queues[row.symbol].append({
                "dt": row.datetime,
                "qty": row.quantity,
                "price": row.price,
                "fee": row.fee,
            })
            continue

        # sell: 匹配最早的买入
        remaining = row.quantity
        q = queues[row.symbol]
        while remaining > 1e-9 and q:
            lot = q[0]
            take = min(lot["qty"], remaining)
            sell_dt = pd.to_datetime(row.datetime)
            buy_dt = pd.to_datetime(lot["dt"])
            hold = (sell_dt - buy_dt).total_seconds() / 86400.0 if pd.notna(sell_dt) and pd.notna(buy_dt) else 0.0
            gross = (row.price - lot["price"]) * take
            # 按比例分摊费用
            buy_fee = lot["fee"] * (take / lot["qty"]) if lot["qty"] else 0.0
            sell_fee = row.fee * (take / row.quantity) if row.quantity else 0.0
            pnl = gross - buy_fee - sell_fee
            cost = lot["price"] * take
            pnl_pct = pnl / cost if cost else 0.0
            roundtrips.append({
                "symbol": row.symbol,
                "buy_dt": lot["dt"],
                "sell_dt": row.datetime,
                "qty": take,
                "buy_price": lot["price"],
                "sell_price": row.price,
                "hold_days": round(hold, 2),
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl_pct, 4),
            })
            lot["fee"] -= buy_fee
            lot["qty"] -= take
            remaining -= take
            if lot["qty"] <= 1e-9:
                q.popleft()
    return roundtrips


def _safe_div(a: float, b: float) -> float:
    return float(a) / float(b) if b else 0.0


def compute_profile(df: pd.DataFrame) -> dict[str, Any]:
    """构建交易画像 dict。

    Args:
        df: 标准化 DataFrame(datetime 解析、排序)。

    Returns:
        avg_holding_days / trade_frequency_per_week / win_rate /
        profit_loss_ratio / total_pnl / max_drawdown / top_symbols /
        market_distribution / hourly_distribution / roundtrips_sample。
    """
    if df.empty:
        return {"error": "empty trade journal"}

    rts = pair_trades_fifo(df)
    rts_df = pd.DataFrame(rts)

    total_trades = len(df)
    span_days = max(1, (df["datetime"].max() - df["datetime"].min()).days)
    freq_per_week = round(total_trades / span_days * 7, 2)

    if not rts_df.empty:
        wins = rts_df[rts_df["pnl"] > 0]
        losses = rts_df[rts_df["pnl"] < 0]
        avg_win = wins["pnl"].mean() if len(wins) else 0.0
        avg_loss = losses["pnl"].mean() if len(losses) else 0.0
        win_rate = round(len(wins) / len(rts_df), 4)
        pnl_ratio = round(_safe_div(avg_win, abs(avg_loss)), 2) if avg_loss else float("inf") if avg_win else 0.0
        avg_hold = round(rts_df["hold_days"].mean(), 2)
        total_pnl = round(rts_df["pnl"].sum(), 2)
        # 累计 PnL → 最大回撤
        cum = rts_df.sort_values("sell_dt")["pnl"].cumsum()
        running_max = cum.cummax()
        drawdown = (cum - running_max).min()
        max_drawdown = round(float(drawdown), 2) if pd.notna(drawdown) else 0.0
    else:
        win_rate = pnl_ratio = avg_hold = total_pnl = max_drawdown = 0.0

    top_symbols = (
        df.groupby("symbol")
        .agg(trades=("symbol", "count"), total_amount=("amount", "sum"))
        .sort_values("total_amount", ascending=False)
        .head(10)
        .round(2)
        .reset_index()
        .to_dict(orient="records")
    )

    market_dist = df["market"].value_counts().to_dict()
    hourly_dist = df["datetime"].dt.hour.value_counts().sort_index().to_dict()
    hourly_dist = {int(h): int(c) for h, c in hourly_dist.items()}

    sample = rts_df.head(5).copy()
    if not sample.empty:
        sample["buy_dt"] = sample["buy_dt"].astype(str)
        sample["sell_dt"] = sample["sell_dt"].astype(str)
        roundtrips_sample = sample.to_dict(orient="records")
    else:
        roundtrips_sample = []

    return {
        "total_trades": total_trades,
        "total_roundtrips": len(rts_df),
        "avg_holding_days": avg_hold,
        "trade_frequency_per_week": freq_per_week,
        "win_rate": win_rate,
        "profit_loss_ratio": pnl_ratio,
        "total_pnl": total_pnl,
        "max_drawdown": max_drawdown,
        "top_symbols": top_symbols,
        "market_distribution": market_dist,
        "hourly_distribution": hourly_dist,
        "roundtrips_sample": roundtrips_sample,
    }


def _severity(score: float, thresholds: tuple[float, float]) -> str:
    """数值分数 → low/medium/high,给定 (med_cutoff, high_cutoff)。"""
    med, high = thresholds
    if score >= high:
        return "high"
    if score >= med:
        return "medium"
    return "low"


def _disposition_effect(rts_df: pd.DataFrame) -> dict[str, Any]:
    """处置效应: 亏单持有比赢单更久。

    Metric = avg_loss_hold / avg_win_hold。比值 > 1 表示用户持有亏损头寸
    比盈利头寸更久 —— 经典处置偏差。
    """
    if rts_df.empty:
        return {"severity": "low", "evidence": "no closed roundtrips"}
    wins = rts_df[rts_df["pnl"] > 0]
    losses = rts_df[rts_df["pnl"] < 0]
    if wins.empty or losses.empty:
        return {
            "severity": "low",
            "evidence": "not enough winners and losers to compare holding times",
        }
    win_hold = float(wins["hold_days"].mean())
    loss_hold = float(losses["hold_days"].mean())
    ratio = loss_hold / win_hold if win_hold > 0 else float("inf")
    severity = _severity(ratio, (1.2, 1.5))
    return {
        "severity": severity,
        "ratio_loss_to_win_hold": round(ratio, 2),
        "avg_winner_hold_days": round(win_hold, 2),
        "avg_loser_hold_days": round(loss_hold, 2),
        "evidence": (
            f"亏损回合平均持有 {loss_hold:.1f} 天 vs 盈利回合 {win_hold:.1f} 天 "
            f"(比值 {ratio:.2f})。"
            + ("典型处置效应: 拿不住盈利、扛得住亏损。" if severity == "high"
               else "轻度'拿亏单更久'倾向。" if severity == "medium"
               else "持有时长大致对称。")
        ),
    }


def _overtrading(df: pd.DataFrame, rts_df: pd.DataFrame) -> dict[str, Any]:
    """过度交易: 高活跃日的 PnL 更差。

    按交易笔数把交易日分上四分位(忙日)和下四分位(闲日),比较卖出落在
    各桶的回合 PnL。
    """
    if df.empty or rts_df.empty:
        return {"severity": "low", "evidence": "insufficient data"}

    daily_trades = df.groupby(df["datetime"].dt.date).size()
    if len(daily_trades) < 4:
        return {"severity": "low", "evidence": "fewer than 4 trading days"}

    busy_cut = daily_trades.quantile(0.75)
    quiet_cut = daily_trades.quantile(0.25)
    busy_days = set(daily_trades[daily_trades >= busy_cut].index)
    quiet_days = set(daily_trades[daily_trades <= quiet_cut].index)

    rts_df = rts_df.copy()
    rts_df["sell_date"] = pd.to_datetime(rts_df["sell_dt"]).dt.date
    busy_pnl = rts_df[rts_df["sell_date"].isin(busy_days)]["pnl"]
    quiet_pnl = rts_df[rts_df["sell_date"].isin(quiet_days)]["pnl"]
    if busy_pnl.empty or quiet_pnl.empty:
        return {"severity": "low", "evidence": "roundtrips not spread across busy/quiet days"}

    busy_avg = float(busy_pnl.mean())
    quiet_avg = float(quiet_pnl.mean())

    gap = quiet_avg - busy_avg
    base = abs(quiet_avg) if quiet_avg != 0 else 1.0
    severity = _severity(gap / base, (0.3, 1.0)) if busy_avg < quiet_avg else "low"

    return {
        "severity": severity,
        "busy_day_avg_pnl": round(busy_avg, 2),
        "quiet_day_avg_pnl": round(quiet_avg, 2),
        "busy_day_trade_threshold": round(float(busy_cut), 1),
        "evidence": (
            f"忙日(≥{busy_cut:.0f} 笔)平均 PnL {busy_avg:+.0f};"
            f"闲日(≤{quiet_cut:.0f} 笔)平均 PnL {quiet_avg:+.0f}。"
            + ("高活跃明显拖累收益。" if severity == "high"
               else "忙日交易有些拖累。" if severity == "medium"
               else "活跃度对 PnL 无明显伤害。")
        ),
    }


def _chasing_momentum(df: pd.DataFrame) -> dict[str, Any]:
    """追涨: 买入集中在同标的近期上涨之后。

    对每笔 BUY,看用户该标的前 3 笔交易;若价格上行(第 3 笔前价格
    上涨 > 3%),计为追涨。
    """
    buys = df[df["side"] == "buy"].sort_values(["symbol", "datetime"]).copy()
    if buys.empty:
        return {"severity": "low", "evidence": "no buys"}

    buys["prev3_price"] = buys.groupby("symbol")["price"].shift(3)
    matured = buys.dropna(subset=["prev3_price"])
    if matured.empty:
        return {
            "severity": "low",
            "evidence": "not enough repeat buys per symbol to evaluate chasing",
        }
    chased = matured[matured["price"] > matured["prev3_price"] * 1.03]
    ratio = len(chased) / len(matured)
    severity = _severity(ratio, (0.4, 0.6))
    return {
        "severity": severity,
        "chase_ratio": round(ratio, 3),
        "buys_evaluated": int(len(matured)),
        "evidence": (
            f"{len(chased)}/{len(matured)} 笔买入({ratio:.0%})发生在同标的"
            "近 3 笔价格涨幅 >3% 之后。"
            + ("明显追涨模式。" if severity == "high"
               else "有一定追涨倾向。" if severity == "medium"
               else "无明确追涨偏差。")
        ),
    }


def _anchoring(df: pd.DataFrame) -> dict[str, Any]:
    """锚定: 重复交易聚集在窄价格带。

    对每个 ≥5 笔交易的标的,计算 σ(price)/mean(price)。低比值(<0.05)
    表示用户反复在相同价格区交易,疑似锚定参考价而非对波动反应。
    """
    grouped = df.groupby("symbol")
    rows: list[dict[str, Any]] = []
    for sym, sub in grouped:
        if len(sub) < 5:
            continue
        mean = float(sub["price"].mean())
        std = float(sub["price"].std())
        if mean == 0:
            continue
        cv = std / mean
        rows.append({"symbol": sym, "trades": len(sub), "mean_price": round(mean, 2), "cv": round(cv, 4)})

    if not rows:
        return {"severity": "low", "evidence": "no symbol has ≥5 trades to evaluate anchoring"}

    anchored = [r for r in rows if r["cv"] < 0.05]
    ratio = len(anchored) / len(rows)
    severity = _severity(ratio, (0.33, 0.66))
    return {
        "severity": severity,
        "anchored_symbol_ratio": round(ratio, 3),
        "symbols_evaluated": len(rows),
        "anchored_symbols": anchored[:5],
        "evidence": (
            f"{len(anchored)}/{len(rows)} 个频繁交易标的集中在窄价格带(CV<5%)。"
            + ("锚定明显 —— 反复在相同价位交易。" if severity == "high"
               else "部分标的出现锚定。" if severity == "medium"
               else "重复交易价格自然波动。")
        ),
    }


def compute_behavior(df: pd.DataFrame) -> dict[str, Any]:
    """运行全部 4 项行为诊断。

    Returns:
        disposition_effect / overtrading / chasing_momentum /
        anchoring 各 {severity, evidence, ...metrics}。
    """
    if df.empty:
        return {"error": "empty trade journal"}
    rts_df = pd.DataFrame(pair_trades_fifo(df))
    return {
        "disposition_effect": _disposition_effect(rts_df),
        "overtrading": _overtrading(df, rts_df),
        "chasing_momentum": _chasing_momentum(df),
        "anchoring": _anchoring(df),
    }
