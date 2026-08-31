"""资金流向采集器 - 经 marketdata 包统一接入"""
import logging

from dataclasses import dataclass

from src.collectors.market_http import TTLCache
from src.models.market import MarketCode

logger = logging.getLogger(__name__)

# 资金流为日级数据、变动慢:中等 TTL 缓存,避免每轮重复拉。
_FLOW_CACHE = TTLCache(default_ttl_sec=120.0)


@dataclass
class CapitalFlow:
    """资金流向数据"""
    symbol: str
    name: str

    # 今日资金流（单位：元）
    main_net_inflow: float      # 主力净流入
    main_net_inflow_pct: float  # 主力净流入占比
    super_net_inflow: float     # 超大单净流入
    big_net_inflow: float       # 大单净流入
    mid_net_inflow: float       # 中单净流入
    small_net_inflow: float     # 小单净流入

    # 5日资金流
    main_net_5d: float | None = None  # 5日主力净流入
    date: str | None = None  # 数据基准日(盘中=T-1收盘)


def get_market_data():
    """惰性导入,避免模块加载时的循环依赖(便于测试 monkeypatch)。"""
    from src.core.marketdata_client import get_market_data as _g
    return _g()


# 国内数据网关(2026-08-10): 东财 push2delay 今日实时资金流
# 双模式数据源接入(2026-08-10 用户需求):
#   - 大陆本地部署: 直连东财 push2delay(无需网关, 大陆网络直通)
#   - 海外/香港部署: 走国内网关代理(115.190.177.213:8100, 东财资金流字段海外被风控断连)
# 自动检测: 默认先试直连(push2delay), 失败自动走网关; 可用环境变量强制指定模式:
#   CN_FLOW_MODE=direct   强制直连(大陆部署, 不依赖网关)
#   CN_FLOW_MODE=gateway  强制走网关(海外部署)
#   CN_FLOW_MODE=auto     自动检测(默认)
import os as _os

_CN_FLOW_MODE = _os.getenv("CN_FLOW_MODE", "auto")
CN_GATEWAY_BASE = _os.getenv("CN_GATEWAY_BASE", "http://115.190.177.213:8100")
_CN_GATEWAY_DISABLED = _os.getenv("CN_GATEWAY_DISABLE") == "1"  # 测试用

# 直连东财 push2delay(大陆网络直通; 海外会被断连)
_DIRECT_FLOW_URL = "https://push2delay.eastmoney.com/api/qt/ulist.np/get"
# 修复(L-3, 2026-08-23): 冷启动 5s 超时易挂 — 提到 8s, 允许 1 次内部重试(网络抖动兜底)。
# 原 5s 是大陆本地部署的乐观估计, 海外/容器环境下命中概率偏低, 现走 requests Session 重试。
_DIRECT_FLOW_TIMEOUT_S = 8.0
_DIRECT_FLOW_MAX_RETRY = 1  # 共 2 次尝试


def _fetch_direct_flow(symbol: str) -> CapitalFlow | None:
    """直连东财 push2delay 取今日实时资金流(大陆部署, 不依赖网关)。

    修复(L-3 + M-4, 2026-08-23):
    - L-3: timeout 5s → 8s + 1 次内部重试(手动, 不依赖 Session, 兼容测试 monkeypatch).
    - M-4: 开盘初期 f62/f184/f66/f72 全 0 识别为"数据未生成"(区分于"无数据"),
           信息级别而非原来的 warn, 上层据此跳过 fallback 视为 "开盘数据未就绪"。
    """
    import requests as _req

    code = symbol.strip()
    secid = f"1.{code}" if code.startswith(("6", "5", "9")) else f"0.{code}"
    fields = "f2,f3,f12,f14,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87"
    url = f"{_DIRECT_FLOW_URL}?secids={secid}&fields={fields}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://data.eastmoney.com/",
    }

    try:
        last_err: Exception | None = None
        r = None
        for attempt in range(_DIRECT_FLOW_MAX_RETRY + 1):
            try:
                r = _req.get(url, headers=headers, timeout=_DIRECT_FLOW_TIMEOUT_S)
                break
            except Exception as e:
                last_err = e
                if attempt < _DIRECT_FLOW_MAX_RETRY:
                    import time as _t
                    _t.sleep(0.3)
                    continue
        if r is None:
            logger.debug("东财直连资金流最终失败 %s: %s", symbol, last_err)
            return None
        if r.status_code != 200:
            return None
        d = r.json()
        diff = (d.get("data") or {}).get("diff") or []
        if not diff:
            return None
        it = diff[0]
        if it.get("f62") is None:
            return None
        # 修复(M-4, 2026-08-23): 开盘初期防 0 值误判。
        # f62/f184/f66/f72 全 0 ⇒ 数据尚未初始化(而非"主力资金平衡")。返回 None + 信息级日志,
        # 让上层 fallback 拿腾讯 / Engine 源补, UI 端可标"开盘数据未生成,请稍后"。
        f62 = it.get("f62")
        f184 = it.get("f184")
        f66 = it.get("f66")
        f72 = it.get("f72")
        if f62 == 0 and f184 == 0 and f66 == 0 and f72 == 0:
            logger.info(
                "东财资金流字段未初始化(全0, 开盘初期/午后重启?), 回退其他源: %s",
                symbol,
            )
            return None
        return CapitalFlow(
            symbol=symbol,
            name=it.get("f14") or "",
            main_net_inflow=float(f62 or 0),
            main_net_inflow_pct=float(f184 or 0) / 100.0,  # ×100 → %
            super_net_inflow=float(f66 or 0),
            big_net_inflow=float(f72 or 0),
            mid_net_inflow=float(it.get("f78") or 0),
            small_net_inflow=float(it.get("f84") or 0),
            main_net_5d=None,
            date=_today_cn(),  # L-1: 统一 YYYY-MM-DD 口径, 与 _today_cn() 同
        )
    except Exception as e:
        logger.debug("东财直连资金流异常 %s: %s", symbol, e)
        return None


