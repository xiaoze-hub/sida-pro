"""全市场成交额/主力净流入 —— 通达信 TQ 原生公式(AMO/SUPAMO)汇总，落 market_flow_snapshots。

为什么单开一个采集器（2026-10-20 用户铁律：逆向出的通达信客户端接口一律优先使用）：

- `ΣAMO`  = 两市成交额（元→亿），**由 5580 只个股公式值直接汇总**，不经任何外部 HTTP 网关；
- `ΣSUPAMO` = 主力净流入（可负），通达信口径，与东财「四档大单」不是同一个定义；
- 成本：一次全市场批量 **实测 36.7s / 5580 只**（分片 500，单线程 17709）。
  ⇒ **绝对不能进请求路径**，只能后台定时跑、结果落库，接口读库。

与既有链路的关系：
- 写入 `market_flow_snapshots`（`total_amount` 列见迁移 `_m207`），API 的库读分支直接复用；
- `total_main_flow` 与该表既有字段同义（亿元）；
- 采集失败 → 什么都不写（保持上一行），**绝不写 0 假装有数据**。
"""
from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_LAST_RUN: dict = {"ts": 0.0, "ok": False, "note": None}


def market_totals(*, with_fund: bool = True) -> dict | None:
    """全市场汇总：{total_amount(亿), main_flow(亿), up/down/flat, codes, ...}。

    失败/样本过少 → None（调用方据此保留旧值，不写伪数据）。
    """
    try:
        from src.core import tdx_boards

        codes = list(tdx_boards.name_map().keys())
        if not codes:
            logger.warning("[tq-market-flow] 全A代码表为空 → 跳过")
            return None
        quotes = tdx_boards.board_quotes(codes, with_fund=with_fund)
        if not quotes:
            return None
        amt = 0.0
        fund = 0.0
        got_amt = 0
        got_fund = 0
        up = down = flat = 0
        for q in quotes.values():
            if not isinstance(q, dict):
                continue
            a = q.get("amount")
            if a is not None:
                amt += float(a)
                got_amt += 1
            f = q.get("fund_net")
            if f is not None:
                fund += float(f)
                got_fund += 1
            z = q.get("change_pct")
            if z is not None:
                if z > 0:
                    up += 1
                elif z < 0:
                    down += 1
                else:
                    flat += 1
        # 覆盖率闸门：不足 80% 视为本批失败（半场数据会把成交额算少一半，是撒谎）
        if got_amt < len(codes) * 0.8:
            logger.warning(
                "[tq-market-flow] 成交额覆盖率不足 %d/%d → 丢弃本批", got_amt, len(codes)
            )
            return None
        out = {
            "total_amount": round(amt / 1e8, 1),
            "main_flow": round(fund / 1e8, 2) if got_fund else None,
            "up": up,
            "down": down,
            "flat": flat,
            "codes": len(codes),
            "got_amount": got_amt,
            "got_fund": got_fund,
            "source": "tdx_tq_amo_supamo",
            "caliber": "通达信 AMO/SUPAMO 全市场汇总（元→亿）",
        }
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("[tq-market-flow] 汇总失败: %s", e)
        return None


def write_snapshot(totals: dict) -> bool:
    """把汇总写入 market_flow_snapshots（一次一行）。失败返回 False，不抛。"""
    if not totals:
        return False
    try:
        from sqlalchemy import text

        from src.web.database import engine

        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO market_flow_snapshots
                        (total_main_flow, up_count, down_count, flat_count,
                         sh_flow, sz_flow, total_amount, source)
                    VALUES
                        (:total_main_flow, :up_count, :down_count, :flat_count,
                         NULL, NULL, :total_amount, :source)
                    """
                ),
                {
                    "total_main_flow": totals.get("main_flow"),
                    "up_count": totals.get("up"),
                    "down_count": totals.get("down"),
                    "flat_count": totals.get("flat"),
                    "total_amount": totals.get("total_amount"),
                    "source": "tdx_tq",
                },
            )
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("[tq-market-flow] 写库失败: %s", e)
        return False


def _has_tq_row_today() -> bool:
    """今天是否已有 TQ 口径的快照行（用于收盘后补跑判定）。"""
    try:
        from sqlalchemy import text

        from src.web.database import engine

        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT 1 FROM market_flow_snapshots "
                    "WHERE source = 'tdx_tq' AND ts >= CURRENT_DATE LIMIT 1"
                )
            ).fetchone()
        return bool(row)
    except Exception:  # noqa: BLE001 - 查不到就当没有, 允许补跑
        return False


def run_scheduled() -> dict | None:
    """调度入口：交易时段跑；收盘后(15:05 起)若今日还没有 TQ 快照则补跑一次。

    补跑很重要：发版/重启往往发生在收盘后, 而"今天没有 TQ 行"会让首页主力净流入
    退回东财口径(或 --)。补跑一次即自愈。
    """
    try:
        from src.core.trading_calendar import is_trading_day, is_trading_session

        if not is_trading_day():
            return None
        if is_trading_session():
            return collect_once()
        import datetime as _dt

        t = _dt.datetime.now()
        tail_close = t.hour == 15 and t.minute <= 10     # 14:57-15:00 集合竞价刚结束
        catch_up = (t.hour > 15 or (t.hour == 15 and t.minute > 10)) and not _has_tq_row_today()
        if tail_close or catch_up:
            logger.info("[tq-market-flow] 收盘后补跑（今日无 TQ 快照）")
            return collect_once()
        return None
    except Exception:  # noqa: BLE001 - 日历不可用时宁可跑，也别静默停摆
        logger.debug("[tq-market-flow] 日历不可用, 直接跑一轮", exc_info=True)
        return collect_once()


def collect_once(*, write: bool = True) -> dict | None:
    """跑一次全市场汇总（≈37s）。并发保护：同一时刻只允许一个实例。"""
    t0 = time.time()
    if not _LOCK.acquire(blocking=False):
        logger.info("[tq-market-flow] 上一轮仍在跑 → 跳过本轮")
        return None
    try:
        totals = market_totals()
        ok = False
        if totals and write:
            ok = write_snapshot(totals)
        elif totals:
            ok = True
        _LAST_RUN.update(
            {"ts": time.time(), "ok": bool(ok), "note": totals, "elapsed": round(time.time() - t0, 1)}
        )
        if totals:
            logger.info(
                "[tq-market-flow] 成交额 %.1f亿 / 主力 %s亿 / %d只 / %.1fs",
                totals["total_amount"],
                totals.get("main_flow"),
                totals["codes"],
                time.time() - t0,
            )
        return totals
    finally:
        _LOCK.release()


def last_run() -> dict:
    return dict(_LAST_RUN)
