# -*- coding: utf-8 -*-
"""全市场暗盘资金 TOP 扫描(设计稿 §6.1「暗盘资金 TOP 榜 .tck/thsdk」, A6, 2026-09-01)。

把 thsdk 批量 DDE 主力资金流接成全市场扫描, 替代 market_scan.scan() 里
dark_top 用的 OHLC 分摊对照项(approximation=True)。核心差异:

  - 旧 dark_top: 逐只拉 K 线 + OHLC 分摊估暗盘, 慢(全市场 60s)且是**对照项**非真实暗盘
  - 新 scan_dark_fund_top: thsdk `get_dde_flow` **批量**接口(200只/批, 全市场 5141 只
    约 26 批 ≈ 16s), 拿同花顺官方**主力净流入**, 是真实资金流。

## 诚实口径(重要)

- `main_net_wan` = thsdk DDE **主力净流入**(同花顺官方口径), 属「主力/大单资金」,
  严格说不等于「暗盘拆单资金」(暗盘 = 拆单隐藏意图, 见 dark_flow._detect_split_orders)。
  字段用 source 显式标注 `thsdk_dde`, 不冒充暗盘。
- `.tck` 融合: 对持仓股且有 .tck 文件的, 附加 postmarket_review 的委托号级精确暗盘
  (tck_dark_net), 与主力净流入并列, 让使用者自行对照, 不混成单一数字。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# 全市场扫描默认市场(北交所 USTM 在代码表里 0 只, 不扫)
DEFAULT_MARKETS = ("USHA", "USZA")
DEFAULT_BATCH = 200

# int32 溢出哨兵值阈值: thsdk 盘后对无真实资金流数据的股票(尤其次新股)返回
# 2^31-1(2147483647) 或 2^31(2147483648) 作占位, 绝对值 >= 此阈值的判定为无效并过滤。
INT32_SENTINEL = 2_147_483_000


def _load_codes_and_names(l2) -> tuple[dict[str, str], dict[str, list[str]]]:
    """拉全市场代码表 → (6位代码→名称映射, 市场→6位代码列表)。

    get_stock_cn_lists 返回 THS 代码(USZA002346), 剥前缀得 6 位代码。
    """
    name_map: dict[str, str] = {}
    by_market: dict[str, list[str]] = {}
    try:
        df = l2.get_stock_cn_lists()
        if df is None or len(df) == 0:
            return name_map, by_market
        for _, row in df.iterrows():
            raw = str(row.get("代码", ""))
            if len(raw) < 6:
                continue
            prefix = raw[:4]
            six = raw[4:]
            name_map[six] = str(row.get("名称", "") or "")
            by_market.setdefault(prefix, []).append(six)
    except Exception as e:  # noqa: BLE001
        logger.warning("[暗盘TOP] 全市场代码表拉取失败: %s", e)
    return name_map, by_market


def scan_dark_fund_top(
    top_n: int = 20,
    markets: tuple[str, ...] = DEFAULT_MARKETS,
    batch_size: int = DEFAULT_BATCH,
    l2=None,
) -> dict:
    """全市场主力资金流(thsdk DDE) TOP 扫描。

    Args:
        top_n: 榜保留条数
        markets: 市场前缀(USHA 沪 / USZA 深)
        batch_size: 每批代码数(实测 200 只/批约 0.6s)

    Returns:
        {
          "generated_at": iso,
          "universe": 全市场代码数,
          "computed": 成功拿到资金流的股票数,
          "top": [{
             "symbol", "name", "ths_code",
             "main_net_wan": 主力净流入(万元),
             "main_net_ratio": 主力净量占比,
             "total_amount_wan": 总成交额(万元),
             "source": "thsdk_dde",
          }]  按 main_net_wan 降序
        }
    """
    if l2 is None:
        from data_source.thsdk_l2 import THSDKL2

        l2 = THSDKL2()

    name_map, by_market = _load_codes_and_names(l2)
    universe = sum(len(v) for v in by_market.values())

    all_rows: list[dict] = []
    for mkt in markets:
        codes = by_market.get(mkt, [])
        for i in range(0, len(codes), batch_size):
            batch = codes[i : i + batch_size]
            if not batch:
                continue
            try:
                dde = l2.get_dde_flow(",".join(batch), market=mkt, detail=False)
                if dde is not None and len(dde):
                    all_rows.extend(dde.to_dict("records"))
            except Exception as e:  # noqa: BLE001
                logger.warning("[暗盘TOP] %s 第%d批失败: %s", mkt, i // batch_size, str(e)[:80])

    # 组装榜单
    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    top_rows: list[dict] = []
    for r in all_rows:
        raw_code = str(r.get("代码", ""))
        six = raw_code[4:] if raw_code[:4] in ("USHA", "USZA", "USTM") else raw_code
        main_net = _f(r.get("主力净流入"))
        if main_net is None:
            continue
        # 过滤 int32 溢出哨兵值(盘后无真实数据的次新股返回 2^31-1/2^31 占位)
        if abs(main_net) >= INT32_SENTINEL:
            continue
        # 主力净量同理过滤溢出(2147483648)
        ratio = _f(r.get("主力净量"))
        if ratio is not None and abs(ratio) >= INT32_SENTINEL:
            ratio = None
        total_amt = _f(r.get("总金额"))
        if total_amt is not None and abs(total_amt) >= INT32_SENTINEL:
            total_amt = None
        top_rows.append(
            {
                "symbol": six,
                "name": name_map.get(six, ""),
                "ths_code": raw_code,
                "main_net_wan": round(main_net / 1e4, 2),       # 元 → 万元
                "main_net_ratio": ratio,
                # 总金额溢出/缺失 → None(不编造成 0)
                "total_amount_wan": round(total_amt / 1e4, 2) if total_amt is not None else None,
                "source": "thsdk_dde",
            }
        )

    top_rows.sort(key=lambda x: -x["main_net_wan"])
    top = top_rows[:top_n]

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "universe": universe,
        "computed": len(top_rows),
        "top": top,
    }


def attach_tck_dark(top: list[dict], positions_symbols: list[str]) -> list[dict]:
    """.tck 融合: 对持仓股且有 .tck 文件的, 附加委托号级精确暗盘净额。

    不覆盖 main_net_wan(主力资金流), 而是并列加 tck_dark_net 字段,
    让使用者自行对照「主力资金」与「委托号级暗盘」两个口径。
    """
    from src.core.postmarket_review import dark_review_from_tck

    syms = set(positions_symbols)
    out = []
    for item in top:
        it = dict(item)
        if item["symbol"] in syms:
            try:
                rev = dark_review_from_tck(item["symbol"])
                if rev.get("available"):
                    it["tck_dark_net_wan"] = round((rev.get("main_net") or 0.0) / 1e4, 2)
                    it["tck_available"] = True
                else:
                    it["tck_available"] = False
            except Exception as e:  # noqa: BLE001
                logger.debug("[暗盘TOP] .tck 融合失败 %s: %s", item["symbol"], e)
                it["tck_available"] = False
        out.append(it)
    return out
