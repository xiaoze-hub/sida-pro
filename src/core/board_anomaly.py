"""板块异动记录(2026-11-11)。

背景: 板块热力图顶部已有「板块异动」芯片 —— 但那是**当前快照**算的: 10:32 的一次
急拉, 10:35 刷新就消失了, 事后完全查不到。本模块把交易时段内的板块快照与命中阈值的
事件落库, 让"异动"变成可回溯的记录。

口径(与前端芯片同一套阈值, 见 frontend/packages/biz-ui/src/lib/board-heatmap.ts):
- 急拉/急跌: 5 分钟涨速 |speed| ≥ 0.5%（板块指数级别, 5 分钟 0.5% 已经不小）
- 放量: 量比代理 ≥ 2.0（今日成交额 vs 前 5 日均额 × 时段进度）

诚实边界:
- 只写**采样到的**事实。快照缺价/缺额 → 该板块这轮不参与判定, 不补 0、不猜。
- 采样中断(进程重启/网关挂/非交易时段)不会补点, 记录里就是空的 —— API 的 coverage
  会如实给出首次/末次采样时刻与采样轮数, 页面据此标注「记录自…」。
- 涨速必须有 5 分钟±容忍窗内的上一端快照才算得出; 重启后第一轮算不出 → 不记急拉,
  宁可漏记也不凭空造。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

from sqlalchemy import text

logger = logging.getLogger(__name__)

_CST = ZoneInfo("Asia/Shanghai")

# ── 阈值(与热力图芯片一致, 改动必须同时改前端 board-heatmap.ts) ────────────────
SPEED_PCT = 0.5          # 涨速阈值(% / 5 分钟)
VOL_RATIO = 2.0          # 量比阈值
# 量比代理的分母是「前 5 日均额 × 时段进度」, 而进度有 0.05 的保底(见 tdx_boards
# volume_ratio_proxy)。开盘头十几分钟里, 集合竞价+早盘冲量让"今日额"轻松超过
# 2×那一点点期望值 → 全市场一起"放量"。实测: 收盘后把一整天额按 0.05 折算, 576 个
# 板块里有 3 个量比就 >2。首刷就刷屏的"异动"没有信息量, 所以进度 <10%(约 09:42 前)
# 不做放量判定 —— 涨速判定不受影响(它要 5 分钟历史, 09:35 之后本来才有)。
SPEED_WINDOW_S = 300.0   # 涨速窗口: 5 分钟
SPEED_TOL_S = 120.0      # 基准样本容忍偏差(采样间隔 2 分钟 → 目标 5 分钟取最近点)
DEDUPE_WINDOW_S = 900    # 同板块同类型 15 分钟内只记一条
VOL_MIN_PROGRESS = 0.10  # 进度低于此值(约 09:42 前)不判放量, 否则早盘全员"放量"
SCAN_INTERVAL_S = 120    # 采样间隔

# 保留期(交易日): 快照只为算涨速, 短期足够; 记录是给人看的, 留得久些。
SNAPSHOT_KEEP_DAYS = 3
RECORD_KEEP_DAYS = 20

KIND_LABEL: dict[str, str] = {"surge": "急拉", "dump": "急跌", "heavy_volume": "放量"}
_BOARD_TYPES = ("industry", "concept")


def label_of(kind: str) -> str:
    return KIND_LABEL.get(kind, kind)


def _f(v: Any) -> float | None:
    try:
        if v is None:
            return None
        out = float(v)
    except (TypeError, ValueError):
        return None
    return out if out == out and out not in (float("inf"), float("-inf")) else None



def _as_dt(v: Any) -> datetime | None:
    """库里的时间统一成 datetime。

    SQLite 没有原生时间类型, 读回来是字符串('2026-11-11 10:30:00.123456'); PG 读回来是
    datetime。两种都要能算涨速 —— 否则本地/CI(SQLite)永远算不出急拉, 只有生产(PG)才对,
    属于「测试绿、环境不同就静默失效」的典型坑。
    """
    if isinstance(v, datetime):
        return v
    if isinstance(v, str):
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f",
                    "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(v.strip(), fmt)
            except ValueError:
                continue
    return None

def trade_date_of(dt: datetime) -> str:
    """交易日字符串(YYYYMMDD), 与 etf_minute_flow 等表同格式。"""
    return dt.strftime("%Y%m%d")


def detect_events(
    current: dict[str, dict],
    history: dict[str, list[tuple[float, float]]],
    *,
    now_ts: float,
    progress: float,
    baseline: dict[str, float] | None = None,
) -> list[dict]:
    """从「本轮快照 + 该板块近 10 分钟历史」判定异动, 纯函数(不碰库/网络)。

    - current: {block_code: {name, board_type, price, change_pct, amount, fund_net}}
    - history: {block_code: [(ts_epoch, price), ...]} 升序
    - 涨速复用 `compute_speed`(与热力图 speed 字段同一实现), 不是自己另写一套。
    返回事件 dict 列表(未去重、未落库), value 为触发的实际数值。
    """
    from src.core.tdx_boards import compute_speed, volume_ratio_proxy

    out: list[dict] = []
    for code, row in (current or {}).items():
        if not isinstance(row, dict):
            continue
        price = _f(row.get("price"))
        change_pct = _f(row.get("change_pct"))
        base = {
            "block_code": code,
            "name": row.get("name"),
            "board_type": row.get("board_type"),
            "change_pct": change_pct,
            "fund_net": _f(row.get("fund_net")),
        }
        if price is not None and price > 0:
            hist = list(history.get(code) or [])
            if hist and (not hist[-1] or hist[-1][1] != price):
                hist = hist + [(now_ts, price)]
            speed = compute_speed(hist, now_ts, window_s=SPEED_WINDOW_S, tol_s=SPEED_TOL_S)
            if speed is not None:
                if speed >= SPEED_PCT:
                    out.append({**base, "kind": "surge", "value": round(speed, 3), "window_min": 5})
                elif speed <= -SPEED_PCT:
                    out.append({**base, "kind": "dump", "value": round(speed, 3), "window_min": 5})
        vr = volume_ratio_proxy(_f(row.get("amount")), (baseline or {}).get(code), progress)
        # 早盘(进度<10%)不判放量: 分母被 0.05 保底压得极小, 量比必然虚高(见 VOL_MIN_PROGRESS 注释)。
        if vr is not None and vr >= VOL_RATIO and progress >= VOL_MIN_PROGRESS:
            out.append({**base, "kind": "heavy_volume", "value": round(vr, 2), "window_min": None})
    return out


def _upsert_snapshots(conn, trade_date: str, ts: datetime, rows: list[dict]) -> int:
    """写本轮快照(同 (ts, block_code) 幂等)。返回写入行数。"""
    n = 0
    for row in rows:
        try:
            conn.execute(
                text(
                    "INSERT INTO board_anomaly_snapshots "
                    "(trade_date, ts, block_code, board_type, price, change_pct, amount, fund_net) "
                    "VALUES (:d, :ts, :code, :bt, :px, :chg, :amt, :fund) "
                    "ON CONFLICT (ts, block_code) DO NOTHING"
                ),
                {
                    "d": trade_date,
                    "ts": ts,
                    "code": row["block_code"],
                    "bt": row.get("board_type") or "",
                    "px": _f(row.get("price")),
                    "chg": _f(row.get("change_pct")),
                    "amt": _f(row.get("amount")),
                    "fund": _f(row.get("fund_net")),
                },
            )
            n += 1
        except Exception as e:  # noqa: BLE001 — 单行失败不阻断整轮采样
            logger.debug("板块快照写入失败 %s: %s", row.get("block_code"), e)
    return n


def _insert_records(conn, trade_date: str, ts: datetime, events: list[dict]) -> int:
    """写异动事件(去重桶保证同板块同类型 15 分钟一条)。"""
    bucket = int(ts.timestamp() // DEDUPE_WINDOW_S)
    n = 0
    for ev in events:
        try:
            res = conn.execute(
                text(
                    "INSERT INTO board_anomaly_records "
                    "(trade_date, ts, block_code, name, board_type, kind, value, window_min, "
                    " change_pct, fund_net, bucket, source) "
                    "VALUES (:d, :ts, :code, :name, :bt, :kind, :val, :win, :chg, :fund, :bucket, :src) "
                    "ON CONFLICT (trade_date, block_code, kind, bucket) DO NOTHING"
                ),
                {
                    "d": trade_date,
                    "ts": ts,
                    "code": ev["block_code"],
                    "name": ev.get("name"),
                    "bt": ev.get("board_type"),
                    "kind": ev["kind"],
                    "val": _f(ev.get("value")),
                    "win": ev.get("window_min"),
                    "chg": _f(ev.get("change_pct")),
                    "fund": _f(ev.get("fund_net")),
                    "bucket": bucket,
                    "src": "tdx_tq",
                },
            )
            n += res.rowcount if res.rowcount and res.rowcount > 0 else 0
        except Exception as e:  # noqa: BLE001
            logger.debug("板块异动写入失败 %s/%s: %s", ev.get("block_code"), ev.get("kind"), e)
    return n


def _load_history(conn, trade_date: str, now: datetime, window_s: float = 600.0) -> dict[str, list[tuple[float, float]]]:
    """取近 window_s 秒的价格快照, 供涨速计算(进程重启后照样能算)。

    `now` 必须是 naive CST(与库中 ts 同口径); tz-aware 会被 PG 按会话时区折算。
    """
    from datetime import timedelta

    if now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    since = now - timedelta(seconds=window_s)
    try:
        rows = conn.execute(
            text(
                "SELECT block_code, ts, price FROM board_anomaly_snapshots "
                "WHERE trade_date = :d AND ts >= :since AND price IS NOT NULL ORDER BY ts"
            ),
            {"d": trade_date, "since": since},
        ).fetchall()
    except Exception as e:  # noqa: BLE001
        logger.debug("读取板块快照历史失败: %s", e)
        return {}
    hist: dict[str, list[tuple[float, float]]] = {}
    for code, ts, price in rows:
        px = _f(price)
        if px is None or px <= 0:
            continue
        dt = _as_dt(ts)
        if dt is None:
            continue
        # 库里存的是**应用时区(CST)的 naive 时间**; 直接 dt.timestamp() 会按宿主时区解释,
        # 宿主是 UTC 时整体偏 8 小时 → 5 分钟窗永远对不上 → 急拉永远记不到(静默失效)。
        epoch = (dt if dt.tzinfo else dt.replace(tzinfo=_CST)).timestamp()
        hist.setdefault(str(code), []).append((epoch, px))
    return hist


def _delete_days(conn, table: str, days: list[str]) -> int:
    """按交易日字符串删行。占位符逐日展开(PG 的 ANY 在 SQLite 不可用)。"""
    if not days:
        return 0
    params = {f"d{i}": d for i, d in enumerate(days)}
    ph = ",".join(f":d{i}" for i in range(len(days)))
    res = conn.execute(text(f"DELETE FROM {table} WHERE trade_date IN ({ph})"), params)
    return res.rowcount or 0


def prune(conn, *, snapshots_keep_days: int = SNAPSHOT_KEEP_DAYS, records_keep_days: int = RECORD_KEEP_DAYS) -> dict:
    """清理过期数据: 快照按交易日数保留, 记录按交易日数保留(都不动当日)。"""
    out: dict[str, int] = {}
    try:
        days = [r[0] for r in conn.execute(
            text("SELECT DISTINCT trade_date FROM board_anomaly_snapshots ORDER BY trade_date DESC")
        ).fetchall()]
        out["snapshots_deleted"] = _delete_days(conn, "board_anomaly_snapshots", days[snapshots_keep_days:])
    except Exception as e:  # noqa: BLE001
        logger.debug("快照清理跳过: %s", e)
    try:
        days = [r[0] for r in conn.execute(
            text("SELECT DISTINCT trade_date FROM board_anomaly_records ORDER BY trade_date DESC")
        ).fetchall()]
        out["records_deleted"] = _delete_days(conn, "board_anomaly_records", days[records_keep_days:])
    except Exception as e:  # noqa: BLE001
        logger.debug("记录清理跳过: %s", e)
    return out


def sample_once(
    *,
    now: datetime | None = None,
    quotes_fn: Callable[[list[str]], dict[str, dict]] | None = None,
    sectors_fn: Callable[[], list[dict]] | None = None,
    baseline_fn: Callable[[list[str]], dict[str, float]] | None = None,
    progress_fn: Callable[[], float] | None = None,
    force: bool = False,
) -> dict:
    """采样一轮: 取全板块实时快照 → 落快照 + 判定异动落记录。

    force=False 时非交易时段直接跳过(返回 skipped), 不写任何东西 —— 收盘后价格是静止的,
    照写只会把"重复同一个点"当成正常采样, 让 coverage 撒谎。
    """
    from src.core.quote_snapshots import in_trading_window

    now = now or datetime.now(_CST)
    if not force and not in_trading_window(now):
        return {"ok": False, "skipped": "not_trading_window", "ts": now.isoformat()}

    td = trade_date_of(now)
    try:
        from src.core import tdx_boards

        sectors_fn = sectors_fn or tdx_boards.sector_items
        quotes_fn = quotes_fn or tdx_boards.board_quotes
        baseline_fn = baseline_fn or tdx_boards.amount_baseline
        progress_fn = progress_fn or tdx_boards.intraday_progress
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"tdx_unavailable:{e}"}

    try:
        sectors = [s for s in (sectors_fn() or []) if s.get("board_type") in _BOARD_TYPES]
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"sector_list_failed:{e}"}
    codes = [s["code"] for s in sectors if s.get("code")]
    if not codes:
        return {"ok": False, "reason": "no_sectors"}

    try:
        quotes = quotes_fn(codes) or {}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"quotes_failed:{e}"}
    # 只要有一个板块拿到价, 就算这一轮有效(单源失败不阻断, 与 board_quotes 契约一致)
    live = {
        s["code"]: {
            "name": s.get("name"),
            "board_type": s.get("board_type"),
            **(quotes.get(s["code"]) or {}),
        }
        for s in sectors
        if s.get("code") and (quotes.get(s["code"]) or {}).get("price")
    }
    if not live:
        return {"ok": False, "reason": "no_live_quotes", "ts": now.isoformat()}

    try:
        baseline = baseline_fn(codes) or {}
    except Exception as e:  # noqa: BLE001
        logger.debug("量比基准取数失败(本轮不放量判定): %s", e)
        baseline = {}
    try:
        progress = float(progress_fn())
    except Exception:  # noqa: BLE001
        progress = 0.5

    now_ts = now.timestamp()
    from src.db.session import engine

    with engine.begin() as conn:
        # ⚠️ 传 naive CST(与库里存的一致): 传 tz-aware 时 psycopg2 会带上 +08, PG 再按
        # 会话时区把参数折算成本地时间, 窗口整体平移 8 小时 —— 测试里恰好仍是"下界更早"
        # 所以看不出来, 换时区就会静默算不出涨速。
        history = _load_history(conn, td, now.replace(tzinfo=None))
        events = detect_events(live, history, now_ts=now_ts, progress=progress, baseline=baseline)
        rows = [{"block_code": c, **v} for c, v in live.items()]
        n_snap = _upsert_snapshots(conn, td, now.replace(tzinfo=None), rows)
        n_ev = _insert_records(conn, td, now.replace(tzinfo=None), events)
        if n_ev:
            logger.info("板块异动: 本轮 %d 个板块命中, 落库 %d 条", len(events), n_ev)

    return {
        "ok": True,
        "ts": now.isoformat(),
        "trade_date": td,
        "boards": len(rows),
        "snapshots": n_snap,
        "events": len(events),
        "inserted": n_ev,
    }

def read_events(
    db,
    *,
    trade_date: str,
    board_type: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """读某交易日的异动记录(时间倒序)。缺失字段原样给 None, 不补 0。"""
    sql = (
        "SELECT ts, block_code, name, board_type, kind, value, window_min, change_pct, fund_net, source "
        "FROM board_anomaly_records WHERE trade_date = :d"
    )
    params: dict[str, Any] = {"d": trade_date, "lim": max(1, min(int(limit), 200))}
    if board_type in _BOARD_TYPES:
        sql += " AND board_type = :bt"
        params["bt"] = board_type
    sql += " ORDER BY ts DESC, id DESC LIMIT :lim"
    rows = db.execute(text(sql), params).mappings().all()
    out: list[dict] = []
    for r in rows:
        dt = _as_dt(r["ts"])
        out.append(
            {
                "ts": (dt.replace(tzinfo=_CST) if dt and dt.tzinfo is None else dt).isoformat()
                if dt else None,
                "block_code": r["block_code"],
                "name": r["name"],
                "board_type": r["board_type"],
                "kind": r["kind"],
                "label": label_of(str(r["kind"])),
                "value": _f(r["value"]),
                "window_min": r["window_min"],
                "change_pct": _f(r["change_pct"]),
                "fund_net": _f(r["fund_net"]),
                "source": r["source"],
            }
        )
    return out


def read_coverage(db, *, trade_date: str, board_type: str | None = None) -> dict:
    """当日采样覆盖: 轮数(不同 ts 数)/首末时刻/板块数 + 采样间隔。

    这些数字就是「记录从哪一刻开始、中间有没有断」的凭据 —— 页面必须原样标注,
    不能因为列表为空就假装「今日无异常」。
    """
    where = "WHERE trade_date = :d"
    params: dict[str, Any] = {"d": trade_date}
    if board_type in _BOARD_TYPES:
        where += " AND board_type = :bt"
        params["bt"] = board_type
    row = db.execute(
        text(
            f"SELECT COUNT(DISTINCT ts) AS samples, MIN(ts) AS first_ts, MAX(ts) AS last_ts "
            f"FROM board_anomaly_snapshots {where}"
        ),
        params,
    ).mappings().first()
    boards = db.execute(
        text(
            "SELECT COUNT(DISTINCT block_code) AS n FROM board_anomaly_snapshots "
            "WHERE trade_date = :d AND ts = (SELECT MAX(ts) FROM board_anomaly_snapshots WHERE trade_date = :d)"
        ),
        {"d": trade_date},
    ).scalar()

    def _iso(v):
        dt = _as_dt(v)
        if dt is None:
            return None
        return (dt.replace(tzinfo=_CST) if dt.tzinfo is None else dt).isoformat()

    return {
        "samples": int((row or {}).get("samples") or 0),
        "first_ts": _iso((row or {}).get("first_ts")),
        "last_ts": _iso((row or {}).get("last_ts")),
        "boards": int(boards or 0),
        "interval_s": int(SCAN_INTERVAL_S),
    }


def latest_trade_date_with_data(db) -> str | None:
    """最近有采样的交易日(YYYYMMDD); 无数据返回 None。"""
    try:
        v = db.execute(text("SELECT MAX(trade_date) FROM board_anomaly_snapshots")).scalar()
    except Exception:  # noqa: BLE001
        return None
    return str(v) if v else None
