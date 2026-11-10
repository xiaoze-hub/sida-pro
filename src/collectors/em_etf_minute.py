"""东财 trends2 → ETF 分钟成交额**兜底源**（2026-11-10）。

为什么需要（实测根因，见 `tq_1m_recovery_findings.md`）：TQ 的 1m/5m/15m/30m 全部来自
客户端 `vipdoc/*/minline/*.lc1`，2026-11-05 21:20 起不再下载 ⇒ 11/06、11/09 等交易日的
1m 恒 0 根，而且返回 `ErrorId:"0"` + `Value:[]`（**不报错**）。TQ 侧没有"不碰客户端就能
补齐历史分钟"的官方方法（`refresh_kline` 只刷本地缓存，`.lc1` mtime 不动）。

东财 `trends2` 仍在服务这些日期：滚动 **5 个交易日（含当日）**、每天 **241 根**（含 09:30
竞价根）、不带 key、20 只 ETF 串行 12.4s。当日被 `not_closed_day` 闸门排除，所以实际能补的
已收盘日通常只有最近 4 个；更早的历史补不到。

本模块的三条硬纪律（写错会静默出错，不会抛异常）：

1. **只写缺口、只 INSERT**：`write_day` 在事务内**先取该交易日的数据库写锁**
   （PG `pg_advisory_xact_lock` / SQLite `BEGIN IMMEDIATE`），拿到锁之后才查该日是否
   已有行，有则整日放弃（返回 0）；写入语句是 INSERT-IGNORE，永不 UPDATE。所以
   「东财半小时数据覆盖 TQ 完整数据」和「两个写者都看到 0 行、各写一半」在代码路径上
   都不可能发生。**不允许用无锁 SELECT 做判重** —— 那是先查后写的经典竞态。
2. **同一天不混源**：日级写锁 + 「该日已有任何行（任何来源）就整日跳过」+ 提交前在
   同一事务内复核「该日所有行的 source 都等于本次来源」，三者同时成立才算写入成功。
   `tq_etf_watch.write_minute` 用同一把锁，并且发现该日已有别的来源时直接拒绝写
   （返回 -2 / `source_conflict`），不会把 `tdx_tq` 覆盖到东财的日上。
3. **不拿残缺冒充完整**：必须是**恰好 240 个交易分钟**（09:31–11:30 / 13:01–15:00，
   缺一根、多一根、重复一根都不行）、末根 15:00、成交量单位自洽
   （`amount/(volume×close)` 偏离 1 超过 2%）、覆盖标的数不到 80% 闸门 —— 整天不写，
   报告里写原因。绝不留半天数据。对账只在「每一只待写标的都拿到日线参照且都在容差内」
   时才标记 `verified=True`，否则 CLI 默认拒绝写入并退出非 0。

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
#: 归一后必须**恰好** 240 根（TQ 口径：09:31–11:30 + 13:01–15:00）。
EXPECTED_MINUTES = 240
#: 旧名保留（下游/测试可能引用），语义已收紧为「必须 240 根」，不再有 235 的宽容。
MIN_MINUTES = EXPECTED_MINUTES

#: 完整交易日的 240 个分钟槽。校验用集合相等，不用「根数 ≥ N」——根数够但缺根、
#: 多根、重复根的报文同样不能入库（基线按分钟对齐，缺一根就是一个假空洞）。
SESSION_MINUTES: tuple[str, ...] = tuple(
    f"{h:02d}{m:02d}"
    for h, lo, hi in ((9, 31, 59), (10, 0, 59), (11, 0, 30), (13, 1, 59), (14, 0, 59), (15, 0, 0))
    for m in range(lo, hi + 1)
)
assert len(SESSION_MINUTES) == EXPECTED_MINUTES == len(set(SESSION_MINUTES))

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
    """单日完整性 + 单位自洽校验。返回 (ok, problems)。**problems 非空即整天不写。**

    完整性是「240 个交易分钟集合相等」，不是「根数够就行」：缺一根、多一根、
    重复一根、或混进 09:30/13:00 这类 TQ 没有的分钟，都会让下游按分钟对齐的基线
    出现单样本假尖峰。
    """
    problems: list[str] = []
    if not bars:
        return False, ["无分钟数据"]
    minutes = [b.minute for b in bars]
    present = set(minutes)
    missing = [m for m in SESSION_MINUTES if m not in present]
    extra = sorted(present - set(SESSION_MINUTES))
    if len(bars) != EXPECTED_MINUTES:
        problems.append(f"根数不是 {EXPECTED_MINUTES}: {len(bars)}")
    if missing:
        problems.append(f"缺 {len(missing)} 个交易分钟: {missing[:5]}")
    if extra:
        problems.append(f"非交易时段/多余分钟: {extra[:5]}")
    if minutes[0] != OPEN_MINUTE:
        problems.append(f"首根不是 {OPEN_MINUTE}（竞价未归一？首根={minutes[0]}）")
    if minutes[-1] != LAST_MINUTE:
        problems.append(f"末根不是 {LAST_MINUTE}，判为 partial（实际末根={minutes[-1]}）")
    if len(present) != len(minutes):
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

#: 日级写锁命名空间（换表/换粒度时改这里，避免和别的表的 advisory lock 撞号）。
_DAY_LOCK_NS = "etf_minute_flow"


def day_lock_key(day: str) -> int:
    """`YYYYMMDD` → 稳定的 64 位整数 key（PG advisory lock 用）。

    不用 Python `hash()`：它带进程随机化，两个进程会算出不同的 key，锁等于没加。
    """
    import hashlib

    digest = hashlib.blake2b(f"{_DAY_LOCK_NS}:{day}".encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def _insert_ignore_sql(dialect: str, table: str, cols: Sequence[str]) -> str:
    """按**真实连接的方言**生成冲突跳过 SQL。

    不走 `src.db.dialect.insert_ignore_sql` 是有意的：那个函数按环境变量判定后端，
    测试/迁移场景里 engine 可能是独立 SQLite 而进程环境仍写着 PG —— 用错语法就是
    硬报错或静默写不进去。这里只看 `conn.dialect.name`。
    """
    names = ", ".join(cols)
    values = ", ".join(f":{c}" for c in cols)
    if dialect == "postgresql":
        return f"INSERT INTO {table} ({names}) VALUES ({values}) ON CONFLICT DO NOTHING"
    if dialect == "sqlite":
        return f"INSERT OR IGNORE INTO {table} ({names}) VALUES ({values})"
    raise RuntimeError(f"不支持的后端方言 {dialect!r}：拒绝写入（无日级锁语义）")


def lock_day(conn, day: str) -> str:
    """在事务开头取得该交易日的**数据库级写锁**，返回方言名。

    必须在任何判重 SELECT 之前调用 —— 先查后写、中间没有锁，是「两个写者都看到 0 行、
    各自写一半」的根因。

    - PostgreSQL：`pg_advisory_xact_lock(key)`，事务结束自动释放；锁 key 只由交易日期
      决定，TQ 与东财写同一日时争的是同一把锁。
    - SQLite：`BEGIN IMMEDIATE`，立即拿库级写锁（配合 busy handler 等待），而不是等到
      第一条 INSERT 才升级锁（升级时可能直接 SQLITE_BUSY 失败）。
    - 其它方言：抛错 = **安全禁用写入**，宁可 CLI 失败也不留无锁写入路径。
    """
    from sqlalchemy import text

    dialect = conn.dialect.name
    if dialect == "postgresql":
        conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": day_lock_key(day)})
    elif dialect == "sqlite":
        conn.exec_driver_sql("BEGIN IMMEDIATE")
    else:
        raise RuntimeError(f"不支持的后端方言 {dialect!r}：拒绝在没有日级锁的情况下写入")
    return dialect


def existing_overview(engine, day: str, *, min_codes: int = 1) -> dict[str, Any]:
    """该交易日现有行概览：行数、标的数、来源集合、分钟覆盖（混源/残缺排查用）。

    `complete=True` 的条件（全部满足）：
    - 只有一个来源（`sources` 长度 1）；
    - 标的数 ≥ `min_codes`（默认 1，编排层传覆盖率闸门）；
    - 该日出现的分钟**恰好**是 240 个交易分钟；
    - 每只标的的行数都是 240（没有「某只只写了半天」）。
    不满足就是 partial —— 不能因为 `COUNT(*) > 0` 就当成「已采完」。
    """
    from sqlalchemy import text

    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT COUNT(*) AS rows, COUNT(DISTINCT etf_code) AS codes "
                "FROM etf_minute_flow WHERE trade_date = :d"
            ),
            {"d": day},
        ).mappings().first() or {}
        per_code = conn.execute(
            text(
                "SELECT etf_code, COUNT(*) AS n FROM etf_minute_flow "
                "WHERE trade_date = :d GROUP BY etf_code"
            ),
            {"d": day},
        ).all()
        minutes = [
            str(r[0])
            for r in conn.execute(
                text("SELECT DISTINCT minute FROM etf_minute_flow WHERE trade_date = :d"),
                {"d": day},
            ).all()
        ]
        sources = [
            str(r[0]) if r[0] is not None else ""
            for r in conn.execute(
                text("SELECT DISTINCT source FROM etf_minute_flow WHERE trade_date = :d"),
                {"d": day},
            ).all()
        ]
    counts = {str(r[0]): int(r[1] or 0) for r in per_code}
    present = set(minutes)
    missing = [m for m in SESSION_MINUTES if m not in present]
    extra = sorted(present - set(SESSION_MINUTES))
    short_codes = sorted(c for c, n in counts.items() if n != EXPECTED_MINUTES)
    uniq_sources = sorted(s for s in sources if s)
    complete = bool(
        counts
        and len(uniq_sources) == 1
        and "" not in sources
        and len(counts) >= max(1, int(min_codes))
        and not missing
        and not extra
        and not short_codes
    )
    return {
        "rows": int(row.get("rows") or 0),
        "codes": int(row.get("codes") or 0),
        "sources": uniq_sources,
        "minutes": len(present),
        "missing_minutes": missing[:5],
        "extra_minutes": extra[:5],
        "short_codes": short_codes[:5],
        "complete": complete,
    }


def write_day(
    day: str,
    data: Mapping[str, Sequence[MinuteBar]],
    *,
    engine=None,
    source: str = SOURCE,
) -> int:
    """写单日分钟数据。返回写入行数；已有数据/冲突/失败返回 0 或 -1。

    三道闸，全部在**同一个事务、同一把日级锁之下**：

    1. `lock_day()`：先拿 DB 级写锁（PG advisory xact lock / SQLite BEGIN IMMEDIATE），
       并发写者在此串行化，判重不再依赖无锁 SELECT；
    2. 锁内查该日是否已有行（任何来源）—— 有就整日放弃（返回 0），保证同一交易日不混源；
    3. INSERT-IGNORE 后在同一事务内复核：该日总行数 == 本次应写行数，且所有行
       `source` 都等于本次来源；任何一条不满足就抛错回滚，绝不留半天/混源数据。
    """
    if not data:
        return 0
    eng = engine if engine is not None else _default_engine()
    from sqlalchemy import text

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
            dialect = lock_day(conn, day)
            have = conn.execute(
                text("SELECT COUNT(*) FROM etf_minute_flow WHERE trade_date = :d"), {"d": day}
            ).scalar()
            if have:
                logger.info("[etf-em-1m] %s 已有 %s 行，整日跳过（不混源/不覆盖）", day, have)
                return 0
            sql = _insert_ignore_sql(dialect, "etf_minute_flow", _INSERT_COLS)
            res = conn.execute(text(sql), params)
            inserted = getattr(res, "rowcount", -1)
            if isinstance(inserted, int) and 0 <= inserted < len(params):
                raise RuntimeError(f"并发写入冲突: 仅插入 {inserted}/{len(params)} 行")
            total = conn.execute(
                text("SELECT COUNT(*) FROM etf_minute_flow WHERE trade_date = :d"), {"d": day}
            ).scalar()
            foreign = conn.execute(
                text(
                    "SELECT COUNT(*) FROM etf_minute_flow "
                    "WHERE trade_date = :d AND (source IS NULL OR source <> :s)"
                ),
                {"d": day, "s": source},
            ).scalar()
            if int(total or 0) != len(params) or int(foreign or 0) != 0:
                raise RuntimeError(
                    f"写入后复核失败: 该日 {total} 行（应 {len(params)}），异源 {foreign} 行"
                )
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


def _plan(day: str, today: date, engine, *, min_codes: int = 1) -> dict[str, Any]:
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
        ov = existing_overview(engine, day, min_codes=min_codes)
    except Exception as e:  # noqa: BLE001
        return {"date": day, "action": "skipped", "reason": "db_unavailable", "error": str(e)[:200]}
    if ov["rows"]:
        # 关键：有行 ≠ 已采完。只写了一两只、或某只只写了半天，都是 partial，
        # 必须作为**失败**暴露给 CLI，而不是当成「已补过」静默成功。
        return {
            "date": day,
            "action": "skipped",
            "reason": "date_already_ingested" if ov["complete"] else "date_partially_ingested",
            "existing_rows": ov["rows"],
            "existing_codes": ov["codes"],
            "existing_sources": ov["sources"],
            "existing_minutes": ov["minutes"],
            "existing_short_codes": ov["short_codes"],
            "existing_missing_minutes": ov["missing_minutes"],
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
        "window": (
            f"东财 trends2 只回最近 {MAX_NDAYS} 个交易日（**含当日**）的滚动窗口；当日已由"
            f" not_closed_day 闸门排除，所以实际可补的已收盘日通常只有 {MAX_NDAYS - 1} 个，"
            "更早的日期补不到"
        ),
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
    require_verified: bool = False,
) -> dict[str, Any]:
    """把指定交易日的分钟数据用东财补齐。**默认 dry_run=True（不写库）。**

    逐日纪律：日期闸门 → 按标的取一次报文（同一报文覆盖东财滚动窗口，20 只 ETF 一次
    20 请求）→ 竞价归一 → 完整 240 分钟/单位校验 → 覆盖率闸门 → TQ 日线对账 → 整日写入。

    `require_verified=True`（CLI 默认）时：只要该日本来要写的标的里有一只没拿到日线
    参照、或对账超差、或 `daily_ref` 缺失，就不写，`reason="unverified"`，退出码非 0。
    `daily_ref=None` 仍可用（显式 `--no-reconcile`），但报告里 `verified=False`，
    不会出现「没对过账却说对过账」。

    报告里每天都有 `action`（written/planned/skipped）、`reason`、`rows`、`etfs`、
    `dropped`（被丢弃的标的及原因）、`reconciled`、`verified` —— 不允许静默成功。
    """
    from src.collectors.tq_etf_watch import ETF_BASKET

    eng = engine if engine is not None else _default_engine()
    now = now or datetime.now()
    today = now.date()
    codes = [str(c) for c in (codes if codes is not None else [c for c, _n, _cat in ETF_BASKET])]
    gate = _gate(len(codes))

    plans = [_plan(str(d), today, eng, min_codes=gate) for d in dates]
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
                                 "rows": 0, "etfs": 0, "dropped": {}, "reconciled": False,
                                 "verified": False}
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

        candidates = len(bars_by_code)
        reconcile: dict[str, Any] = {"compared": 0, "missing": [], "mismatched": []}
        if daily_ref is None:
            # 显式不做对账：诚实地把 reconciled 留 False，不许把「没对过」写成「对过」。
            entry["reconcile_error"] = "未提供日线参照（--no-reconcile），本日对账不可证实"
        elif bars_by_code:
            try:
                ref = daily_ref(day) or {}
            except Exception as e:  # noqa: BLE001
                # 参照整体取不到时**不能**把每只都当「缺参照」丢掉：那会把一个本来
                # 完整的日子变成 insufficient_etfs。保留数据，明确标 reconciled=False。
                entry["reconcile_error"] = f"{type(e).__name__}: {e}"[:200]
            else:
                for code in list(bars_by_code):
                    want = ref.get(code)
                    if want is None or want <= 0:
                        reconcile["missing"].append(code)
                        dropped[code] = "对账缺少 TQ 日线参照（无法证实，整只不写）"
                        del bars_by_code[code]
                        continue
                    total = sum(b.amount_wan for b in bars_by_code[code])
                    if abs(total - want) / want > RECONCILE_TOL:
                        reconcile["mismatched"].append(code)
                        dropped[code] = (
                            f"对账超差: 分钟合计 {total:.2f} 万 vs TQ 日线 {want:.2f} 万"
                            f"（容差 {RECONCILE_TOL:.1%}）"
                        )
                        del bars_by_code[code]
                        continue
                    reconcile["compared"] += 1
            # reconciled 的含义收紧为「**写入集**里每一只都拿到了参照且都在容差内」；
            # verified 更严：候选集里没有任何一只因缺参照/超差被丢掉。
            entry["reconciled"] = bool(
                not entry.get("reconcile_error")
                and reconcile["compared"] > 0
                and reconcile["compared"] == len(bars_by_code)
            )
            entry["reconcile_compared"] = reconcile["compared"]
            entry["reconcile_missing"] = reconcile["missing"]
            entry["reconcile_mismatched"] = reconcile["mismatched"]

        entry["verified"] = bool(
            entry["reconciled"]
            and candidates
            and not reconcile["missing"]
            and not reconcile["mismatched"]
        )

        entry["etfs"] = len(bars_by_code)
        entry["dropped"] = dropped
        if len(bars_by_code) < gate:
            entry.update(reason="insufficient_etfs", gate=gate)
            days_report.append(entry)
            continue
        if require_verified and not entry["verified"]:
            # CLI 默认路径：没有可证实的对账就不写。部分可证实（丢了几只）同样拒绝 ——
            # 宁可整天不补，也不要把一个「说不清是谁的」日子留在库里。
            entry.update(
                reason="unverified",
                gate=gate,
                rows_planned=sum(len(b) for b in bars_by_code.values()),
            )
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
            # 写 0 行只可能是「并发/先到者已占住这一天」。区分完整占用与残缺占用：
            # 残缺日必须报失败（partial），不能因为 COUNT(*)>0 就当成已补过。
            try:
                ov = existing_overview(eng, day, min_codes=gate)
                entry.update(
                    reason="date_already_ingested" if ov["complete"] else "date_partially_ingested",
                    existing_rows=ov["rows"],
                    existing_codes=ov["codes"],
                    existing_sources=ov["sources"],
                    existing_minutes=ov["minutes"],
                )
            except Exception:  # noqa: BLE001
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
#: 注意：`date_already_ingested` 只在现有数据**完整**（单来源 + 240 分钟 + 每只 240 行
#: + 覆盖达标）时才会出现；残缺日一律是 `date_partially_ingested` → 退出码 1。
_BENIGN_SKIP = {"date_already_ingested", "non_trading_day", "not_closed_day"}


def exit_code(report: Mapping[str, Any]) -> int:
    """0 = 每个请求日要么写了/能写，要么属于正当跳过；1 = 有真正没补上的。

    残缺日（`date_partially_ingested`）、对账不可证实（`unverified`）、取数/写库失败
    都返回 1 —— 运维脚本必须能靠退出码分辨「补好了」和「看起来有行其实只有半天」。
    """
    bad = [
        d for d in report.get("days", [])
        if d.get("action") not in ("written", "planned") and d.get("reason") not in _BENIGN_SKIP
    ]
    return 0 if not bad else 1


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m src.collectors.em_etf_minute",
        description="东财 trends2 补 ETF 分钟缺口（默认 dry-run 不写库；写库默认要求可证实对账）",
    )
    ap.add_argument("--dates", help="逗号分隔的 YYYYMMDD")
    ap.add_argument("--gaps", type=int, default=0, help="先盘查最近 N 个交易日的缺口并补这些日期")
    ap.add_argument(
        "--days",
        type=int,
        default=5,
        help="--gaps 的盘查窗口（默认 5 个交易日）；东财只能回最近 5 个交易日（含当日），"
        "已收盘可补的通常只有 4 个",
    )
    ap.add_argument("--write", action="store_true", help="真的写库（缺省只演练）")
    ap.add_argument(
        "--no-reconcile",
        action="store_true",
        help="跳过 TQ 日线对账（显式放弃可证实性；报告 verified=False，仍可写库）",
    )
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
    report = backfill(
        dates,
        engine=engine,
        dry_run=not args.write,
        daily_ref=daily_ref,
        require_verified=not args.no_reconcile,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return exit_code(report)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
