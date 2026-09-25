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
from marketdata.types import Bar, DividendItem, MoreInfo, Quote, ShareholderItem
from marketdata.vendors.base import (
    DividendVendor,
    KlineVendor,
    MoreInfoVendor,
    QuoteVendor,
    ShareholdersVendor,
)

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


def _fmt_day(s: object) -> str:
    """TQ 日期 → 东财风格 'YYYY-MM-DD'(跨源同格式, 否则 API 里 ex_date 排序会混)。

    TQ 回 '20260630'; 东财路径产出 '2026-06-30'。两源混排时纯数字串会**全部
    排在带横线串之后**, 前端展示顺序就乱了。
    """
    raw = str(s or "").strip()
    t = raw[:10].replace("/", "-")
    if "-" in t:
        return t
    if len(t) >= 8 and t[:8].isdigit():
        return f"{t[:4]}-{t[4:6]}-{t[6:8]}"
    return raw


def tq_bars_fresh(dates: list | None) -> bool:
    """TQ K线日期是否新鲜(纯函数, 可单测)。空列表视为不新鲜。

    2026-09-06 28号: floor 由 today-1 放宽到 today-3 —— 周日跑批时 Friday
    数据距 today-1(周六) 还差一天, 会被误判陈旧导致全量降级(实测: 妖股池
    回填全市场时 TQ 主链路全灭)。3 天覆盖周末+1天假期; 更长假期靠备选源。
    """
    norm = [_norm_day(d) for d in (dates or []) if str(d).strip()]
    if not norm:
        return False
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    floor = (today - timedelta(days=3)).strftime("%Y%m%d")
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
# P2-18: 失败缓存 5min 后重探(网关重启可恢复); 成功缓存永久
_TQ_URL_CACHE_OK = False
_TQ_URL_CACHE_TS = 0.0
_TQ_FAIL_TTL = 300.0
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
    """解析可用的 TQ 网关地址(成功缓存永久, 失败缓存 5min 后重探)。"""
    import time as _time

    global _TQ_URL_CACHE, _TQ_URL_CACHE_OK, _TQ_URL_CACHE_TS
    if _TQ_URL_CACHE and (_TQ_URL_CACHE_OK or _time.time() - _TQ_URL_CACHE_TS < _TQ_FAIL_TTL):
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
            _TQ_URL_CACHE_OK = True
            _TQ_URL_CACHE_TS = _time.time()
            logger.info("TQ 网关自动命中: %s", u)
            return u

    _TQ_URL_CACHE = candidates[0] if candidates else _FALLBACK_URL
    _TQ_URL_CACHE_OK = False
    _TQ_URL_CACHE_TS = _time.time()
    logger.warning("TQ 网关探测全部失败, 沿用默认 %s(将降级其他数据源)", _TQ_URL_CACHE)
    return _TQ_URL_CACHE


def _rpc(method: str, params: dict, timeout: float = _TIMEOUT_S, *, full: bool = False):
    """发 JSON-RPC; 返回 result.Value 或抛异常(Engine 捕获后转下一源)。

    full=True → 返回整个 result。给 get_divid_factors 这类**多数组并列**的响应
    用: 它的 Date/Type/Value 是三个平行数组靠下标对齐, 只取 Value 会把除权日期
    整段丢掉(实测网关**有**回 Date, 是这里被丢的)。
    """
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
    return result if full else result.get("Value", result)


def tq_rpc(method: str, params: dict, timeout: float = _TIMEOUT_S):
    """公开 TQ JSON-RPC 入口(封单成色采样/妖股池等核心模块复用)。

    核心模块要调 get_stock_info/get_zdt_data 等未封装方法, 之前只能 import
    私有 _rpc; 这里给一个稳定公开入口, 参数语义与 _rpc 一致。
    """
    return _rpc(method, params, timeout=timeout)


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
    # P2-17 (2026-09-05 28号审计): 92 前缀优先判 BJ(920xxx 会先命中下面的 "9"→SH)
    if code.startswith(("92", "4", "8")):
        return f"{code}.BJ"
    if code.startswith(("6", "9", "5")):
        return f"{code}.SH"
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
            # P2-18: 非 dict 直接跳过(旧代码会掉进 _parse_more_info 炸批)
            if not isinstance(raw, dict):
                logger.warning("TQ get_more_info %s 非 dict 响应, 跳过", tqc)
                continue
            if raw.get("ErrorId") not in ("0", None, "", 0):
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


# ---------------------------------------------------------------------------
# 股东户数 / 分红 (2026-09-24)
#
# 起因: 这两个能力在生产**恒为空** —— 智兔 429, 而东财侧 filter 拿的是带交易所
# 后缀的 code(SECURITY_CODE="002361.SZ" 恒 0 条; 裸码 3 条), 后缀源于
# Symbol.parse 不归一化(已修)。TQ 侧数据实测可用且与东财逐笔对齐 → 接为备源。
# 定位: 东财(主源, 字段更全: 送/转可分开 + 方案进度) → TQ(备源) → 智兔(付费)。
# ---------------------------------------------------------------------------

_GP01_WINDOW_DAYS = 500  # 覆盖 ≥4 个报告期, 供"较上期"户数环比计算


def _gp01_series(tqc: str) -> list[tuple[str, int]]:
    """GP01(股东人数) 序列 → [(报告期, 户数)] 按日期升序。"""
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    data = gp_series(
        ["GP01"],
        tqc,
        start_time=(now - timedelta(days=_GP01_WINDOW_DAYS)).strftime("%Y%m%d"),
        end_time=now.strftime("%Y%m%d"),
    )
    rows: list[tuple[str, int]] = []
    for rec in (data or {}).get("GP01") or []:
        if not isinstance(rec, dict):
            continue
        vals = rec.get("Value")
        if not isinstance(vals, (list, tuple)) or not vals:
            continue
        num = _to_int(vals[0])
        day = _fmt_day(rec.get("Date"))
        if num is None or not day:
            continue
        rows.append((day, num))
    rows.sort(key=lambda r: r[0])
    return rows


