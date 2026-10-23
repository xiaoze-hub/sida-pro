"""通达信 TQ `get_exday_data` 四档资金结构解析与因子（2026-10-23 实测标定）。

## 这个模块解决什么

`get_exday_data` 早在 vendor 里就有封装，但产品只读了 `BCancel`/`BOrder` 两个标量，
把同一行里最有信息量的 **4×4 矩阵**整块丢掉了。

## 矩阵语义（三个不同价位标的交叉验证过，不是照文档抄）

```
Amo[行][列]  行 = [特大, 大, 中, 小]   列 = [买入额, 卖出额, 主动买额, 主动卖额]  元
Vol[行][列]  同形，单位 = 手
```

三条恒等式一起成立才算标定成功（2026-10-23 真网关实测）：

| 等式 | 601318(平安) | 600519(茅台) | 002361(神剑) |
|---|---|---|---|
| Σ行[0] = Σ行[1] = 当日成交额 | 51.65亿 ✓ | 76.77亿 = 日线 Amount ✓ | 5.48亿 ✓ |
| Σ[2] + Σ[3] = 当日成交额 | 28.55+23.11 ✓ | 41.47+35.30 ✓ | 2.76+2.72 ✓ |
| Σ([2]−[3]) = `get_more_info.Zjl`（外盘−内盘） | 54,400 = 54,404 ✓ | 61,771 vs 61,740 ✓ | 452 vs 452.68 ✓ |
| row3(小单) 占成交额 | 21.3% | **0.0%**（1 手=13.35 万，结构上不可能有小单） | 56.9% |

行序由「茅台小单必为 0」唯一钉死，列序由 `Zjl` 在三个数量级上精确对齐 —— 都不是猜的。

## 单位与口径红线

- 金额=元，量=手（**不是股**）；本模块输出的 `*_lots` 一律手，`*_amt` 一律元。
- `caliber = "tdx_exday4"`：**按单笔成交金额分 4 档**的客户端口径。
  与 `eastmoney4`（东财 push2 四档）、`tick`（腾讯逐笔 ≥30 万）互不可换算、
- 档位阈值官方未公开，本文按「小 <5万 / 中 5~20万 / 大 20~100万 / 特大 >100万」推断，
  依据是茅台小单结构上为 0 + 三票分布一致性。**用到档位边界的结论必须标注"推断阈值"**。
- 只有**当日**：`get_exday_data` 的 count / start_time / date 参数实测全部只返当天 1 行，
  历史只能靠 `src/collectors/exday_daily.py` 每日落库累积。
"""
from __future__ import annotations

from typing import Any

#: 档位顺序（与 TQ 返回的行序一致，已实测钉死）
TIERS: tuple[str, ...] = ("xl", "lg", "md", "sm")

TIER_LABELS: dict[str, str] = {
    "xl": "特大单",
    "lg": "大单",
    "md": "中单",
    "sm": "小单",
}

#: 推断阈值（官方未公开；写进文案时必须带「推断」）
TIER_THRESHOLDS_HINT = "特大>100万 / 大20~100万 / 中5~20万 / 小<5万（推断阈值）"

CALIBER = "tdx_exday4"
SOURCE = "tdx_tq_exday"

#: 主力的档位（特大 + 大）。中单是否算主力：不做，保持可解释边界。
MAIN_TIERS: tuple[str, ...] = ("xl", "lg")

#: 委托粒度判定的阈值（拆单隐匿）。见 `_conclusions` 的说明。
HIDING_RATIO_STRONG = 3.0     # 委托粒度 / 成交粒度 ≥3 → 明显拆单结构
HIDING_RATIO_WEAK = 1.5
FILL_RATIO_LOW = 0.5          # 委托笔数 < 成交笔数的一半 → 一笔委托被拆成多笔成交
CANCEL_RATE_HIGH = 0.35       # 撤单率 ≥35% → 假挂单偏多（与 cancel_order_intent 同阈值）


def _num(v: Any) -> float | None:
    """数值化；非数/NaN/Inf 一律 None（不拿 0 冒充缺失）。"""
    try:
        f = float(str(v).strip())
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def parse_matrix(raw: Any) -> list[list[float]] | None:
    """把 `Amo`/`Vol` 的原始 4×4 转成 float 矩阵；形状不对返回 None（不猜）。"""
    if not isinstance(raw, list) or len(raw) != len(TIERS):
        return None
    out: list[list[float]] = []
    for row in raw:
        if not isinstance(row, list) or len(row) != 4:
            return None
        vals = [_num(x) for x in row]
        if any(v is None for v in vals):
            return None
        out.append([float(v) for v in vals])  # type: ignore[arg-type]
    return out


