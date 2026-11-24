"""真实持仓防空位核验（B4.1 首批 B，2026-11-21）。

## 解决什么

盘中的「持仓防守位有没有破」有两个互相独立的问题，此前都没有解：

1. **覆盖分母不对**。发布版的全市场管线（`pipeline_market`）按「正文点名的代码 + 自选」
   展开，上限 8 只。自选 ≠ 持仓，持仓 12 笔时第 9 笔开始根本不会被取数；更糟的是
   没有自选却有几笔持仓的用户，会拿到「未取到」而不知道自己在第几笔上失明。
   本模块的分母**只由权限校验后的实际持仓集驱动**：先按 `portfolio_scope` 的
   「账户可见 AND 持仓可见」取行，再按 市场+代码 合并取一次行情，**逐笔**判定。
   8 只是显示规模的上限，不是判定上限。
2. **「安全」是猜出来的**。模型读几行文字就会写「都在防守位上方，暂时安全」。
   这里把结论做成三值确定量：`已触发 / 未触发 / 无法核验`，并且
   **只有覆盖完整（每笔都真的核验过）才允许说全部未触发**；缺价位、缺行情、
   行情过期、时点证不出来，一律落 `无法核验`，绝不落「安全」。

## 契约来源（不新立口径）

- 防守位：`positions.defense_price`（绝对价，NULL = 用户没设，系统不许补一条）。
  击穿判定直接调用 `intraday_monitor.evaluate_defense_breach`
  —— 「现价 ≤ 防守价即击穿（含相等）」这一条只允许有一处实现。
- 持仓归属：`core.portfolio_scope` 的 `account_user_scope_clause` /
  `position_user_scope_clause`，与持仓页 `/api/accounts/positions`、
  `_portfolio_positions` 同源。
- 行情：`core.marketdata_client.md_quote_rows`，字段语义与
  `accounts._quote_to_row` / `quote_stream._proven_quote_stamp` 一致
  （`quote_date` 是证据不是服务端 today；时刻与日期互不自洽就都不用）。
- 时点：`MARKETS[..].is_trading_time`（A 股含交易日历）区分三种口径 ——
  连续竞价内要求当日快照且行龄 ≤ `DEFENSE_QUOTE_MAX_AGE_SEC`；午休/盘后当日
  按「当日最近一笔」判（日期必须还是今天）；休市只接受**最近一个已开盘过的
  交易日**的价，更早的（停牌多日）算过期。

## 边界

- 只读。不改持仓、不设防线、不写提醒、不产生交易指令。
- 只做实盘账户；模拟盘（`paper_trading`）不在本口径内，`defense_price` 也只挂在实盘持仓上。
- 严格本人归属优先：账户与持仓**两侧都 NULL**（无主历史行）不算任何人的持仓，
  不纳入分母，只在报告末尾报条数（与 `count_unattributed_positions` 同一态度）。
- 运行记录只写条数，不写完整私有数据（不落代码、不落价格）。

## 接线

`src/harness/portfolio_risk.py::answer(db, text, bound, user, runtime=None)`
与 `quick_answer.answer` 同形：命中且能给出确定性结论就返回字符串，否则返回
`None` 交回主流程。调用方在 `chat.py` 的问价快答之后加一段（见 README/交接说明），
本模块不 import `src.web`（B4.1 core 不依赖 web）。
"""

from __future__ import annotations

import asyncio
import logging
import math
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)

__all__ = [
    "DEFENSE_QUOTE_MAX_AGE_SEC",
    "Coverage",
    "HoldingRow",
    "HoldingVerdict",
    "answer",
    "evaluate_holdings",
    "is_portfolio_defense_question",
    "load_holdings",
    "render",
]

# 盘中对「现价」的时效上限（秒）。与库内快照 60s 复用口径不同 —— 那是缓存命中门槛,
# 这里是**结论可用性**门槛: 5 分钟内的一笔成交足以判「是否已跌破」, 再旧就只能说
# 无法核验。测试可显式传 now / max_age_sec 覆盖。
DEFENSE_QUOTE_MAX_AGE_SEC = 300.0

# 明细行显示上限：只影响排版，不影响判定与分母（>8 的支持体现在判定上）。
MAX_DETAIL_ROWS = 80

# 三值结论
VERDICT_BREACHED = "breached"
VERDICT_SAFE = "safe"
VERDICT_UNKNOWN = "unknown"

