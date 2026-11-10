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
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_CST = ZoneInfo("Asia/Shanghai")
_WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
#: A 股连续竞价收盘时刻（用于判定「最近已收盘交易日」）。
MARKET_CLOSE_HOUR = 15
_ISO_DAY_RE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")


# ──────────────────────── 证据时效硬校验（2026-11-10）────────────────────────
# 用户现场：11-05 的旧证据被当成 11-10 的「今日盘面」。提示词叮嘱不算校验，
# 所以时效在这里用**代码**判定：每一条注入路径各自用自己的真实基准日比对
# 交易日历（单一事实源 src.core.ladder_freshness），得出：
#   - 基准日是不是今天 / 是不是最近交易日；
#   - 落后几个交易日；
#   - 无交易日历（年份未覆盖）时**不猜**、明写无法判定。
# 历史数据允许在场（历史对比有价值），但必须写真实基准日 + 过期说明，
# 不得冒充今日，也不许被静默删除。


def opportunity_header(latest, *, now=None) -> str:
    """按快照实际日期标注机会候选，日历缺失时不猜。"""
    fresh = resolve_freshness(latest, now=now)
    if fresh.get("is_today") is True:
        return f"【今日机会候选】{latest}"
    if fresh.get("is_current") is True:
        return f"【机会候选·基准日 {latest}】最近交易日候选"
    if fresh.get("calendar") == "unavailable":
        return f"【机会候选·基准日 {latest}】时效未判定（交易日历不可用）"
    lag = fresh.get("lag_trading_days")
    return f"【历史机会候选】基准日 {latest}（落后 {lag if lag is not None else '?'} 个交易日，非今日，仅作背景/对比）"


def now_cn(now: datetime | None = None) -> datetime:
    """北京时间当前时刻。传入 naive datetime 视为北京时间（测试注入用）。"""
    if now is None:
        return datetime.now(_CST)
    if now.tzinfo is None:
        return now.replace(tzinfo=_CST)
    return now.astimezone(_CST)


