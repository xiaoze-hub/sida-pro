# -*- coding: utf-8 -*-
"""数智参谋 · 市场三榜 / DDE 榜只读数据适配（2026-11-07）。

定位：**只读读取层**，供未来的参谋工具（如 `get_market_scan_ranks` /
`get_dark_fund_top`）直接调用；本文件不注册工具，不碰 registry / tools_catalog /
recipes / actions / chat，也不触发任何 scan / refresh。

## 权限与审计：与页面端点严格同源

对照 `src/web/api/market_scan.py`：

| 榜单 | 表 | 权限点 | 高价值审计标签 |
| :--- | :--- | :--- | :--- |
| 三榜 | `market_scan_ranks` | `view_opportunities` | `opportunities` |
| 暗盘资金 TOP | `dark_fund_top_snapshots` | `view_dark` | `dark_fund` |

两处都复用 `core.permissions.enforce_perm`（含 member 试用计数）与
`core.hv_api_log.log_high_value_call`，且顺序与页面一致：**先权限、再审计、最后读快照**。
因此无快照时同样会落一条审计（页面行为如此，不另立口径）；无权限时不落审计。
权限失败抛 `fastapi.HTTPException`（401/403），与页面一致，不在这里改写成别的异常类型。

## 口径红线

1. **两个"暗盘"不是一回事，禁止合并/互算**：
   - `market_scan_ranks.payload.dark_top` 是 OHLC 日 K 分摊**对照项**，每条
     `approximation=True`；信封标 `dark_top_source="ohlc_approx"`。
   - `dark_fund_top_snapshots` 是 thsdk DDE **真实主力资金流**，行级
     `source="thsdk_dde"`；信封标 `source="thsdk_dde"`。
   两榜各自独立函数、独立标签，本模块**不提供**合并入口。
2. **过时快照必须写清日期**：返回 `snapshot_date` / `expected_data_day`（最近应更新的
   交易日，按 A 股交易日历，周末回看周五）/ `stale` / `age_days` /
   `age_trading_days`；渲染时永远带快照日期，过时明确标「已过期」，不冒充当日。
3. **只读**：不 import `core.market_scan.scan` / `core.dark_fund_scan.scan_dark_fund_top`；
   不写 `market_scan_ranks` / `dark_fund_top_snapshots`。唯一写入是页面同规则的
   高价值接口审计行。
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    # 仅类型标注: 运行期不拖入 SQLAlchemy / models, 保持模块轻量
    from sqlalchemy.orm import Session

    from src.db.models import User

logger = logging.getLogger(__name__)

__all__ = [
    "RANKS_SOURCE",
    "RANKS_DARK_TOP_SOURCE",
    "DARK_FUND_SOURCE",
    "RANKS_API_NAME",
    "DARK_FUND_API_NAME",
    "read_market_scan_ranks",
    "read_dark_fund_top",
    "render_market_scan_ranks",
    "render_dark_fund_top",
]

CST = ZoneInfo("Asia/Shanghai")

#: 三榜快照来源（表名，便于调用方溯源）
RANKS_SOURCE = "market_scan_ranks"
#: 三榜内 `dark_top` 的口径：OHLC 日K分摊对照项，不是真实暗盘
RANKS_DARK_TOP_SOURCE = "ohlc_approx"
#: 独立暗盘资金 TOP 榜来源：thsdk DDE 真实主力资金流
DARK_FUND_SOURCE = "thsdk_dde"

#: 与页面 `log_high_value_call(..., api_name=...)` 完全一致的标签
RANKS_API_NAME = "opportunities"
DARK_FUND_API_NAME = "dark_fund"

_DISPLAY_ROWS = 10


# ──────────────────────────── 日期 / 过时判定 ────────────────────────────


def _coerce_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _cst_today() -> date:
    return datetime.now(CST).date()


def _expected_data_day(as_of: date) -> date:
    """最近应更新快照的 A 股交易日。周末/节假日回看上一交易日。

    交易日历未覆盖年份时显式降级为自然日（并留日志），绝不猜节假日。
    """
    try:
        from src.core.trading_calendar import trading_day_anchor

        return trading_day_anchor(as_of)
    except Exception as e:  # noqa: BLE001
        logger.warning("交易日历不可用, 快照新鲜度按自然日判定: %r", e)
        return as_of


def _trading_gap(start: date, end: date) -> int | None:
    """start→end 之间落后的交易日数（含 end 当日）。日历不可用返回 None。"""
    if end <= start:
        return 0
    try:
        from src.core.trading_calendar import trading_days_between

        return trading_days_between(start, end, inclusive=True)
    except Exception as e:  # noqa: BLE001
        logger.warning("交易日历不可用, 落后交易日数记 None: %r", e)
        return None


def _snapshot_meta(snapshot_date: Any, as_of: date | None = None) -> dict[str, Any]:
    """快照时点元信息。`stale=False` 只代表"不早于最近应更新交易日"。

    周末读周五快照 age_days=1 但 stale=False —— 不能用自然日硬判"过时"。
    """
    today = _coerce_date(as_of) or _cst_today()
    expected = _expected_data_day(today)
    snap = _coerce_date(snapshot_date)

    if snap is None:
        return {
            "snapshot_date": None,
            "as_of": today.isoformat(),
            "expected_data_day": expected.isoformat(),
            "stale": None,
            "age_days": None,
            "age_trading_days": None,
        }

    stale = snap < expected
    return {
        "snapshot_date": snap.isoformat(),
        "as_of": today.isoformat(),
        "expected_data_day": expected.isoformat(),
        "stale": stale,
        "age_days": (today - snap).days,
        "age_trading_days": _trading_gap(snap, expected) if stale else 0,
    }


# ──────────────────────────── 权限 + 审计（页面同源） ────────────────────────────


def _authorize(db: Session, user: User | None, perm: str, api_name: str) -> None:
    """复用页面同一条权限链；顺序：权限 → 高价值审计 → 读数据。

    - `enforce_perm` 内部含 member 试用计数，无权限/试用尽抛 HTTPException；
    - `log_high_value_call` 是 best-effort，失败不影响读取（与页面一致）。
    """
    from src.core.hv_api_log import log_high_value_call
    from src.core.permissions import enforce_perm

    enforce_perm(user, perm, db)
    log_high_value_call(db, user, api_name)


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return str(value) if value else None


def _envelope(
    *,
    row: Any,
    payload: dict[str, Any],
    source: str,
    market: str,
    as_of: date | None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把快照行整成信封：payload 平铺 + 元信息（元信息优先，防 payload 冒充）。"""
    data: dict[str, Any] = dict(payload or {})
    data.update(
        {
            "available": True,
            "source": source,
            "market": getattr(row, "stock_market", None) or market,
            **_snapshot_meta(getattr(row, "snapshot_date", None), as_of),
            "updated_at": _iso(
                getattr(row, "updated_at", None) or getattr(row, "created_at", None)
            ),
            "note": payload.get("note") or "",
        }
    )
    if extra:
        data.update(extra)
    return data


