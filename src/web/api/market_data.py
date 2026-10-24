"""市场数据代理 API: 把 marketdata 包的关键方法暴露成只读 HTTP 端点。

设计原则(与 calendar.py 一致):
- 直接调用 marketdata 包的 vendor/Engine, **不重写数据源逻辑**
- config=None → vendor 自动从容器 DB 的 data_sources 表读 UI 维护的 key
  (即「设置 → 接口Key」配置的凭证, 改了立即生效, 无需重启)
- 供 8010 预测引擎在宿主机调用(宿主机无 marketdata 包, 经 8000 HTTP 取数)

暴露:
- GET /api/market-data/dragon-tiger/{date}  龙虎榜(ftshare vendor; P1 后台化+缓存)
- GET /api/market-data/dragon-tiger/{date}/status  龙虎榜单日抓取状态
- GET /api/market-data/dragon-tiger/range/status  龙虎榜多日范围抓取状态
- GET /api/market-data/capital-flow/{symbol}  资金流(经 MarketData Engine, 走 UI 配置 vendor)
- GET /api/market-data/fundamentals-detail/{symbol}  个股基本面明细合并端点(龙虎榜/两融/股东户数/分红/事件日历)
- GET /api/market-data/anomalies  东财异动池(交易所「严重异常波动」口径, 供首页 Dashboard)
- GET /api/market-data/hot-stocks  同花顺热榜(小时榜/日榜, 含 AI 归因, 供首页 Dashboard)
- GET /api/market-data/market-capital-flow  大盘资金(对齐同花顺APP口径, 顺手写 30s 快照入 DB)
- GET /api/market-data/market-capital-flow/history?hours=4  当日大盘资金快照序列
- GET /api/market-data/breadth-distribution  全市场涨跌幅 9 档分桶(P1 后台化, 立即返回缓存/空)
- GET /api/market-data/breadth-distribution/status  涨跌幅分布后台计算状态(前端轮询)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Callable

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text

from src.web.cache.biz_cache import biz_cache
from src.core.cn_gateway import gateway_url
from src.core.caliber import CAPITAL_FLOW_TAG as _CAPITAL_FLOW_TAG
from src.core.jobs import result_failure
from src.db.dialect import is_postgres

logger = logging.getLogger(__name__)

router = APIRouter()


# ──────────── P1 后台任务框架(慢接口异步化, 2026-09-18) ────────────
# 目标: 32s+ 的同步计算改为「立即返回缓存/空结果 + 后台任务计算 + 前端轮询」。
# 状态存进程内 dict(与 snapshot 节流同风格); 重启后自动重建, 无需落库。
# 单飞: 同一 job_key 已有 running 时不重复起线程。
class _BgJob:
    __slots__ = ("key", "status", "started_at", "finished_at", "error", "result_key", "lock")

    def __init__(self, key: str, result_key: str = ""):
        self.key = key
        self.result_key = result_key or key
        self.status = "running"  # running | succeeded | failed
        self.started_at = time.time()
        self.finished_at: float | None = None
        self.error = ""
        self.lock = threading.Lock()


_bg_jobs: dict[str, _BgJob] = {}
_bg_jobs_lock = threading.Lock()


def _bg_job_status(key: str) -> dict:
    with _bg_jobs_lock:
        job = _bg_jobs.get(key)
    if job is None:
        return {"status": "idle", "key": key}
    with job.lock:
        return {
            "key": job.key,
            "status": job.status,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "elapsed_s": round(
                (job.finished_at or time.time()) - job.started_at, 2
            ),
            "error": job.error,
            "result_key": job.result_key,
        }


def _bg_start(key: str, fn: Callable[[], dict], *, result_key: str = "",
              cache_ttl: int | None = None) -> bool:
    """启动后台计算(单飞)。返回 True=本次新起了线程, False=已在跑或刚完成。"""
    with _bg_jobs_lock:
        job = _bg_jobs.get(key)
        if job is not None and job.status == "running":
            return False
        job = _BgJob(key, result_key=result_key)
        _bg_jobs[key] = job

    def _runner() -> None:
        try:
            result = fn()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"后台任务 {key} 失败: {e}")
            with job.lock:
                job.status = "failed"
                job.error = str(e)[:300]
                job.finished_at = time.time()
            return
        # P0-1(2026-10-08): 返回体显式 ok=False 时也是失败 —— 过去只要 fn() 不抛就
        # 记 succeeded, 数据源全空/断链时作业面板显示成功而结果是空, 无人察觉。
        reason = result_failure(result)
        if reason is not None:
            logger.warning(f"后台任务 {key} 返回失败: {reason}")
            with job.lock:
                job.status = "failed"
                job.error = reason[:300]
                job.finished_at = time.time()
            return
        if cache_ttl and result_key:
            try:
                biz_cache.set_json(result_key, result, ttl=cache_ttl)
            except Exception:
                pass
        with job.lock:
            job.status = "succeeded"
            job.finished_at = time.time()

    threading.Thread(target=_runner, name=f"bg-{key[:40]}", daemon=True).start()
    return True



def _breadth_with_age(payload: dict) -> dict:
    """给响应补 age_s / stale（aged 数据必须显式标注，不许冒充新鲜）。"""
    captured = payload.get("captured_at") or ""
    age = None
    if captured:
        try:
            from datetime import datetime as _dt

            age = int((_dt.now() - _dt.fromisoformat(captured)).total_seconds())
        except Exception:
            age = None
    out = dict(payload)
    out["age_s"] = age
    out["stale"] = bool(age is not None and age > 900)
    return out


def _breadth_from_db(max_age_s: int = 1800) -> dict | None:
    """读最近一条快照。冷启动/重启后首页仍能秒出图（原先只能等 32s 重算）。"""
    try:
        from src.db.session import SessionLocal
        from src.db.models import BreadthDistributionSnapshot

        db = SessionLocal()
        try:
            row = (
                db.query(BreadthDistributionSnapshot)
                .order_by(BreadthDistributionSnapshot.captured_at.desc())
                .first()
            )
            if row is None:
                return None
            captured = row.captured_at
            age = None
            if captured is not None:
                from datetime import datetime as _dt

                if getattr(captured, "tzinfo", None) is not None:
                    captured = captured.replace(tzinfo=None)
                age = int((_dt.now() - captured).total_seconds())
                if age > max_age_s:
                    return None
            return {
                "count": row.valid,
                "total": row.total,
                "items": json.loads(row.items or "[]"),
                "note": "",
                "pending": False,
                "captured_at": captured.isoformat(timespec="seconds") if captured else "",
                "source": row.source or "db",
            }
        finally:
            db.close()
    except Exception as e:  # 读库失败不应阻断现算路径
        logger.warning("breadth 读库失败: %s", e)
        return None


def _breadth_save(payload: dict) -> None:
    """把算好的分布落库（同一 trade_date 每 15 分钟最多一条，避免撑爆表）。"""
    try:
        from datetime import datetime as _dt

        from src.db.models import BreadthDistributionSnapshot
        from src.db.session import SessionLocal

        if payload.get("pending") or not payload.get("count"):
            return
        db = SessionLocal()
        try:
            last = (
                db.query(BreadthDistributionSnapshot)
                .order_by(BreadthDistributionSnapshot.captured_at.desc())
                .first()
            )
            now = _dt.now()
            if last is not None and last.captured_at is not None:
                prev = last.captured_at
                if getattr(prev, "tzinfo", None) is not None:
                    prev = prev.replace(tzinfo=None)
                if (now - prev).total_seconds() < 900:
                    return
            db.add(
                BreadthDistributionSnapshot(
                    trade_date=now.strftime("%Y-%m-%d"),
                    captured_at=now,
                    total=int(payload.get("total") or 0),
                    valid=int(payload.get("count") or 0),
                    items=json.dumps(payload.get("items") or [], ensure_ascii=False),
                    source=str(payload.get("source") or "sina"),
                )
            )
            db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.warning("breadth 落库失败(忽略): %s", e)


def _empty_breadth() -> dict:
    return {
        "count": 0,
        "total": 0,
        "items": [{"bucket": b[2], "count": 0} for b in _BUCKET_BOUNDS],
        "note": "后台计算中, 请轮询 /breadth-distribution 或 /breadth-distribution/status",
        "pending": True,
    }


# ──────────── Task 1: 大盘资金快照(双方言兼容, v0.4.7) ────────────
# market-capital-flow 接口成功返回后, 异步写一条快照入 market_flow_snapshots;
# 同一进程 30s 节流(前端高频轮询不会撑爆表), 失败静默不阻断主流程。
# 建表由 B 层版本化迁移 src/web/migrations.py _m138 负责(W1.5/A5 收编)。
_SNAPSHOT_INTERVAL_S = 30.0
_snapshot_lock = threading.Lock()
_snapshot_last_write_ts: float = 0.0


def _try_write_snapshot_async(payload: dict) -> None:
    """后台线程写一条大盘资金快照(失败静默, try/except + logger.debug)。

    payload 来自 market-capital-flow 接口聚合后的 dict, 仅取需要的字段。
    30s 节流: 同一进程内连续调用时只写第一条, 避免前端高频轮询拖垮表。
    """
    def _runner() -> None:
        try:
            from src.web.database import engine
            with engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        INSERT INTO market_flow_snapshots
                            (total_main_flow, up_count, down_count, flat_count,
                             sh_flow, sz_flow, total_amount)
                        VALUES
                            (:total_main_flow, :up_count, :down_count, :flat_count,
                             :sh_flow, :sz_flow, :total_amount)
                        """
                    ),
                    {
                        "total_main_flow": payload.get("total_main_flow"),
                        "up_count": payload.get("up_count"),
                        "down_count": payload.get("down_count"),
                        "flat_count": payload.get("flat_count"),
                        "total_amount": payload.get("total_amount"),
                        "sh_flow": payload.get("sh_flow"),
                        "sz_flow": payload.get("sz_flow"),
                    },
                )
        except Exception as e:
            logger.debug(f"market_flow_snapshots 写入失败(静默): {e}")

    now = time.monotonic()
    with _snapshot_lock:
        global _snapshot_last_write_ts
        if now - _snapshot_last_write_ts < _SNAPSHOT_INTERVAL_S:
            return  # 节流窗口内, 跳过
        _snapshot_last_write_ts = now

    t = threading.Thread(target=_runner, name="mkt-flow-snapshot-writer", daemon=True)
    t.start()


# ──────── A-7(2026-09-29): 端点优先读已落库快照(够新且在交易时段) ────────
# 问题(审计 docs/TQ切换面审计_20260924.md 第五节第7条): 本端点的资金字段此前**只依赖
# 实时外呼**(国内网关→东财), 上游一旦不可用(已多次发生)资金字段整块缺失, 只能降级成
# 「仅涨跌家数」。而应用**本来就在持续落库大盘资金快照**(采样器每分钟 + 接口 30s 节流,
# 都写 market_flow_snapshots) —— 应先读自己的库, 与上游可用性解耦。
#
# 判定规则(纯读路径, 不改动任何降级语义):
#   1. 非交易时段 → 不读库, 照旧走实时链路;
#   2. 读 market_flow_snapshots 最新一行, 行龄(秒) > 阈值 → 过旧, 照旧走实时链路;
#   3. 命中(够新) → 直接用该行组装响应, 来源标注 source="db_snapshot" + 快照时间戳,
#      **绝不把库里的陈旧值冒充实时值**;
#   4. 库空/库不可达/资金字段缺失 → 回退实时链路(缺失即显式标注, 绝不补 0)。
# 阈值默认 300s(5 分钟), 可用环境变量 MARKET_FLOW_SNAPSHOT_FRESH_SEC 覆盖。
_SNAPSHOT_FRESH_SEC_DEFAULT = 300
_SNAPSHOT_FRESH_ENV = "MARKET_FLOW_SNAPSHOT_FRESH_SEC"


def _snapshot_fresh_sec() -> int:
    """快照新鲜度阈值(秒): 环境变量覆盖, 非法/非正一律回落默认 300。"""
    raw = os.environ.get(_SNAPSHOT_FRESH_ENV)
    if raw:
        try:
            v = int(float(raw))
            if v > 0:
                return v
        except (TypeError, ValueError):
            logger.debug("%s 非法(%r), 用默认 %ss", _SNAPSHOT_FRESH_ENV, raw,
                         _SNAPSHOT_FRESH_SEC_DEFAULT)
    return _SNAPSHOT_FRESH_SEC_DEFAULT


def _snapshot_age_sql() -> str:
    """方言安全的「行龄(秒)」SQL 片段。

    关键: 用 **DB 端 now** 与 ts 比较 —— ts 由 DB 默认 CURRENT_TIMESTAMP 写入(见
    _try_write_snapshot_async / market_flow_sampler), 与写入口径自洽, 避免在 Python
    侧猜 ts 是 UTC 还是本地时区而算错行龄。PG 走 EXTRACT, SQLite 走 julianday。
    方言只认 src/db/dialect.py::is_postgres()(禁裸方言布尔量, CI 门禁)。
    """
    if is_postgres():
        return "EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - ts))"
    return "(julianday('now') - julianday(ts)) * 86400.0"


