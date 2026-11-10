"""东财 trends2 → ETF 分钟成交额**兜底源**（2026-11-10）。

为什么需要（实测根因，见 `tq_1m_recovery_findings.md`）：TQ 的 1m/5m/15m/30m 全部来自
客户端 `vipdoc/*/minline/*.lc1`，2026-11-05 21:20 起不再下载 ⇒ 11/06、11/09 等交易日的
1m 恒 0 根，而且返回 `ErrorId:"0"` + `Value:[]`（**不报错**）。TQ 侧没有"不碰客户端就能
补齐历史分钟"的官方方法（`refresh_kline` 只刷本地缓存，`.lc1` mtime 不动）。

东财 `trends2` 仍在服务这些日期：滚动 **5 个交易日**、每天 **241 根**（含 09:30 竞价根）、
不带 key、20 只 ETF 串行 12.4s。它补不回更早历史，但足以救「最近几个缺口日」。

本模块的三条硬纪律（写错会静默出错，不会抛异常）：

1. **只写缺口、只 INSERT**：`write_day` 在事务内先查该交易日是否已有行，有则整日放弃
   （返回 0）；写入语句是 INSERT-IGNORE，永不 UPDATE。所以「东财半小时数据覆盖 TQ 完整
   数据」在代码路径上不可能发生。
2. **同一天不混源**：`backfill` 的日期闸门是"该日有任何行就跳过"，因此一天的
   `etf_minute_flow.source` 只有一种值。已有行的来源会写进报告（`existing_sources`）。
3. **不拿残缺冒充完整**：根数 < 235、末根不是 15:00、出现非交易时段分钟、成交量单位不自洽
   （`amount/(volume×close)` 偏离 1 超过 2%）、覆盖标的数不到 80% 闸门 —— 整天不写，
   报告里写原因。绝不留半天数据。

口径（对齐 TQ，逐条实测标定）：

===========  ==============================  ====================================
项            东财 trends2                    落库/对齐方式
===========  ==============================  ====================================
时间          `f51` = `YYYY-MM-DD HH:MM` CST  取 HHMM；日期用报文里的真实交易日
成交额        `f57` 元（3 位小数）             ÷1e4 → `amount_wan`（万元，与 TQ `Amount` 同）
成交量        `f56` **手**                     ×100 → 股（与 TQ `Volume` 同）
开盘          `f52` 恒 0.000（不可用）        不使用；`close`=`f53` 仅用于单位自检
竞价归属      09:30 独立一根，收盘=当日开盘价    并入 09:31（TQ 就是把竞价并在 09:31 根里）
时段          09:30 / 09:31–11:30 / 13:01–15:00 无 13:00；14:58–14:59 常为零量占位
===========  ==============================  ====================================

对拍证据（2026-11-10 盘前，真实 HTTP + TQ 探针落盘）：11/04、11/05 两天各 240/240 根
分钟全等，`close` 最大差 0.000，`volume` 最大差 100 股，`amount` 最大差 0.06 元（手取整）。

**不做的事**：不在调度里自动跑（盘中时效未验证，且自动补数会掩盖 TQ 的健康度），
只提供显式 CLI / 函数调用；不写生产库（默认 dry-run）。也没接腾讯/新浪：同一交易日
只允许一个兜底源，多源并存就无法解释「这一天的分钟是谁的」。
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Mapping, Sequence

logger = logging.getLogger(__name__)

# ── 常量 ─────────────────────────────────────────────────────────────────

#: 落库来源标识（`etf_minute_flow.source`）。TQ 主源写 `tdx_tq`。
SOURCE = "em_trends2"
SOURCE_TQ = "tdx_tq"

#: 端点与参数（2026-11-10 实测可用）。`secid` 前缀：沪 1 / 深 0。
EM_TRENDS_URL = "https://push2his.eastmoney.com/api/qt/stock/trends2/get"
EM_HOST_KEY = "push2his.eastmoney.com"
EM_FIELDS1 = "f1,f2,f3,f4,f5,f6,f7,f8"
EM_FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58"
EM_USER_AGENT = "Mozilla/5.0 (compatible; sida-pro/etf-minute-fallback)"
#: 滚动窗口上限：传 >5 无效（实测始终只回 5 个交易日）。
MAX_NDAYS = 5

AUCTION_MINUTE = "0930"
OPEN_MINUTE = "0931"
LAST_MINUTE = "1500"
#: 归一后应有 240 根（TQ 口径）。低于此数按「数据不全」拒绝。
EXPECTED_MINUTES = 240
MIN_MINUTES = 235

#: 单位自检：amount(元) / (volume(股) × close) 应 ≈ 1（可用均价/收盘差异 ≤2%）。
UNIT_RATIO_LO, UNIT_RATIO_HI = 0.98, 1.02
#: 单位自检里允许的超差根数比例（收盘价与均价的正常偏离 + 零量占位根）。
UNIT_BAD_TOLERANCE = 0.05

#: 覆盖率闸门（与 `src.core.etf_watch_feed.COVERAGE_MIN` 同值，唯一真值见那里）。
COVERAGE_MIN = 0.80

#: 与 TQ 日线的对账容差（相对误差）。实测缺口日差 1e-8 量级，这里给到 0.2%（新浪口径是
#: -0.12%，超 0.2% 说明单位/标的错配，不是取整）。
RECONCILE_TOL = 0.002


class SourceMismatch(RuntimeError):
    """报文里的标的代码与请求不一致（secid 混用会静默返回别的标的）。"""


@dataclass(frozen=True)
class MinuteBar:
    """一根归一后的分钟线。`amount_wan` = 万元，`volume` = 股（与库内口径一致）。"""

    minute: str
    amount_wan: float
    volume: int
    close: float | None = None


# ── 代码 / secid ─────────────────────────────────────────────────────────

_CODE_RE = re.compile(r"^(\d{6})\.(SH|SZ)$")
_MARKET_PREFIX = {"SH": "1", "SZ": "0"}


def secid(code: str) -> str:
    """`510300.SH` → `1.510300`。格式不认识就抛错 —— 猜市场只会静默 0 数据。

    坑：指数在东财是 `1.000001`，TQ 是 `999999.SH`。两者混用会静默返回 null 或别的
    标的，所以这里只接受 `NNNNNN.SH|SZ`，指数由调用方显式给出正确 secid 语义。
    """
    m = _CODE_RE.match((code or "").strip().upper())
    if not m:
        raise ValueError(f"无法解析标的代码（需 NNNNNN.SH|SZ）: {code!r}")
    return f"{_MARKET_PREFIX[m.group(2)]}.{m.group(1)}"


def _num(v: Any) -> float | None:
    try:
        f = float(str(v).strip())
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


# ── 取数（唯一 HTTP 缝） ──────────────────────────────────────────────────

def _get_json(code: str, *, ndays: int = MAX_NDAYS, timeout: float = 8.0) -> dict | None:
    """取一只标的的 trends2 报文。**这是唯一 HTTP 缝，测试只打桩这里。**

    失败返回 None（不抛）：调用方按「无数据」处理，绝不会写 0 冒充。
    """
    from src.collectors.market_http import market_get

    params = {
        "secid": secid(code),
        "fields1": EM_FIELDS1,
        "fields2": EM_FIELDS2,
        "ndays": str(max(1, min(int(ndays), MAX_NDAYS))),
        "iscr": "0",
        "iscca": "0",
    }
    payload = market_get(
        EM_TRENDS_URL,
        host_key=EM_HOST_KEY,
        params=params,
        headers={"User-Agent": EM_USER_AGENT, "Referer": "https://quote.eastmoney.com/"},
        parse="json",
        timeout=timeout,
        retries=1,
        max_total_s=max(timeout * 2, 10.0),
        symbol=code,
        log_label="[etf-em-1m] trends2",
    )
    return payload if isinstance(payload, dict) else None


def fetch_trends(code: str, *, ndays: int = MAX_NDAYS, timeout: float = 8.0) -> dict | None:
    return _get_json(code, ndays=ndays, timeout=timeout)


# ── 解析 / 归一 / 校验 ───────────────────────────────────────────────────

def parse_trends(payload: dict | None, code: str) -> dict[str, list[MinuteBar]]:
    """报文 → {YYYYMMDD: [MinuteBar, ...]}（**保留 09:30 竞价根**，由归一函数处理）。

    - `data` 为 null（secid 错/无数据）→ 返回 {}；
    - 报文 `code` 与请求不一致 → `SourceMismatch`（宁可失败也不混标的）。
    """
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    if not isinstance(data, dict):
        return {}
    want = _CODE_RE.match((code or "").strip().upper())
    got = str(data.get("code") or "").strip()
    if want and got and got.zfill(6) != want.group(1):
        raise SourceMismatch(f"报文标的 {got} ≠ 请求 {code}（secid 可能传成了别的标的）")

    out: dict[str, list[MinuteBar]] = {}
    bad = 0
    for line in data.get("trends") or []:
        parts = str(line).split(",")
        if len(parts) < 8:
            bad += 1
            continue
        stamp = parts[0].strip()
        day_raw, _, hm_raw = stamp.partition(" ")
        day = day_raw.replace("-", "")
        hm = hm_raw.replace(":", "")[:4]
        if len(day) != 8 or not day.isdigit() or len(hm) != 4 or not hm.isdigit():
            bad += 1
            continue
        lots = _num(parts[5])
        yuan = _num(parts[6])
        close = _num(parts[2])
        # 量、额任一缺失 → 该分钟不入库（不写 0）。手 → 股用 ×100，四舍五入到整股。
        if lots is None or yuan is None:
            bad += 1
            continue
        out.setdefault(day, []).append(
            MinuteBar(minute=hm, amount_wan=yuan / 1e4, volume=int(round(lots * 100)), close=close)
        )
    if bad:
        logger.warning("[etf-em-1m] %s 报文有 %d 行无法解析（已跳过）", code, bad)
    return out


def canonicalize_auction(bars: Sequence[MinuteBar]) -> list[MinuteBar]:
    """把 09:30 集合竞价**并入 09:31**（TQ 口径），并保证幂等。

    - 有 09:30 且有 09:31：量、额相加，收盘取 09:31 那根（竞价不改分钟收盘）；
    - 有 09:30 没有 09:31（极端）：竞价本身改标为 09:31，金额一分不丢；
    - 没有 09:30：原样返回（已归一的输入重复调用结果不变）。
    """
    auction: MinuteBar | None = None
    rest: list[MinuteBar] = []
    for b in bars:
        if b.minute == AUCTION_MINUTE:
            if auction is None:
                auction = b
            else:  # 不该出现；出现了也不能丢金额
                auction = MinuteBar(
                    AUCTION_MINUTE,
                    auction.amount_wan + b.amount_wan,
                    auction.volume + b.volume,
                    b.close if b.close is not None else auction.close,
                )
            continue
        rest.append(b)
    if auction is None:
        return rest
    if rest and rest[0].minute == OPEN_MINUTE:
        first = rest[0]
        rest[0] = MinuteBar(
            OPEN_MINUTE,
            first.amount_wan + auction.amount_wan,
            first.volume + auction.volume,
            first.close if first.close is not None else auction.close,
        )
    else:
        rest.insert(0, MinuteBar(OPEN_MINUTE, auction.amount_wan, auction.volume, auction.close))
    return rest


def _in_session(hm: str) -> bool:
    return ("0931" <= hm <= "1130") or ("1301" <= hm <= "1500")


def validate_day(day: str, bars: Sequence[MinuteBar]) -> tuple[bool, list[str]]:
    """单日完整性 + 单位自洽校验。返回 (ok, problems)。**problems 非空即整天不写。**"""
    problems: list[str] = []
    if not bars:
        return False, ["无分钟数据"]
    minutes = [b.minute for b in bars]
    if len(bars) < MIN_MINUTES:
        problems.append(f"根数不足: {len(bars)} < {MIN_MINUTES}（东财完整日应为 {EXPECTED_MINUTES} 根）")
    if minutes[0] != OPEN_MINUTE:
        problems.append(f"首根不是 {OPEN_MINUTE}（竞价未归一？首根={minutes[0]}）")
    if minutes[-1] != LAST_MINUTE:
        problems.append(f"末根不是 {LAST_MINUTE}，判为 partial（实际末根={minutes[-1]}）")
    off = sorted({m for m in minutes if not _in_session(m)})
    if off:
        problems.append(f"非交易时段分钟: {off[:5]}")
    if len(set(minutes)) != len(minutes):
        dup = sorted({m for m in minutes if minutes.count(m) > 1})
        problems.append(f"重复分钟: {dup[:5]}")
    total_vol = sum(b.volume for b in bars)
    if total_vol <= 0:
        problems.append("全日成交量为 0")

    checked = bad = 0
    for b in bars:
        if b.volume <= 0 or not b.close or b.close <= 0:
            continue  # 零量占位根（14:58/14:59）与缺价根不参与自检
        checked += 1
        ratio = (b.amount_wan * 1e4) / (b.volume * b.close)
        if not (UNIT_RATIO_LO <= ratio <= UNIT_RATIO_HI):
            bad += 1
    if checked and bad / checked > UNIT_BAD_TOLERANCE:
        problems.append(
            f"单位不自洽: {bad}/{checked} 根 amount/(volume×close) 落在"
            f" [{UNIT_RATIO_LO}, {UNIT_RATIO_HI}] 之外（手/股 或 元/万元 换算错了）"
        )
    return (not problems), problems


# ── 落库（只 INSERT，永不 UPDATE） ───────────────────────────────────────

def _default_engine():
    from src.web.database import engine

    return engine


_INSERT_COLS = ["trade_date", "minute", "etf_code", "amount_wan", "volume", "source",
                "created_at", "updated_at"]


def existing_overview(engine, day: str) -> dict[str, Any]:
    """该交易日现有行概览：行数、标的数、来源集合（混源排查用）。"""
    from sqlalchemy import text

    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT COUNT(*) AS rows, COUNT(DISTINCT etf_code) AS codes "
                "FROM etf_minute_flow WHERE trade_date = :d"
            ),
            {"d": day},
        ).mappings().first() or {}
        sources = [
            str(r[0]) if r[0] is not None else ""
            for r in conn.execute(
                text("SELECT DISTINCT source FROM etf_minute_flow WHERE trade_date = :d"),
                {"d": day},
            ).all()
        ]
    return {
        "rows": int(row.get("rows") or 0),
        "codes": int(row.get("codes") or 0),
        "sources": sorted(s for s in sources if s),
    }


def write_day(
    day: str,
    data: Mapping[str, Sequence[MinuteBar]],
    *,
    engine=None,
    source: str = SOURCE,
) -> int:
    """写单日分钟数据。返回写入行数；已有数据/冲突/失败返回 0 或 -1。

    两道锁：
    1. **事务内**先查该日是否已有行 —— 有任何行（任何来源）就整日放弃，保证同日不混源；
    2. 写入用 INSERT-IGNORE（`src.db.dialect.insert_ignore_sql`），并发下也不会覆盖已有分钟。
    """
    if not data:
        return 0
    eng = engine if engine is not None else _default_engine()
    from sqlalchemy import text

    from src.db.dialect import insert_ignore_sql

    sql = insert_ignore_sql("etf_minute_flow", _INSERT_COLS)
    now = datetime.now()
    params = [
        {
            "trade_date": day,
            "minute": b.minute,
            "etf_code": code,
            "amount_wan": b.amount_wan,
            "volume": int(b.volume),
            "source": source,
            "created_at": now,
            "updated_at": now,
        }
        for code, bars in data.items()
        for b in bars
    ]
    if not params:
        return 0
    try:
        with eng.begin() as conn:
            have = conn.execute(
                text("SELECT COUNT(*) FROM etf_minute_flow WHERE trade_date = :d"), {"d": day}
            ).scalar()
            if have:
                logger.info("[etf-em-1m] %s 已有 %s 行，整日跳过（不混源/不覆盖）", day, have)
                return 0
            res = conn.execute(text(sql), params)
            inserted = getattr(res, "rowcount", -1)
            # 并发写入者抢先插了同一主键 → 这里会少于应写行数。回滚，别把半天数据留下。
            if isinstance(inserted, int) and 0 <= inserted < len(params):
                raise RuntimeError(f"并发写入冲突: 仅插入 {inserted}/{len(params)} 行")
        return len(params)
    except Exception as e:  # noqa: BLE001
        logger.warning("[etf-em-1m] %s 写库失败（已回滚）: %s", day, e)
        return -1


# ── 编排 ─────────────────────────────────────────────────────────────────

def _gate(expected: int) -> int:
    """覆盖率闸门（≥80%，20 只 → 16 只）。唯一真值在 `src.core.etf_watch_feed`。"""
    try:
        from src.core.etf_watch_feed import min_etfs_for_complete

        return int(min_etfs_for_complete(expected))
    except Exception:  # noqa: BLE001 - 单元隔离场景下退化成同值计算
        return max(1, math.ceil(expected * COVERAGE_MIN - 1e-9))


def _plan(day: str, today: date, engine) -> dict[str, Any]:
    """日期闸门：只决定「能不能取数」，不联网。"""
    from src.core.trading_calendar import is_trading_day

    try:
        d = datetime.strptime(day, "%Y%m%d").date()
    except (TypeError, ValueError):
        return {"date": str(day), "action": "skipped", "reason": "bad_date"}
    if d.weekday() >= 5 or not is_trading_day(d):
        return {"date": day, "action": "skipped", "reason": "non_trading_day"}
    if d >= today:
        # 当日盘中东财的 09:30 根/尾根形态未验证，且 15:00 前必然是 partial —— 一律不补。
        return {"date": day, "action": "skipped", "reason": "not_closed_day"}
    try:
        ov = existing_overview(engine, day)
    except Exception as e:  # noqa: BLE001
        return {"date": day, "action": "skipped", "reason": "db_unavailable", "error": str(e)[:200]}
    if ov["rows"]:
        return {
            "date": day,
            "action": "skipped",
            "reason": "date_already_ingested",
            "existing_rows": ov["rows"],
            "existing_codes": ov["codes"],
            "existing_sources": ov["sources"],
        }
    return {"date": day, "action": "fetch"}


def calibration_note() -> dict[str, str]:
    """来源与口径声明（报告里原样带出，供下游/审计判断这批数字是谁的）。"""
    return {
        "source": SOURCE,
        "endpoint": EM_TRENDS_URL,
        "amount_wan": "万元",
        "volume": "股",
        "auction": "09:30 集合竞价并入 09:31 根（对齐 TQ 口径）",
        "session": "09:31-11:30 / 13:01-15:00（无 13:00 根；14:58-14:59 常为零量占位）",
        "window": f"东财滚动 {MAX_NDAYS} 个交易日，超过窗口的日期补不到",
        "trading_day_source": "src.core.trading_calendar（节假日/调休感知）",
        "verified_against": "TQ 2026-11-04 / 2026-11-05 分钟线（240/240 根；close 0 差，量 ≤100 股，额 ≤0.06 元）",
    }


DayRef = Callable[[str], dict[str, float]]


def tq_daily_ref(codes: Sequence[str], *, timeout: float = 15.0) -> DayRef:
    """构造「TQ 日线成交额（万元）」参照函数，用于逐只对账。

    TQ 不可用/该日无数据时抛异常 —— 编排层据此把 `reconciled` 标 False 并写明原因，
    不会把"没对过账"说成"对过账"。
    """

    def _ref(day: str) -> dict[str, float]:
        from src.collectors.tq_etf_watch import _num as _tq_num
        from src.collectors.tq_etf_watch import _rpc_full

        out: dict[str, float] = {}
        wanted = set(codes)
        for page in range(40):
            payload = _rpc_full(
                "get_market_data",
                {
                    "stock_list": list(codes),
                    "period": "1d",
                    "start_time": day,
                    "end_time": day,
                    "stock_page_index": page,
                },
                timeout=timeout,
            )
            value = payload.get("Value") if isinstance(payload, dict) else None
            if not isinstance(value, dict) or not value:
                break
            new = [k for k in value if k in wanted and k not in out]
            for code in new:
                node = value.get(code) or {}
                dates = node.get("Date") or []
                amts = node.get("Amount") or []
                for i, d0 in enumerate(dates):
                    if str(d0) == day and i < len(amts):
                        v = _tq_num(amts[i])
                        if v is not None:
                            out[code] = v
                        break
            if not new or not payload.get("has_more"):
                break
        if not out:
            raise RuntimeError(f"TQ 日线不含 {day} 的成交额（网关无数据或该日未收录）")
        return out

    return _ref


def backfill(
    dates: Sequence[str],
    *,
    codes: Sequence[str] | None = None,
    engine=None,
    now: datetime | None = None,
    dry_run: bool = True,
    daily_ref: DayRef | None = None,
    max_ndays: int = MAX_NDAYS,
) -> dict[str, Any]:
    """把指定交易日的分钟数据用东财补齐。**默认 dry_run=True（不写库）。**

    逐日纪律：日期闸门 → 按标的取一次报文（同一报文覆盖 5 天，20 只 ETF 一次 20 请求）
    → 竞价归一 → 完整性/单位校验 → 覆盖率闸门 → （可选）TQ 日线对账 → 整日写入。

    报告里每天都有 `action`（written/planned/skipped）、`reason`、`rows`、`etfs`、
    `dropped`（被丢弃的标的及原因）、`reconciled` —— 不允许静默成功。
    """
    from src.collectors.tq_etf_watch import ETF_BASKET

    eng = engine if engine is not None else _default_engine()
    now = now or datetime.now()
    today = now.date()
    codes = [str(c) for c in (codes if codes is not None else [c for c, _n, _cat in ETF_BASKET])]
    gate = _gate(len(codes))

    plans = [_plan(str(d), today, eng) for d in dates]
    pending = [p for p in plans if p["action"] == "fetch"]

    fetched: dict[str, dict[str, list[MinuteBar]]] = {}
    fetch_errors: dict[str, str] = {}
    window: set[str] = set()
    if pending:
        for code in codes:
            try:
                payload = fetch_trends(code, ndays=max_ndays)
                by_day = parse_trends(payload, code) if payload else {}
            except Exception as e:  # noqa: BLE001
                fetch_errors[code] = f"{type(e).__name__}: {e}"[:200]
                continue
            if not by_day:
                fetch_errors[code] = "空报文（data 为 null 或 trends 为空；secid 错/该源无数据）"
                continue
            fetched[code] = by_day
            window |= set(by_day)

    days_report: list[dict[str, Any]] = []
    for plan in plans:
        if plan["action"] != "fetch":
            days_report.append(plan)
            continue
        day = plan["date"]
        entry: dict[str, Any] = {"date": day, "action": "skipped", "reason": None,
                                 "rows": 0, "etfs": 0, "dropped": {}, "reconciled": None}
        if not fetched:
            entry.update(reason="fetch_failed", error=next(iter(fetch_errors.values()), "全部标的取数失败"))
            entry["dropped"] = dict(fetch_errors)
            days_report.append(entry)
            continue
        if day not in window:
            entry.update(reason="out_of_window")
            days_report.append(entry)
            continue

        dropped: dict[str, str] = {}
        bars_by_code: dict[str, list[MinuteBar]] = {}
        for code, by_day in fetched.items():
            raw = by_day.get(day)
            if not raw:
                dropped[code] = "该日无数据（停牌或不在窗口）"
                continue
            bars = canonicalize_auction(raw)
            ok, problems = validate_day(day, bars)
            if not ok:
                dropped[code] = "数据不完整: " + "; ".join(problems)
                continue
            bars_by_code[code] = bars
        for code, err in fetch_errors.items():
            dropped.setdefault(code, err)

        if daily_ref is not None and bars_by_code:
            try:
                ref = daily_ref(day) or {}
                entry["reconciled"] = True
            except Exception as e:  # noqa: BLE001
                ref = {}
                entry["reconciled"] = False
                entry["reconcile_error"] = f"{type(e).__name__}: {e}"[:200]
            for code in list(bars_by_code):
                want = ref.get(code)
                if want is None or want <= 0:
                    continue
                total = sum(b.amount_wan for b in bars_by_code[code])
                if abs(total - want) / want > RECONCILE_TOL:
                    dropped[code] = (
                        f"对账超差: 分钟合计 {total:.2f} 万 vs TQ 日线 {want:.2f} 万"
                        f"（容差 {RECONCILE_TOL:.1%}）"
                    )
                    del bars_by_code[code]

        entry["etfs"] = len(bars_by_code)
        entry["dropped"] = dropped
        if len(bars_by_code) < gate:
            entry.update(reason="insufficient_etfs", gate=gate)
            days_report.append(entry)
            continue

        if dry_run:
            entry.update(action="planned", rows=sum(len(b) for b in bars_by_code.values()))
            days_report.append(entry)
            continue

        written = write_day(day, bars_by_code, engine=eng)
        if written < 0:
            entry.update(reason="write_failed")
        elif written == 0:
            entry.update(reason="date_already_ingested")
        else:
            entry.update(action="written", rows=written)
        days_report.append(entry)

    return {
        "source": SOURCE,
        "dry_run": bool(dry_run),
        "fetched_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "caliber": calibration_note(),
        "gate_etfs": gate,
        "etfs_requested": len(codes),
        "window_dates": sorted(window),
        "written_rows": sum(int(d.get("rows") or 0) for d in days_report if d["action"] == "written"),
        "days": days_report,
        "fetch_errors": fetch_errors,
    }


def gap_dates(
    days: int = 30,
    *,
    end: date | None = None,
    engine=None,
    codes: Sequence[str] | None = None,
) -> list[str]:
    """最近 N 个交易日里「标的覆盖不足闸门」的日期（缺数据或只有零星几只）。

    当前生产形态就是这一类：11/06、11/09 表里 0 行；更隐蔽的是只落了几只标的的日期，
    用 `COUNT(*) > 0` 判会误报成"已采"。
    """
    from sqlalchemy import text

    from src.collectors.tq_etf_watch import ETF_BASKET, recent_trade_dates

    eng = engine if engine is not None else _default_engine()
    expected = len(codes) if codes is not None else len(ETF_BASKET)
    gate = _gate(expected)
    dates = recent_trade_dates(days, end=end or date.today())
    if not dates:
        return []
    with eng.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT trade_date, COUNT(DISTINCT etf_code) AS codes FROM etf_minute_flow "
                "WHERE trade_date >= :lo AND trade_date <= :hi AND amount_wan IS NOT NULL "
                "GROUP BY trade_date"
            ),
            {"lo": dates[0], "hi": dates[-1]},
        ).all()
    have = {str(r[0]): int(r[1] or 0) for r in rows}
    return [d for d in dates if have.get(d, 0) < gate]


# ── CLI（显式运维入口，默认 dry-run） ────────────────────────────────────

#: 这些原因属于「本来就不该补」，不算失败（退出码 0）。
_BENIGN_SKIP = {"date_already_ingested", "non_trading_day", "not_closed_day"}


def exit_code(report: Mapping[str, Any]) -> int:
    """0 = 每个请求日要么写了/能写，要么属于正当跳过；1 = 有真正没补上的。"""
    bad = [
        d for d in report.get("days", [])
        if d.get("action") not in ("written", "planned") and d.get("reason") not in _BENIGN_SKIP
    ]
    return 0 if not bad else 1

def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m src.collectors.em_etf_minute",
        description="东财 trends2 补 ETF 分钟缺口（默认 dry-run 不写库）",
    )
    ap.add_argument("--dates", help="逗号分隔的 YYYYMMDD")
    ap.add_argument("--gaps", type=int, default=0, help="先盘查最近 N 个交易日的缺口并补这些日期")
    ap.add_argument("--days", type=int, default=5, help="--gaps 的窗口（默认 5，即东财窗口上限）")
    ap.add_argument("--write", action="store_true", help="真的写库（缺省只演练）")
    ap.add_argument("--no-reconcile", action="store_true", help="跳过 TQ 日线对账")
    args = ap.parse_args(argv)

    engine = _default_engine()
    dates = [d.strip() for d in (args.dates or "").split(",") if d.strip()]
    if args.gaps:
        dates = sorted(set(dates) | set(gap_dates(args.gaps, engine=engine)))
    if not dates:
        print("未指定 --dates 或 --gaps：无可补日期")
        return 2

    from src.collectors.tq_etf_watch import ETF_BASKET

    daily_ref = None if args.no_reconcile else tq_daily_ref([c for c, _n, _cat in ETF_BASKET])
    report = backfill(dates, engine=engine, dry_run=not args.write, daily_ref=daily_ref)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return exit_code(report)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