def _unavailable(*, source: str, market: str, as_of: date | None, note: str) -> dict[str, Any]:
    return {
        "available": False,
        "source": source,
        "market": market,
        **_snapshot_meta(None, as_of),
        "note": note,
    }


# ──────────────────────────── 1. 市场三榜（读快照） ────────────────────────────


def read_market_scan_ranks(
    db: Session,
    user: User | None,
    market: str = "CN",
    *,
    as_of: date | None = None,
) -> dict[str, Any]:
    """读最新三榜快照（`market_scan_ranks`），权限 `view_opportunities`。

    与 `GET /api/market-scan/ranks` 同源同序：按 `snapshot_date DESC, id DESC`
    取最新一行；无快照返回 `available=False`，绝不编造榜单，也不触发扫描。

    Args:
        as_of: 仅用于「快照是否过时」判定的日期覆盖（测试/回放用），默认 CST 今天。

    Raises:
        fastapi.HTTPException: 401 未登录 / 403 无权限或试用次数用尽（页面同款）。
    """
    from src.core.permissions import PERM_VIEW_OPPORTUNITIES
    from src.db.models import MarketScanRank

    market = (market or "CN").strip().upper()
    _authorize(db, user, PERM_VIEW_OPPORTUNITIES, RANKS_API_NAME)

    row = (
        db.query(MarketScanRank)
        .filter(MarketScanRank.stock_market == market)
        .order_by(MarketScanRank.snapshot_date.desc(), MarketScanRank.id.desc())
        .first()
    )
    if not row:
        return _unavailable(
            source=RANKS_SOURCE,
            market=market,
            as_of=as_of,
            note="暂无三榜快照(盘后 15:30 cron 落库, 或 POST /refresh 手动触发)；本次不猜数据。",
        )

    payload = row.payload if isinstance(row.payload, dict) else {}
    return _envelope(
        row=row,
        payload=payload,
        source=RANKS_SOURCE,
        market=market,
        as_of=as_of,
        extra={"dark_top_source": RANKS_DARK_TOP_SOURCE},
    )


