"""主力意图双源对比(腾讯逐笔 vs thsdk L2) v0.5.0。

compare_main_flow(symbol): 同时拉取两路主力净额并比对一致性:
  - tencent: src.core.dark_flow.compute_dark_flow(腾讯逐笔口径)
    - 主力净额 main_net(元, ≥20万, 剔除竞价), 超大单 big_net(元, ≥100万)
  - thsdk : data_source.thsdk_l2.compute_main_flow(同花顺 L2 口径)
    - 主买主卖净额 net_wan(万元, 全量主动买-主动卖), 大单净额 big_net_wan(万元)

一致性 consistency(0-100):
  - 两源:  1 - |a-b| / max(|a|,|b|,1), ×100 截断。
  - delta_pct = 100 - consistency(发散幅度, 与一致性互补)。

容错: 任一源失败只影响该源(返 None + note), 至少一路成功就返回;
      两路全失败返回空。

v0.5.0(2026-08-24): 移除恒生聚源 DDE 第三源, 回归腾讯 vs thsdk 双源对比。
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

# 30s 进程内缓存: {symbol -> (ts, result)}。盘中每轮监控窗口内命中, 避免重复翻页/拉取。
_CACHE_TTL = 30.0
_cache: dict[str, tuple[float, dict]] = {}


def clear_cache() -> None:
    """清空进程内缓存(测试 / 运维手动刷新用)。"""
    _cache.clear()


def _to_thsdk_symbol(code: str) -> str | None:
    """6位A股代码 -> thsdk 代码(USZA 深A / USHA 沪A / USTM 北交所)。

    与 src.web.api.auction._normalize_symbol 口径一致, 本地独立实现避免循环依赖。
    """
    code = (code or "").strip()
    if not code.isdigit() or len(code) != 6:
        return None
    if code.startswith(("60", "68")):
        return f"USHA{code}"
    if code.startswith(("00", "30")):
        return f"USZA{code}"
    if code.startswith(("8", "4", "92")):
        return f"USTM{code}"
    return None


def _extract_tencent(dark: dict | None) -> dict | None:
    """从 compute_dark_flow 结果提取可比字段(单位为元)。数据不足返回 None。"""
    if not dark:
        return None
    data_status = dark.get("data_status")
    if data_status == "insufficient":
        return {
            "available": False,
            "data_status": data_status,
            "note": "腾讯逐笔数据不足(<30笔非竞价成交), 主力意图参考性低",
        }
    return {
        "available": True,
        "main_net": dark.get("main_net"),      # 元
        "big_net": dark.get("big_net"),        # 元(超大单 ≥100万)
        "mid_net": dark.get("mid_net"),        # 元(大单 20-100万)
        "retail_net": dark.get("small_net", dark.get("retail_net")),  # 元(散户)
        "signal": dark.get("signal"),
        "tick_count": dark.get("tick_count"),
        "data_status": data_status,
    }


def _extract_thsdk(flow: dict | None) -> dict | None:
    """从 compute_main_flow 结果提取可比字段(净额统一换算为元)。"""
    if not flow or flow.get("error") == "no_data" or flow.get("net_wan") is None:
        return None
    net_wan = flow.get("net_wan", 0.0) or 0.0
    big_net_wan = flow.get("big_net_wan", 0.0) or 0.0
    return {
        "available": True,
        "main_net": round(net_wan * 10000.0),                    # 元
        "big_net": round(big_net_wan * 10000.0),                 # 元(≥100万)
        "main_net_wan": round(net_wan, 2),                       # 万元(原口径)
        "big_net_wan": round(big_net_wan, 2),                    # 万元(原口径)
        "main_buy_wan": flow.get("main_buy_wan"),
        "main_sell_wan": flow.get("main_sell_wan"),
        "big_buy_wan": flow.get("big_buy_wan"),
        "big_sell_wan": flow.get("big_sell_wan"),
        "total_ticks": flow.get("total_ticks"),
        "valid_ticks": flow.get("valid_ticks"),
    }


def _consistency(tencent_main: float, thsdk_main: float) -> tuple[float, float]:
    """计算一致性(0-100)与发散幅度 delta_pct(%), 单位需一致(这里均为元)。

    两者都接近 0 时视为一致(均无主力动作), consistency=100, delta_pct=0。
    """
    denom = max(abs(tencent_main), abs(thsdk_main), 1.0)
    diff = abs(tencent_main - thsdk_main)
    delta_pct = diff / denom * 100.0
    consistency = max(0.0, min(100.0, 100.0 - delta_pct))
    return round(consistency, 1), round(delta_pct, 1)


def _tencent_symbol(code: str):
    """构建腾讯口径所需的 marketdata Symbol(懒加载, 避免导入环)。"""
    from marketdata import Symbol as MDSymbol

    return MDSymbol.parse(code, "CN")


def compare_main_flow(symbol: str) -> dict:
    """主力意图双源对比。symbol 为 6 位 A 股代码(如 002361)。

    返回:
      {
        "symbol": code,
        "tencent":   dict|None,   # 腾讯逐笔口径(元)
        "thsdk":     dict|None,   # 同花顺 L2 口径(元), 失败为 None
        "consistency": float|None,  # 0-100(双源)
        "delta_pct":   float|None,
        "note": str,
        "notes": [str],             # 提示哪几路可用/失败
      }
    容错: 任一源失败只降级该源, 至少一路成功就返回。
    """
    code = (symbol or "").strip()
    now = time.time()
    cached = _cache.get(code)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]

    notes: list[str] = []

    # 腾讯逐笔(暗盘主链路)
    tencent = None
    try:
        from src.core.dark_flow import compute_dark_flow

        dark = compute_dark_flow(_tencent_symbol(code))
        tencent = _extract_tencent(dark)
    except Exception as e:  # noqa: BLE001 - 数据源异常统一降级, 不崩
        logger.warning("[main_flow] 腾讯逐笔对比失败 %s: %r", code, e)
        tencent = None
    if tencent and tencent.get("available"):
        notes.append("tencent: 可用")
    else:
        notes.append("tencent: 数据暂不可用")

    # thsdk L2(同花顺口径)
    thsdk = None
    tsym = _to_thsdk_symbol(code)
    if tsym:
        try:
            from data_source.thsdk_l2 import compute_main_flow

            flow = compute_main_flow(tsym)
            thsdk = _extract_thsdk(flow)
        except Exception as e:  # noqa: BLE001
            logger.warning("[main_flow] thsdk 对比失败 %s(%s): %r", code, tsym, e)
            thsdk = None
    notes.append("thsdk: 可用" if (thsdk and thsdk.get("available")) else "thsdk: 数据暂不可用")

    # 一致性比对: 收集可用 main_net(元), 按源顺序 [tencent, thsdk]
    available = [
        v for v in (
            (tencent.get("main_net") if tencent and tencent.get("available") else None),
            (thsdk.get("main_net") if thsdk and thsdk.get("available") else None),
        ) if v is not None
    ]

    consistency = None
    delta_pct = None
    if len(available) == 2:
        consistency, delta_pct = _consistency(float(available[0]), float(available[1]))
    # len(available) < 2 -> 无法比对, consistency=None

    note = "双源一致性比对(腾讯逐笔 vs thsdk L2)"
    if len(available) < 2:
        failed = [n.split(":")[0] for n in notes if "数据暂不可用" in n]
        if failed:
            note = f"{note}; {'; '.join(f'{f} 数据暂不可用' for f in failed)}"

    result = {
        "symbol": code,
        "tencent": tencent,
        "thsdk": thsdk,
        "consistency": consistency,
        "delta_pct": delta_pct,
        "note": note,
        "notes": notes,
    }
    _cache[code] = (time.time(), result)
    return result