def _fetch_cn_gateway_flow(symbol: str) -> CapitalFlow | None:
    """调国内网关取今日实时主力资金流(东财 push2delay, 盘中实时)。

    返回 CapitalFlow; 网关不可用/无数据/禁用返回 None(调用方回退旧源)。
    """
    if _CN_GATEWAY_DISABLED:
        return None
    try:
        import requests
        r = requests.get(
            f"{CN_GATEWAY_BASE}/cn/stock-flow/{symbol}",
            timeout=5,
        )
        if r.status_code != 200:
            return None
        d = r.json()
        if d.get("error") or d.get("main_net_inflow") is None:
            return None
        return CapitalFlow(
            symbol=symbol,
            name=d.get("name") or "",
            main_net_inflow=float(d.get("main_net_inflow") or 0),
            main_net_inflow_pct=float(d.get("main_net_pct") or 0) / 100.0,  # ×100 → %
            super_net_inflow=float(d.get("super_net_inflow") or 0),
            big_net_inflow=float(d.get("big_net_inflow") or 0),
            mid_net_inflow=float(d.get("mid_net_inflow") or 0),
            small_net_inflow=float(d.get("small_net_inflow") or 0),
            main_net_5d=None,
            date=_today_cn(),  # 今日实时
        )
    except Exception:
        return None


def _today_cn() -> str:
    import datetime
    return datetime.date.today().strftime("%Y-%m-%d")


