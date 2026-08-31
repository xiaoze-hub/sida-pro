"""
thsdk 完整接口封装
====================

基于 thsdk 1.7.18(同花顺官方 Python SDK)
封装 **全部 25+ 个接口能力**,覆盖行情/L2/板块/资讯/选股/财务等

**实测能力(2026-08-19 国内服务器 101.35.244.238 验证)**:
- L2 逐笔 tick_super_level1:4899 行/次,2755ms
- 大单流向 big_order_flow:1703 行/次,38ms
- 20 档盘口 order_book_bid/ask:236ms
- 五档盘口 depth:36ms
- 问财 NLP wencai_nlp:325ms
- K 线 klines:Python 3.12 有 tzinfo bug,需绕开
- 行业列表 ths_industry:90 行
- 概念列表 ths_concept:390 行
- 板块成分股 block_constituents:339 行

**账户模式**:
- 默认游客模式(无需账号,稳定性差)
- 可选正式账户(配 .env:THS_USERNAME/THS_PASSWORD/THS_MAC)

**已知问题**:
- klines 接口在 Python 3.12 报 'tzinfo' bug
- 成交方向字段编码未公开(0, 1, 5, 15, 17, 21, 4294967295)
- 大额成交方向(15/17/21)含义未明
- 游客账户不稳定(临时分配 thsguest_xxx)

**典型用法**:
```python
from data_source.thsdk_l2 import THSDKL2

l2 = THSDKL2()
l2.connect()

# 行情类
quote = l2.get_quote("USZA002361")
depth = l2.get_depth("USZA002361")
bid, ask = l2.get_order_book_20("USZA002361")

# L2 类
ticks = l2.get_l2_ticks("USZA002361")
orders = l2.get_big_orders("USZA002361")

# K 线 / 分时(注意:klines 在 Py3.12 有 bug,推荐用 baostock)
df = l2.get_klines("USZA002361", interval="day", count=10)
intraday = l2.get_intraday("USZA002361")
hist_min = l2.get_min_snapshot("USZA002361", date="20260818")

# 板块/选股
industries = l2.get_ths_industry()
concepts = l2.get_ths_concept()
block = l2.get_block_market("URFI883404", "基础数据")
constituents = l2.get_block_constituents("URFI883404")

# 指数/跨市场
idx = l2.get_market_data_index("USHI000001")
hk = l2.get_market_data_hk("UHKG00700", "基础数据")
us = l2.get_market_data_us("UNQQAAPL", "基础数据")

# 高级扩展
main_flow = l2.get_market_data_cn("USZA002361", "扩展1")  # 主力净流入

# 资讯/AI
news = l2.get_news()
ipo_today = l2.get_ipo_today()
ipo_wait = l2.get_ipo_wait()
wencai = l2.get_wencai_nlp("均线多头排列,MACD金叉,非ST")

# 竞价
auction = l2.get_auction_anomaly("USHA")
tick_3s = l2.get_tick_level1("USZA002361")

# 代码解析
syms = l2.search_symbol("神剑股份")

l2.close()
```

**部署注意**:
- pip install thsdk
- 国内服务器延迟 < 200ms,海外节点可能超时
- 限频 20ms/次,生产需 time.sleep(0.05)
- 必须用 `THS(config) as ths: ...` 上下文模式
"""

import os
os.environ.setdefault("PYTHONUTF8", "1")

import logging
import math
import time
from typing import Optional, Tuple, List, Dict, Any, Union

import pandas as pd

try:
    from thsdk import THS
except ImportError as e:
    raise ImportError(
        "thsdk 未安装。请运行:pip install thsdk\n"
        "Windows 中文环境:$env:PYTHONUTF8='1'; pip install thsdk"
    ) from e

logger = logging.getLogger(__name__)

# 无效数据常量(thsdk 用 uint32 上限表示无效)
THS_INVALID_UINT32 = 4294967295

# DDE 大单资金流 datatype(2026-08-20 实测, 见 SDK examples/dde.py)
# 汇总裁定:主力净流入/主力净量/总金额 等
DDE_DATATYPE_SUMMARY = "6,7,8,9,10,13,19,592888,592890"
# 分档明细:在汇总基础上追加 主动/被动 × 特大单/大单 × 买入/卖出 金额
DDE_DATATYPE_DETAIL = "6,7,8,9,10,13,19,223,224,225,226,227,228,229,230"

# 限频保护(实测:游客模式 50ms 不触发封号,正式账户可能更严)
# 保护策略:
#   - 游客模式:最小 50ms(实测安全)
#   - 正式账户:最小 200ms(保守,避免触发正式账户限频)
THS_RATE_LIMIT_MS = 50
THS_RATE_LIMIT_MS_FORMAL = 200

# 连接超时(实测建连偶尔 2.4 秒,设 5 秒避免误杀)
THS_CONNECT_TIMEOUT = 5

# 单次失败重试次数
THS_MAX_RETRIES = 3
THS_RETRY_BACKOFF_SEC = 1.0

# 失败统计(供高级用户做熔断)
_failure_count = 0
_failure_window_start = 0
FAILURE_THRESHOLD = 10  # 60 秒内连续失败 10 次,触发熔断
FAILURE_WINDOW_SEC = 60
_circuit_open = False
_circuit_open_until = 0

# 问财增强版缓存(30s 进程内,避免重复拉 thsdk)
_WENCAI_CACHE: Dict[str, Tuple[float, pd.DataFrame]] = {}
_WENCAI_TTL = 30.0

# 默认游客模式提示
GUEST_MODE_WARNING = """
⚠️ 当前使用临时游客账户(仅供测试)
⚠️ 临时账户可能随时失效,不适合生产环境使用
⚠️ 建议使用您自己的账户以确保服务稳定性
⚠️ 配置方式:export THS_USERNAME=xxx; export THS_PASSWORD=xxx
"""

# thsdk 代码前缀常量
THS_PREFIX_A_SH = "USZA"   # 深 A
THS_PREFIX_A_SH_H = "USHA"  # 沪 A
THS_PREFIX_BJ = "USTM"     # 北交所
THS_PREFIX_INDEX_SH = "USHI"  # 上证指数
THS_PREFIX_INDEX_SZ = "USZI"  # 深证指数
THS_PREFIX_BLOCK = "URFI"    # 行业/概念板块
THS_PREFIX_HK = "UHKG"       # 港股
THS_PREFIX_US = "UNQQ"       # 美股
THS_PREFIX_FX = "UFXB"       # 外汇
THS_PREFIX_FUTURES = "UCFS"  # 期货


