"""thsdk 板块数据采集与轮动计算(阶段2.1/2.2, v0.3.0)。

能力:
- fetch_ths_industry / fetch_ths_concept: 行业/概念列表(30 分钟 TTL 缓存)
- fetch_block_detail / fetch_block_constituents: 单个板块行情/成分股(1 小时 TTL 缓存)
- sync_boards_to_db: cron 每日拉列表 + 板块日线写入 Board / BoardDaily
- compute_rotation: 基于 BoardDaily 计算板块轮动排序(强度分 0-100)
- register_board_sync_job: 把 08:30 工作日同步任务挂到【已有】APScheduler 实例

设计要点:
- 复用 data_source/thsdk_l2.py 的 THSDKL2, 不重写 SDK 调用
- 异常容错: thsdk 调用失败返回 None + warning 日志, 不向调用方抛异常
- 缓存线程安全(锁 + TTL), 供 Web / cron / 测试共用
- SQLite / PostgreSQL 双兼容(纯 ORM, 无方言 SQL)
"""

from __future__ import annotations

import logging
import math
import threading
import time
from datetime import date as date_cls
from datetime import timedelta
from typing import Any, Optional

import pandas as pd

# thsdk 仅在部署机(国内正式服务器)安装; 本机/CI 可能缺失。
# 用容错导入保证: 模块可 import(不崩 Web 启动), 实际调 thsdk 时再报错。
try:
    from data_source.thsdk_l2 import THSDKL2, THS_PREFIX_BLOCK
except ImportError:  # pragma: no cover - 部署机才有真实 thsdk
    THSDKL2 = None  # type: ignore[assignment,misc]
    THS_PREFIX_BLOCK = "URFI"

from src.core.timezone import beijing_now_naive
from src.web.database import SessionLocal, acquire_write

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# TTL 缓存(线程安全)
# ---------------------------------------------------------------------------
_CACHE_TTL_INDUSTRY = 30 * 60        # 板块列表: 30 分钟
_CACHE_TTL_DETAIL = 60 * 60          # 板块详情/成分股: 1 小时

_cache: dict[str, tuple[Any, float]] = {}
_cache_lock = threading.Lock()

# 轮动强度分权重(满分 100)
_SCORE_W_MOMENTUM = 45   # 5 日累计涨幅(区间映射, 一半权重)
_SCORE_W_FUND = 30       # 当日资金净流入(区间映射)
_SCORE_W_CONSECUTIVE = 15  # 连续上涨天数(最多 5 天计满分)
_SCORE_W_BASE = 10       # 覆盖/数据完整度基础分


def clear_cache() -> None:
    """清空全部模块级 TTL 缓存(测试隔离/手动刷新用)。"""
    with _cache_lock:
        _cache.clear()
    logger.debug("thsdk_board 缓存已清空")


def _cache_get(key: str) -> Any:
    with _cache_lock:
        item = _cache.get(key)
        if item is None:
            return None
        value, expires_at = item
        if time.monotonic() >= expires_at:
            _cache.pop(key, None)
            return None
        return value


def _cache_set(key: str, value: Any, ttl: int) -> None:
    with _cache_lock:
        _cache[key] = (value, time.monotonic() + ttl)


# ---------------------------------------------------------------------------
# 数据清洗工具
# ---------------------------------------------------------------------------
def _json_safe(value: Any) -> Any:
    """把 numpy 标量 / NaN / Inf 归一成 JSON 安全值。"""
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, (int, str, bool)) or value is None:
        return value
    # numpy 数值类型(pandas 转换后的常见残留)
    try:
        import numpy as np  # noqa: PLC0415

        if isinstance(value, np.generic):
            item = value.item()
            if isinstance(item, float) and (math.isnan(item) or math.isinf(item)):
                return None
            return item
    except Exception:
        pass
    return value


def _records(df: Optional[pd.DataFrame]) -> Optional[list[dict]]:
    """DataFrame → list[dict], NaN 归一为 None; 空/异常返回 None。

    返回 None 表示数据不可用(调用方按容错规则处理), 返回 [] 表示确实为空。
    """
    if df is None:
        return None
    try:
        rows = df.to_dict("records")
        return [{k: _json_safe(v) for k, v in row.items()} for row in rows]
    except Exception as e:  # noqa: BLE001 - 数据层异常按容错处理
        logger.warning("thsdk DataFrame 转 dict 失败: %s", e)
        return None


