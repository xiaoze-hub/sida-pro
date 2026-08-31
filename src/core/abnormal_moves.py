"""Exchange abnormal-move rule monitor (Task C, 2026-08-24).

Sources (借鉴 tick-stock-panel 的 abnormal_moves 设计, 对齐沪深北三大交易所
《股票交易规则》《上市公司监管指引》):

  普通异动(分板块):
      * 主板(60/00 开头, 不含 688): 3 个交易日内累计偏离值达 +-20%
      * 创业板(30 开头): 3 个交易日内累计偏离值达 +-30%
      * 科创板(688 开头): 3 个交易日内累计偏离值达 +-30%
      * 北交所(8/4/92 开头): 3 个交易日内累计偏离值达 +-40%

  严重异动(全板块通用, 沪深北交易所关于"严重异常波动"条款,
  阈值就高从严):
      * 10 个交易日内累计 +100% / -50%(主板/创业板/科创板/北交所)
      * 30 个交易日内累计 +200% / -70%(主板/创业板/科创板/北交所;
        负向更严, 用于提前告警的更保守阈值)
      实际规则原文: 10 日偏离值达 +-100% 或 30 日偏离值达 +200% / -50%;
      这里把更严的 -50%/-70% 提前到监控阈值, 拉齐"快速回调"极端场景。

偏离值定义:
  N 日偏离 = 个股 N 日累计涨跌幅 - 对应基准指数同期涨跌幅
  沪股  -> 上证指数 (000001)
  深股  -> 深证成指 (399001)
  创业板 -> 创业板指 (399006)
  科创板 -> 上证指数 (尚未独立指数)
  北交所 -> 北证50  (899050)

接近度(proximity)分级:
  proximity = |当前偏离| / 该方向阈值
  proximity >= 1.0      -> "已触发"
  0.7  <= proximity < 1.0 -> "边缘"
  0.5  <= proximity < 0.7 -> "观察"
  proximity < 0.5       -> "正常"

字段口径:
  - 所有比例(累计涨跌幅 / 偏离 / 阈值)单位是 %(已含百分号, 7 表示 7%)
  - status 取值: triggered / edge / watch / normal / unknown
  - windows: 普通 3 日(板异)+ 严重 10 日 + 严重 30 日, 共 3 条

入口:
  analyze_abnormal_moves(symbol) -> dict
  analyze_for_symbols(symbols)  -> list[dict]    # 批量, proximity 倒序, 异常隔离
  scan_all(min_proximity)       -> list[dict]    # 兼容入口
"""
from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from typing import Callable, Iterable

from src.collectors.kline_collector import KlineCollector, get_index_klines
from src.models.market import MarketCode

logger = logging.getLogger(__name__)

# ---- board classification (基于 6 位代码前缀) ------------------------

_RE_SH_MAIN = re.compile(r"^(60|90)\d{4}$")
_RE_SZ_MAIN = re.compile(r"^(00|20)\d{4}$")
_RE_CHINEXT = re.compile(r"^30\d{4}$")
_RE_STAR = re.compile(r"^688\d{3}$")
_RE_BSE = re.compile(r"^[48]\d{5}$")
_RE_BSE_92 = re.compile(r"^92\d{4}$")


def board_of(symbol: str) -> str:
    """6 位代码 -> main / cyb / star / bse / unknown."""
    code = (symbol or "").strip()
    if not (code.isdigit() and len(code) == 6):
        return "unknown"
    if _RE_STAR.match(code):
        return "star"
    if _RE_CHINEXT.match(code):
        return "cyb"
    if _RE_SH_MAIN.match(code):
        return "main"
    if _RE_SZ_MAIN.match(code):
        return "main"
    if _RE_BSE.match(code) or _RE_BSE_92.match(code):
        return "bse"
    return "unknown"


BOARD_INDEX: dict[str, dict] = {
    "main": {
        "sh": {"code": "000001", "name": "上证指数", "tencent": "sh000001"},
        "sz": {"code": "399001", "name": "深证成指", "tencent": "sz399001"},
    },
    "cyb": {
        "default": {"code": "399006", "name": "创业板指", "tencent": "sz399006"},
    },
    "star": {
        "default": {"code": "000001", "name": "上证指数", "tencent": "sh000001"},
    },
    "bse": {
        "default": {"code": "899050", "name": "北证50", "tencent": "bj899050"},
    },
    "unknown": {
        "default": {"code": "000001", "name": "上证指数", "tencent": "sh000001"},
    },
}

BOARD_DISPLAY_NAME: dict[str, str] = {
    "main": "主板",
    "cyb": "创业板",
    "star": "科创板",
    "bse": "北交所",
    "unknown": "未识别",
}


def _exchange_prefix(symbol: str) -> str:
    """6 位代码 -> SH / SZ / BJ."""
    code = (symbol or "").strip()
    if not code:
        return "SH"
    if code.startswith(("60", "68", "90")):
        return "SH"
    if code.startswith(("00", "20", "30")):
        return "SZ"
    if code.startswith(("4", "8", "92")):
        return "BJ"
    return "SH"