class THSDKL2:
    """thsdk 完整接口封装类(覆盖全部 25+ 个能力)"""

    def __init__(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        mac: Optional[str] = None,
    ):
        """
        初始化 thsdk 数据源

        :param username: 同花顺账号(留空读 env THS_USERNAME)
        :param password: 同花顺密码(留空读 env THS_PASSWORD)
        :param mac: MAC 地址(留空读 env THS_MAC)

        重要:不在 __init__ 里自动连接,thsdk 必须用 with 模式,
        长连接对象在 with 外调用会返回空数据。本封装在每个查询方法
        内部都用 with 上下文,确保连接正确建立/释放。
        """
        self.username = username or os.environ.get("THS_USERNAME")
        self.password = password or os.environ.get("THS_PASSWORD")
        self.mac = mac or os.environ.get("THS_MAC")
        self._is_guest = not (self.username and self.password)

        if self._is_guest:
            logger.warning(GUEST_MODE_WARNING)

    def connect(self):
        """兼容接口(实际无操作,thsdk 连接在查询时按需建立)"""
        logger.debug("THSDKL2 已初始化")

    def close(self):
        """兼容接口(thsdk 连接随 with 退出自动释放)"""
        logger.debug("THSDKL2 已断开")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False

    def _rate_limit(self):
        """限频保护(根据账户模式自动选择间隔)"""
        rate = THS_RATE_LIMIT_MS_FORMAL if not self._is_guest else THS_RATE_LIMIT_MS
        time.sleep(rate / 1000)

    def _check_circuit(self):
        """熔断检查:失败太多则熔断"""
        global _circuit_open, _circuit_open_until
        if _circuit_open:
            if time.time() < _circuit_open_until:
                raise RuntimeError(
                    f"thsdk 熔断中,需等到 {_circuit_open_until - time.time():.0f}s 后"
                    "(连续失败触发保护)"
                )
            _circuit_open = False
            logger.info("thsdk 熔断已恢复")

    def _record_failure(self):
        """记录一次失败"""
        global _failure_count, _failure_window_start, _circuit_open, _circuit_open_until
        now = time.time()
        if now - _failure_window_start > FAILURE_WINDOW_SEC:
            _failure_count = 0
            _failure_window_start = now
        _failure_count += 1
        if _failure_count >= FAILURE_THRESHOLD:
            _circuit_open = True
            _circuit_open_until = now + FAILURE_WINDOW_SEC
            logger.error(
                f"⚠️ thsdk 连续失败 {_failure_count} 次,熔断 {FAILURE_WINDOW_SEC}s"
            )

    def _record_success(self):
        """记录一次成功(重置失败计数)"""
        global _failure_count
        _failure_count = 0

    def _build_config(self) -> dict:
        """构建 thsdk 登录配置"""
        config = {}
        if self.username:
            config["username"] = self.username
        if self.password:
            config["password"] = self.password
        if self.mac:
            config["mac"] = self.mac
        return config

    def _query(self, func_name: str, *args, **kwargs):
        """
        thsdk 通用查询包装器(带限频/重试/熔断)

        重要:thsdk 必须用 `with THS() as ths: ...` 模式,
        长连接对象在 with 外调用会返回空数据。

        保护机制:
        - 限频:游客 50ms / 正式 200ms
        - 重试:失败最多重试 3 次,指数退避
        - 熔断:60s 内失败 10 次自动熔断
        """
        self._check_circuit()
        self._rate_limit()
        config = self._build_config()

        last_err = None
        for attempt in range(THS_MAX_RETRIES):
            try:
                with THS(config) if config else THS() as ths:
                    method = getattr(ths, func_name)
                    result = method(*args, **kwargs)
                    self._record_success()
                    return result
            except Exception as e:
                last_err = e
                self._record_failure()
                if attempt < THS_MAX_RETRIES - 1:
                    backoff = THS_RETRY_BACKOFF_SEC * (2 ** attempt)
                    logger.warning(
                        f"thsdk.{func_name} 第 {attempt+1}/{THS_MAX_RETRIES} 次失败:"
                        f"{str(e)[:60]}, {backoff:.1f}s 后重试"
                    )
                    time.sleep(backoff)

        # 全部重试失败
        raise RuntimeError(
            f"thsdk.{func_name} 失败 {THS_MAX_RETRIES} 次:{str(last_err)[:80]}"
        )

    @staticmethod
    def _clean_invalid(df: pd.DataFrame, col: str = "成交方向") -> pd.DataFrame:
        """过滤无效数据(成交方向 = uint32 上限, 或 None/缺失)。

        2026-08-19 修正: 尾部日期标记行(全字段 None, 时间字段为 20260819 非 epoch 秒)
        的成交方向为 None, 原逻辑 None != 4294967295 会误保留该行,
        导致金额_元 等数值列被 NaN 污染, 一并过滤。
        """
        if df is None or len(df) == 0:
            return df
        if col in df.columns:
            return df[(df[col] != THS_INVALID_UINT32) & (df[col].notna())].copy()
        return df

    @staticmethod
    def _normalize_amount_to_wan(df: pd.DataFrame, col: str = "总金额") -> pd.DataFrame:
        """
        标准化金额单位:thsdk 总金额字段单位是【元】(2026-08-19 实测修正,旧记录误记为"厘")
        保留原始元值列 金额_元,并新增 金额_万元(= 元 ÷ 1e4)

        实测依据(2026-08-19 国内服务器 101.35.244.238):
        - 神剑股份 集合竞价匹配行:成交量 5,357,500 股 × 价格 12.53 = 总金额 67,129,475 元(精确匹配)
        - 贵州茅台 集合竞价匹配行:成交量 31,500 股 × 价格 1300   = 总金额 40,950,000 元(精确匹配)
        - 神剑股份日终:成交量 252,814,870 股,总金额 2,971,296,000 元(与行情快照总手/金额一致)
        注意:成交量单位是【股】,不是手;tick 数据内 成交量/总金额 为当日累计口径,逐笔需相邻行差分还原
        """
        if df is None or len(df) == 0:
            return df
        if col in df.columns:
            df = df.copy()
            df["金额_元"] = df[col].astype(float)
            df["金额_万元"] = df[col].astype(float) / 1e4
        return df

    @staticmethod
    def _to_dataframe(resp) -> pd.DataFrame:
        """
        thsdk Response → DataFrame 统一转换。

        thsdk 返回对象形态多样:
        - 带 `.df` 属性(如 klines / intraday) → 直接返回
        - 带 `.data` 且为 list[dict](如 news / corporate_action / dde) → DataFrame(list)
        - 带 `.data` 且为 dict(单行,如 hs300 单条) → DataFrame([dict])
        - 空 / None → 空 DataFrame

        :param resp: thsdk 查询返回的 Response 对象
        :return: 转换后的 DataFrame
        """
        if resp is None:
            return pd.DataFrame()
        if hasattr(resp, "df") and resp.df is not None:
            return resp.df
        data = getattr(resp, "data", None)
        if isinstance(data, list):
            return pd.DataFrame(data) if data else pd.DataFrame()
        if isinstance(data, dict):
            return pd.DataFrame([data])
        return pd.DataFrame()

    # ========================================================================
    # 第一类:代码解析 & 基础信息
    # ========================================================================

    def search_symbol(self, keyword: str) -> list:
        """
        代码解析:股票名/代码片段 → THSCODE

        :param keyword: 股票名(如 "神剑股份") 或代码片段(如 "002361")
        :return: list of dict,含 THSCODE、Name、MarketDisplay 等
        """
        resp = self._query("search_symbols", keyword)
        return resp.data if hasattr(resp, "data") else []

    # ========================================================================
    # 第二类:行情快照
    # ========================================================================

    def get_quote(self, symbol: str = "USZA002361") -> dict:
        """
        行情快照(A 股基础数据)

        :param symbol: 股票代码(USZA 深 A / USHA 沪 A)
        :return: dict,含最新价、今开、昨收、最高、最低、涨跌幅、总手、金额、换手率
        """
        resp = self._query("market_data_cn", symbol, "基础数据")
        if hasattr(resp, "data") and resp.data:
            return resp.data[0] if isinstance(resp.data, list) else resp.data
        return {}

    def get_market_data_cn(
        self, symbol: str, extended: str = "基础数据"
    ) -> dict:
        """
        A 股行情 - 多档扩展

        :param symbol: 股票代码
        :param extended: 档位名称:
            - "基础数据": 最新价/成交量等
            - "扩展1": 含**主力净流入**(正式账户才解锁,游客返回 0 行)
        :return: dict
        """
        resp = self._query("market_data_cn", symbol, extended)
        if hasattr(resp, "data") and resp.data:
            return resp.data[0] if isinstance(resp.data, list) else resp.data
        return {}

    def get_market_data_index(self, symbol: str = "USHI000001") -> pd.DataFrame:
        """
        指数行情(上证综指 / 深证成指 等)

        :param symbol: 指数代码(USHI000001 上证 / USZI399001 深证)
        :return: DataFrame,含最新点位、涨跌幅、成交额等
        注意:游客账户返回 0 行,需正式账户
        """
        resp = self._query("market_data_index", symbol)
        return self._to_dataframe(resp)

    def get_market_data_hk(
        self, symbol: str = "UHKG00700", extended: str = "基础数据"
    ) -> pd.DataFrame:
        """
        港股行情(腾讯 00700 等)

        :param symbol: 港股代码(UHKG00700 腾讯)
        :param extended: 档位名称(基础数据/扩展1/扩展2)
        :return: DataFrame,含最新价、涨跌幅等
        注意:游客账户返回 0 行,需正式账户
        """
        resp = self._query("market_data_hk", symbol, extended)
        return self._to_dataframe(resp)

    def get_market_data_us(
        self, symbol: str = "UNQQAAPL", extended: str = "基础数据"
    ) -> pd.DataFrame:
        """
        美股行情(苹果 AAPL 等)

        :param symbol: 美股代码(UNQQAAPL 苹果)
        :param extended: 档位名称
        :return: DataFrame,含最新价、涨跌幅等
        """
        resp = self._query("market_data_us", symbol, extended)
        return self._to_dataframe(resp)

    # ========================================================================
    # 第三类:K 线 / 分时
    # ========================================================================

    def get_klines(
        self,
        symbol: str = "USZA002361",
        interval: str = "day",
        count: int = 250,
        adjust: str = "forward",
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
    ) -> pd.DataFrame:
        """
        K 线数据(日 / 周 / 月 / 分钟)

        :param symbol: 股票代码
        :param interval: 周期
            - "day" 日 K
            - "week" 周 K
            - "month" 月 K
            - "5m" 5 分钟(注意:必须 "5m",不是 "5min")
            - "15m" / "30m" / "60m" 其他分钟
        :param count: K 线根数(默认 250)
        :param adjust: 复权方式
            - "forward" 前复权
            - "backward" 后复权
            - "none" 不复权
        :param start_time: 起始时间(可选,ISO 格式,与 count 二选一)
        :param end_time: 结束时间(可选,与 start_time 配合)
        :return: DataFrame(OHLCV 数据)

        ⚠️ 已知问题:thsdk 1.7.18 + Python 3.12 报 'tzinfo' bug
        建议:用 baostock/tushare/wudao 等替代,K 线不是 thsdk 的强项
        """
        kwargs = {"interval": interval, "count": count, "adjust": adjust}
        if start_time:
            kwargs["start_time"] = start_time
            kwargs.pop("count", None)
        if end_time:
            kwargs["end_time"] = end_time

        resp = self._query("klines", symbol, **kwargs)
        return resp.df if hasattr(resp, "df") else pd.DataFrame()

    def get_intraday(self, symbol: str = "USZA002361") -> pd.DataFrame:
        """
        今日分时数据(241 行,每分钟一行)

        :param symbol: 股票代码
        :return: DataFrame,约 241 行,含价格/成交量等
        """
        resp = self._query("intraday_data", symbol)
        return resp.df if hasattr(resp, "df") else pd.DataFrame()

    def get_min_snapshot(self, symbol: str = "USZA002361", date: str = "20260819") -> pd.DataFrame:
        """
        历史某日分时数据

        :param symbol: 股票代码
        :param date: 日期字符串(YYYYMMDD,如 "20260819")
        :return: DataFrame,约 241 行
        """
        resp = self._query("min_snapshot", symbol, date=date)
        return resp.df if hasattr(resp, "df") else pd.DataFrame()

    # ========================================================================
    # 第四类:盘口 / L2 数据
    # ========================================================================

    def get_depth(self, symbol: str = "USZA002361") -> dict:
        """
        五档盘口

        :param symbol: 股票代码
        :return: dict,含买1-5价/量、卖1-5价/量、昨收价
        """
        resp = self._query("depth", symbol)
        if hasattr(resp, "data") and resp.data:
            return resp.data[0] if isinstance(resp.data, list) else resp.data
        return {}

    def get_order_book_20(self, symbol: str = "USZA002361") -> Tuple[pd.DataFrame, pd.DataFrame]:
        """
        买卖 20 档盘口 + 委托队列(同花顺 L2 独家)

        :param symbol: 股票代码
        :return: (bid_df, ask_df),各 20 行,含列:
            orderlevel, price, ordersque(委托笔数数组,L2 独家)
        """
        bid = self._query("order_book_bid", symbol).df
        ask = self._query("order_book_ask", symbol).df
        return bid, ask

    def get_tick_level1(self, symbol: str = "USZA002361") -> pd.DataFrame:
        """
        3 秒 Tick 数据(基础逐笔)

        :param symbol: 股票代码
        :return: DataFrame,约 4898 行
        """
        resp = self._query("tick_level1", symbol)
        df = resp.df if hasattr(resp, "df") else pd.DataFrame()
        df = self._clean_invalid(df)
        df = self._normalize_amount_to_wan(df)
        return df

    def get_l2_ticks(self, symbol: str = "USZA002361") -> pd.DataFrame:
        """
        L2 超级盘口 + 全量逐笔

        :param symbol: 股票代码
        :return: DataFrame,约 4899 行,含 30 列:
            时间, 价格, 成交方向, 成交量, 外盘成交量,
            买1-5价/量, 卖1-5价/量, 交易笔数, 总金额,
            委托买入价, 委托卖出价, 当前量, 五日成交总量, 昨结,
            + 委托队列(ordersque 列,L2 独家)

        注意:
        - 成交方向字段编码未公开(0, 1, 5, 15, 17, 21, 4294967295)
        - 建议用 委托买入价/卖出价 自行判断方向
        - 总金额单位是【元】,成交量单位是【股】(2026-08-19 实测修正,非旧记录的厘/手)
        - 该接口按约 3 秒条返回,成交量/总金额 为当日累计口径,逐笔(区间增量)= 相邻行差分
        """
        resp = self._query("tick_super_level1", symbol)
        df = resp.df if hasattr(resp, "df") else pd.DataFrame()
        df = self._clean_invalid(df)
        df = self._normalize_amount_to_wan(df)
        return df

    def get_big_orders(self, symbol: str = "USZA002361") -> pd.DataFrame:
        """
        大单流向(官方主买/主卖标签)

        :param symbol: 股票代码
        :return: DataFrame,约 1703 行,含列:
            时间, 成交方向, 成交量, 总金额,
            委托买入价, 委托卖出价

        成交方向含义(实测):
        - 1 = 主买(?)
        - 5 = 主卖(?)
        - 15/17/21 = 大额成交(可能含暗盘标记)
        - 0 = 中性/集合竞价
        - 4294967295 = 无效

        注意:
        - 字段含义未经官方确认
        - 不建议直接用 1-5 差作为"主力净额"
        - 需要更多样本对照同花顺/东财验证
        """
        resp = self._query("big_order_flow", symbol)
        df = resp.df if hasattr(resp, "df") else pd.DataFrame()
        df = self._clean_invalid(df)
        df = self._normalize_amount_to_wan(df)
        return df

    # ========================================================================
    # 第五类:竞价 / 异动
    # ========================================================================

    def get_auction_anomaly(self, market: str = "USHA") -> pd.DataFrame:
        """
        集合竞价异动数据

        :param market: 市场代码
            - "USHA" 沪 A
            - "USZA" 深 A
            - "USTM" 北交所
        :return: DataFrame,约 1000 行,含竞价异动股票
        """
        resp = self._query("call_auction_anomaly", market)
        return resp.df if hasattr(resp, "df") else pd.DataFrame()

    # ========================================================================
    # 第六类:板块 / 概念 / 选股
    # ========================================================================

    def get_ths_industry(self) -> pd.DataFrame:
        """
        同花顺行业列表(约 90 个)

        :return: DataFrame,行业代码 + 名称
        """
        resp = self._query("ths_industry")
        return resp.df if hasattr(resp, "df") else pd.DataFrame()

    def get_ths_concept(self) -> pd.DataFrame:
        """
        同花顺概念列表(约 390 个)

        :return: DataFrame,概念代码 + 名称
        """
        resp = self._query("ths_concept")
        return resp.df if hasattr(resp, "df") else pd.DataFrame()

    def get_block_market(
        self, block_code: str = "URFI883404", extended: str = "基础数据"
    ) -> dict:
        """
        板块行情数据

        :param block_code: 板块代码(URFI 开头,如 "URFI883404")
        :param extended: 档位名称(基础数据/扩展1)
        :return: dict,板块当前行情
        """
        resp = self._query("market_data_block", block_code, extended)
        if hasattr(resp, "data") and resp.data:
            return resp.data[0] if isinstance(resp.data, list) else resp.data
        return {}

    def get_block_constituents(self, block_code: str = "URFI883404") -> pd.DataFrame:
        """
        板块成分股列表

        :param block_code: 板块代码
        :return: DataFrame,约 339 行,含成分股代码和名称
        """
        resp = self._query("block_constituents", block_code)
        return resp.df if hasattr(resp, "df") else pd.DataFrame()

    # ========================================================================
    # 第七类:资讯 / IPO / 问财
    # ========================================================================

    def get_news(
        self, symbol: Optional[str] = None, text_id: int = 0x3814
    ) -> pd.DataFrame:
        """
        实时财经资讯(约 20 条/次)

        :param symbol: thsdk 股票代码(如 USZA002361)。传 None 拉全局财经资讯;
            传代码则按该标的拉个股新闻。
        :param text_id: 资讯频道 ID(默认 0x3814 个股新闻频道)。
        :return: DataFrame,含新闻标题、时间、来源、链接等
        """
        if symbol:
            resp = self._query("news", symbol, text_id)
        else:
            resp = self._query("news")
        return self._to_dataframe(resp)

    def get_ipo_today(self) -> pd.DataFrame:
        """
        今日 IPO 列表

        :return: DataFrame,当日上市/申购的新股
        """
        resp = self._query("ipo_today")
        return resp.df if hasattr(resp, "df") else pd.DataFrame()

    def get_ipo_wait(self) -> pd.DataFrame:
        """
        待发行 IPO 列表

        :return: DataFrame,未来待发行的新股
        """
        resp = self._query("ipo_wait")
        return resp.df if hasattr(resp, "df") else pd.DataFrame()

    def get_wencai_nlp(self, query: str = "均线多头排列,MACD金叉,非ST") -> pd.DataFrame:
        """
        问财 NLP 自然语言选股(同花顺 AI 选股)

        :param query: 自然语言查询,如:
            - "均线多头排列,MACD金叉,非ST"
            - "主力净流入由大到小排名前20,非ST"
            - "今日涨停,非ST,创业板"
            - "神剑股份昨日龙虎榜买入卖出营业部" (2026-08-20 实测可用)
        :return: DataFrame,命中的股票列表

        注意:返回代码为 300033.SZ 格式,需转换为 USZA300033 再查行情

        2026-08-20 修复: thsdk Response 没 .df 属性, 直接用 .data(原始 list[dict])
        """
        resp = self._query("wencai_nlp", query)
        data = getattr(resp, "data", None)
        if isinstance(data, list):
            return pd.DataFrame(data)
        if isinstance(data, dict):
            return pd.DataFrame([data])
        return pd.DataFrame()

    # ========================================================================
    # 第七点五类:高价值待接能力落地(v0.3.1 目标 A)
    # 依赖 thsdk 1.7.18 examples: news / corporate_action / dde / hs300 /
    #   market_data_cn(扩展1) / market_data_index / market_data_hk / market_data_us /
    #   market_data_bond / market_data_fund / wencai_nlp(增强)
    # 注意:market_data_cn(扩展1)/index/hk 对游客账户返 0 行,代码/路由建好等正式账户解锁
    # ========================================================================

    def get_corporate_action(self, symbol: str = "USZA002361") -> pd.DataFrame:
        """
        公司行动(分红/送转/配股等)

        :param symbol: thsdk 股票代码(USZA 深 A / USHA 沪 A)
        :return: DataFrame,含分红、送转、配股等公司行动记录
        """
        resp = self._query("corporate_action", symbol)
        return self._to_dataframe(resp)

    def get_dde(self, symbol: str = "USZA002361") -> pd.DataFrame:
        """
        DDE 大单动向(同花顺 Level-2 DDE 指标)

        :param symbol: thsdk 股票代码
        :return: DataFrame,含 DDE 大单净量、DDX/DDY/DDZ 等大单动向指标
        """
        resp = self._query("dde", symbol)
        return self._to_dataframe(resp)

    def get_hs300_constituents(self) -> pd.DataFrame:
        """
        沪深 300 成分股列表

        :return: DataFrame,沪深 300 全部成分股代码 + 名称
        """
        resp = self._query("hs300")
        return self._to_dataframe(resp)

    def get_market_data_cn_extended(
        self, symbol: str = "USZA002361", extended: str = "扩展1"
    ) -> pd.DataFrame:
        """
        A 股行情 - 扩展档(含**主力净流入**)

        :param symbol: thsdk 股票代码
        :param extended: 档位名称:
            - "基础数据": 最新价/成交量等(与 get_market_data_cn 同源)
            - "扩展1": 含**主力净流入**(正式账户才解锁,游客返 0 行)
        :return: DataFrame,含主力净流入等扩展字段
        """
        resp = self._query("market_data_cn", symbol, extended)
        return self._to_dataframe(resp)

    def get_market_data_bond(self, symbol: str = "USBK113550") -> pd.DataFrame:
        """
        可转债行情

        :param symbol: 可转债 thsdk 代码(前缀 USBK,如 113550)
        :return: DataFrame,含最新价、涨跌幅等可转债行情
        """
        resp = self._query("market_data_bond", symbol)
        return self._to_dataframe(resp)

    def get_market_data_fund(self, symbol: str = "USZF510300") -> pd.DataFrame:
        """
        基金 / ETF 行情

        :param symbol: 基金/ETF thsdk 代码(前缀 USZF,如 510300 沪深300ETF)
        :return: DataFrame,含最新价、涨跌幅等基金行情
        """
        resp = self._query("market_data_fund", symbol)
        return self._to_dataframe(resp)

    def get_wencai_enhanced(self, query: str, use_cache: bool = True) -> pd.DataFrame:
        """
        问财 NLP 增强版(带 30s 进程内缓存)

        :param query: 自然语言查询条件(与 get_wencai_nlp 同源)
        :param use_cache: 是否启用 30s 缓存(默认 True,同 query 30s 内不重复拉 thsdk)
        :return: DataFrame,命中的股票列表
        """
        global _WENCAI_CACHE
        q = (query or "").strip()
        if use_cache:
            now = time.time()
            cached = _WENCAI_CACHE.get(q)
            if cached and (now - cached[0]) < _WENCAI_TTL:
                logger.debug(f"[wencai-enhanced] 缓存命中 query={q!r}")
                return cached[1]
        df = self._to_dataframe(self._query("wencai_nlp", q))
        if use_cache:
            _WENCAI_CACHE[q] = (time.time(), df)
        return df

    # ========================================================================
    # 第八类:高级分析(自实现)
    # ========================================================================

    @staticmethod
    def infer_direction(row: pd.Series) -> str:
        """
        用 成交价 + 委托买卖价 自判断方向(替代 thsdk 未知编码)

        :param row: DataFrame 单行,需含 价格/委托买入价/委托卖出价
        :return: 'buy' / 'sell' / 'neutral' / 'invalid'
        """
        tick = row.get("价格", 0)
        bid = row.get("委托买入价", 0)
        ask = row.get("委托卖出价", 0)

        if bid in (THS_INVALID_UINT32, 0) or ask in (THS_INVALID_UINT32, 0):
            return "invalid"
        if tick >= ask:
            return "buy"  # 吃卖一 → 主动买入
        elif tick <= bid:
            return "sell"  # 吃买一 → 主动卖出
        else:
            return "neutral"  # 撮合成交

    def compute_main_flow(
        self, symbol: str, amount_threshold_yuan: float = 1_000_000.0
    ) -> Dict[str, Any]:
        """
        主买主卖净额(基于自判断方向,不依赖 thsdk 成交方向字段)

        :param symbol: 股票代码
        :param amount_threshold_yuan: 大单阈值(元),默认 100 万
            (2026-08-19 单位修正:thsdk 总金额为元口径,阈值改按元比较,不再用万元)
        :return: dict:
            {
                'symbol': str,
                'total_ticks': int,
                'valid_ticks': int,
                'main_buy_wan': float,   # 主动买入总额(万元)
                'main_sell_wan': float,  # 主动卖出总额(万元)
                'net_wan': float,        # 净额(万元)
                'big_buy_wan': float,    # 大单买入(万元)
                'big_sell_wan': float,   # 大单卖出(万元)
                'big_net_wan': float,    # 大单净额(万元)
            }

        注意:这是"明盘资金"口径(对齐东方财富主力净流入)
        不等于同花顺"暗盘资金"

        修复 2026-08-21(国内生产实测): get_l2_ticks 的 总金额/成交量 是**当日累计**
        口径(约 3 秒条), 直接 sum() 会把净额放大上千倍(神剑实测 -2144亿 vs 腾讯
        -1.57亿, 放大 1363 倍)。现改为**相邻行差分**还原区间增量后再汇总,
        与 src.core.dark_l2.fetch_l2_ticks 同一套差分逻辑。
        """
        ticks = self.get_l2_ticks(symbol)
        if len(ticks) == 0:
            return {"symbol": symbol, "error": "no_data"}

        ticks["真方向"] = ticks.apply(self.infer_direction, axis=1)
        valid = ticks[ticks["真方向"].isin(["buy", "sell"])].copy()
        if len(valid) == 0:
            return {
                "symbol": symbol,
                "total_ticks": len(ticks),
                "valid_ticks": 0,
                "main_buy_wan": 0.0,
                "main_sell_wan": 0.0,
                "net_wan": 0.0,
                "big_buy_wan": 0.0,
                "big_sell_wan": 0.0,
                "big_net_wan": 0.0,
            }

        # 日累计 → 区间增量差分(与 dark_l2.fetch_l2_ticks 一致):
        # 按原始行序(时间升序)对 总金额 做相邻差分, 得到每个 3 秒条的成交额增量。
        # 注意必须用全量行(含中性/无效方向行)做差分基准, 否则累计值断档。
        amt_series = ticks["金额_元"].astype(float).reset_index(drop=True)
        delta_amt = amt_series.diff().fillna(amt_series.iloc[0]).clip(lower=0.0)
        ticks = ticks.reset_index(drop=True)
        ticks["_delta_amt_yuan"] = delta_amt

        valid = ticks[ticks["真方向"].isin(["buy", "sell"])]
        buy_total_wan = float(valid[valid["真方向"] == "buy"]["_delta_amt_yuan"].sum()) / 1e4
        sell_total_wan = float(valid[valid["真方向"] == "sell"]["_delta_amt_yuan"].sum()) / 1e4

        # 大单阈值按元口径比较(单条增量为该 3 秒条的成交额)
        big = valid[valid["_delta_amt_yuan"] >= amount_threshold_yuan]
        big_buy_wan = float(big[big["真方向"] == "buy"]["_delta_amt_yuan"].sum()) / 1e4
        big_sell_wan = float(big[big["真方向"] == "sell"]["_delta_amt_yuan"].sum()) / 1e4

        return {
            "symbol": symbol,
            "total_ticks": len(ticks),
            "valid_ticks": len(valid),
            "main_buy_wan": round(buy_total_wan, 2),
            "main_sell_wan": round(sell_total_wan, 2),
            "net_wan": round(buy_total_wan - sell_total_wan, 2),
            "big_buy_wan": round(big_buy_wan, 2),
            "big_sell_wan": round(big_sell_wan, 2),
            "big_net_wan": round(big_buy_wan - big_sell_wan, 2),
        }

    def get_comprehensive_snapshot(self, symbol: str) -> Dict[str, Any]:
        """
        一站式综合快照:拉取单只股票的所有可用数据

        :param symbol: 股票代码
        :return: dict,含:
            - quote: 行情快照
            - depth: 五档盘口
            - order_book_20: 20 档盘口(只返回行数,数据量大)
            - intraday: 今日分时
            - main_flow: 主买主卖净额
            - timestamp: 拉取时间
        """
        import datetime

        return {
            "symbol": symbol,
            "timestamp": datetime.datetime.now().isoformat(),
            "quote": self.get_quote(symbol),
            "depth": self.get_depth(symbol),
            "order_book_20_rows": len(self.get_order_book_20(symbol)[0]),
            "intraday_rows": len(self.get_intraday(symbol)),
            "main_flow": self.compute_main_flow(symbol),
        }

    # ========================================================================
    # 第九类:DDE 主力资金 + 代码补齐 + 市场代码表(v0.3.1 选项B 新增)
    # ========================================================================

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        """将 thsdk 数值安全转为 float,无效/NaN 返回 None(便于 JSON 序列化)。"""
        try:
            f = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(f):
            return None
        return f

    @staticmethod
    def _amount_cols_to_wan(df: pd.DataFrame, cols: List[str]) -> pd.DataFrame:
        """把指定【元】金额列复制为新列 <col>_万元。"""
        if df is None or len(df) == 0:
            return df
        df = df.copy()
        for c in cols:
            if c in df.columns:
                df[f"{c}_万元"] = df[c].astype(float) / 1e4
        return df

    def get_dde_flow(
        self,
        codelist: str,
        market: str = "USHA",
        detail: bool = False,
    ) -> pd.DataFrame:
        """
        同花顺 DDE 大单资金流向(官方主力资金)

        底层走 `query_data`(thsdk 未封装为类方法, 见 SDK examples/dde.py)。
        实测字段(2026-08-20 神剑/茅台/平安):
        - summary(detail=False): 价格, 成交量, 总金额, 代码, 主力净量, 主力净流入,
          昨收价, 开盘价, 最高价, 最低价
        - detail(detail=True): 追加 主动/被动 × 特大单/大单 × 买入/卖出 金额
          (主动买入特大单金额, 主动卖出特大单金额, 主动买入大单金额, 主动卖出大单金额,
           被动买入特大单金额, 被动卖出特大单金额, 被动买入大单金额, 被动卖出大单金额)

        :param codelist: 同市场 6 位代码, 逗号分隔(如 "600519,601318"); 传 THS 代码自动剥前缀
        :param market: 市场前缀(USHA/USZA/USTM...), codelist 需同属该市场
        :param detail: True 返回分档明细, False 返回汇总(含 主力净流入/主力净量)
        :return: DataFrame
        """
        codes = [c.strip() for c in codelist.split(",") if c.strip()]
        # 兼容 THS 代码(USHA600519 → 600519)
        if codes and codes[0][:4] in ("USHA", "USZA", "USTM", "UNQQ", "UHKG"):
            codes = [c[4:] for c in codes]
        datatype = DDE_DATATYPE_DETAIL if detail else DDE_DATATYPE_SUMMARY
        params = {
            "id": 200,
            "codelist": ",".join(codes),
            "market": market,
            "datatype": datatype,
            "service": "zhu",
        }
        resp = self._query("query_data", params)
        df = resp.df if hasattr(resp, "df") else pd.DataFrame()
        if len(df) == 0:
            return df
        amount_cols = [
            "总金额",
            "主力净流入",
            "主动买入特大单金额", "主动卖出特大单金额",
            "主动买入大单金额", "主动卖出大单金额",
            "被动买入特大单金额", "被动卖出特大单金额",
            "被动买入大单金额", "被动卖出大单金额",
        ]
        df = self._amount_cols_to_wan(df, amount_cols)
        return df

    def get_main_flow_official(self, symbol: str) -> Dict[str, Any]:
        """
        单只股票 同花顺官方主力资金(汇总 + 分档明细)

        :param symbol: 6 位 A 股代码或 THS 代码
        :return: dict:
            {
                'symbol', 'ths_code', 'price',
                'total_amount_wan',      # 总成交额(万元)
                'main_net_amount_wan',   # 主力净流入(万元), 同花顺官方口径
                'main_net_ratio',        # 主力净量(占比)
                'summary': {...},        # 汇总行
                'detail': {...},         # 特大单/大单 主动/被动 明细
            }
        """
        codes = self.complete_ths_code(symbol)
        if not codes:
            return {"symbol": symbol, "error": "no_code"}
        ths_code = codes[0]
        market = ths_code[:4]
        six = ths_code[4:]

        summary = self.get_dde_flow(six, market=market, detail=False)
        detail = self.get_dde_flow(six, market=market, detail=True)
        row = summary.iloc[0].to_dict() if len(summary) else {}
        prow = detail.iloc[0].to_dict() if len(detail) else {}

        total_wan = self._to_float(row.get("总金额"))
        main_net_wan = self._to_float(row.get("主力净流入"))

        return {
            "symbol": symbol,
            "ths_code": ths_code,
            "price": self._to_float(row.get("价格")),
            "total_amount_wan": total_wan / 1e4 if total_wan is not None else None,
            "main_net_amount_wan": main_net_wan / 1e4 if main_net_wan is not None else None,
            "main_net_ratio": self._to_float(row.get("主力净量")),
            "summary": row,
            "detail": prow,
        }

    def complete_ths_code(self, codes: Union[str, List[str]]) -> List[str]:
        """
        补齐任意证券代码为标准 THS 代码(A/HK/US 等)

        实测(2026-08-20):
        - complete_ths_code('002361') → ['USZA002361']
        - complete_ths_code(['300033','600519','TSLA']) → ['USZA300033','USHA600519','UNQQTSLA']
        ⚠️ 指数代码(如 '1A0001')不返回, 需自行处理

        :param codes: 单个代码(字符串) 或 代码列表
        :return: 补齐后的 THS 代码列表
        """
        resp = self._query("complete_ths_code", codes)
        df = resp.df if hasattr(resp, "df") else pd.DataFrame()
        if len(df) and "代码" in df.columns:
            return df["代码"].astype(str).tolist()
        data = getattr(resp, "data", None)
        if isinstance(data, list):
            return [d.get("代码", "") for d in data if isinstance(d, dict)]
        return []

    def get_market_codes(self, market: str = "USHA") -> pd.DataFrame:
        """
        市场代码全量列表(代码 + 名称)

        实测(2026-08-20): USHA 2235 行 / USZA 2901 行, 列 ['代码','名称']
        :param market: 市场前缀(USHA 沪 / USZA 深 / USTM 北 / UNQS 美股纳指等)
        :return: DataFrame, 列 ['代码','名称']
        """
        resp = self._query("market_block", market)
        return resp.df if hasattr(resp, "df") else pd.DataFrame()

    def get_stock_cn_lists(self) -> pd.DataFrame:
        """
        A 股全市场代码列表(5220 行)

        实测(2026-08-20): 列 ['代码','名称']
        :return: DataFrame
        """
        resp = self._query("stock_cn_lists")
        return resp.df if hasattr(resp, "df") else pd.DataFrame()


