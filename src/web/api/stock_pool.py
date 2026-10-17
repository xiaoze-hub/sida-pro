"""数智决策选股池 API(盘中实时, 2026-08-30)。

POST /api/stock-pool/screen
请求体: {"symbols": ["002361", "600519", ...]}
→ 对每只股票批量算数智决策三指标(GS + 机构活跃度 + L2主力净流入),
  按"共振强度"排序返回。

共振判定(买入方向):
  趋势分: gs.state == "G区"(**已确认**区间, 与 K 线图层同源) 或 gs.signal == "G"
  强度分: institution_activity.level in ("大牛","强势") 或 activity >= 3
  资金分: l2.available 且 zjl_hb > 0
  三项全满足="强", 满足两项="弱", 否则="无"。
  gs.state/signal 均为**已确认**口径(末根疑似交叉只作 pending, 不接管区间)。

2026-10-14 性能/缓存:
- **并发化**: `_screen` 由串行 for 改 `ThreadPoolExecutor`(上限 10, 参考
  `src/web/api/klines.py::_compute_kline_summary` 成熟写法)—— 50 只选股池此前**串行**
  打 TQ + 联网拉 K 线, 首屏耗时线性叠加; 并发后 ≈ 最长的一只。
- **结果缓存**: 进程内 dict(30s) → `biz_cache`(L1 内存 + L2 Redis, TTL 60s, 与同文件
  `screen_cached`/jbp 一致); 命中**零外呼**, 跨 worker / 重启共享。命中顺序:
  biz_cache(预落库基底) → 现算。
- **降级基底**: 单只实时取数彻底失败时, 尽力用 `decision_cache` 的**预落库全局基底**
  补 bars 派生字段(trend→GS 区 / 活跃度); 其不携带(或口径不同)的 gs_signal/pending/
  L2 资金一律如实置 None(维护单位口径, 不编造)。
- 单只失败标"无"不阻塞整批; 数据缺失显式 None 不编造。
"""
from __future__ import annotations

import concurrent.futures
import logging
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter()

_MAX_SYMBOLS = 50
#: 并发上限: 单股 Enrichment 是网络 I/O 瓶颈(TQ L2 + 联网 K 线), 每只互不依赖。
_MAX_WORKERS = 10
#: 结果缓存 TTL(秒): 与同文件 jbp `screen_cached` 的 60s 一致。
_SCREEN_TTL = 60

#: GS 趋势态 → 已确认区间(仅用于从 decision_cache 基底还原趋势维, 与 gs_state 同词表)。
_ZONE_BY_TREND = {"G信号": "G区", "G区间": "G区", "S信号": "S区", "S区间": "S区"}


class StockPoolScreenRequest(BaseModel):
    symbols: list[str] = Field(..., description="6位A股代码列表, 最多50只")


def _valid_symbols(raw: list[str]) -> list[str]:
    out: list[str] = []
    for s in raw:
        code = (s or "").strip()
        if code.isdigit() and len(code) == 6 and code not in out:
            out.append(code)
    return out[:_MAX_SYMBOLS]


def _resonance(gs, act, l2) -> tuple[str, int]:
    """返回 (共振级别, 得分 0-2)。

    2026-08-30 调整: GS 只做"趋势过滤"不做"买卖触发"——日线均线交叉的
    signal(G/S点)滞后一天(知乎第三方实测"套都被套死了才提示"), 实战无价值;
    只认 state(G区/S区)作方向过滤, S区/无GS 直接不共振。买卖强度看活跃度+L2资金。
    """
    # GS 趋势过滤: 只认 G区(趋势向上), S区/无GS 直接过滤
    # gs.state 已是**已确认**区间(单一事实源 gs_state.resolve_gs_state, 见 decision_pioneer);
    # 末根疑似反向交叉只作 gs.pending, 不抢跑区间。
    zone_g = bool(gs and gs.get("state") == "G区")
    activity = (act or {}).get("activity")
    if activity is None and (act or {}).get("level") in ("大牛", "强势"):
        activity = 3.0  # 老口径: level 达标等价活跃度达标
    fund = (l2 or {}).get("zjl_hb") if (l2 or {}).get("available") else None
    # 共享判定(唯一真相源, 与全市场共振扫描同规则)
    from src.core.resonance_scan import resonance_level

    return resonance_level(trend_zone_g=zone_g, activity=activity, fund_net=fund)


def _empty_row(symbol: str) -> dict:
    """实时+基底都拿不到 → 显式"无"(不编造), 不阻塞整批。"""
    return {
        "symbol": symbol,
        "activity": None,
        "activity_level": None,
        "gs_state": None,
        "gs_signal": None,
        "gs_pending": False,
        "l2_net": None,
        "resonance": "无",
        "score": 0,
        "source": "none",
    }