class CapitalFlowCollector:
    """资金流向采集器"""

    def __init__(self, market: MarketCode):
        self.market = market

    def get_capital_flow(self, symbol: str) -> CapitalFlow | None:
        """获取单只股票的资金流向。

        取数优先级(2026-08-11 更新):
        1) 东财 push2delay 今日实时资金流(直连/网关, 含完整四档) — 开盘初期全 0 视为未就绪回退
        2) Engine 四档实时(新浪 T-1 / 东财 push2his)

        悟道 intraday_main_flow 已移除: 9:15-10:30 限流且只给主力净额无四档明细。
        """
        cache_key = f"{self.market.value}:{symbol}"
        cached = _FLOW_CACHE.get(cache_key)
        if cached is not None:
            return cached

        capital_flow = None
        # 0) 今日实时资金流(双模式: 大陆直连 / 海外走网关, 2026-08-10 接入)
        # 网关已含完整四档(超大/大/中/小)且为今日实时, 命中则直接返回
        try:
            cf = None
            if _CN_FLOW_MODE == "direct":
                cf = _fetch_direct_flow(symbol)
            elif _CN_FLOW_MODE == "gateway":
                cf = _fetch_cn_gateway_flow(symbol)
            else:  # auto: 先直连(大陆快), 失败走网关(海外)
                cf = _fetch_direct_flow(symbol)
                if cf is None:
                    cf = _fetch_cn_gateway_flow(symbol)
            if cf is not None:
                _FLOW_CACHE.set(cache_key, cf)
                return cf
        except Exception as e:
            logger.debug(f"今日实时资金流失败, 回退腾讯/Engine: {e}")
        # 1) 腾讯证券实时资金流(2026-08-11 接入): 与东财同为当日实时四档口径,
        #    东财开盘初期 f62=0 未就绪或直连失败时, 腾讯侧通常已有数据 —— 第二实时源
        if capital_flow is None:
            try:
                from marketdata.vendors.tencent_fundflow import TencentFundflowVendor
                from marketdata import Symbol as MDSymbol

                vendor = TencentFundflowVendor()
                rows = vendor.fetch([MDSymbol.parse(symbol, self.market.value)], {})
                if rows:
                    tcf = rows[0]
                    if tcf.main_net_inflow is not None and abs(tcf.main_net_inflow) > 0:
                        capital_flow = CapitalFlow(
                            symbol=tcf.symbol, name=tcf.name,
                            main_net_inflow=tcf.main_net_inflow,
                            main_net_inflow_pct=tcf.main_net_inflow_pct,
                            super_net_inflow=tcf.super_net_inflow,
                            big_net_inflow=tcf.big_net_inflow,
                            mid_net_inflow=tcf.mid_net_inflow,
                            small_net_inflow=tcf.small_net_inflow,
                            main_net_5d=tcf.main_net_5d,
                            # L-1 修复: date 统一 YYYY-MM-DD 口径, 不用空串
                            date=_today_cn(),
                        )
                        _FLOW_CACHE.set(cache_key, capital_flow)
                        logger.debug(f"腾讯实时资金流命中: {symbol} 主力={tcf.main_net_inflow}")
                        return capital_flow
            except Exception as e:
                logger.debug(f"腾讯实时资金流失败: {e}")

        # 2) Engine 四档实时(腾讯/东财)补全
        md_cf = get_market_data().capital_flow(symbol, market=self.market.value)
        if md_cf is not None:
            if capital_flow is None:
                capital_flow = CapitalFlow(
                    symbol=md_cf.symbol, name=md_cf.name,
                    main_net_inflow=md_cf.main_net_inflow,
                    main_net_inflow_pct=md_cf.main_net_inflow_pct,
                    super_net_inflow=md_cf.super_net_inflow,
                    big_net_inflow=md_cf.big_net_inflow,
                    mid_net_inflow=md_cf.mid_net_inflow,
                    small_net_inflow=md_cf.small_net_inflow,
                    main_net_5d=md_cf.main_net_5d,
                    date=md_cf.date,
                )
            else:
                # 悟道实时净额优先, 四档用 Engine
                capital_flow.super_net_inflow = md_cf.super_net_inflow
                capital_flow.big_net_inflow = md_cf.big_net_inflow
                capital_flow.mid_net_inflow = md_cf.mid_net_inflow
                capital_flow.small_net_inflow = md_cf.small_net_inflow
                capital_flow.main_net_5d = md_cf.main_net_5d
                capital_flow.date = md_cf.date
                if md_cf.name:
                    capital_flow.name = md_cf.name

        if capital_flow is None:
            return None
        _FLOW_CACHE.set(cache_key, capital_flow)
        return capital_flow

    def get_capital_flow_summary(self, symbol: str) -> dict:
        """获取资金流向摘要（用于 prompt）"""
        flow = self.get_capital_flow(symbol)

        if not flow:
            return {"error": "无资金流向数据"}

        # 判断资金状态
        if flow.main_net_inflow > 0:
            if flow.main_net_inflow_pct > 10:
                status = "主力大幅流入"
            elif flow.main_net_inflow_pct > 5:
                status = "主力明显流入"
            else:
                status = "主力小幅流入"
        elif flow.main_net_inflow < 0:
            if flow.main_net_inflow_pct < -10:
                status = "主力大幅流出"
            elif flow.main_net_inflow_pct < -5:
                status = "主力明显流出"
            else:
                status = "主力小幅流出"
        else:
            status = "主力资金平衡"

        # 5日趋势
        trend_5d = "无数据"
        if flow.main_net_5d is not None:
            if flow.main_net_5d > 0:
                trend_5d = f"5日净流入{flow.main_net_5d/1e8:.2f}亿"
            else:
                trend_5d = f"5日净流出{abs(flow.main_net_5d)/1e8:.2f}亿"

        return {
            "status": status,
            "main_net_inflow": flow.main_net_inflow,
            "main_net_inflow_pct": flow.main_net_inflow_pct,
            "super_net_inflow": flow.super_net_inflow,
            "big_net_inflow": flow.big_net_inflow,
            "mid_net_inflow": flow.mid_net_inflow,
            "small_net_inflow": flow.small_net_inflow,
            "trend_5d": trend_5d,
            "date": flow.date,  # 数据基准日(盘中=T-1, 明确标注防误导)
        }