def compute(
    amo: list[list[float]] | None,
    vol: list[list[float]] | None = None,
    *,
    b_order: float | None = None,
    s_order: float | None = None,
    total_b_order: float | None = None,
    total_s_order: float | None = None,
    b_cancel: float | None = None,
    s_cancel: float | None = None,
    cjbs: float | None = None,
    kline_amount: float | None = None,
) -> dict[str, Any]:
    """由两张矩阵 + 委托/撤单标量算出结构指标与因子（纯函数）。

    `kline_amount` 传入时做**自校验**：Σ买入额 与当日成交额偏差 >5% → `suspect`
    （客户端口径漂移或取数串行导致的行错位，宁可疑不可当真）。
    """
    empty = {"available": False, "caliber": CALIBER, "reason": "四档矩阵缺失或形状不符"}
    if not amo:
        return empty

    amount = sum(row[0] for row in amo)
    sell_total = sum(row[1] for row in amo)
    active_buy = sum(row[2] for row in amo)
    active_sell = sum(row[3] for row in amo)
    if amount <= 0:
        return {"available": False, "caliber": CALIBER, "reason": "成交额合计为 0（无当日数据）"}

    quality, reason = "ok", ""
    # 恒等式自校验 1：Σ买入额 必须 = Σ卖出额（每笔成交同时计买方与卖方）。
    # 不成立说明取数错位/口径漂移 —— 这类错**不会报错**，只能靠恒等式抓。
    if sell_total > 0:
        skew = abs(amount - sell_total) / amount
        if skew > 0.01:
            quality = "suspect"
            reason = f"买卖合计不等（买 {amount / 1e8:.2f}亿 / 卖 {sell_total / 1e8:.2f}亿，差 {skew * 100:.1f}%）"
    # 恒等式自校验 2（有日线成交额时）：四档买入额合计 ≈ 当日成交额
    if kline_amount and kline_amount > 0:
        dev = abs(amount - float(kline_amount)) / float(kline_amount)
        if dev > 0.05:
            quality = "suspect"
            reason = (reason + "；" if reason else "") + (
                f"四档买入额合计 {amount / 1e8:.2f}亿 与当日成交额 "
                f"{float(kline_amount) / 1e8:.2f}亿 偏差 {dev * 100:.1f}% (>5%)")

    tiers: dict[str, dict[str, Any]] = {}
    for idx, key in enumerate(TIERS):
        row = amo[idx]
        lots_row = vol[idx] if vol and len(vol) > idx else [0.0, 0.0, 0.0, 0.0]
        buy, sell, act_buy, act_sell = row
        tiers[key] = {
            "label": TIER_LABELS[key],
            "buy_amt": round(buy, 2),
            "sell_amt": round(sell, 2),
            "active_buy_amt": round(act_buy, 2),
            "active_sell_amt": round(act_sell, 2),
            "active_net_amt": round(act_buy - act_sell, 2),
            "buy_lots": round(lots_row[0], 2),
            "sell_lots": round(lots_row[1], 2),
            "active_buy_lots": round(lots_row[2], 2),
            "active_sell_lots": round(lots_row[3], 2),
            "buy_share": round(buy / amount * 100, 2),
        }

    main_active_net = sum(tiers[k]["active_net_amt"] for k in MAIN_TIERS)
    sm_active_net = tiers["sm"]["active_net_amt"]
    retail_ratio = tiers["sm"]["buy_share"]
    xl_share = tiers["xl"]["buy_share"]
    active_ratio = (active_buy / amount * 100) if amount else None

    # ── 委托粒度（拆单代理）──────────────────────────────────────────────
    granularity: dict[str, Any] = {"available": False}
    if b_order and s_order and total_b_order and total_s_order:
        # 买卖两侧**分开算**再各自与成交粒度比：一手委托与一手成交的比值在两侧
        # 常常差一个量级（茅台买 48.4 手/笔 vs 卖 11.6 手/笔），合并平均会把
        # "哪一侧在被拆"这个信息抹平。加权合并值只作 headline。
        avg_buy = b_order / total_b_order
        avg_sell = s_order / total_s_order
        avg_order_lots = (b_order + s_order) / (total_b_order + total_s_order)
        traded_lots = sum(row[0] for row in vol) if vol else None
        avg_trade_lots = (traded_lots / cjbs) if (traded_lots and cjbs) else None
        gran_hiding = (avg_order_lots / avg_trade_lots) if avg_trade_lots else None
        order_fill_ratio = ((total_b_order + total_s_order) / cjbs) if cjbs else None
        cancel_buy_rate = (b_cancel / b_order) if (b_cancel is not None and b_order) else None
        cancel_sell_rate = (s_cancel / s_order) if (s_cancel is not None and s_order) else None
        granularity = {
            "available": True,
            "avg_order_lots": round(avg_order_lots, 2),
            "avg_order_lots_buy": round(avg_buy, 2),
            "avg_order_lots_sell": round(avg_sell, 2),
            "avg_trade_lots": round(avg_trade_lots, 3) if avg_trade_lots else None,
            "gran_hiding": round(gran_hiding, 2) if gran_hiding is not None else None,
            "gran_hiding_buy": round(avg_buy / avg_trade_lots, 2) if avg_trade_lots else None,
            "gran_hiding_sell": round(avg_sell / avg_trade_lots, 2) if avg_trade_lots else None,
            "order_fill_ratio": round(order_fill_ratio, 3) if order_fill_ratio is not None else None,
            "cancel_buy_rate": round(cancel_buy_rate * 100, 2) if cancel_buy_rate is not None else None,
            "cancel_sell_rate": round(cancel_sell_rate * 100, 2) if cancel_sell_rate is not None else None,
            "cancel_skew": (
                round((cancel_sell_rate - cancel_buy_rate) * 100, 2)
                if (cancel_buy_rate is not None and cancel_sell_rate is not None) else None
            ),
        }

    out: dict[str, Any] = {
        "available": True,
        "caliber": CALIBER,
        "source": SOURCE,
        "quality": quality,
        "reason": reason,
        "tier_thresholds_hint": TIER_THRESHOLDS_HINT,
        "amount": round(amount, 2),
        "sell_amount": round(sell_total, 2),
        "volume_lots": round(sum(row[0] for row in vol), 2) if vol else None,
        "active_buy_amt": round(active_buy, 2),
        "active_sell_amt": round(active_sell, 2),
        "active_ratio": round(active_ratio, 2) if active_ratio is not None else None,
        "tiers": tiers,
        "factors": {
            "retail_ratio": round(retail_ratio, 2),          # 散户化率（小单买入占比%）
            "xl_share": round(xl_share, 2),                  # 特大单占比%
            "main_active_net": round(main_active_net, 2),    # 特大+大 主动净额(元)
            "main_active_net_pct": round(main_active_net / amount * 100, 3),
            "sm_active_net": round(sm_active_net, 2),
            # 筹码迁移: 大单净买 − 小单净买。>0 = 大单进小单出（筹码向大资金集中）
            "chip_migration": round(main_active_net - sm_active_net, 2),
        },
        "granularity": granularity,
        # 原始标量原样带出（落库/回溯用），缺项保持 None
        "order_raw": {
            "b_order": b_order, "s_order": s_order,
            "total_b_order": total_b_order, "total_s_order": total_s_order,
            "b_cancel": b_cancel, "s_cancel": s_cancel, "cjbs": cjbs,
        },
    }
    out["tags"] = _conclusions(out)
    return out


