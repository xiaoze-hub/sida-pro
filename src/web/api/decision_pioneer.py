"""数智决策三指标 API(盘中实时)。

GET /api/decision-pioneer/002361?market=CN
→ {symbol, market, institution_activity, gs, l2, main_intent, data_time}

进程内 30s 缓存(盘中多用户/多轮询防重复重算)。
权限(2026-09-15): view_forecast — member 3次/天试用, pro/owner 全开。

跨市场诚实降级(2026-10-10 P2-7): 三指标里的 L2 主力净流入/主力意图是 **CN 专有源**
(腾讯逐笔 + TQ get_more_info), 非 CN(HK/US)拿不到 → 由原来的裸 `400` 改为**结构化降级**:
HTTP 200 + `l2_supported=false` + `degraded=true` + 显式 `note`『仅 CN 支持 L2 主力』;
趋势/活跃度(K线可得)仍如实返回。**不编造资金、不静默 None**。
"""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from src.core.decision_pioneer import fetch_decision_pioneer
from src.web.api.auth import get_current_user
from src.web.database import get_db
from src.web.models import User

logger = logging.getLogger(__name__)

router = APIRouter()

# 进程内缓存: {key: (ts, data)}, TTL 30s
_CACHE: dict[str, tuple[float, dict]] = {}
_TTL = 30.0

# 非 CN 降级说明(唯一出处, 主/历史端点共用)
L2_NON_CN_NOTE = "仅 CN 支持 L2 主力; 非 CN 已降级(无 L2 主力/主力意图, 趋势/活跃度可用)"


def _valid_symbol(raw: str, market: str = "CN") -> str:
    """按市场校验股票代码: CN=6位数字 / HK=5位数字 / 其余(US 等)字母数字放宽。"""
    code = (raw or "").strip()
    mkt = (market or "CN").upper()
    if mkt == "CN":
        ok = code.isdigit() and len(code) == 6
    elif mkt == "HK":
        ok = code.isdigit() and len(code) == 5
    else:  # US 等: 允许字母/数字/`.`/`-`(如 BRK.B / AAPL)
        ok = bool(code) and all(ch.isalnum() or ch in ".-" for ch in code)
    if not ok:
        raise HTTPException(400, f"非法股票代码: {raw!r}(市场 {mkt})")
    return code


def _fmt_trade_date(v) -> str:
    """YYYYMMDD → YYYY-MM-DD; 不可解析原样返回(不编造)。"""
    s = str(v or "")
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) >= 8 and s[:8].isdigit() else s


# 卡片「主力净流入(L2·TQ)」实际展示的实时会话字段(Zjl_HB 单列为元的口径在上游 _l2_summary 已转)。
# 全 0 = 非交易时段会话值, 不能当真实值(与 /quotes/{symbol}/more-info 同款判据)。
_L2_LIVE_FIELDS = ("zjl_hb", "cancel_buy", "cancel_sell", "l2_tick_num", "l2_order_num")


def _apply_l2_fallback(data: dict) -> dict:
    """非交易时段 / 会话值全 0 → 回退最近收盘快照 + 显式 as_of; 无快照 → available:false。

    真 bug(2026-10-11, 同 /quotes/{symbol}/more-info 的 P1 修复): 卡片「主力净流入(L2·TQ)」
    走 **另一条取数路**(`fetch_decision_pioneer` → TQ `get_more_info` 实时会话值)。非交易时段
    TQ 一律回 0, 卡片显示\"0万 平衡 / 逐笔0笔·委托0笔\", 把 0 冒充真实值。此处套**同款回退**:
      - 会话任一字段非 0 → 原样返回(标 source='live');
      - 全 0 → `latest_snapshot()` 最近收盘快照, 显式 source/as_of/note(0 不冒充);
      - 无快照 → `available:false` + 显式无数据文案(绝不返 0)。
    B4.1: 回退+标注在 web 层做, 落库/读取在 `l2_fund_snapshot` core 模块。
    """
    l2 = data.get("l2")
    if not isinstance(l2, dict) or not l2.get("available"):
        return data

    from src.core.l2_fund_snapshot import has_live_values, latest_snapshot

    live_vals = {f: l2.get(f) for f in _L2_LIVE_FIELDS}
    if has_live_values(live_vals):
        l2.setdefault("source", "live")
        return data

    from src.core.trading_calendar import is_trading_session

    in_session = is_trading_session()
    snap = None
    try:
        snap = latest_snapshot(data.get("symbol") or "", data.get("market") or "CN")
    except Exception as e:  # noqa: BLE001 回退失败不挡端点, 走显式无数据
        logger.warning("decision-pioneer L2 收盘快照回退失败 %s: %s", data.get("symbol"), e)

    reason = "非交易时段" if not in_session else "实时会话值全 0"
    if snap:
        zjl_wan = snap.get("zjl_hb")  # 快照落的是 TQ 原始值(万元)
        net_yuan = zjl_wan * 1e4 if isinstance(zjl_wan, (int, float)) else None
        direction = None
        if net_yuan is not None:
            direction = "净流入" if net_yuan > 0 else ("净流出" if net_yuan < 0 else "平衡")
        data["l2"] = {
            **l2,
            "zjl_hb": net_yuan,
            "direction": direction,
            "l2_tick_num": snap.get("l2_tick_num"),
            "l2_order_num": snap.get("l2_order_num"),
            "available": True,
            "source": snap.get("origin") or "snapshot",
            "as_of": _fmt_trade_date(snap.get("trade_date")),
            "note": f"{reason}·显示 {_fmt_trade_date(snap.get('trade_date'))} 收盘值",
        }
        return data

    data["l2"] = {
        "available": False,
        "source": "session_closed",
        "as_of": None,
        "note": f"{reason}·无收盘快照, 无数据(不冒充 0)",
    }
    return data