# 无法核验的原因（固定措辞，便于聚合与测试断言）
REASON_NO_LINE = "no_defense_line"          # 用户没设防守位 → 不猜、不判安全
REASON_MISSING_QUOTE = "missing_quote"      # 行情源没有这只票
REASON_INVALID_QUOTE = "invalid_quote"      # 有价字段但 <=0 / 非数值 / NaN
REASON_STALE_INTRADAY = "stale_intraday"    # 盘中, 但快照不是当日/太旧
REASON_STALE_CLOSE = "stale_close"          # 休市, 但价不是最近一个交易日的
REASON_UNKNOWN_TIME = "unknown_quote_time"  # 无时刻/无日期/自相矛盾 → 证不出时点
REASON_QUOTE_ERROR = "quote_error"          # 取数异常（源故障, 不是「没跌破」）
REASON_UNKNOWN_MARKET = "unknown_market"    # 市场代码不认识, 无法套时点规则

# 价格口径（用于文案, 不参与判定）
BASIS_INTRADAY = "intraday"   # 盘中, 行龄在阈值内
BASIS_TODAY = "today"         # 当日, 但当前不在连续竞价内（午休/盘后）
BASIS_CLOSE = "close"         # 更早一个交易日的收盘价
BASIS_NONE = "unknown"

_KNOWN_MARKETS = {"CN", "HK", "US"}

# ── 识别口径 ──────────────────────────────────────────────────────────────
# 收窄到「本人持仓 + 防守位 + 核查动作」三件同时成立。任何一条不成立就返回 None:
# 单股问价（"600519 现价多少"）没有持仓词; 诊断（"我的持仓怎么样"）没有防守词;
# 设置类（"帮我设防守位"）有写意图, 本模块不写任何东西。
_DEFENSE_WORDS = (
    "防守位", "防守线", "防守价", "防线", "纪律位", "纪律线",
    "破位", "破防守", "击穿", "跌破防守", "跌破防线",
)
_PORTFOLIO_WORDS = (
    "持仓", "仓位", "持股", "我的票", "我的股", "组合", "账户", "实盘",
)
_CHECK_WORDS = (
    "吗", "？", "?", "是否", "有没有", "有没", "检查", "查一下", "查查", "看一下",
    "看看", "确认", "核验", "触发", "破位", "击穿", "守住", "还在上方", "情况",
)
# 写意图 / 建议意图 → 交给主流程（会解释怎么设，但不能由本模块代办）
_WRITE_WORDS = (
    "设置", "设一个", "设个", "该设", "怎么设", "如何设", "建议", "帮我设",
    "调整为", "改一下", "改成", "取消", "清空", "删除", "清除",
)
# 全市场扫描意图：即便带了「持仓」也是问「市场里…」，不是核验自己的持仓
_MARKET_WORDS = ("大盘", "全市场", "两市", "选股", "哪些股票", "有什么股票", "买什么", "扫一遍")


@dataclass(frozen=True)
class HoldingRow:
    """一笔实盘持仓（判定单位；同券不同账户是两个单位）。"""

    position_id: int
    account_id: int
    account_name: str
    symbol: str
    market: str
    name: str
    quantity: float
    cost_price: float | None
    defense_price: float | None


@dataclass
class HoldingVerdict:
    row: HoldingRow
    status: str
    reason: str | None = None
    price: float | None = None
    basis: str = BASIS_NONE
    as_of: str | None = None       # 便于人读的时点（"11-21 10:03" / "11-20 收盘"）
    breach_pct: float | None = None  # (现价 - 防守价) / 防守价 * 100
    loss_pct: float | None = None    # (现价 - 成本) / 成本 * 100
    market_trading: bool = False


@dataclass
class Coverage:
    """覆盖分母。`complete` 只在每一笔都真的核验过时为真。"""

    total: int = 0
    verified: int = 0
    breached: int = 0
    safe: int = 0
    unknown: int = 0
    no_defense_line: int = 0
    unknown_reasons: dict[str, int] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return self.total > 0 and self.verified == self.total

    def as_dict(self) -> dict:
        return {
            "total": self.total,
            "verified": self.verified,
            "breached": self.breached,
            "safe": self.safe,
            "unknown": self.unknown,
            "no_defense_line": self.no_defense_line,
            "complete": self.complete,
            "unknown_reasons": dict(self.unknown_reasons),
        }


# ── 识别 ─────────────────────────────────────────────────────────────────