# ========================================================================
# 便捷函数(函数式 API)
# ========================================================================

_default_client: Optional[THSDKL2] = None


def _get_default_client() -> THSDKL2:
    """获取默认客户端(单例)"""
    global _default_client
    if _default_client is None:
        _default_client = THSDKL2()
    return _default_client


# 第一类:基础信息
def search_symbol(keyword: str) -> list:
    return _get_default_client().search_symbol(keyword)


# 第二类:行情
def get_quote(symbol: str = "USZA002361") -> dict:
    return _get_default_client().get_quote(symbol)


def get_market_data_cn(symbol: str, extended: str = "基础数据") -> dict:
    return _get_default_client().get_market_data_cn(symbol, extended)


def get_market_data_index(symbol: str = "USHI000001") -> pd.DataFrame:
    return _get_default_client().get_market_data_index(symbol)


def get_market_data_hk(symbol: str = "UHKG00700", extended: str = "基础数据") -> pd.DataFrame:
    return _get_default_client().get_market_data_hk(symbol, extended)


def get_market_data_us(symbol: str = "UNQQAAPL", extended: str = "基础数据") -> pd.DataFrame:
    return _get_default_client().get_market_data_us(symbol, extended)


# 第三类:K 线 / 分时
def get_klines(
    symbol: str = "USZA002361",
    interval: str = "day",
    count: int = 250,
    adjust: str = "forward",
) -> pd.DataFrame:
    return _get_default_client().get_klines(symbol, interval, count, adjust)


