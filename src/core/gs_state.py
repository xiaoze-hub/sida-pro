# -*- coding: utf-8 -*-
"""GS 状态单一事实源(只消费 `gs_strategy` 输出; `gs_strategy` 本体禁改)。

## 为什么需要这个模块(2026-10-13 用户实报)
同一只股票同一时刻, 两套「GS」给出了相反的结论(600111 收盘后):

- **K 线图层**: GS 买卖点/区间色带走 `gs_strategy.compute_gs_signals`
  (逐根交叉, 带 confirmed/pending); 区间色带**只由 confirmed 锚点建立**
  (口径见前端 `packages/biz-ui/src/lib/gs-zones.ts::gsZonePoints`)→ 显示 **G**;
- **数智决策面板**: 「GS 区间」走 `decision_pioneer` 自带的 a0/bb0 比较
  (`compute_gs_signal.state`), 且**吃末根盘中价** —— 末根出现**疑似**反向交叉时
  面板立刻翻面, 而图表仍停在上一根**已确认**信号处 → 显示 **S区**。

末根就是「今日 live 合成 bar(partial)」时, 那个交叉按官方语义是 **pending**
(次日不消失, 收盘才定死), 但面板把它当成了定论 ⇒ 「两张脸」。

## 本模块的口径(与图表**逐字一致**)
- **zone/signal**: 取 `compute_gs_signals` 里 `confirmed=True` 的**最后一条**
  (图表实心 ▲/▼ 的最后一枚)。首个已确认信号之前 → 无数据(不猜区间)。
- **pending**: 末根 `confirmed=False` 的疑似交叉(图表空心 ○G/○S),
  只作「待确认」标注, **不接管区间**。
- **a0(快线)/bb0(慢线)**: 取自 `gs_strategy.eval_gs`(同一份 bars / 同一 GS 内核),
  与区间同源, **仅供展示, 不参与区间判定**。

## 数据截止口径(不冒充定型)
- 末根 bar 无论是「今日盘中合成 bar(partial)」还是收盘后的最后一根, 其上的交叉
  在 `compute_gs_signals` 里**一律** `confirmed=False`(`i < n-1` 才确认)——
  即「待确认」要等**下一根 bar 到来**才解除。面板与图表同此口径, 不另立标准。

禁改 `gs_strategy`; 本模块只消费其输出(`compute_gs_signals` / `eval_gs`)。
"""

from __future__ import annotations

from typing import Optional, Sequence

# 区域语义(与 gs_strategy 同名同值)
ZONE_G = "G区"
ZONE_S = "S区"

#: 待确认文案 —— 与 K 线图图例**逐字一致**
#: (packages/biz-ui/src/components/KlineChart.tsx 的 `gs-legend`: "空心 ○ = 待确认(盘中价, 收盘才定死)")
PENDING_TEXT = "○{side} 待确认(盘中价, 收盘才定死)"


def _empty(note: str) -> dict:
    return {
        "available": False,
        "zone": None,
        "signal": None,
        "signal_date": None,
        "signal_price": None,
        "signal_confirmed": False,
        "pending": False,
        "pending_side": None,
        "pending_date": None,
        "pending_price": None,
        "a0": None,
        "bb0": None,
        "label": "无数据",
        "pending_label": None,
        "note": note,
        "as_of": None,
    }