def _conclusions(m: dict[str, Any]) -> list[dict[str, str]]:
    """把结构数字翻成**可核对的机读判语**（不含买卖建议）。

    每条都写明命中依据，让人能自己复核；不命中就返回空列表，不硬凑。
    """
    tags: list[dict[str, str]] = []
    g = m.get("granularity") or {}
    f = m.get("factors") or {}
    active_ratio = m.get("active_ratio")

    gran = g.get("gran_hiding")
    fill = g.get("order_fill_ratio")
    if gran is not None and fill is not None and gran >= HIDING_RATIO_STRONG and fill < FILL_RATIO_LOW:
        tags.append({
            "code": "passive_split",
            "label": "被动拆单结构",
            "basis": (f"委托粒度 {gran}×成交粒度、委托/成交笔数比 {fill}"
                      f"（一笔委托平均拆成 {round(1 / fill, 1) if fill else '--'} 笔成交）"),
        })
    elif gran is not None and gran >= HIDING_RATIO_WEAK:
        tags.append({
            "code": "granularity_wide",
            "label": "委托粒度偏大",
            "basis": f"委托粒度 {gran}×成交粒度（≥{HIDING_RATIO_WEAK}×）",
        })

    cb = g.get("cancel_buy_rate")
    cs = g.get("cancel_sell_rate")
    if cb is not None and cb >= CANCEL_RATE_HIGH * 100 and (active_ratio or 0) <= 50:
        tags.append({
            "code": "fake_bid",
            "label": "虚假托盘嫌疑",
            "basis": f"买撤 {cb}%（≥{CANCEL_RATE_HIGH * 100:.0f}%）+ 主动买占比 {active_ratio}%（≤50%）",
        })
    if cs is not None and cs >= CANCEL_RATE_HIGH * 100 and (active_ratio or 0) >= 50:
        tags.append({
            "code": "fake_ask",
            "label": "压盘恐吓嫌疑",
            "basis": f"卖撤 {cs}%（≥{CANCEL_RATE_HIGH * 100:.0f}%）+ 主动买占比 {active_ratio}%（≥50%）",
        })

    retail = f.get("retail_ratio")
    if retail is not None and retail >= 50:
        tags.append({
            "code": "retail_dominated",
            "label": "散户主导成交",
            "basis": f"小单买入占成交额 {retail}%（≥50%）",
        })
    mig = f.get("chip_migration")
    if mig is not None and mig > 0 and (f.get("main_active_net") or 0) > 0:
        tags.append({
            "code": "chip_to_big",
            "label": "筹码向大单集中",
            "basis": f"大单主动净额 {f['main_active_net'] / 1e4:+.0f}万 vs 小单 {f['sm_active_net'] / 1e4:+.0f}万",
        })
    elif mig is not None and mig < 0 and (f.get("main_active_net") or 0) < 0:
        tags.append({
            "code": "chip_to_small",
            "label": "筹码向小单转移",
            "basis": f"大单主动净额 {f['main_active_net'] / 1e4:+.0f}万 vs 小单 {f['sm_active_net'] / 1e4:+.0f}万",
        })
    return tags


