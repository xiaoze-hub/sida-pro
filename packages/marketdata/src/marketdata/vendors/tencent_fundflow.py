"""腾讯证券个股资金流 vendor(2026-08-11 接入)。

端点: proxy.finance.qq.com/cgi/cgi-bin/fundflow/hsfundtab
- todayFundFlow   : 当日资金流(主力/散户, 含超大/大/中/小四档净流入) —— 实时滚动
- fiveDayFundFlow : 近5日主力净流入(每日)
- todayFundTrend  : 当日分时资金趋势(逐分钟 主力/超大/大/中/小)
- historyFundFlow : 历史资金流(暂未用)

口径说明(接口 desc 原文): 主力 = 超大单 + 大单; 成交额>=20万 或 >=6万股 判为"主力资金"。
与东财 push2delay 同为当日实时口径, 可交叉验证; 开盘初期东财 f62=0 未就绪时,
腾讯侧通常已有数据, 是理想的第二源。

字段单位: 接口返回元(如 mainNetIn=-17965947 = -1796.6万), 入库统一转元。
"""
from __future__ import annotations

import json
import logging
import urllib.request

from marketdata.symbol import Symbol
from marketdata.types import CapitalFlow
from marketdata.vendors.base import CapitalFlowVendor

logger = logging.getLogger(__name__)

_FUNDFLOW_URL = "https://proxy.finance.qq.com/cgi/cgi-bin/fundflow/hsfundtab"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://gu.qq.com/",
}


def _tencent_code(symbol: Symbol) -> str | None:
    """转腾讯代码格式(sz002361/sh600519): 6/9开头=沪, 0/2/3=深。"""
    code = (symbol.code or "").strip()
    if not code.isdigit() or len(code) != 6:
        return None
    if code[0] in ("6", "9") or code.startswith("688"):
        return f"sh{code}"
    if code[0] in ("0", "2", "3"):
        return f"sz{code}"
    return None


def _fetch_fundflow(code: str, types: str) -> dict | None:
    url = f"{_FUNDFLOW_URL}?code={code}&type={types}&klineNeedDay=20"
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            body = resp.read().decode("utf-8", "replace")
        j = json.loads(body)
        if j.get("code") != 0:
            logger.warning(f"[tencent_fundflow] {code} 返回 code={j.get('code')}: {j.get('msg')}")
            return None
        return j.get("data") or {}
    except Exception as e:
        logger.warning(f"[tencent_fundflow] {code} 请求失败: {type(e).__name__}: {e}")
        return None


class TencentFundflowVendor(CapitalFlowVendor):
    """腾讯证券个股资金流(CN, 当日实时四档 + 5日趋势)。"""

    name = "tencent"
    supports_markets = {"CN"}

    def fetch(self, symbols: list[Symbol], config: dict) -> list[CapitalFlow]:
        out: list[CapitalFlow] = []
        for symbol in symbols:
            code = _tencent_code(symbol)
            if not code:
                continue
            data = _fetch_fundflow(code, "todayFundFlow,fiveDayFundFlow")
            if not data:
                continue
            today = data.get("todayFundFlow") or {}
            if not today.get("stockCode"):
                continue
            try:
                main_net = _f(today.get("mainNetIn"))
                main_rate = _f(today.get("mainInRate"))
                flow = CapitalFlow(
                    symbol=symbol.code,
                    name="",
                    main_net_inflow=main_net,
                    # 腾讯只给占比整数(12%=12), 与东财的百分比口径不同; 用占比原始整数存,
                    # 调用方需注意口径差异(此处存 12 表示 12%)
                    main_net_inflow_pct=main_rate,
                    super_net_inflow=_f(today.get("superFlow")),
                    big_net_inflow=_f(today.get("bigFlow")),
                    mid_net_inflow=_f(today.get("normalFlow")),
                    small_net_inflow=_f(today.get("smallFlow")),
                    main_net_5d=_f((data.get("fiveDayFundFlow") or {}).get("fiveDayMainNetIn")),
                    date="",
                )
                out.append(flow)
            except (TypeError, ValueError) as e:
                logger.warning(f"[tencent_fundflow] {code} 解析失败: {e}")
        return out


def _f(v) -> float | None:
    """转 float, 空/None 返回 None。"""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
