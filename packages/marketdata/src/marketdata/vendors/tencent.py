"""腾讯行情 vendor(HTTP,GBK)。移植自 akshare_collector._parse_tencent_line/_fetch_tencent_quotes。

2026-09-08 (风险方案1.1) 字段缺失语义:
- 解析层对缺失/空字段一律保留 None, 绝不回退 0(0 价参与涨跌幅算术会伪造 -100% 假暴跌)。
- Quote.status: ok=核心字段齐 / partial=部分缺失 / missing=无有效价格字段。
- Quote.missing_fields: 记录缺失字段名, 供下游显式标注「无数据」。

turnover 单位实测(2026-09-08, qt.gtimg.cn 真实报文, 收盘后):
  sh600519 贵州茅台: parts[35]="1309.30/17534/2302823753", 恒等式
    amt / (price × vol(手) × 100) = 2302823753 / (1309.30×17534×100) = 1.0031 ≈ 1
  sh601318 中国平安: 3930272360 / (55.70×700768×100) = 1.0069 ≈ 1
  (偏差 <0.7% 即 VWAP≠收盘价的正常范围) → parts[35] 第三段成交额单位 = 元。
  交叉印证: parts[37] = amt/10000 (230282 vs 2302823753, 比值 10000.02) = 万元口径。
  恒等式约定见 docs/_frozen/data.md。

parts[30] 语义实测(2026-10-29 19:20, 收盘后同一批报文, 这批证据决定 UI 文案):
  sh600519 正常交易 vol=42992 手   parts[30]='20261029161457'
  sh601326 今日停牌 vol=0          parts[30]='20261029161445'
  sz002827 今日停牌 vol=0          parts[30]='20261029161430'
  ⇒ ① CN 收盘 15:00 之后仍报 16:14:xx(那是源端整理/刷新时点, 不是最后一笔成交);
    ② 停牌股今日没有成交, parts[30] 照样刷成当日 16:14。
  ⇒ **它只能证明"报文在这个时刻被刷新过"**, 不能证明"这个时刻发生过成交",
    更不能用来否定停牌。`Quote.quote_time_kind` 因此标 'snapshot_refresh',
    UI 必须写"源快照时间", 不许写"成交时间"。
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from marketdata.http import market_get
from marketdata.symbol import Symbol
from marketdata.types import Quote
from marketdata.vendors.base import QuoteVendor

logger = logging.getLogger(__name__)

_URL = "http://qt.gtimg.cn/q="
_HOST = "qt.gtimg.cn"
_MIN_INTERVAL_S = 0.15

# 腾讯行情接口的成交量字段是**手**（A 股 1 手 = 100 股），而本项目的 volume 口径统一为**股**
# （`vol × price == turnover`，见 AGENTS.md 业务硬约束）。2026-11-01 实测 600111：
# 接口 volume=301798，成交额 10.43 亿，股价 34.21 → 301798×100×34.21≈10.32 亿（股口径才对得上）。
# 港股/美股成交量本身就是股，不做换算。
_CN_SHARES_PER_LOT = 100
_MARKET_TIMEZONES = {
    "CN": "Asia/Shanghai",
    "HK": "Asia/Hong_Kong",
    "US": "America/New_York",
}

# 核心字段的 (parts 下标, 字段名)。缺失时进 missing_fields 并降级 status。
_CORE_FIELDS = (
    (3, "current_price"),
    (4, "prev_close"),
    (5, "open_price"),
    (6, "volume"),
    (7, "volume_outer"),
    (8, "volume_inner"),
    (31, "change_amount"),
    (32, "change_pct"),
    (33, "high_price"),
    (34, "low_price"),
)


def _to_shares(value: float | None, market: str) -> float | None:
    """腾讯给的成交量(手) → 本项目口径(股)。港股/美股本来就是股，原样返回。"""
    if value is None:
        return None
    if market == "CN":
        return value * _CN_SHARES_PER_LOT
    return value


def _to_float(value: str | None) -> float | None:
    if value is None:
        return None
    v = str(value).strip()
    if not v:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


#: 腾讯 parts[30] 实测格式（2026-10-29 实盘报文，按市场不同）：
#:   CN sh600519 = '20261029161457'（14 位紧凑）
#:   HK hk00700  = '2026/10/29 16:08:29'（斜杠）
#:   US usAAPL   = '2026-10-28 16:00:01'（横杠；**市场当地**时间，可能是上一交易日）
_QUOTE_TIME_FORMATS = (
    ("%Y%m%d%H%M%S", 14),
    ("%Y-%m-%d %H:%M:%S", 19),
    ("%Y/%m/%d %H:%M:%S", 19),
)


def _parse_quote_time(value: str | None, market: str) -> datetime | None:
    """解析行情源自带的报时（parts[30]），按**市场时区**贴 tzinfo；解析不出给 None。

    2026-10-29 F3: 旧实现只认 14 位紧凑格式 —— 港美报时是带分隔符的格式，于是
    `quote_time` 恒为 None，WS/防线对港美永远只能判"时刻未知"（明明源给了时刻）。
    这里按顺序试三种实测格式；都不匹配就返回 None（绝不猜、绝不用抓取时间顶替）。

    ⚠️ **语义不是"成交时刻"**（2026-10-29 实盘复测，见模块 docstring）：
    CN 收盘后 parts[30] 仍是 16:14:xx（15:00 收盘之后），且**停牌股**（今日成交量 0）
    的 parts[30] 同样刷新成 16:14:xx。⇒ 它是**源端快照刷新时间**，只证明"这条报文
    在这时被刷新过"，不证明"这时发生过成交"。调用方必须把它标成
    `Quote.quote_time_kind="snapshot_refresh"`，UI 也不许写成"成交时间"。
    """
    text = str(value or "").strip()
    if not text:
        return None
    parsed: datetime | None = None
    for fmt, width in _QUOTE_TIME_FORMATS:
        if len(text) < width:
            continue
        try:
            parsed = datetime.strptime(text[:width], fmt)
            break
        except (TypeError, ValueError):
            continue
    if parsed is None:
        return None
    timezone = _MARKET_TIMEZONES.get(market)
    return parsed.replace(tzinfo=ZoneInfo(timezone)) if timezone else parsed


#: parts[30] 的时刻语义（按市场分，2026-10-29 复审）：
#:   CN  = "snapshot_refresh"：**已证实是源端刷新时点**，不是成交时刻。
#:         证据(2026-10-29 19:20/19:49 收盘后, qt.gtimg.cn)：正常股 parts[30]=16:14:57、
#:         停牌股(601326/002827)同样刷成 16:14:xx，而 A 股 15:00 已收盘 ⇒ 只证明"报文被
#:         刷新过"，不证明"这时成交过"。
#:   港美 = "vendor_stamp"：**只到"供应商报时间"这一层，语义未核实**。
#:         2026-10-29 19:49 实盘：HK 8 只 16:08:24~16:08:29（落在收盘竞价随机收市窗口内）、
#:         US 15 只 2026-10-28 16:00:01（收盘竞价打印时点）、BRK.A 16:04:49（更晚）。
#:         这些时刻**可能就是最后一笔成交**，没有证据能否定 ⇒ 不许沿用 CN 那句更强的断言
#:         （"快照刷新/非成交"），只许说"源报时，不能证明成交"。
QUOTE_TIME_KIND_SNAPSHOT_REFRESH = "snapshot_refresh"
QUOTE_TIME_KIND_VENDOR_STAMP = "vendor_stamp"


def _quote_time_kind(market: str, quote_time: datetime | None) -> str:
    """报价时刻的语义标签；没有时刻就是空串（绝不半条证据）。"""
    if quote_time is None:
        return ""
    return QUOTE_TIME_KIND_SNAPSHOT_REFRESH if market == "CN" else QUOTE_TIME_KIND_VENDOR_STAMP


def _parse_line(line: str, market: str) -> Quote | None:
    if '=""' in line or not line.strip():
        return None
    try:
        _, value = line.split('="', 1)
        parts = value.rstrip('";').split("~")
        if len(parts) < 35:
            return None

        missing: list[str] = []
        values: dict[str, float | None] = {}
        for idx, field_name in _CORE_FIELDS:
            v = _to_float(parts[idx])
            values[field_name] = v
            if v is None:
                missing.append(field_name)

        # parts[35] = "最新价/成交量(手)/成交额(元)" 复合字段; 成交额单位实测=元(见模块 docstring)。
        turnover = None
        if "/" in str(parts[35]):
            tp = parts[35].split("/")
            if len(tp) >= 3:
                turnover = _to_float(tp[2])
                if turnover is None:
                    missing.append("turnover")

        symbol = parts[2]
        if "." in symbol and not symbol.startswith("."):
            symbol = symbol.split(".")[0]

        turnover_rate = _to_float(parts[38]) if len(parts) > 39 else None
        pe_ratio = _to_float(parts[39]) if len(parts) > 39 else None
        pb_ratio = _to_float(parts[46]) if len(parts) > 46 else None
        circulating = _to_float(parts[44]) if len(parts) > 45 else None
        total = _to_float(parts[45]) if len(parts) > 45 else None
        volume_ratio = _to_float(parts[49]) if len(parts) > 49 else None

        price_fields = ("current_price", "prev_close", "open_price", "high_price", "low_price")
        if all(values.get(f) is None for f in price_fields):
            status = "missing"
        elif missing:
            status = "partial"
        else:
            status = "ok"
        quote_time = _parse_quote_time(parts[30], market)

        return Quote(
            symbol=symbol,
            market=market,
            name=parts[1],
            current_price=values["current_price"],
            prev_close=values["prev_close"],
            open_price=values["open_price"],
            volume=_to_shares(values["volume"], market),
            volume_outer=_to_shares(values["volume_outer"], market),   # 外盘(主动买)
            volume_inner=_to_shares(values["volume_inner"], market),   # 内盘(主动卖)
            change_amount=values["change_amount"],
            change_pct=values["change_pct"],
            high_price=values["high_price"],
            low_price=values["low_price"],
            turnover=turnover,
            turnover_rate=turnover_rate,
            volume_ratio=volume_ratio,
            pe_ratio=pe_ratio,
            pb_ratio=pb_ratio,
            circulating_market_value=circulating,
            total_market_value=total,
            quote_time=quote_time,
            quote_time_kind=_quote_time_kind(market, quote_time),
            status=status,
            missing_fields=missing,
        )
    except (ValueError, IndexError) as e:
        logger.debug(f"解析腾讯行情失败: {e}")
        return None


def _fetch_lines(tencent_symbols: list[str]) -> list[str]:
    """按原始腾讯符号批量拉取响应,GBK 解码后按 ';' 切分为行。tencent quote / index 共用取数核。"""
    if not tencent_symbols:
        return []
    codes = ",".join(tencent_symbols)
    content = market_get(
        _URL + codes,
        host_key=_HOST,
        min_interval_s=_MIN_INTERVAL_S,
        timeout=10,
        retries=2,
        parse="content",
        log_label="腾讯报价",
    )
    if not content:
        return []
    text = content.decode("gbk", errors="ignore") if isinstance(content, (bytes, bytearray)) else str(content)
    return text.strip().split(";")


def fetch_raw(tencent_symbols: list[str]) -> list[dict]:
    """按原始腾讯符号(sh000001/hkHSI/usDJI…)取行情,不经 Symbol.parse。

    供指数等显式符号场景复用(指数代码与个股代码可能撞号,如 000001 既是平安银行又是上证指数)。
    返回 dict 列表:symbol/name/current_price/change_pct/change_amount/prev_close/volume/turnover。
    """
    out: list[dict] = []
    for line in _fetch_lines(tencent_symbols):
        q = _parse_line(line, "")
        # 无价 Quote 缺 price 无法构成报价, 由调用方按「无数据」处理; 绝不回退 0 参与下游算术。
        if q and q.current_price is not None and q.current_price > 0:
            out.append({
                "symbol": q.symbol,
                "name": q.name,
                "current_price": q.current_price,
                "change_pct": q.change_pct,
                "change_amount": q.change_amount,
                "prev_close": q.prev_close,
                "volume": q.volume,
                "turnover": q.turnover,
            })
    return out


class TencentQuoteVendor(QuoteVendor):
    name = "tencent"
    supports_markets = {"CN", "HK", "US"}

    def fetch(self, symbols: list[Symbol], config: dict) -> list[Quote]:
        if not symbols:
            return []
        market = symbols[0].market.value
        codes = [s.to_tencent() for s in symbols]
        out: list[Quote] = []
        for line in _fetch_lines(codes):
            q = _parse_line(line, market)
            # partial(价格在、个别字段缺)照常透传, status/missing_fields 随 Quote 下游标注;
            # 缺价(missing)不出现在结果里 = 调用方视角的「无数据」。
            if q and q.current_price is not None and q.current_price > 0:
                out.append(q)
        return out