# ──────────────────────────── 2. 暗盘资金 TOP（DDE 真值榜） ────────────────────────────


def read_dark_fund_top(
    db: Session,
    user: User | None,
    market: str = "CN",
    *,
    as_of: date | None = None,
) -> dict[str, Any]:
    """读最新暗盘资金 TOP 快照（`dark_fund_top_snapshots`），权限 `view_dark`。

    来源是 thsdk DDE 真实主力资金流（`source=thsdk_dde`），与三榜内
    `dark_top` 的 OHLC 分摊对照项**不是同一口径**，禁止互算。
    无快照返回 `available=False`，不触发扫描。

    Args:
        as_of: 仅用于「快照是否过时」判定的日期覆盖（测试/回放用），默认 CST 今天。

    Raises:
        fastapi.HTTPException: 401 未登录 / 403 无权限或试用次数用尽（页面同款）。
    """
    from src.core.permissions import PERM_VIEW_DARK
    from src.db.models import DarkFundTopSnapshot

    market = (market or "CN").strip().upper()
    _authorize(db, user, PERM_VIEW_DARK, DARK_FUND_API_NAME)

    row = (
        db.query(DarkFundTopSnapshot)
        .filter(DarkFundTopSnapshot.stock_market == market)
        .order_by(
            DarkFundTopSnapshot.snapshot_date.desc(),
            DarkFundTopSnapshot.id.desc(),
        )
        .first()
    )
    if not row:
        return _unavailable(
            source=DARK_FUND_SOURCE,
            market=market,
            as_of=as_of,
            note="暂无暗盘资金 TOP 快照(盘后 15:30 cron 落库, 或 POST /dark-fund-top/refresh 手动触发)；本次不猜数据。",
        )

    payload = row.payload if isinstance(row.payload, dict) else {}
    return _envelope(
        row=row,
        payload=payload,
        source=DARK_FUND_SOURCE,
        market=market,
        as_of=as_of,
    )


# ──────────────────────────── 3. 渲染（给未来工具 handler 直接返回） ────────────────────────────


def _num(value: Any, digits: int = 2) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "--"
    return f"{value:,.{digits}f}"


def _stale_line(data: dict[str, Any]) -> str | None:
    if not data.get("stale"):
        return None
    gap = data.get("age_trading_days")
    gap_txt = f"，落后 {gap} 个交易日" if gap is not None else ""
    return (
        f"⚠️ 快照已过期：{data.get('snapshot_date')}"
        f"（最近应更新交易日 {data.get('expected_data_day')}{gap_txt}）"
    )


def _symbols(rows: Any, limit: int = _DISPLAY_ROWS) -> str:
    items = [str(r.get("symbol") or "").strip() for r in (rows or []) if isinstance(r, dict)]
    items = [s for s in items if s]
    if not items:
        return "无"
    shown = "、".join(items[:limit])
    if len(items) > limit:
        shown += f" …另有 {len(items) - limit} 只"
    return shown