def _row_from_decision_base(symbol: str) -> Optional[dict]:
    """尽力从 `decision_cache` 的**预落库全局基底**还原一行(实时取数彻底失败时的兜底)。

    只取基底中**口径兼容**的字段: `parts.trend`(趋势标签 → GS 区)与 `parts.activity`
    (活跃度)。基底**不携带** gs_signal(已确认信号)/gs_pending, 资金维口径亦不同
    (基底 = `compute_pool_flow` 明暗盘 main_net; 本池 = L2·TQ zjl_hb) —— 这些字段
    一律**如实置 None**(绝不拿口径不符的数字冒充)。取不到/无可用信号 → None。
    """
    try:
        from src.core.decision_cache import get_cached_decision

        base = get_cached_decision(symbol, "CN")
    except Exception as e:  # noqa: BLE001 基底不可用 → 退回"无"
        logger.debug("stock-pool decision_cache 基底读取失败 %s: %s", symbol, e)
        return None
    if not base:
        return None
    payload = base.get("payload") or {}
    parts = payload.get("parts") or {}
    zone = _ZONE_BY_TREND.get(str(parts.get("trend") or ""))
    activity = parts.get("activity")
    act_ok = isinstance(activity, (int, float))
    if zone is None and not act_ok:
        return None  # 基底没给出任何可用信号 → 不冒充

    level = None
    if act_ok:
        try:
            from src.core.ai_activity import activity_of_value

            level = activity_of_value(activity).get("level")
        except Exception:  # noqa: BLE001 档位推导失败 → None
            level = None

    # 资金维无 L2 → 不参与共振(显式 available=False, 不拿基底明暗盘冒充 L2)
    level_str, score = _resonance(
        {"state": zone} if zone else None,
        {"activity": activity, "level": level},
        {"available": False},
    )
    return {
        "symbol": symbol,
        "activity": round(activity, 2) if act_ok else None,
        "activity_level": level,
        "gs_state": zone,
        "gs_signal": None,
        "gs_pending": False,
        "l2_net": None,
        "resonance": level_str,
        "score": score,
        "source": "decision_cache",
    }


def _row_of(symbol: str) -> dict:
    """单只股票 → 共振行(线程池内执行; 单只异常不阻塞整批)。"""
    from src.core.decision_pioneer import fetch_decision_pioneer

    try:
        d = fetch_decision_pioneer(symbol, "CN")
    except Exception as e:  # noqa: BLE001
        logger.warning("stock-pool screen %s failed: %s", symbol, e)
        d = None
    if not d:
        # 实时取数失败 → 退预落库基底; 仍无 → 显式"无"
        return _row_from_decision_base(symbol) or _empty_row(symbol)
    act = d.get("institution_activity")
    gs = d.get("gs")
    l2 = d.get("l2") or {}
    level, score = _resonance(gs, act, l2)
    zjl = l2.get("zjl_hb")
    return {
        "symbol": symbol,
        "activity": round(act["activity"], 2) if act and isinstance(act.get("activity"), (int, float)) else None,
        "activity_level": act.get("level") if act else None,
        "gs_state": gs.get("state") if gs else None,
        "gs_signal": gs.get("signal") if gs else None,
        # 末根疑似交叉(未定型): 显式透出, 供前端/复盘区分"已确认"与"待确认"
        "gs_pending": bool(gs.get("pending")) if gs else False,
        "l2_net": round(zjl / 1e4, 0) if isinstance(zjl, (int, float)) else None,  # 万元
        "resonance": level,
        "score": score,
        "source": "live",
    }


def _screen(symbols: list[str]) -> dict:
    truncated = False
    if len(symbols) > _MAX_SYMBOLS:
        truncated = True
        symbols = symbols[:_MAX_SYMBOLS]

    # 并发: 串行 for → ThreadPoolExecutor(网络 I/O 瓶颈: TQ L2 + 联网 K 线; 每只互不依赖)。
    # max_workers 取 min(上限, 标的数), 单只不必开池; `ex.map` 保序(排序前顺序与以前一致)。
    if len(symbols) <= 1:
        rows = [_row_of(s) for s in symbols]
    else:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(_MAX_WORKERS, len(symbols))
        ) as ex:
            rows = list(ex.map(_row_of, symbols))

    # 排序: resonance(强>弱>无) → score 降序 → symbol 升序
    order = {"强": 0, "弱": 1, "无": 2}
    rows.sort(key=lambda r: (order.get(r["resonance"], 2), -r["score"], r["symbol"]))
    return {"rows": rows, "total": len(rows), "truncated": truncated}


