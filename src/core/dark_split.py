# -*- coding: utf-8 -*-
"""暗盘 / 明盘 拆分 + big_order_flow 方向解码(纯函数层, 无 IO, 可单测)。

## 方向编码实测结论(2026-08-31, thsdk 1.7.18, 神剑股份 USZA002361, 253 行)

`big_order_flow` 的 `成交方向` 只取 **1 / 2 / -1 / -2** 四个值:

| 编码 | 含义           | 成交价落点         |
|------|----------------|--------------------|
|   1  | 主动买(吃卖盘) | 成交在**卖**档价   |
|  -1  | 主动卖(吃买盘) | 成交在**买**档价   |
|   2  | 被动买(挂买被吃)| 成交在**买**档价   |
|  -2  | 被动卖(挂卖被吃)| 成交在**卖**档价   |

即:**符号 = 买(+) / 卖(-), 绝对值 = 主动(1) / 被动(2)**。

### 判定依据
用同时间的 `tick_super_level1` 档位价反推成交性质(隐含价 = 总金额 / 成交量):

    方向 =  1 → 85.9% 落在卖档价(主动买)   ✅
    方向 = -1 → 83.6% 落在买档价(主动卖)   ✅
    方向 =  2 → 82.9% 落在买档价(被动买)   ✅
    方向 = -2 → 90.2% 落在卖档价(被动卖)   ✅

### ⚠️ 与仓库既有注释的冲突(已纠正)
`data_source/thsdk_l2.py::get_big_orders` 的 docstring 原写
「1=主买, 5=主卖, 15/17/21=大额成交, 0=中性, 4294967295=无效」——
**那是 `tick_super_level1` 的编码, 被错贴到了 big_order_flow 上**。
本次实测 big_order_flow 253 行里**没有出现 5/15/17/21/0/4294967295 任何一个**。

### ⚠️ 主动/被动不分的后果(实测, 同一份数据)
    主动净额(|方向|=1 相减) = +10,562,488 元   → 净流入
    被动净额(|方向|=2 相减) = -13,969,851 元   → 净流出
    全口径净额(不分主动被动) =  -3,407,363 元   → 净流出

**两种口径结论符号相反**。资金口径必须显式声明取哪一种, 禁止混用。

## 单位口径
价格 = 元, 成交量 = 股, 金额 = 元(项目硬约束)。
`big_order_flow` 无价格列, 隐含价 = 总金额 / 成交量(实测 10.31~10.57 元, 与当日区间吻合)。

## 官方口径(《决策先锋8问8答》PDF + 复刻手册, 2026-08-31)
    主力资金净流入 = 明盘净额 + 暗盘净额
    明盘 = 单笔 > 30 万 的大单资金(市场可见, 可被拆单伪装)
    暗盘 = AI 识别的私募量化单 / 机构游资对倒拆单 / 大单拆小单

数学自洽验证(官方示例, 单位万元):
    中航产融  1887.3 + 1052.8 = 2940.1 = 主力 ; 散户 -2940.1  ✅
    欣锐科技 -4557.4 - 5116.7 = -9674.1 = 主力 ; 散户  9674.1  ✅
    杭州热电   -0.43 + 1.33   =   0.90  = 主力 ; 散户  -0.90   ✅
即 全市场净流入 = 主力 + 散户 = 0, 散户 = -主力。

## ⚠️ big_order_flow 就是"明盘"专用源(实测)
单笔金额 min = **300,105 元**, 253 行 **100% >= 30 万**(p25=33.9万 / 中位=43.3万 / max=428万)。
接口本身已经按官方 30 万阈值过滤 → **对它再按 30 万拆分明盘/暗盘是无意义的**(暗盘恒为 0)。

正确用法:
    - 明盘净额  → `ming_net_from_big_orders()` 直接汇总 big_order_flow
    - 暗盘净额  → 需全量逐笔(thsdk tick_super_level1 / 腾讯逐笔) + 拆单识别,
                  L1 近似 = OHLC 分摊 + big_order_flow 校准(见复刻手册阶段1)
    - 主力净额  = 明盘净额 + 暗盘净额, > 0 流入(红), < 0 流出(绿)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# --- big_order_flow 方向编码 ---
BIG_ORDER_ACTIVE_BUY = 1      # 主动买(吃卖盘)
BIG_ORDER_ACTIVE_SELL = -1    # 主动卖(吃买盘)
BIG_ORDER_PASSIVE_BUY = 2     # 被动买(挂买被吃)
BIG_ORDER_PASSIVE_SELL = -2   # 被动卖(挂卖被吃)

# 方向 → (成交性质, 买卖)
_DIR_MAP: dict[int, tuple[str, str]] = {
    BIG_ORDER_ACTIVE_BUY: ("active", "B"),
    BIG_ORDER_ACTIVE_SELL: ("active", "S"),
    BIG_ORDER_PASSIVE_BUY: ("passive", "B"),
    BIG_ORDER_PASSIVE_SELL: ("passive", "S"),
}

# --- 明盘阈值(单笔净额, 元) ---
MING_THRESHOLD_30W = 300_000.0    # 30 万
MING_THRESHOLD_100W = 1_000_000.0  # 100 万


@dataclass
class FlowSplit:
    """明盘 / 暗盘 拆分结果。单位: 元。"""

    ming_buy: float = 0.0     # 明盘买入额(元)
    ming_sell: float = 0.0    # 明盘卖出额(元)
    dark_buy: float = 0.0     # 暗盘买入额(元)
    dark_sell: float = 0.0    # 暗盘卖出额(元)
    ming_count: int = 0       # 明盘笔数
    dark_count: int = 0       # 暗盘笔数
    skipped: int = 0          # 方向无效被跳过的笔数

    @property
    def ming_net(self) -> float:
        """明盘净额(元) = 大单买 - 大单卖。"""
        return self.ming_buy - self.ming_sell

    @property
    def dark_net(self) -> float:
        """暗盘净额(元) = 小单买 - 小单卖。"""
        return self.dark_buy - self.dark_sell

    @property
    def total_net(self) -> float:
        """明盘净额 + 小单净额(元)。

        ⚠️ 官方"主力净额" = 明盘 + **暗盘**, 而暗盘 ⊂ 小单(小单中被识别为拆单/对倒的部分)。
        本属性把**全部小单**都算进去了, 因此是主力净额的**上界近似**,
        暗盘识别(L1 近似 = OHLC 分摊 + big_order_flow 校准)落地后应以暗盘替换小单。
        """
        return (self.ming_buy + self.dark_buy) - (self.ming_sell + self.dark_sell)

    def to_dict(self) -> dict:
        return {
            "ming_buy": round(self.ming_buy, 2),
            "ming_sell": round(self.ming_sell, 2),
            "ming_net": round(self.ming_net, 2),
            "dark_buy": round(self.dark_buy, 2),
            "dark_sell": round(self.dark_sell, 2),
            "dark_net": round(self.dark_net, 2),
            "total_net": round(self.total_net, 2),
            "ming_count": self.ming_count,
            "dark_count": self.dark_count,
            "skipped": self.skipped,
        }


def decode_direction(raw_dir: int) -> Optional[tuple[str, str]]:
    """big_order_flow 方向编码 → (成交性质, 买卖)。

    Args:
        raw_dir: `成交方向` 原始值(1 / 2 / -1 / -2)

    Returns:
        (side, d): side ∈ {"active", "passive"}, d ∈ {"B", "S"}
        非法编码返回 None(调用方必须显式跳过, 不得猜方向)。

    注意: 编码是 int, 但 thsdk 可能给 float(如 1.0), 这里做兼容。
    """
    if raw_dir is None:
        return None
    try:
        key = int(raw_dir)
    except (TypeError, ValueError):
        return None
    return _DIR_MAP.get(key)


def split_ming_dark(
    ticks: Iterable[dict],
    threshold_yuan: float = MING_THRESHOLD_30W,
) -> FlowSplit:
    """按单笔金额阈值把**全量逐笔**拆成明盘 / 小单。

    ⚠️ 输入必须是全量逐笔(thsdk tick_super_level1 / 腾讯逐笔),
    **不能喂 `big_order_flow`**——它已被接口侧按 30 万过滤(实测 253 行 100% >= 30万),
    再拆分时暗盘恒为 0, 会把"小单净额"伪装成"暗盘净额"。

    口径(对齐《决策先锋8问8答》):
        明盘 = 单笔金额 >= threshold_yuan 的大单
        小单 = 单笔金额 <  threshold_yuan
        注意: 官方"暗盘"= 小单中被 AI 识别为拆单/对倒的部分, **不等同于全部小单**;
              本函数给出的是"小单净额", 暗盘需在其基础上叠加拆单识别(L1 近似 = OHLC 分摊)。

    Args:
        ticks: 全量逐笔, 每项需含 `amt`(元) 与 `d`("B"/"S")。
               可选 `side`("active"/"passive"), 仅作统计不参与判定。
        threshold_yuan: 明盘阈值(元), 默认 30 万。

    Returns:
        FlowSplit。金额单位 = 元。

    缺失处理: `amt` 缺失或非正、`d` 非 "B"/"S" 的行计入 skipped, 不参与任何汇总,
    绝不按 0 补齐(那会把"无数据"伪装成"无成交")。
    """
    out = FlowSplit()
    for t in ticks:
        amt = t.get("amt")
        d = t.get("d")
        if not isinstance(amt, (int, float)) or amt <= 0:
            out.skipped += 1
            continue
        if d not in ("B", "S"):
            out.skipped += 1
            continue
        if amt >= threshold_yuan:
            if d == "B":
                out.ming_buy += float(amt)
            else:
                out.ming_sell += float(amt)
            out.ming_count += 1
        else:
            if d == "B":
                out.dark_buy += float(amt)
            else:
                out.dark_sell += float(amt)
            out.dark_count += 1
    return out


def active_passive_net(ticks: Iterable[dict]) -> dict:
    """按成交性质(主动/被动)分别统计净额。

    ⚠️ 与 `split_ming_dark` 是两个正交维度:
        - 明盘/暗盘 按**单笔金额**切
        - 主动/被动 按**成交性质**切
    实测两者结论可能符号相反, 必须分别呈现, 禁止合并成一个"净额"。

    Returns:
        {"active_net": 元, "passive_net": 元, "total_net": 元,
         "active_count": int, "passive_count": int, "unknown": int}
    """
    active_net = passive_net = 0.0
    active_count = passive_count = unknown = 0
    for t in ticks:
        amt = t.get("amt")
        d = t.get("d")
        side = t.get("side")
        if not isinstance(amt, (int, float)) or amt <= 0 or d not in ("B", "S"):
            unknown += 1
            continue
        signed = float(amt) if d == "B" else -float(amt)
        if side == "active":
            active_net += signed
            active_count += 1
        elif side == "passive":
            passive_net += signed
            passive_count += 1
        else:
            unknown += 1
    return {
        "active_net": round(active_net, 2),
        "passive_net": round(passive_net, 2),
        "total_net": round(active_net + passive_net, 2),
        "active_count": active_count,
        "passive_count": passive_count,
        "unknown": unknown,
    }


def ming_net_from_big_orders(ticks: Iterable[dict]) -> dict:
    """直接汇总 `big_order_flow`(官方"明盘"专用源) → 明盘净额。

    ⚠️ 这是明盘净额的**权威**算法: big_order_flow 单笔金额 min = 300,105 元
    (2026-08-31 神剑股份实测, 253 行 100% >= 30 万), 接口侧已完成 30 万过滤,
    因此不做二次阈值拆分——拆了暗盘恒为 0, 是错误用法。

    Args:
        ticks: `dark_l2.fetch_l2_ticks(code, "thsdk_big_order")` 的输出,
               每项含 `amt`(元) / `d`("B"/"S") / `side`("active"/"passive")。

    Returns:
        {
          "ming_buy"/"ming_sell"/"ming_net": 元,
          "active_net"/"passive_net": 元,     # 主动/被动分列(官方未定义, 仅作参考)
          "count"/"skipped": int,
          "source": "big_order_flow",
        }

    缺失处理: 方向非法 / 金额非正的行计入 skipped, 不猜方向。
    """
    ming_buy = ming_sell = 0.0
    active_net = passive_net = 0.0
    count = skipped = 0
    for t in ticks:
        amt = t.get("amt")
        d = t.get("d")
        if not isinstance(amt, (int, float)) or amt <= 0 or d not in ("B", "S"):
            skipped += 1
            continue
        signed = float(amt) if d == "B" else -float(amt)
        if d == "B":
            ming_buy += float(amt)
        else:
            ming_sell += float(amt)
        side = t.get("side")
        if side == "active":
            active_net += signed
        elif side == "passive":
            passive_net += signed
        else:
            pass  # side 缺失不影响明盘汇总, 只影响主动/被动分列
        count += 1
    return {
        "ming_buy": round(ming_buy, 2),
        "ming_sell": round(ming_sell, 2),
        "ming_net": round(ming_buy - ming_sell, 2),
        "active_net": round(active_net, 2),
        "passive_net": round(passive_net, 2),
        "count": count,
        "skipped": skipped,
        "source": "big_order_flow",
    }


# ---------------------------------------------------------------------------
# .tck 委托号级暗盘还原(2026-09-01, Hermes 0831 口径)
# ---------------------------------------------------------------------------
def find_tck_file(symbol: str, date_: str | None = None) -> str | None:
    """在 PANWATCH_TCK_DIR 下按代码(可选日期)找 .tck 文件。

    约定文件名包含 6 位代码; 传了 date_ 则再要求包含该日期(支持 `2026-08-31`
    与 `20260831` 两种写法)。目录未配置或文件不存在 → None(调用方显式"无数据")。
    """
    base = (os.environ.get("PANWATCH_TCK_DIR") or "").strip()
    if not base or not os.path.isdir(base):
        return None
    code = (symbol or "").strip()
    if not code:
        return None
    date_keys = []
    if date_:
        d = str(date_).strip()
        date_keys.append(d.replace("-", ""))
        date_keys.append(d)
    try:
        hits = []
        for name in os.listdir(base):
            if not name.lower().endswith(".tck"):
                continue
            if code not in name:
                continue
            if date_keys and not any(k in name for k in date_keys):
                continue
            hits.append(name)
        if not hits:
            return None
        hits.sort()
        return os.path.join(base, hits[-1])
    except Exception as e:  # noqa: BLE001
        logger.warning("扫描 .tck 目录失败 %s: %s", base, e)
        return None


def dark_flow_from_tck(trades: Iterable[dict], orders: Iterable[dict] | None = None,
                       threshold_yuan: float = MING_THRESHOLD_30W) -> dict:
    """.tck 主笔级暗盘还原(Hermes 0831 口径)。

    口径说明(严格按实测事实, 不编造):
      - **主动侧**: `trades` 的 tag 为 `2B`(主买) / `2S`(主卖), 与委托的 a28/a32
        是 1:1 对应关系 → 主动买/卖净额可直接精确汇总。
      - **被动侧**: 委托单(tag `00`)的 `a28`(→主动买成交) / `a32`(→主动卖成交)
        指向吃掉它的那笔主动成交。若 a28 非空说明这笔委托**被主动买吃掉**
        → 该委托是挂卖单(被动卖); a32 非空 → 挂买单(被动买)。
        ⚠️ maker(挂单方)在 .tck 里**未完整落盘**, 因此被动侧只能做到
        **主笔级还原**, 不是逐笔委托号级完整还原, 结果带 `partial=True` 标记。

    明盘/暗盘分档: 复用 `split_ming_dark`(单笔 > threshold_yuan 归明盘, 其余归小单),
    暗盘 ⊂ 小单中被识别为拆单的部分, 此处未做拆单识别 → 返回的是**小单口径**,
    以 `dark_basis: "small_orders"` 明示, 不做等价冒充。

    单位: 金额 = 元, 量 = 股(项目硬约束)。

    Returns:
        {active_buy, active_sell, active_net, passive_buy, passive_sell, passive_net,
         net, ming_net, small_net, trade_count, order_count, partial, dark_basis}
        无 trades → net 等为 None 且 note="无数据"。
    """
    trades = list(trades or [])
    orders = list(orders or [])
    if not trades:
        return {
            "active_buy": None, "active_sell": None, "active_net": None,
            "passive_buy": None, "passive_sell": None, "passive_net": None,
            "net": None, "ming_net": None, "small_net": None,
            "trade_count": 0, "order_count": 0,
            "partial": True, "dark_basis": "small_orders", "note": "无数据",
        }

    active_buy = active_sell = 0.0
    for t in trades:
        amt = float(t.get("amt") or 0.0)
        if (t.get("dir") or "").upper().startswith("B"):
            active_buy += amt
        elif (t.get("dir") or "").upper().startswith("S"):
            active_sell += amt

    passive_buy = passive_sell = 0.0
    for o in orders:
        amt = float(o.get("amt") or 0.0)
        if o.get("a32"):        # 被主动卖吃掉 → 挂买单(被动买)
            passive_buy += amt
        elif o.get("a28"):      # 被主动买吃掉 → 挂卖单(被动卖)
            passive_sell += amt

    # 适配: parse_tck 的成交用 `dir`("B"/"S"), split_ming_dark 约定用 `d`。
    # 在边界转换, 不改两侧接口; 方向无法识别的笔交给 split_ming_dark 计入 skipped。
    fs = split_ming_dark(
        [{"amt": t.get("amt"), "d": (t.get("dir") or "").upper()[:1]} for t in trades],
        threshold_yuan=threshold_yuan,
    )
    return {
        "active_buy": round(active_buy, 2),
        "active_sell": round(active_sell, 2),
        "active_net": round(active_buy - active_sell, 2),
        "passive_buy": round(passive_buy, 2),
        "passive_sell": round(passive_sell, 2),
        "passive_net": round(passive_buy - passive_sell, 2),
        "net": round((active_buy + passive_buy) - (active_sell + passive_sell), 2),
        "ming_net": round(fs.ming_net, 2),
        "small_net": round(fs.dark_net, 2),
        "trade_count": len(trades),
        "order_count": len(orders),
        # maker 未落盘 → 被动侧仅是主笔级还原, 非委托号级完整还原
        "partial": True,
        "dark_basis": "small_orders",
        "note": "被动侧 maker 未落盘, 仅主笔级还原; 暗盘为小单口径(未做拆单识别)",
    }