def _read_fresh_db_snapshot() -> dict | None:
    """读已落库的最新大盘资金快照; 仅在「够新 ∩ 交易时段」时返回, 否则 None。

    返回 None = 应回退实时链路(非交易时段/库空/过旧/库不可达), 调用方维持原降级语义。
    行值一律如实透传: null 保留为 null(资金字段可能因网关故障而缺失), **绝不补 0**。
    """
    try:
        from src.core.quote_snapshots import in_trading_window

        if not in_trading_window():
            return None
    except Exception as e:  # noqa: BLE001
        logger.debug("交易时段判定不可用, 回退实时链路: %s", e)
        return None
    try:
        from src.web.database import engine

        with engine.connect() as conn:
            # 2026-10-20: total_amount 列由 _m207 引入。老库/迁移未落时**不能整条读失败**
            # （那会让首页静默回退实时外呼），退化为不带该列的查询，成交额显式 None。
            try:
                row = conn.execute(
                    text(
                        "SELECT ts, total_main_flow, up_count, down_count, flat_count, "
                        "sh_flow, sz_flow, total_amount, " + _snapshot_age_sql() + " AS age_sec "
                        "FROM market_flow_snapshots ORDER BY ts DESC LIMIT 1"
                    )
                ).fetchone()
                has_amount = True
            except Exception as col_err:  # noqa: BLE001 - 缺列/老 schema
                logger.debug("快照表缺 total_amount 列, 退化为无成交额查询: %s", col_err)
                row = conn.execute(
                    text(
                        "SELECT ts, total_main_flow, up_count, down_count, flat_count, "
                        "sh_flow, sz_flow, " + _snapshot_age_sql() + " AS age_sec "
                        "FROM market_flow_snapshots ORDER BY ts DESC LIMIT 1"
                    )
                ).fetchone()
                has_amount = False
    except Exception as e:  # noqa: BLE001
        logger.debug("大盘资金快照读库失败(回退实时链路): %s", e)
        return None
    if row is None:
        return None
    try:
        age = float(row[8] if has_amount else row[7]) if (row[8] if has_amount else row[7]) is not None else None
    except (TypeError, ValueError):
        age = None
    # 行龄算不出, 或过旧(留 5s 容差应对时钟微偏) → 不猜, 回退实时链路
    if age is None or age > _snapshot_fresh_sec() or age < -5:
        return None
    ts_val = row[0]
    ts_str = ts_val.isoformat() if hasattr(ts_val, "isoformat") else str(ts_val)
    return {
        "snapshot_ts": ts_str,
        "snapshot_age_sec": int(max(0, round(age))),
        "total_main_flow": float(row[1]) if row[1] is not None else None,
        "sh_flow": float(row[5]) if row[5] is not None else None,
        "sz_flow": float(row[6]) if row[6] is not None else None,
        "up_count": int(row[2]) if row[2] is not None else None,
        "down_count": int(row[3]) if row[3] is not None else None,
        "flat_count": int(row[4]) if row[4] is not None else None,
        "total_amount": (float(row[7]) if row[7] is not None else None) if has_amount else None,
    }


def _boards_to_inout(items: list[dict]) -> tuple[list[dict], list[dict]]:
    """板块 items → (流入榜 top10, 流出榜 top10)。net_inflow 缺失的板块不参与排序
    (此前 or 0.0 把"无数据"当 0, 会挤占榜单); 正/负筛选, 无正(负)则对应榜为空列表。"""
    valid = [b for b in (items or []) if isinstance(b, dict) and b.get("net_inflow") is not None]
    s = sorted(valid, key=lambda b: b["net_inflow"], reverse=True)
    inflow = [
        {"name": b.get("board_name"), "net_inflow": round(b["net_inflow"], 2),
         "change_pct": b.get("change_pct")}
        for b in s[:10]
        if b["net_inflow"] > 0
    ]
    outflow = [
        {"name": b.get("board_name"), "net_inflow": round(b["net_inflow"], 2),  # 亿(负=流出)
         "change_pct": b.get("change_pct")}
        for b in reversed(s[-10:])
        if b["net_inflow"] < 0
    ]
    return inflow, outflow


def _fresh_board_inout(board_type: str = "industry") -> tuple[list[dict], list[dict]] | None:
    """读够新的板块资金快照并派生流入/流出榜; 无够新快照/读取失败 → None(不编造)。"""
    try:
        from src.core import board_flow_sampler as _bfs

        snap = _bfs.read_fresh(board_type)
    except Exception:  # noqa: BLE001
        return None
    if not snap:
        return None
    return _boards_to_inout(snap.get("items") or [])




# 2026-10-20: 两市成交额兜底。上游 market-overview 的 total_amount 经常为 None（网关不返回该字段），
# 而首页把它当核心 KPI。用指数成交额补：上证(000001) + 深证成指(399001) 的 turnover 之和，
# 单位元 → 亿元。与同花顺口径一致（实测 11:00 约 8265 亿）。60s 进程内缓存，避免每次请求外呼。
_AMOUNT_CACHE: dict = {"ts": 0.0, "val": None}


def _tq_index_amount() -> tuple[float | None, str]:
    """两市成交额 —— **首选通达信 TQ 指数快照**(2026-10-20 用户铁律: 逆向出的客户端接口优先)。

    口径: 上证指数(999999.SH, 全沪市) + 深证综指(399106.SZ, 全深市) 的 `Amount`(万元) 之和。
    与腾讯指数口径实测相差 ~1%(通达信含集合竞价/盘后固定价格), 属正常口径差。
    只打 2 次 RPC, 单线程 17709 上可接受; 每次间隔 0.15s 避免突发。
    """
    try:
        from marketdata.vendors.tq import tq_rpc

        total_yuan = 0.0
        got = 0
        for idx, code in enumerate(("999999.SH", "399106.SZ")):
            if idx:
                time.sleep(0.15)
            resp = tq_rpc("get_market_snapshot", {"stock_code": code}) or {}
            # ⚠️ 2026-10-20 线上实测: 经 tq_rpc 拿到的指数快照是**扁平 dict**({Amount, Now, ...}),
            # 不带 Value 包裹; 而个股路径有时带。两种形状都要吃, 否则永远 tdx_tq_partial。
            v = resp.get("Value") if isinstance(resp, dict) and isinstance(resp.get("Value"), dict) else resp
            if not isinstance(v, dict) or v.get("Amount") is None:
                continue
            amt = v.get("Amount")
            if amt is None:
                continue
            try:
                total_yuan += float(amt) * 1e4  # 万元 → 元
                got += 1
            except (TypeError, ValueError):
                continue
        if got < 2:
            return None, "tdx_tq_partial"
        return round(total_yuan / 1e8, 1), "tdx_tq_index"
    except Exception as e:  # noqa: BLE001 - 兜底失败就走腾讯
        logger.debug("TQ 指数成交额失败: %s", e)
        return None, "tdx_tq_error"


def _total_amount_from_indices() -> float | None:
    """腾讯指数成交额口径(历史兜底实现, 测试契约沿用; 端点优先走 _refresh_amount_fallback)。"""
    return _refresh_amount_fallback()[0]