def _normalize_board_row(row: dict) -> dict:
    """从 thsdk 行数据中提取 block_code / name(容忍列名差异)。"""
    rec = dict(row)
    code = (
        rec.get("代码")
        or rec.get("板块代码")
        or rec.get("block_code")
        or rec.get("code")
        or ""
    )
    name = (
        rec.get("名称")
        or rec.get("板块名称")
        or rec.get("name")
        or rec.get("板块名")
        or ""
    )
    rec["block_code"] = str(code)
    rec["name"] = str(name)
    if rec["block_code"] and not str(rec["block_code"]).startswith(THS_PREFIX_BLOCK):
        rec["block_code"] = f"{THS_PREFIX_BLOCK}{rec['block_code']}"
    return rec


def _client() -> THSDKL2:
    """惰性创建 thsdk 客户端(每次调用都是独立实例, 内部 with 模式建连)。

    部署机未安装 thsdk 时抛错(由上层 fetch 容错捕获, 返回 None)。
    """
    if THSDKL2 is None:
        raise RuntimeError("thsdk 未安装, 板块数据不可用")
    return THSDKL2()


# ---------------------------------------------------------------------------
# 板块列表(30 分钟缓存)
# ---------------------------------------------------------------------------
def fetch_ths_industry() -> Optional[list[dict]]:
    """同花顺行业列表 → list[dict], 30 分钟缓存。

    失败返回 None + warning(不抛异常)。
    """
    cached = _cache_get("industry")
    if cached is not None:
        return cached
    try:
        df = _client().get_ths_industry()
        rows = _records(df)
        if rows is None:
            raise ValueError("get_ths_industry 返回空 DataFrame")
        data = [_normalize_board_row(r) for r in rows if r.get("代码") or r.get("名称")]
        _cache_set("industry", data, _CACHE_TTL_INDUSTRY)
        logger.info("thsdk 行业列表拉取成功: %d 条", len(data))
        return data
    except Exception as e:  # noqa: BLE001 - 容错: 失败不抛
        logger.warning("thsdk 行业列表拉取失败: %s", e)
        return None


def fetch_ths_concept() -> Optional[list[dict]]:
    """同花顺概念列表 → list[dict], 30 分钟缓存。

    失败返回 None + warning(不抛异常)。
    """
    cached = _cache_get("concept")
    if cached is not None:
        return cached
    try:
        df = _client().get_ths_concept()
        rows = _records(df)
        if rows is None:
            raise ValueError("get_ths_concept 返回空 DataFrame")
        data = [_normalize_board_row(r) for r in rows if r.get("代码") or r.get("名称")]
        _cache_set("concept", data, _CACHE_TTL_INDUSTRY)
        logger.info("thsdk 概念列表拉取成功: %d 条", len(data))
        return data
    except Exception as e:  # noqa: BLE001
        logger.warning("thsdk 概念列表拉取失败: %s", e)
        return None


# ---------------------------------------------------------------------------
# 板块详情 / 成分股(1 小时缓存)
# ---------------------------------------------------------------------------
def fetch_block_detail(block_code: str) -> Optional[dict]:
    """板块行情详情(基础数据) → dict, 1 小时缓存。

    失败返回 None + warning(不抛异常)。block_code 为空直接返回 None。
    """
    if not block_code:
        logger.warning("fetch_block_detail: block_code 为空")
        return None
    key = f"detail:{block_code}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    try:
        detail = _client().get_block_market(block_code, "基础数据")
        if not detail:
            raise ValueError(f"get_block_market({block_code}) 返回空")
        safe = {k: _json_safe(v) for k, v in detail.items()}
        _cache_set(key, safe, _CACHE_TTL_DETAIL)
        return safe
    except Exception as e:  # noqa: BLE001
        logger.warning("thsdk 板块详情拉取失败 %s: %s", block_code, e)
        return None


def fetch_block_constituents(block_code: str) -> Optional[list[dict]]:
    """板块成分股 → list[dict], 1 小时缓存。

    失败返回 None + warning(不抛异常)。
    """
    if not block_code:
        logger.warning("fetch_block_constituents: block_code 为空")
        return None
    key = f"constituents:{block_code}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    try:
        df = _client().get_block_constituents(block_code)
        rows = _records(df)
        if rows is None:
            raise ValueError(f"get_block_constituents({block_code}) 返回空")
        _cache_set(key, rows, _CACHE_TTL_DETAIL)
        return rows
    except Exception as e:  # noqa: BLE001
        logger.warning("thsdk 板块成分股拉取失败 %s: %s", block_code, e)
        return None


