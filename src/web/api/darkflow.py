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

from fastapi import APIRouter, HTTPException, Query

from marketdata import Symbol as MDSymbol
from marketdata.vendors.tencent import TencentQuoteVendor
from src.core.dark_flow import _judge_mnemonic, _position_from_range, compute_dark_flow

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
            mnemonic = _judge_mnemonic(dark, quote)
        except Exception as e:
            logger.debug(f"口诀判定失败 {symbol_code}: {e}")
    elif quote:
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

    return {
        "main_intent": main_intent,
        "inner_outer": inner_outer,
        "mnemonic": mnemonic,
        "l2": l2,
        "dark_order": dark_order,
    }


@router.get("")
def dark_flow(symbol: str = Query(..., description="6位A股代码, 如 002361")):
    """内盘外盘口诀 + 主力意图(轻接口, 分时卡片用)。"""
    return build_darkflow_response(symbol)


@router.get("/{symbol}")
def dark_flow_path(symbol: str):
    """路径式别名: /api/dark-flow/002361。"""
    return build_darkflow_response(symbol)
