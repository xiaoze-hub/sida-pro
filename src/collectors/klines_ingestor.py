"""K线后台入库 worker (2026-08-17, 2026-09-08 风险方案1.2/B1 重写)
- 走 marketdata engine 单链取数(腾讯/东财, 均前复权口径), 写入 PG klines hypertable
- 盘后收盘作业跑一次(主), 盘中 5m 跑一次(可选; 注: 5m 路径为历史遗留, 实际写入的是
  日K柱且无读取方, 已弃用未删除)
- 入库用 ON CONFLICT DO UPDATE 幂等 —— 同键重跑覆盖旧值, 除权后前复权基准变化可自愈
  (旧 DO NOTHING 会把 qfq 基准永久冻结在首次写入日, 即 0.7 勘查的 B1 污染机制)
- source = engine 实际胜出 vendor(真源标签)。P2-19 的"单链复写
  tencent/eastmoney/sina 三份假标签"已废 —— 0.7 勘查: 69,154 个 (symbol,date)
  三源逐格相等, source 列毫无区分度
- adjust='qfq' 常驻列(迁移 _m137), 读方按复权维度分区取数, 永不混维度

调用:
- 收盘后跑日K:     python -m src.collectors.klines_ingestor --period 1d --backfill 800
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, text

from src.collectors.kline_collector import KlineData
from src.models.market import MarketCode
from src.db.dialect import DB_URL  # 复用应用 DB 连接

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

# B1.1(2026-09-10): 单日涨跌幅宽容度 —— 留出四舍五入/复权误差, 超过板块限幅×该系数
# 视为可疑跳变(保留但打 quality_flag=0, 由哨兵 B1.2 汇总)。
_JUMP_TOLERANCE = 1.02

# 硬错误(直接拒绝入库)
_HARD_REJECT_REASONS = (
    "unparsable",
    "non_positive_price",
    "ohlc_relation",
    "negative_volume",
)


def validate_bar(
    symbol: str, k: KlineData, prev_close: float | None, *, is_st: bool | None = None
) -> str | None:
    """单根日 K 合法性校验(B1.1/KI-040)。

    返回 None = 通过; "jump_beyond_limit" = 可疑跳变(保留 + quality_flag=0);
    其余为硬错误(拒绝入库): unparsable / non_positive_price / ohlc_relation / negative_volume。

    说明: 前复权序列理论上不应出现超过涨跌停的跳变(除权已被复权抹平), 出现的
    多为 vendor 侧脏柱(见 KI-011 的 10 条缺口), 故标记而非静默接受。
    """
    try:
        o, h, l, c = float(k.open), float(k.high), float(k.low), float(k.close)
        v = float(k.volume or 0)
    except Exception:
        return "unparsable"
    if min(o, h, l, c) <= 0:
        return "non_positive_price"
    if l > min(o, c) + 1e-9 or max(o, c) > h + 1e-9:
        return "ohlc_relation"
    if v < 0:
        return "negative_volume"
    if prev_close and prev_close > 0:
        from src.core.limit_rules import limit_ratio

        ratio = limit_ratio(symbol, is_st)
        if ratio is not None and abs(c / prev_close - 1.0) > ratio * _JUMP_TOLERANCE:
            return "jump_beyond_limit"
    return None


def _to_db_row(symbol: str, market: str, period: str, source: str, k: KlineData, ts,
               adjust: str = "qfq", quality_flag: int = 1) -> dict:
    """KlineData → klines 表字典。"""
    return {
        "ts": ts,
        "symbol": symbol,
        "market": market,
        "period": period,
        "source": source,
        "adjust": adjust,
        "open": float(k.open),
        "high": float(k.high),
        "low": float(k.low),
        "close": float(k.close),
        "volume": int(k.volume or 0),
        # B1.3: vendor 提供成交额时入库; 缺失留 NULL(诚实缺失, 不伪造)
        "amount": (float(k.amount) if getattr(k, "amount", None) is not None else None),
        "quality_flag": int(quality_flag),
    }


def _fetch_klines_with_vendor(symbol: str, market: MarketCode, days: int) -> tuple[list, str]:
    """marketdata engine 单链取数, 返回 (bars, 实际胜出 vendor)。

    必须直连 engine, 不得走 KlineCollector.get_klines —— 那是 PG 优先的读取
    路径, 入库 worker 走它会把 PG 旧数据再抄回 PG, qfq 基准永远刷不新。
    """
    from src.core.marketdata_client import get_market_data

    need = (max(10, min(days, 30)) if market == MarketCode.US
            else (max(120, int(days * 0.6)) if market in (MarketCode.CN, MarketCode.HK) else 1))
    want = min(max(days, 3000), 20000) if market in (MarketCode.CN, MarketCode.HK) else days
    return get_market_data().klines_with_vendor(
        symbol, market=market.value, days=want, min_count=need)


async def ingest_symbol(
    db_engine,
    symbol: str,
    market: MarketCode,
    period: str,
    days: int,
) -> dict:
    """拉 1 只股的 K线, 单链单标签入库。返回入库统计。

    2026-09-08 风险方案1.2/B1: klines_with_vendor() 返回真实胜出 vendor,
    只写一份、source=真源(vendor 为空时诚实标 'unknown', 不编造)。
    adjust='qfq'; ON CONFLICT DO UPDATE 使重跑自愈。
    """
    try:
        klines, vendor = await asyncio.to_thread(_fetch_klines_with_vendor, symbol, market, days)
    except Exception as e:
        return {"symbol": symbol, "market": market.value, "period": period,
                "ingested": 0, "by_source": {},
                "fail_details": [{"source": "mixed", "error": f"{type(e).__name__}: {e}"}]}

    source = vendor or "unknown"
    # 2026-08-23 修复(M-11): 收集失败明细, 便于上游聚合日志。
    fail_details: list[dict] = []
    rows: list[dict] = []
    hard_rejected = 0
    flagged = 0
    if not isinstance(klines, list) or not klines:
        fail_details.append({"source": source, "error": "empty/no klines"})
    else:
        # B1.1: 升序遍历, prev_close 用于跳变校验
        prev_close: float | None = None
        for k in sorted(klines, key=lambda x: str(getattr(x, "date", ""))):
            reason = validate_bar(symbol, k, prev_close, is_st=None)
            if reason in _HARD_REJECT_REASONS:
                hard_rejected += 1
                fail_details.append(
                    {"source": source, "error": f"reject:{reason}@{getattr(k, 'date', '')}"}
                )
                continue
            # KlineData.date 是 'YYYY-MM-DD'(CST 交易日) → 当天 00:00 CST 转 UTC
            # P2-19: 旧代码 .replace(tzinfo=utc) 把北京时间午夜标成 UTC 午夜, 差 8h
            try:
                ts = datetime.fromisoformat(str(k.date)).replace(tzinfo=ZoneInfo("Asia/Shanghai"))
            except Exception:
                try:
                    ts = datetime.fromisoformat(str(k.date)).replace(tzinfo=timezone.utc)
                except Exception:
                    ts = datetime.now(timezone.utc)
            rows.append(
                _to_db_row(
                    symbol, market.value, period, source, k, ts, adjust="qfq",
                    quality_flag=0 if reason == "jump_beyond_limit" else 1,
                )
            )
            if reason == "jump_beyond_limit":
                flagged += 1
            prev_close = float(k.close)

    # 入库: DO UPDATE 同键覆盖(自愈复权基准), 一次 1 行确保 ON CONFLICT 走对路径
    total = 0
    if rows:
        with db_engine.begin() as conn:
            # 单一 source 不变量: 该股 qfq 分区只保留本轮胜出 vendor 的行。
            # 否则 vendor 切换日(如腾讯风控回落东财)新旧两源并存 → 同日双柱,
            # 正是 0.7 勘查的污染形态之一。
            conn.execute(
                text(
                    "DELETE FROM klines WHERE symbol=:s AND market=:m AND period=:p "
                    "AND adjust='qfq' AND source <> :src"
                ),
                {"s": symbol, "m": market.value, "p": period, "src": source},
            )
            for row in rows:
                result = conn.execute(
                    text(
                        "INSERT INTO klines (ts, symbol, market, period, source, adjust, "
                        "open, high, low, close, volume, amount, quality_flag) "
                        "VALUES (:ts, :symbol, :market, :period, :source, :adjust, "
                        ":open, :high, :low, :close, :volume, :amount, :quality_flag) "
                        "ON CONFLICT (symbol, market, period, ts, source, adjust) "
                        "DO UPDATE SET open=EXCLUDED.open, high=EXCLUDED.high, "
                        "low=EXCLUDED.low, close=EXCLUDED.close, "
                        "volume=EXCLUDED.volume, amount=EXCLUDED.amount, "
                        "quality_flag=EXCLUDED.quality_flag"
                    ),
                    row,
                )
                total += result.rowcount
    return {
        "symbol": symbol,
        "market": market.value,
        "period": period,
        "ingested": total,
        "by_source": {source: len(rows)} if rows else {},
        "fail_details": fail_details,
        "rejected": hard_rejected,
        "flagged": flagged,
    }


async def ingest_batch(
    db_engine,
    symbols: list[tuple[str, str]],
    period: str = "1d",
    days: int = 800,
    concurrency: int = 5,
) -> dict:
    """批量入库,限制并发避免打爆数据源。"""
    sem = asyncio.Semaphore(concurrency)

    async def one(sym: str, mkt_str: str):
        async with sem:
            try:
                mkt = MarketCode(mkt_str.upper())
            except ValueError:
                return None
            return await ingest_symbol(db_engine, sym, mkt, period, days)

    tasks = [one(s, m) for s, m in symbols]
    # 2026-08-23 修复(M-11): 三源失败的 symbol 之前静默 warn 即丢失,
    # 改为聚合统计 + ERROR 级, 让人/值班系统能第一时间发现数据源异常。
    results = await asyncio.gather(*tasks, return_exceptions=True)

    total_ingested = 0
    success_symbols: list[str] = []
    fail_symbols: list[dict] = []
    for r in results:
        if isinstance(r, Exception):
            # gather 自身抛了任务创建异常
            fail_symbols.append({"symbol": "?", "reason": f"{type(r).__name__}: {r}"})
            continue
        if not r:
            continue
        total_ingested += r["ingested"]
        fd = r.get("fail_details") or []
        if not r["ingested"] and fd:
            fail_symbols.append({
                "symbol": r["symbol"],
                "market": r.get("market", ""),
                "period": r.get("period", period),
                "reasons": fd,
            })
        else:
            success_symbols.append(r["symbol"])
        logger.info(
            f"  {r['symbol']}.{period}: "
            f"ingested={r['ingested']}, "
            f"by_source={r['by_source']}"
        )

    # 2026-08-23 修复(M-11): 整体聚合日志。三源全空就升级到 ERROR(无人值守时易漏掉)。
    summary = {
        "total_symbols": len(symbols),
        "success": len(success_symbols),
        "fail": len(fail_symbols),
        "total_ingested": total_ingested,
    }
    if fail_symbols:
        logger.error(
            "klines_ingestor 聚合: %s | 失败明细样本(前5): %s",
            summary,
            fail_symbols[:5],
        )
    else:
        logger.info("klines_ingestor 聚合: %s | 全部成功", summary)
    return {"total_ingested": total_ingested, **summary, "fail_symbols": fail_symbols}


def _today_cst() -> str:
    """返回 Asia/Shanghai 时区的"今天" YYYY-MM-DD。

    2026-08-24: 候选池 snapshot_date 是 CST 日期串, 不能用 datetime.now() 拿 host 本地时间,
    否则 server 在 UTC 会跨日拉错/漏拉。统一走 Asia/Shanghai。
    """
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")


def get_default_symbols() -> list[tuple[str, str]]:
    """从 users.stocks + entry_candidates 当日快照合并去重返回 (symbol, market)。

    2026-08-17 修复: 多用户场景下(每用户各自加自选), 同一 (symbol, market) 会出现多行。
    K线是全局数据(主键不含 user_id), 入库会去重 — 但 get_default_symbols 拉取前
    应该先去重, 避免每天 18:00 cron 重复拉同一股 14 次(网络浪费)。

    2026-08-24 修复(K线覆盖候选池, 方案2):
    候选池当日新进的票(多源共振计分入池的),不在 watchlist 中 → K线 cron 不会拉它们,
    导致机会页 K线只显示当天 1 根。这里把 entry_candidates 当日 DISTINCT (symbol, market)
    并入拉取列表,与 watchlist 按 (symbol, market) 去重,一并补 800 天历史。
    market 缺省 CN(对齐 EntryCandidate.stock_market 列 default)。
    """
    from src.db.session import SessionLocal
    from src.db.models import EntryCandidate, Stock

    today = _today_cst()

    with SessionLocal() as db:
        # 1) watchlist 自选股(可能跨用户重复)
        watch_rows = db.query(Stock.symbol, Stock.market).all()
        # 2) 候选池当日 distinct 标的(snapshot_date = CST today)
        cand_rows = (
            db.query(EntryCandidate.stock_symbol, EntryCandidate.stock_market)
            .filter(EntryCandidate.snapshot_date == today)
            .distinct()
            .all()
        )

        seen: set[tuple[str, str]] = set()
        result: list[tuple[str, str]] = []

        def _add(sym: str, mkt: str) -> None:
            if not sym:
                return
            m = (mkt or "CN").value if hasattr(mkt, "value") else (mkt or "CN")
            # 兼容 enum / 空串 / None: 一律规范成大写字符串
            m = str(m).upper() or "CN"
            key = (sym, m)
            if key in seen:
                return
            seen.add(key)
            result.append(key)

        # 先 watchlist(用户明确关注的优先),再候选池当日新增
        for r in watch_rows:
            _add(r.symbol, r.market)
        for r in cand_rows:
            _add(r.stock_symbol, r.stock_market)

        return result


async def main_async(args):
    db_engine = create_engine(DB_URL, pool_pre_ping=True, pool_size=5, max_overflow=10)
    symbols = get_default_symbols()
    logger.info(f"开始入库: {len(symbols)} 只股, period={args.period}, days={args.days}")

    start = time.time()
    result = await ingest_batch(db_engine, symbols, period=args.period, days=args.days)
    elapsed = time.time() - start

    fail_n = result.get("fail", 0)
    logger.info(
        f"\n✅ 完成: {result['total_ingested']} 行入库 / {elapsed:.1f}s / "
        f"{result['total_ingested']/elapsed:.0f} 行/秒 | "
        f"成功 {result.get('success',0)} 失败 {fail_n}/{result.get('total_symbols',0)}"
    )
    if fail_n:
        logger.error(
            "klines_ingestor 主任务检测到失败, 请检查数据源链路 / 网络 / 鉴权"
        )
    db_engine.dispose()


def main():
    parser = argparse.ArgumentParser(description="K线后台入库 worker")
    parser.add_argument("--period", default="1d", help="周期: 1d / 5m / 1m")
    parser.add_argument("--days", type=int, default=800, help="拉几天(默认 800 ≈ 2.2 年)")
    parser.add_argument("--intraday", action="store_true", help="盘中模式:5m K")
    args = parser.parse_args()

    if args.intraday:
        args.period = "5m"
        args.days = 2  # 盘中只拉最近 2 天(避免过大)

    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()