def _get(symbol: str, market: str) -> dict:
    key = f"{market}:{symbol}"
    now = time.time()
    hit = _CACHE.get(key)
    # 非交易时段(盘后/周末): 延长内存缓存 TTL 至 3600 秒(1小时), 避免切标签时重复重算 120 根 K 线
    from src.core.quote_snapshots import in_trading_window
    in_trading = in_trading_window()
    effective_ttl = _TTL if in_trading else 3600.0

    if hit and now - hit[0] < effective_ttl:
        return hit[1]
    data = fetch_decision_pioneer(symbol, market)
    # 非交易时段 L2 会话值全 0 → 回退收盘快照/显式无数据(0 不冒充), 与 more-info 同款
    data = _apply_l2_fallback(data)
    _CACHE[key] = (now, data)
    # 09-03: 新鲜快照落库(历史回查用; best-effort, 失败不影响读链路)
    try:
        from src.core.history_store import record_dp_snapshot

        record_dp_snapshot(symbol, market, data)
    except Exception:  # noqa: BLE001
        pass
    return data


@router.get("/{symbol}/history")
def get_decision_pioneer_history(
    symbol: str,
    market: str = "CN",
    days: int = 30,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """数智决策历史快照(09-03 落库回查; 空=该股尚无落库, 不编造)。"""
    from src.core.permissions import PERM_VIEW_FORECAST, enforce_perm

    enforce_perm(user, PERM_VIEW_FORECAST, db)
    mkt = market.upper()
    code = _valid_symbol(symbol, mkt)
    if mkt != "CN":
        # 非 CN 无 L2 主力 → 结构化降级(显式), 不裸 400
        return {
            "symbol": code, "market": mkt, "rows": [],
            "l2_supported": False, "degraded": True, "note": L2_NON_CN_NOTE,
        }
    from src.core.history_store import query_dp_history

    return {"symbol": code, "market": mkt, "rows": query_dp_history(code, mkt, days)}


@router.get("/{symbol}")
def get_decision_pioneer(
    symbol: str,
    market: str = "CN",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """数智决策三指标 + L2 主力净流入 + 主力意图(盘中实时)。
    调用日志(2026-09-15 C.1): 每次调用落 high_value_api_logs。"""
    from src.core.permissions import PERM_VIEW_FORECAST, enforce_perm

    enforce_perm(user, PERM_VIEW_FORECAST, db)
    from src.core.hv_api_log import log_high_value_call

    log_high_value_call(db, user, "forecast", symbol)
    mkt = market.upper()
    code = _valid_symbol(symbol, mkt)
    if mkt != "CN":
        # 非 CN: L2 主力/主力意图无源 → 结构化降级(显式标注可用性), 不裸 400
        data: dict
        try:
            data = dict(fetch_decision_pioneer(code, mkt))
        except Exception as e:  # noqa: BLE001
            logger.warning("decision-pioneer 非 CN 取数 %s failed: %s", code, e)
            data = {"symbol": code, "market": mkt}
        data.setdefault("symbol", code)
        data.setdefault("market", mkt)
        data["l2_supported"] = False
        data["degraded"] = True
        data["note"] = L2_NON_CN_NOTE
        return data
    try:
        return _get(code, mkt)
    except Exception as e:  # noqa: BLE001
        logger.warning("decision-pioneer API %s failed: %s", code, e)
        raise HTTPException(502, f"数智决策数据获取失败: {e}") from e
