"""thsdk 高价值待接能力 API(v0.3.1 目标 A)

覆盖 11 个 thsdk 1.7.18 剩余能力:
- 资讯/基本面: news / corporate_action / dde / hs300
- 跨市场行情: market_data_cn(扩展1, 主力净流入) / index / hk / us / bond / fund
- AI 选股: wencai-enhanced(带 30s 缓存)

全部端点走 `protected` 鉴权(见 app.py 注册处的 dependencies=protected,
端点内部仍用 get_current_user 保证单端点鉴权)。

⚠️ 游客账户对 market_data_cn(扩展1)/index/hk 返 0 行:代码/路由已建好,
等正式账户解锁(同 v0.3.0 风控/灰度策略)。端点对空结果返回 rows=[] 而非报错。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from src.web.api.auth import get_current_user

logger = logging.getLogger(__name__)

# 模块层 import 让 monkeypatch.setattr 能替换(否则 lazy import 是局部变量)。
# thsdk 未安装/导入失败时置 None,运行时再 lazy import 兜底。
try:
    from data_source.thsdk_l2 import (
        get_news,
        get_corporate_action,
        get_main_flow_official,  # 2026-08-20: thsdk 1.7.18 无 get_dde(), 改调主净额官方口径
        get_hs300_constituents,
        get_market_data_cn_extended,
        get_market_data_index,
        get_market_data_hk,
        get_market_data_us,
        get_market_data_bond,
        get_market_data_fund,
        get_wencai_enhanced,
    )  # noqa: F401
except Exception:  # noqa: BLE001 - thsdk 不可用时降级,不阻塞 import
    get_news = None  # type: ignore[assignment]
    get_corporate_action = None  # type: ignore[assignment]
    get_main_flow_official = None  # type: ignore[assignment]
    get_hs300_constituents = None  # type: ignore[assignment]
    get_market_data_cn_extended = None  # type: ignore[assignment]
    get_market_data_index = None  # type: ignore[assignment]
    get_market_data_hk = None  # type: ignore[assignment]
    get_market_data_us = None  # type: ignore[assignment]
    get_market_data_bond = None  # type: ignore[assignment]
    get_market_data_fund = None  # type: ignore[assignment]
    get_wencai_enhanced = None  # type: ignore[assignment]

router = APIRouter(tags=["thsdk-extended"])


# ---------- 通用工具 ----------

def _invoke(name: str, *args: Any) -> Any:
    """调 data_source.thsdk_l2 的包装函数(支持测试 monkeypatch 模块级属性)。"""
    fn: Optional[Callable] = globals().get(name)
    if fn is None:
        try:
            mod = __import__("data_source.thsdk_l2", fromlist=[name])
            fn = getattr(mod, name, None)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[thsdk-extended] lazy import 失败 {name}: {e}")
            fn = None
    if fn is None:
        raise RuntimeError(f"thsdk 数据源不可用: {name}")
    return fn(*args)


def _to_jsonable(value: Any) -> Any:
    """值 → JSON 安全(优先用项目统一 to_jsonable,失败回退 str)。"""
    try:
        from src.core.json_safe import to_jsonable

        return to_jsonable(value)
    except Exception:  # noqa: BLE001
        try:
            import json

            return json.loads(json.dumps(value, ensure_ascii=False, default=str))
        except Exception:  # noqa: BLE001
            return str(value)


def _rows(df: Any) -> List[dict]:
    """DataFrame → JSON 安全 records 列表;空/异常返回 []。"""
    if df is None:
        return []
    try:
        records = df.to_dict("records")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[thsdk-extended] DataFrame 转 records 失败: {e}")
        return []
    rows = _to_jsonable(records)
    return rows if isinstance(rows, list) else []


def _fetch_rows(name: str, *args: Any) -> List[dict]:
    """拉取能力 + 转 rows(异常转 HTTPException)。"""
    try:
        fn = _invoke(name, *args)
        return _rows(fn)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[thsdk-extended] {name} 查询失败: {e}", exc_info=True)
        raise HTTPException(
            500, f"thsdk {name} 查询失败: {str(e)[:120]}"
        ) from e


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _summarize(name: str, *args: Any) -> dict:
    """取 DataFrame + 汇总 dict(端点共用返回结构)。"""
    df = _invoke(name, *args)
    return {
        "rows": _rows(df),
        "count": 0 if df is None else int(getattr(df, "shape", (0,))[0]),
        "fetched_at": _now(),
    }


# ---------- 端点 ----------

@router.get("/news/{symbol}")
def api_news(symbol: str, user=Depends(get_current_user)) -> dict:
    """个股新闻(thsdk news)。symbol 传 thsdk 代码,如 USZA002361。"""
    try:
        out = _summarize("get_news", symbol)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"news 失败: {e}") from e
    out["symbol"] = symbol
    return out


@router.get("/corporate-action/{symbol}")
def api_corporate_action(symbol: str, user=Depends(get_current_user)) -> dict:
    """公司行动(分红/送转/配股等)。"""
    try:
        out = _summarize("get_corporate_action", symbol)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"corporate-action 失败: {e}") from e
    out["symbol"] = symbol
    return out


@router.get("/dde/{symbol}")
def api_dde(symbol: str, user=Depends(get_current_user)) -> dict:
    """DDE 大单动向(同花顺官方主力资金)。

    2026-08-20 修复: thsdk 1.7.18 没有 `dde()` 方法(`'THS' object has no attribute 'dde'`),
    改调 `get_main_flow_official(symbol)`,内部走 `get_dde_flow()` (官方 DDE API),
    返回 主力净流入(万元)/主力净量(占比)/总金额(万元) + 8 档明细。

    注意:thsdk DDE 仅支持最近交易日(不是当日实时,游客账户可用)。
    """
    try:
        result = _invoke("get_main_flow_official", symbol)
    except RuntimeError as e:
        raise HTTPException(503, f"thsdk DDE 数据源不可用: {e}") from e
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[thsdk-extended] get_main_flow_official 失败: {e}", exc_info=True)
        raise HTTPException(500, f"dde 失败: {str(e)[:120]}") from e
    if not isinstance(result, dict):
        return {"symbol": symbol, "rows": [], "count": 0, "fetched_at": _now()}
    # 把 dict 拍平到 rows(端点兼容原 rows[0] 结构)
    summary = result.get("summary") or {}
    detail = result.get("detail") or {}
    rows: list = []
    rows.append(_to_jsonable({k: v for k, v in result.items() if k not in ("summary", "detail")}))
    if isinstance(summary, dict) and summary:
        rows.append(_to_jsonable({**summary, "_row_type": "summary"}))
    if isinstance(detail, dict) and detail:
        rows.append(_to_jsonable({**detail, "_row_type": "detail"}))
    return {
        "symbol": symbol,
        "ths_code": result.get("ths_code"),
        "price": result.get("price"),
        "main_net_amount_wan": result.get("main_net_amount_wan"),  # 主力净流入(万元) - 同花顺官方口径
        "main_net_ratio": result.get("main_net_ratio"),          # 主力净量占比
        "total_amount_wan": result.get("total_amount_wan"),
        "rows": rows,
        "count": 1 if rows else 0,
        "fetched_at": _now(),
    }


@router.get("/hs300-constituents")
def api_hs300(user=Depends(get_current_user)) -> dict:
    """沪深 300 成分股列表。"""
    try:
        return _summarize("get_hs300_constituents")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"hs300 失败: {e}") from e


@router.get("/market-data-cn-extended/{symbol}")
def api_market_data_cn_extended(symbol: str, user=Depends(get_current_user)) -> dict:
    """A 股扩展行情(含主力净流入)。游客账户返 0 行,正式账户解锁。"""
    try:
        out = _summarize("get_market_data_cn_extended", symbol)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"market-data-cn-extended 失败: {e}") from e
    out["symbol"] = symbol
    return out


@router.get("/market-data-index/{symbol}")
def api_market_data_index(symbol: str, user=Depends(get_current_user)) -> dict:
    """指数行情(如 USHI000001 上证综指)。游客账户返 0 行。"""
    try:
        out = _summarize("get_market_data_index", symbol)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"market-data-index 失败: {e}") from e
    out["symbol"] = symbol
    return out


@router.get("/market-data-hk/{symbol}")
def api_market_data_hk(symbol: str, user=Depends(get_current_user)) -> dict:
    """港股行情(如 UHKG00700 腾讯)。游客账户返 0 行。"""
    try:
        out = _summarize("get_market_data_hk", symbol)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"market-data-hk 失败: {e}") from e
    out["symbol"] = symbol
    return out


@router.get("/market-data-us/{symbol}")
def api_market_data_us(symbol: str, user=Depends(get_current_user)) -> dict:
    """美股行情(如 UNQQAAPL 苹果)。"""
    try:
        out = _summarize("get_market_data_us", symbol)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"market-data-us 失败: {e}") from e
    out["symbol"] = symbol
    return out


@router.get("/market-data-bond/{symbol}")
def api_market_data_bond(symbol: str, user=Depends(get_current_user)) -> dict:
    """可转债行情。"""
    try:
        out = _summarize("get_market_data_bond", symbol)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"market-data-bond 失败: {e}") from e
    out["symbol"] = symbol
    return out


@router.get("/market-data-fund/{symbol}")
def api_market_data_fund(symbol: str, user=Depends(get_current_user)) -> dict:
    """基金 / ETF 行情。"""
    try:
        out = _summarize("get_market_data_fund", symbol)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"market-data-fund 失败: {e}") from e
    out["symbol"] = symbol
    return out


@router.get("/wencai-enhanced")
def api_wencai_enhanced(
    query: str = Query(..., description="问财自然语言选股条件(URL 编码)"),
    user=Depends(get_current_user),
) -> dict:
    """增强版问财 NLP(带 30s 进程内缓存)。"""
    q = (query or "").strip()
    if not q:
        raise HTTPException(400, "问财条件为空, 请填写选股条件")
    try:
        df = _invoke("get_wencai_enhanced", q)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"wencai-enhanced 失败: {e}") from e
    return {
        "query": q,
        "rows": _rows(df),
        "count": 0 if df is None else int(getattr(df, "shape", (0,))[0]),
        "fetched_at": _now(),
    }