# ---------------------------------------------------------------------------
# 板块日线指标提取(兼容 thsdk 字段名差异)
# ---------------------------------------------------------------------------
def _extract_block_metrics(detail: Optional[dict]) -> dict:
    """从板块行情 dict 提取 {change_pct, fund_net, volume}, 缺字段给 None。

    thsdk market_data_block 的字段名未公开, 这里按常见中文/英文名模糊匹配:
    - 涨跌幅: 涨跌幅 / 涨跌 / change_pct / 涨幅
    - 资金净流入: 净流入 / 主力净流入 / 资金净流入 / fund_net / 主力净额
    - 成交量: 成交量 / 成交额 / volume / 成交金额(板块量能参考用成交额更稳)
    """
    if not detail:
        return {"change_pct": None, "fund_net": None, "volume": None}

    def _first_num(*keys: str) -> Optional[float]:
        for k in keys:
            v = detail.get(k)
            if v is None:
                continue
            try:
                f = float(v)
                if math.isnan(f) or math.isinf(f):
                    return None
                return f
            except (TypeError, ValueError):
                continue
        return None

    return {
        "change_pct": _first_num("涨跌幅", "涨跌", "涨幅", "change_pct", "changePercent"),
        "fund_net": _first_num(
            "主力净流入", "资金净流入", "净流入", "主力净额", "fund_net", "main_net_inflow"
        ),
        "volume": _first_num("成交额", "成交金额", "成交量", "volume", "amount", "成交额(元)"),
    }


# ---------------------------------------------------------------------------
# DB 同步(阶段2.1: 列表 + 日线入库)
# ---------------------------------------------------------------------------
def sync_boards_to_db(db=None) -> dict:
    """拉取行业+概念列表写 Board, 每个板块拉详情写 BoardDaily。

    幂等: 同一 (block_code, date) 已存在则更新不重复插入。
    失败容错: 单个板块失败只记日志, 不中断整体。
    返回统计 dict, 不抛异常。
    """
    from src.web.models import Board, BoardDaily

    own_session = db is None
    session = db if db is not None else SessionLocal()
    stats = {
        "boards_upserted": 0,
        "daily_rows": 0,
        "skipped": 0,
        "failed_boards": [],
        "industry_failed": False,
        "concept_failed": False,
    }
    try:
        today = date_cls.today()

        industries = fetch_ths_industry()
        concepts = fetch_ths_concept()
        if industries is None:
            stats["industry_failed"] = True
        if concepts is None:
            stats["concept_failed"] = True
        if industries is None and concepts is None:
            logger.warning("[board sync] 行业/概念列表都拉取失败, 跳过本轮同步")
            return stats

        board_rows: list[tuple[str, str, str]] = []
        for rec in (industries or []):
            if rec.get("block_code"):
                board_rows.append((rec["block_code"], rec.get("name", ""), "industry"))
        for rec in (concepts or []):
            if rec.get("block_code"):
                board_rows.append((rec["block_code"], rec.get("name", ""), "concept"))

        # 去重(行业/概念可能重叠代码)
        seen: set[str] = set()
        deduped = []
        for code, name, btype in board_rows:
            if code in seen:
                continue
            seen.add(code)
            deduped.append((code, name, btype))

        lock = acquire_write()
        try:
            for code, name, btype in deduped:
                board = (
                    session.query(Board).filter(Board.block_code == code).first()
                )
                if board is None:
                    board = Board(block_code=code, board_type=btype)
                    session.add(board)
                    stats["boards_upserted"] += 1
                if name:
                    board.name = name
                board.board_type = btype
                board.last_synced_at = beijing_now_naive()

            # 每板块拉详情写日线(容量控制: 逐个拉, thsdk 内部自带限频/重试)
            for code, _name, _btype in deduped:
                try:
                    detail = fetch_block_detail(code)
                    metrics = _extract_block_metrics(detail)
                    if metrics["change_pct"] is None:
                        # 板块可能停牌/无成交, 跳过日线写入
                        stats["skipped"] += 1
                        continue
                    daily = (
                        session.query(BoardDaily)
                        .filter(
                            BoardDaily.block_code == code,
                            BoardDaily.date == today,
                        )
                        .first()
                    )
                    if daily is None:
                        daily = BoardDaily(
                            block_code=code, date=today, change_pct=0.0
                        )
                        session.add(daily)
                    daily.change_pct = metrics["change_pct"]
                    daily.fund_net = metrics["fund_net"]
                    daily.volume = metrics["volume"]
                    stats["daily_rows"] += 1
                except Exception as e:  # noqa: BLE001
                    logger.warning("[board sync] 板块 %s 日线写入失败: %s", code, e)
                    stats["failed_boards"].append(code)

            session.commit()
        finally:
            lock.release()
        logger.info(
            "[board sync] 完成: %d 板块入库, %d 行日线, %d 跳过, %d 失败",
            stats["boards_upserted"],
            stats["daily_rows"],
            stats["skipped"],
            len(stats["failed_boards"]),
        )
        return stats
    except Exception as e:  # noqa: BLE001 - 兜底: 同步失败不崩 cron
        logger.exception("[board sync] 同步异常: %s", e)
        session.rollback()
        return stats
    finally:
        if own_session:
            session.close()