def get_intraday(symbol: str = "USZA002361") -> pd.DataFrame:
    return _get_default_client().get_intraday(symbol)


def get_min_snapshot(symbol: str = "USZA002361", date: str = "20260819") -> pd.DataFrame:
    return _get_default_client().get_min_snapshot(symbol, date)


# 第四类:盘口 / L2
def get_depth(symbol: str = "USZA002361") -> dict:
    return _get_default_client().get_depth(symbol)


def get_order_book_20(symbol: str = "USZA002361") -> Tuple[pd.DataFrame, pd.DataFrame]:
    return _get_default_client().get_order_book_20(symbol)


def get_tick_level1(symbol: str = "USZA002361") -> pd.DataFrame:
    return _get_default_client().get_tick_level1(symbol)


def get_l2_ticks(symbol: str = "USZA002361") -> pd.DataFrame:
    return _get_default_client().get_l2_ticks(symbol)


def get_big_orders(symbol: str = "USZA002361") -> pd.DataFrame:
    return _get_default_client().get_big_orders(symbol)


# 第五类:竞价
def get_auction_anomaly(market: str = "USHA") -> pd.DataFrame:
    return _get_default_client().get_auction_anomaly(market)


# 第六类:板块
def get_ths_industry() -> pd.DataFrame:
    return _get_default_client().get_ths_industry()