def render_market_scan_ranks(data: dict[str, Any]) -> str:
    """三榜读取结果 → 人话（未来 chat 工具直接返回；永远带快照日期/来源）。"""
    if not data.get("available"):
        return (
            f"[市场扫描榜] {data.get('note') or '暂无三榜快照，本次不猜数据。'}"
            f"（最近应更新交易日 {data.get('expected_data_day')}；只读快照，不自动触发扫描）"
        )

    lines: list[str] = []
    stale_line = _stale_line(data)
    if stale_line:
        lines.append(stale_line)
    lines.append(
        f"[市场扫描榜 · 三榜快照 {data.get('snapshot_date')}] "
        f"扫描池 {data.get('universe')} 只｜计算 {data.get('computed')}｜"
        f"跳过 {data.get('skipped')}｜暗盘窗口 {data.get('dark_days')} 日"
    )
    if data.get("generated_at"):
        lines.append(f"  扫描生成时间：{data.get('generated_at')}")
    lines.append(f"  新 G 点（快照日末根疑似，待确认）：{_symbols(data.get('new_g_points'))}")

    dark_rows = [r for r in (data.get("dark_top") or []) if isinstance(r, dict)]

    def _dark_wan(r: dict) -> str:
        wan = r.get("dark_net_wan")
        if wan is None and isinstance(r.get("dark_net"), (int, float)):
            wan = r["dark_net"] / 1e4  # 元 → 万元，仅单位换算
        return _num(wan)

    dark_txt = "、".join(
        f"{r.get('symbol')} {_dark_wan(r)} 万元" for r in dark_rows[:_DISPLAY_ROWS]
    )
    if len(dark_rows) > _DISPLAY_ROWS:
        dark_txt += f" …另有 {len(dark_rows) - _DISPLAY_ROWS} 只"
    lines.append(
        "  暗盘资金 TOP（OHLC 日K分摊对照项，approximation=True，非逐笔真实暗盘）："
        + (dark_txt or "无")
    )

    act_rows = [r for r in (data.get("activity_top") or []) if isinstance(r, dict)]
    act_txt = "、".join(
        f"{r.get('symbol')} {_num(r.get('activity'))}（{r.get('level') or '--'}）"
        for r in act_rows[:_DISPLAY_ROWS]
    )
    if len(act_rows) > _DISPLAY_ROWS:
        act_txt += f" …另有 {len(act_rows) - _DISPLAY_ROWS} 只"
    lines.append(f"  机构活跃度 TOP：{act_txt or '无'}")

    zljc = data.get("zljc")
    if isinstance(zljc, dict) and zljc:
        lines.append(
            f"  ZLJC 主力进出（明盘参考）：已覆盖 {len(zljc)} 只"
            "（超/大/中单净量三档，明细未展开）"
        )
    elif zljc:
        lines.append(f"  ZLJC 主力进出（明盘参考）：{str(zljc)[:200]}")
    lines.append(
        "  口径：与页面 GET /api/market-scan/ranks 同源；只读快照，未触发扫描/刷新。"
    )
    return "\n".join(lines)


def render_dark_fund_top(data: dict[str, Any]) -> str:
    """DDE 榜读取结果 → 人话；来源与三榜 OHLC 近似榜严格区分。"""
    if not data.get("available"):
        return (
            f"[暗盘资金 TOP] {data.get('note') or '暂无暗盘资金 TOP 快照，本次不猜数据。'}"
            f"（最近应更新交易日 {data.get('expected_data_day')}；只读快照，不自动触发扫描）"
        )

    lines: list[str] = []
    stale_line = _stale_line(data)
    if stale_line:
        lines.append(stale_line)
    lines.append(
        f"[暗盘资金 TOP · thsdk DDE 真实主力资金流 快照 {data.get('snapshot_date')}] "
        f"扫描池 {data.get('universe')} 只｜拿到资金流 {data.get('computed')} 只"
    )
    if data.get("generated_at"):
        lines.append(f"  扫描生成时间：{data.get('generated_at')}")

    rows = [r for r in (data.get("top") or []) if isinstance(r, dict)]
    for i, r in enumerate(rows[:_DISPLAY_ROWS], 1):
        lines.append(
            f"  {i}. {r.get('symbol')} {r.get('name') or ''} "
            f"主力净额 {_num(r.get('main_net_wan'))} 万元"
            f"（成交额 {_num(r.get('total_amount_wan'))} 万元）"
        )
    if len(rows) > _DISPLAY_ROWS:
        lines.append(f"  …另有 {len(rows) - _DISPLAY_ROWS} 只")
    if not rows:
        lines.append("  榜单为空（本次不猜）。")

    lines.append(
        "  口径：thsdk DDE 主力资金流（source=thsdk_dde），与三榜 dark_top 的 "
        "OHLC 分摊近似项（approximation=True）不是同一口径，禁止互算/互相校验。"
    )
    lines.append("  只读快照，与页面 GET /api/market-scan/dark-fund-top 同源，未触发扫描/刷新。")
    return "\n".join(lines)
