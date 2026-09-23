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
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Callable

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text

from src.web.cache.biz_cache import biz_cache

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
            if cache_ttl and result_key:
                try:
                    biz_cache.set_json(result_key, result, ttl=cache_ttl)
                except Exception:
                    pass
            with job.lock:
                job.status = "succeeded"
                job.finished_at = time.time()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"后台任务 {key} 失败: {e}")
            with job.lock:
                job.status = "failed"
                job.error = str(e)[:300]
                job.finished_at = time.time()

    threading.Thread(target=_runner, name=f"bg-{key[:40]}", daemon=True).start()
    return True


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
                             sh_flow, sz_flow)
                        VALUES
                            (:total_main_flow, :up_count, :down_count, :flat_count,
                             :sh_flow, :sz_flow)
                        """
                    ),
                    {
                        "total_main_flow": payload.get("total_main_flow"),
                        "up_count": payload.get("up_count"),
                        "down_count": payload.get("down_count"),
                        "flat_count": payload.get("flat_count"),
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

        items = []
        for i in rows:
            ft = ft_by_sym.get(i.symbol)
            items.append(
                {
                    "trade_date": getattr(i, "trade_date", trade_date),
                    "symbol": getattr(i, "symbol", ""),
                    "name": getattr(i, "name", ""),
                    "close": getattr(i, "close", None),
                    "change_pct": getattr(i, "change_pct", None),
                    "net_buy": getattr(i, "net_buy", None),
                    "buy_amt": getattr(i, "buy_amt", None),
                    "sell_amt": getattr(i, "sell_amt", None),
                    "reason": getattr(i, "reason", None),
                    "top_buyers": list(getattr(ft, "top_buyers", []) or []) if ft else [],
                    "top_sellers": list(getattr(ft, "top_sellers", []) or []) if ft else [],
                }
            )
        return {
            "trade_date": trade_date,
            "market": market,
            "count": len(items),
            "items": items,
            "pending": False,
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
    C2: 源故障/空返回且有备份 → 回退旧快照并带 stale/stale_age_sec 标注。
    """
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
        payload = {
            "board_type": board_type,
            "count": len(boards),
            "items": [
                {
                    "board_name": b.board_name,
                    "board_type": b.board_type,
                    "index_value": b.index_value,
                    "change_pct": b.change_pct,
                    "inflow": b.inflow,
                    "outflow": b.outflow,
                    "net_inflow": b.net_inflow,
                    "stock_count": b.stock_count,
                    "leader_name": b.leader_name,
                    "leader_change_pct": b.leader_change_pct,
                    "leader_price": b.leader_price,
                    "rank": b.rank,
                }
                for b in boards
            ],
        }
        _stale_put(f"board-flow:{board_type}", payload)
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
    """
    try:
        import requests as _req

        def _fetch_overview():
            # P0(2026-09-18): 同步 requests 不得在 async def 内直接调用(会阻塞事件循环),
            # 包一层 asyncio.to_thread 交给线程池。
            return _req.get(
                "http://115.190.177.213:8100/cn/market-overview", timeout=6
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
        from src.core.marketdata_client import get_market_data
        md = get_market_data()
        boards = md.board_capital_flow(board_type="industry") or []
        # P1(audit-20260915): net_inflow 缺失的板块不参与排序
        # (此前 or 0.0 把"无数据"当成 0, 会挤占流入/流出榜单位置)
        boards_valid = [b for b in boards if b.net_inflow is not None]
        boards_sorted = sorted(
            boards_valid, key=lambda b: b.net_inflow, reverse=True
        )
        inflow_boards = [
            {
                "name": b.board_name,
                "net_inflow": round(b.net_inflow, 2),  # 亿
                "change_pct": b.change_pct,
            }
            for b in boards_sorted[:10]
            if b.net_inflow > 0
        ]
        outflow_boards = [
            {
                "name": b.board_name,
                "net_inflow": round(b.net_inflow, 2),  # 亿(负=流出)
                "change_pct": b.change_pct,
            }
            for b in reversed(boards_sorted[-10:])
            if b.net_inflow < 0
        ]
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
            # 口径标签(B3/3.4): 主力净流入为按单金额四档归类(资金面参考),
            # 板块明细为同花顺行业资金 —— 均禁止用于主力意图判定(AGENTS.md 红线)
            "caliber": "eastmoney4",
            "caliber_label": "主力净流入: 东财四档·资金面参考, 禁用于主力意图判定; "
                             "板块明细: 同花顺行业资金(参考)",
            "timestamp": None,
        }
        # 涨跌家数改走 TQ(2026-09-23 清单切换): 去掉对 cn 网关该字段的依赖。
        # 只换 up/down/flat 三个字段, 主力净流入与板块明细仍走原源(口径差异不混)。
        # ⚠️ to_thread: TQ 查询是同步 HTTP(全A 约 1s), 在 async def 里直接调会阻塞事件循环
        #    (见本函数开头 P0 注释 —— 同一类问题)。
        result = await asyncio.to_thread(_apply_tq_breadth, result)
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

    # 1) 龙虎榜: **TQ 长序列为主骨架**(2026-09-23 清单切换), 东财市场级缓存补
    #    reason(上榜原因)/close/change_pct/席位名 —— 这几个 TQ 没有(不编造),
    #    而 TQ 有东财没有的: 一年以上历史 + 机构/营业部/沪深股通买卖拆解。
    #    两者合并 = 比任一单源都全。
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
            tq_rows = _lhb_from_tq(symbol)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"基本面明细-龙虎榜[TQ {symbol}]失败, 降级东财缓存: {e}")

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
        return {
            "count": valid,
            "total": len(pcts),
            "items": items,
            "note": "" if valid else "数据源返回为空(可能非交易日)",
            "pending": False,
        }

    cached = biz_cache.get_json(_BREADTH_CACHE_KEY)
    if cached is not None:
        return cached

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
