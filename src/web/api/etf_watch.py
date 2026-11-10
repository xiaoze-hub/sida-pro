"""国家队 ETF 看板 API（TDX/TQ 口径，2026-11-05；2026-11-07 抽公用只读层）。

四个数据面，全部**读库**（采集在 `src/collectors/tq_etf_watch.py`，不进请求路径）：

GET /api/etf-watch/basket              20 只重点 ETF + 12 类分组 + 数据源声明
GET /api/etf-watch/intraday?date=      分钟级聚合：20 只合计 + 上证综指 + 20 日同期均值
GET /api/etf-watch/support             市场级 ETF 份额（SC8）+ 托底日样本 + 分位 + 新鲜度
GET /api/etf-watch/daily?days=30       日度：总成交额 / 分类成交额 / 净申购

**取数与聚合实现在 `src/core/etf_watch_feed.py`**，与数智参谋 `query_data_domain("etf.flow")`
共用同一批 SQL —— 页面和参谋不会算出两个「20 只篮子成交额」。本文件只做 HTTP 收尾：
注入时钟、从 engine 取连接、给盘中图补上证综指旁证。

口径纪律（用户对幻觉敏感，这里逐条写死）：

1. **净申购只有市场级是真的**。单只 ETF 的 `get_gb_info` 份额是「当前值回填到每一天」
   （实测 6 只主流 ETF × 63 个交易日零变化），拿它做单只净申购必然恒为 0。
   所以响应里 `single_share_supported=false` + `single_share_note`，前端必须显式标注，
   **禁止用别的算法"凑"一个单只净申购出来**。
2. 数据缺失一律 `null` + `note`，不写 0。分钟行不全（覆盖率 <80%）时 `state="insufficient"`
   而不是拿半场数据算活跃度。
3. 单位：成交额对外统一 **亿元**（库内 `amount_wan` 是万元，出口 ÷10000）；**SC8 两列是份额
   （亿份）不是金额**，出口带 `unit/scale_unit/net_sub_unit`，不许渲染成亿元或「净申购金额」。
   最新日超出 T+1 容差时 `state="stale"` 且 `as_of` 原样返回真实日期（上游 SC8 已停更）。
4. 时段：`phase` 区分 `pre / live / closed / non_trading_day`，盘中均值只和**同日已发生
   的分钟**比（拿今天累计比过去 20 日全天累计是错的）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query
from sqlalchemy import text

from src.collectors import tq_etf_watch as ew
from src.core import etf_watch_feed as feed
from src.web.database import engine

logger = logging.getLogger(__name__)

router = APIRouter()

_CN = ZoneInfo("Asia/Shanghai")

#: 覆盖率闸门（旧名保留：外部引用不断）。
_COVERAGE_MIN = feed.COVERAGE_MIN

#: 20 日基线窗口（交易日，不含当日）。
BASELINE_DAYS = feed.BASELINE_DAYS


# ── 工具 ─────────────────────────────────────────────────────────────────

def _now_cn() -> datetime:
    return datetime.now(timezone.utc).astimezone(_CN)


def _q(sql: str, params: dict | None = None) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params or {}).mappings().all()
    return [dict(r) for r in rows]


def _feed_run(key: str, params: dict | None = None, fmt: dict | None = None) -> list[dict]:
    """把 `etf_watch_feed` 的语句键展开成本方言 SQL 后执行（IN 占位符走 fmt）。"""
    return _q(feed.SQL[key].format(**(fmt or {})), params or {})


def _int_param(v: object, default: int) -> int:
    """FastAPI 注入时是 int；直接函数调用时可能是 `Query(...)` 对象。"""
    return int(getattr(v, "default", v) or default)


# ── basket ───────────────────────────────────────────────────────────────

@router.get("/basket")
def get_basket() -> dict:
    """20 只重点 ETF 与分类。前端据此渲染图例与分类表，不在前端硬编码名单。"""
    return feed.basket_payload()


# ── intraday ─────────────────────────────────────────────────────────────

def _index_points(target: str) -> list[dict]:
    """上证综指同分钟点位（旁证，拿不到不影响主结论，独立标注）。"""
    points: list[dict] = []
    try:
        for hm, close, _amt in ew.fetch_index_minute(target):
            if close is not None:
                points.append({"minute": hm, "close": feed.round_or_none(close, 2)})
    except Exception as e:  # noqa: BLE001 - 指数是旁证, 失败不拖累主图
        logger.info("[etf-watch] 指数分钟线不可用: %s", e)
    return points


@router.get("/intraday")
def get_intraday(date_str: str | None = Query(default=None, alias="date")) -> dict:
    """盘中分钟观测：20 只 ETF 合计成交额 + 上证综指 + 20 日同期均值。"""
    target = date_str or _now_cn().strftime("%Y%m%d")
    payload = feed.intraday_payload(
        _feed_run,
        date_str,
        now=_now_cn(),
        index_points=_index_points(target) if len(str(target)) == 8 and str(target).isdigit() else [],
    )
    if "error" in payload:
        return payload
    # 回退到历史交易日时，指数旁证必须跟回退后的日期取，不能拿今天冒充。
    if payload.get("date") != target and isinstance(payload.get("benchmark"), dict):
        payload["benchmark"]["points"] = _index_points(str(payload["date"]))
    return payload


# ── support（托底信号） ──────────────────────────────────────────────────

@router.get("/support")
def get_support(days: int = Query(default=750, ge=30, le=1500)) -> dict:
    """市场级 ETF 份额（SC8）+ 托底日样本 + 分位 + 新鲜度（滞后只标注，不改数据）。"""
    return feed.support_payload(_feed_run, _int_param(days, 750), now=_now_cn())


# ── daily（日度资金全景） ────────────────────────────────────────────────

@router.get("/daily")
def get_daily(days: int = Query(default=30, ge=5, le=120)) -> dict:
    """日度：总成交额 / 分类成交额 / 净申购（净申购仅市场级）。"""
    return feed.daily_payload(_feed_run, _int_param(days, 30))
