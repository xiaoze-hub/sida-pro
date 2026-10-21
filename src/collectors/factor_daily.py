"""个股因子日快照（2026-10-21）。

为什么单独建表：`strategy_factor_snapshots` 是**信号级**（只有被策略选中的股票才有），
诊断时需要的是"个股 × 日期"的因子值 + 横截面分位。

口径纪律（宁可缺，不许编）：
- 只用能精确算出的因子：动量、波动、量能 z、区间位置 —— 全部来自 PG 前复权日线；
- 换手率缺流通股本历史、主力净额缺逐笔历史 → **暂不写入**，留 NULL（后续按日累积再补）；
- 横截面分位按**同一交易日全市场**排名，绝不用未来数据。
"""
from __future__ import annotations

import logging
import math
from datetime import datetime

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

FACTORS = ("momentum_20", "momentum_60", "vol_20", "volume_z20", "position_120",
           "concentration")


def compute_factors(bars: list[dict]) -> dict[str, float | None]:
    """bars 升序。样本不足的因子返回 None（不写 0 冒充）。"""
    closes = [float(b["close"]) for b in bars if b.get("close")]
    vols = [float(b.get("volume") or 0) for b in bars]
    highs = [float(b["high"]) for b in bars if b.get("high")]
    lows = [float(b["low"]) for b in bars if b.get("low")]
    out: dict[str, float | None] = {k: None for k in FACTORS}

    if len(closes) > 20 and closes[-21]:
        out["momentum_20"] = round((closes[-1] / closes[-21] - 1) * 100, 2)
    if len(closes) > 60 and closes[-61]:
        out["momentum_60"] = round((closes[-1] / closes[-61] - 1) * 100, 2)

    if len(closes) > 21:
        rets = [(closes[i] / closes[i - 1] - 1) for i in range(len(closes) - 20, len(closes))]
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / max(len(rets) - 1, 1)
        out["vol_20"] = round(math.sqrt(var) * math.sqrt(252) * 100, 2)  # 年化 %

    if len(vols) >= 60:
        win = vols[-60:]
        mu = sum(win) / len(win)
        sd = math.sqrt(sum((v - mu) ** 2 for v in win) / max(len(win) - 1, 1))
        if sd > 0:
            out["volume_z20"] = round((vols[-1] - mu) / sd, 2)

    if highs and lows and len(highs) >= 60:
        hi, lo = max(highs[-120:]), min(lows[-120:])
        if hi > lo:
            out["position_120"] = round((closes[-1] - lo) / (hi - lo) * 100, 1)

    # 第 6 个因子：CYQ 集中度 + 筹码峰（用同一段日线现算，口径与 get_chip_accumulation 一致）
    out["peak_price"] = None
    if len(bars) >= 60:
        try:
            from src.core.chip_distribution import compute_chips

            cyq = compute_chips(bars[-120:]) or {}
            if cyq.get("concentration") is not None:
                out["concentration"] = round(float(cyq["concentration"]), 4)
            if cyq.get("peak_price") is not None:
                out["peak_price"] = round(float(cyq["peak_price"]), 2)
        except Exception:  # noqa: BLE001 - 筹码算不出不影响其它因子
            out["concentration"] = None
    return out


def _pct_rank(values: list[float]) -> list[float]:
    """横截面分位（0~100）。"""
    if not values:
        return []
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    n = len(values)
    for pos, idx in enumerate(order):
        ranks[idx] = round(pos / max(n - 1, 1) * 100, 1)
    return ranks


def collect_once(db: Session, limit: int | None = None, trade_date: str | None = None,
                 asof: str | None = None) -> dict:
    """asof='YYYYMMDD' 时按该日截断日线重算（历史回填用）。"""
    """跑一轮全市场因子快照。返回统计（写入行数/跳过数）。"""
    from src.db.klines_repo import load_qfq_bars
    from src.db.models import StockFactorDaily
    from sqlalchemy import text

    day = trade_date or datetime.now().strftime("%Y%m%d")
    try:
        with db.get_bind().connect() as conn:
            rows = conn.execute(text(
                "SELECT DISTINCT symbol FROM klines WHERE period='1d' AND adjust='qfq' "
                "AND market='CN' AND ts >= now() - interval '90 days'"
            )).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning("因子快照取股票池失败: %s", exc)
        return {"written": 0, "skipped": 0, "reason": "pool_failed"}

    symbols = [str(r[0]) for r in rows if r[0]]
    if limit:
        symbols = symbols[:limit]

    records: list[dict] = []
    skipped = 0
    for sym in symbols:
        try:
            bars = load_qfq_bars(sym, "CN", days=200)
        except Exception:  # noqa: BLE001
            skipped += 1
            continue
        use_bars = bars
        if asof:
            use_bars = [b for b in bars if str(b.get("date", "")).replace("-", "") <= asof]
            if not use_bars:
                skipped += 1
                continue
        f = compute_factors(use_bars)
        if f["momentum_20"] is None:  # 样本不足整行不写，避免半截数据
            skipped += 1
            continue
        records.append({"symbol": sym, "trade_date": day, **f})
    if not records:
        return {"written": 0, "skipped": skipped, "reason": "no_samples"}

    for key, col in (("momentum_20", "rank_momentum_20"), ("momentum_60", "rank_momentum_60"),
                     ("vol_20", "rank_vol_20"), ("volume_z20", "rank_volume_z20"),
                     ("position_120", "rank_position_120")):
        vals = [r[key] for r in records if r.get(key) is not None]
        if len(vals) < 20:
            continue
        # 分位只在有值的样本内排名，缺值的保持 None
        ranked = _pct_rank(vals)
        it = iter(ranked)
        for r in records:
            if r.get(key) is not None:
                r[col] = next(it)

    written = 0
    for r in records:
        try:
            obj = db.query(StockFactorDaily).filter_by(symbol=r["symbol"], trade_date=day).first()
            if obj is None:
                db.add(StockFactorDaily(**r))
            else:
                for k, v in r.items():
                    setattr(obj, k, v)
            written += 1
        except Exception as exc:  # noqa: BLE001 - 单只失败不该毁整批
            logger.info("因子写入失败 %s: %s", r["symbol"], exc)
    db.commit()
    logger.info("因子快照完成 day=%s written=%d skipped=%d", day, written, skipped)
    return {"written": written, "skipped": skipped}