def is_portfolio_defense_question(text: str) -> bool:
    """是不是「核验本人持仓的防守位」这一问题。纯本地判定，不看 `bound`。

    `bound` 是会话状态，拿它判等于让上一轮的票改道这一轮（与 `pipeline_router`
    的 P0 教训同源）。真正的要判断的标的只认正文——且必须同时出现持仓词，
    否则「600519 破位了吗」这种单股问句会被这里抢走。
    """
    t = (text or "").strip()
    if not t or len(t) > 80:  # 长句基本是复合意图，交给主流程
        return False
    if any(w in t for w in _WRITE_WORDS):
        return False
    if any(w in t for w in _MARKET_WORDS):
        return False
    if not any(w in t for w in _DEFENSE_WORDS):
        return False
    if not any(w in t for w in _PORTFOLIO_WORDS):
        return False
    if not any(w in t for w in _CHECK_WORDS):
        return False
    # 点名了机构/国家队资金的问句不是「本人持仓」：否则「汇金持仓破位了吗」会拿到
    # 自己账户的核验（数据是真的，但答的不是他问的对象）。交给机构直查或主流程。
    if _mentions_institution(t) is not False:
        return False
    return True


def _mentions_institution(text: str) -> bool | None:
    """正文是否点名机构；None 表示识别失败，不得推定为本人持仓。"""
    try:
        from src.core.national_team import resolve_institution_scopes

        return bool(resolve_institution_scopes(text))
    except Exception as exc:  # noqa: BLE001 - 未核清主体，不读取本人数据
        logger.warning("[portfolio_risk] 无法确认机构范围，跳过本人持仓直查 (%s)", type(exc).__name__)
        return None


def _explicit_codes(text: str) -> list[str]:
    """正文里明确写出的 6 位代码（去重保序，排除「像 600111 这样的票」举例）。

    口径复用 `pipeline_router.question_codes`（同一套正则 + 举例判定），避免两处
    对「点名了哪只票」判断不一致；失败时交回主流程，不能把作用域扩大到全仓。
    """
    from src.harness.pipeline_router import question_codes

    return question_codes(text or "")


# ── 取数 ─────────────────────────────────────────────────────────────────


def load_holdings(db, user_id: str | None) -> tuple[list[HoldingRow], int, int]:
    """按归属取出本人实盘持仓 → (行, 无主行数, 零数量行数)。

    归属口径与持仓页同源（`portfolio_scope`），但**两侧都 NULL 的无主行不纳入**：
    那种行既不属于本人也不属于任何账户责任人，把它当「你的持仓」去判防守位，
    等于替用户编一个仓位。它单独计数，由调用方在报告里如实写「未纳入 N 笔」。
    """
    uid = str(user_id) if user_id else ""
    if not uid or db is None:
        return [], 0, 0

    from src.core.portfolio_scope import (
        account_user_scope_clause,
        position_user_scope_clause,
    )
    from src.db.models import Account, Position, Stock

    base = (
        db.query(Position, Stock, Account)
        .join(Stock, Stock.id == Position.stock_id)
        .join(Account, Account.id == Position.account_id)
        .filter(
            account_user_scope_clause(uid),
            position_user_scope_clause(uid),
        )
    )
    rows: list[HoldingRow] = []
    unattributed = 0
    zero_qty = 0
    for pos, stock, account in base.order_by(
        Account.id.asc(), Position.sort_order.asc(), Position.id.asc()
    ).all():
        # 账户与持仓都无所属人 → 无法归属到任何人, 不计数为「本人持仓」。
        if getattr(account, "user_id", None) is None and getattr(pos, "user_id", None) is None:
            unattributed += 1
            continue
        if account.enabled is False:
            continue  # 停用账户不参与(与 _portfolio_positions 的 enabled_accounts_only 同口径)
        qty = _finite(getattr(pos, "quantity", None))
        if qty is None or qty <= 0:
            zero_qty += 1
            continue
        symbol = str(getattr(stock, "symbol", "") or "").strip()
        market = str(getattr(stock, "market", "") or "").strip().upper()
        if not symbol:
            continue
        rows.append(
            HoldingRow(
                position_id=int(getattr(pos, "id", 0) or 0),
                account_id=int(getattr(account, "id", 0) or 0),
                account_name=str(getattr(account, "name", "") or ""),
                symbol=symbol,
                market=market,
                name=str(getattr(stock, "name", "") or symbol),
                quantity=float(qty),
                cost_price=_positive(getattr(pos, "cost_price", None)),
                defense_price=_positive(getattr(pos, "defense_price", None)),
            )
        )
    # 上面已经把「两侧都 NULL」剔掉, 让 SQL 与 Python 判定保持同一条口径。
    return rows, unattributed, zero_qty


