# -*- coding: utf-8 -*-
"""今日系统信号摘要(设计稿 §7.3「被动注入」, 2026-09-01)。

把 5 块市场级信号(情绪周期/市场主线/全市场三榜/涨停复盘/指数资金)聚合渲染成
≤800 字纯文本, 盘后 cron 预生成落库 signal_summary_daily, 对话热路径只读 text
注入 system message(毫秒级)。

## 设计要点
- 5 块全部复用现有生产接口, **零新采集**;
- 每块独立 try/except, 失败 → data_status="missing", 不阻塞其它块;
- data_status 三态: ok(完整) / partial(部分) / missing(缺失), AI 看字段自识别;
- text 是渲染后的纯文本(无 emoji, 精简结论), blocks 是结构化数据(供审码/扩展)。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# 5 块顺序(决定 text 渲染顺序)
_BLOCK_ORDER = ("sentiment", "mainline", "market_scan", "limit_up", "indices_flow")


# ──────────────────────────── 各块采集 ────────────────────────────

async def _block_sentiment() -> dict:
    """块1 情绪周期: 涨停池指标 → classify_sentiment_cycle。"""
    from src.core.report_generator import _collect_limit_up_summary
    from src.core.sentiment_cycle import classify_sentiment_cycle

    try:
        summary = await _collect_limit_up_summary()
        if not isinstance(summary, dict) or summary.get("error"):
            return {"data_status": "missing", "content": "情绪周期数据缺失"}
        metrics = {
            "limit_up_count": summary.get("total"),
            "max_board_height": summary.get("max_days"),
            "break_rate": summary.get("break_rate"),
            "yesterday_board_perf": summary.get("yesterday_board_perf"),
            "losing_effect": summary.get("losing_effect"),
        }
        r = classify_sentiment_cycle(metrics)
        if r.get("cycle") == "数据不足":
            return {"data_status": "partial", "content": "情绪周期数据不足，无法判别"}
        content = (
            f"情绪周期：{r.get('cycle','?')}(置信度{r.get('confidence','?')})"
            f"。涨停{metrics.get('limit_up_count') or 0}家/"
            f"连板{metrics.get('max_board_height') or 0}板/"
            f"炸板率{metrics.get('break_rate') if metrics.get('break_rate') is not None else '?'}%"
        )
        return {
            "data_status": "ok",
            "content": content,
            "cycle": r.get("cycle"),
            "confidence": r.get("confidence"),
            "limit_up_count": metrics.get("limit_up_count"),
            "max_board_height": metrics.get("max_board_height"),
            "break_rate": metrics.get("break_rate"),
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("[信号摘要] 情绪周期块失败: %s", e)
        return {"data_status": "missing", "content": "情绪周期数据缺失"}


async def _block_mainline() -> dict:
    """块2 市场主线: 涨停池 → aggregate_mainline, 取 Top3。"""
    try:
        from src.collectors.market_sentiment_collector import MarketSentimentCollector
        from src.core.market_mainline import aggregate_mainline

        pool = await asyncio.to_thread(MarketSentimentCollector().get_limit_up_pool)
        result = await asyncio.to_thread(aggregate_mainline, pool)
        ranked = result.get("ranked_groups") or []
        if not ranked:
            return {"data_status": "missing", "content": "市场主线数据缺失"}
        top = ranked[:3]
        parts = []
        for g in top:
            name = g.get("name", "?")
            cnt = g.get("limit_up_count", 0)
            leader = g.get("leader") or {}
            leader_name = leader.get("name") if isinstance(leader, dict) else None
            s = f"{name}({cnt}家)"
            if leader_name:
                s += f"龙头{leader_name}"
            parts.append(s)
        return {
            "data_status": "ok",
            "content": "市场主线：" + "/".join(parts),
            "top": [{"name": g.get("name"), "limit_up_count": g.get("limit_up_count")} for g in top],
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("[信号摘要] 主线块失败: %s", e)
        return {"data_status": "missing", "content": "市场主线数据缺失"}


def _block_market_scan(db) -> dict:
    """块3 全市场三榜: 读 market_scan_ranks 最新快照(盘后 cron 已落库)。"""
    try:
        from src.web.models import MarketScanRank

        row = (
            db.query(MarketScanRank)
            .order_by(MarketScanRank.snapshot_date.desc(), MarketScanRank.id.desc())
            .first()
        )
        if not row or not isinstance(row.payload, dict):
            return {"data_status": "missing", "content": "三榜数据缺失(盘后 15:30 生成)"}
        p = row.payload
        new_g = (p.get("new_g_points") or [])[:3]
        dark = (p.get("dark_top") or [])[:3]
        act = (p.get("activity_top") or [])[:3]

        def _syms(rows, key="symbol"):
            return "、".join(str(r.get(key, "")) for r in rows) or "无"

        dark_str = "、".join(
            f"{r.get('symbol')}({r.get('dark_net_wan')}万)" for r in dark
        ) or "无"
        act_str = "、".join(
            f"{r.get('symbol')}({r.get('level') or '-'})" for r in act
        ) or "无"
        content = (
            f"三榜：新G点[{_syms(new_g)}]；"
            f"暗盘TOP[{dark_str}]；"
            f"机构活跃[{act_str}]"
        )
        return {
            "data_status": "ok",
            "content": content,
            "new_g": [r.get("symbol") for r in new_g],
            "dark_top": [{"symbol": r.get("symbol"), "net_wan": r.get("dark_net_wan")} for r in dark],
            "activity_top": [{"symbol": r.get("symbol"), "level": r.get("level")} for r in act],
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("[信号摘要] 三榜块失败: %s", e)
        return {"data_status": "missing", "content": "三榜数据缺失"}


async def _block_limit_up() -> dict:
    """块4 涨停复盘: 涨停家数/连板梯队。"""
    from src.core.report_generator import _collect_limit_up_summary

    try:
        summary = await _collect_limit_up_summary()
        if not isinstance(summary, dict) or summary.get("error"):
            return {"data_status": "missing", "content": "涨停复盘数据缺失"}
        total = summary.get("total") or 0
        max_days = summary.get("max_days") or 0
        break_rate = summary.get("break_rate")
        content = f"涨停复盘：{total}家涨停，最高{max_days}板"
        if break_rate is not None:
            content += f"，炸板率{break_rate}%"
        return {
            "data_status": "ok",
            "content": content,
            "total": total,
            "max_days": max_days,
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("[信号摘要] 涨停复盘块失败: %s", e)
        return {"data_status": "missing", "content": "涨停复盘数据缺失"}


async def _block_indices_flow() -> dict:
    """块5 指数+资金流: 4 核心指数 + 两市主力今日净额。"""
    from src.core.report_generator import _collect_indices, _collect_market_flow

    idx_status, flow_status = "missing", "missing"
    idx_parts, flow_part = [], ""
    try:
        indices = await _collect_indices()
        if indices:
            idx_status = "ok"
            for it in indices[:4]:
                chg = it.get("change_pct")
                chg_s = f"{chg:+.2f}%" if isinstance(chg, (int, float)) else "?"
                idx_parts.append(f"{it.get('name','?')}{chg_s}")
    except Exception as e:  # noqa: BLE001
        logger.warning("[信号摘要] 指数块失败: %s", e)

    try:
        flow = await _collect_market_flow()
        if isinstance(flow, dict) and not flow.get("error"):
            flow_status = "ok"
            net = flow.get("total_main_flow")
            if net is None:
                net = flow.get("net_inflow")
            if net is None:
                sh = flow.get("sh_flow")
                sz = flow.get("sz_flow")
                if isinstance(sh, (int, float)) and isinstance(sz, (int, float)):
                    net = sh + sz
            if isinstance(net, (int, float)):
                flow_part = f"主力净流入{net:+.0f}亿"
            else:
                flow_part = f"主力净额未知"
    except Exception as e:  # noqa: BLE001
        logger.warning("[信号摘要] 资金流块失败: %s", e)

    if idx_status == "ok" and flow_status == "ok":
        status = "ok"
    elif idx_status == "ok" or flow_status == "ok":
        status = "partial"
    else:
        status = "missing"

    content = "指数资金：" + "、".join(idx_parts) if idx_parts else "指数资金数据缺失"
    if flow_part:
        content += f"；{flow_part}"

    return {
        "data_status": status,
        "content": content,
        "indices": idx_parts,
        "main_flow": flow_part,
    }


# ──────────────────────────── 聚合 + 渲染 ────────────────────────────

async def build_signal_summary(db) -> dict:
    """聚合 5 块 → 返回 {snapshot_date, blocks, text}。每块独立降级。"""
    blocks = {
        "sentiment": await _block_sentiment(),
        "mainline": await _block_mainline(),
        "market_scan": _block_market_scan(db),
        "limit_up": await _block_limit_up(),
        "indices_flow": await _block_indices_flow(),
    }
    text = render_signal_summary(blocks)
    return {
        "snapshot_date": datetime.now().strftime("%Y-%m-%d"),
        "blocks": blocks,
        "text": text,
    }


def render_signal_summary(blocks: dict) -> str:
    """渲染纯文本摘要(≤800 字), 注入 system message 用。"""
    date = datetime.now().strftime("%Y-%m-%d")
    lines = [f"--- 今日系统信号摘要({date}) ---"]
    for key in _BLOCK_ORDER:
        b = blocks.get(key) or {}
        lines.append(b.get("content", "数据缺失"))
    return "\n".join(lines)


def run_signal_summary_job() -> dict:
    """盘后 cron 入口(report_scheduler 调用): 聚合 + 落库, 失败不抛。

    内部自开 DB session(与 run_market_scan_job 同款), 不与 FastAPI 依赖耦合。
    """
    from src.web.database import SessionLocal
    from src.web.models import SignalSummaryDaily

    db = SessionLocal()
    try:
        result = asyncio.run(build_signal_summary(db))
        snap = result["snapshot_date"]
        row = (
            db.query(SignalSummaryDaily)
            .filter(
                SignalSummaryDaily.snapshot_date == snap,
                SignalSummaryDaily.stock_market == "CN",
            )
            .first()
        )
        if row:
            row.blocks = result["blocks"]
            row.text = result["text"]
        else:
            db.add(
                SignalSummaryDaily(
                    snapshot_date=snap,
                    stock_market="CN",
                    blocks=result["blocks"],
                    text=result["text"],
                )
            )
        db.commit()
        statuses = {k: (v or {}).get("data_status") for k, v in result["blocks"].items()}
        return {"ok": True, "snapshot_date": snap, "text_len": len(result["text"]), "statuses": statuses}
    except Exception as e:  # noqa: BLE001
        db.rollback()
        logger.exception("信号摘要聚合/落库失败: %s", e)
        return {"ok": False, "error": str(e)}
    finally:
        db.close()


def read_latest_summary_text(db) -> str | None:
    """对话热路径: 读最新信号摘要 text(纯文本), 无快照 → None(不注入)。"""
    try:
        from src.web.models import SignalSummaryDaily

        row = (
            db.query(SignalSummaryDaily)
            .order_by(SignalSummaryDaily.snapshot_date.desc(), SignalSummaryDaily.id.desc())
            .first()
        )
        if row and row.text:
            return row.text
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning("[信号摘要] 读快照失败: %s", e)
        return None


# ──────────── 存量库兜底建表 ────────────
# 生产启动会 create_all, 但老库升级/测试环境可能没有该表。
# 用 __table__.create(checkfirst=True) 跨 SQLite/PG 通用(与 market_scan.py 同款)。
def _ensure_summary_table() -> None:
    try:
        from src.web.database import engine
        from src.web.models import SignalSummaryDaily

        SignalSummaryDaily.__table__.create(bind=engine, checkfirst=True)
    except Exception as e:  # noqa: BLE001
        logger.debug("signal_summary_daily 兜底建表失败(可能已由 create_all 建): %s", e)


_ensure_summary_table()
