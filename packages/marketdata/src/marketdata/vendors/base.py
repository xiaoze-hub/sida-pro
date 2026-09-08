"""Vendor 抽象:每个 vendor 只负责"一家源怎么抓 + 解析成标准类型",内部无 fallback。"""

from __future__ import annotations

import functools
import threading
from abc import ABC, abstractmethod

from marketdata.symbol import Symbol

# ── 数据源失败监听 ──
# marketdata 包内零依赖: 失败事件经 emit_vendor_failure 广播, 由宿主应用
# (src/web/api/health.py)启动时注册桥接回 Prometheus sida_datasource_failures_total。
_FAILURE_LISTENERS: list = []
_FAILURE_LISTENERS_LOCK = threading.Lock()


def on_vendor_failure(listener) -> None:
    """注册 vendor 失败监听 listener(provider, kind)。重复注册幂等。"""
    with _FAILURE_LISTENERS_LOCK:
        if listener not in _FAILURE_LISTENERS:
            _FAILURE_LISTENERS.append(listener)


def emit_vendor_failure(provider: str, kind: str = "fetch") -> None:
    """广播一次 vendor 失败。监听者异常互不影响, 绝不反噬业务。"""
    with _FAILURE_LISTENERS_LOCK:
        listeners = list(_FAILURE_LISTENERS)
    for cb in listeners:
        try:
            cb(provider, kind)
        except Exception:
            pass


def _instrument_fetch(original):
    """fetch 失败自动上报后原样抛出(不吞异常, 由上层决定降级)。"""
    @functools.wraps(original)
    def wrapper(self, symbols, config):
        try:
            return original(self, symbols, config)
        except Exception:
            emit_vendor_failure(self.name or type(self).__name__, "fetch")
            raise
    wrapper._failure_instrumented = True
    return wrapper


class Vendor(ABC):
    #: 注册名,与 SourceConfig.vendor / DataSource.provider 对齐
    name: str = ""
    #: 支持的市场集合(空集=全部);Engine 会按市场过滤
    supports_markets: set[str] = set()

    def __init_subclass__(cls, **kwargs):
        # 每个子类的 fetch 定义处包一次失败上报(直调 vendor 与 Engine 两条路径都覆盖)
        super().__init_subclass__(**kwargs)
        f = cls.__dict__.get("fetch")
        if (
            callable(f)
            and not getattr(f, "__isabstractmethod__", False)
            and not getattr(f, "_failure_instrumented", False)
        ):
            cls.fetch = _instrument_fetch(f)

    @abstractmethod
    def fetch(self, symbols: list[Symbol], config: dict) -> list:
        """抓取并解析。失败应抛异常(Engine 捕获后转移),空结果返回 []。"""
        ...


class QuoteVendor(Vendor):
    """报价 vendor:fetch 返回 list[Quote]。"""

    pass


class KlineVendor(Vendor):
    """K 线 vendor:fetch 返回 list[Bar]。单 symbol。"""

    pass


class CapitalFlowVendor(Vendor):
    """资金流向 vendor:fetch 返回 list[CapitalFlow]。单 symbol。"""

    pass


class EventsVendor(Vendor):
    """事件 vendor:fetch 返回 list[EventItem]。批量(多 symbol)。"""

    pass


class FlashNewsVendor(Vendor):
    """快讯 vendor:fetch 返回 list[FlashNews]。市场级,symbols 可空。"""

    pass


class NewsVendor(Vendor):
    """新闻 vendor:返回 list[NewsArticle],按 symbol。"""

    pass


class FundamentalsVendor(Vendor):
    """基本面/财务 vendor:fetch 返回 list[Fundamentals]。按 symbol(批量)。"""

    pass


class DragonTigerVendor(Vendor):
    """龙虎榜 vendor:fetch 返回 list[DragonTigerItem]。市场级(symbols 恒空),按 date 过滤。"""

    pass


class MarginVendor(Vendor):
    """融资融券 vendor:fetch 返回 list[MarginItem]。按 symbol(逐只取最新快照)。"""

    pass


class ShareholdersVendor(Vendor):
    """股东户数 vendor:fetch 返回 list[ShareholderItem]。按 symbol(逐只取最新一期)。"""

    pass


class DividendVendor(Vendor):
    """分红 vendor:fetch 返回 list[DividendItem]。按 symbol(逐只返回全部历史)。"""

    pass


class NorthboundVendor(Vendor):
    """北向资金 vendor:fetch 返回 list[NorthboundItem]。市场级(symbols 可空)。"""

    pass


class MoreInfoVendor(Vendor):
    """扩展指标 vendor:fetch 返回 list[MoreInfo]。按 symbol(批量, TQ get_more_info)。"""

    pass
