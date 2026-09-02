# -*- coding: utf-8 -*-
"""主力资金 1/3/5 日 + 0 轴多空判定(决策先锋官方口径)。

## 官方规格(8问8答 问题三 / 原PDF 页4)

    主力资金 = 明盘净额 + 暗盘净额
    支持 **1 日 / 3 日 / 5 日** 周期
    **上穿 0 轴**(由绿转红) = 资金看多; **下穿 0 轴**(由红转绿) = 资金利空
    红 = 流入 / 绿 = 流出

## ⚠️ 数据可得性(2026-09-02 实测, 本模块的硬约束)

| 组成 | 当日 | 历史(N 日累计) |
|------|------|----------------|
| 明盘 | ✅ `big_order_flow`(真值, 与官方扩展1 精确到元一致) | ❌ **无源**: TQ `get_more_info` 未连接(0 条); ZLJC 历史 `return_count=5` 返空 `{}` |
| 暗盘 | ✅ L2 逐笔拆单(主线, `dark_flow_l2`) | 🟡 **仅 OHLC 分摊对照项**(已知大振幅日 23 倍误差) |

因此本模块严守"缺即空、不编造":

- **net_1d**(当日主力净额) = 明盘 + 暗盘 → 两边齐全时**完整可得** ✅
- **net_nd**(N>1 日主力净额) → 明盘历史无源, **输出 None + 说明**。
  ⚠️ 绝不拿"暗盘 N 日"冒充主力净额 —— 那等于把 23 倍误差的对照项当真值,
  而且官方明确"主力净额 = 明盘 + 暗盘", 少一项就不是同一口径。
- **0 轴判定** → 基于**逐日暗盘序列**(口径内部一致), 结果里显式带
  `approximation=True` + `basis="暗盘(OHLC对照)"`, 前端展示必须带口径标记。
  明盘历史一旦有源(TQ 恢复 / 接入主力净额历史), 只需把 `ming_nd` 填上,
  `net_nd` 与 0 轴口径会自动升级为完整主力净额, **本模块无需改动结构**。

## 单位
金额 = 元, 成交量 = 股。
"""

from __future__ import annotations

import logging
from typing import Optional, Sequence

logger = logging.getLogger(__name__)

# 官方支持的周期
PERIODS = (1, 3, 5)
DEFAULT_DAYS = 5

# 明盘历史无源的说明(一处定义, 多处复用)
MING_HISTORY_NOTE = (
    "明盘历史无源: TQ get_more_info 未连接、ZLJC 历史返空; "
    "big_order_flow 仅提供当日。N>1 日明盘累计暂不可得(不编造)"
)

# 明盘历史开关: 需 TQ 网关可达 + ZLJC 单位校验通过后才能开(默认关, 见 ming_history_tq)
MING_HISTORY_ENABLED = __import__("os").environ.get("PANWATCH_MING_HISTORY", "0").strip() == "1"


