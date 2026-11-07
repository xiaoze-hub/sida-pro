# -*- coding: utf-8 -*-
"""数智参谋 · 市场三榜 / DDE 榜只读数据适配（2026-11-07，复审阻断项修复版）。

定位：**只读读取层**，供未来的参谋工具（如 `get_market_scan_ranks` /
`get_dark_fund_top`）直接调用；本文件不注册工具，不碰 registry / tools_catalog /
recipes / actions / chat，也不触发任何 scan / refresh。

## 权限与审计：与页面端点严格同源

对照 `src/web/api/market_scan.py`：

| 榜单 | 表 | 权限点 | 高价值审计标签 |
| :--- | :--- | :--- | :--- |
| 三榜 | `market_scan_ranks` | `view_opportunities` | `opportunities` |
| 主力净流入榜 | `dark_fund_top_snapshots` | `view_dark` | `dark_fund` |

两处都复用 `core.permissions.enforce_perm`（含 member 试用计数）与
`core.hv_api_log.log_high_value_call`，顺序与页面一致：
**身份 → 权限 → 审计 → 读快照**。
无快照/数据不可用时同样落一条审计（页面行为如此，不另立口径）；无权限时不落审计。
权限失败抛 `fastapi.HTTPException`（401/403），不在这里改写成别的异常类型。

### 身份条件（与页面 `get_current_user` 对齐的那部分）

页面端点 `GET /api/market-scan/ranks`、`GET /api/market-scan/dark-fund-top` 都挂
`Depends(get_current_user)`，它会做：JWT 验签 → 用户存在 → `is_active` → `is_deleted`
→ `token_version` → 会话存活(`jti`) 六道校验。本模块拿到的是**已经过鉴权的 User 对象**，
因此这里再补一道**对象级状态闸**（防止有人绕过依赖、直接把 DB 里的 User 塞进来）：

- `None` → 401；
- `id` / `username` 为空（空身份、构造不全的对象）→ 401；
- `is_service=True` 或 `role=="service"`（服务令牌 / `ServicePrincipal`）→ 403：
  页面这两个端点只认登录用户，**服务身份本来就进不来**，这里显式拒绝而不是靠
  `enforce_perm` 的「无此权限点」兜底；
- `is_active` 假值 → 403「账号已禁用」；`is_deleted` 真值 → 403「账号已申请注销」。

**边界（不夸大）**：JWT 签名、`token_version` 轮换、会话/设备存活只能由上游
`get_current_user` 用原始 token 验证 —— 本 helper 只拿到 `User` 对象，**无法也不声称**
能自行验证 token；它不负责、也不能替代鉴权依赖。

## 时效口径：只用可信系统时钟，调用方无时间入参

- 公共函数**没有** `as_of` / `now` / `today` 等参数 —— 新鲜度一律由进程内系统时钟
  （Asia/Shanghai）推导。唯一例外是模块私有钩子 `_now_cst()`，仅单测 monkeypatch 用；
  模型/工具调用方拿不到任何可注入的时间位，因此**旧数据无法被装成新鲜**。
- `expected_data_day` = 「到此刻为止本应已落库的最近数据日」：
  交易日 15:30（盘后 cron）之后算当日，之前算上一交易日；非交易日回看上一交易日。
  交易日历不可用（如年份未覆盖）时**不回落自然日**，直接判「时效不可判断」。
- `data_date` 只在能确证时才给：快照日期可解析、且**本身是 A 股交易日**、且不晚于今天。
  写入日 ≠ 数据日 —— 节假日/周末补跑写进来的快照（`snapshot_date` 落在非交易日）无法
  确认装的是哪个交易日的数据，一律 `data_date=None`、`freshness="unknown"`、
  `stale=None`，并在 `note` 里写明「数据日期未知、时效不可判断」，**绝不**给出
  `stale=False`，也绝不说「今日/实时」。
- 数据日不可确证时 `available=False`（fail-closed）：宁可说「不知道/不给」，也不把
  无法定日的数据当最新榜单喂给模型。
- `stale=False` 只表示「数据日不早于最近应有数据日」，不代表盘中实时；所有渲染都以
  `as_of`（当前系统日期）为视角写明数据日。

## 口径红线

1. **DDE 主力净流入 ≠ 暗盘拆单，三榜 dark_top 也 ≠ 暗盘拆单，三者禁止合并/互算**：
   - `market_scan_ranks.payload.dark_top`：OHLC 日 K 分摊**对照项**，行级
     `approximation=True`，信封标 `dark_top_source="ohlc_approx"`；
   - `dark_fund_top_snapshots`：thsdk DDE **主力净流入**（同花顺官方 main_net 口径），
     行级 `source="thsdk_dde"`。它属于「主力/大单资金」，**不是**暗盘拆单/拆单隐藏意图；
   - 真正的暗盘拆单是 `.tck` 委托号级（`postmarket_review` / `dark_flow._detect_split_orders`），
     本模块**不提供**该口径，也不拿 DDE 数字冒充它；若 payload 附带
     `tck_dark_net_wan`，只作**并列另列**展示，不合并成单一数字。
   两榜各自独立函数、独立标签，本模块**不提供**合并入口。
2. **payload 不得冒充信封**：payload 自带的 `available` / `stale` / `snapshot_date` /
   `data_date` / `source` 等元信息一律不采信；`payload.available=False`（扫描失败落库）
   必须原样认账为 `available=False`，**不覆盖成 True**、不返回榜单内容。
3. **只读**：不 import `core.market_scan.scan` / `core.dark_fund_scan.scan_dark_fund_top`；
   不写 `market_scan_ranks` / `dark_fund_top_snapshots`。唯一写入是页面同规则的高价值
   接口审计行。
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException

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
    "FRESHNESS_FRESH",
    "FRESHNESS_STALE",
    "FRESHNESS_UNKNOWN",
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
#: 独立主力净流入榜来源：thsdk DDE 真实主力资金流（不是暗盘拆单）
DARK_FUND_SOURCE = "thsdk_dde"

#: 与页面 `log_high_value_call(..., api_name=...)` 完全一致的标签
RANKS_API_NAME = "opportunities"
DARK_FUND_API_NAME = "dark_fund"

#: 时效三态。`unknown` 是**正常返回值**，不是异常：日期不可确证时只说不知道。
FRESHNESS_FRESH = "fresh"
FRESHNESS_STALE = "stale"
FRESHNESS_UNKNOWN = "unknown"

#: 盘后 cron 落库时点（15:30 CST）：此前当日快照本不该存在
_SNAPSHOT_READY_HM: tuple[int, int] = (15, 30)

#: 信封自有的元信息键。payload 里出现同名键一律丢弃，防 payload 冒充新鲜度/来源。
_RESERVED_KEYS = frozenset(
    {
        "available",
        "snapshot_date",
        "data_date",
        "raw_snapshot_date",
        "expected_data_day",
        "freshness",
        "freshness_note",
        "stale",
        "age_days",
        "age_trading_days",
        "source",
        "market",
        "as_of",
        "now_cst",
        "time_source",
    }
)

_DISPLAY_ROWS = 10


# ──────────────────────────── 时钟（私有测试钩子） ────────────────────────────


def _now_cst() -> datetime:
    """当前可信系统时间（CST）。

    **私有测试钩子**：只允许单测 monkeypatch 来覆盖「现在几点」。
    生产与工具调用方没有任何时间入参 —— 新鲜度必须由系统时钟推导，
    否则调用方传一个 as_of 就能把两周前的快照说成新鲜。
    """
    return datetime.now(CST)


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


def _expected_data_day(now: datetime) -> date | None:
    """到此刻为止**本应已落库**的最近数据日；日历不可用时返回 None（不猜）。

    - 非交易日（周末/节假日）→ 最近上一个交易日；
    - 交易日且已过 15:30 落库时点 → 当日；
    - 交易日但未到 15:30 → 上一交易日（当日快照本来就还没生成）。
    """
    today = now.date()
    try:
        from src.core.trading_calendar import (
            is_trading_day,
            prev_trading_day,
            trading_day_anchor,
        )

        if not is_trading_day(today):
            return trading_day_anchor(today)
        if (now.hour, now.minute) >= _SNAPSHOT_READY_HM:
            return today
        return prev_trading_day(today)
    except Exception as e:  # noqa: BLE001
        # 红线: 日历不可用不回落自然日(那会把周末/节假日误判成过时或新鲜), 直接判不可判断
        logger.warning("交易日历不可用, 无法确定最近应有数据日: %r", e)
        return None


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


def _resolve_data_date(raw_snapshot: Any, today: date) -> tuple[date | None, str]:
    """快照行上的日期 → 可确证的**数据日**。返回 (data_date, 不可确证原因)。

    写入日 ≠ 数据日：盘后 cron / 手动 refresh 都是 `datetime.now()` 落 `snapshot_date`，
    节假日补跑（或历史手写行）会让写入日落在非交易日，那时无法判断里面装的是哪个
    交易日的数据 —— 只能如实说「数据日期未知」，不能拿写入日冒充数据日。
    """
    snap = _coerce_date(raw_snapshot)
    if snap is None:
        return None, f"快照日期 {raw_snapshot!r} 无法解析为日期"
    if snap > today:
        return None, f"快照日期 {snap} 晚于当前系统日期 {today}（时钟或落库异常）"
    try:
        from src.core.trading_calendar import is_trading_day

        trading = is_trading_day(snap)
    except Exception as e:  # noqa: BLE001
        return None, f"交易日历不可用（{e}），无法判定 {snap} 是否为交易日"
    if not trading:
        return None, (
            f"快照写入日 {snap} 不是 A 股交易日（节假日/周末休市/停市），"
            "写入日不等于数据日，无法确认数据所属交易日"
        )
    return snap, ""


def _freshness(raw_snapshot: Any, now: datetime) -> dict[str, Any]:
    """时效块：data_date / freshness / stale / age_* + 不可判断时的原因。"""
    today = now.date()
    expected = _expected_data_day(now)
    data_date, why = _resolve_data_date(raw_snapshot, today)
    snap = _coerce_date(raw_snapshot)
    raw = None if raw_snapshot is None else str(raw_snapshot).strip() or None

    meta: dict[str, Any] = {
        "as_of": today.isoformat(),
        "now_cst": now.isoformat(timespec="seconds"),
        "time_source": "system_clock_cst",
        "snapshot_date": snap.isoformat() if snap else None,
        "raw_snapshot_date": raw,
        "expected_data_day": expected.isoformat() if expected else None,
        "data_date": data_date.isoformat() if data_date else None,
        "freshness": FRESHNESS_UNKNOWN,
        "freshness_note": "",
        "stale": None,
        "age_days": None,
        "age_trading_days": None,
    }

    if data_date is None:
        meta["freshness_note"] = f"数据日期未知，时效不可判断：{why}"
        return meta

    # 数据日可确证 → 自然日龄总能算（不依赖交易日历），先落上
    meta["age_days"] = (today - data_date).days

    if expected is None:
        meta["freshness_note"] = (
            f"时效不可判断：{why or '交易日历不可用，无法确定最近应有数据日'}"
            f"（数据日 {data_date} 距当前 {meta['age_days']} 天）"
        )
        return meta

    stale = data_date < expected
    gap: int | None = 0
    note = ""
    if stale:
        gap = _trading_gap(data_date, expected)
        if gap is None:
            note = "落后交易日数不可计算（交易日历不可用）"
    meta.update(
        {
            "freshness": FRESHNESS_STALE if stale else FRESHNESS_FRESH,
            "freshness_note": note,
            "stale": stale,
            "age_trading_days": gap,
        }
    )
    return meta


# ──────────────── 身份 / 权限 + 审计（页面同源，身份额外 fail-closed） ────────────────


def _assert_page_identity(user: Any) -> None:
    """页面身份条件的对象级闸门（见模块 docstring「身份条件」）。

    只校验 `User` 对象自身状态；token 验签/`token_version`/会话存活由上游
    `get_current_user` 负责，本函数不声称也不具备验证 token 的能力。
    """
    if user is None:
        raise HTTPException(401, "未登录")

    if getattr(user, "is_service", False) or str(getattr(user, "role", "") or "").strip().lower() == "service":
        raise HTTPException(
            403,
            "服务身份不可读取该榜：页面端点只接受登录用户（服务令牌仅用于只读行情口）",
        )

    uid = str(getattr(user, "id", "") or "").strip()
    uname = str(getattr(user, "username", "") or "").strip()
    if not uid or not uname:
        raise HTTPException(401, "身份不完整（缺少用户 id / username）")

    if not getattr(user, "is_active", False):
        raise HTTPException(403, "账号已禁用")
    if getattr(user, "is_deleted", False):
        raise HTTPException(403, "账号已申请注销")


def _authorize(db: Session, user: Any, perm: str, api_name: str) -> None:
    """页面同一条权限链；顺序：身份 → 权限 → 高价值审计 → 读数据。

    - `_assert_page_identity` 补对象级状态闸（禁用/软删除/服务/空身份）；
    - `enforce_perm` 内部含 member 试用计数，无权限/试用尽抛 HTTPException；
    - `log_high_value_call` 是 best-effort，失败不影响读取（与页面一致）。
    """
    from src.core.hv_api_log import log_high_value_call
    from src.core.permissions import enforce_perm

    _assert_page_identity(user)
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
    now: datetime,
    unavailable_note: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把快照行整成信封：payload 平铺 + 元信息（元信息优先，payload 不许冒充）。

    `payload.available=False`（扫描失败也照样落库）必须原样认账为不可用 ——
    旧实现无条件 `available=True` 把它盖掉了。
    """
    payload_note = str(payload.get("note") or "").strip()

    if payload.get("available") is False:
        note = f"{payload_note or '扫描未成功'}"
        if unavailable_note:
            note = f"{note}（payload.available=false，不覆盖）"
        return _unavailable(source=source, market=market, now=now, note=note)

    meta = _freshness(getattr(row, "snapshot_date", None), now)
    if meta["data_date"] is None:
        # 数据日不可确证 → fail-closed: 不把无法定日的数据当最新榜单
        return _unavailable(
            source=source,
            market=market,
            now=now,
            note=f"{meta['freshness_note']}；本次不提供榜单数据（避免旧数据装新鲜）",
            raw_snapshot_date=meta["raw_snapshot_date"],
            extra=extra,
        )

    data: dict[str, Any] = {
        k: v for k, v in (payload or {}).items() if k not in _RESERVED_KEYS
    }
    data.update(
        {
            "available": True,
            "source": source,
            "market": getattr(row, "stock_market", None) or market,
            **{k: v for k, v in meta.items() if k != "snapshot_date"},
            "snapshot_date": meta["snapshot_date"],
            "updated_at": _iso(
                getattr(row, "updated_at", None) or getattr(row, "created_at", None)
            ),
            "note": payload_note,
        }
    )
    if extra:
        data.update(extra)
    return data