def get_ths_concept() -> pd.DataFrame:
    return _get_default_client().get_ths_concept()


def get_block_market(block_code: str = "URFI883404", extended: str = "基础数据") -> dict:
    return _get_default_client().get_block_market(block_code, extended)


def get_block_constituents(block_code: str = "URFI883404") -> pd.DataFrame:
    return _get_default_client().get_block_constituents(block_code)


# 第七类:资讯 / IPO / 问财
def get_news(symbol: Optional[str] = None, text_id: int = 0x3814) -> pd.DataFrame:
    return _get_default_client().get_news(symbol, text_id)


def get_ipo_today() -> pd.DataFrame:
    return _get_default_client().get_ipo_today()


def get_ipo_wait() -> pd.DataFrame:
    return _get_default_client().get_ipo_wait()


def get_wencai_nlp(query: str = "均线多头排列,MACD金叉,非ST") -> pd.DataFrame:
    return _get_default_client().get_wencai_nlp(query)


# 第七点五类:高价值待接能力(v0.3.1 目标 A)
def get_corporate_action(symbol: str = "USZA002361") -> pd.DataFrame:
    return _get_default_client().get_corporate_action(symbol)


def get_dde(symbol: str = "USZA002361") -> pd.DataFrame:
    return _get_default_client().get_dde(symbol)