class TqShareholdersVendor(ShareholdersVendor):
    """股东户数: GP01(股东人数, 季频)取最新一期, 环比由前一期现算。

    实测标定: 002361.SZ 2026-06-30 GP01 = 264938 户, 与东财
    RPT_HOLDERNUMLATEST 的 HOLDER_NUM **完全一致**(独立源交叉验证)。
    ⚠️ GP01 只给户数, 没有户均持股 → avg_shares 留 None(不拿流通股本现推,
    避免与东财 AVG_FREE_SHARES 口径混)。
    """

    name = "tq"
    supports_markets = {"CN"}

    def fetch(self, symbols: list[Symbol], config: dict) -> list[ShareholderItem]:
        out: list[ShareholderItem] = []
        for sym in symbols:
            tqc = to_tq_code(sym)
            if not tqc:
                continue
            try:
                rows = _gp01_series(tqc)
            except Exception as e:  # noqa: BLE001
                logger.warning("TQ GP01 %s failed: %s", tqc, e)
                continue
            if not rows:
                continue
            cur_date, cur_num = rows[-1]
            change_num: int | None = None
            change_ratio: float | None = None
            if len(rows) >= 2:
                prev_num = rows[-2][1]
                if prev_num:
                    change_num = cur_num - prev_num
                    change_ratio = round(change_num / prev_num * 100, 2)
            out.append(
                ShareholderItem(
                    report_date=cur_date,
                    symbol=sym.code,
                    holder_num=cur_num,
                    change_num=change_num,
                    change_ratio=change_ratio,
                )
            )
        return out


class TqDividendVendor(DividendVendor):
    """分红: get_divid_factors 带除权除息日的历史(与东财同契约)。

    实测标定: 002361.SZ 13 笔与东财 RPT_SHAREBONUS_DET 13 笔**同日对齐**。
    ⚠️ 与东财的两处口径差(证据见 divid_factors_rows):
      · TQ bonus 是每10股口径 → dividend_per_share = bonus/10
      · TQ share_bonus 是**送股+转增合计**, 不分送/转 → 记入 bonus_ratio,
        transfer_ratio 留 None(前端文案是"每10股转增X 每10股送股Y", 分开显示,
        所以送转合并只在备源生效, 主源东财不受影响)。
    progress 留空: TQ 只给已除权历史, 无方案进度字段, 不臆造。
    """

    name = "tq"
    supports_markets = {"CN"}

    def fetch(self, symbols: list[Symbol], config: dict) -> list[DividendItem]:
        out: list[DividendItem] = []
        for sym in symbols:
            tqc = to_tq_code(sym)
            if not tqc:
                continue
            try:
                rows = divid_factors_rows(tqc)
            except Exception as e:  # noqa: BLE001
                logger.warning("TQ get_divid_factors %s failed: %s", tqc, e)
                continue
            for r in rows:
                bonus = r.get("bonus")
                share_bonus = r.get("share_bonus")
                out.append(
                    DividendItem(
                        ex_date=r.get("date") or "",
                        symbol=sym.code,
                        dividend_per_share=round(bonus / 10, 4) if bonus else None,
                        transfer_ratio=None,
                        bonus_ratio=share_bonus if share_bonus else None,
                        progress="",
                    )
                )
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



# ============================================================================
# 专业数据序列 / 扩展接口 (2026-09-23 全量盘点后补齐)
#
# ⚠️ 网关参数契约与官方 SDK 不同(实测自曝, 写错时服务端回显 "json has no table_list"):
#     个股 get_gpjy_value    : {"code": "002361.SZ", "table_list": ["GP2"],  "start_time": "20240101"}
#     市场 get_scjy_value    : {"code": "999999.SH", "table_list": ["SC3"],  "start_time": "20260101"}
#     板块 get_bkjy_value    : {"code": "880201.SH", "table_list": ["BK1"],  "start_time": "20260101"}
#     单个 get_gp_one_data   : {"code": "...",       "table_list": ["GO47"]}
#     财务 get_financial_data: {"code": "...",       "table_list": ["Fn193"], "start_time": "..."}
#   即: 单数 code + table_list。官方 SDK 用 stock_list + field_list 是其内部映射, 网关不认。
#
# ⚠️ 命名的坑: T0002\bigdata_all.txt 那 404 个码(LHBD1/DZJY1/GDRS/JGDY...)
#   是客户端「大数据」UI 模块的字段名, 不是 GP/SC 表名, 喂进来只会拿到 null。
#
# 编号空间实测(002361.SZ / 2024-01-01~):
#   GP 有数据 38 个: GP01-03,06,08-22,24,25,27,30-34,36-40,42,44,47-50
#   SC 有数据 33 个: SC01-19(缺09,20), SC21-35
# ============================================================================

# GPJYVALUE 官方编号(个股交易数据); 每项 = (编号, 含义, 单位)
GP_TABLES: dict[str, str] = {
    "GP1": "股东户数(户)",
    "GP2": "龙虎榜 买入总计/卖出总计(万元)",
    "GP3": "融资融券1 融资余额(万元)/融券余量(股)",
    "GP4": "大宗交易 成交均价(元)/成交额(万元)",
    "GP5": "增减持 成交均价(元)/变动股数(股)",
    "GP6": "陆股通持股量(股)",
    "GP7": "陆股通市场成交净额(万元)",
    "GP8": "龙虎榜机构(卖方) 机构个数/卖出金额(万元)",
    "GP9": "龙虎榜机构(买方) 机构个数/买入金额(万元)",
    "GP10": "近3月机构调研 调研次数/调研机构数量",
    "GP11": "融资融券2 融资买入额(万元)/融资偿还额(万元)",
    "GP12": "融资融券3 融券卖出量(股)/融券偿还量(股)",
    "GP13": "融资融券4 融资净买入(万元)/融券净卖出(股)",
    "GP15": "涨跌停 状态/封单金额(万元) 2=涨停 1=曾涨停 -2=跌停 -1=曾跌停",
    "GP16": "总市值(万元)",
    "GP17": "龙虎榜营业部数据 买入金额/卖出金额(万元)",
    "GP18": "龙虎榜沪深股通数据 买入金额/卖出金额(万元)",
    "GP19": "每周股票质押数量 无限售(万)/有限售(万元)",
    "GP20": "每周股票质押比例(%)",
    "GP24": "涨停时间/封单额(仅涨停日有值)",
}