def _unavailable(
    *,
    source: str,
    market: str,
    now: datetime,
    note: str,
    raw_snapshot_date: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """无数据 / 数据日不可确证：`available=False` + 明确 note，绝不编造榜单。"""
    meta = _freshness(None, now)
    meta.update(
        {
            "snapshot_date": None,
            "raw_snapshot_date": raw_snapshot_date,
            "freshness": FRESHNESS_UNKNOWN,
            "freshness_note": note,
            "stale": None,
            "age_days": None,
            "age_trading_days": None,
        }
    )
    data: dict[str, Any] = {
        "available": False,
        "source": source,
        "market": market,
        **meta,
        "note": note,
    }
    if extra:
        data.update(extra)
    return data


# ──────────────────────────── 1. 市场三榜（读快照） ────────────────────────────


def read_market_scan_ranks(
    db: Session,
    user: User | None,
    market: str = "CN",
) -> dict[str, Any]:
    """读最新三榜快照（`market_scan_ranks`），权限 `view_opportunities`。

    与 `GET /api/market-scan/ranks` 同源同序：按 `snapshot_date DESC, id DESC`
    取最新一行；无快照/数据日不可确证返回 `available=False`，绝不编造榜单，也不触发扫描。

    新鲜度由**系统时钟**推导（本函数没有 `as_of`/`now` 入参，调用方无法伪造）。

    Raises:
        fastapi.HTTPException: 401 未登录/身份不完整；403 无权限、试用用尽、
            账号禁用/已注销、服务身份（页面同款）。
    """
    from src.core.permissions import PERM_VIEW_OPPORTUNITIES
    from src.db.models import MarketScanRank

    market = (market or "CN").strip().upper()
    _authorize(db, user, PERM_VIEW_OPPORTUNITIES, RANKS_API_NAME)

    now = _now_cst()
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
            now=now,
            note="暂无三榜快照(盘后 15:30 cron 落库, 或 POST /refresh 手动触发)；本次不猜数据。",
            extra={"dark_top_source": RANKS_DARK_TOP_SOURCE},
        )

    payload = row.payload if isinstance(row.payload, dict) else {}
    if not payload:
        return _unavailable(
            source=RANKS_SOURCE,
            market=market,
            now=now,
            note="快照行存在但 payload 为空/非对象；本次不猜数据。",
            raw_snapshot_date=str(row.snapshot_date),
            extra={"dark_top_source": RANKS_DARK_TOP_SOURCE},
        )

    return _envelope(
        row=row,
        payload=payload,
        source=RANKS_SOURCE,
        market=market,
        now=now,
        unavailable_note="三榜扫描未成功",
        extra={"dark_top_source": RANKS_DARK_TOP_SOURCE},
    )


