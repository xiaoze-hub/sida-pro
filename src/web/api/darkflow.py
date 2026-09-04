"""内盘外盘口诀 + 主力意图 轻接口(分时图卡片渲染用, 2026-08-13)。

GET /api/dark-flow?symbol=002361
GET /api/dark-flow/002361
→ 响应包装后 {code:0, success:true, data: {main_intent, inner_outer, mnemonic}}

- main_intent: 主力意图(净额/参与度/买占比/信号文本/数据状态), 复用 compute_dark_flow
- inner_outer: 内盘外盘(金额/占比/量比/涨跌/位置), 结构化供卡片
- mnemonic: 7 口诀命中(规则预判, 只提示不改结论), 未命中 None; 数据不足给中性提示

不依赖 chat.py 的 get_main_intent(那是文本摘要, 给对话用); 本接口是给前端卡片的结构化数据。
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from marketdata import Symbol as MDSymbol
from marketdata.vendors.tencent import TencentQuoteVendor
from src.core.dark_flow import (
    _judge_mnemonic,
    _position_from_range,
    clear_ticks_cache,
    compute_dark_flow,
    compute_tck_active_ratio,  # v0.4.79: .tck 主动率(口诀活代码化)
)
from src.web.api.auth import require_owner

logger = logging.getLogger(__name__)

router = APIRouter()


def _fetch_quote_dict(symbol: MDSymbol) -> dict | None:
    """拉腾讯 Quote 转 dict(供口诀判定/字段兜底), 失败返回 None 不崩。"""
    try:
        q = TencentQuoteVendor().fetch([symbol], {})[0]
        return {
            "current_price": q.current_price,
            "high_price": q.high_price,
            "low_price": q.low_price,
            "prev_close": q.prev_close,
            "change_pct": q.change_pct,
            "volume_ratio": q.volume_ratio,
            "volume_outer": q.volume_outer,
            "volume_inner": q.volume_inner,
            "volume": q.volume,
        }
    except Exception as e:
        logger.debug(f"dark-flow quote 获取失败 {symbol}: {e}")
        return None


def _validate_symbol(raw: str) -> MDSymbol:
    code = (raw or "").strip()
    if not code.isdigit() or len(code) != 6:
        raise HTTPException(400, f"非法股票代码: {raw!r}(需要6位A股代码)")
    return MDSymbol.parse(code, "CN")


def build_darkflow_response(symbol_code: str) -> dict:
    """核心组装(供 endpoint 与测试直接调用): 主力意图 + 内盘外盘 + 口诀。"""
    symbol = _validate_symbol(symbol_code)
    dark = None
    try:
        dark = compute_dark_flow(symbol)
    except Exception as e:
        logger.warning(f"dark-flow compute 异常 {symbol_code}: {e}")
        dark = None
    if not dark:
        raise HTTPException(502, "暗盘/逐笔数据获取失败, 请稍后重试(可能非交易时段或无成交)")

    quote = _fetch_quote_dict(symbol)
    data_status = dark.get("data_status", "ok")

    main_intent = {
        "main_net": dark.get("main_net"),
        "big_net": dark.get("big_net"),
        "mid_net": dark.get("mid_net"),
        # 散户净额: dark_flow 的 result 字段名是 small_net(2026-08-15 修复:
        # 之前读 retail_net → None → 前端显示 "--", 信息不全)
        "retail_net": dark.get("small_net", dark.get("retail_net")),
        "main_intensity": dark.get("main_intensity"),
        "main_buy_ratio": dark.get("main_buy_ratio"),
        "signal": dark.get("signal"),
        "data_status": data_status,
        # 2026-09-04 P1-4: 结论翻转注记(无翻转则 None, 前端有值才展示)。
        "verdict_note": dark.get("verdict_note"),
    }

    # 内盘外盘: compute_dark_flow 已算好金额/占比; 量比/涨跌/位置用本接口自拉的 Quote 兜底刷新
    inner_outer = dict(dark.get("inner_outer") or {})
    if quote:
        for k in ("volume_ratio", "change_pct"):
            if quote.get(k) is not None:
                inner_outer[k] = quote.get(k)
        if not inner_outer.get("position") or inner_outer.get("position") == "unknown":
            inner_outer["position"] = _position_from_range(quote)
    if data_status == "insufficient":
        inner_outer["data_status"] = "insufficient"
        inner_outer["note"] = "逐笔数据不足(开盘初期/成交稀疏), 内外盘占比仅供参考"

    # 口诀: 数据充分才按规则判定; 不足给中性提示(卡片可直接展示)
    mnemonic = None
    if data_status == "ok":
        try:
            # v0.4.79 口诀活代码化: 有 .tck 数据时用「主动率」原始口诀, 无则兜底
            tck_ratio = compute_tck_active_ratio(symbol_code)
            mnemonic = _judge_mnemonic(dark, quote, tck_active_ratio=tck_ratio)
        except Exception as e:
            logger.debug(f"口诀判定失败 {symbol_code}: {e}")
    elif quote:
        # 2026-09-04: suspect(熔断)与 insufficient(真不足)分开文案。
        # 此前 suspect 也套用"不足30笔"模板, 实际逐笔 5 万+却显示数据不足, 误导。
        if data_status == "suspect":
            mnemonic = {
                "mnemonic": "数据异常",
                "direction": "中性",
                "divergence": False,
                "detail": "主力成交额超总成交额(疑逐笔重复计数), 本轮不判意图, 口诀暂停",
            }
        else:
            mnemonic = {
                "mnemonic": "数据不足",
                "direction": "中性",
                "divergence": False,
                "detail": "逐笔成交不足30笔, 内盘外盘口诀暂不判定(数据不足)",
            }

    # L2 主力净流入(TQ get_more_info 盘中实时, 明盘口径: 同花顺"主力净额", 非暗盘)
    l2 = None
    try:
        from src.core.decision_pioneer import _l2_summary, fetch_tq_l2
        l2 = _l2_summary(fetch_tq_l2(symbol_code))
    except Exception as e:  # noqa: BLE001
        logger.debug(f"dark-flow L2 获取失败 {symbol_code}: {e}")

    # 暗盘资金(拆单识别 v3): 主力伪装的中小单(逆势+位置确认), 同花顺"暗盘资金"口径。
    # split_order = {buy_amt(疑似主力买), sell_amt(疑似主力卖), net(暗盘净额),
    #                herd_buy/herd_sell(散户顺势/解套), groups(拆单组明细top10)}
    dark_order = dark.get("split_order")
    # 2026-09-04: 簇只有日内时刻(t0/t1), 跨日时(如昨日尾盘簇)无日期会误导。
    # 逐笔按自然日重置, 簇日期恒为当日, 此处显式标注。
    trade_date = None
    if isinstance(dark_order, dict):
        try:
            from src.core.dark_flow import _cache_day
            trade_date = _cache_day()
            dark_order = {**dark_order, "trade_date": trade_date}
        except Exception:  # noqa: BLE001
            pass

    # 2026-09-04 P1-5: stale 只标停滞(data_status 不动, 不断口诀链路)。
    _stale = _tick_staleness(dark.get("last_tick_t"), trade_date)
    return {
        "main_intent": main_intent,
        "inner_outer": inner_outer,
        "mnemonic": mnemonic,
        "l2": l2,
        "dark_order": dark_order,
        # 2026-09-04: 运维可见性(冻住了一眼可见): 逐笔总数/末笔时刻/交易日/拉到页数。
        "diag": {
            "tick_count": dark.get("tick_count"),
            "last_tick_t": dark.get("last_tick_t"),
            "trade_date": trade_date,
            "tick_pages": dark.get("tick_pages"),
            "stale": _stale["stale"],
            "tick_lag_sec": _stale["lag_sec"],
        },
    }


def _tick_staleness(
    last_tick_t: str | None,
    trade_date: str | None,
    now=None,
) -> dict:
    """P1-5 停滞检测(纯函数, now 可注入单测)。

    仅工作日 09:25-15:05 内判定: 末笔落后超 10 分钟 → stale=True。
    非交易时段/跨日/无数据 → stale=False(不误报)。data_status 不动,
    只是 diag 里标, 口诀链路不断。
    """
    import datetime as _dt
    now = now or _dt.datetime.now()
    info: dict = {"stale": False, "lag_sec": None}
    try:
        if not last_tick_t or trade_date != now.date().isoformat():
            return info
        if now.weekday() >= 5:
            return info
        if not ("09:25:00" <= now.strftime("%H:%M:%S") <= "15:05:00"):
            return info
        h, m, s = (int(x) for x in last_tick_t.split(":"))
        lag = (now.hour * 3600 + now.minute * 60 + now.second) - (h * 3600 + m * 60 + s)
        info["lag_sec"] = lag
        info["stale"] = lag > 600
    except Exception:  # noqa: BLE001
        pass
    return info


@router.post("/cache/clear")
def clear_darkflow_ticks_cache(
    symbol: str | None = Query(default=None, description="6位A股代码, 如 002361; 不传=清全部"),
    owner=Depends(require_owner),
):
    """清逐笔缓存(运维杠杆, 仅管理员)。

    main_net 长时间钉死不动时手动清, 下次请求全量重拉(去重修复在重拉路径生效)。
    同步落盘, 重启不回血。返回清除的缓存数 + 下次是否重拉。
    """
    from src.core.dark_flow import _tencent_code

    if symbol:
        tcode = _tencent_code(_validate_symbol(symbol))
        n = clear_ticks_cache(tcode) if tcode else 0
        return {"cleared": n, "symbol": symbol, "tcode": tcode, "refetch_next": True}
    n = clear_ticks_cache(None)
    return {"cleared": n, "symbol": None, "tcode": None, "refetch_next": True}


@router.post("/refetch")
def refetch_darkflow(
    symbol: str = Query(..., description="6位A股代码, 如 002361"),
    owner=Depends(require_owner),
):
    """强制全量重拉并返回 diff(运维杠杆 P2-7, 仅管理员)。

    清缓存→重算, 一次看清: 去重删了多少笔、结论变没变。
    before 走缓存(秒回), after 全量重拉(数秒)。生产验证例:
    002361 before 4717笔/-1.25亿 → after 3958笔/+3490万。
    """
    from src.core.dark_flow import _tencent_code

    before = build_darkflow_response(symbol)
    tcode = _tencent_code(_validate_symbol(symbol))
    cleared = clear_ticks_cache(tcode) if tcode else 0
    after = build_darkflow_response(symbol)
    b, a = before.get("diag") or {}, after.get("diag") or {}
    b_net = (before.get("main_intent") or {}).get("main_net")
    a_net = (after.get("main_intent") or {}).get("main_net")
    return {
        "symbol": symbol,
        "cleared": cleared,
        "before": {"tick_count": b.get("tick_count"), "main_net": b_net},
        "after": {
            "tick_count": a.get("tick_count"),
            "main_net": a_net,
            "data_status": (after.get("main_intent") or {}).get("data_status"),
        },
        "dedup_removed": (b.get("tick_count") or 0) - (a.get("tick_count") or 0),
        "verdict_changed": bool(
            b_net is not None and a_net is not None and ((b_net < 0) != (a_net < 0))
        ),
        "verdict_note": (after.get("main_intent") or {}).get("verdict_note"),
    }


@router.get("")
def dark_flow(symbol: str = Query(..., description="6位A股代码, 如 002361")):
    """内盘外盘口诀 + 主力意图(轻接口, 分时卡片用)。"""
    return build_darkflow_response(symbol)


@router.get("/{symbol}")
def dark_flow_path(symbol: str):
    """路径式别名: /api/dark-flow/002361。"""
    return build_darkflow_response(symbol)