async def _fetch_quotes(rows: list[HoldingRow]) -> tuple[dict, set[str]]:
    """按 市场+代码 合并取行情 → ({(market, symbol): row}, 取数失败的市场集合)。

    为什么按市场分组: `md_quote_rows` 的接口就是批量的, 逐笔拉会把 12 个持仓
    变成 12 次 RPC。合并只影响**取数次数**, 不影响判定 —— 每笔持仓仍各自比自己的
    防守价（同券不同账户必须能得出不同结论）。
    """
    groups: dict[str, set[str]] = {}
    for r in rows:
        groups.setdefault(r.market, set()).add(r.symbol)

    out: dict[tuple[str, str], dict] = {}
    failed: set[str] = set()
    if not groups:
        return out, failed
    try:
        from src.core.marketdata_client import md_quote_rows
    except Exception as e:  # noqa: BLE001 - 行情客户端不可用 → 全部无法核验, 不是安全
        logger.warning("portfolio_risk: 行情客户端不可用: %s", type(e).__name__)
        return out, set(groups)

    for market, symbols in groups.items():
        try:
            items = await asyncio.to_thread(md_quote_rows, sorted(symbols), market)
        except Exception as e:  # noqa: BLE001 - 单市场故障不影响其它市场
            logger.warning("portfolio_risk: %s 行情批量失败: %s", market, type(e).__name__)
            failed.add(market)
            continue
        for item in items or []:
            sym = str(item.get("symbol") or "").strip()
            if sym:
                out[(market, sym)] = item
    return out, failed


# ── 判定 ─────────────────────────────────────────────────────────────────


def evaluate_holdings(
    rows: list[HoldingRow],
    quotes: dict,
    *,
    failed_markets: set[str] | None = None,
    now: datetime | None = None,
    max_age_sec: float = DEFENSE_QUOTE_MAX_AGE_SEC,
) -> tuple[list[HoldingVerdict], Coverage]:
    """逐笔判定 → (明细, 覆盖)。纯函数（除 `now` 默认为当前时刻外无副作用）。"""
    failed = set(failed_markets or ())
    out: list[HoldingVerdict] = []
    cov = Coverage(total=len(rows))

    for row in rows:
        trading = _market_trading(row.market, now)
        quote = quotes.get((row.market, row.symbol))
        verdict = _judge_one(
            row,
            quote,
            trading=trading,
            quote_failed=row.market in failed,
            now=now,
            max_age_sec=max_age_sec,
        )
        out.append(verdict)

        if verdict.status == VERDICT_BREACHED:
            cov.breached += 1
        elif verdict.status == VERDICT_SAFE:
            cov.safe += 1
        else:
            cov.unknown += 1
            reason = verdict.reason or REASON_UNKNOWN_TIME
            cov.unknown_reasons[reason] = cov.unknown_reasons.get(reason, 0) + 1
            if reason == REASON_NO_LINE:
                cov.no_defense_line += 1
    cov.verified = cov.breached + cov.safe

    # 排序: 已跌破(跌得最深在前) → 无法核验(缺位最先, 提示去设) → 未跌破(最贴近防线在前)。
    out.sort(key=_sort_key)
    return out, cov


_STATUS_ORDER = {VERDICT_BREACHED: 0, VERDICT_UNKNOWN: 1, VERDICT_SAFE: 2}


def _sort_key(v: HoldingVerdict) -> tuple:
    """稳定排序键。同券异账户必须靠 `position_id` 收尾，否则两次渲染顺序会抖。"""
    rank = _STATUS_ORDER.get(v.status, 9)
    if v.status == VERDICT_BREACHED:
        depth = v.breach_pct if v.breach_pct is not None else 0.0   # 越负 = 跌破越深 = 越前
    elif v.status == VERDICT_SAFE:
        depth = abs(v.breach_pct) if v.breach_pct is not None else 1e9  # 越贴近防线 = 越前
    else:
        depth = 0.0
    return (
        rank,
        0 if v.reason == REASON_NO_LINE else 1,
        depth,
        v.row.account_id,
        v.row.symbol,
        v.row.position_id,
    )