def fetch(symbol: str, *, kline_amount: float | None = None) -> dict[str, Any]:
    """单票取数（TQ `get_exday_data`，实测 24~26ms）+ 计算。失败返回 available=False。"""
    code = _tq_code(symbol)
    if not code:
        return {"available": False, "caliber": CALIBER, "reason": f"代码无法识别: {symbol!r}"}
    try:
        from marketdata.vendors.tq import tq_rpc

        rows = tq_rpc("get_exday_data", {"stock_code": code, "count": 1}, timeout=8)
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "caliber": CALIBER, "reason": f"TQ 取数失败: {type(exc).__name__}"}
    if not isinstance(rows, list) or not rows:
        return {"available": False, "caliber": CALIBER, "reason": "TQ 无当日 exday 数据（非交易日/客户端未开）"}
    r = rows[-1]
    amo = parse_matrix(r.get("Amo"))
    vol = parse_matrix(r.get("Vol"))
    res = compute(
        amo, vol,
        b_order=_num(r.get("BOrder")), s_order=_num(r.get("SOrder")),
        total_b_order=_num(r.get("TotalBOrder")), total_s_order=_num(r.get("TotalSOrder")),
        b_cancel=_num(r.get("BCancel")), s_cancel=_num(r.get("SCancel")),
        cjbs=_num(r.get("CJBS")), kline_amount=kline_amount,
    )
    if res.get("available"):
        res["symbol"] = str(symbol)
        res["trade_date"] = str(r.get("Date") or "")
    return res


def _tq_code(symbol: str) -> str | None:
    """裸码 → TQ 带后缀代码（`*_jy_value`/`exday` 不吃裸码）。"""
    s = str(symbol or "").strip().upper()
    if not s:
        return None
    if "." in s:
        return s
    if s.startswith(("6", "9", "5")):
        return f"{s}.SH"
    if s.startswith(("0", "3", "1", "2")):
        return f"{s}.SZ"
    if s.startswith(("4", "8")):
        return f"{s}.BJ"
    return None