def _latest_tq_flow_row(max_age_sec: int = 1800) -> dict | None:
    """最近一次 TQ 全市场汇总(主力净流入)。超过 max_age_sec 视为过旧 → None(不拿旧数当实时)。

    返回 {"total_main_flow", "age_sec"}。读库失败 → None。
    """
    try:
        from sqlalchemy import text

        from src.web.database import engine

        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT total_main_flow, ts FROM market_flow_snapshots
                    WHERE total_main_flow IS NOT NULL AND source = 'tdx_tq'
                    ORDER BY ts DESC LIMIT 1
                    """
                )
            ).fetchone()
        if not row:
            return None
        val, ts = row[0], row[1]
        age = 0
        try:
            import datetime as _dt

            if ts.tzinfo is None:
                # 库里存的是**本地时间**(CST, naive)。当成 UTC 会算出 -28758s 的"未来快照"。
                ts = ts.replace(tzinfo=_dt.timezone(_dt.timedelta(hours=8)))
            age = int((_dt.datetime.now(_dt.timezone.utc) - ts).total_seconds())
        except Exception:  # noqa: BLE001
            age = 0
        if age > max_age_sec:
            return None
        return {"total_main_flow": float(val), "age_sec": age}
    except Exception as e:  # noqa: BLE001
        logger.debug("读取 TQ 资金快照失败: %s", e)
        return None


def _refresh_amount_fallback() -> tuple[float | None, str]:
    """成交额兜底链: TQ 指数(首选) → 腾讯指数(次选)。60s 缓存。"""
    now = time.time()
    if now - float(_AMOUNT_CACHE.get("ts") or 0) < 60 and _AMOUNT_CACHE.get("val") is not None:
        return _AMOUNT_CACHE["val"], _AMOUNT_CACHE.get("src") or "cache"

    val, src = _tq_index_amount()
    if val is None:
        try:
            from src.core.marketdata_client import get_market_data

            items = get_market_data().index_quotes(["sh000001", "sz399106"])
            total = 0.0
            got = 0
            for it in items or []:
                tv = it.get("turnover")
                if tv:
                    total += float(tv)
                    got += 1
            if got >= 2:
                val, src = round(total / 1e8, 1), "tencent_index"
        except Exception as e:  # noqa: BLE001
            logger.debug("腾讯指数成交额兜底失败: %s", e)
    if val is not None:
        _AMOUNT_CACHE.clear()
        _AMOUNT_CACHE.update({"ts": now, "val": val, "src": src})
    return val, src


def _snapshot_to_response(snap: dict) -> dict:
    """把库快照行组装成与实时分支同构的响应, 并**如实标注来源与口径**。

    字段名/结构与实时分支保持一致(前端不改), 仅新增来源标注字段; 快照表**未存**的
    字段(成交额/指数点位涨跌/创业板主力)一律显式置空 —— 不编造、不补 0。
    板块明细(2026-10-14 收口)改为从 board_flow_snapshots 读够新快照派生; 无够新
    板块快照时置空(不编造)。口径标签沿用现网口径(eastmoney4,
    见 docs/_frozen/caliber_matrix.md), 并显式标注为「由快照继承」而非行内自带。
    """
    has_funds = snap.get("total_main_flow") is not None
    board_inout = _fresh_board_inout("industry")
    if board_inout is not None:
        inflow_boards, outflow_boards = board_inout
    else:
        inflow_boards, outflow_boards = [], []
    resp = {
        # 资金类字段(快照表存了这些)
        "total_main_flow": snap.get("total_main_flow"),  # 亿
        "sh_flow": snap.get("sh_flow"),
        "sz_flow": snap.get("sz_flow"),
        # 快照表未存的字段: 显式置空(缺失标注, 不编造)
        "cyb_flow": None,
        "total_amount": snap.get("total_amount"),
        "sh": None,
        "sz": None,
        "cyb": None,
        # 板块明细: 从板块资金快照读库派生(收口前恒空)
        "inflow_boards": inflow_boards,
        "outflow_boards": outflow_boards,
        "board_source": "db_snapshot" if board_inout is not None else None,
        # 市场统计(家数快照表存了)
        "up_count": snap.get("up_count"),
        "down_count": snap.get("down_count"),
        "flat_count": snap.get("flat_count"),
        # 来源标注(A-7): 库读而非实时外呼 —— 绝不冒充实时值
        "source": "db_snapshot",
        "data_source": "db_snapshot",
        "data_origin": "market_flow_snapshots",
        "breadth_source": "db_snapshot",
        "snapshot_used": True,
        "snapshot_ts": snap.get("snapshot_ts"),
        "snapshot_age_sec": snap.get("snapshot_age_sec"),
        "timestamp": snap.get("snapshot_ts"),
        # 口径标签: 沿用现网口径, 并标注为快照继承(行内未自带 caliber 列)
        "caliber": _CAPITAL_FLOW_TAG.caliber,
        "direction_semantics": _CAPITAL_FLOW_TAG.direction_semantics,
        "caliber_label": "主力净流入: 东财四档·资金面参考, 禁用于主力意图判定 "
                         "(口径由已落库快照继承)",
        "caliber_source": "db_snapshot_inherited",
        "note": "读自已落库大盘资金快照(非实时外呼); 板块明细读板块资金快照; "
                "快照表未存成交额/指数点位, 已显式置空, 不编造。",
    }
    if not has_funds:
        # 库里最新快照仅含涨跌家数(网关故障期间采样器落的行) → 显式降级, 不补 0
        resp["degraded"] = True
        resp["note"] = ("库内最新快照仅含涨跌家数, 资金字段缺失(未编造 0); "
                        "资金面请参照实时链路或稍后重试。")
    return resp


@router.get("/dragon-tiger/range/status")
async def dragon_tiger_range_status(
    market: str = Query("CN", description="市场"),
    days: int = Query(10, ge=1, le=30, description="回溯天数(自然日)"),
):
    """龙虎榜多日范围后台抓取状态(供 fundamentals-detail 冷启动轮询)。

    注意: 必须注册在 /dragon-tiger/{trade_date} 之前, 否则 "range" 会被当成 trade_date。
    """
    key = _lhb_range_cache_key(market, days)
    job_key = f"lhb_range:{market}:{max(1, min(int(days), 30))}"
    cached = biz_cache.get_json(key)
    st = _bg_job_status(job_key)
    return {
        "market": market,
        "days": days,
        "job": st,
        "ready": cached is not None,
        "date_count": len((cached or {}).get("by_date") or {}) if cached else 0,
    }


@router.get("/dragon-tiger/{trade_date}")
async def dragon_tiger_proxy(
    trade_date: str,
    market: str = Query("CN", description="市场"),
    wait: int = Query(0, ge=0, le=15, description="缓存未命中时最多等待秒数(0=立即返回)"),
):
    """龙虎榜(经 marketdata dragon_tiger vendor, 主源东财 + ftshare 补席位)。

    trade_date: YYYYMMDD
    key 来自「设置→接口Key」配置的 data_sources(type=dragon_tiger), 实时生效。

    P1 异步化(2026-09-18):
    - 结果写入 biz_cache(默认 1h, 交易日数据日终不变)
    - 缓存未命中 → 启动后台任务抓取, 立即返回空 + pending=True
    - wait>0 时最多阻塞等待 wait 秒(兼容旧前端同步期望)
    - 轮询状态: GET /dragon-tiger/{date}/status

    2026-08-20: 东财 datacenter 有汇总(净买/原因/上榜明细)无席位, ftshare 有席位
    但需全市场翻页。合并:东财做主, ftshare 补同 symbol 行的席位字段。
    """
    cache_key = f"mkt:dragon_tiger:{market}:{trade_date}"
    job_key = f"dragon_tiger:{market}:{trade_date}"
    cache_ttl = 3600  # 龙虎榜日终不变, 1h 足够

    cached = biz_cache.get_json(cache_key)
    if cached is not None:
        return cached

    # 读侧(2026-10-14 落库收口): 该日已落库 → 直接读库返回(历史日永不回源),
    # 并回填响应缓存。仅当库里没有该日(冷启动/当日未落)才走下面的回源链路。
    db_payload = _dragon_tiger_from_db(trade_date, market)
    if db_payload is not None:
        biz_cache.set_json(cache_key, db_payload, ttl=cache_ttl)
        return db_payload

    def _compute() -> dict:
        from src.core.marketdata_client import get_market_data
        md = get_market_data()
        rows = md.dragon_tiger(date=trade_date, market=market) or []

        # 补席位: 直接调 ftshare vendor(Engine 跳过 enabled=0, 走旁路)
        try:
            from marketdata.vendors.ftshare import FtshareDragonTigerVendor
            _ft = FtshareDragonTigerVendor()
            ft_rows = _ft.fetch([], {"date": trade_date, "market": market}) or []
            ft_by_sym = {r.symbol: r for r in ft_rows if r.symbol}
        except Exception:
            ft_by_sym = {}

        # 2026-10-20 用户铁律「备弱用强」：**枚举靠东财**(TQ 无市场级榜单接口)，
        # **金额以 TQ 为准**(本地零配额、可与东财对账；东财网关 502 时金额常整块缺失)，
        # 席位名/上榜原因仍由东财+ftshare 提供。TQ 拿不到的票才用东财金额。
        tq_amt = {}
        try:
            tq_amt = _lhb_amounts_from_tq([getattr(r, "symbol", "") for r in rows], trade_date)
        except Exception as e:  # noqa: BLE001
            logger.warning("龙虎榜 TQ 金额补强失败(不改用东财): %s", e)

        items = []
        for i in rows:
            ft = ft_by_sym.get(i.symbol)
            sym = getattr(i, "symbol", "")
            em_buy = getattr(i, "buy_amt", None)
            em_sell = getattr(i, "sell_amt", None)
            em_net = getattr(i, "net_buy", None)
            tq_row = tq_amt.get(sym)
            if tq_row and (tq_row.get("buy_amt") is not None or tq_row.get("sell_amt") is not None):
                buy_amt, sell_amt, net_buy = tq_row["buy_amt"], tq_row["sell_amt"], tq_row["net_buy"]
                amt_source = "tdx_tq_gp02"
            else:
                buy_amt, sell_amt, net_buy = em_buy, em_sell, em_net
                amt_source = "eastmoney" if em_buy is not None or em_sell is not None else "unavailable"
            items.append(
                {
                    "trade_date": getattr(i, "trade_date", trade_date),
                    "symbol": sym,
                    "name": getattr(i, "name", ""),
                    "close": getattr(i, "close", None),
                    "change_pct": getattr(i, "change_pct", None),
                    "net_buy": net_buy,
                    "buy_amt": buy_amt,
                    "sell_amt": sell_amt,
                    # 对账字段：TQ 生效时保留东财值，两者不一致时一眼可见
                    "buy_amt_em": em_buy,
                    "sell_amt_em": em_sell,
                    "amount_source": amt_source,
                    "reason": getattr(i, "reason", None),
                    "top_buyers": list(getattr(ft, "top_buyers", []) or []) if ft else [],
                    "top_sellers": list(getattr(ft, "top_sellers", []) or []) if ft else [],
                }
            )
        # 拉到即落(2026-10-14 落库收口): 东财-ftshare 行按 (date, symbol) upsert;
        # 下次同请求直接读库, 历史日不再回源。
        if items:
            _used_tq = any(it.get("amount_source") == "tdx_tq_gp02" for it in items)
            _lhb_upsert_rows(
                items, market=market,
                source="em_ftshare+tq_amt" if _used_tq else "em_ftshare",
            )
        return {
            "trade_date": trade_date,
            "market": market,
            "count": len(items),
            "items": items,
            "pending": False,
            "source": "em_ftshare",
            "amount_source": "tdx_tq_gp02(缺失逐票回落东财)",
            "data_origin": "realtime",
        }

    started = _bg_start(job_key, _compute, result_key=cache_key, cache_ttl=cache_ttl)

    # 兼容路径: wait>0 时短暂等待后台完成(旧前端无需改造); 用 asyncio.sleep 不卡事件循环
    if wait > 0:
        deadline = time.time() + wait
        while time.time() < deadline:
            done = biz_cache.get_json(cache_key)
            if done is not None:
                return done
            st = _bg_job_status(job_key)
            if st.get("status") == "failed":
                raise HTTPException(502, f"数据源调用失败: {st.get('error', '')}")
            if st.get("status") == "idle" and not started:
                break
            await asyncio.sleep(0.3)
        cached = biz_cache.get_json(cache_key)
        if cached is not None:
            return cached

    return {
        "trade_date": trade_date,
        "market": market,
        "count": 0,
        "items": [],
        "pending": True,
        "note": "后台抓取中, 请轮询本接口或 /status",
    }


@router.get("/dragon-tiger/{trade_date}/status")
async def dragon_tiger_status(
    trade_date: str,
    market: str = Query("CN", description="市场"),
):
    """龙虎榜后台抓取状态(pending/succeeded/failed + 是否已有缓存)。"""
    cache_key = f"mkt:dragon_tiger:{market}:{trade_date}"
    job_key = f"dragon_tiger:{market}:{trade_date}"
    cached = biz_cache.get_json(cache_key)
    st = _bg_job_status(job_key)
    return {
        "trade_date": trade_date,
        "market": market,
        "job": st,
        "ready": cached is not None,
        "count": (cached or {}).get("count", 0) if cached else 0,
    }


@router.get("/capital-flow/{symbol}")
async def capital_flow_proxy(
    symbol: str,
    market: str = Query("CN", description="市场"),
):
    """资金流(经 MarketData Engine, 走 UI 配置的 capital_flow vendor, 默认 sina/eastmoney)。

    key 来自「设置→接口Key」配置的 data_sources(type=capital_flow), 实时生效。
    """
    try:
        from src.core.marketdata_client import get_market_data
        md = get_market_data()
        cf = md.capital_flow(symbol, market=market)
        if cf is None:
            return {"symbol": symbol, "market": market, "error": "no_data"}
        return {
            "symbol": symbol,
            "market": market,
            "main_net_inflow": cf.main_net_inflow,
            "main_net_inflow_pct": cf.main_net_inflow_pct,
            "super_net_inflow": cf.super_net_inflow,
            "big_net_inflow": cf.big_net_inflow,
            "mid_net_inflow": cf.mid_net_inflow,
            "small_net_inflow": cf.small_net_inflow,
            "main_net_5d": cf.main_net_5d,
        }
    except Exception as e:
        logger.warning(f"资金流代理失败 [{symbol}]: {e}")
        raise HTTPException(502, f"数据源调用失败: {e}")


# ── C2 stale-on-error (2026-09-10, OpenTerminal 借鉴 C2) ─────────────────────
# 展示类资金流: 源故障/空返回时回退"上次成功快照 + 显式标注"(过期数据+标注 > 空白)。
# ⚠️ 仅展示类端点接入(板块/大盘资金); 行情/结算/下单路径严禁复用 —— 报价必须实时。
_STALE_TTL_S = 24 * 3600  # 备份保留窗口(旧数据上限; 响应带 stale_age_sec 供前端标注)


def _stale_put(kind: str, payload: dict) -> None:
    """成功后备份一份供源故障时回退; 备份失败静默, 不影响主流程。"""
    try:
        biz_cache.set_json(f"stale:{kind}", {"saved_at": time.time(), "payload": payload}, ttl=_STALE_TTL_S)
    except Exception:  # noqa: BLE001
        pass


def _stale_take(kind: str) -> dict | None:
    """取备份并打 stale 标注; 无备份返回 None(调用方维持原有 502/错误语义)。"""
    try:
        rec = biz_cache.get_json(f"stale:{kind}")
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(rec, dict) or not isinstance(rec.get("payload"), dict):
        return None
    try:
        age = max(0, int(time.time() - float(rec.get("saved_at") or 0)))
    except (TypeError, ValueError):
        age = 0
    return {**rec["payload"], "stale": True, "stale_age_sec": age}


@router.get("/board-capital-flow")
async def board_capital_flow_proxy(
    board_type: str = Query("industry", description="industry 行业 / concept 概念"),
):
    """板块资金流向(同花顺行业/概念资金,免登录免费源)。

    返回按净额降序的板块资金列表(流入/流出/净额,单位亿)。

    落库收口(2026-10-14, 迁移 m191): 读侧『够新读库、过旧回源 + stale 标注』——
      · 库里有够新快照(且交易时段) → **直接读库(零回源)**, source=db_snapshot;
      · 过旧/库空/非交易时段 → 回源实时(成功后顺带采样落库, 1~5min 节流);
      · 回源失败/空返回 → 回退 stale 备份(带 stale/stale_age_sec); 无备份则显式 degraded。
    先例: market_flow_snapshots(m138) + _try_write_snapshot_async。
    """
    from src.core import board_flow_sampler as _bfs

    # 1) 读侧: 够新读库(过旧/非交易时段/库不可达 → None, 回源实时)
    try:
        snap = await asyncio.to_thread(_bfs.read_fresh, board_type)
    except Exception:  # noqa: BLE001
        snap = None
    if snap is not None:
        items = snap["items"]
        return {
            "board_type": board_type,
            "count": len(items),
            "items": items,
            "source": "db_snapshot",
            "data_source": "db_snapshot",
            "data_origin": "board_flow_snapshots",
            "snapshot_ts": snap.get("snapshot_ts"),
            "snapshot_age_sec": snap.get("snapshot_age_sec"),
            "stale": False,
        }

    # 2) 过旧/缺失 → 回源实时
    try:
        from src.core.marketdata_client import get_market_data
        md = get_market_data()
        boards = md.board_capital_flow(board_type=board_type)
        if not boards:
            stale = _stale_take(f"board-flow:{board_type}")
            if stale is not None:
                logger.info("板块资金源返回空, 回退备份(age=%ss)", stale.get("stale_age_sec"))
                return stale
            # KI-044: 空且无备份 —— 显式 degraded, 不与"今日无板块资金"混同
            return {
                "board_type": board_type,
                "count": 0,
                "items": [],
                "degraded": True,
                "note": "板块资金源暂无数据(源故障或空返回); 非「今日无资金流」",
            }
        items = [_bfs.board_to_item(b) for b in boards]
        payload = {
            "board_type": board_type,
            "count": len(items),
            "items": items,
            "source": "ths_realtime",
            "data_origin": "realtime",
            "snapshot_used": False,
            "snapshot_ts": None,
        }
        _stale_put(f"board-flow:{board_type}", payload)
        # 3) 采样落库(后台线程, 1~5min 节流, 失败静默)
        try:
            _bfs.try_write_async(board_type, items)
        except Exception:  # noqa: BLE001
            pass
        return payload
    except Exception as e:
        logger.warning(f"板块资金代理失败: {e}")
        stale = _stale_take(f"board-flow:{board_type}")
        if stale is not None:
            logger.warning("板块资金源故障, 回退备份(age=%ss): %s", stale.get("stale_age_sec"), e)
            return stale
        raise HTTPException(502, f"数据源调用失败: {e}")


@router.get("/market-capital-flow")
async def market_capital_flow_proxy():
    """大盘资金(对齐同花顺APP口径: 两市主力净流入 + 总成交额 + 涨跌家数 + 板块明细)。

    2026-08-10 重构: 之前用同花顺 hyzjl 行业资金求和(总流入2611亿口径不对),
    改为国内网关东财两市主力净流入(超大单+大单汇总, 与APP一致)。

    A-7(2026-09-29): **优先读已落库的最新快照**(够新且在交易时段内) —— 与上游网关
    可用性解耦; 未命中(库空/过旧/非交易时段)则照旧走实时链路, 失败再按原逻辑降级。
    """
    # A-7: 先试库(读 market_flow_snapshots 最新一行, 够新且交易时段内才采纳)。
    # 同步读库 → to_thread, 不阻塞事件循环(与本函数下面 P0 注释同类问题)。
    snap = await asyncio.to_thread(_read_fresh_db_snapshot)
    if snap is not None:
        out = _snapshot_to_response(snap)
        if out.get("total_amount") is None:
            # 2026-10-20: 兜底链改为 TQ 优先(TQ 指数快照 → 腾讯指数)
            _amt, _src = await asyncio.to_thread(_refresh_amount_fallback)
            out["total_amount"] = _amt
            out["amount_source"] = (
                "通达信 TQ 指数成交额(沪综指+深综指)" if _src == "tdx_tq_index"
                else ("腾讯指数成交额口径" if _src == "tencent_index" else "不可用")
            )
        return out
    try:
        import requests as _req

        def _fetch_overview():
            # P0(2026-09-18): 同步 requests 不得在 async def 内直接调用(会阻塞事件循环),
            # 包一层 asyncio.to_thread 交给线程池。
            return _req.get(
                gateway_url("cn/market-overview"), timeout=6
            ).json()

        # 1. 国内网关: 两市主力净流入 + 成交额 + 涨跌家数
        ov = await asyncio.to_thread(_fetch_overview)
        if ov.get("error"):
            # C2: 网关显式报错同属"源不可用" → 有备份则回退旧快照+标注
            stale = _stale_take("market-flow")
            # 但**涨跌家数不该跟着一起陈旧**(2026-09-23 清单切换): TQ 走本地客户端,
            # 网关是否挂与它无关 → 网关报错分支里也要拿下 TQ 实时值盖上去。
            br = await asyncio.to_thread(_tq_breadth)
            if stale is not None:
                out = dict(stale)
                if br:
                    out["up_count"] = br["up"]
                    out["down_count"] = br["down"]
                    out["flat_count"] = br["flat"]
                    out["breadth_source"] = "tdx_tq"
                return out
            if br:
                # 资金类字段彻底没有(无备份) → 只给涨跌家数, 并显式标注降级,
                # 不假装资金字段存在(用户对编造数字敏感)
                return {
                    "up_count": br["up"], "down_count": br["down"], "flat_count": br["flat"],
                    "breadth_source": "tdx_tq",
                    "degraded": True,
                    "error": ov["error"],
                }
            return {"error": ov["error"]}
        # 2026-09-05 口径修正: 上游网关 point/change_pct 放大了100倍
        # (point=393012实际3930.12, change_pct=-30实际-0.3%), 此处归一化。
        for _k in ("sh", "sz", "cyb"):
            _d = ov.get(_k)
            if isinstance(_d, dict):
                if isinstance(_d.get("point"), (int, float)):
                    _d["point"] = round(_d["point"] / 100, 2)
                if isinstance(_d.get("change_pct"), (int, float)):
                    _d["change_pct"] = round(_d["change_pct"] / 100, 2)
        # 2. 板块资金明细(同花顺 hyzjl 行业, 流入/流出榜)
        from src.core import board_flow_sampler as _bfs
        from src.core.marketdata_client import get_market_data
        md = get_market_data()
        boards = md.board_capital_flow(board_type="industry") or []
        # P1(audit-20260915): net_inflow 缺失的板块不参与排序
        # (此前 or 0.0 把"无数据"当成 0, 会挤占流入/流出榜单位置) —— 排序/筛选逻辑
        # 统一收口到 _boards_to_inout(与库读分支同口径)。
        board_items = [_bfs.board_to_item(b) for b in boards]
        inflow_boards, outflow_boards = _boards_to_inout(board_items)
        # 板块资金采样落库(节流, 后台线程): 供板块端点与大盘资金库读分支复用
        try:
            _bfs.try_write_async("industry", board_items)
        except Exception:  # noqa: BLE001
            pass
        result = {
            # 两市主力净流入(对齐同花顺APP)
            "total_main_flow": ov.get("total_main_flow"),      # 亿
            "sh_flow": (ov.get("sh") or {}).get("main_flow"),  # 沪市主力
            "sz_flow": (ov.get("sz") or {}).get("main_flow"),  # 深市主力
            "cyb_flow": (ov.get("cyb") or {}).get("main_flow"),
            # 市场统计(同花顺APP盘面)
            "total_amount": ov.get("total_amount"),            # 两市成交额亿
            "up_count": ov.get("up_count"),
            "down_count": ov.get("down_count"),
            "flat_count": ov.get("flat_count"),
            "sh": ov.get("sh"), "sz": ov.get("sz"), "cyb": ov.get("cyb"),
            # 板块明细
            "inflow_boards": inflow_boards,
            "outflow_boards": outflow_boards,
            "source": "eastmoney_push2delay_cn",
            "breadth_source": "cn_gateway",
            # 收口(2026-10-14): 无够新大盘资金快照 → 走实时并**显式标注**(不静默)
            "snapshot_used": False,
            "snapshot_note": "库内无够新大盘资金快照(或非交易时段), 走实时链路",
            # 口径标签(B3/3.4): 主力净流入为按单金额四档归类(资金面参考),
            # 板块明细为同花顺行业资金 —— 均禁止用于主力意图判定(AGENTS.md 红线)
            "caliber": "eastmoney4",
            "caliber_label": "主力净流入: 东财四档·资金面参考, 禁用于主力意图判定; "
                             "板块明细: 同花顺行业资金(参考)",
            # A-7: 与库读分支口径字段对齐(方向语义透传给下游/UI)
            "direction_semantics": _CAPITAL_FLOW_TAG.direction_semantics,
            "timestamp": None,
        }
        # 涨跌家数改走 TQ(2026-09-23 清单切换): 去掉对 cn 网关该字段的依赖。
        # 只换 up/down/flat 三个字段, 主力净流入与板块明细仍走原源(口径差异不混)。
        # ⚠️ to_thread: TQ 查询是同步 HTTP(全A 约 1s), 在 async def 里直接调会阻塞事件循环
        #    (见本函数开头 P0 注释 —— 同一类问题)。
        result = await asyncio.to_thread(_apply_tq_breadth, result)
        # 2026-10-20: 上游 total_amount 常为 None → 用指数成交额兜底（否则首页成交额恒为 --，
        # 且落库后读库分支也一直是空）。
        if result.get("total_amount") is None:
            _amt, _src = await asyncio.to_thread(_refresh_amount_fallback)
            result["total_amount"] = _amt
            result["amount_source"] = (
                "通达信 TQ 指数成交额(沪综指+深综指)" if _src == "tdx_tq_index"
                else ("腾讯指数成交额口径" if _src == "tencent_index" else "不可用")
            )
        # 2026-10-20: 主力净流入上游(cn 网关)常缺 → 退回库内最近一次 TQ 全市场汇总(AMO/SUPAMO),
        # 标注快照年龄; 连库也没有就保持 None(前端 --), 绝不用 0 顶替。
        # 2026-10-20 铁律: TQ 优先 —— 库内有 10 分钟内的 TQ 汇总就用它, 东财只作兜底。
        _snap_fresh = await asyncio.to_thread(_latest_tq_flow_row, 600)
        if _snap_fresh is not None:
            if result.get("total_main_flow") is not None:
                result["main_flow_eastmoney"] = result["total_main_flow"]   # 保留东财值供对账
            result["total_main_flow"] = _snap_fresh["total_main_flow"]
            result["main_flow_source"] = f"tdx_tq_supamo(快照 {_snap_fresh['age_sec']}s 前)"
            result["caliber"] = "tdx_supamo"
            result["caliber_label"] = "主力净流入: 通达信 SUPAMO 全市场汇总(资金面参考, 禁用于主力意图判定)"
        if result.get("total_main_flow") is None:
            _snap = await asyncio.to_thread(_latest_tq_flow_row)
            if _snap is not None:
                result["total_main_flow"] = _snap["total_main_flow"]
                result["main_flow_source"] = f"tdx_tq_supamo(快照 {_snap['age_sec']}s 前)"
                result["caliber"] = "tdx_supamo"
                result["caliber_label"] = "主力净流入: 通达信 SUPAMO 全市场汇总(资金面参考, 禁用于主力意图判定)"
        # v0.4.7: 顺手异步写库(30s 节流, 失败静默不阻断接口)
        try:
            _try_write_snapshot_async(result)
        except Exception:
            pass
        # C2: 成功后备份(供源故障时回退+标注)
        _stale_put("market-flow", result)
        return result
    except Exception as e:
        logger.warning(f"大盘资金代理失败: {e}")
        stale = _stale_take("market-flow")
        if stale is not None:
            logger.warning("大盘资金源故障, 回退备份(age=%ss): %s", stale.get("stale_age_sec"), e)
            return stale
        raise HTTPException(502, f"数据源调用失败: {e}")


# ──────────── 大盘资金快照历史(v0.4.7) ────────────
_FLOW_COLS = "ts, total_main_flow, up_count, down_count, sh_flow, sz_flow"


def _is_flat(items: list[dict]) -> bool:
    vals = [i["total_main_flow"] for i in items if i.get("total_main_flow") is not None]
    return len(vals) >= 2 and (max(vals) - min(vals)) < 1e-9


def _flow_items(rows) -> list[dict]:
    """行 → 前端契约(6 键)。ts 统一转 ISO 字符串; null 保留为 null, 不编造 0。"""
    items = []
    for r in rows:
        ts_val = r[0]
        ts_str = ts_val.isoformat() if hasattr(ts_val, "isoformat") else str(ts_val)
        items.append({
            "ts": ts_str,
            "total_main_flow": float(r[1]) if r[1] is not None else None,
            "up_count": int(r[2]) if r[2] is not None else None,
            "down_count": int(r[3]) if r[3] is not None else None,
            "sh_flow": float(r[4]) if r[4] is not None else None,
            "sz_flow": float(r[5]) if r[5] is not None else None,
        })
    return items


def _last_varying_session(conn, *, lookback_days: int = 10):
    """回溯期内**最近一个全天序列真有变动**的交易日; 找不到返回 (None, None)。

    不排除任何日期: 逐日自己判平。交易日晚上请求 4h 窗口时, 窗口是平的(收盘后 vendor
    返回常量)但**当天全天序列是有波动的** —— 那时该给当天, 不该跳到昨天
    (2026-09-12 v0.5.84 修: 原 `exclude=窗口首日` 会让它跳过当天)。

    调用方必须**在连接仍然打开时**调用(2026-09-12 v0.5.82 修: 曾在 with 块外调用,
    conn 已关闭 → 整个端点落到 except 分支, 恒返回 0 点 + "This Connection is closed")。
    """
    from datetime import timedelta

    since = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d 00:00:00")
    dates = conn.execute(
        text("SELECT DISTINCT date(ts) AS d FROM market_flow_snapshots "
             "WHERE ts >= :since ORDER BY d DESC"),
        {"since": since},
    ).fetchall()
    for row in dates:
        d = str(row[0])
        rows = conn.execute(
            text(f"SELECT {_FLOW_COLS} FROM market_flow_snapshots "
                 "WHERE date(ts) = :d ORDER BY ts ASC LIMIT 500"),
            {"d": d},
        ).fetchall()
        items = _flow_items(rows)
        if len(items) >= 2 and not _is_flat(items):
            return items, d
    return None, None


@router.get("/market-capital-flow/history")
async def market_capital_flow_history(
    hours: int = Query(4, ge=1, le=24, description="回溯小时数(默认 4h, 上限 24h)"),
):
    """读取 market_flow_snapshots 序列(按 ts 升序), 上限 500 条。

    返回 [{ts, total_main_flow, up_count, down_count, sh_flow, sz_flow}, ...]
    数据缺失/库不可达: 返回空数组 + note(前端展示"暂无快照"占位)。
    """
    try:
        from src.web.database import engine
        # 用 datetime/timedelta 计算 cutoff; SQLite 与 PG 都接受 ISO 字符串
        from datetime import timedelta
        cutoff = (datetime.now() - timedelta(hours=max(1, min(int(hours), 24)))).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"""
                    SELECT {_FLOW_COLS}
                    FROM market_flow_snapshots
                    WHERE ts >= :cutoff
                    ORDER BY ts ASC
                    LIMIT 500
                    """
                ),
                {"cutoff": cutoff},
            ).fetchall()
            items = _flow_items(rows)
            # 请求窗口内 vendor 反复返回同一个值时曲线是一条直线, 看着像坏了。
            # 回退到最近一个**全天真有变动**的交易日并标明, 不拿直线冒充曲线。
            # 必须在 with 块内做: 回退查询要复用这条连接。
            session, session_date = "today", (items[0]["ts"][:10] if items else None)
            if _is_flat(items):
                fb_items, fb_date = _last_varying_session(conn)
                if fb_items:
                    items, session_date = fb_items, fb_date
                    session = "today_full" if fb_date == datetime.now().strftime("%Y-%m-%d") else "prev"
        note = ""
        if not items:
            note = "暂无快照(等待大盘资金接口写入)"
        elif session == "today_full":
            note = f"所选 {hours}h 窗口内资金无变动, 显示 {session_date} 全天盘中曲线"
        elif session == "prev":
            note = f"所选 {hours}h 窗口内资金无变动, 显示最近有变动的 {session_date} 盘中曲线"
        return {
            "hours": hours,
            "count": len(items),
            "items": items,
            "session": session,
            "session_date": session_date,
            "note": note,
        }
    except Exception as e:
        logger.warning(f"大盘资金历史读取失败: {e}")
        return {
            "hours": hours,
            "count": 0,
            "items": [],
            "session": None,
            "session_date": None,
            "note": f"读取失败: {e}",
        }


# ──────────────── 个股基本面明细合并(龙虎榜/两融/股东户数/分红/事件日历) ────────────────

# P1(2026-09-18): 龙虎榜逐日循环后台化。
# 原实现: fetch_fundamentals_detail 内 for 每个交易日调 md.dragon_tiger + ftshare,
# 冷启动 12-17s 直接拖死请求。现改为:
# - 市场级(与 symbol 无关)多日抓取结果缓存 `mkt:lhb_range:{market}:{days}`
# - 未命中 → 后台任务抓取, 请求侧只读缓存(可空)
# - 状态查询: GET /dragon-tiger/range/status?market=&days=
_LHB_RANGE_TTL = 3600  # 1h: 交易日内多次请求复用; 日终后自然过期


def _lhb_range_cache_key(market: str, days: int) -> str:
    return f"mkt:lhb_range:{market}:{max(1, min(int(days), 30))}"


def _compute_lhb_range(market: str, days: int) -> dict:
    """后台任务: 逐日抓龙虎榜 + ftshare 席位, 按日期缓存行(与 symbol 无关)。"""
    from datetime import date, timedelta

    from src.core.marketdata_client import get_market_data

    md = get_market_data()
    try:
        from marketdata.vendors.ftshare import FtshareDragonTigerVendor
        _ft = FtshareDragonTigerVendor()
    except Exception:
        _ft = None

    scanned = max(1, min(int(days), 30))
    by_date: dict[str, list[dict]] = {}
    d = date.today()
    errors: list[str] = []
    for _ in range(scanned):
        ds = d.strftime("%Y%m%d")
        # P0(2026-09-18): 周末跳过 —— 龙虎榜仅交易日发布
        is_weekend = d.weekday() >= 5
        d -= timedelta(days=1)
        if is_weekend:
            continue
        # 读侧(2026-10-14 落库收口): 该日已落库 → 直接读库, 不回源(历史日永不回源)。
        day_db = _lhb_read_date(ds, market)
        if day_db:
            by_date[ds] = day_db
            continue
        try:
            rows = md.dragon_tiger(date=ds, market=market) or []
        except Exception as e:
            errors.append(f"{ds}:{e}")
            logger.warning(f"龙虎榜范围抓取[{ds}]失败(跳过): {e}")
            continue
        ft_by_sym: dict = {}
        if _ft and rows:
            try:
                ft_rows = _ft.fetch([], {"date": ds, "market": market}) or []
                ft_by_sym = {r.symbol: r for r in ft_rows if r.symbol}
            except Exception:
                pass
        day_items: list[dict] = []
        for i in rows:
            sym = getattr(i, "symbol", "")
            ft = ft_by_sym.get(sym)
            day_items.append(
                {
                    "trade_date": getattr(i, "trade_date", ds),
                    "symbol": sym,
                    "name": getattr(i, "name", ""),
                    "reason": getattr(i, "reason", None),
                    "close": getattr(i, "close", None),
                    "change_pct": getattr(i, "change_pct", None),
                    "net_buy": getattr(i, "net_buy", None),
                    "buy_amt": getattr(i, "buy_amt", None),
                    "sell_amt": getattr(i, "sell_amt", None),
                    "turnover_pct": getattr(i, "turnover_pct", None),
                    "top_buyers": list(getattr(ft, "top_buyers", []) or []) if ft else [],
                    "top_sellers": list(getattr(ft, "top_sellers", []) or []) if ft else [],
                }
            )
        if day_items:
            # 拉到即落(2026-10-14): 东财-ftshare 按日落库, 下次(1h TTL 过期/重启)直接读库。
            _lhb_upsert_rows(day_items, market=market, source="em_ftshare")
            by_date[ds] = day_items
    return {
        "market": market,
        "days": scanned,
        "by_date": by_date,
        "errors": errors,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def _tq_breadth() -> dict | None:
    """涨跌家数(TQ pricevol)。供 API 与采样器复用; 不可用返回 None(调用方降级)。

    单独抽出来是为了能在**网关报错分支**里也调到 —— 之前把注入写在网关成功分支之后,
    结果网关挂时(恰恰是本切换要解决的场景)根本走不到 TQ。
    """
    try:
        from src.core.tdx_boards import market_breadth

        return market_breadth()
    except Exception as e:  # noqa: BLE001
        logger.debug("涨跌家数(TQ)不可用: %s", e)
        return None


def _apply_tq_breadth(result: dict) -> dict:
    """把 TQ 涨跌家数盖到 result 上(拿不到就原样返回, 不覆盖成 None)。"""
    br = _tq_breadth()
    if not br:
        return result
    result["up_count"] = br["up"]
    result["down_count"] = br["down"]
    result["flat_count"] = br["flat"]
    result["breadth_source"] = "tdx_tq"
    return result


def _get_lhb_range(market: str, days: int) -> dict | None:
    """读多日龙虎榜缓存; 未命中则启动后台任务并返回 None。"""
    key = _lhb_range_cache_key(market, days)
    cached = biz_cache.get_json(key)
    if cached is not None:
        return cached
    job_key = f"lhb_range:{market}:{max(1, min(int(days), 30))}"
    _bg_start(
        job_key,
        lambda: _compute_lhb_range(market, days),
        result_key=key,
        cache_ttl=_LHB_RANGE_TTL,
    )
    return None


def _sym_to_tq_code(symbol: str) -> str:
    """'002361' / '002361.sz' → '002361.SZ'(TQ 格式)。已带后缀则规范化返回。

    规则与 marketdata.vendors.tq.to_tq_code 一致(92/4/8 → BJ 优先判) ——
    前端传裸代码时不加这层会整个龙虎榜取空。
    """
    s = (symbol or "").strip().upper()
    if "." in s:
        code, _, suf = s.partition(".")
        return f"{code}.{suf}"
    if len(s) != 6 or not s.isdigit():
        return s
    if s.startswith(("92", "4", "8")):
        return f"{s}.BJ"
    if s.startswith(("6", "9", "5")):
        return f"{s}.SH"
    return f"{s}.SZ"


def _lhb_from_tq(symbol: str, *, start_time: str = "") -> list[dict]:
    """TQ GP 序列 → 前端龙虎榜行(与东财路径**同契约**)。

    单位对齐: 东财 `buy_amt`/`sell_amt` 口径是**元**(BILLBOARD_BUY_AMT),
    TQ GP02 是**万元** → ×1e4。对不齐会错 10000 倍(已实测标定: GP16 总市值
    946279.81 万 ≈ 9.51 亿股 × 10.10 元)。

    TQ **没有**的字段(上榜原因 reason / 收盘 close / 涨跌幅 / 席位名)一律留空,
    由调用方用东财缓存补 —— 不编造。
    """
    from marketdata.vendors.tq import lhb_series

    rows = lhb_series(_sym_to_tq_code(symbol), start_time=start_time)
    if not rows:
        return []
    out = []
    for r in rows:
        buy_wan, sell_wan = r.get("buy"), r.get("sell")
        buy_yuan = buy_wan * 1e4 if buy_wan is not None else None
        sell_yuan = sell_wan * 1e4 if sell_wan is not None else None
        net = None
        if buy_yuan is not None or sell_yuan is not None:
            net = (buy_yuan or 0.0) - (sell_yuan or 0.0)
        out.append(
            {
                "trade_date": r["date"],
                "symbol": symbol,
                "name": "",
                "reason": None,
                "close": None,
                "change_pct": None,
                "net_buy": net,
                "buy_amt": buy_yuan,
                "sell_amt": sell_yuan,
                "turnover_pct": None,
                "top_buyers": [],
                "top_sellers": [],
                # TQ 独有的结构拆解(万元, 原口径)
                "inst_buy_wan": r.get("inst_buy_amount"),
                "inst_sell_wan": r.get("inst_sell_amount"),
                "yyb_buy_wan": r.get("yyb_buy"),
                "yyb_sell_wan": r.get("yyb_sell"),
                "hsgt_buy_wan": r.get("hsgt_buy"),
                "hsgt_sell_wan": r.get("hsgt_sell"),
                "suspicious": bool(r.get("suspicious")),
            }
        )
    return out


# ──────────── 龙虎榜按日落库(2026-10-14 资金类落库收口, 迁移 m190) ────────────
# 审计定性: fundamentals-detail 经 _lhb_from_tq **每次请求现拉** TQ GP 长序列(个股整段
# 历史), /dragon-tiger/{date} 亦逐次现拉东财+ftshare。现把拉到的行按 (trade_date,
# symbol, market) upsert 落库 dragon_tiger_daily:
#   拉取侧: 任何来源(TQ GP / 东财-ftshare)拉到即落(_lhb_upsert_rows);
#   读侧:  请求只读库, 仅**当日**(尚未落库)增量回源; 历史日永不回源。
_LHB_DB_COLS = (
    "trade_date", "symbol", "market", "name", "reason", "close", "change_pct",
    "net_buy", "buy_amt", "sell_amt", "turnover_pct", "top_buyers", "top_sellers",
    "inst_buy_wan", "inst_sell_wan", "yyb_buy_wan", "yyb_sell_wan",
    "hsgt_buy_wan", "hsgt_sell_wan", "suspicious", "source",
)
# 读回时 SELECT 的列顺序(与 _lhb_rec_to_row 一一对应)。
_LHB_READ_SQL = (
    "SELECT trade_date, symbol, name, reason, close, change_pct, net_buy, buy_amt, "
    "sell_amt, turnover_pct, top_buyers, top_sellers, inst_buy_wan, inst_sell_wan, "
    "yyb_buy_wan, yyb_sell_wan, hsgt_buy_wan, hsgt_sell_wan, suspicious, source "
    "FROM dragon_tiger_daily "
)


def _lhb_json_list(v) -> str | None:
    """席位列表 → JSON 文本(空/空列表 → NULL, 不写 "[]" 占位)。"""
    if not v:
        return None
    try:
        return json.dumps(v, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return None


def _lhb_row_to_db(r: dict, market: str, source: str) -> dict:
    """前端龙虎榜行 → dragon_tiger_daily 落库行(纯函数, 便于单测)。"""
    d = str(r.get("trade_date") or "")[:10].replace("-", "")
    sym = str(r.get("symbol") or "").strip()
    if "." in sym:
        sym = sym.split(".")[0]
    susp = r.get("suspicious")
    return {
        "trade_date": d,
        "symbol": sym,
        "market": market or "CN",
        "name": r.get("name") or None,
        "reason": r.get("reason") or None,
        "close": r.get("close"),
        "change_pct": r.get("change_pct"),
        "net_buy": r.get("net_buy"),
        "buy_amt": r.get("buy_amt"),
        "sell_amt": r.get("sell_amt"),
        "turnover_pct": r.get("turnover_pct"),
        "top_buyers": _lhb_json_list(r.get("top_buyers")),
        "top_sellers": _lhb_json_list(r.get("top_sellers")),
        "inst_buy_wan": r.get("inst_buy_wan"),
        "inst_sell_wan": r.get("inst_sell_wan"),
        "yyb_buy_wan": r.get("yyb_buy_wan"),
        "yyb_sell_wan": r.get("yyb_sell_wan"),
        "hsgt_buy_wan": r.get("hsgt_buy_wan"),
        "hsgt_sell_wan": r.get("hsgt_sell_wan"),
        "suspicious": (None if susp is None else bool(susp)),
        "source": source or None,
    }


def _lhb_amounts_from_tq(symbols: list[str], trade_date: str,
                         *, max_symbols: int = 200, budget_sec: float = 12.0) -> dict[str, dict]:
    """用 TQ 的 GP 序列**补强**龙虎榜金额（用户铁律：谁强用谁，弱的备用）。

    分工（2026-10-20）：
    - **枚举**（某日全部上榜股）→ 东财强，TQ 无市场级接口，不碰；
    - **金额** → TQ 走本地客户端、零配额、与东财互相印证；东财网关 502 时金额常整块缺失。
      所以：TQ 有值就用 TQ（东财值保留成 `buy_amt_em/sell_amt_em` 对账），TQ 没值才用东财；
    - **席位名/上榜原因** → TQ 无此语义，仍由东财/ftshare 提供。

    返回 {symbol: {buy_amt, sell_amt, net_buy, source}}（金额单位=元）。失败/超时静默返回已取到的部分。
    """
    out: dict[str, dict] = {}
    if not symbols:
        return out
    compact = str(trade_date).replace("-", "")
    t0 = time.time()
    try:
        from marketdata.vendors.tq import lhb_series, to_tq_code
        from marketdata.symbol import Symbol
    except Exception as e:  # noqa: BLE001
        logger.debug("TQ 龙虎榜不可用: %s", e)
        return out
    for i, sym in enumerate(symbols[:max_symbols]):
        if time.time() - t0 > budget_sec:
            logger.info("TQ 龙虎榜金额补强超预算(已补 %d 只) → 其余沿用东财", len(out))
            break
        try:
            tqc = to_tq_code(Symbol.parse(str(sym), "CN"))
            if not tqc:
                continue
            rows = lhb_series(tqc, start_time=compact, end_time=compact) or []
        except Exception as e:  # noqa: BLE001
            logger.debug("TQ 龙虎榜 %s 失败: %s", sym, e)
            continue
        for r in rows:
            if str(r.get("date") or "").replace("-", "") != compact:
                continue
            buy_wan, sell_wan = r.get("buy"), r.get("sell")
            if buy_wan is None and sell_wan is None:
                continue
            buy = float(buy_wan) * 1e4 if buy_wan is not None else None   # 万元 → 元
            sell = float(sell_wan) * 1e4 if sell_wan is not None else None
            out[str(sym)] = {
                "buy_amt": buy,
                "sell_amt": sell,
                "net_buy": (buy or 0.0) - (sell or 0.0),
                "source": "tdx_tq_gp02",
            }
            break
        if i % 20 == 19:
            time.sleep(0.05)   # 单线程 17709：别突发
    return out


def _lhb_upsert_rows(rows: list[dict], market: str = "CN", source: str = "") -> int:
    """按日 upsert 落库 (trade_date, symbol, market) 唯一键 → 重拉幂等。失败静默返回 0。"""
    if not rows:
        return 0
    try:
        from src.db.dialect import upsert_sql
        from src.web.database import engine

        stmt = upsert_sql(
            "dragon_tiger_daily",
            list(_LHB_DB_COLS),
            ["trade_date", "symbol", "market"],
            [c for c in _LHB_DB_COLS if c not in ("trade_date", "symbol", "market")],
        )
        saved = 0
        seen: set[tuple] = set()
        with engine.begin() as conn:
            for r in rows:
                row = _lhb_row_to_db(r, market, source)
                if len(row["trade_date"]) != 8 or len(row["symbol"]) != 6:
                    continue  # 键不全的行不入库(不编造)
                key = (row["trade_date"], row["symbol"], row["market"])
                if key in seen:
                    continue
                seen.add(key)
                conn.execute(text(stmt), row)
                saved += 1
        return saved
    except Exception as e:  # noqa: BLE001
        logger.debug("龙虎榜落库失败(静默, 不阻断): %s", e)
        return 0


def _lhb_json_parse(v) -> list:
    if not v:
        return []
    try:
        x = json.loads(v) if isinstance(v, str) else v
    except (TypeError, ValueError):
        return []
    return x if isinstance(x, list) else []


def _lhb_rec_to_row(r, symbol: str) -> dict:
    """dragon_tiger_daily 行 → 前端契约行(与 _lhb_from_tq/东财路径同键)。"""
    return {
        "trade_date": str(r[0]),
        "symbol": r[1] or symbol,
        "name": r[2] or "",
        "reason": r[3],
        "close": r[4],
        "change_pct": r[5],
        "net_buy": r[6],
        "buy_amt": r[7],
        "sell_amt": r[8],
        "turnover_pct": r[9],
        "top_buyers": _lhb_json_parse(r[10]),
        "top_sellers": _lhb_json_parse(r[11]),
        "inst_buy_wan": r[12],
        "inst_sell_wan": r[13],
        "yyb_buy_wan": r[14],
        "yyb_sell_wan": r[15],
        "hsgt_buy_wan": r[16],
        "hsgt_sell_wan": r[17],
        "suspicious": bool(r[18]) if r[18] is not None else False,
        # 金额来源（TQ 补强过就标 TQ；老行为东财口径）。source 列见 dragon_tiger_daily。
        "amount_source": (
            "tdx_tq_gp02" if "tq_amt" in str(r[19] or "") else "eastmoney"
        ),
    }


def _lhb_read_symbol(symbol: str, market: str = "CN", limit: int = 400) -> list[dict]:
    """读某 symbol 的落库龙虎榜(新→旧)。失败/无表静默返回 [](调用方回源)。"""
    sym = str(symbol or "").strip()
    if "." in sym:
        sym = sym.split(".")[0]
    try:
        from src.web.database import engine

        with engine.connect() as conn:
            recs = conn.execute(
                text(_LHB_READ_SQL + "WHERE symbol = :s AND market = :m "
                     "ORDER BY trade_date DESC LIMIT :lim"),
                {"s": sym, "m": market or "CN", "lim": limit},
            ).fetchall()
    except Exception as e:  # noqa: BLE001
        logger.debug("龙虎榜读库失败(回源): %s", e)
        return []
    return [_lhb_rec_to_row(r, sym) for r in recs]


def _lhb_read_date(trade_date: str, market: str = "CN") -> list[dict]:
    """读某交易日的落库龙虎榜(全市场)。失败/无表静默返回 [](调用方回源)。"""
    d = str(trade_date or "")[:10].replace("-", "")
    if len(d) != 8:
        return []
    try:
        from src.web.database import engine

        with engine.connect() as conn:
            recs = conn.execute(
                text(_LHB_READ_SQL + "WHERE trade_date = :d AND market = :m ORDER BY symbol"),
                {"d": d, "m": market or "CN"},
            ).fetchall()
    except Exception as e:  # noqa: BLE001
        logger.debug("龙虎榜读库(按日)失败(回源): %s", e)
        return []
    return [_lhb_rec_to_row(r, r[1] or "") for r in recs]


def _dragon_tiger_from_db(trade_date: str, market: str = "CN") -> dict | None:
    """该交易日已落库 → 组装与回源分支同构的响应(items 为前端契约行); 无行返回 None。"""
    rows = _lhb_read_date(trade_date, market)
    if not rows:
        return None
    return {
        "trade_date": str(trade_date or "")[:10].replace("-", ""),
        "market": market,
        "count": len(rows),
        "items": rows,
        "pending": False,
        "source": "db",
        "data_origin": "dragon_tiger_daily",
    }


def _next_day_yyyymmdd(d: str) -> str:
    """'20251219' → '20251220'; 非法输入返回 ''(调用方按全量回源处理)。"""
    s = str(d or "").replace("-", "")[:8]
    if len(s) != 8 or not s.isdigit():
        return ""
    try:
        from datetime import date as _date, timedelta as _td

        nxt = _date(int(s[:4]), int(s[4:6]), int(s[6:8])) + _td(days=1)
        return nxt.strftime("%Y%m%d")
    except ValueError:
        return ""


def _lhb_symbol_rows(symbol: str, market: str = "CN") -> list[dict]:
    """个股龙虎榜: **只读库 + 当日增量回源**(历史日永不回源)。

    1) 库里有该 symbol 的行且最新日 >= 今天 → 直接返回, **零回源**;
    2) 库里有行但最新日 < 今天(当日榜单未落) → 仅回源 [最新日+1, 今天] 的增量
       (TQ GP 序列, start_time 收窄), 落库后与库行合并返回;
    3) 库空(从未落库/冷启动) → 一次性回源整段并落库;
    库不可达/回源异常 → 由调用方容错(此处不抛, 返回已得部分)。
    """
    db_rows = _lhb_read_symbol(symbol, market)
    today = datetime.now().strftime("%Y%m%d")
    if db_rows:
        latest = max((str(r.get("trade_date") or "") for r in db_rows), default="")
        if latest >= today:
            return db_rows
        start = _next_day_yyyymmdd(latest)
        fetched = _lhb_from_tq(symbol, start_time=start) if start else _lhb_from_tq(symbol)
        if fetched:
            _lhb_upsert_rows(fetched, market=market, source="tdx_tq")
            merged: dict[str, dict] = {str(r.get("trade_date")): r for r in fetched}
            for r in db_rows:
                merged.setdefault(str(r.get("trade_date")), r)
            return sorted(merged.values(), key=lambda x: str(x.get("trade_date") or ""), reverse=True)
        return db_rows
    fetched = _lhb_from_tq(symbol)
    if fetched:
        _lhb_upsert_rows(fetched, market=market, source="tdx_tq")
    return fetched


def fetch_fundamentals_detail(symbol: str, market: str = "CN", dt_days: int = 10) -> dict:
    """个股基本面明细合并取数(纯函数, 供 HTTP 端点与对话助手共用)。

    - dragon_tiger: P1(2026-09-18) 改为读市场级多日缓存(后台预取);
      冷启动时 dragon_tiger 返回空 + lhb_pending=True, 不再同步逐日阻塞。
    - margin / shareholders / dividend / events: 按 symbol 批量接口
    每类独立容错: 单类 vendor 失败只记日志、该类别返回空数组, 不拖垮整体。
    """
    from src.core.marketdata_client import get_market_data

    md = get_market_data()
    out: dict = {
        "symbol": symbol,
        "market": market,
        "dragon_tiger": [],
        "margin": [],
        "shareholders": [],
        "dividend": [],
        "events": [],
        "lhb_pending": False,
    }

    # 1) 龙虎榜: **已落库的库行为主骨架**(2026-10-14 落库收口), 东财市场级缓存补
    #    reason(上榜原因)/close/change_pct/席位名 —— 这几个 TQ 没有(不编造),
    #    而 TQ 有东财没有的: 一年以上历史 + 机构/营业部/沪深股通买卖拆解。
    #    读侧只读库 + 当日增量回源(历史日永不回源), 不再每次现拉整段 TQ GP 序列。
    em_rows: list[dict] = []
    lhb_range = _get_lhb_range(market, dt_days)
    if lhb_range is not None:
        by_date: dict[str, list[dict]] = lhb_range.get("by_date") or {}
        for ds in sorted(by_date.keys(), reverse=True):
            for row in by_date[ds]:
                if row.get("symbol") == symbol:
                    em_rows.append(row)

    tq_rows: list[dict] = []
    if market == "CN":
        try:
            tq_rows = _lhb_symbol_rows(symbol, market)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"基本面明细-龙虎榜[库/TQ {symbol}]失败, 降级东财缓存: {e}")

    if tq_rows:
        # 东财有值的字段覆盖到 TQ 行上(TQ 侧留空的才补, 不覆盖 TQ 的金额口径)
        em_by_date: dict[str, dict] = {}
        for e in em_rows:
            key = str(e.get("trade_date") or "").replace("-", "")
            if key:
                em_by_date[key] = e
        for r in tq_rows:
            e = em_by_date.get(str(r.get("trade_date") or "").replace("-", ""))
            if not e:
                continue
            for k in ("name", "reason", "close", "change_pct", "turnover_pct",
                      "top_buyers", "top_sellers"):
                if not r.get(k) and e.get(k):
                    r[k] = e[k]
        # 东财有、TQ 无的日期也保留(两只票都不丢)
        tq_dates = {str(r.get("trade_date") or "").replace("-", "") for r in tq_rows}
        merged = list(tq_rows)
        for e in em_rows:
            if str(e.get("trade_date") or "").replace("-", "") not in tq_dates:
                merged.append(e)
        out["dragon_tiger"] = merged
        out["lhb_source"] = "tdx_tq+em_merge" if em_rows else "tdx_tq"
        # TQ 拿到就不需要等东财后台任务了(冷启动不再 pending)
    else:
        out["dragon_tiger"] = em_rows
        out["lhb_source"] = "em"
        if lhb_range is None:
            out["lhb_pending"] = True
            out["lhb_note"] = "龙虎榜多日数据后台抓取中, 请稍后重试或轮询 /dragon-tiger/range/status"
    # 龙虎榜按交易日倒序(新→旧)
    out["dragon_tiger"].sort(key=lambda r: r.get("trade_date") or "", reverse=True)

    # 2) 融资融券(按 symbol, 取最新快照)
    try:
        for i in md.margin([symbol], market=market) or []:
            out["margin"].append(
                {
                    "date": getattr(i, "date", ""),
                    "symbol": getattr(i, "symbol", symbol),
                    "rz_balance": getattr(i, "rz_balance", None),
                    "rz_buy": getattr(i, "rz_buy", None),
                    "rz_repay": getattr(i, "rz_repay", None),
                    "rq_balance": getattr(i, "rq_balance", None),
                    "rq_sell_vol": getattr(i, "rq_sell_vol", None),
                    "rq_repay_vol": getattr(i, "rq_repay_vol", None),
                    "total_balance": getattr(i, "total_balance", None),
                }
            )
    except Exception as e:
        logger.warning(f"基本面明细-两融[{symbol}]查询失败: {e}")

    # 3) 股东户数(按 symbol, 取最新一期)
    try:
        for i in md.shareholders([symbol], market=market) or []:
            out["shareholders"].append(
                {
                    "report_date": getattr(i, "report_date", ""),
                    "symbol": getattr(i, "symbol", symbol),
                    "holder_num": getattr(i, "holder_num", None),
                    "change_num": getattr(i, "change_num", None),
                    "change_ratio": getattr(i, "change_ratio", None),
                    "avg_shares": getattr(i, "avg_shares", None),
                }
            )
    except Exception as e:
        logger.warning(f"基本面明细-股东户数[{symbol}]查询失败: {e}")

    # 4) 分红(按 symbol, 全部历史, 按除权日倒序)
    try:
        for i in md.dividend([symbol], market=market) or []:
            out["dividend"].append(
                {
                    "ex_date": getattr(i, "ex_date", ""),
                    "symbol": getattr(i, "symbol", symbol),
                    "dividend_per_share": getattr(i, "dividend_per_share", None),
                    "transfer_ratio": getattr(i, "transfer_ratio", None),
                    "bonus_ratio": getattr(i, "bonus_ratio", None),
                    "progress": getattr(i, "progress", ""),
                }
            )
        out["dividend"].sort(key=lambda r: r["ex_date"] or "", reverse=True)
    except Exception as e:
        logger.warning(f"基本面明细-分红[{symbol}]查询失败: {e}")

    # 5) 事件日历(按 symbol, 近 since_days=7 日公告/业绩)
    try:
        for i in md.events([symbol], market=market, since_days=7) or []:
            ts = getattr(i, "publish_time", None)
            out["events"].append(
                {
                    "source": getattr(i, "source", ""),
                    "external_id": getattr(i, "external_id", ""),
                    "event_type": getattr(i, "event_type", ""),
                    "title": getattr(i, "title", ""),
                    "publish_time": ts.isoformat() if ts else None,
                    "importance": getattr(i, "importance", 0),
                    "url": getattr(i, "url", ""),
                }
            )
        out["events"].sort(
            key=lambda r: r["publish_time"] or "", reverse=True
        )
    except Exception as e:
        logger.warning(f"基本面明细-事件[{symbol}]查询失败: {e}")

    # 6) 日线统计(TQ 独有, 2026-09-23 清单新增): **撤单量** BCancel/SCancel + 四档委托。
    #    免费东财层完全拿不到撤单量(实测 002361 BCancel=194573) → 没有"切换"对象, 这是净增。
    #    单位: 金额元 / 量手; Amo/Vol 是 4×4 四档矩阵, 原样透传(不臆造含义)。
    out["exday"] = {}
    if market == "CN":
        try:
            from marketdata.vendors.tq import exday_latest

            out["exday"] = exday_latest(_sym_to_tq_code(symbol))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"基本面明细-日线统计[TQ {symbol}]失败: {e}")

    # 7) TQ「异动资金档案」(2026-10-20 用户裁决: 逆向接口优先):
    #    大宗交易 GP04 / 增减持 GP05 / 陆股通持股 GP06 / 质押 GP19+20 / 财务 FN1+FN3。
    #    单位全部经交叉验证(见 src/core/tdx_corporate.py 顶部表格)。
    #    东财这五类要拉四个 datacenter 接口还会限流, TQ 一次拿全且零配额。
    #    单类失败只在各自 note 里标注, 不影响其余数据。
    if market == "CN":
        try:
            from src.core.tdx_corporate import dossier as _tq_dossier

            out["corporate"] = _tq_dossier(symbol)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"基本面明细-异动资金档案[TQ {symbol}]失败: {e}")
            out["corporate"] = {"symbol": symbol, "available": False, "note": f"取数异常: {e}"}
    else:
        out["corporate"] = {"symbol": symbol, "available": False, "note": "TQ 仅覆盖 A 股"}

    return out


@router.get("/fundamentals-detail/{symbol}")
async def fundamentals_detail_proxy(
    symbol: str,
    market: str = Query("CN", description="市场"),
    dt_days: int = Query(10, ge=1, le=30, description="龙虎榜回溯天数(自然日)"),
    _refresh: int = Query(0, description="v0.4.77: 1=跳过缓存强刷; 0=命中 24h 缓存"),
):
    """个股基本面明细合并端点: 龙虎榜/融资融券/股东户数/分红/事件日历。

    每类独立容错, 无数据返回空数组; 单类 vendor 失败不影响其余四类。
    key 来自「设置→接口Key」配置的 data_sources(type=dragon_tiger/margin/...), 实时生效。

    v0.4.77: 加 24h 进程内缓存 + PG 落库(small_data_cache 通用小数据缓存);
       龙虎榜/融资融券日终不变, 24h TTL 足够, 单接口冷启动从 12-17s 降到 <300ms。
       _refresh=1 强刷, 用于管理/调试。
    """
    if not _refresh:
        # 尝试命中缓存(summary_cache 复用: payload <50KB)
        from src.core.summary_cache import get_cached_summary, put_cached_summary
        cache_key_symbol = f"fundamentals:{market}:{symbol}:{dt_days}"
        cached = get_cached_summary(cache_key_symbol, market, ttl_s=86400)  # 24h
        if cached:
            return cached
    try:
        result = fetch_fundamentals_detail(symbol, market=market, dt_days=dt_days)
    except Exception as e:
        logger.warning(f"基本面明细代理失败 [{symbol}]: {e}")
        raise HTTPException(502, f"数据源调用失败: {e}")
    if not _refresh:
        # ⚠️ 降级响应**不入缓存**(2026-09-24 v0.13.8 生产实踩):
        # 容器刚重启时 TQ 客户端尚未就绪, 首次调用会拿到空的龙虎榜 + lhb_pending=True;
        # 原实现无条件按 24h 缓存 ⇒ 这个空值把"TQ 长历史龙虎榜"这个新能力对用户
        # **完全遮住一整天**, 而且**每次发版重启都会重演**(页面不报错, 就是没数据 ——
        # 最坏的一种失效)。现在只缓存完整结果, 降级的下次请求会重试。
        if not result.get("lhb_pending"):
            try:
                from src.core.summary_cache import put_cached_summary
                cache_key_symbol = f"fundamentals:{market}:{symbol}:{dt_days}"
                put_cached_summary(cache_key_symbol, market, result, ttl_s=86400)
            except Exception:  # noqa: BLE001
                pass
    return result


# ──────────────── 首页 Dashboard: 东财异动池 + 同花顺热榜 ────────────────


@router.get("/anomalies")
async def anomalies_proxy(
    limit: int = Query(20, ge=1, le=50, description="返回条数(默认20, 最大50)"),
):
    """东财异动池(交易所「严重异常波动」口径), 供首页 Dashboard 直接调用。

    复用 marketdata 的 EmAnomalyVendor(与对话工具 get_market_anomalies 同源),
    但返回结构化 JSON 数组而非文本。同步 vendor 放线程池执行, 不阻塞事件循环;
    无数据返回空数组, vendor 失败返回 502。
    """
    try:
        import asyncio
        from marketdata.vendors.em_anomaly import EmAnomalyVendor

        vendor = EmAnomalyVendor()
        items = await asyncio.to_thread(vendor.fetch, [], {"page_size": limit})
        # 2026-09-05 去重: 东财同股同规则可下多条(days 9/10各一),
        # 按(symbol, rule_code)只留 days 最大的一条。
        best: dict = {}
        for it in (items or []):
            key = (getattr(it, "symbol", ""), getattr(it, "rule_code", 0))
            prev = best.get(key)
            if prev is None or (getattr(it, "days", 0) or 0) > (getattr(prev, "days", 0) or 0):
                best[key] = it
        items = list(best.values())
        return [
            {
                "symbol": getattr(it, "symbol", ""),
                "name": getattr(it, "name", ""),
                "market": getattr(it, "market", ""),
                "change_pct": getattr(it, "change_pct", None),
                "deviation": getattr(it, "deviation", None),
                "days": getattr(it, "days", None),
                "rule_code": getattr(it, "rule_code", 0),
                "rule": getattr(it, "rule", "") or "",
                "is_today": bool(getattr(it, "is_today", False)),
                "trade_date": getattr(it, "trade_date", ""),
            }
            for it in (items or [])
        ]
    except Exception as e:
        logger.warning(f"东财异动池代理失败: {e}")
        raise HTTPException(502, f"数据源调用失败: {e}")


@router.get("/hot-stocks")
async def hot_stocks_proxy(
    period: str = Query("hour", description="热榜周期: hour(小时榜,默认)/day(日榜)"),
    limit: int = Query(20, ge=1, le=50, description="返回条数(默认20, 最大50)"),
):
    """同花顺热榜(小时榜/日榜), 供首页 Dashboard 直接调用。

    复用 marketdata 的 ThsHotListVendor(与对话工具 get_hot_stocks 同源),
    返回结构化 JSON 数组(排名/代码/名称/涨跌幅/热度/概念标签/AI归因 analyse)。
    同步 vendor 放线程池执行, 不阻塞事件循环; 无数据返回空数组, vendor 失败返回 502。
    """
    period = (period or "hour").strip().lower()
    if period not in ("hour", "day"):
        period = "hour"
    try:
        import asyncio
        from marketdata.vendors.ths_hot import ThsHotListVendor

        vendor = ThsHotListVendor()
        items = await asyncio.to_thread(
            vendor.fetch, [], {"period": period, "limit": limit}
        )
        return [
            {
                "rank": getattr(it, "rank", 0) or 0,
                "symbol": getattr(it, "symbol", ""),
                "name": getattr(it, "name", ""),
                "market": getattr(it, "market", ""),
                "change_pct": getattr(it, "change_pct", None),
                "heat": getattr(it, "heat", None),
                "concepts": list(getattr(it, "concepts", ()) or ()),
                "reason": (getattr(it, "reason", "") or "").strip(),  # AI 归因(analyse)
            }
            for it in (items or [])
        ]
    except Exception as e:
        logger.warning(f"同花顺热榜代理失败 [{period}]: {e}")
        raise HTTPException(502, f"数据源调用失败: {e}")


# ──────────── 全市场涨跌幅 9 档分桶(v0.4.7) ────────────
# 数据源: 东财 push2 clist 全 A 股列表(沪深京A), 字段 f2=最新价(元), f3=涨跌幅%。
# 仅一次性 HTTP 拉一页拿到 ~5000 行即可覆盖全 A; 加 300s biz_cache 防止高频轮询撞东财限流。
# 2026-09-16: TTL 60s→300s — 首次计算耗时 32s(新浪 79 页), 60s TTL 导致频繁重算超时。
_BREADTH_CACHE_KEY = "breadth:distribution:v1"
_BREADTH_CACHE_TTL = 300

# 9 档分桶定义(从弱到强, 与涨停/跌停并列两极)
_BUCKET_BOUNDS = [
    (-10.0, -9.5, "跌停"),
    (-9.5, -5.0, "<-5%"),
    (-5.0, -3.0, "-5~-3%"),
    (-3.0, -1.0, "-3~-1%"),
    (-1.0, 1.0, "-1~1%"),
    (1.0, 3.0, "1~3%"),
    (3.0, 5.0, "3~5%"),
    (5.0, 9.5, ">5%"),
    (9.5, 10.0, "涨停"),
]

# 涨跌幅近似涨停/跌停阈值(ST 5% / 普通 10%), 取 9.5 作为普通股的"准涨停"分界。
# 真涨停识别: |pct - 10| < 0.05(或 ST: |pct - 5| < 0.05), 边界更稳。
_LIMIT_UP_TOLERANCE = 0.2
_LIMIT_DOWN_TOLERANCE = 0.2


def _classify_bucket(pct: float | None) -> str | None:
    """根据涨跌幅(%)映射到 9 档之一; None/异常返回 None(不计入总数)。"""
    if pct is None:
        return None
    try:
        p = float(pct)
    except (TypeError, ValueError):
        return None
    # 准涨停/准跌停用绝对阈值, 其他用区间
    if p >= 9.8:  # 普通股涨停≈10
        return "涨停"
    if p <= -9.8:  # 普通股跌停≈-10
        return "跌停"
    for lo, hi, label in _BUCKET_BOUNDS:
        if lo <= p < hi:
            return label
    # 极小概率的 p >= 10 或 p < -10(异常), 归到 涨停 / 跌停
    if p >= 10.0:
        return "涨停"
    return "跌停"


def _fetch_breadth_change_pcts() -> list[float]:
    """拉全 A 股涨跌幅(%)数组(v0.4.7.1: 新浪主源, 东财兜底)。

    新浪 Market_Center.getHQNodeData 生产实测可达(东财 push2 clist 在生产云 IP 断连),
    每页 80 只 × 分页拉满(~5400 只, 约 68 页, 每页间隔 60ms 防限流);
    新浪失败回落东财 push2 clist 单页 pz=5000。失败/超时抛异常(由调用方兜底)。
    """
    import time as _time

    import httpx

    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

    def _fetch_sina() -> list[float]:
        out: list[float] = []
        with httpx.Client(timeout=8.0, follow_redirects=True, headers=headers) as client:
            for page in range(1, 80):  # 上限保护: 79页×80 ≈ 6300 > 全A
                resp = client.get(
                    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                    "Market_Center.getHQNodeData",
                    params={
                        "page": str(page), "num": "80", "sort": "changepercent",
                        "asc": "0", "node": "hs_a",
                    },
                )
                rows = resp.json()
                if not rows:
                    break
                for it in rows:
                    try:
                        v = it.get("changepercent")
                        if v is not None and v != "":
                            out.append(float(v))
                    except (TypeError, ValueError):
                        continue
                _time.sleep(0.06)
        return out

    def _fetch_eastmoney() -> list[float]:
        # v0.4.7.3: push2delay 域每页上限 100 条 → 分页拉全A(total~12365 含京市,
        # 上限 140 页保护); 涨跌分布是统计图, 15 分钟延迟无影响
        url = "https://push2delay.eastmoney.com/api/qt/clist/get"
        em_headers = {**headers, "Referer": "https://quote.eastmoney.com/"}
        out: list[float] = []

        def _parse(diff: list) -> None:
            for item in diff:
                # f3 单位是 %(东财惯例, 非小数), 直接拿来用
                try:
                    v = item.get("f3")
                    if v is not None:
                        out.append(float(v))
                except (TypeError, ValueError):
                    continue

        with httpx.Client(timeout=8.0, follow_redirects=True, headers=em_headers) as client:
            total = None
            for page in range(1, 141):
                resp = client.get(url, params={
                    "pn": str(page), "pz": "100", "po": "1", "np": "1",
                    "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                    "fltt": "2", "invt": "2", "fid": "f3",
                    "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81",
                    "fields": "f2,f3",
                })
                data = (resp.json() or {}).get("data") or {}
                if total is None:
                    total = data.get("total") or 0
                diff = data.get("diff") or []
                _parse(diff)
                if len(diff) < 100 or (total and page * 100 >= total):
                    break
        return out

    # 新浪主源(生产可达); 空结果或失败 → 东财兜底; 都挂才抛
    try:
        sina = _fetch_sina()
        if len(sina) >= 1000:  # 合理下限: 全A应>5000
            return sina
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[breadth] 新浪拉取失败: {e!r}, 回落东财")
    return _fetch_eastmoney()


@router.get("/breadth-distribution")
async def breadth_distribution(
    wait: int = Query(0, ge=0, le=20, description="缓存未命中时最多等待秒数(0=立即返回)"),
):
    """全市场 A 股涨跌幅 9 档分桶(P1 异步化, 2026-09-18)。

    原实现: 同步计算 ~32s(新浪 79 页分页), 直接把请求线程/事件循环拖死。
    现在:
    - 命中 biz_cache → 立即返回
    - 未命中 → 启动后台任务计算, 立即返回全 0 + pending=True
    - 前端轮询本接口或 GET /breadth-distribution/status 获取结果
    - wait>0 时最多阻塞等待(兼容旧同步期望; 用 asyncio.sleep 不卡事件循环)

    返回格式(与历史兼容):
      [{"bucket": "跌停", "count": n}, {"bucket": "<-5%", "count": n}, ...]
    数据缺失: 返回全 0 计数 + note(明示数据源不可用)。
    """
    def _compute() -> dict:
        try:
            pcts = _fetch_breadth_change_pcts()
        except Exception as e:
            logger.warning(f"breadth-distribution 数据源失败: {e}")
            return {
                "count": 0,
                "total": 0,
                "items": [{"bucket": b[2], "count": 0} for b in _BUCKET_BOUNDS],
                "note": f"数据源不可用: {e}",
                "pending": False,
            }
        # 分桶
        buckets: dict[str, int] = {b[2]: 0 for b in _BUCKET_BOUNDS}
        valid = 0
        for pct in pcts:
            label = _classify_bucket(pct)
            if label is None:
                continue
            valid += 1
            buckets[label] = buckets.get(label, 0) + 1
        items = [{"bucket": b[2], "count": buckets.get(b[2], 0)} for b in _BUCKET_BOUNDS]
        payload = {
            "count": valid,
            "total": len(pcts),
            "items": items,
            "note": "" if valid else "数据源返回为空(可能非交易日)",
            "pending": False,
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "source": "sina",
        }
        _breadth_save(payload)
        return payload

    cached = biz_cache.get_json(_BREADTH_CACHE_KEY)
    if cached is not None:
        return _breadth_with_age(cached)

    # 2026-10-20: 缓存未命中先读库（冷启动/重启秒出），再考虑后台重算。
    from_db = _breadth_from_db()
    if from_db is not None:
        try:
            biz_cache.set_json(_BREADTH_CACHE_KEY, from_db, ttl=60)   # 短暂回填，避免连打库
        except Exception:
            pass
        return _breadth_with_age(from_db)

    job_key = "breadth:distribution"
    _bg_start(
        job_key, _compute,
        result_key=_BREADTH_CACHE_KEY, cache_ttl=_BREADTH_CACHE_TTL,
    )

    if wait > 0:
        deadline = time.time() + wait
        while time.time() < deadline:
            done = biz_cache.get_json(_BREADTH_CACHE_KEY)
            if done is not None:
                return done
            await asyncio.sleep(0.4)
        cached = biz_cache.get_json(_BREADTH_CACHE_KEY)
        if cached is not None:
            return cached

    return _empty_breadth()


@router.get("/breadth-distribution/status")
async def breadth_distribution_status():
    """涨跌幅分布后台计算状态(供前端轮询)。

    返回: status(idle/running/succeeded/failed) + ready(是否已有缓存) + elapsed。
    """
    job_key = "breadth:distribution"
    cached = biz_cache.get_json(_BREADTH_CACHE_KEY)
    st = _bg_job_status(job_key)
    return {
        "job": st,
        "ready": cached is not None,
        "cache_ttl_s": _BREADTH_CACHE_TTL,
        # ready=True 时前端可直接再调 /breadth-distribution 拿数据
        "hint": "ready=true 后 GET /api/market-data/breadth-distribution",
    }

@router.get("/breadth-history")
async def market_breadth_history(
    days: int = Query(60, ge=5, le=250, description="返回最近 N 个交易日"),
) -> dict:
    """全市场情绪温度与涨跌家数**历史序列**（自算口径，落库见 market_breadth_daily）。

    真实路径: `/api/market-data/breadth-history`（本 router 挂在 prefix="/api/market-data"）。
    前端用 `fetchAPI('/market-data/breadth-history')`（fetchAPI 自带 /api 前缀）。

    与 `/breadth-distribution` 的区别：那个是**实时**九档分桶（新浪/东财），
    这个是**盘后自算**的日序列（PG klines → 涨跌家数 + 六指标 ADL/ADR/ARMS/BTI/MCL/STIX
    + 情绪温度 0-100），用于画曲线和看分位。

    ⚠️ 覆盖度：最新交易日 PG 往往还在回填（当日 klines 未收全），此时家数会**偏低**。
    消费方必须看 `symbols/total` 比例，不能把"覆盖不全"读成"市场缩量"。
    """
    from src.db.session import SessionLocal

    db = SessionLocal()
    try:
        rows = list(db.execute(text("""
            SELECT trade_date, up_count, down_count, flat_count, symbols,
                   adl, adr, arms, bti, bti_thrust, mcl, mcl_summation, stix,
                   up_ratio, sentiment_score
              FROM market_breadth_daily
             ORDER BY trade_date DESC
             LIMIT :lim
        """), {"lim": int(days)}).fetchall())
    except Exception as e:  # noqa: BLE001 — 表未建(迁移未跑)
        logger.warning("[breadth-history] 查询失败: %r", e)
        return {"ok": False, "reason": "no_table", "items": [],
                "hint": "market_breadth_daily 不可用（迁移未跑或表未建）"}
    finally:
        db.close()

    items = [{
        "date": r[0],
        "up": r[1], "down": r[2], "flat": r[3], "symbols": r[4],
        "adl": r[5], "adr": r[6], "arms": r[7], "bti": r[8], "bti_thrust": r[9],
        "mcl": r[10], "mcl_summation": r[11], "stix": r[12],
        "up_ratio": r[13], "temperature": r[14],
    } for r in rows]
    items.reverse()  # 时间升序, 便于直接画曲线
    if not items:
        return {"ok": False, "reason": "no_data", "items": [],
                "hint": "尚无自算序列（见 src/core/market_breadth_scheduler.py，每交易日 18:40 落库）"}

    latest = items[-1]
    temps = [i["temperature"] for i in items if isinstance(i["temperature"], (int, float))]
    pct = None
    if isinstance(latest.get("temperature"), (int, float)) and len(temps) >= 5:
        below = sum(1 for t in temps if t <= latest["temperature"])
        pct = round(below / len(temps) * 100, 1)

    note = ""
    cov = latest.get("symbols") or 0
    if cov and cov < 3000:
        note = (f"最新交易日 {latest['date']} 仅覆盖 {cov} 只（PG 当日仍在回填），"
                "当日家数偏低，勿读作缩量")
    return {
        "ok": True,
        "days": len(items),
        "items": items,
        "latest": latest,
        "temperature_percentile": pct,
        "coverage": {"symbols": cov, "full_market": 5577},
        "note": note,
        "caliber": "PG klines 自算（非实时）：涨跌家数 + 六指标 + 情绪温度 0-100；"
                   "与客户端全市场快照口径同向（2026-09-25 交叉验证：涨跌比 0.28 vs 0.26）",
    }


# ─────────────────────────────────────────────────────────────────────────────
# 四档资金结构多日序列（2026-10-24 新增）
#
# 为什么需要这个端点：`fundamentals-detail` 里的 `exday` 字段是 TQ **当日实时 RPC**
# 的标量（`exday_latest`, count=1），只能回答「今天四档什么结构」，回答不了
# 「特大单净额连续 5 日在流出吗」这类分位/趋势问题。历史序列只能读 `exday_daily`
# 表（`src/collectors/exday_daily.py` 每交易日 15:45 全市场落库）。
#
# 口径红线：本端点输出 `caliber="tdx_exday4"`（通达信按**单笔成交额**四档的客户端口径），
# 与东财 push2 四档(`eastmoney4`)、同花顺 DDE(`ths`)、腾讯逐笔(`tick`) **不可换算**。
# 四档的金额阈值是**推断值**（TQ 不返回阈值），同屏必须带标签，禁止拿它做方向判定。
# ─────────────────────────────────────────────────────────────────────────────

EXDAY4_THRESHOLDS_HINT = (
    "特大单 >100万 / 大单 20~100万 / 中单 5~20万 / 小单 <5万（推断值，TQ 未返回阈值）"
)
EXDAY4_CALIBER_NOTE = (
    "四档=按单笔成交金额分档（通达信客户端口径 tdx_exday4）；"
    "与东财四档(eastmoney4)、同花顺 DDE(ths)、腾讯逐笔(tick) 不可换算，禁止跨口径相加或比较绝对值"
)


@router.get("/fund-structure/{symbol}")
async def fund_structure_series(
    symbol: str,
    days: int = Query(20, ge=1, le=120, description="回溯交易日数（按 trade_date 倒序取）"),
    market: str = Query("CN", description="市场"),
):
    """四档资金结构多日序列（读 `exday_daily`，口径 `tdx_exday4`）。

    数据来源与限制：
      - TQ `get_exday_data` **只返当日 1 行**（count/start_time/date 参数实测无效），
        历史只能靠每日 15:45 落库累积 → 本端点能回溯的长度 = 落库开始至今。
      - 表空时返回 `ok=true / days=0` 并给 `hint`，**不编造**。
    """
    sym = str(symbol or "").split(".")[0].strip()
    if not sym:
        raise HTTPException(status_code=400, detail="symbol 不能为空")
    mkt = (market or "CN").upper()

    items: list[dict] = []
    try:
        from sqlalchemy import text as _sql

        from src.db.session import SessionLocal

        db = SessionLocal()
        try:
            rows = db.execute(
                _sql(
                    "SELECT trade_date, amount, volume_lots, active_buy_amt, active_sell_amt,"
                    " active_ratio, xl_buy_amt, lg_buy_amt, md_buy_amt, sm_buy_amt,"
                    " xl_sell_amt, lg_sell_amt, md_sell_amt, sm_sell_amt,"
                    " xl_active_net, lg_active_net, md_active_net, sm_active_net"
                    " FROM exday_daily WHERE symbol = :s AND market = :m"
                    " ORDER BY trade_date DESC LIMIT :n"
                ),
                {"s": sym, "m": mkt, "n": int(days)},
            ).fetchall()
        finally:
            db.close()
        for r in reversed(rows):  # 时间正序返回，前端直接画
            items.append(
                {
                    "trade_date": str(r[0]),
                    "amount": r[1],
                    "volume_lots": r[2],
                    "active_buy_amt": r[3],
                    "active_sell_amt": r[4],
                    "active_ratio": r[5],
                    "tiers": {
                        "xl": {"buy": r[6], "sell": r[10], "net": r[14]},
                        "lg": {"buy": r[7], "sell": r[11], "net": r[15]},
                        "md": {"buy": r[8], "sell": r[12], "net": r[16]},
                        "sm": {"buy": r[9], "sell": r[13], "net": r[17]},
                    },
                }
            )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"四档资金结构[{sym}]查询失败: {e}")
        return {
            "ok": False,
            "symbol": sym,
            "market": mkt,
            "days": 0,
            "items": [],
            "caliber": "tdx_exday4",
            "thresholds_hint": EXDAY4_THRESHOLDS_HINT,
            "note": EXDAY4_CALIBER_NOTE,
            "hint": f"查询异常: {type(e).__name__}",
        }

    latest = items[-1] if items else None
    if latest:
        t = latest["tiers"]
        latest = {
            **latest,
            "main_net": (t["xl"]["net"] or 0) + (t["lg"]["net"] or 0),
        }

    hint = ""
    if not items:
        hint = ("exday_daily 无该标的记录：该表 2026-10-23 起由每交易日 15:45 全市场采集，"
                "历史不可回补（TQ 只返当日 1 行）")

    return {
        "ok": True,
        "symbol": sym,
        "market": mkt,
        "days": len(items),
        "items": items,
        "latest": latest,
        "caliber": "tdx_exday4",
        "tier_thresholds_inferred": True,
        "thresholds_hint": EXDAY4_THRESHOLDS_HINT,
        "note": EXDAY4_CALIBER_NOTE,
        "hint": hint,
    }