def _judge_one(
    row: HoldingRow,
    quote: dict | None,
    *,
    trading: bool,
    quote_failed: bool,
    now: datetime | None,
    max_age_sec: float,
) -> HoldingVerdict:
    """单笔判定。顺序刻意先判「有没有防线」——没设防守位时现价再新也不能判安全。"""
    base = HoldingVerdict(row=row, status=VERDICT_UNKNOWN, market_trading=trading)

    if row.market not in _KNOWN_MARKETS:
        base.reason = REASON_UNKNOWN_MARKET
        return base
    if row.defense_price is None:
        # 未设防守位: 直接落到「无法核验」, 且**不去说它安全**。现价仍尽量补上,
        # 便于用户对照自己该在哪设（只读, 不设）。
        price, basis, as_of, _reason = _resolve_price(
            quote if not quote_failed else None,
            row.market,
            trading=trading,
            now=now,
            max_age_sec=max_age_sec,
            failed=quote_failed,
        )
        base.reason = REASON_NO_LINE
        base.price = price
        base.basis = basis
        base.as_of = as_of
        base.loss_pct = _loss_pct(price, row.cost_price)
        return base

    price, basis, as_of, reason = _resolve_price(
        quote if not quote_failed else None,
        row.market,
        trading=trading,
        now=now,
        max_age_sec=max_age_sec,
        failed=quote_failed,
    )
    base.price = price
    base.basis = basis
    base.as_of = as_of
    base.loss_pct = _loss_pct(price, row.cost_price)
    if price is None:
        base.reason = reason or REASON_MISSING_QUOTE
        return base

    # 阈值口径只认一处实现: 现价 <= 防守价即击穿(含相等)。
    from src.agents.intraday_monitor import evaluate_defense_breach

    verdict = evaluate_defense_breach(
        defense_price=row.defense_price,
        current_price=price,
        cost_price=row.cost_price,
    )
    if verdict is None:  # 理论不可达(上面已保证 d>0 且 cur>0), 保守按无法核验
        base.reason = REASON_UNKNOWN_TIME
        return base

    base.breach_pct = (price - row.defense_price) / row.defense_price * 100.0
    base.status = VERDICT_BREACHED if verdict["breached"] else VERDICT_SAFE
    base.reason = None
    return base


def _resolve_price(
    quote: dict | None,
    market: str,
    *,
    trading: bool,
    now: datetime | None,
    max_age_sec: float,
    failed: bool = False,
) -> tuple[float | None, str, str | None, str | None]:
    """行情 → (可用现价, 口径, 时点文案, 不可用原因)。

    时点证据规则（与 `quote_stream._proven_quote_stamp` 同款自洽检查）:
    `quote_date` 是源证据, 绝不是服务端 today; 时刻与日期互相矛盾 → 两个都不用。
    """
    if failed:
        return None, BASIS_NONE, None, REASON_QUOTE_ERROR
    if not quote:
        return None, BASIS_NONE, None, REASON_MISSING_QUOTE

    price = _positive(quote.get("current_price"))
    if price is None:
        return None, BASIS_NONE, None, (
            REASON_MISSING_QUOTE if quote.get("current_price") in (None, "") else REASON_INVALID_QUOTE
        )

    status = str(quote.get("status") or "").strip().lower()
    if status == "missing":
        return None, BASIS_NONE, None, REASON_MISSING_QUOTE

    quote_time_raw = str(quote.get("quote_time") or "").strip()
    declared_date = str(quote.get("quote_date") or "").strip()[:10]
    time_date = quote_time_raw[:10] if len(quote_time_raw) >= 10 else ""
    if time_date and declared_date and time_date != declared_date:
        # 上游串源: 宁可 unknown, 也不替它挑一个「看起来对」的。
        return None, BASIS_NONE, None, REASON_UNKNOWN_TIME
    quote_date = declared_date or time_date
    if not quote_date:
        return None, BASIS_NONE, None, REASON_UNKNOWN_TIME
    try:
        src_date = date.fromisoformat(quote_date)
    except ValueError:
        return None, BASIS_NONE, None, REASON_UNKNOWN_TIME

    market_now = _market_now(market, now)
    market_today = market_now.date()

    if trading:
        if src_date != market_today:
            return None, BASIS_NONE, None, REASON_STALE_INTRADAY
        stamp = _parse_quote_time(quote_time_raw, market)
        if stamp is None:
            # 日期只证到「是哪一天」, 盘中还需要「是不是刚刚」。
            return None, BASIS_NONE, None, REASON_UNKNOWN_TIME
        age = (market_now - stamp).total_seconds()
        if age < -5.0 or age > max_age_sec:
            return None, BASIS_NONE, None, REASON_STALE_INTRADAY
        return price, BASIS_INTRADAY, stamp.strftime("%m-%d %H:%M"), None

    expected = _last_opened_session(market, market_now)
    if expected is None:  # 交易日历不可用 → 证不出「最近一个交易日」, 不猜
        return None, BASIS_NONE, None, REASON_UNKNOWN_TIME
    if src_date != expected:
        return None, BASIS_NONE, None, REASON_STALE_CLOSE
    if src_date == market_today:
        stamp = _parse_quote_time(quote_time_raw, market)
        as_of = stamp.strftime("%m-%d %H:%M") if stamp else src_date.isoformat()
        return price, BASIS_TODAY, as_of, None
    return price, BASIS_CLOSE, src_date.isoformat(), None


# ── 市场时点 ─────────────────────────────────────────────────────────────


