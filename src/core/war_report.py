"""主力资金战报(规格 §4.4『主力资金战报』, 2026-10-10)。

汇总**当日主力动向**:
  - 全市场大单净流入 **TOP / BOTTOM**(thsdk DDE 批量, `dark_fund_scan.scan_dde_universe`);
  - **个股主力净额变化**(`dde_minute_flow` 采样序列的最新区间增量; 无采样 → None, 显式缺失);
  - **行业分布**(TQ SUPAMO 板块主力资金, `tdx_boards.sector_items` + `board_quotes`);
  - **拆单/对倒计数**(委托号级 .tck 拆单簇; 对倒需账户信息 → 无源显式不可识别)。

诚实口径(硬约束, AGENTS): 任一子块缺源 → 显式 `available=False` + `note`, 绝不编造/回 0;
口径标签随 `caliber=ths` / `direction_semantics` 一并给出(**禁用于主力意图方向性判定**)。
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from src.core.caliber import DIRECTION_THS

logger = logging.getLogger(__name__)

_CST = ZoneInfo("Asia/Shanghai")

DEFAULT_TOP_N = 20


def _now_cst(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(_CST)
    if now.tzinfo is None:
        return now.replace(tzinfo=_CST)
    return now.astimezone(_CST)


def _change_map(db, trade_date: str, market: str, symbols: list[str]) -> dict[str, dict]:
    """{symbol: {net_change_wan, samples, last_sample_ts}} —— 来自 dde_minute_flow 采样序列。

    无采样/无表 → 空 dict(调用方显式 None, 不编造)。
    """
    from sqlalchemy import text

    out: dict[str, dict] = {}
    if not symbols:
        return out
    try:
        placeholders = ", ".join(f":s{i}" for i in range(len(symbols)))
        params = {f"s{i}": s for i, s in enumerate(symbols)}
        params.update({"d": trade_date, "m": market})
        rows = db.execute(
            text(
                "SELECT symbol, sample_ts, delta_net_wan FROM dde_minute_flow "
                f"WHERE trade_date = :d AND market = :m AND symbol IN ({placeholders}) "
                "ORDER BY sample_ts"
            ),
            params,
        ).fetchall()
    except Exception as e:  # noqa: BLE001
        logger.debug("war_report 读 DDE 采样失败: %s", e)
        return out
    for r in rows:
        sym = str(r[0])
        rec = out.setdefault(sym, {"net_change_wan": None, "samples": 0, "last_sample_ts": None})
        rec["samples"] += 1
        rec["last_sample_ts"] = str(r[1])
        if r[2] is not None:
            rec["net_change_wan"] = round(float(r[2]), 2)
    return out


def _enrich(rows: list[dict], changes: dict[str, dict]) -> list[dict]:
    out: list[dict] = []
    for r in rows:
        c = changes.get(str(r.get("symbol")))
        out.append(
            {
                "symbol": r.get("symbol"),
                "name": r.get("name"),
                "main_net_wan": r.get("main_net_wan"),
                # 个股主力净额变化: 采样区间增量(万元); 无采样 → None(显式缺失)
                "net_change_wan": c.get("net_change_wan") if c else None,
                "samples": c.get("samples") if c else 0,
                "last_sample_ts": c.get("last_sample_ts") if c else None,
                "source": r.get("source"),
                "caliber": r.get("caliber"),
            }
        )
    return out


def industry_distribution(top_n: int = DEFAULT_TOP_N) -> dict:
    """行业分布(TQ SUPAMO 板块主力资金, 亿元)。缺源/全空 → available=False + note。"""
    try:
        from src.core.tdx_boards import board_quotes, sector_items

        items = sector_items()
        inds = [it for it in items
                if it.get("board_type") == "industry" and it.get("code")]
        if not inds:
            return {"available": False, "note": "无行业板块目录(TQ 通达信不可用)", "rows": []}
        quotes = board_quotes([it["code"] for it in inds], with_fund=True) or {}
        rows: list[dict] = []
        for it in inds:
            f = (quotes.get(it["code"]) or {}).get("fund_net")
            if f is None:
                continue
            rows.append({
                "name": it.get("name") or "",
                "code": it.get("code"),
                "fund_net_yi": round(float(f) / 1e8, 3),  # 元 → 亿元
            })
        if not rows:
            return {"available": False, "note": "行业主力资金(SUPAMO)全空(非交易时段/客户端未更新)",
                    "rows": []}
        rows.sort(key=lambda x: -x["fund_net_yi"])
        return {
            "available": True,
            "note": "TQ SUPAMO 板块主力资金(元→亿元)",
            "rows": rows,
            "top": rows[:top_n],
            "bottom": list(reversed(rows[-top_n:])),
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("行业资金(SUPAMO)取数失败: %s", e)
        return {"available": False, "note": f"行业资金(SUPAMO)源不可用: {e}", "rows": []}


def split_wash_counts(symbols: list[str]) -> dict:
    """拆单/对倒计数。

    - 拆单簇: 委托号级 `postmarket_review.dark_review_from_tck`(仅覆盖有 .tck 的标的);
    - 对倒(自买自卖): **需账户信息, .tck 无账户字段 → 不可识别**(恒 None, 显式无源)。
    无 .tck 源 → available=False + note。
    """
    from src.core.postmarket_review import dark_review_from_tck

    covered: list[str] = []
    split_clusters = 0
    for s in symbols:
        try:
            rev = dark_review_from_tck(s)
        except Exception as e:  # noqa: BLE001
            logger.debug("war_report .tck 复盘失败 %s: %s", s, e)
            continue
        if rev.get("available"):
            covered.append(s)
            split_clusters += len(rev.get("clusters") or [])
    if not covered:
        return {
            "available": False,
            "split_count": None,
            "wash_count": None,
            "covered": [],
            "note": "无委托号级 .tck 数据源 → 拆单/对倒计数不可得(显式无数据)",
        }
    return {
        "available": True,
        "split_count": split_clusters,
        "wash_count": None,
        "covered": covered,
        "note": "拆单簇来自 .tck 委托号级(仅覆盖有 .tck 的标的); "
                "对倒(自买自卖)需账户信息, .tck 无账户字段 → 不可识别",
    }


def build_daily_war_report(
    *,
    market: str = "CN",
    top_n: int = DEFAULT_TOP_N,
    l2=None,
    db=None,
    now: datetime | None = None,
) -> dict:
    """构建当日主力资金战报。数据源不可用 → available=False + note(不编造)。"""
    n = _now_cst(now)
    trade_date = n.strftime("%Y-%m-%d")
    base = {
        "market": market,
        "generated_at": n.isoformat(timespec="seconds"),
        "snapshot_date": trade_date,
        "caliber": "ths",
        "direction_semantics": DIRECTION_THS,
        "sources": ["thsdk_dde", "tq_supamo", "tck_dark"],
    }
    if market.upper() != "CN":
        return {**base, "available": False,
                "note": f"主力资金战报仅支持 CN 市场(当前 {market})"}

    from src.core.dark_fund_scan import scan_dde_universe

    try:
        uni = scan_dde_universe(l2=l2)
    except Exception as e:  # noqa: BLE001
        logger.warning("战报全市场 DDE 扫描失败: %s", e)
        return {**base, "available": False, "note": f"DDE 全市场扫描失败: {e}"}

    rows = list(uni.get("rows") or [])
    if not rows:
        return {**base, "available": False,
                "note": "DDE 全市场扫描无数据(数据源不可用或非交易时段)"}

    rows.sort(key=lambda x: -(x.get("main_net_wan") or 0.0))
    top_syms = [str(r.get("symbol")) for r in rows[:top_n]]
    bottom_syms = [str(r.get("symbol")) for r in rows[-top_n:]]

    owns_db = db is None
    if owns_db:
        from src.db.session import SessionLocal

        db = SessionLocal()
    try:
        changes = _change_map(db, trade_date, market.upper(), top_syms + bottom_syms)
    finally:
        if owns_db:
            db.close()

    return {
        **base,
        "available": True,
        "universe": uni.get("universe", 0),
        "computed": uni.get("computed", 0),
        "top_n": top_n,
        # 全市场大单净流入 TOP / 流入最少(BOTTOM)
        "top_inflow": _enrich(rows[:top_n], changes),
        "top_outflow": _enrich(list(reversed(rows[-top_n:])), changes),
        "industry": industry_distribution(top_n),
        "split_wash": split_wash_counts(top_syms + bottom_syms),
        "note": "主力净流入为同花顺 DDE 大单口径(资金面参考), 禁用于主力意图方向性判定",
    }


def run_war_report_job(
    *, top_n: int = DEFAULT_TOP_N, l2=None, now: datetime | None = None
) -> dict:
    """盘后 cron / 手动 refresh 入口: 构建 + 落 war_report_daily(失败不抛, ok=False 表达)。

    数据源不可用 → **不落库**(避免把"无数据"存成快照), 显式 ok=False + reason。
    返回 {ok, reason(失败时), report(成功时的完整 payload)}。
    """
    from src.db.models import WarReportDaily
    from src.db.session import SessionLocal

    try:
        result = build_daily_war_report(top_n=top_n, l2=l2, now=now)
    except Exception as e:  # noqa: BLE001
        logger.exception("主力资金战报构建失败: %s", e)
        return {"ok": False, "reason": str(e)}

    if not result.get("available"):
        return {"ok": False, "reason": result.get("note") or "战报数据源不可用"}

    snap = result["snapshot_date"]
    db = SessionLocal()
    try:
        row = (
            db.query(WarReportDaily)
            .filter(
                WarReportDaily.snapshot_date == snap,
                WarReportDaily.stock_market == "CN",
            )
            .first()
        )
        if row:
            row.payload = result
        else:
            db.add(WarReportDaily(snapshot_date=snap, stock_market="CN", payload=result))
        db.commit()
    except Exception as e:  # noqa: BLE001
        db.rollback()
        logger.exception("主力资金战报落库失败: %s", e)
        return {"ok": False, "reason": f"落库失败: {e}"}
    finally:
        db.close()

    return {
        "ok": True,
        "snapshot_date": snap,
        "top": len(result.get("top_inflow") or []),
        "bottom": len(result.get("top_outflow") or []),
        "industry": bool((result.get("industry") or {}).get("available")),
        "split_wash": bool((result.get("split_wash") or {}).get("available")),
        "report": result,
    }