def resolve_gs_state(bars: Optional[Sequence[dict]]) -> dict:
    """由 `gs_strategy` 输出派生**唯一** GS 状态(图表 / 面板 / 推送 / 共振同源)。

    Args:
        bars: 按日期升序的日K(与图表/面板用的是同一份取数 `decision_pioneer.fetch_bars`)。

    Returns:
        {
          "available": bool,        # 是否有**已确认**区间(与图表色带同门控)
          "zone": "G区"/"S区"/None,  # 最近一次已确认信号所定义的区间
          "signal": "G"/"S"/None,    # 最近一次已确认信号方向(图表实心 ▲/▼)
          "signal_date": "YYYY-MM-DD"|None,
          "signal_price": float|None, # 信号时点收盘价(账本留痕用)
          "signal_confirmed": bool,  # signal 非 None 时恒 True(与图表实心同口径)
          "pending": bool,           # 末根是否存在疑似交叉(图表空心 ○)
          "pending_side": "G"/"S"|None,
          "pending_date": "YYYY-MM-DD"|None,
          "pending_price": float|None,
          "a0": float|None,          # 快线(展示项, 与区间同源)
          "bb0": float|None,         # 慢线(展示项, 与区间同源)
          "label": str,              # 面板主文案: G区/S区/无数据(尚无已确认锚点时 = 疑似方向)
          "pending_label": str|None, # "○S 待确认(盘中价, 收盘才定死)"(与图例逐字一致)
          "note": str|None,          # 状态说明(待确认/无数据)
          "as_of": "YYYY-MM-DD"|None,# 末根 bar 日期(数据截止口径)
        }
    """
    if not bars:
        return _empty("无数据")

    # 懒导入: 杜绝 `decision_pioneer -> gs_state -> gs_strategy -> decision_pioneer` 环形导入
    from src.core.gs_strategy import compute_gs_signals, eval_gs

    out = _empty("无数据")
    out["as_of"] = _norm_date(
        getattr(bars[-1], "date", None)
        if not isinstance(bars[-1], dict)
        else bars[-1].get("date")
    )

    # 展示项(快线/慢线)唯一出处: gs_strategy.eval_gs(与区间同一份 bars / 同一 GS 内核)
    try:
        ev = eval_gs(bars) or {}
        out["a0"] = ev.get("a0")
        out["bb0"] = ev.get("bb0")
    except Exception:  # noqa: BLE001 展示项取不到不影响区间判定(下面照常)
        pass

    seq = compute_gs_signals(bars) or []
    confirmed = [s for s in seq if s.get("confirmed") and s.get("side") in ("G", "S")]
    pending = [s for s in seq if not s.get("confirmed") and s.get("side") in ("G", "S")]

    if confirmed:
        last = confirmed[-1]
        side = last["side"]
        out["signal"] = side
        out["signal_date"] = last.get("date")
        out["signal_price"] = last.get("price")   # 信号时点收盘价(账本留痕用, 不硬填)
        out["signal_confirmed"] = True
        out["zone"] = ZONE_G if side == "G" else ZONE_S
        out["available"] = True

    if pending:
        p = pending[-1]
        out["pending"] = True
        out["pending_side"] = p["side"]
        out["pending_date"] = p.get("date")
        out["pending_price"] = p.get("price")
        out["pending_label"] = PENDING_TEXT.format(side=p["side"])

    # 面板主文案 + 说明(不冒充定论)
    if out["zone"]:
        out["label"] = out["zone"]
        if out["pending"]:
            out["note"] = (
                f"{out['pending_date'] or '末根'} 疑似转{out['pending_side']}"
                f"(待确认), 区间以最近已确认信号({out['signal_date']})为准"
            )
        else:
            out["note"] = None
    elif out["pending"]:
        # 尚无已确认锚点: 主文案给疑似方向, 但 available=False 且 pending=True,
        # 消费方(面板/过滤)必须当「待确认」处理, 不得当定论。
        out["label"] = ZONE_G if out["pending_side"] == "G" else ZONE_S
        out["note"] = "尚无已确认信号, 末根疑似交叉未定型"
    else:
        out["note"] = "无数据(日K不足或无已确认 GS 信号)"
    return out


def _norm_date(d) -> Optional[str]:
    """日期归一 'YYYY-MM-DD'(与 gs_strategy._norm_date 同口径的本地实现, 不猜)。"""
    s = str(d or "").strip()
    if not s:
        return None
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s[:10]