# SCJYVALUE 官方编号(市场级交易数据)
SC_TABLES: dict[str, str] = {
    "SC1": "沪深融资余额/融券余额(万元)",
    "SC2": "陆股通资金流入 沪股通/深股通(亿元)",
    "SC3": "沪深涨停股个数 涨停/曾涨停(炸板)",
    "SC4": "沪深跌停股个数 跌停/曾跌停",
    "SC5": "上证50股指期货净持仓(手)",
    "SC6": "沪深300股指期货净持仓(手)",
    "SC7": "中证500股指期货净持仓(手)",
    "SC8": "ETF基金规模(亿)/净申赎(亿)",
    "SC9": "每月新增投资者数量(户)",
    "SC10": "增减持统计 增持额/减持额(万元)",
    "SC11": "大宗交易 溢价额/折价额(万元)",
    "SC12": "限售解禁 计划额/实际上市额(亿元)",
    "SC13": "市场总分红额(亿元)",
    "SC14": "市场总募资额(亿元)",
    "SC15": "打板资金 封板成功/封板失败(亿元)",
    "SC16": "龙虎榜 买入总金额/卖出总金额(亿元)",
    "SC17": "龙虎榜机构数据 买入/卖出(亿元)",
    "SC18": "龙虎榜营业部数据 买入/卖出(亿元)",
    "SC19": "龙虎榜沪深股通数据 买入/卖出(亿元)",
    "SC20": "陆股通净买入 买入/卖出(亿元)",
    "SC21": "每周无限售质押率 深市/沪市(%)",
    "SC22": "每周有限售质押率 深市/沪市(%)",
    "SC23": "连板家数 含ST及未开板新股/不含",
    "SC24": "沪深涨跌停股个数 涨停(不含ST及新股)/跌停(不含ST)",
    "SC25": "沪深融资买入额(万元)/融券卖出量(万股)",
    "SC26": "每周市场质押比例(%)",
    "SC27": "央行公开市场净投放(亿元)",
    "SC28": "扩展序列(官方未列, 待标定)",
    "SC29": "扩展序列(官方未列, 待标定)",
    "SC30": "扩展序列(官方未列, 待标定)",
    "SC31": "扩展序列(官方未列, 待标定)",
    "SC32": "扩展序列(官方未列, 待标定)",
    "SC33": "扩展序列(官方未列, 待标定)",
    "SC34": "扩展序列(官方未列, 待标定)",
    "SC35": "扩展序列(官方未列, 待标定)",
}

_SC_FALLBACK_CODE = "999999.SH"  # 市场级序列的默认标的(上证指数)


def _pro_series(method: str, table_list: list[str], *, code: str = "",
                start_time: str = "", end_time: str = "", timeout: float = 30.0) -> dict:
    """通用专业序列取数。返回 {表名: [{Date, Value:[...]}, ...]}; 空表不出现在结果里。

    ⚠️ 网关参数契约是**递进必填**的, 少一个就报 `ErrorId=10 json has no XXX`
    (服务端会把收到的 params 原样回显, 照着补):
        {"table_list": [...]}                          → "json has no table_list"? 不, 先要 table_list
        + start_time                                   → "json has no end_time"
    实测: 只要带了 start_time 就**必须同时带 end_time**, 否则整批失败。
    本函数把 end_time 默认补成今天, 调用方不必关心。

    ⚠️ 单表无数据时服务端返回 Value:null(该股该期真的没这类事件/无权限/窗太窄),
    这里统一过滤掉, 调用方按"缺表"处理, **不要当 0**。
    """
    if not table_list:
        return {}
    if not end_time:
        end_time = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
    params: dict = {"table_list": list(table_list), "end_time": end_time}
    if code:
        params["code"] = code
    if start_time:
        params["start_time"] = start_time
    v = _rpc(method, params, timeout=max(_TIMEOUT_S, timeout))
    if not isinstance(v, dict):
        return {}
    # 网关把多表结果平铺在 {表名: [...]}; 偶尔会夹带 ErrorId/Error
    out: dict = {}
    for k, val in v.items():
        if k in ("ErrorId", "Error", "run_id"):
            continue
        if val:
            out[k] = val
    return out


def gp_series(tables: list[str], code: str, *, start_time: str = "",
              end_time: str = "") -> dict:
    """个股交易数据序列(GP)。code 形如 '002361.SZ'。"""
    return _pro_series("get_gpjy_value", tables, code=code,
                       start_time=start_time, end_time=end_time)


def sc_series(tables: list[str], *, start_time: str = "", end_time: str = "",
              code: str = _SC_FALLBACK_CODE) -> dict:
    """市场交易数据序列(SC)。情绪周期历史基线的核心来源。"""
    return _pro_series("get_scjy_value", tables, code=code,
                       start_time=start_time, end_time=end_time)


def bk_series(tables: list[str], code: str, *, start_time: str = "",
              end_time: str = "") -> dict:
    """板块交易数据序列(BK)。code 形如 '880201.SH'。"""
    return _pro_series("get_bkjy_value", tables, code=code,
                       start_time=start_time, end_time=end_time)


def gp_one_data(tables: list[str], code: str) -> dict:
    """股票单个数据(GO, 非序列)。如 GO47=涨停时间。"""
    return _pro_series("get_gp_one_data", tables, code=code)


def financial_data(tables: list[str], code: str, *, start_time: str = "",
                   end_time: str = "", report_type: str = "announce_time") -> dict:
    """专业财务数据(FnXXX)。report_type: report_time=按截止日 / announce_time=按公告日。"""
    params = {"table_list": list(tables), "code": code, "report_type": report_type}
    if start_time:
        params["start_time"] = start_time
    if end_time:
        params["end_time"] = end_time
    v = _rpc("get_financial_data", params, timeout=max(_TIMEOUT_S, 30.0))
    if not isinstance(v, dict):
        return {}
    return {k: x for k, x in v.items()
            if k not in ("ErrorId", "Error", "run_id") and x}


def zdt_snapshot(codes: list[str]) -> dict:
    """涨跌停快照(当日)。每只返回 14 字段:

    FDVolMaxZT 最大封单量 / VolZT 涨停量 / FirstTimeZT 首封时间 /
    LastTimeZT 最后封板时间 / OpenTimesZT 打开次数(=炸板次数) /
    LastOpenTimeZT 最后打开时间 / ZDTStatusNow 当前状态 / ZDTStatusOri 原始状态 /
    TimeNow / 以及 DT(跌停) 同名字段 6 个。

    这是**当日快照**, 要历史必须每日落库(见 tq_sentiment_series)。
    传入全 A 列表即可还原整个涨停池; 客户端单次上限约 100 只时请分批。
    """
    if not codes:
        return {}
    v = _rpc("get_zdt_data", {"stock_list": list(codes)}, timeout=max(_TIMEOUT_S, 30.0))
    return v if isinstance(v, dict) else {}