def _sync_boards_in_worker() -> None:
    """cron worker: 在线程里跑同步(阻塞网络调用不进事件循环)。"""
    try:
        sync_boards_to_db()
    except Exception as e:  # noqa: BLE001
        logger.exception("[board sync] worker 异常: %s", e)


# ---------------------------------------------------------------------------
# 板块轮动(阶段2.2)
# ---------------------------------------------------------------------------
def _flow_fund_net(db, block_code: str, latest_date) -> Optional[float]:
    """当日资金净流入: 优先 thsdk_flow 表(如后续阶段落地), 否则 BoardDaily.fund_net。

    thsdk_flow 目前仓库内不存在(阶段2.3 可能新增), 这里做运行时探测,
    表存在且列名匹配则自动启用, 避免硬编码依赖尚不存在的表。
    """
    from sqlalchemy import inspect, text

    try:
        inspector = inspect(db.bind)
        if "thsdk_flow" in inspector.get_table_names():
            cols = {c["name"] for c in inspector.get_columns("thsdk_flow")}
            if {"block_code", "fund_net"} <= cols:
                date_col = "date" if "date" in cols else "trade_date"
                row = db.execute(
                    text(
                        f"SELECT fund_net FROM thsdk_flow "
                        f"WHERE block_code = :bc AND {date_col} = :d "
                        f"ORDER BY {date_col} DESC LIMIT 1"
                    ),
                    {"bc": block_code, "d": latest_date},
                ).first()
                if row and row[0] is not None:
                    try:
                        return float(row[0])
                    except (TypeError, ValueError):
                        return None
    except Exception as e:  # noqa: BLE001
        logger.debug("thsdk_flow 探测/读取失败, 回退 BoardDaily.fund_net: %s", e)
    return None


