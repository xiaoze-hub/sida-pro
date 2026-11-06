"""国家队 ETF 看板采集器（TDX/TQ 口径，2026-11-05）。

要对标的是「20 只重点宽基 ETF 每分钟成交额加总 → 判断国家队是否在托底」这套口径。
实测（生产容器 → 172.27.16.1:17709）结论决定了两件事：

1. **盘中分钟层 100% 由 TQ 提供，且成本极低**：
   `get_market_data(period="1m", stock_list=[20 只])` **一次调用 0.15s 返回 20/20**，
   不是"每只一个接口"。原文担心的"21 个接口 × 上千人刷新被限流"在本地网关不存在
   （零配额、零外部依赖）。可按 `start_time/end_time` 取任意历史交易日的 240 根分钟线
   （实测回溯到 2026-09-01 正常），因此"今日 vs 过去 20 日同期均值"可直接算，不必等累积。

2. **净申购只有市场级是真的，单只份额是假的**：
   `get_scjy_value(SC8)` 给全市场 `[ETF规模(亿元), 当日净申购(亿元)]`，日频，回溯到 2023-10
   —— 正是这类看板的起点。而 `get_gb_info(gb_type=1)` 对 ETF 返回的 `Zgb` 是
   **当前份额回填到每一天**：实测 6 只主流 ETF × 63 个交易日 **0 次变化**，
   拿它算单只净申购必然恒为 0。所以本模块：
   - 净申购一律走 SC8（市场级），落 `market_sentiment_daily.etf_scale/etf_net_sub`；
   - **预留 `single_share_source` 开关**（默认 None）：将来接 Tushare/交易所份额时填这里，
     不改变上层 API 契约。未接时 API 显式返回 `single_share_supported=False`，不伪造。

单位口径（实测标定，勿按文档猜）：
- `get_market_data` 的 `Amount` = **万元**（510300 日线 1,206,621.63 万元，与
  2,951,453,184 股 × 4.085 元 ≈ 120.6 亿元精确吻合）
- `Volume` = **股**
- SC8 两列 = **亿元**
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

#: 20 只重点宽基 ETF（名称取 TQ `get_stock_info` 实测值，不臆造）。
#: 分 12 类：11 个宽基指数 + 1 个「行业主题」桶。
ETF_BASKET: tuple[tuple[str, str, str], ...] = (
    ("510300.SH", "沪深300ETF华泰柏瑞", "沪深300"),
    ("159919.SZ", "沪深300ETF嘉实", "沪深300"),
    ("510310.SH", "沪深300ETF易方达", "沪深300"),
    ("510330.SH", "沪深300ETF华夏", "沪深300"),
    ("510050.SH", "上证50ETF华夏", "上证50"),
    ("510500.SH", "中证500ETF南方", "中证500"),
    ("512100.SH", "中证1000ETF南方", "中证1000"),
    ("159845.SZ", "中证1000ETF华夏", "中证1000"),
    ("563300.SH", "中证2000ETF华泰柏瑞", "中证2000"),
    ("512050.SH", "A500ETF华夏", "中证A500"),
    ("159352.SZ", "A500ETF南方", "中证A500"),
    ("588000.SH", "科创50ETF华夏", "科创50"),
    ("159915.SZ", "创业板ETF易方达", "创业板"),
    ("159949.SZ", "创业板50ETF华安", "创业板50"),
    ("510180.SH", "上证180ETF华安", "上证180"),
    ("159901.SZ", "深证100ETF易方达", "深证100"),
    ("512880.SH", "证券ETF国泰", "行业主题"),
    ("512480.SH", "半导体ETF国联安", "行业主题"),
    ("512010.SH", "医药ETF易方达", "行业主题"),
    ("515790.SH", "光伏ETF华泰柏瑞", "行业主题"),
)

#: 指数对照（上证综指），分钟级同图对比用。
BENCHMARK_INDEX = "999999.SH"
BENCHMARK_NAME = "上证综指"

#: 托底日判定阈值（亿元）——与公开口径一致：单日净流入超 100 亿视为托底日。
SUPPORT_FLOW_THRESHOLD_YI = 100.0

#: 单只份额数据源开关。None = 仅市场级 SC8（当前实现）；非 None 时由未来接入方填写标识
#: （如 "tushare_fund_share"），API 会据它决定是否声明 single_share_supported。
single_share_source: str | None = None

#: 分钟落库保留交易日数（20 日基线 + 余量）。
MINUTE_RETENTION_DAYS = 30


# ── 元数据查询（纯函数，前端/API 共用） ────────────────────────────────────

def categories() -> list[str]:
    """按配置顺序返回去重后的分类名。"""
    out: list[str] = []
    for _code, _name, cat in ETF_BASKET:
        if cat not in out:
            out.append(cat)
    return out


def basket_payload() -> list[dict[str, str]]:
    return [{"code": c, "name": n, "category": cat} for c, n, cat in ETF_BASKET]


def name_of(code: str) -> str:
    for c, n, _cat in ETF_BASKET:
        if c == code:
            return n
    return code


# ── TQ 取数 ───────────────────────────────────────────────────────────────

def _rpc(method: str, params: dict, timeout: float = 20.0):
    """单一网络缝（测试打桩点）。与 tdx_boards._rpc 同源，勿另造。"""
    from marketdata.vendors.tq import tq_rpc

    return tq_rpc(method, params, timeout=timeout)


def _num(v: Any) -> float | None:
    try:
        f = float(str(v).strip())
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _hhmm(raw: Any) -> str | None:
    """TQ 分钟时间字段 → 'HHMM'。实测有 '145800' / 145800 / '1458' 三种写法。"""
    if raw is None:
        return None
    s = str(raw).strip().split(".")[0]
    if not s.isdigit():
        return None
    if len(s) <= 4:  # HHMM / HMM
        return s.zfill(4)
    return s.zfill(6)[:4]  # HHMMSS / HMMSS


def fetch_minute(
    codes: list[str], trade_date: str, *, timeout: float = 20.0
) -> dict[str, list[tuple[str, float | None, int | None]]]:
    """取指定交易日全部分钟线 → {code: [(HHMM, 成交额万元, 成交量股), ...]}。

    `trade_date` 用 'YYYYMMDD'。**一次调用批量取全部标的**（20 只实测 0.15s）。
    TQ 不可用/无数据 → 抛异常或返回空 dict，由调用方决定是否写库（绝不写 0 冒充）。
    """
    payload = _rpc(
        "get_market_data",
        {
            "stock_list": list(codes),
            "period": "1m",
            "start_time": trade_date,
            "end_time": trade_date,
        },
        timeout=timeout,
    )
    if not isinstance(payload, dict):
        return {}

    out: dict[str, list[tuple[str, float | None, int | None]]] = {}
    for code in codes:
        node = payload.get(code)
        if not isinstance(node, dict):
            continue
        dates = node.get("Date") or []
        times = node.get("Time") or node.get("Minute") or []
        amounts = node.get("Amount") or []
        volumes = node.get("Volume") or []
        rows: list[tuple[str, float | None, int | None]] = []
        for i in range(len(dates)):
            if str(dates[i]) != trade_date:
                continue
            hm = _hhmm(times[i]) if i < len(times) else None
            if not hm:
                continue
            amt = _num(amounts[i]) if i < len(amounts) else None
            vol = _num(volumes[i]) if i < len(volumes) else None
            rows.append((hm, amt, int(vol) if vol is not None else None))
        if rows:
            out[code] = rows
    return out


def fetch_index_minute(trade_date: str, *, timeout: float = 20.0) -> list[tuple[str, float | None, float | None]]:
    """上证综指同分钟线 → [(HHMM, 点位, 成交额万元)]。拿不到返回 []。"""
    got = fetch_minute([BENCHMARK_INDEX], trade_date, timeout=timeout)
    rows = got.get(BENCHMARK_INDEX) or []
    # fetch_minute 只回 (HHMM, amount, volume)；指数要的是收盘点位，这里再取一次 Close
    payload = _rpc(
        "get_market_data",
        {"stock_list": [BENCHMARK_INDEX], "period": "1m",
         "start_time": trade_date, "end_time": trade_date},
        timeout=timeout,
    )
    closes: dict[str, float | None] = {}
    if isinstance(payload, dict):
        node = payload.get(BENCHMARK_INDEX) or {}
        if isinstance(node, dict):
            ds = node.get("Date") or []
            ts = node.get("Time") or []
            cs = node.get("Close") or []
            for i in range(len(ds)):
                if str(ds[i]) != trade_date or i >= len(ts):
                    continue
                hm = _hhmm(ts[i])
                if hm:
                    closes[hm] = _num(cs[i]) if i < len(cs) else None
    return [(hm, closes.get(hm), amt) for hm, amt, _vol in rows]


def fetch_sc8(start_date: str, end_date: str, *, timeout: float = 30.0) -> list[tuple[str, float | None, float | None]]:
    """市场级 ETF 规模/净申购 → [(YYYYMMDD, 规模亿元, 净申购亿元)]。

    这是**唯一**可信的净申购来源：单只 ETF 的 `get_gb_info` 份额是当前值回填，不可用。
    """
    payload = _rpc(
        "get_scjy_value",
        {"table_list": ["SC8"], "code": BENCHMARK_INDEX,
         "start_time": start_date, "end_time": end_date},
        timeout=timeout,
    )
    rows = None
    if isinstance(payload, dict):
        rows = payload.get("SC8") or payload.get("Value")
    if not isinstance(rows, list):
        return []
    out: list[tuple[str, float | None, float | None]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        d = str(item.get("Date") or "").strip()
        vals = item.get("Value")
        if not d or not isinstance(vals, list):
            continue
        scale = _num(vals[0]) if len(vals) > 0 else None
        net = _num(vals[1]) if len(vals) > 1 else None
        out.append((d, scale, net))
    return out


# ── 落库 ─────────────────────────────────────────────────────────────────

def write_minute(trade_date: str, data: dict[str, list[tuple[str, float | None, int | None]]]) -> int:
    """写 etf_minute_flow（幂等 upsert）。返回写入行数；失败返回 0 并记 warning。"""
    if not data:
        return 0
    try:
        from sqlalchemy import text

        from src.db.dialect import upsert_sql
        from src.web.database import engine

        cols = ["trade_date", "minute", "etf_code", "amount_wan", "volume", "updated_at"]
        sql = upsert_sql(
            "etf_minute_flow", cols, ["trade_date", "minute", "etf_code"],
            ["amount_wan", "volume", "updated_at"],
        )
        now = datetime.now()
        params: list[dict[str, Any]] = []
        for code, rows in data.items():
            for hm, amt, vol in rows:
                params.append({
                    "trade_date": trade_date, "minute": hm, "etf_code": code,
                    "amount_wan": amt, "volume": vol, "updated_at": now,
                })
        if not params:
            return 0
        with engine.begin() as conn:
            conn.execute(text(sql), params)
        return len(params)
    except Exception as e:  # noqa: BLE001
        logger.warning("[etf-watch] 分钟数据写库失败(%s): %s", trade_date, e)
        return 0


def write_sc8(rows: list[tuple[str, float | None, float | None]]) -> int:
    """把 SC8 写回 market_sentiment_daily（只覆盖 etf_scale/etf_net_sub 两列）。

    该表以 trade_date 为主键（字符串 YYYYMMDD）；行不存在时不插入 —— 情绪序列的
    行集由 SC3 交易日骨架决定，本模块只补列不造行，避免污染情绪基线。
    """
    if not rows:
        return 0
    try:
        from sqlalchemy import text

        from src.web.database import engine

        n = 0
        with engine.begin() as conn:
            for d, scale, net in rows:
                res = conn.execute(
                    text(
                        "UPDATE market_sentiment_daily "
                        "SET etf_scale = :s, etf_net_sub = :n "
                        "WHERE trade_date = :d"
                    ),
                    {"s": scale, "n": net, "d": d},
                )
                n += int(res.rowcount or 0)
        return n
    except Exception as e:  # noqa: BLE001
        logger.warning("[etf-watch] SC8 回写失败: %s", e)
        return 0


# ── 调度入口 ─────────────────────────────────────────────────────────────

def _now() -> datetime:
    """当前时间（单一缝：测试用 monkeypatch 固定"现在是哪天/几点"）。"""
    return datetime.now()


def today_str() -> str:
    return _now().strftime("%Y%m%d")


def recent_trade_dates(n: int, *, end: date | None = None) -> list[str]:
    """最近 n 个交易日（借项目统一交易日历，节假日/调休感知）。"""
    from src.core.trading_calendar import is_trading_day

    out: list[str] = []
    d = end or date.today()
    guard = 0
    while len(out) < n and guard < n * 3 + 30:
        if d.weekday() < 5 and is_trading_day(d):
            out.append(d.strftime("%Y%m%d"))
        d -= timedelta(days=1)
        guard += 1
    return list(reversed(out))


def ingest_day(trade_date: str) -> dict:
    """采集并落库单日分钟数据。返回 {date, rows, etfs, ok}。"""
    codes = [c for c, _n, _cat in ETF_BASKET]
    try:
        data = fetch_minute(codes, trade_date)
    except Exception as e:  # noqa: BLE001
        logger.warning("[etf-watch] %s 取数失败: %s", trade_date, e)
        return {"date": trade_date, "rows": 0, "etfs": 0, "ok": False, "error": str(e)[:160]}
    rows = write_minute(trade_date, data)
    return {"date": trade_date, "rows": rows, "etfs": len(data), "ok": rows > 0}


def run_intraday() -> dict:
    """盘中每分钟跑一次：只采当天，且只在 09:25–15:05 窗口内做事。

    窗口外的 15 小时里每分钟再拉一次全天分钟线是纯浪费（0.15s 取数 + 4800 行 upsert），
    收盘后的定型由 `run_close` 负责。
    """
    from src.core.trading_calendar import is_trading_day

    now = _now()
    today = now.date()
    if today.weekday() >= 5 or not is_trading_day(today):
        return {"skipped": "non-trading-day", "date": today.strftime("%Y%m%d")}
    hm = now.strftime("%H%M")
    if not ("0925" <= hm <= "1505"):
        return {"skipped": "out-of-session", "date": today.strftime("%Y%m%d"), "at": hm}
    return ingest_day(today.strftime("%Y%m%d"))


def run_close() -> dict:
    """收盘定型（交易日 15:35）：补采当日完整分钟线 + 回补当日 SC8。幂等。"""
    from src.core.trading_calendar import is_trading_day

    today = _now().date()
    if today.weekday() >= 5 or not is_trading_day(today):
        return {"skipped": "non-trading-day", "date": today.strftime("%Y%m%d")}
    day = today.strftime("%Y%m%d")
    out = ingest_day(day)
    try:
        out["sc8"] = run_sc8_backfill(start_date=(today - timedelta(days=7)).strftime("%Y%m%d"))
    except Exception as e:  # noqa: BLE001
        out["sc8"] = {"ok": False, "error": str(e)[:120]}
    return out


def ensure_backfilled(days: int = MINUTE_RETENTION_DAYS) -> dict:
    """启动自愈：表为空时回补最近 N 个交易日的分钟线（首次上线/换库）。

    已有数据则只补「缺掉的最近 3 个交易日」——发版往往发生在收盘后，重启即补。
    """
    try:
        from sqlalchemy import text

        from src.web.database import engine

        with engine.connect() as conn:
            row = conn.execute(text("SELECT COUNT(*) AS n FROM etf_minute_flow")).fetchone()
        have = int(row[0]) if row else 0
    except Exception as e:  # noqa: BLE001
        logger.warning("[etf-watch] 启动自愈查库失败: %s", e)
        return {"ok": False, "error": str(e)[:120]}

    if have == 0:
        return {"mode": "full", **run_backfill(days)}
    return {"mode": "tail", **run_backfill(days=3)}


def run_backfill(days: int = MINUTE_RETENTION_DAYS) -> dict:
    """回补最近 N 个交易日的分钟数据（首次上线/断档修复用）。

    20 日基线需要 20 个交易日的完整分钟线；每天一次批量调用（实测 0.15s），
    30 天约 5s 取数 + 落库。非交易日不采。
    """
    dates = recent_trade_dates(days)
    detail = [ingest_day(d) for d in dates]
    ok = sum(1 for x in detail if x.get("ok"))
    return {"days": len(dates), "ok_days": ok, "rows": sum(int(x.get("rows") or 0) for x in detail),
            "detail": detail[-5:]}


def run_sc8_backfill(start_date: str = "20231001") -> dict:
    """回补市场级 ETF 规模/净申购（SC8），补齐到最近交易日。"""
    try:
        rows = fetch_sc8(start_date, today_str())
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:160], "rows": 0}
    n = write_sc8(rows)
    return {"ok": n > 0, "fetched": len(rows), "updated": n, "start": start_date}