def exday_data(code: str, count: int = 1) -> list:
    """日线统计数据: 四档资金 Amo/Vol + 委托 BOrder/SOrder/TotalBOrder/TotalSOrder
    + 撤单 BCancel/SCancel + 成交笔数 CJBS + VolNum。单位: 金额元/量手。"""
    v = _rpc("get_exday_data", {"stock_code": code, "count": int(count)},
             timeout=max(_TIMEOUT_S, 30.0))
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        return v.get("Value") or []
    return []


def gb_info(code: str, *, date_list: list[str] | None = None, count: int = 1) -> list:
    """股本信息(总股本 Zgb / 流通股本 Ltgb)。date_list 需升序。"""
    params = {"stock_code": code, "count": int(count)}
    if date_list:
        params["date_list"] = list(date_list)
    v = _rpc("get_gb_info", params)
    return v if isinstance(v, list) else (v.get("Value") or [] if isinstance(v, dict) else [])


def divid_factors(code: str, *, start_time: str = "", end_time: str = "") -> list:
    """除权除息数据(分红送配)。"""
    params = {"stock_code": code}
    if start_time:
        params["start_time"] = start_time
    if end_time:
        params["end_time"] = end_time
    v = _rpc("get_divid_factors", params)
    return v if isinstance(v, list) else (v.get("Value") or [] if isinstance(v, dict) else [])


def divid_factors_rows(code: str) -> list[dict]:
    """除权除息**带日期**的结构化行(供分红历史用)。

    返回 [{date, bonus, allot_price, share_bonus, allotment}], 按原序。

    为什么另起一个函数: get_divid_factors 的响应是 Date/Type/Value 三个**平行
    数组**(下标对齐), 原 divid_factors() 只取 Value。那对 tdx_calendar 算复权
    因子够用, 但分红历史缺 ex_date —— 实测网关其实**有回 Date**, 是被丢掉的。

    列语义取客户端 SDK(tqcenter.py)的 DataFrame 列名, 非猜:
        Bonus=派息 / AllotPrice=配股价 / ShareBonus=送股 / Allotment=配股

    ⚠️ 单位标定(2026-09-24, 002361.SZ, 与东财 RPT_SHAREBONUS_DET 逐笔对齐):
      · Bonus 是**每10股**派息 —— TQ 1.50 ↔ 东财 PRETAX_BONUS_RMB 1.5,
        方案原文"10转10.00派1.50元"。→ 落地成"每股派息"时必须 /10。
      · ShareBonus 是**送股+转增合计** —— 两笔验证: 送0转10 → 10.00;
        送2转8 → 10.00。TQ 不区分送/转, 只能记合计。
      · Bonus **只有 2 位小数**(通达信侧精度), 东财到 4 位 —— 600519 2026-06-26
        TQ 280.24 vs 东财 280.2423。对拍容差要按 0.01 元/10股 取, 不是 1e-6。
    """
    v = _rpc("get_divid_factors", {"stock_code": code}, full=True)
    if not isinstance(v, dict):
        return []
    dates, vals = v.get("Date") or [], v.get("Value") or []
    if not isinstance(dates, list) or not isinstance(vals, list):
        return []
    out: list[dict] = []
    for i, d in enumerate(dates):
        if i >= len(vals):
            break
        row = vals[i]
        if not isinstance(row, (list, tuple)) or len(row) < 4:
            continue
        out.append(
            {
                "date": _fmt_day(d),
                "bonus": _to_float(row[0]),
                "allot_price": _to_float(row[1]),
                "share_bonus": _to_float(row[2]),
                "allotment": _to_float(row[3]),
            }
        )
    return out


def pricevol(codes: list[str]) -> dict:
    """批量价量: {代码: {LastClose, Now, Volume, Zaf}}。全市场涨跌家数走这个。"""
    if not codes:
        return {}
    v = _rpc("get_pricevol", {"stock_list": list(codes)}, timeout=max(_TIMEOUT_S, 30.0))
    return v if isinstance(v, dict) else {}


def trading_calendar(market: str = "SH", *, start_time: str = "",
                     end_time: str = "") -> list:
    """交易日历(需客户端已下载上证指数盘后数据)。"""
    v = _rpc("get_trading_calendar",
             {"market": market, "start_time": start_time, "end_time": end_time})
    if isinstance(v, dict):
        return v.get("Date") or []
    return v if isinstance(v, list) else []


def sector_list(list_type: int = 1) -> list:
    """全部板块(587 个)。list_type=0 指数 / 1 板块。"""
    v = _rpc("get_sector_list", {"list_type": int(list_type)})
    return v if isinstance(v, list) else []


def sector_stocks(block_code: str, *, block_type: int = 0, list_type: int = 0) -> list:
    """板块成分股。block_code 可为板块代码/名称/自定义简称; block_type=2 走期货前缀。"""
    v = _rpc("get_stock_list_in_sector",
             {"block_code": block_code, "block_type": int(block_type),
              "list_type": int(list_type)})
    return v if isinstance(v, list) else []


def stock_list(market: str = "5", list_type: int = 5) -> list:
    """股票列表。list_type: 5 所有A股 / 0 自选 / 1 持仓 / 12 概念 / 31 ETF / 32 可转债。

    (实测: 不传 market/list_type 会报 '参数缺少:market/list_type')
    """
    v = _rpc("get_stock_list", {"market": str(market), "list_type": int(list_type)})
    return v if isinstance(v, list) else []


def relation(code: str) -> list:
    """股票所属板块(行业/概念/风格 + 成分数)。"""
    v = _rpc("get_relation", {"stock_code": code})
    return v if isinstance(v, list) else []


# 申购类型 → 中文(工具层/端点直接用)
_IPO_TYPE_CN = {0: "新股", 1: "新发债", 2: "新股+新债"}