def compute_rotation(days: int = 5, db=None) -> list[dict]:
    """板块轮动排序: 基于 BoardDaily 近 N 天日线。

    每板块产出:
        block_code / name / rotation_score(0-100) / change_5d(5 日累计涨幅 %)
        fund_net(当日资金净流入) / consecutive_days(连续上涨天数)

    强度分 = 动量(45) + 资金(30) + 连续性(15) + 基础(10), 区间映射线性打分。
    排序按 rotation_score 降序。数据不足的板块按可用数据计算。
    """
    from src.web.models import Board, BoardDaily

    days = max(1, int(days))
    own_session = db is None
    session = db if db is not None else SessionLocal()
    try:
        start_date = date_cls.today() - timedelta(days=days * 2)  # 留宽裕, 覆盖节假日
        rows = (
            session.query(BoardDaily, Board)
            .join(Board, Board.block_code == BoardDaily.block_code)
            .filter(BoardDaily.date >= start_date)
            .order_by(BoardDaily.block_code, BoardDaily.date)
            .all()
        )
        if not rows:
            return []

        # 按板块聚合(按日期升序)
        grouped: dict[str, dict[str, Any]] = {}
        for daily, board in rows:
            g = grouped.setdefault(
                daily.block_code,
                {"block_code": daily.block_code, "name": board.name or "", "series": []},
            )
            g["series"].append(
                {
                    "date": daily.date,
                    "change_pct": daily.change_pct,
                    "fund_net": daily.fund_net,
                }
            )

        results: list[dict] = []
        for g in grouped.values():
            series = [s for s in g["series"] if s["change_pct"] is not None]
            if not series:
                continue
            tail = series[-days:]
            # 5 日累计涨幅(复利口径)
            change_5d = 1.0
            for s in tail:
                change_5d *= 1 + (s["change_pct"] / 100.0)
            change_5d = (change_5d - 1.0) * 100.0
            # 当日资金净流入(取最新一天)
            latest = series[-1]
            fund_net = _flow_fund_net(session, g["block_code"], latest["date"])
            if fund_net is None and latest["fund_net"] is not None:
                fund_net = latest["fund_net"]
            # 连续上涨天数(从最新一天往回数, change_pct > 0)
            consecutive_days = 0
            for s in reversed(series):
                if s["change_pct"] > 0:
                    consecutive_days += 1
                else:
                    break
            results.append(
                {
                    "block_code": g["block_code"],
                    "name": g["name"],
                    "rotation_score": 0.0,  # 区间映射后回填
                    "change_5d": round(change_5d, 2),
                    "fund_net": round(fund_net, 2) if fund_net is not None else None,
                    "consecutive_days": consecutive_days,
                }
            )

        if not results:
            return []

        # 区间映射(线性归一) → 强度分 0-100
        changes = [r["change_5d"] for r in results]
        funds = [r["fund_net"] for r in results if r["fund_net"] is not None]
        c_min, c_max = min(changes), max(changes)
        f_min, f_max = (min(funds), max(funds)) if funds else (0.0, 0.0)

        for r in results:
            momentum = (
                _norm(r["change_5d"], c_min, c_max) if c_max > c_min else 0.5
            )
            fund_raw = r["fund_net"]
            if fund_raw is None:
                fund_part = 0.0
            elif f_max > f_min:
                fund_part = _norm(fund_raw, f_min, f_max)
            else:
                fund_part = 0.5 if fund_raw >= 0 else 0.0
            consecutive = min(r["consecutive_days"], 5) / 5.0
            score = (
                _SCORE_W_MOMENTUM * momentum
                + _SCORE_W_FUND * fund_part
                + _SCORE_W_CONSECUTIVE * consecutive
                + _SCORE_W_BASE
            )
            r["rotation_score"] = round(max(0.0, min(100.0, score)), 1)

        results.sort(key=lambda r: r["rotation_score"], reverse=True)
        return results
    except Exception as e:  # noqa: BLE001 - 容错: 轮动计算失败返回空列表
        logger.warning("板块轮动计算失败: %s", e)
        return []
    finally:
        if own_session:
            session.close()


def _norm(value: float, lo: float, hi: float) -> float:
    """线性映射 [lo, hi] → [0, 1] 并夹紧。"""
    if hi <= lo:
        return 0.5
    return max(0.0, min(1.0, (value - lo) / (hi - lo)))


# ---------------------------------------------------------------------------
# cron 集成(复用现有 APScheduler, 不新建调度器)
# ---------------------------------------------------------------------------
BOARD_SYNC_CRON = {"hour": 8, "minute": 30}
BOARD_SYNC_JOB_ID = "boards_daily_sync"


def register_board_sync_job(scheduler) -> None:
    """把板块每日同步任务注册到【已有】AsyncIOScheduler 实例。

    工作日(周一至五) 08:30 触发一遍 sync_boards_to_db。
    scheduler: 复用 server.py 启动的主调度器(scheduler.scheduler)即可,
    本函数不创建任何新调度器。可重复调用(同 id 会 replace)。
    """
    import asyncio

    async def _job() -> None:
        await asyncio.to_thread(_sync_boards_in_worker)

    scheduler.add_job(
        _job,
        "cron",
        day_of_week="mon-fri",
        hour=BOARD_SYNC_CRON["hour"],
        minute=BOARD_SYNC_CRON["minute"],
        id=BOARD_SYNC_JOB_ID,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    logger.info(
        "板块数据同步任务已注册: 工作日 %02d:%02d (job_id=%s)",
        BOARD_SYNC_CRON["hour"],
        BOARD_SYNC_CRON["minute"],
        BOARD_SYNC_JOB_ID,
    )