def _market_now(market: str, now: datetime | None) -> datetime:
    """把「现在」换成该市场的本地时间（CN 由 ZoneInfo 给出）。"""
    from src.models.market import MARKETS, MarketCode

    mdef = MARKETS.get(MarketCode(market))
    if mdef is None:
        return now or datetime.now()
    tz = mdef.get_tz()
    if now is None:
        return datetime.now(tz)
    if now.tzinfo is None:
        return now.replace(tzinfo=tz)
    return now.astimezone(tz)


def _market_trading(market: str, now: datetime | None) -> bool:
    """当前是否在该市场的连续竞价内（A 股含交易日历/调休）。未知市场 → False。"""
    if market not in _KNOWN_MARKETS:
        return False
    try:
        from src.models.market import is_market_trading_time

        return bool(is_market_trading_time(market, now))
    except Exception as e:  # noqa: BLE001 - 日历未覆盖年份等; 退成「休市」口径再验日期
        logger.warning("portfolio_risk: %s 交易时段判定失败: %s", market, type(e).__name__)
        return False


def _last_opened_session(market: str, market_now: datetime) -> date | None:
    """最近一个**已经开过盘**的交易日（休市时用来界定「收盘价」的可接受日期）。

    今日若已有开盘（含收盘后/午休），当日就是它；否则退到前一个交易日。
    A 股走 `trading_calendar`（节假日/调休），港美按工作日。日历未覆盖 → None
    （调用方据此把结论降为「无法核验」，不许猜一个日期出来）。
    """
    from src.models.market import MARKETS, MarketCode

    mdef = MARKETS.get(MarketCode(market))
    if mdef is None or not mdef.sessions:
        return None
    first_open = mdef.sessions[0].start
    cur = market_now.date()
    for _ in range(45):  # 长假 + 停牌兜底上限
        try:
            ok = _is_trading_day(market, cur)
        except Exception:  # noqa: BLE001 - TradingCalendarError 等
            return None
        if ok and (cur < market_now.date() or market_now.time() >= first_open):
            return cur
        cur = cur - timedelta(days=1)
    return None


def _is_trading_day(market: str, d: date) -> bool:
    if market == "CN":
        from src.core.trading_calendar import is_trading_day

        return bool(is_trading_day(d))
    return d.weekday() < 5


def _parse_quote_time(raw: str, market: str) -> datetime | None:
    """解析源报时。不带时区的按**市场时区**理解（与 vendor `_parse_quote_time`
    的 `parsed.replace(tzinfo=ZoneInfo(market_tz))` 同口径）—— 这是解释源报文，
    不是拿服务端 now 去补时刻；万一是别家按 UTC 发的，行龄检查会把它判成过期
    （失败方向是「无法核验」，不是「安全」）。"""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        from src.models.market import MARKETS, MarketCode

        mdef = MARKETS.get(MarketCode(market))
        if mdef is None:
            return None
        dt = dt.replace(tzinfo=mdef.get_tz())
    return dt


# ── 排版 ─────────────────────────────────────────────────────────────────