def _parse_day(value) -> date | None:
    """YYYY-MM-DD / YYYYMMDD / date / datetime → date；其它 → None（不猜）。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s:
        return None
    m = _ISO_DAY_RE.search(s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    digits = re.sub(r"\D", "", s)
    if len(digits) >= 8:
        try:
            return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
        except ValueError:
            return None
    return None


def extract_dates(text: str) -> list[str]:
    """抽出文本里所有 ISO 日期（YYYY-MM-DD），去重保序。"""
    out: list[str] = []
    for m in _ISO_DAY_RE.finditer(str(text or "")):
        day = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        if day not in out:
            out.append(day)
    return out


def _expected_latest(now: datetime) -> tuple[date | None, str]:
    """期望最新交易日（<= now 当日）。日历未覆盖年份 → (None, "unavailable")。"""
    try:
        from src.core.ladder_freshness import expected_latest_trading_date

        return _parse_day(expected_latest_trading_date(now)), "ok"
    except Exception as exc:  # noqa: BLE001 - 日历缺失/未覆盖年份
        logger.warning("[时效] 交易日历不可用，无法判定最新交易日: %s", exc)
        return None, "unavailable"


def _trading_lag(base: date, expected: date) -> int | None:
    try:
        from src.core.ladder_freshness import missing_trading_days

        return len(
            missing_trading_days(base.strftime("%Y%m%d"), expected.strftime("%Y%m%d"))
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[时效] 缺失交易日计数失败: %s", exc)
        return None


def resolve_freshness(base_date, *, now: datetime | None = None) -> dict:
    """判定某条证据的真实基准日相对「今天（北京时间）」的时效。

    返回 dict:
      base_date / today / weekday / expected / calendar(ok|unavailable)
      is_today(bool|None)   base == 北京今天
      is_current(bool|None) base == 最近交易日
      stale(bool|None)      base != 最近交易日
      lag_trading_days(int|None) / label / note
    任何一项判不出来就返回 None，**不回落墙钟猜测**。
    """
    dt = now_cn(now)
    today = dt.date()
    weekday = _WEEKDAY_CN[today.weekday()]
    base = _parse_day(base_date)
    expected, calendar = _expected_latest(dt)

    out: dict = {
        "base_date": base.isoformat() if base else None,
        "today": today.isoformat(),
        "weekday": weekday,
        "expected": expected.isoformat() if expected else None,
        "calendar": calendar,
        "is_today": None,
        "is_current": None,
        "stale": None,
        "lag_trading_days": None,
        "label": "",
        "note": "",
    }

    if base is None:
        out["label"] = "基准日未知"
        out["note"] = (
            "该域未标注数据基准日（未知），无法判定新旧 —— "
            "不得当作今日盘面，需人工核对后再用"
        )
        return out

    out["is_today"] = base == today

    if calendar != "ok" or expected is None:
        out["label"] = f"基准日 {base.isoformat()}"
        out["note"] = (
            f"基准日 {base.isoformat()}；北京时间今日 {today.isoformat()}；"
            "交易日历不可用，无法判定是否为最新交易日 —— "
            "不得当作今日盘面，需人工核对"
        )
        return out

    lag = _trading_lag(base, expected) if base <= expected else None
    out["lag_trading_days"] = lag
    out["is_current"] = base == expected
    out["stale"] = base != expected

    if base == expected and base == today:
        out["label"] = "今日"
        out["note"] = (
            f"基准日 {base.isoformat()}（今日，与最近交易日一致，可用于今日结论）"
        )
    elif base == expected:
        out["label"] = "最近交易日"
        out["note"] = (
            f"基准日 {base.isoformat()}（最近交易日；今日 {today.isoformat()} "
            f"{weekday} 休市，非今日新增数据，禁止写成「今日」）"
        )
    elif base < expected:
        out["label"] = "历史"
        out["note"] = (
            f"基准日 {base.isoformat()} 早于最近交易日 {expected.isoformat()}"
            f"（落后 {lag if lag is not None else '?'} 个交易日）；"
            "属历史快照，可作背景/对比，不得冒充今日盘面或直接当作今日结论依据"
        )
    else:
        out["label"] = "基准日异常"
        out["note"] = (
            f"基准日 {base.isoformat()} 晚于最近交易日 {expected.isoformat()}"
            "（库内日期异常或时区错位），不得当作今日盘面"
        )
    return out


def time_anchor_block(now: datetime | None = None) -> str:
    """system 用的时间锚（北京时间 + 交易日历），全部由代码算出。"""
    dt = now_cn(now)
    today = dt.date()
    weekday = _WEEKDAY_CN[today.weekday()]
    expected, calendar = _expected_latest(dt)
    lines = [
        "--- 当前时间（北京时间，代码计算，非模型推测/非宿主墙钟）---",
        f"现在：{dt:%Y-%m-%d %H:%M} {weekday}",
    ]
    if calendar != "ok" or expected is None:
        lines.append(
            f"今日：{today.isoformat()}（{weekday}）；交易日历不可用，"
            "无法判定今日是否开市、最近交易日是哪天 —— "
            "任何数据基准日都不得改写成今日"
        )
        return "\n".join(lines)

    if expected == today:
        if dt.hour < MARKET_CLOSE_HOUR:
            try:
                from src.core.trading_calendar import prev_trading_day

                closed = prev_trading_day(today)
            except Exception:  # noqa: BLE001
                closed = None
            lines.append(f"今日：{today.isoformat()}（{weekday}，交易日，尚未收盘）")
            if closed is not None:
                lines.append(f"最近已收盘交易日：{closed.isoformat()}")
        else:
            lines.append(f"今日：{today.isoformat()}（{weekday}，交易日，已收盘）")
            lines.append(f"最近已收盘交易日：{today.isoformat()}")
        lines.append(f"最近交易日（<=今日）：{expected.isoformat()}")
    else:
        lines.append(f"今日：{today.isoformat()}（{weekday}，休市）")
        lines.append(f"最近已收盘交易日：{expected.isoformat()}")
        lines.append(f"最近交易日（<=今日）：{expected.isoformat()}")
    return "\n".join(lines)


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
        from src.db.models import MarketScanRank

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
            f"三榜：新G点(待确认)[{_syms(new_g)}]；"
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
    """块4 涨停复盘: 涨停家数/连板梯队/炸板率/跌停家数。

    2026-10-23 修复(严重): 原实现读 `summary.get("total")` 与 `summary.get("max_days")`，
    而 `MarketSentimentCollector.get_sentiment_summary()` 实际返回的键是
    `limit_up_count` 与 `max_streak` —— 键名不匹配 → 两个值都取到 None → 被 `or 0` 兜成 0，
    于是**每天摘要都写「0家涨停，最高0板」**（实测 2026-10-22 真实 44 家、10-23 真实 86 家）。
    现在改为按真实键名读，并顺带补齐梯队/炸板/跌停，任何一项缺失都写显式缺口而不是 0。
    """
    from src.core.report_generator import _collect_limit_up_summary

    try:
        summary = await _collect_limit_up_summary()
        if not isinstance(summary, dict) or summary.get("error"):
            return {
                "data_status": "missing",
                "content": "涨停复盘数据缺失（涨停池取数失败）",
            }

        # 真实键名：limit_up_count / max_streak / ladder / top_stocks / top_sectors
        total = summary.get("limit_up_count")
        max_days = summary.get("max_streak")
        if total is None or max_days is None:
            return {
                "data_status": "missing",
                "content": "涨停复盘数据缺失（涨停池字段不全）",
            }

        ladder = summary.get("ladder") or {}
        ladder_parts = [f"{k}板{v}家" for k, v in sorted(ladder.items(), key=lambda x: -int(x[0]))]
        content = f"涨停复盘：{total}家涨停，最高{max_days}板"
        if ladder_parts:
            content += "，梯队" + "/".join(ladder_parts)

        block: dict = {
            "data_status": "ok",
            "content": content,
            "total": total,
            "max_days": max_days,
            "ladder": {str(k): v for k, v in ladder.items()},
            "top_stocks": list(summary.get("top_stocks") or [])[:5],
            "caliber": summary.get("limit_up_source") or "unknown",
        }

        # 炸板/跌停：取自 `market_replay_daily`（东财三池同源同日），缺失就写缺口不写 0
        try:
            from src.core.market_replay import read_latest

            from src.db.session import SessionLocal

            _db = SessionLocal()
            try:
                replay = read_latest(_db)
            finally:
                _db.close()
            if replay:
                bk = replay.get("broken") or {}
                dt = replay.get("limit_down") or {}
                if bk.get("break_rate_pct") is not None:
                    block["break_rate_pct"] = bk.get("break_rate_pct")
                    block["seal_rate_pct"] = bk.get("seal_rate_pct")
                    content += (
                        f"，炸板{bk.get('total')}家/{bk.get('break_rate_pct')}%"
                        f"(封板率{bk.get('seal_rate_pct')}%)"
                    )
                if dt.get("total") is not None:
                    block["limit_down"] = dt.get("total")
                    content += f"，跌停{dt.get('total')}家"
        except Exception as e:  # noqa: BLE001
            logger.debug("[信号摘要] 炸板/跌停补充失败: %s", e)

        block["content"] = content
        return block
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
    dt = now_cn()
    snap = dt.strftime("%Y-%m-%d")
    text = render_signal_summary(blocks, snapshot_date=snap, now=dt)
    return {
        "snapshot_date": snap,
        "blocks": blocks,
        "text": text,
    }


def render_signal_summary(blocks: dict, *, snapshot_date: str | None = None,
                          now: datetime | None = None) -> str:
    """渲染纯文本摘要(≤800 字), 注入 system message 用。

    标题写**真实基准日**并用 `resolve_freshness` 判时效（2026-11-10）：
    旧实现标题一律写「今日系统信号摘要(now)」，把 11-05 的快照伪装成当天数据。
    """
    dt = now_cn(now)
    day = str(snapshot_date or "").strip() or dt.strftime("%Y-%m-%d")
    fresh = resolve_freshness(day, now=dt)
    lines = [
        f"--- 系统信号摘要(基准日 {fresh['base_date'] or day}) ---",
        f"时效：{fresh['note']}",
    ]
    for key in _BLOCK_ORDER:
        b = blocks.get(key) or {}
        lines.append(b.get("content", "数据缺失"))
    return "\n".join(lines)


def run_signal_summary_job() -> dict:
    """盘后 cron 入口(report_scheduler 调用): 聚合 + 落库, 失败不抛。

    内部自开 DB session(与 run_market_scan_job 同款), 不与 FastAPI 依赖耦合。
    """
    from src.db.session import SessionLocal
    from src.db.models import SignalSummaryDaily

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


def read_latest_summary_text(db, *, now: datetime | None = None) -> str | None:
    """对话热路径: 读最新信号摘要 text(纯文本), 无快照 → None(不注入)。

    2026-11-10 时效修复: 旧实现原样返回库里的 text —— 而 text 第一行是写入时
    渲染的「今日系统信号摘要(当天)」, 于是 11-05 的快照进了 11-10 的 system 后
    仍自称「今日」。现在**每条快照都按自己的 snapshot_date 重算时效**:
    标题换成真实基准日, 第二行写时效(今日 / 最近交易日 / 历史落后 N 个交易日 /
    日历不可用时无法判定)。历史快照仍注入(作背景), 但不再冒充今日。
    """
    try:
        from src.db.models import SignalSummaryDaily

        row = (
            db.query(SignalSummaryDaily)
            .order_by(SignalSummaryDaily.snapshot_date.desc(), SignalSummaryDaily.id.desc())
            .first()
        )
        if not (row and row.text):
            return None
        dt = now_cn(now)
        base = str(getattr(row, "snapshot_date", "") or "").strip() or None
        if not base:
            dates = extract_dates(row.text)
            base = dates[0] if dates else None
        fresh = resolve_freshness(base, now=dt)

        body = [
            ln
            for ln in str(row.text).splitlines()
            if not (ln.strip().startswith("---") and "系统信号摘要" in ln)
            and not ln.strip().startswith("时效：")
        ]
        while body and not body[0].strip():
            body.pop(0)
        header = f"--- 系统信号摘要(基准日 {fresh['base_date'] or '未知'}) ---"
        return "\n".join([header, f"时效：{fresh['note']}", *body])
    except Exception as e:  # noqa: BLE001
        logger.warning("[信号摘要] 读快照失败: %s", e)
        return None


# ──────────── 存量库兜底建表 ────────────
# 已收编进 B 层版本化迁移(W1.5/A5 2026-09-08): signal_summary_daily →
# src/web/migrations.py _m141; schema 变更唯一入口是迁移。