def to_row(m: dict[str, Any], *, symbol: str, trade_date: str, market: str = "CN") -> dict[str, Any]:
    """计算结果的扁平化行（供 `exday_daily` 落库；缺项写 None 不补 0）。"""
    f = m.get("factors") or {}
    g = m.get("granularity") or {}
    t = m.get("tiers") or {}

    def tier_amt(key: str, field: str) -> float | None:
        return (t.get(key) or {}).get(field)

    row: dict[str, Any] = {
        "trade_date": str(trade_date).replace("-", "")[:8],
        "symbol": str(symbol).strip(),
        "market": market,
        "amount": m.get("amount"),
        "volume_lots": m.get("volume_lots"),
        "active_buy_amt": m.get("active_buy_amt"),
        "active_sell_amt": m.get("active_sell_amt"),
        "active_ratio": m.get("active_ratio"),
        "retail_ratio": f.get("retail_ratio"),
        "xl_share": f.get("xl_share"),
        "main_active_net": f.get("main_active_net"),
        "main_active_net_pct": f.get("main_active_net_pct"),
        "sm_active_net": f.get("sm_active_net"),
        "chip_migration": f.get("chip_migration"),
        "gran_hiding": g.get("gran_hiding"),
        "order_fill_ratio": g.get("order_fill_ratio"),
        "avg_order_lots": g.get("avg_order_lots"),
        "avg_trade_lots": g.get("avg_trade_lots"),
        "cancel_buy_rate": g.get("cancel_buy_rate"),
        "cancel_sell_rate": g.get("cancel_sell_rate"),
        "cancel_skew": g.get("cancel_skew"),
        "b_order": None, "s_order": None, "total_b_order": None, "total_s_order": None,
        "b_cancel": None, "s_cancel": None, "cjbs": None,
        "quality": m.get("quality"),
        "reason": (m.get("reason") or "")[:200] or None,
        "source": SOURCE,
    }
    raw = m.get("order_raw") or {}
    for key in ("b_order", "s_order", "total_b_order", "total_s_order", "b_cancel", "s_cancel", "cjbs"):
        if raw.get(key) is not None:
            row[key] = raw[key]
    for key in ("xl", "lg", "md", "sm"):
        row[f"{key}_buy_amt"] = tier_amt(key, "buy_amt")
        row[f"{key}_sell_amt"] = tier_amt(key, "sell_amt")
        row[f"{key}_active_net"] = tier_amt(key, "active_net_amt")
    return row


def render(m: dict[str, Any], *, title: str = "") -> str:
    """人话渲染（工具层用）。缺项写「--」，口径与阈值提示必须带出来。"""
    if not m.get("available"):
        return f"[四档资金结构] {m.get('reason', '无数据')}——不做推断。"
    f = m["factors"]
    g = m.get("granularity") or {}
    head = f"[数据源: 通达信 TQ get_exday_data · 口径 {CALIBER} · {m.get('trade_date') or '当日'}]"
    if title:
        head += f" {title}"
    lines = [head, f"四档阈值：{m.get('tier_thresholds_hint')}"]
    if m.get("quality") != "ok":
        lines.append(f"⚠️ 数据待核：{m.get('reason')}")

    lines.append("成交额 {:.2f}亿｜主动买 {:.2f}亿 / 主动卖 {:.2f}亿｜主动买占比 {}%".format(
        (m["amount"] or 0) / 1e8,
        (m["active_buy_amt"] or 0) / 1e8,
        (m["active_sell_amt"] or 0) / 1e8,
        "--" if m.get("active_ratio") is None else m["active_ratio"],
    ))
    for key in TIERS:
        t = m["tiers"][key]
        lines.append(
            "  {}：买 {:.2f}亿（占 {:.1f}%）｜主动净 {:+.0f}万".format(
                t["label"], t["buy_amt"] / 1e8, t["buy_share"], t["active_net_amt"] / 1e4
            )
        )
    lines.append(
        "结构因子：散户化率 {r}%｜特大单占比 {x}%｜主力(特大+大)主动净额 {m:+.0f}万"
        "（占成交额 {p}%）｜筹码迁移 {c:+.0f}万".format(
            r=f["retail_ratio"], x=f["xl_share"], m=f["main_active_net"] / 1e4,
            p=f["main_active_net_pct"], c=f["chip_migration"] / 1e4,
        )
    )
    if g.get("available"):
        lines.append(
            "委托粒度：笔均委托 {a} 手｜笔均成交 {b} 手｜**隐匿度 {h}×**｜委托/成交笔数比 {r}"
            "｜买撤 {cb}% 卖撤 {cs}%".format(
                a=g.get("avg_order_lots"), b=g.get("avg_trade_lots"),
                h=g.get("gran_hiding"), r=g.get("order_fill_ratio"),
                cb=g.get("cancel_buy_rate"), cs=g.get("cancel_sell_rate"),
            )
        )
        if g.get("gran_hiding"):
            lines.append(
                "  读法：隐匿度>1 = 委托被拆成多笔成交（被动拆单）；委托/成交笔数比<1 表示"
                "一笔委托对应多笔成交，越小拆单越重。"
            )
    else:
        lines.append("委托粒度：未取到（缺委托量/笔数）——不推断。")
    for tag in m.get("tags") or []:
        lines.append(f"  ▸ {tag['label']}：{tag['basis']}")
    lines.append("⚠️ 口径：按单笔成交金额分档的客户端口径，与东财四档、腾讯逐笔不可混用；"
                 "档位阈值官方未公开，本文为推断值。")
    return "\n".join(lines)