def ipo_info(ipo_type: int = 2, ipo_date: int = 1) -> list[dict]:
    """新股/新债申购日历 → **结构化行**(实测 2026-09-25, 原薄壳只透传裸 list)。

    ipo_type: 0=新股 / 1=新发债 / 2=两者;  ipo_date: 0=仅今天 / 1=今天及以后。
    返回 [{code, name, sg_date, sg_price, sg_code, max_sg, pe_issue, ipo_type, ipo_type_cn}]
    —— 服务端**直接返 list[dict]**(不套 Value), 字段全为字符串, 按原样保留不做单位换算:
      · sg_date 形如 "20260924"(申购日); sg_price = 申购价(元), 新债恒为 100.00;
      · max_sg = 申购上限(**原文单位**, 客户端只给数值不给单位);
      · pe_issue 常为 "0.00" = **未披露**, 消费侧按"缺失"处理, 不要当 0 倍市盈率展示。
    过滤无 Code 的空壳行(服务端偶发返回)。callers: 对话工具 get_ipo_calendar。
    """
    v = _rpc("get_ipo_info", {"ipo_type": int(ipo_type), "ipo_date": int(ipo_date)})
    if isinstance(v, dict):  # 某些版本可能套一层
        v = v.get("Value") or v.get("Data") or []
    if not isinstance(v, list):
        return []
    out: list[dict] = []
    for row in v:
        if not isinstance(row, dict):
            continue
        code = str(row.get("Code") or "").strip()
        if not code:
            continue
        out.append({
            "code": code,
            "name": str(row.get("Name") or "").strip(),
            "sg_date": str(row.get("SGDate") or "").strip(),
            "sg_price": _to_float(row.get("SGPrice")),
            "sg_code": str(row.get("SGCode") or "").strip(),
            "max_sg": _to_float(row.get("MaxSG")),
            "pe_issue": _to_float(row.get("PE_Issue")),
            "ipo_type": int(ipo_type),
            "ipo_type_cn": _IPO_TYPE_CN.get(int(ipo_type), ""),
        })
    return out


def kzz_info(code: str) -> dict:
    """可转债条款/基础信息。⚠️ 必须传**带后缀**代码(如 `128136.SZ`); 裸码报 codestr error。

    实测 2026-09-25 修正: 响应是 `result.Value = [ {...} ]`(**列表**), 原实现判
    `isinstance(v, dict)` 于是**恒返 {}**(静默空)。这里取首行。
    字段: KZZCode/KZZName/KZZNow(转债现价)/HSCode(正股)/ZGPrice(转股价)/CurRate(当期利率)/
    RestScope(剩余规模)/ForceRedeem(强赎触发价)/PutBack(回售触发价)/ZGDate(转股日)/
    EndPrice(到期价)/EndDate(到期日)/RealValue(纯债价值)/HSScore(评级)/ExpireYield 等。
    查不到该转债 → {}。
    """
    v = _rpc("get_kzz_info", {"stock_code": code})
    if isinstance(v, dict):
        return v
    if isinstance(v, list) and v and isinstance(v[0], dict):
        return v[0]
    return {}


def trackzs_etf(zs_code: str) -> list:
    """跟踪某指数的 ETF 列表(含 IOPV/规模)。"""
    v = _rpc("get_trackzs_etf_info", {"zs_code": zs_code})
    return v if isinstance(v, list) else []


def download_file(*, stock_code: str = "", down_time: str = "",
                  down_type: int = 1) -> dict:
    """程序化触发客户端数据下载(落 .\\PYPlugins\\data)。

    down_type: 1 十大股东(下 10 名数据, down_time 只生效年份)
               2 ETF 申赎清单(down_time 生效到日期)
               3 最近舆情
               4 综合信息文件
    """
    v = _rpc("download_file", {"stock_code": stock_code, "down_time": down_time,
                               "down_type": int(down_type)},
             timeout=max(_TIMEOUT_S, 60.0))
    return v if isinstance(v, dict) else {}


#: 服务端批次元数据键: 不属于公式结果, 解析时必须剔除(SDK 同名过滤)。
_FORMULA_META_KEYS = frozenset({
    "ErrorId", "Error", "BatchFormulaPaged", "batch_start_index", "batch_end_index",
    "batch_stock_count", "stock_total", "next_stock_index", "has_more_batch",
})

#: 全市场扫描的单批标的数。**实测(2026-09-25)**: 500 只/批 = 1.6s/批,
#: 全 A(5577) 12 批 = **24.2s** 要回 5570 只; 而 50 只/批要 112 次 ≈ 145s(踩过),
#: 一次性传全市场则由服务端按 800 只/页分页 = 33s。故取 500。
TQ_SCAN_CHUNK = 500

#: 扫描的时间窗(自然日)。**必须给窄窗口**: 不带日期时网关按"全部历史"算,
#: 每片响应过重, 全市场扫描从 24s 拖到分钟级(实测踩过)。
_SCAN_LOOKBACK_DAYS = 20

#: 单批翻页保护: 服务端游标不前进时不要死循环。
_FORMULA_MAX_PAGES = 40

#: 非交易信号值(视为未命中)。网关回字符串 "0"/"1", 缺失回 null。
_FORMULA_FALSY = frozenset({"", "0", "0.0", "0.00", "none", "null", "false"})


def _formula_raw(formula_name: str, stocks: list[str], *, formula_arg: str,
                 stock_period: str, start_time: str, end_time: str,
                 return_count: int, return_date: bool, count: int,
                 dividend_type: int, batch_start_index: int) -> dict:
    """单批原始调用。

    用 `full=True` 自行判 ErrorId: **19 = "数据过大只能部分返回"** 是正常现象,
    交给 _rpc 会抛异常(旧实现即如此), 那就永远扫不完全市场。
    """
    params = {
        "formula_name": formula_name,
        "formula_arg": formula_arg,
        "stock_list": list(stocks),
        "stock_period": stock_period,
        "periodstr": stock_period,
        "start_time": start_time,
        "end_time": end_time,
        "return_count": return_count,
        "return_date": return_date,
        "count": count,
        "dividend_type": dividend_type,
        "batch_start_index": int(batch_start_index),
    }
    return _rpc("formula_process_mul_xg", params, timeout=max(_TIMEOUT_S, 120.0), full=True)


