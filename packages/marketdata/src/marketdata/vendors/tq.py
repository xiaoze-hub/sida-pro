"""通达信TQ行情 vendor(本机网关 http://127.0.0.1:5100, JSON-RPC)。

链路: PanWatch(容器, host网络可达宿主127.0.0.1:5100) → frps(云7100/5100)
      → 小主机frpc → 通达信客户端自带TQ HTTP服务(127.0.0.1:17709)。

实测延迟(上海生产机): 快照/扩展指标 ~27-30ms, K线(10只×250日) ~48ms,
并发10路单次中位67ms — 全部远优于腾讯/东财 HTTP 爬源。
仅 CN 市场可用; 客户端未开时接口连接失败 → Engine 自动降级下一源。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from marketdata.symbol import Market, Symbol
from marketdata.types import Bar, MoreInfo, Quote
from marketdata.vendors.base import KlineVendor, MoreInfoVendor, QuoteVendor

logger = logging.getLogger(__name__)

_TIMEOUT_S = 4.0  # 正常 <100ms; 隧道断开时快速失败交给降级链

# ---------------------------------------------------------------------------
# TQ 陈旧快照防护 (2026-09-04, 09-03 漏数事故)
#
# 事故: TdxW.exe 未更新时, TQ 网关返回 09-02 快照却报成功 → Engine 视为
# 成功不再 failover, 全站停在前天。快照(get_market_snapshot)无日期字段,
# 无法自判; 但 K线(get_market_data)带日期, 在此做新鲜度门禁:
# 最新 bar 日期 < (今天-1天) → 视为陈旧, 返回 [] 触发 Engine 降级下一源。
# 阈值取 today-1(而非 today): 盘前/周末/节假日允许差一天, 误杀只会多走
# 一次腾讯(正确数据), 不会丢数; 陈旧 TQ 排后仍可当最后兜底(由 DB priority 定)。
# ---------------------------------------------------------------------------

def _norm_day(s: object) -> str:
    return str(s).replace("-", "")[:8]


def tq_bars_fresh(dates: list | None) -> bool:
    """TQ K线日期是否新鲜(纯函数, 可单测)。空列表视为不新鲜。"""
    norm = [_norm_day(d) for d in (dates or []) if str(d).strip()]
    if not norm:
        return False
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    floor = (today - timedelta(days=1)).strftime("%Y%m%d")
    return max(norm) >= floor

# ---------------------------------------------------------------------------
# TQ 网关地址**自动发现**(2026-09-02)
#
# 背景: 长期以来生产 TQ 一直报 Connection refused, 实际是地址没配对 ——
#   代码默认 `http://172.18.0.1:5100/`(容器网桥 / 旧 frps 隧道),
#   而本机部署(Win11 + WSL2 docker)通的是 **宿主 WSL 网卡 172.27.16.1:17709**
#   (通达信 TdxW.exe 监听, 实测容器内可达, p50 19ms)。
# 运维很难记住配 TDX_QUANT_URL, 故改为**按环境自适应探测**:
#   1) 显式环境变量 TDX_QUANT_URL 优先(保持既有部署兼容)
#   2) 否则按候选列表探测(默认网关 → WSL/Docker 常见网段 → 回环), 命中即缓存
# 探测只在进程内做一次(成本 ~几十 ms), 失败保持旧默认, 行为不变。
# ---------------------------------------------------------------------------
_TQ_URL_CACHE: str | None = None
_FALLBACK_URL = "http://172.18.0.1:5100/"


def _host_gateway() -> str | None:
    """读 /proc/net/route 取默认网关(容器内即宿主地址)。失败返回 None。"""
    try:
        with open("/proc/net/route", encoding="utf-8") as f:
            for line in f.read().splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 3 and parts[1] == "00000000":  # 目的地址全 0 = 默认路由
                    ip = int(parts[2], 16)
                    return "%d.%d.%d.%d" % (ip & 255, (ip >> 8) & 255,
                                            (ip >> 16) & 255, (ip >> 24) & 255)
    except Exception:  # noqa: BLE001
        return None
    return None


def _probe_tq(url: str, timeout: float = 1.5) -> bool:
    """最轻探测: get_stock_list 能回 result 即视为可用。"""
    body = json.dumps(
        {"id": 1, "method": "get_stock_list", "params": {"market": "5", "list_type": 0}}
    ).encode("utf-8")
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, content=body,
                               headers={"Content-Type": "application/json; charset=utf-8"})
            if resp.status_code != 200:
                return False
            data = json.loads(resp.content.decode("utf-8"))
        return bool((data.get("result") or {}).get("Value")) or "result" in data
    except Exception:  # noqa: BLE001
        return False


def _resolve_tq_url() -> str:
    """解析可用的 TQ 网关地址(一次探测, 结果缓存)。"""
    global _TQ_URL_CACHE
    if _TQ_URL_CACHE:
        return _TQ_URL_CACHE

    env_url = (os.environ.get("TDX_QUANT_URL") or "").strip()
    gw = _host_gateway()
    candidates: list[str] = []
    if env_url:
        candidates.append(env_url.rstrip("/") + "/")
    for host in [gw, "172.27.16.1", "172.28.0.1", "172.17.0.1", "172.18.0.1", "127.0.0.1"]:
        if not host:
            continue
        for port in (17709, 5100):
            u = f"http://{host}:{port}/"
            if u not in candidates:
                candidates.append(u)

    for u in candidates:
        if _probe_tq(u):
            _TQ_URL_CACHE = u
            logger.info("TQ 网关自动命中: %s", u)
            return u

    _TQ_URL_CACHE = candidates[0] if candidates else _FALLBACK_URL
    logger.warning("TQ 网关探测全部失败, 沿用默认 %s(将降级其他数据源)", _TQ_URL_CACHE)
    return _TQ_URL_CACHE


def _rpc(method: str, params: dict, timeout: float = _TIMEOUT_S):
    """发 JSON-RPC; 返回 result.Value 或抛异常(Engine 捕获后转下一源)。"""
    body = json.dumps({"id": 1, "method": method, "params": params}, ensure_ascii=False).encode("utf-8")
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(_resolve_tq_url(), content=body,
                          headers={"Content-Type": "application/json; charset=utf-8"})
        resp.raise_for_status()
        data = json.loads(resp.content.decode("utf-8"))
    if "error" in data:
        raise RuntimeError(f"TQ rpc error: {data['error']}")
    result = data.get("result") or {}
    # 快照类直接平铺在 result 里(ErrorId 字段共存); 列表/K线在 result.Value
    err = str(result.get("ErrorId", "0"))
    if err not in ("0", "") and "Value" in result or (err not in ("0", "") and "Value" not in result):
        raise RuntimeError(f"TQ {method} ErrorId={err}: {result.get('Error', '')}")
    return result.get("Value", result)


def _to_float(v) -> float | None:
    try:
        f = float(str(v).strip())
        return f
    except Exception:  # noqa: BLE001
        return None


def _to_int(v) -> int | None:
    try:
        return int(float(str(v).strip()))
    except Exception:  # noqa: BLE001
        return None


def to_tq_code(sym: Symbol) -> str | None:
    """CN 代码 → TQ 格式(600519.SH / 000001.SZ / 430047.BJ); 非 CN 返回 None。"""
    code = sym.code.strip()
    if sym.market != Market.CN or len(code) != 6 or not code.isdigit():
        return None
    if code.startswith(("6", "9", "5")):
        return f"{code}.SH"
    if code.startswith(("4", "8", "92")):
        return f"{code}.BJ"
    return f"{code}.SZ"


def _parse_more_info(symbol: str, raw: dict) -> MoreInfo:
    """将 get_more_info 原始 dict 解析为 MoreInfo 强类型 + raw 透传。"""
    return MoreInfo(
        symbol=symbol,
        market="CN",
        turnover_rate=_to_float(raw.get("fHSL")),
        volume_ratio=_to_float(raw.get("fLianB")),
        commission_ratio=_to_float(raw.get("Wtb")),
        total_market_value=_to_float(raw.get("Zsz")),
        circulating_market_value=_to_float(raw.get("Ltsz")),
        change_pct=_to_float(raw.get("ZAF")),
        change_pct_5d=_to_float(raw.get("ZAFPre5")),
        change_pct_20d=_to_float(raw.get("ZAFPre20")),
        change_pct_ytd=_to_float(raw.get("ZAFYear")),
        limit_up_amount=_to_float(raw.get("FCAmo")),
        limit_up_ratio=_to_float(raw.get("FCb")),
        open_amount=_to_float(raw.get("OpenAmo")),
        open_limit_buy=_to_float(raw.get("OpenZTBuy")),
        consecutive_limit_days=_to_int(raw.get("EverZTCount")),
        consecutive_up_days=_to_int(raw.get("ConZAFDateNum")),
        pe_dynamic=_to_float(raw.get("DynaPE")),
        pe_ttm=_to_float(raw.get("StaticPE_TTM")),
        pb=_to_float(raw.get("PB_MRQ")),
        dividend_yield=_to_float(raw.get("DYRatio")),
        beta=_to_float(raw.get("BetaValue")),
        ma5_price=_to_float(raw.get("MA5Value")),
        high_52w=_to_float(raw.get("HisHigh")),
        low_52w=_to_float(raw.get("HisLow")),
        l2_tick_num=_to_int(raw.get("L2TicNum")),
        l2_order_num=_to_int(raw.get("L2OrderNum")),
        total_buy_vol=_to_float(raw.get("TotalBVol")),
        total_sell_vol=_to_float(raw.get("TotalSVol")),
        cancel_buy=_to_float(raw.get("BCancel")),
        cancel_sell=_to_float(raw.get("SCancel")),
        zjl=_to_float(raw.get("Zjl")),
        zjl_hb=_to_float(raw.get("Zjl_HB")),
        raw=dict(raw),
        quote_time=datetime.now(ZoneInfo("Asia/Shanghai")),
    )


class TqQuoteVendor(QuoteVendor):
    name = "tq"
    supports_markets = {"CN"}

    def fetch(self, symbols: list[Symbol], config: dict) -> list[Quote]:
        out: list[Quote] = []
        for sym in symbols:
            tqc = to_tq_code(sym)
            if not tqc:
                continue
            v = _rpc("get_market_snapshot", {"stock_code": tqc})
            if not isinstance(v, dict) or not v:
                continue
            now = _to_float(v.get("Now"))
            if now is None:
                continue
            last_close = _to_float(v.get("LastClose")) or 0.0
            change_amount = round(now - last_close, 4) if last_close else None
            change_pct = round((now - last_close) / last_close * 100, 4) if last_close else None
            inside = _to_float(v.get("Inside"))
            outside = _to_float(v.get("Outside"))
            # 尝试合并 more_info 丰富 quote（失败不阻塞，单 RPC 降级）
            turnover_rate = None
            volume_ratio = None
            pe_ratio = None
            pb_ratio = None
            circ_mv = None
            total_mv = None
            try:
                mi = _rpc("get_more_info", {"stock_code": tqc})
                if isinstance(mi, dict) and mi.get("ErrorId") in ("0", None, ""):
                    turnover_rate = _to_float(mi.get("fHSL"))
                    volume_ratio = _to_float(mi.get("fLianB"))
                    # 优先 DynaPE，否则 MorePE/StaticPE_TTM
                    pe_ratio = _to_float(mi.get("DynaPE")) or _to_float(mi.get("MorePE")) or _to_float(mi.get("StaticPE_TTM"))
                    pb_ratio = _to_float(mi.get("PB_MRQ"))
                    circ_mv = _to_float(mi.get("Ltsz"))
                    total_mv = _to_float(mi.get("Zsz"))
            except Exception:  # noqa: BLE001
                pass
            out.append(
                Quote(
                    symbol=sym.code,
                    market="CN",
                    name="",  # TQ快照不带名称, 上层已有名称映射; 不猜名
                    current_price=now,
                    prev_close=last_close or None,
                    open_price=_to_float(v.get("Open")),
                    high_price=_to_float(v.get("Max")),
                    low_price=_to_float(v.get("Min")),
                    change_amount=change_amount,
                    change_pct=change_pct,
                    volume=_to_float(v.get("Volume")),
                    turnover=_to_float(v.get("Amount")),
                    turnover_rate=turnover_rate,
                    volume_ratio=volume_ratio,
                    volume_inner=(inside if inside is not None else None),
                    volume_outer=(outside if outside is not None else None),
                    pe_ratio=pe_ratio,
                    pb_ratio=pb_ratio,
                    circulating_market_value=circ_mv,
                    total_market_value=total_mv,
                    quote_time=datetime.now(ZoneInfo("Asia/Shanghai")),
                )
            )
        return out


class TqMoreInfoVendor(MoreInfoVendor):
    """TQ 扩展指标 vendor - 直接透传 get_more_info 104字段。"""

    name = "tq"
    supports_markets = {"CN"}

    def fetch(self, symbols: list[Symbol], config: dict) -> list[MoreInfo]:
        out: list[MoreInfo] = []
        for sym in symbols:
            tqc = to_tq_code(sym)
            if not tqc:
                continue
            try:
                raw = _rpc("get_more_info", {"stock_code": tqc})
            except Exception as e:  # noqa: BLE001
                logger.warning("TQ get_more_info %s failed: %s", tqc, e)
                continue
            if not isinstance(raw, dict) or raw.get("ErrorId") not in ("0", None, ""):
                # TQ 返回 ErrorId 非0 时 raw 可能含 Error 字段，直接跳过
                if isinstance(raw, dict) and raw.get("ErrorId") not in ("0", None, "", 0):
                    logger.warning("TQ get_more_info %s ErrorId=%s", tqc, raw.get("ErrorId"))
                    continue
            # 成功：raw 本身就是 104字段平铺 dict
            out.append(_parse_more_info(sym.code, raw))
        return out


class TqKlineVendor(KlineVendor):
    name = "tq"
    supports_markets = {"CN"}

    def fetch(self, symbols: list[Symbol], config: dict) -> list[Bar]:
        if not symbols:
            return []
        sym = symbols[0]
        tqc = to_tq_code(sym)
        if not tqc:
            return []
        try:
            days = int(config.get("days") or 120)
        except Exception:  # noqa: BLE001
            days = 120
        days = min(max(days, 1), 800)
        # TQ 冷缓存只返回最新1根 → 先刷新K线缓存(实测刷新后 count 生效)
        try:
            _rpc("refresh_kline", {"stock_list": [tqc], "period": "1d"}, timeout=_TIMEOUT_S)
        except Exception:  # noqa: BLE001  刷新失败不阻塞, 直接尝试取数
            pass
        v = _rpc(
            "get_market_data",
            {
                "stock_list": [tqc],
                "period": "1d",
                "count": days,
                "dividend_type": "front",
            },
            timeout=max(_TIMEOUT_S, 15.0),
        )
        rows = (v or {}).get(tqc) if isinstance(v, dict) else None
        if not rows:
            return []
        dates = rows.get("Date") or []
        opens = rows.get("Open") or []
        closes = rows.get("Close") or []
        highs = rows.get("High") or []
        lows = rows.get("Low") or []
        volumes = rows.get("Volume") or []
        out: list[Bar] = []
        for i, d in enumerate(dates):
            try:
                out.append(
                    Bar(
                        date=str(d),
                        open=float(opens[i]),
                        close=float(closes[i]),
                        high=float(highs[i]),
                        low=float(lows[i]),
                        volume=float(volumes[i]) if i < len(volumes) else 0.0,
                    )
                )
            except Exception:  # noqa: BLE001
                continue
        if out and not tq_bars_fresh([b.date for b in out]):
            # 陈旧快照(见模块头注释): 当失败处理, Engine 自动降级下一源
            logger.warning("[tq] K线陈旧(最新 %s), 触发降级", out[-1].date)
            return []
        return out


def formula_mul(
    formula_name: str,
    stock_list: list[str],
    *,
    formula_arg: str = "",
    stock_period: str = "1d",
    count: int = -1,
    return_count: int = 1,
    dividend_type: int = 0,
    xsflag: int = -1,
) -> dict:
    """批量执行通达信指标公式(formula_process_mul_zb), 返回 {代码: {指标名: [值...]}}。

    周期参数 stock_period + periodstr 必须同传(TQ 17709 特有: 缺 periodstr 报
    "periodstr error"; 缺 stock_period 报 "formula data counts < 1")。
    常用内置公式: MACD / ZLJC(主力进出: JCL/JCM/JCS 三档净量)。
    L2_AMO 是公式函数(非独立公式), 需在客户端"公式管理器"自定义指标公式后按名调用。
    """
    params = {
        "formula_name": formula_name,
        "formula_arg": formula_arg,
        "stock_list": stock_list,
        "stock_period": stock_period,
        "periodstr": stock_period,
        "count": count,
        "return_count": return_count,
        "dividend_type": dividend_type,
        "xsflag": xsflag,
    }
    v = _rpc("formula_process_mul_zb", params, timeout=max(_TIMEOUT_S, 60.0))
    if not isinstance(v, dict):
        return {}
    v.pop("ErrorId", None)
    v.pop("Error", None)
    return v


def formula_zb_single(
    formula_name: str,
    stock_code: str,
    *,
    formula_arg: str = "",
    xsflag: int = -1,
) -> dict:
    """单只指标公式(依赖客户端当前打开的数据, 盘中实时单只场景)。"""
    v = _rpc("formula_zb", {
        "formula_name": formula_name,
        "formula_arg": formula_arg,
        "stock_code": stock_code,
        "xsflag": xsflag,
    }, timeout=max(_TIMEOUT_S, 60.0))
    if not isinstance(v, dict):
        return {}
    v.pop("ErrorId", None)
    v.pop("Error", None)
    return v