# ───────────────────── 2. DDE 主力净流入（页面名「暗盘资金 TOP」） ─────────────────────


def read_dark_fund_top(
    db: Session,
    user: User | None,
    market: str = "CN",
) -> dict[str, Any]:
    """读最新 DDE 主力净流入快照（`dark_fund_top_snapshots`），权限 `view_dark`。

    表名/路由历史上叫「暗盘资金 TOP」，但口径是 thsdk DDE **主力净流入**
    （`source="thsdk_dde"`），**不是暗盘拆单/拆单隐藏意图**，也与三榜内
    `dark_top` 的 OHLC 分摊对照项**不是同一口径**，禁止互算。函数名沿用页面/表名
    以便溯源，渲染层一律按「DDE 主力净流入」称谓。

    无快照/数据日不可确证返回 `available=False`，不触发扫描。新鲜度由系统时钟推导。

    Raises:
        fastapi.HTTPException: 401 未登录/身份不完整；403 无权限、试用用尽、
            账号禁用/已注销、服务身份（页面同款）。
    """
    from src.core.permissions import PERM_VIEW_DARK
    from src.db.models import DarkFundTopSnapshot

    market = (market or "CN").strip().upper()
    _authorize(db, user, PERM_VIEW_DARK, DARK_FUND_API_NAME)

    now = _now_cst()
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
            now=now,
            note="暂无 DDE 主力净流入快照(盘后 15:30 cron 落库, 或 POST /dark-fund-top/refresh 手动触发)；本次不猜数据。",
        )

    payload = row.payload if isinstance(row.payload, dict) else {}
    if not payload:
        return _unavailable(
            source=DARK_FUND_SOURCE,
            market=market,
            now=now,
            note="快照行存在但 payload 为空/非对象；本次不猜数据。",
            raw_snapshot_date=str(row.snapshot_date),
        )

    return _envelope(
        row=row,
        payload=payload,
        source=DARK_FUND_SOURCE,
        market=market,
        now=now,
        unavailable_note="DDE 主力净流入扫描未成功",
    )