def formula_xg_mul(formula_name: str, stock_list_: list[str], *,
                   formula_arg: str = "", stock_period: str = "1d",
                   start_time: str = "", end_time: str = "",
                   return_count: int = 0, return_date: bool = True,
                   count: int = 0, dividend_type: int = 0,
                   max_pages: int = _FORMULA_MAX_PAGES) -> dict:
    """批量条件选股公式(formula_process_mul_xg) → {代码: {信号名: [{Date, Value}]}}。

    ⚠️ 服务端**分页返回**: 每批带 `BatchFormulaPaged`/`next_stock_index`/`has_more_batch`,
    必须按 `batch_start_index` 游标续取到 `has_more_batch` 为假。只取第一批会
    **静默漏掉大部分标的**(2026-09-25 修: 旧实现无翻页, 大列表下结果残缺而看不出)。
    `ErrorId=19`(数据过大) 时返回已取到的部分 + 记警告, **不抛**。
    """
    out: dict = {}
    start = 0
    for _ in range(max(1, int(max_pages))):
        res = _formula_raw(formula_name, stock_list_, formula_arg=formula_arg,
                           stock_period=stock_period, start_time=start_time,
                           end_time=end_time, return_count=return_count,
                           return_date=return_date, count=count,
                           dividend_type=dividend_type, batch_start_index=start)
        if not isinstance(res, dict):
            break
        err = str(res.get("ErrorId", "0"))
        if err == "19":
            logger.warning("TQ 条件选股 %s: 数据过大, 部分返回(已取 %d 只)",
                           formula_name, len(out))
        elif err not in ("0", ""):
            raise RuntimeError(
                f"TQ formula_process_mul_xg ErrorId={err}: {res.get('Error', '')}")
        out.update({k: v for k, v in res.items() if k not in _FORMULA_META_KEYS})
        if not res.get("BatchFormulaPaged") or not res.get("has_more_batch"):
            break
        try:
            nxt = int(str(res.get("next_stock_index")))
        except (TypeError, ValueError):
            logger.warning("TQ 条件选股 %s: 批次游标异常, 停止续取", formula_name)
            break
        if nxt <= start:
            logger.warning("TQ 条件选股 %s: 批次游标未前进(%d), 停止续取", formula_name, nxt)
            break
        start = nxt
    return out


def _date_rows_seen(res: dict, date: str) -> int:
    """该日期在响应里出现的**数据行数**(不看值)。

    用途: 区分"非交易日/无数据"(0 行) 与"当日无票触发"(有行但值全 0)。
    两者都算不出命中, 但**落库含义完全不同** —— 前者写 0 会把基线拉低(等于用假期
    稀释掉真实基线), 后者才是真的 0 家。
    """
    if not date:
        return -1
    n = 0
    for code, block in (res or {}).items():
        if code in _FORMULA_META_KEYS or not isinstance(block, dict):
            continue
        for series in block.values():
            if isinstance(series, list):
                n += sum(1 for r in series
                         if isinstance(r, dict) and str(r.get("Date")) == str(date))
    return n


def _formula_hits(res: dict, date: str = "") -> tuple[list[dict], str]:
    """逐日信号序列 → 命中清单。返回 (hits, 实际使用的日期)。"""
    hits: list[dict] = []
    used_date = date
    for code, block in (res or {}).items():
        if not isinstance(block, dict) or not code or code in _FORMULA_META_KEYS:
            continue
        for sig, series in block.items():
            if not isinstance(series, list) or not series:
                continue
            row = None
            if date:
                row = next((r for r in series if str(r.get("Date")) == str(date)), None)
            else:
                row = series[-1]
                used_date = used_date or str(row.get("Date") or "")
            if not isinstance(row, dict):
                continue
            val = row.get("Value")
            if val is None:
                continue
            text = str(val).strip()
            if text.lower() in _FORMULA_FALSY:
                continue
            hits.append({"symbol": code, "signal": sig,
                         "date": str(row.get("Date") or date), "value": text})
    return hits, used_date