def ming_history_tq(symbol: str, days: int) -> Optional[dict]:
    """明盘历史(通达信 TQ `ZLJC` 主力进出) → N 日累计净额(元)。

    **为什么默认关**(环境变量 `PANWATCH_MING_HISTORY=1` 才启用):

    1. **TQ 网关依赖**: ZLJC 走通达信客户端 JSON-RPC(127.0.0.1:17709)。
       生产小主机跑着通达信客户端 → **可达**; 本机/容器无客户端 → 返空 `{}`
       (2026-09-02 本地实测: 返 {} 且耗时 21.6s, 属超时空转)。
    2. **单位未校验**: ZLJC 的 JCL/JCM/JCS 是"净量"(手 or 股未在生产确认),
       换算成金额要 ×100 ×均价, 单位搞错就是**数量级错误** —— 宁可不给数。

    **启用前的校验方法**(一步即可):
        拿当日 ZLJC 换算值 与 `big_order_flow` 明盘真值(已与官方扩展1 精确到元对齐)
        对比量级, 一致才把 `PANWATCH_MING_HISTORY` 置 1。

    Returns:
        {"net": 元, "days": n, "source": "tq_zljc", "confidence": "proxy_approximation"}
        不可用 → None。
    """
    if not MING_HISTORY_ENABLED or days <= 1:
        return None
    try:
        from src.core.marketdata_client import md_formula_mul

        raw = md_formula_mul("ZLJC", [symbol], return_count=int(days))
        metrics = (raw or {}).get(symbol) or {}
        series = [metrics.get(k) or [] for k in ("JCL", "JCM", "JCS")]
        if not any(series):
            return None
        # 逐日: 三档净量求和 → × 100(手→股) → 金额用当日均价近似(口径: 代理)
        from src.core.decision_pioneer import fetch_bars

        bars = fetch_bars(symbol, "CN", days=max(int(days), 30)) or []
        if len(bars) < days:
            return None
        prices = [(b.get("high") or 0) + (b.get("low") or 0) for b in bars[-days:]]
        total = 0.0
        used = 0
        for i in range(days):
            day_qty = 0.0
            ok = True
            for s in series:
                v = s[i] if i < len(s) else None
                if isinstance(v, (int, float)):
                    day_qty += float(v)
                else:
                    ok = False
                    break
            if not ok or prices[i] <= 0:
                continue
            total += day_qty * 100.0 * (prices[i] / 2.0)   # 手→股 × 当日均价
            used += 1
        if used == 0:
            return None
        return {
            "net": round(total, 2),
            "days": used,
            "source": "tq_zljc",
            "confidence": "proxy_approximation",   # 代理口径, 非官方明盘算法
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("明盘历史(ZLJC)取数失败 %s: %s", symbol, e)
        return None


def _bar_values(b: dict) -> tuple:
    """取 bar 的 OHLCV, 兼容 dict / 对象。"""
    def g(k: str):
        return b.get(k) if isinstance(b, dict) else getattr(b, k, None)
    return g("open"), g("high"), g("low"), g("close"), g("volume"), str(g("date") or "")


def daily_dark_series(bars: Sequence[dict], limit: Optional[int] = None) -> list[dict]:
    """逐日暗盘净额序列(OHLC 分摊对照项)。

    Returns:
        [{"date": "YYYY-MM-DD", "net": 元 | None, "approximation": True}, ...]
        按日期升序; 单根字段非法 → 该日 net=None(跳过, 不按 0 补齐)。
    """
    from src.core.ohlc_dark import allocate_bar

    rows = list(bars or [])
    if limit:
        rows = rows[-limit:]
    out: list[dict] = []
    for b in rows:
        o, h, l, c, v, date = _bar_values(b)
        a = allocate_bar(o=o, h=h, l=l, c=c, volume=v, date=date)
        out.append({
            "date": date,
            "net": round(a.net, 2) if a is not None else None,
            "approximation": True,
        })
    return out


def _sum_or_none(values: Sequence[Optional[float]]) -> Optional[float]:
    """求和; 序列为空或含 None → None(不允许把缺失当 0 累加)。"""
    vals = [v for v in values if isinstance(v, (int, float))]
    if len(vals) != len(values):
        return None
    if not vals:
        return None
    return round(float(sum(vals)), 2)


def zero_axis_cross(
    series: Sequence[Optional[float]],
    basis: str = "暗盘(OHLC对照)",
    approximation: bool = True,
) -> dict:
    """0 轴上穿/下穿判定(官方多空信号)。

    官方: 由绿转红、**上穿 0 轴** = 资金看多; 由红转绿、**下穿 0 轴** = 资金利空。

    Args:
        series: 按时间升序的净额序列(元), 允许 None(缺即不判定)
        basis: 序列口径说明(随数据可得性变化, 结果里透传给前端)
        approximation: 该序列是否为近似对照项

    Returns:
        {
          "cross": "上穿0轴" / "下穿0轴" / "无穿越" / None,
          "signal": "看多" / "利空" / "流入" / "流出" / "无数据",
          "prev": float | None, "cur": float | None,
          "basis": str, "approximation": bool,
          "note": str | None,
        }
    """
    empty = {
        "cross": None, "signal": "无数据", "prev": None, "cur": None,
        "basis": basis, "approximation": approximation,
        "note": "无数据(序列不足 2 期或含缺失)",
    }
    if not series or len(series) < 2:
        return empty
    prev, cur = series[-2], series[-1]
    if not isinstance(prev, (int, float)) or not isinstance(cur, (int, float)):
        return empty

    if prev <= 0 < cur:
        return {"cross": "上穿0轴", "signal": "看多", "prev": prev, "cur": cur,
                "basis": basis, "approximation": approximation, "note": None}
    if prev >= 0 > cur:
        return {"cross": "下穿0轴", "signal": "利空", "prev": prev, "cur": cur,
                "basis": basis, "approximation": approximation, "note": None}
    return {"cross": "无穿越", "signal": "流入" if cur > 0 else ("流出" if cur < 0 else "平衡"),
            "prev": prev, "cur": cur,
            "basis": basis, "approximation": approximation, "note": None}


def fund_flow_nd(
    symbol: str,
    bars: Optional[Sequence[dict]] = None,
    days: int = DEFAULT_DAYS,
    ming_net_today: Optional[float] = None,
    dark_net_today: Optional[float] = None,
    fetch_today: bool = True,
) -> dict:
    """主力资金 N 日聚合 + 0 轴判定。

    Args:
        symbol: A 股代码
        bars: 日K(升序); None 时自行 fetch(建议 ≥60 根, GS 需 27)
        days: 周期, 官方支持 1 / 3 / 5
        ming_net_today: 当日明盘净额(元); 传 None 且 fetch_today → 自行取
        dark_net_today: 当日暗盘净额(元); 同上
        fetch_today: 是否允许本函数去拉当日明盘/暗盘(单测可关, 保持纯函数)

    Returns:
        {
          "symbol", "days",
          "net_1d": float | None,      # 当日主力净额 = 明盘 + 暗盘(完整口径)
          "ming_1d": float | None,
          "dark_1d": float | None,
          "net_nd": float | None,      # N 日主力净额; 明盘历史无源 → None + note
          "ming_nd": None,             # N 日明盘: 无源
          "dark_nd": float | None,     # N 日暗盘(OHLC 对照, approximation=True)
          "daily": [ {date, net} ... ],# 逐日暗盘序列(供 0 轴判定 / 前端画柱)
          "zero_axis": {...},          # 上穿/下穿 0 轴
          "note": str | None,
        }
    """
    if bars is None:
        try:
            from src.core.decision_pioneer import fetch_bars

            bars = fetch_bars(symbol, "CN", days=max(int(days), 60))
        except Exception as e:  # noqa: BLE001
            logger.warning("fund_flow_nd 取 K 线失败 %s: %s", symbol, e)
            bars = []

    bars = list(bars or [])
    days = int(days) if int(days) in PERIODS else DEFAULT_DAYS

    # ① 当日主力净额(完整口径: 明盘 + 暗盘)
    ming_1d, dark_1d = ming_net_today, dark_net_today
    if fetch_today and (ming_1d is None or dark_1d is None):
        try:
            from src.core.dark_pool_flow import compute_pool_flow

            pool = compute_pool_flow(symbol) or {}
            ming_1d = (pool.get("ming") or {}).get("net") if ming_1d is None else ming_1d
            dark_1d = (pool.get("dark") or {}).get("net") if dark_1d is None else dark_1d
        except Exception as e:  # noqa: BLE001
            logger.warning("fund_flow_nd 取当日资金失败 %s: %s", symbol, e)

    net_1d = None
    if isinstance(ming_1d, (int, float)) and isinstance(dark_1d, (int, float)):
        net_1d = round(float(ming_1d) + float(dark_1d), 2)

    # ② N 日暗盘(对照项): 逐日 OHLC 分摊
    daily = daily_dark_series(bars, limit=days)
    dark_nd = _sum_or_none([d["net"] for d in daily])

    # ③ N 日明盘: 走通达信 TQ ZLJC(需显式启用, 见 ming_history_tq 的单位校验说明)
    ming_hist = ming_history_tq(symbol, days) if days > 1 else None
    ming_nd = (ming_hist or {}).get("net")

    # ④ N 日主力净额: 明盘与暗盘**都齐**才合成(缺一项就不是官方口径, 不给数)
    net_nd = None
    if isinstance(ming_nd, (int, float)) and isinstance(dark_nd, (int, float)):
        net_nd = round(float(ming_nd) + float(dark_nd), 2)
    note: Optional[str] = None
    if days > 1:
        if net_nd is not None:
            note = "N 日明盘走 ZLJC 代理口径(approximation), 与当日明盘真值算法不同"
        else:
            note = MING_HISTORY_NOTE
        if daily and all(d["net"] is None for d in daily):
            note = "无数据: 逐日暗盘全部无法分摊(K线字段缺失)"
    elif net_1d is None:
        note = "当日主力净额不可得: 明盘/暗盘至少一项缺失(不编造)"

    # ⑤ 0 轴: 基于逐日暗盘序列(口径内部一致); 1 日时退化为"当期方向"
    if days == 1:
        basis_series = [dark_1d]
        zero = zero_axis_cross([0.0, dark_1d] if isinstance(dark_1d, (int, float)) else [None, None],
                               basis="当日暗盘(L2 逐笔拆单)", approximation=False)
    else:
        basis_series = [d["net"] for d in daily]
        zero = zero_axis_cross(basis_series, basis="暗盘(OHLC对照)", approximation=True)

    return {
        "symbol": symbol,
        "days": days,
        "net_1d": net_1d,
        "ming_1d": ming_1d,
        "dark_1d": dark_1d,
        "net_nd": net_nd,
        "ming_nd": ming_nd,
        "dark_nd": dark_nd,
        "daily": daily,
        "zero_axis": zero,
        "note": note,
    }
