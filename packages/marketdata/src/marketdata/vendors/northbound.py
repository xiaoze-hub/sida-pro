"""北向资金 vendor:同花顺(ths/hexin)当日分钟累计净买入,市场级(symbols 恒空)。

背景:东财 datacenter/push2 的北向资金接口(kamt)自 2024-08 起断供(返回 NaN/0),
不可用。改走同花顺 hexin 私有接口 `data.hexin.cn/market/hsgtApi/method/dayChart/`,
返回当日分钟级累计净买入序列,`hgt`(沪股通)/`sgt`(深股通),单位均为"亿元"。

响应结构(2026-08-09 实抓海外节点 43.128.140.167):root level 三个键
  `time` str[] 时间序列(09:10..15:00 共 262 个,含午休跳点)
  `hgt`  float[] 沪股通累计净买入(亿元),长度 = len(time) = 262
  `sgt`  float[] 深股通累计净买入(亿元),长度 < len(time)(本会话实测 35,断在 09:44)
响应里**没有 date 字段**——date 走 config['date'] 或今天。

已知坑(SKILL 标注):`sgt`(深股通)近期数据不可靠,实测 35 个值就断在 09:44,且末值 379.75
亿元量级明显异常(正常单日深股通净买入 |x|<100 亿)。必须容错:
- `sgt` 长度 < 0.25 * len(time) 视为断流 → sgt_net=None,不参与 total_net
- 单值 abs>2000 一律视为脏值丢弃
"""
from __future__ import annotations

from marketdata.http import market_get
from marketdata.symbol import Symbol
from marketdata.types import NorthboundItem
from marketdata.vendors.base import NorthboundVendor as _NorthboundVendorBase

_HEXIN_URL = "https://data.hexin.cn/market/hsgtApi/method/dayChart/"
_HEXIN_HOST = "data.hexin.cn"

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

_HEADERS = {
    "Host": _HEXIN_HOST,
    "Referer": "https://data.hexin.cn/",
    "User-Agent": _UA,
}

# sgt(深股通)近期不可靠,可能出现量级异常(远超合理"亿元"净买入范围)的脏值;
# 超过此绝对值阈值一律视为异常丢弃。阈值本身是防御性经验值,非精确业务规则。
_SGT_MAX_ABS = 2000.0


def _to_float(value) -> float | None:
    """宽松转 float;None/无法转换/NaN 一律 None(NaN 用 f != f 判定,不额外 import math)。"""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f


def _sgt_valid(value) -> float | None:
    """sgt 专用:在 _to_float 基础上再做量级容错(近期不可靠,可能 NaN/异常大)。"""
    f = _to_float(value)
    if f is None:
        return None
    if abs(f) > _SGT_MAX_ABS:
        return None
    return f


def _unwrap_payload(resp) -> dict:
    """实抓响应是 root-level {time, hgt, sgt},但保留对 {data:{...}} 包裹结构的兼容。
    识别规则:root dict 里只要含 'time'+'hgt'(或 'sgt')任一,就视为扁平结构直接返回 root;
    否则剥一层 `data` 再试一次;再剥不出就空 dict。
    """
    if not isinstance(resp, dict):
        return {}
    if "time" in resp and ("hgt" in resp or "sgt" in resp):
        return resp
    layer = resp.get("data")
    if not isinstance(layer, dict):
        return {}
    inner = layer.get("data")
    if isinstance(inner, dict):
        return inner
    return layer


def _series_last(series) -> float | None:
    """hexin 实抓响应中 hgt/sgt 是裸 float 数组(不是 [{time,value},...] 对象序列)。
    取末值,自动转 float + 量级容错(_sgt_valid 在外层做)。
    """
    if not isinstance(series, (list, tuple)) or not series:
        return None
    return _to_float(series[-1])


class HexinNorthboundVendor(_NorthboundVendorBase):
    """北向资金(同花顺 hexin):市场级,fetch 忽略 symbols。取当日分钟序列末值组装 1 条。"""

    name = "ths"
    supports_markets = {"CN"}

    def fetch(self, symbols: list[Symbol], config: dict) -> list[NorthboundItem]:
        data = market_get(
            _HEXIN_URL,
            host_key=_HEXIN_HOST,
            headers=_HEADERS,
            parse="json",
            retries=2,
            timeout=8,
            log_label="北向资金",
        )
        if not data:
            return []

        payload = _unwrap_payload(data)
        if not payload:
            return []

        time_series = payload.get("time") or []
        hgt_series = payload.get("hgt") or []
        sgt_series = payload.get("sgt") or []

        # hgt 末值(全天 262 个点是稳定的,直接用)
        hgt_net = _to_float(hgt_series[-1]) if hgt_series else None

        # sgt 容错:长度 < 0.25 * len(time) 视为断流(实测断在 09:44,长度 35)→ sgt_net=None
        # 单值 abs>2000 视为脏值
        sgt_net = None
        if sgt_series and time_series and len(sgt_series) >= 0.25 * len(time_series):
            candidate = _to_float(sgt_series[-1])
            if candidate is not None and abs(candidate) <= _SGT_MAX_ABS:
                sgt_net = candidate

        if hgt_net is None and sgt_net is None:
            return []

        # total_net:两端都有才求和(缺一端时 None,避免误导)
        total_net = None
        if hgt_net is not None and sgt_net is not None:
            total_net = hgt_net + sgt_net

        # date:响应里没有 date 字段,走 config['date'] 或今天
        from datetime import datetime
        date = str((config or {}).get("date") or datetime.now().strftime("%Y-%m-%d"))
        time_str = str(time_series[-1]) if time_series else ""

        return [
            NorthboundItem(
                date=date,
                hgt_net=hgt_net,
                sgt_net=sgt_net,
                total_net=total_net,
                time=time_str,
            )
        ]