def formula_scan(formula_name: str, *, formula_arg: str = "", date: str = "",
                 codes: list[str] | None = None, chunk: int = TQ_SCAN_CHUNK,
                 market: str = "5", list_type: int = 1) -> dict:
    """全市场扫一遍条件选股公式, 返回命中清单。

    实测性能(2026-09-25): 500 只/批 = 1.6s → 全 A(5577) 12 批 = **24.2s**,
    返回 5570 只(差 7 只=停牌/无数据)。分片是**硬约束** —— 批量接口超限会把
    Windows 客户端打成假死(唯一解整机重启, 见 tq-capability-audit)。

    ⚠️ 必须给**窄时间窗**: 实测不带日期(count=-1=全部历史)时每片响应过重,
    全市场扫描会从 24s 拖到分钟级(客户端一直忙着算历史)。这里默认回看
    `_SCAN_LOOKBACK_DAYS` 个自然日, 足够覆盖目标交易日。

    诚实性: 任一分片失败都会计入 `chunks_failed` 并把 `complete` 置 False,
    调用方**不得**把不完整结果当成"全市场命中数"(会变成编造)。

    `list_type=1` 实测回全 A 5577 只(与 stock_list 文档里的 "1=持仓" 不符,
    以实测为准; 见 CHANGELOG)。
    """
    pool = list(codes) if codes else [str(i.get("Code")) for i in (stock_list(market, list_type) or [])
                                      if isinstance(i, dict) and i.get("Code")]
    if not pool:
        return {"formula": formula_name, "formula_arg": formula_arg, "date": date,
                "scanned": 0, "hit_count": 0, "hits": [], "per_signal": {},
                "chunks_failed": 0, "complete": False,
                "error": "代码池为空(取股票列表失败)"}

    # 窄窗口: end = 目标日(默认今天), start = end - N 自然日
    end_day = _norm_day(date) or datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
    try:
        start_day = (datetime.strptime(end_day, "%Y%m%d")
                     - timedelta(days=_SCAN_LOOKBACK_DAYS)).strftime("%Y%m%d")
    except ValueError:
        start_day = ""

    step = max(1, int(chunk))
    hits: list[dict] = []
    failed = 0
    scanned = 0
    resolved = date
    date_rows = 0
    for i in range(0, len(pool), step):
        part = pool[i: i + step]
        try:
            res = formula_xg_mul(formula_name, part, formula_arg=formula_arg,
                                 start_time=start_day, end_time=end_day)
        except Exception as e:  # noqa: BLE001
            failed += 1
            logger.warning("TQ 条件选股 %s: 第 %d 片失败(%s)", formula_name, i // step + 1, e)
            continue
        scanned += len(part)
        date_rows += max(0, _date_rows_seen(res, date))
        part_hits, resolved = _formula_hits(res, date)
        hits.extend(part_hits)
    per_signal: dict[str, int] = {}
    for h in hits:
        per_signal[h["signal"]] = per_signal.get(h["signal"], 0) + 1
    return {
        "formula": formula_name,
        "formula_arg": formula_arg,
        "date": resolved or date,
        "requested_date": date,
        "scanned": scanned,
        "hit_count": len(hits),
        "hits": hits,
        "per_signal": per_signal,
        "chunks_failed": failed,
        "complete": failed == 0,
        # ⚠️ 区分两种 0: date_rows==0 = 该日**没有数据**(非交易日/窗口没覆盖),
        # 此时 hit_count=0 **不是**"今日无票触发" —— 落库方必须跳过, 否则假期 0 会稀释基线。
        "date_rows": date_rows,
        "date_has_data": date_rows > 0 if date else None,
    }


def formula_all(formula_type: int = 0) -> list:
    """公式列表。formula_type: 0 技术指标 / 1 条件选股 / 2 专家系统 / 3 最新财务选股
    / 4 实时行情选股 / 5 逻辑运算选股。(实测 type=1 有 108 个条件选股公式)"""
    v = _rpc("formula_get_all", {"formula_type": int(formula_type)})
    return v if isinstance(v, list) else []


def match_stkinfo(key_word: str) -> list:
    """按关键词检索证券(代码/名称/拼音)。"""
    v = _rpc("get_match_stkinfo", {"key_word": key_word})
    return v if isinstance(v, list) else []


def refresh_cache(*, market: str = "AG", force: bool = False) -> dict:
    """刷新行情缓存。刷新后 5 分钟内取快照/K线不再触发刷新。

    串行调用会显著拖慢客户端, 批量取数前调用一次即可。
    """
    v = _rpc("refresh_cache", {"market": market, "force": bool(force)},
             timeout=max(_TIMEOUT_S, 30.0))
    return v if isinstance(v, dict) else {}


def refresh_kline(codes: list[str], *, period: str = "1d") -> dict:
    """缓存历史 K 线(仅支持 1m/5m/1d)。一次别更新太多, 会堵塞策略与客户端。"""
    v = _rpc("refresh_kline", {"stock_list": list(codes), "period": period},
             timeout=max(_TIMEOUT_S, 60.0))
    return v if isinstance(v, dict) else {}


def subscribe_hq(codes: list[str]) -> dict:
    """订阅行情更新(客户端侧推送, 单次上限约 100 条)。"""
    v = _rpc("subscribe_hq", {"stock_list": list(codes)},
             timeout=max(_TIMEOUT_S, 20.0))
    return v if isinstance(v, dict) else {}


def unsubscribe_hq(codes: list[str]) -> dict:
    v = _rpc("unsubscribe_hq", {"stock_list": list(codes)},
             timeout=max(_TIMEOUT_S, 20.0))
    return v if isinstance(v, dict) else {}


def send_message(msg: str) -> dict:
    """推送一条消息到客户端 TQ 策略界面('|' 或 '\\n' 分行)。"""
    v = _rpc("send_message", {"message": msg})
    return v if isinstance(v, dict) else {}


def send_warn(stock_list_: list[str], time_list: list[str], price_list: list[str],
              close_list: list[str], volum_list: list[str],
              bs_flag_list: list[str], reason_list: list[str]) -> dict:
    """推送预警信号到客户端预警列表。bs_flag 0买1卖2未知; time 形如 '20260923141115'。"""
    v = _rpc("send_warn", {
        "stock_list": list(stock_list_),
        "time_list": list(time_list),
        "price_list": list(price_list),
        "close_list": list(close_list),
        "volum_list": list(volum_list),
        "bs_flag_list": list(bs_flag_list),
        "warn_type_list": ["0"] * len(stock_list_),
        "reason_list": list(reason_list),
        "count": len(stock_list_),
    }, timeout=max(_TIMEOUT_S, 20.0))
    return v if isinstance(v, dict) else {}


# ════════════ 清单切换 (2026-09-23): 东财单日快照 → TQ 历史序列 ════════════
#
# 为什么切: ①东财 push2ex/datacenter 只给**当日快照**, 要历史必须按日循环抓
# (N 次 HTTP + 限流风险); ②TQ GP/SC 一次给 400+ 交易日; ③TQ 走本地客户端, 零配额。
# 解析与 RPC 分离: 纯函数可单测(CI 无 TQ 网关)。

#: 龙虎榜相关 GP 表。GP02 买卖总额 / GP08 机构(卖) / GP09 机构(买) /
#: GP17 营业部买卖 / GP18 沪深股通买卖。实测单位: 万元(GP02/08/09/17/18 同口径)。
LHB_TABLES = ("GP02", "GP08", "GP09", "GP17", "GP18")


def gp_pairs(v) -> dict:
    """GP/SC 返回结构 → {date: {表名: [值...]}}。

    网关原始形如 {"GP02": [{"Date": "20251219", "Value": ["14881.66", "23043.70"]}, ...],
    "GP08": [...]}。按 **日期** 归并成一张宽表, 便于多表 left join。
    """
    out: dict = {}
    if not isinstance(v, dict):
        return out
    for tab, rows in v.items():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            d = str(row.get("Date") or "").strip()
            vals = row.get("Value")
            if not d or not isinstance(vals, list):
                continue
            out.setdefault(d, {})[str(tab)] = [_to_float(x) for x in vals]
    return out


def _pair(vals, i: int) -> float | None:
    """安全取 Value[i](缺失/短数组 → None, 不抛)。"""
    if not isinstance(vals, list) or len(vals) <= i:
        return None
    return vals[i]


def lhb_rows(pairs: dict) -> list:
    """{date: {GPxx: [..]}} → 龙虎榜行(按日期升序)。

    字段(**单位: 万元** —— 已用独立交叉验证标定: GP16 总市值 946279.81 万
    ≈ 总股本 9.51 亿股 × 10.10 元 = 96 亿 = 960000 万, 量级吻合):
      buy/sell                GP02[0]/[1]         买卖总额
      inst_sell_amount/_cnt   GP08[1]/[0]         机构卖出金额/机构数
      inst_buy_amount/_cnt    GP09[1]/[0]         机构买入金额/机构数
      yyb_buy/yyb_sell        GP17[0]/[1]         营业部买入/卖出
      hsgt_buy/hsgt_sell      GP18[0]/[1]         沪深股通买入/卖出

    ⚠️ 实测 GP08/GP09 是 [机构个数, 金额] 而 GP17/18 是 [买, 卖] —— 顺序不同, 别套用。
    ⚠️ 交叉核对: GP02 ≈ GP17 + GP18 **只是近似, 不是恒等式** —— 实测 20251229 精确相等,
       但 20260819 差 +4629.77 万(营业部+沪深股通 只覆盖龙虎榜总额的一部分, 机构专用席位等
       不在 GP17/18 里)。**别拿它当校验断言**, 只当量级合理性参考。

    `suspicious`: buy == sell 且量级远超邻日(实测 20251229 单日 1443198.50 万 = 144 亿,
    是相邻日的 ~60 倍; 144 亿 > 该股总市值 94.6 亿, 不像真实成交)。
    只标记不丢弃: 口径归 TQ, 消费方自行决定要不要过滤。
    """
    rows = []
    for d in sorted((pairs or {}).keys()):
        g = pairs[d] or {}
        gp02 = g.get("GP02")
        # 完全没值的日期跳过(该股当日未上榜)
        if gp02 is None and not any(g.get(t) for t in LHB_TABLES[1:]):
            continue
        buy, sell = _pair(gp02, 0), _pair(gp02, 1)
        rows.append(
            {
                "date": d,
                "buy": buy,
                "sell": sell,
                "inst_sell_cnt": _pair(g.get("GP08"), 0),
                "inst_sell_amount": _pair(g.get("GP08"), 1),
                "inst_buy_cnt": _pair(g.get("GP09"), 0),
                "inst_buy_amount": _pair(g.get("GP09"), 1),
                "yyb_buy": _pair(g.get("GP17"), 0),
                "yyb_sell": _pair(g.get("GP17"), 1),
                "hsgt_buy": _pair(g.get("GP18"), 0),
                "hsgt_sell": _pair(g.get("GP18"), 1),
            }
        )
    # 异常标记: 买卖完全相等(真实龙虎榜买卖几乎不可能一模一样) 且量级突出
    vals = [r["buy"] for r in rows if r.get("buy")]
    med = sorted(vals)[len(vals) // 2] if vals else 0.0
    for r in rows:
        b, s = r.get("buy"), r.get("sell")
        r["suspicious"] = bool(
            b is not None and s is not None and b == s and med and abs(b) > med * 10
        )
    return rows


def lhb_series(code: str, *, start_time: str = "", end_time: str = "",
               _rpc_fn=None) -> list:
    """个股龙虎榜历史序列(东财只给单日 → TQ 一次给整段)。

    start_time/end_time: YYYYMMDD; end_time 缺省即今天(网关把 end_time 当必填)。
    """
    from datetime import datetime as _dt

    fn = _rpc_fn or _rpc
    params = {
        "table_list": list(LHB_TABLES),
        "code": code,
        "start_time": start_time or "20200101",
        "end_time": end_time or _dt.now().strftime("%Y%m%d"),
    }
    v = fn("get_gpjy_value", params, timeout=max(_TIMEOUT_S, 30.0))
    return lhb_rows(gp_pairs(v))


def breadth(codes: list, *, chunk: int = 500, _rpc_fn=None) -> dict:
    """全市场涨跌家数(替代东财 ulist.np / cn 网关 market-overview)。

    实测(2026-09-23): pricevol 500 只 / 0.07s, 全 A 5576 只分 12 片约 1s。
    分片大小 500 是**实测安全值**(get_market_data 的 ≤50 限制不适用于本方法,
    但绝不单次发全市场 —— 那次把客户端压进假死态, 见 market_sentiment_collector 注释)。

    返回: {up, down, flat, total, codes_ok, failed_chunks}。Zaf 缺失的票不计入任何一档。
    """
    fn = _rpc_fn or _rpc
    up = down = flat = 0
    ok = 0
    failed = 0
    for i in range(0, len(codes or []), max(1, int(chunk))):
        part = list(codes[i : i + max(1, int(chunk))])
        try:
            v = fn("get_pricevol", {"stock_list": part}, timeout=max(_TIMEOUT_S, 30.0))
        except Exception:  # noqa: BLE001
            failed += 1
            continue
        if not isinstance(v, dict):
            failed += 1
            continue
        for item in v.values():
            if not isinstance(item, dict):
                continue
            z = _to_float(item.get("Zaf"))
            if z is None:
                continue
            ok += 1
            if z > 0:
                up += 1
            elif z < 0:
                down += 1
            else:
                flat += 1
    return {"up": up, "down": down, "flat": flat, "total": up + down + flat,
            "codes_ok": ok, "failed_chunks": failed}


#: exday_data 的标量字段 → 中文/语义名。Amo/Vol 是 4×4 矩阵(四档×4), 原样透传。
_EXDAY_SCALARS = {
    "BCancel": "b_cancel", "SCancel": "s_cancel",
    "BOrder": "b_order", "SOrder": "s_order",
    "TotalBOrder": "total_b_order", "TotalSOrder": "total_s_order",
    "CJBS": "trades", "VolNum": "vol_num",
}


def exday_latest(code: str, *, count: int = 1, _rpc_fn=None) -> dict:
    """最近 N 日日线统计 → 结构化(含**撤单量**, 免费东财层拿不到)。

    ⚠️ 网关注册的 JSON key 是 `stock_code`; 报错文案里叫 "codestr" 是**服务端内部命名**,
    别照着报错改成 codestr(实测 codestr 反而报错)。units: 金额元 / 量手。
    返回 {"Amo": [[4],[4],[4],[4]], "Vol": ..., "b_cancel": ..., ...}; 空 → {}。
    """
    fn = _rpc_fn or _rpc
    v = fn("get_exday_data", {"stock_code": code, "count": int(count)},
           timeout=max(_TIMEOUT_S, 30.0))
    if isinstance(v, dict):
        v = v.get("Value") or []
    if not isinstance(v, list) or not v:
        return {}
    row = v[-1] if isinstance(v[-1], dict) else {}
    out = {"Amo": row.get("Amo"), "Vol": row.get("Vol")}
    for k, name in _EXDAY_SCALARS.items():
        out[name] = _to_float(row.get(k))
    out["date"] = str(row.get("Date") or "")
    return out