def get_hs300_constituents() -> pd.DataFrame:
    return _get_default_client().get_hs300_constituents()


def get_market_data_cn_extended(
    symbol: str = "USZA002361", extended: str = "扩展1"
) -> pd.DataFrame:
    return _get_default_client().get_market_data_cn_extended(symbol, extended)


def get_market_data_bond(symbol: str = "USBK113550") -> pd.DataFrame:
    return _get_default_client().get_market_data_bond(symbol)


def get_market_data_fund(symbol: str = "USZF510300") -> pd.DataFrame:
    return _get_default_client().get_market_data_fund(symbol)


def get_wencai_enhanced(query: str, use_cache: bool = True) -> pd.DataFrame:
    return _get_default_client().get_wencai_enhanced(query, use_cache)


# 第八类:高级分析
def compute_main_flow(symbol: str, amount_threshold_yuan: float = 1_000_000.0) -> dict:
    return _get_default_client().compute_main_flow(symbol, amount_threshold_yuan)


def get_comprehensive_snapshot(symbol: str) -> dict:
    return _get_default_client().get_comprehensive_snapshot(symbol)


# 第九类:DDE 主力资金 + 代码补齐 + 市场代码表
def get_dde_flow(codelist: str, market: str = "USHA", detail: bool = False) -> pd.DataFrame:
    return _get_default_client().get_dde_flow(codelist, market, detail)