# ──────────────────────────── 3. 渲染（给未来工具 handler 直接返回） ────────────────────────────


def _num(value: Any, digits: int = 2) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "--"
    return f"{value:,.{digits}f}"


def _freshness_line(data: dict[str, Any]) -> str:
    """时效一行话：永远写清数据日与视角日期，`unknown` 时明说「不可判断」。"""
    freshness = data.get("freshness")
    if freshness == FRESHNESS_STALE:
        gap = data.get("age_trading_days")
        gap_txt = f"，落后 {gap} 个交易日" if gap is not None else ""
        return (
            f"⚠️ 快照已过期：数据日 {data.get('data_date')}"
            f"（最近应有数据日 {data.get('expected_data_day')}{gap_txt}）"
        )
    if freshness == FRESHNESS_FRESH:
        return (
            f"数据日 {data.get('data_date')}"
            f"（当前系统日期 {data.get('as_of')}；最近应有数据日 "
            f"{data.get('expected_data_day')}，未过期——不代表盘中实时）"
        )
    note = data.get("freshness_note") or "数据日期未知，时效不可判断"
    return f"⚠️ {note}"


def _head_date(data: dict[str, Any]) -> str:
    if data.get("data_date"):
        return f"数据日 {data['data_date']}"
    if data.get("raw_snapshot_date"):
        return f"写入日 {data['raw_snapshot_date']}（数据日未知）"
    return "数据日未知"


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
    """三榜读取结果 → 人话（未来 chat 工具直接返回；永远带数据日/来源）。"""
    if not data.get("available"):
        head = f"[市场扫描榜] {data.get('note') or '暂无三榜快照，本次不猜数据。'}"
        expected = data.get("expected_data_day")
        tail = f"（最近应有数据日 {expected}）" if expected else "（数据日期未知、时效不可判断）"
        return f"{head}{tail}；只读快照，不自动触发扫描。"

    lines: list[str] = [_freshness_line(data)]
    lines.append(
        f"[市场扫描榜 · 三榜 {_head_date(data)}] "
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
        "  暗盘资金 TOP（OHLC 日K分摊对照项，approximation=True；**非暗盘拆单**，"
        "也非 DDE 主力净流入，三口径不可互算）：" + (dark_txt or "无")
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
        "  口径：与页面 GET /api/market-scan/ranks 同源；只读快照，未触发扫描/刷新；"
        "时效由系统时钟推导，数据日见上。"
    )
    return "\n".join(lines)