def render(
    verdicts: list[HoldingVerdict],
    coverage: Coverage,
    *,
    unattributed: int = 0,
    zero_qty: int = 0,
    scoped_codes: list[str] | None = None,
    error_note: str | None = None,
) -> str:
    """确定性排版。结论第一行必须能独立读懂「有没有判完、能不能说安全」。"""
    total = coverage.total
    if total == 0:
        head = "【持仓防守位核验】实盘账户里没有可核验的持仓。"
        tail = (
            "\n\n口径：只核验实盘账户（模拟盘不在本口径内）；未做任何写操作。"
        )
        if unattributed:
            tail += f"\n另有 {unattributed} 笔归属未标注的历史持仓（账户与持仓均无所属人）未纳入。"
        return head + tail

    if coverage.complete and coverage.breached == 0:
        concl = (
            f"结论：{total} 笔全部核验完毕，**均未跌破**你自己设的防守位"
            f"（覆盖 {coverage.verified}/{total}）。"
        )
    elif coverage.complete:
        concl = (
            f"结论：{coverage.breached} 笔**已跌破**防守位，{coverage.safe} 笔未跌破"
            f"（覆盖 {coverage.verified}/{total}）。"
        )
    else:
        concl = (
            f"结论：已跌破 {coverage.breached} 笔，未跌破 {coverage.safe} 笔，"
            f"无法核验 {coverage.unknown} 笔 —— 覆盖 {coverage.verified}/{total}，"
            f"**不能据此认定其余持仓安全**。"
        )

    lines: list[str] = ["【持仓防守位核验】按实盘账户逐笔判定（行情按 市场+代码 合并取用）", ""]
    if scoped_codes:
        lines.append(f"本次只核验正文点名的：{'、'.join(scoped_codes)}")
        lines.append("")
    lines.append(concl)
    lines.append("")

    shown = 0
    for title, status in (("▍已跌破（现价 ≤ 防守价）", VERDICT_BREACHED),
                          ("▍无法核验（不判安全）", VERDICT_UNKNOWN),
                          ("▍未跌破（现价 > 防守价）", VERDICT_SAFE)):
        group = [v for v in verdicts if v.status == status]
        if not group:
            continue
        lines.append(title)
        for v in group:
            if shown >= MAX_DETAIL_ROWS:
                break
            lines.append(_row_line(v))
            shown += 1
        lines.append("")
    if shown < len(verdicts):
        lines.append(f"（另有 {len(verdicts) - shown} 笔未逐行展开；结论与分母已包含它们。）")
        lines.append("")

    lines.append("▍口径")
    lines.append("- 防守价取你在持仓页自己设的绝对价（positions.defense_price）；未设 ≠ 有防线，不代你补 -8%。")
    lines.append("- 连续竞价内：现价须为当日且源快照距现在 ≤5 分钟；午休/盘后当日：按当日最近一笔；休市：只接受最近一个开盘交易日的收盘价，更早算过期。")
    lines.append("- 现价恰好等于防守价按「已跌破」计（与设置防守线的端点同口径）。")
    lines.append("- 同一只票在多个账户里分别判定，各按自己那笔的防守价。")
    lines.append("- 只读：不改持仓、不设防线、不写提醒、不下单。以上为数据核验，不构成投资建议。")

    if coverage.no_defense_line:
        lines.append("")
        lines.append(
            f"提示：有 {coverage.no_defense_line} 笔持仓没设防守位，"
            "这类仓位任何价格都不会被判「安全」——需要你自己在持仓页写下那条价。"
        )
    if unattributed or zero_qty:
        lines.append("")
        extra = []
        if unattributed:
            extra.append(f"{unattributed} 笔归属未标注（账户与持仓均无所属人）")
        if zero_qty:
            extra.append(f"{zero_qty} 笔数量为 0")
        lines.append("未纳入：" + "、".join(extra) + "。")
    if error_note:
        lines.append("")
        lines.append(error_note)
    return "\n".join(lines)


def _row_line(v: HoldingVerdict) -> str:
    row = v.row
    who = f"{row.name} {row.symbol}（{row.account_name or '未命名账户'}）"
    if v.status == VERDICT_UNKNOWN:
        why = _reason_text(v)
        price = ""
        if v.price is not None:
            price = f"，现价 {_px(v.price)}"
            if v.as_of:
                price += f"（{_basis_text(v)}）"
        return f"- {who} — {why}{price}"
    bits = [f"现价 {_px(v.price)} vs 防守 {_px(row.defense_price)}"]
    if v.breach_pct is not None:
        if v.breach_pct <= 0:
            bits.append(f"低于防线 {abs(v.breach_pct):.2f}%")
        else:
            bits.append(f"高于防线 {v.breach_pct:.2f}%")
    if v.loss_pct is not None:
        bits.append(f"成本 {_px(row.cost_price)}（浮动 {v.loss_pct:+.2f}%）")
    if v.as_of:
        bits.append(_basis_text(v))
    return f"- {who} " + "，".join(bits)


def _basis_text(v: HoldingVerdict) -> str:
    if v.basis == BASIS_INTRADAY:
        return f"源快照 {v.as_of}"
    if v.basis == BASIS_TODAY:
        return f"今日 {v.as_of}"
    if v.basis == BASIS_CLOSE:
        return f"最近收盘 {v.as_of}"
    return ""


_REASON_TEXT = {
    REASON_NO_LINE: "未设防守位（无法判定，也不判安全）",
    REASON_MISSING_QUOTE: "取不到现价（行情源无此标的）",
    REASON_INVALID_QUOTE: "现价不可用（<=0/非数值）",
    REASON_STALE_INTRADAY: "盘中行情已过期（非当日或超过 5 分钟）",
    REASON_STALE_CLOSE: "休市行情不是最近一个交易日（疑似停牌/陈旧）",
    REASON_UNKNOWN_TIME: "行情时点无法证明（缺时刻/日期或自相矛盾）",
    REASON_QUOTE_ERROR: "行情取数失败（数据源异常）",
    REASON_UNKNOWN_MARKET: "市场代码无法识别（套不上时点规则）",
}


def _reason_text(v: HoldingVerdict) -> str:
    return _REASON_TEXT.get(v.reason or "", "无法核验")


