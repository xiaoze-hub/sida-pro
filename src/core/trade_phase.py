"""交易阶段 / 数据日期标注(2026-10-18, 审计 A-03 修复)。

**铁律**: 任何"池 / 榜 / 快照"类接口都必须显式回答两件事 ——

1. **这份数据属于哪个交易日**(`data_date`)；
2. **现在是交易日的哪个阶段**(`trade_phase`)，据此决定它是不是"实时"(`is_live`)。

修复的原始缺陷(`docs/research/SIDA_PRO整体项目深度审计与创新功能规划.md` A-03):
竞价池响应体既没有 `trade_date` 也没有 `as_of`，前端用 `new Date()`(浏览器本地时钟)
渲染"同步 15:00:12"，周日打开时用户会以为"系统刚刚同步了竞价"，实际看到的是上周五
9:25 的池子。浏览器时钟**不是**数据时刻，必须由后端给。

本模块只做"时间语义"，不碰业务判断，供竞价池 / 机会池 / 转债雷达等共用。
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from src.core.trading_calendar import is_trading_day, trading_day_anchor

CN_TZ = ZoneInfo("Asia/Shanghai")

#: 交易阶段枚举(前端按此渲染表头文案，不按浏览器时钟猜)
PHASE_NON_TRADING_DAY = "non_trading_day"
PHASE_PRE_OPEN = "pre_open"              # 交易日 00:00-09:15
PHASE_AUCTION = "auction"                # 09:15-09:25 集合竞价
PHASE_AUCTION_MATCH = "auction_match"    # 09:25-09:30 撮合定型
PHASE_CONTINUOUS = "continuous"          # 09:30-11:30 / 13:00-15:00
PHASE_NOON_BREAK = "noon_break"          # 11:30-13:00
PHASE_POST_CLOSE = "post_close"          # 15:00 之后

_PHASE_LABELS: dict[str, str] = {
    PHASE_NON_TRADING_DAY: "非交易日",
    PHASE_PRE_OPEN: "盘前",
    PHASE_AUCTION: "集合竞价 09:15-09:25",
    PHASE_AUCTION_MATCH: "竞价撮合 09:25-09:30",
    PHASE_CONTINUOUS: "连续竞价",
    PHASE_NOON_BREAK: "午间休市",
    PHASE_POST_CLOSE: "收盘后",
}

#: 这些阶段拿到的最新池子可以标"实时"(前提是 data_date == 今日)
_LIVE_PHASES = frozenset({PHASE_AUCTION, PHASE_CONTINUOUS})


def cn_now(now: datetime | None = None) -> datetime:
    """归一到 Asia/Shanghai；naive 视为上海墙上时间(便于单测注入)。"""
    if now is None:
        return datetime.now(CN_TZ)
    if now.tzinfo is None:
        return now.replace(tzinfo=CN_TZ)
    return now.astimezone(CN_TZ)


def trade_phase(now: datetime | None = None) -> str:
    """当前交易阶段。周末/节假日一律 `non_trading_day`(不用星期近似)。"""
    n = cn_now(now)
    if not is_trading_day(n.date()):
        return PHASE_NON_TRADING_DAY
    t = n.time()
    if t < datetime.strptime("09:15", "%H:%M").time():
        return PHASE_PRE_OPEN
    if t <= datetime.strptime("09:25", "%H:%M").time():
        return PHASE_AUCTION
    if t < datetime.strptime("09:30", "%H:%M").time():
        return PHASE_AUCTION_MATCH
    if t <= datetime.strptime("11:30", "%H:%M").time():
        return PHASE_CONTINUOUS
    if t < datetime.strptime("13:00", "%H:%M").time():
        return PHASE_NOON_BREAK
    if t <= datetime.strptime("15:00", "%H:%M").time():
        return PHASE_CONTINUOUS
    return PHASE_POST_CLOSE


def phase_label(phase: str) -> str:
    return _PHASE_LABELS.get(phase, phase or "未知阶段")


def phase_meta(now: datetime | None = None) -> dict:
    """阶段元数据(直接摊进接口响应)。"""
    n = cn_now(now)
    phase = trade_phase(n)
    return {
        "trade_phase": phase,
        "phase_label": phase_label(phase),
        "is_trading_day": is_trading_day(n.date()),
        "is_live_session": phase in _LIVE_PHASES,
        "now": n.isoformat(timespec="seconds"),
    }


def parse_date(v: date | datetime | str | None) -> date | None:
    """尽力把 date/datetime/`YYYY-MM-DD`/`YYYYMMDD`/ISO 串解析成 date, 失败 None。"""
    if v is None:
        return None
    if isinstance(v, datetime):
        return cn_now(v).date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    try:  # ISO datetime
        return datetime.fromisoformat(s.replace(" ", "T")[:19]).date()
    except ValueError:
        return None


def resolve_data_date(
    *,
    now: datetime | None = None,
    last_sync: date | datetime | str | None = None,
    prefer_last_sync: bool = True,
) -> date | None:
    """推断"这份池子属于哪个交易日"。

    规则(宁可直接说不知道，也不拿今天冒充)：
    - 非交易时段 / 盘前：取最近一个交易日 = `trading_day_anchor(今天)`；
      `last_sync` 若是更早的交易日(说明今天还没同步过)，如实取 `last_sync`；
    - 交易时段内：今天就是数据所属日(池子现在就能刷新)；
    - 都推不出来 → None(调用方必须显式渲染"数据日期未知")。
    """
    n = cn_now(now)
    today = n.date()
    sync_d = parse_date(last_sync)
    anchor = trading_day_anchor(today)

    if is_trading_day(today):
        # 盘中/竞价：今天的池子就是今天的；但若池子是非竞价阶段拿到的缓存，
        # 而今天还没有任何同步记录，仍按"最近交易日"标注更诚实。
        if phase_meta(n)["is_live_session"]:
            return today
        if prefer_last_sync and sync_d and sync_d < anchor:
            return sync_d
        return anchor
    # 非交易日：永远只能给最近一个交易日(绝对不返回今天)
    if prefer_last_sync and sync_d and sync_d <= anchor:
        return sync_d
    return anchor


def data_date_iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


def is_live_now(*, data_date: date | None, now: datetime | None = None) -> bool:
    """`data_date` 是今天且当前处于可刷新的交易阶段，才算实时。"""
    n = cn_now(now)
    if data_date is None or data_date != n.date():
        return False
    return phase_meta(n)["is_live_session"]


def as_of_iso(now: datetime | None = None) -> str:
    """**服务端**组装时刻(替换前端 `new Date()` 的浏览器时钟)。"""
    return cn_now(now).isoformat(timespec="seconds")