def render_dark_fund_top(data: dict[str, Any]) -> str:
    """DDE 主力净流入读取结果 → 人话；与「暗盘拆单」「OHLC 近似」严格区分。"""
    if not data.get("available"):
        head = f"[DDE 主力净流入] {data.get('note') or '暂无快照，本次不猜数据。'}"
        expected = data.get("expected_data_day")
        tail = f"（最近应有数据日 {expected}）" if expected else "（数据日期未知、时效不可判断）"
        return f"{head}{tail}；只读快照，不自动触发扫描。"

    lines: list[str] = [_freshness_line(data)]
    lines.append(
        f"[DDE 主力净流入 TOP · thsdk_dde · 非暗盘拆单 · {_head_date(data)}] "
        f"扫描池 {data.get('universe')} 只｜拿到资金流 {data.get('computed')} 只"
    )
    if data.get("generated_at"):
        lines.append(f"  扫描生成时间：{data.get('generated_at')}")

    rows = [r for r in (data.get("top") or []) if isinstance(r, dict)]
    for i, r in enumerate(rows[:_DISPLAY_ROWS], 1):
        line = (
            f"  {i}. {r.get('symbol')} {r.get('name') or ''} "
            f"主力净额 {_num(r.get('main_net_wan'))} 万元"
            f"（成交额 {_num(r.get('total_amount_wan'))} 万元）"
        )
        # .tck 委托号级精确暗盘 = 真正的暗盘拆单口径; 只并列另列, 绝不与主力净额合并/互算
        if isinstance(r.get("tck_dark_net_wan"), (int, float)):
            line += f"｜委托号级暗盘拆单另列 {_num(r.get('tck_dark_net_wan'))} 万元（勿与主力净额互算）"
        lines.append(line)
    if len(rows) > _DISPLAY_ROWS:
        lines.append(f"  …另有 {len(rows) - _DISPLAY_ROWS} 只")
    if not rows:
        lines.append("  榜单为空（本次不猜）。")

    lines.append(
        "  口径：thsdk DDE **主力净流入**（main_net_wan，source=thsdk_dde），属主力/大单资金；"
        "**不是暗盘拆单**（暗盘拆单 = .tck 委托号级拆单隐藏意图），也不等于三榜 dark_top 的 "
        "OHLC 分摊近似项（approximation=True）—— 三者禁止合并/互算/互相校验。"
    )
    lines.append(
        "  只读快照，与页面 GET /api/market-scan/dark-fund-top 同源（页面历史名「暗盘资金 TOP」），"
        "未触发扫描/刷新；时效由系统时钟推导，数据日见上。"
    )
    return "\n".join(lines)