# ── 数值 ─────────────────────────────────────────────────────────────────


def _finite(value) -> float | None:
    """None / 非数值 / bool / NaN / Inf → None；0 是有效数值，不动它。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _positive(value) -> float | None:
    """可用价格/价位：有限且 > 0（0/负数不是价格）。"""
    f = _finite(value)
    return f if f is not None and f > 0 else None


def _loss_pct(price: float | None, cost: float | None) -> float | None:
    if price is None or cost is None or cost <= 0:
        return None
    return (price - cost) / cost * 100.0


def _px(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else "-"


# ── 对外入口（与 quick_answer.answer 同形） ───────────────────────────────


async def answer(db, text: str, bound: str | None, user, runtime=None) -> str | None:
    """核验本人实盘持仓的防守位 → 确定性文本；不适用/不可判 → None 交回主流程。

    返回 None 的三种情况（都刻意保守）:
      1. 问题不是「本人持仓 + 防守位 + 核查」；
      2. 没有用户会话（服务令牌/后台线程）—— 私有数据路径一律 fail-closed；
      3. 取数或判定过程中出现未预期异常（快路径不许把对话弄挂）。
    """
    try:
        if not is_portfolio_defense_question(text):
            return None
        uid = getattr(user, "id", None)
        if db is None or not uid:
            return None

        t0 = _time.perf_counter()
        scoped = [c for c in _explicit_codes(text) if c]
        rows, unattributed, zero_qty = load_holdings(db, uid)
        if scoped:
            rows = [r for r in rows if r.symbol in scoped]
            # 点名收窄后, 「未纳入」的计数(无主行/零数量)属于全量口径, 会误导, 不再展示。
            unattributed = zero_qty = 0

        quotes, failed = await _fetch_quotes(rows)
        verdicts, coverage = evaluate_holdings(rows, quotes, failed_markets=failed)
        out = render(
            verdicts,
            coverage,
            unattributed=unattributed,
            zero_qty=zero_qty,
            scoped_codes=scoped if scoped else None,
        )
        if scoped and coverage.total == 0:
            held = _held_codes(db, uid)
            held_txt = ""
            if held:
                shown = "、".join(held[:20]) + ("等" if len(held) > 20 else "")
                held_txt = f"（你在实盘持有的：{shown}）"
            out = (
                f"【持仓防守位核验】正文点名的 {'、'.join(scoped)} 不在你的实盘持仓里"
                + held_txt
                + "。\n\n口径：只核验本人实盘账户的持仓；未做任何写操作。"
            )

        _trace = getattr(runtime, "trace", None)
        if _trace is not None:
            # 只落条数与市场, 不落代码/价格/账户名 —— 运行记录不留完整私有数据。
            # 「用户没设防守位」是配置状态，不是故障：只标 partial，不判失败；
            # 行情缺失/过期/取数失败/时钟证不出才是数据不可用，必须落 failed ——
            # 否则 run 以 ok 收尾，治理页和基线会把「证不出安全」记成成功。
            reasons = dict(coverage.unknown_reasons)
            blocking = sum(v for k, v in reasons.items() if k != REASON_NO_LINE)
            reason_text = ",".join(f"{k}:{v}" for k, v in sorted(reasons.items())) or "-"
            ok = blocking == 0
            try:
                _trace.step(
                    "portfolio_defense_check",
                    {"scope": "self_holdings", "markets": sorted({r.market for r in rows})},
                    ok=ok,
                    error=None if ok else "holdings_not_fully_verified",
                    result=f"total={coverage.total} verified={coverage.verified} "
                           f"breached={coverage.breached} unknown={coverage.unknown} "
                           f"no_defense_line={coverage.no_defense_line} reasons={reason_text}",
                    elapsed_ms=int((_time.perf_counter() - t0) * 1000),
                )
            except Exception:  # noqa: BLE001 - 埋点失败不影响作答
                pass

        logger.info(
            "portfolio_risk: 防守位核验 total=%d verified=%d breached=%d unknown=%d",
            coverage.total, coverage.verified, coverage.breached, coverage.unknown,
        )
        return out
    except Exception as e:  # noqa: BLE001 - 快路径失败 → 正常走模型
        logger.warning("portfolio_risk 快路径失败，回落主流程: %s", type(e).__name__)
        return None


def _held_codes(db, user_id: str) -> list[str]:
    """本人实盘持仓代码（只用于「你点名的不在持仓里」的引导文案）。"""
    try:
        rows, _u, _z = load_holdings(db, user_id)
        out: list[str] = []
        for r in rows:
            if r.symbol not in out:
                out.append(r.symbol)
        return out
    except Exception:  # noqa: BLE001
        return []