def get_main_flow_official(symbol: str) -> dict:
    return _get_default_client().get_main_flow_official(symbol)


def complete_ths_code(codes) -> list:
    return _get_default_client().complete_ths_code(codes)


def get_market_codes(market: str = "USHA") -> pd.DataFrame:
    return _get_default_client().get_market_codes(market)


def get_stock_cn_lists() -> pd.DataFrame:
    return _get_default_client().get_stock_cn_lists()


# ========================================================================
# 复刻同花顺暗盘 - 实验模块(状态:实验性,未经验证)
# ========================================================================


def identify_split_orders_v6(
    ticks: pd.DataFrame,
    window_sec: int = 30,
    min_count: int = 5,
    min_total_wan: float = 50.0,
    price_tol: float = 0.01,
) -> list:
    """
    拆单识别算法(实验性,未对齐同花顺暗盘)

    思路:
    - 时间窗口内,同方向 + 同价位 + N 笔聚合 = 疑似"主力拆单"

    实测结论:
    - 多氟多:算 +151745 万 vs 目标 +28125 万(差 4.4 倍)
    - 神农种业:算 +28122 万 vs 目标 +10482 万(差 1.7 倍)
    - 不能 1:1 复刻同花顺暗盘
    """
    if len(ticks) == 0:
        return []

    ticks = ticks.sort_values("时间").reset_index(drop=True)
    ticks["真方向"] = ticks.apply(THSDKL2.infer_direction, axis=1)
    ticks = ticks[ticks["真方向"].isin(["buy", "sell"])].copy()

    if len(ticks) == 0:
        return []

    splits = []
    used = set()

    for i in range(len(ticks)):
        if i in used:
            continue
        row = ticks.iloc[i]
        t_start = row["时间"]
        p_center = row["价格"]
        direction = row["真方向"]

        cluster = [i]
        for j in range(i + 1, len(ticks)):
            if j in used:
                continue
            if ticks.iloc[j]["时间"] - t_start > window_sec:
                break
            if ticks.iloc[j]["真方向"] != direction:
                continue
            if abs(ticks.iloc[j]["价格"] - p_center) > price_tol:
                continue
            cluster.append(j)

        if len(cluster) >= min_count:
            total_wan = ticks.iloc[cluster]["金额_万元"].sum()
            if total_wan >= min_total_wan:
                splits.append(
                    {
                        "direction": direction,
                        "time": t_start,
                        "price": p_center,
                        "count": len(cluster),
                        "total_wan": total_wan,
                    }
                )
                used.update(cluster)

    return splits