def benchmark_for(symbol: str) -> dict:
    """该股对应的基准指数信息 {code, name, tencent, board, market}."""
    board = board_of(symbol)
    exchange = _exchange_prefix(symbol)
    bucket = BOARD_INDEX.get(board) or {}
    if board == "main" and exchange.lower() in bucket:
        out = dict(bucket[exchange.lower()])
    elif "default" in bucket:
        out = dict(bucket["default"])
    elif bucket:
        out = dict(next(iter(bucket.values())))
    else:
        out = dict(BOARD_INDEX["unknown"]["default"])
    out["board"] = board
    out["market"] = MarketCode.CN.value
    return out


# ---- 异动规则表 ------------------------------------------------------

@dataclass(frozen=True)
class AbnormalRule:
    """单条异动规则."""
    key: str
    window: int           # 交易日窗口(3/10/30)
    board: str            # main / cyb / star / bse / all
    up_threshold: float   # 正向阈值 %
    down_threshold: float  # 负向阈值 %(正值)
    severity: str         # normal / severe


_RULES_3D: dict[str, AbnormalRule] = {
    "main": AbnormalRule("3d_main", 3, "main", 20.0, 20.0, "normal"),
    "cyb": AbnormalRule("3d_cyb", 3, "cyb", 30.0, 30.0, "normal"),
    "star": AbnormalRule("3d_star", 3, "star", 30.0, 30.0, "normal"),
    "bse": AbnormalRule("3d_bse", 3, "bse", 40.0, 40.0, "normal"),
}

_RULES_SEVERE = [
    AbnormalRule("10d_all", 10, "all", 100.0, 50.0, "severe"),
    AbnormalRule("30d_all", 30, "all", 200.0, 70.0, "severe"),
]


def rules_for(board: str) -> list[AbnormalRule]:
    """某板的全部规则: 1 条普通 3 日 + 2 条严重 10/30 日."""
    out: list[AbnormalRule] = []
    base = _RULES_3D.get(board)
    if base is not None:
        out.append(base)
    out.extend(_RULES_SEVERE)
    return out


# ---- 计算工具 --------------------------------------------------------

def _klines_to_closes(klines) -> list[float]:
    """只取 close, 过滤 None."""
    closes: list[float] = []
    for k in klines or []:
        try:
            closes.append(float(k.close))
        except (TypeError, ValueError):
            continue
    return closes


def cumulative_change(closes, window):
    """近 N 个交易日累计涨跌幅(%).

    需要 window+1 个交易日. 数据不足时用已有区间作近似, 同时返回 used_n.
    返回 (pct, used_n); pct is None 表数据无效(空或 anchor 非正).
    """
    if window is None or window <= 0:
        return 0.0, 0
    if not closes or len(closes) < 2:
        return None, len(closes) if closes else 0
    need = window + 1
    if len(closes) < need:
        anchor = closes[0]
        end = closes[-1]
        used = max(0, len(closes) - 1)
    else:
        anchor = closes[-need]
        end = closes[-1]
        used = window
    try:
        anchor_f = float(anchor)
        end_f = float(end)
    except (TypeError, ValueError):
        return None, len(closes)
    if anchor_f <= 0:
        return None, len(closes)
    pct = (end_f / anchor_f - 1.0) * 100.0
    return round(pct, 2), used


def compute_deviation(stock_closes, index_closes, window) -> dict:
    """N 日偏离 = 个股累计 - 指数同期累计."""
    s_pct, s_used = cumulative_change(stock_closes, window)
    i_pct, i_used = cumulative_change(index_closes, window)
    available = s_pct is not None and i_pct is not None
    if not available:
        return {
            "stock_pct": s_pct,
            "index_pct": i_pct,
            "deviation_pct": None,
            "window": window,
            "available": False,
            "stock_used_n": s_used,
            "index_used_n": i_used,
        }
    return {
        "stock_pct": s_pct,
        "index_pct": i_pct,
        "deviation_pct": round(s_pct - i_pct, 2),
        "window": window,
        "available": True,
        "stock_used_n": s_used,
        "index_used_n": i_used,
    }


# ---- 接近度分级 ------------------------------------------------------

_THRESHOLD_TRIGGERED = 1.0
_THRESHOLD_EDGE = 0.7
_THRESHOLD_WATCH = 0.5


def status_of(proximity):
    """proximity (>=0) -> triggered / edge / watch / normal / unknown."""
    if proximity is None:
        return "unknown"
    if proximity >= _THRESHOLD_TRIGGERED:
        return "triggered"
    if proximity >= _THRESHOLD_EDGE:
        return "edge"
    if proximity >= _THRESHOLD_WATCH:
        return "watch"
    return "normal"


def proximity_of(deviation_pct, rule):
    """proximity = |deviation| / 方向阈值."""
    if deviation_pct is None:
        return None
    if deviation_pct > 0:
        denom = rule.up_threshold
    elif deviation_pct < 0:
        denom = rule.down_threshold
    else:
        return 0.0
    if denom is None or denom <= 0:
        return None
    prox = abs(deviation_pct) / denom
    return round(prox, 3)