@router.post("/screen")
def screen_stock_pool(req: StockPoolScreenRequest):
    """数智决策选股池共振扫描(批量, 盘中实时)。

    结果走 `biz_cache`(L1 内存 + L2 Redis, TTL 60s): 命中**零外呼**, 跨 worker/重启共享
    (与同文件 `screen_cached`/jbp 同款写法); 未命中才现算并回填。

    2026-10-18 审计 A-03: 响应追加 `data_date / trade_phase / phase_label / is_live / as_of`
    —— 机会池也必须显式标注"数据属于哪个交易日 + 当前交易阶段", 不许前端用浏览器时钟
    冒充实时。时间字段**不进缓存**(缓存只存 rows, 组装时现算), 避免把缓存写入时刻当数据时刻。
    """
    symbols = _valid_symbols(req.symbols)
    if not symbols:
        return _with_phase({"rows": [], "total": 0, "truncated": False})

    from src.web.cache.biz_cache import biz_cache

    key = "stock_pool:screen:" + ",".join(symbols)
    cached = biz_cache.get_json(key)
    if cached is not None:
        return _with_phase(cached)
    data = _screen(symbols)
    biz_cache.set_json(key, data, ttl=_SCREEN_TTL)
    return _with_phase(data)


def _with_phase(payload: dict) -> dict:
    """给机会池响应注入时间语义(A-03)。缓存里只有 rows, 时刻在这里现算。"""
    from src.core.trade_phase import (
        PHASE_AUCTION,
        PHASE_CONTINUOUS,
        as_of_iso,
        cn_now,
        phase_meta,
    )
    from src.core.trading_calendar import trading_day_anchor

    meta = phase_meta()
    today = cn_now().date()
    data_date = trading_day_anchor(today)
    live = meta["trade_phase"] in (PHASE_AUCTION, PHASE_CONTINUOUS) and data_date == today
    return {
        **payload,
        "data_date": data_date.isoformat(),
        "trade_phase": meta["trade_phase"],
        "phase_label": meta["phase_label"],
        "is_live": live,
        "as_of": as_of_iso(),
        "source": "TQ L2 主力净额 + Engine 日线(GS/活跃度)",
    }


class JbpScreenRequest(BaseModel):
    symbols: list[str] = Field(..., description="6位A股代码列表")
    wencai_keywords: Optional[str] = Field(None, description="可选: 问财自然语言选股条件")


@router.post("/jbp")
def jbp_screen(req: JbpScreenRequest):
    """聚宝盆选股(规格 §4.2): 暗盘流入 AND 活跃度>6(>12更佳) AND GS在G区+G信号 AND 问财。

    逐条件 AND 联合筛选, 输出**逐条件通过/未过明细**(证据非建议); 任一必需条件缺数据
    → 显式降级、该股不出池(不编造)。结果走 biz_cache(60s)。
    """
    symbols = _valid_symbols(req.symbols)
    if not symbols:
        return {"universe": 0, "scanned": 0, "in_pool": 0, "rows": [], "wencai": {"provided": False}}
    data = screen_cached(symbols, req.wencai_keywords)
    return _attach_strength_label(data)


def _attach_strength_label(data: dict) -> dict:
    """给聚宝盆行附加首封×封成比强度标签(影子运行, 只读附加字段)。

    在**web 层**做(不污染 core 纯计算); 缓存里的原对象不可变 → 深拷再改。
    影子表无数据 → 各行 strength_label=None(前端不出徽章), 绝不编造。
    """
    import copy

    try:
        from src.core.limit_strength_shadow import labels_for_date

        lab = labels_for_date()  # 最近一日
        label_map = lab.get("labels") or {}
        date = lab.get("trade_date")
    except Exception as e:  # noqa: BLE001 影子表未迁移/异常 → 无徽章
        logger.debug("聚宝盆强度标签附加失败(不影响选股): %s", e)
        return data
    out = copy.deepcopy(data)
    for r in out.get("rows") or []:
        row = label_map.get(str(r.get("symbol")))
        r["strength_label"] = (row or {}).get("strength_label")
        r["strength_date"] = date if row else None
    return out


def screen_cached(symbols: list[str], wencai_keywords: Optional[str] = None) -> dict:
    """聚宝盆选股带缓存(biz_cache L1+L2, TTL 60s)。

    **缓存包装在 web 层**: B4.1 门禁禁止 src/core 引 src/web, biz_cache 位于
    src/web/cache —— core 只留纯计算(jbp_pool.screen), 缓存归本层。
    """
    from src.core import jbp_pool
    from src.web.cache.biz_cache import biz_cache

    codes = _valid_symbols(symbols)
    kw = (wencai_keywords or "").strip()
    key = "jbp:screen:" + ",".join(codes) + (f"|wencai={kw}" if kw else "")
    cached = biz_cache.get_json(key)
    if cached is not None:
        return cached
    data = jbp_pool.screen(codes, kw)
    biz_cache.set_json(key, data, ttl=60)
    return data