# ========================================================================
# 演示 / 自测
# ========================================================================

if __name__ == "__main__":
    print("=" * 70)
    print("thsdk 完整接口封装演示(覆盖全部 25+ 个能力)")
    print("=" * 70)

    SYMBOL = "USZA002361"  # 神剑股份

    l2 = THSDKL2()
    l2.connect()

    print(f"\n【第一类】代码解析")
    syms = l2.search_symbol("神剑股份")
    if syms:
        print(f"  神剑股份: {syms[0]}")

    print(f"\n【第二类】行情快照({SYMBOL})")
    quote = l2.get_quote(SYMBOL)
    for k in ["最新价", "涨跌幅", "总手", "金额"]:
        if k in quote:
            print(f"  {k}: {quote[k]}")

    print(f"\n【第二类】指数行情(上证综指)")
    idx = l2.get_market_data_index("USHI000001")
    print(f"  返回: {len(idx)} 行" + (f"\n{idx.head(2)}" if len(idx) else " (游客受限)"))

    print(f"\n【第二类】港股(腾讯 00700)")
    hk = l2.get_market_data_hk("UHKG00700", "基础数据")
    print(f"  返回: {len(hk)} 行" + (f"\n{hk.head(2)}" if len(hk) else " (游客受限)"))

    print(f"\n【第二类】美股(苹果 AAPL)")
    us = l2.get_market_data_us("UNQQAAPL", "基础数据")
    print(f"  返回: {len(us)} 行" + (f"\n{us.head(2)}" if len(us) else " (游客受限)"))

    print(f"\n【第三类】K 线 / 分时")
    try:
        klines = l2.get_klines(SYMBOL, interval="day", count=5)
        print(f"  日 K: {len(klines)} 行")
        if len(klines) > 0:
            print(klines.head(2))
    except Exception as e:
        print(f"  K 线 ⚠️ {type(e).__name__}:{str(e)[:60]}")

    intraday = l2.get_intraday(SYMBOL)
    print(f"  今日分时: {len(intraday)} 行")

    hist = l2.get_min_snapshot(SYMBOL, date="20260818")
    print(f"  历史分时(8/18): {len(hist)} 行")

    print(f"\n【第四类】盘口 / L2")
    depth = l2.get_depth(SYMBOL)
    for k in ["买1价", "买1量", "卖1价", "卖1量"]:
        if k in depth:
            print(f"  {k}: {depth[k]}")

    bid, ask = l2.get_order_book_20(SYMBOL)
    print(f"  20 档盘口: 买{len(bid)} 档 / 卖{len(ask)} 档")

    tick_3s = l2.get_tick_level1(SYMBOL)
    print(f"  3 秒 Tick: {len(tick_3s)} 行")

    l2_ticks = l2.get_l2_ticks(SYMBOL)
    print(f"  L2 逐笔: {len(l2_ticks)} 行")

    big_orders = l2.get_big_orders(SYMBOL)
    print(f"  大单流向: {len(big_orders)} 行")

    print(f"\n【第五类】竞价异动")
    auction = l2.get_auction_anomaly("USHA")
    print(f"  沪 A 竞价异动: {len(auction)} 条")

    print(f"\n【第六类】板块 / 概念 / 选股")
    industries = l2.get_ths_industry()
    print(f"  行业列表: {len(industries)} 个")
    if len(industries) > 0:
        print(industries.head(2))

    concepts = l2.get_ths_concept()
    print(f"  概念列表: {len(concepts)} 个")

    block = l2.get_block_market("URFI883404", "基础数据")
    print(f"  板块行情: {block.get('代码', 'N/A')}")

    constituents = l2.get_block_constituents("URFI883404")
    print(f"  板块成分股: {len(constituents)} 只")

    print(f"\n【第七类】资讯 / IPO / 问财")
    news = l2.get_news()
    print(f"  实时资讯: {len(news)} 条")

    ipo_today = l2.get_ipo_today()
    print(f"  今日 IPO: {len(ipo_today)} 只")

    ipo_wait = l2.get_ipo_wait()
    print(f"  待发 IPO: {len(ipo_wait)} 只")

    wencai = l2.get_wencai_nlp("均线多头排列,MACD金叉,非ST")
    print(f"  问财选股: {len(wencai)} 只")
    if len(wencai) > 0:
        print(wencai.head(2))

    print(f"\n【第八类】高级分析 - 主买主卖净额")
    flow = l2.compute_main_flow(SYMBOL, amount_threshold_yuan=1_000_000)
    for k, v in flow.items():
        if isinstance(v, float):
            print(f"  {k}: {v:,.0f}")
        else:
            print(f"  {k}: {v}")

    print(f"\n【第八类】一站式综合快照")
    snapshot = l2.get_comprehensive_snapshot(SYMBOL)
    print(f"  symbol: {snapshot['symbol']}")
    print(f"  timestamp: {snapshot['timestamp']}")
    print(f"  depth keys: {list(snapshot['depth'].keys())[:5]}")
    print(f"  main_flow: 大单净额 {snapshot['main_flow'].get('big_net_wan', 0):,.0f} 万")

    l2.close()

    print("\n" + "=" * 70)
    print("✅ 全部 25+ 个接口演示完成")
    print("=" * 70)