def worst_window(rule_results):
    """rule_results 列表里取 proximity 最大那条. 数据不足跳过."""
    valid = [r for r in rule_results if r.get("proximity") is not None]
    if not valid:
        return None
    valid.sort(key=lambda r: r["proximity"], reverse=True)
    return valid[0]


# ---- 单只分析 --------------------------------------------------------

def analyze_abnormal_moves(symbol: str) -> dict:
    """对一只 A 股跑全套窗口, 返回结构化 dict."""
    board = board_of(symbol)
    benchmark = benchmark_for(symbol)

    kc = KlineCollector(MarketCode.CN)
    stock_klines = []
    index_klines = []
    try:
        stock_klines = kc.get_klines(symbol, days=35) or []
    except Exception as e:
        logger.debug("[abnormal_moves] 个股 K 线拉取失败 %s: %r", symbol, e)
    try:
        index_klines = get_index_klines(benchmark["code"], MarketCode.CN, days=35) or []
    except Exception as e:
        logger.debug("[abnormal_moves] 指数 K 线拉取失败 %s: %r", benchmark.get("code"), e)

    stock_closes = _klines_to_closes(stock_klines)
    index_closes = _klines_to_closes(index_klines)

    rules = rules_for(board)
    windows = []
    for rule in rules:
        dev = compute_deviation(stock_closes, index_closes, rule.window)
        prox = proximity_of(dev["deviation_pct"], rule)
        st = status_of(prox)
        dval = dev["deviation_pct"]
        if dval is None:
            threshold_used = rule.up_threshold
            direction = "na"
        elif dval > 0:
            threshold_used = rule.up_threshold
            direction = "up"
        elif dval < 0:
            threshold_used = rule.down_threshold
            direction = "down"
        else:
            threshold_used = rule.up_threshold
            direction = "flat"
        windows.append({
            "rule_key": rule.key,
            "window": rule.window,
            "board": rule.board,
            "severity": rule.severity,
            "up_threshold": rule.up_threshold,
            "down_threshold": rule.down_threshold,
            "threshold_used": threshold_used,
            "direction": direction,
            "stock_pct": dev["stock_pct"],
            "index_pct": dev["index_pct"],
            "deviation_pct": dev["deviation_pct"],
            "available": dev["available"],
            "stock_used_n": dev["stock_used_n"],
            "index_used_n": dev["index_used_n"],
            "proximity": prox,
            "status": st,
        })

    worst = worst_window(windows)
    main_proximity = worst["proximity"] if worst else None
    main_status = status_of(main_proximity)
    available_any = any(w["available"] for w in windows)

    return {
        "symbol": symbol,
        "board": board,
        "board_name": BOARD_DISPLAY_NAME.get(board, "未识别"),
        "benchmark": {
            "code": benchmark.get("code"),
            "name": benchmark.get("name"),
            "tencent": benchmark.get("tencent"),
        },
        "available": available_any,
        "worst": worst,
        "windows": windows,
        "status": main_status if available_any else "unknown",
        "proximity": main_proximity,
    }


def analyze_for_symbols(
    symbols: Iterable[str],
    *,
    min_proximity: float = 0.5,
    analyzer: Callable[[str], dict] | None = None,
) -> list[dict]:
    """批量 symbol 跑异常监控, proximity 倒序, 过滤掉 <min_proximity / 无数据."""
    analyze = analyzer or analyze_abnormal_moves
    results = []
    for sym in symbols:
        try:
            r = analyze(sym)
        except Exception as e:
            logger.debug("[abnormal_moves] 单只分析失败 %s: %r", sym, e)
            continue
        if not r or not r.get("available"):
            continue
        prox = r.get("proximity")
        if prox is None or prox < min_proximity:
            continue
        results.append(r)
    results.sort(
        key=lambda r: (
            -(r.get("proximity") or 0.0),
            r.get("symbol") or "",
        )
    )
    return results


def scan_all(min_proximity: float = 0.5) -> list[dict]:
    """兼容入口. 真实 symbol 集合在 api 层组装."""
    return analyze_for_symbols([], min_proximity=min_proximity)


__all__ = [
    "AbnormalRule",
    "BOARD_DISPLAY_NAME",
    "BOARD_INDEX",
    "analyze_abnormal_moves",
    "analyze_for_symbols",
    "benchmark_for",
    "board_of",
    "compute_deviation",
    "cumulative_change",
    "proximity_of",
    "rules_for",
    "scan_all",
    "status_of",
    "worst_window",
]


def _asdict_safe(obj):
    try:
        return asdict(obj)
    except Exception:
        return str(obj)


if __name__ == "__main__":
    import json
    for code in ("002361", "300750", "688981", "830799"):
        try:
            print("==", code)
            print(json.dumps(analyze_abnormal_moves(code), ensure_ascii=False, indent=2, default=_asdict_safe))
        except Exception as e:
            print(code, "分析失败:", e)
