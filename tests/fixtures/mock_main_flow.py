"""主力意图双源对比 / 竞价异动池 测试的 mock 数据与桩模块构造(阶段1.1/1.2, v0.3.0)。

thsdk 在本环境未安装, 但 main_flow_compare / auction_pool 在函数内懒加载
`data_source.thsdk_l2`。测试通过 monkeypatch.setitem(sys.modules, ...) 注入
一个内存桩模块(带可 mock 的 compute_main_flow / get_auction_anomaly), 从而
在无 thsdk 的环境验证双源对比与异动池转换逻辑。
"""
from __future__ import annotations

import sys
import types

import pandas as pd

# ── 腾讯逐笔(compute_dark_flow) mock 返回值 ─────────────────────────────
TENCENT_OK = {
    "main_net": 5_000_000,          # 元 (>0: 净流入)
    "big_net": 3_000_000,
    "mid_net": 2_000_000,
    "small_net": -1_000_000,
    "signal": "主力净流入, 疑似吸筹",
    "tick_count": 1200,
    "data_status": "ok",
}

TENCENT_INSUFFICIENT = {"main_net": 0, "big_net": 0, "small_net": 0,
                        "tick_count": 5, "data_status": "insufficient"}

# ── thsdk compute_main_flow mock 返回值(净额单位为万元) ──────────────────
# 与 TENCENT_OK 完全一致的 thsdk 口径: main_net=5,000,000 元 -> net_wan=500.0
THSDK_SAME = {
    "symbol": "USZA002361",
    "total_ticks": 1800,
    "valid_ticks": 1600,
    "main_buy_wan": 1500.0,
    "main_sell_wan": 1000.0,
    "net_wan": 500.0,
    "big_buy_wan": 800.0,
    "big_sell_wan": 500.0,
    "big_net_wan": 300.0,
}

# 与腾讯完全相反: 腾讯 +500 万, thsdk -500 万(net_wan=-500.0)
THSDK_OPPOSITE = {**THSDK_SAME, "net_wan": -500.0, "big_net_wan": -300.0,
                  "main_sell_wan": 1500.0, "main_buy_wan": 1000.0}

# 净额都为 0
THSDK_ZERO = {**THSDK_SAME, "net_wan": 0.0, "big_net_wan": 0.0}

# 数据源不可用(thsdk 返回 no_data)
THSDK_NO_DATA = {"symbol": "USZA002361", "error": "no_data"}


def fake_thsdk_l2_module():
    """构造一个 data_source.thsdk_l2 内存桩模块(可再 patch 其函数)。

    用法:
        from tests.fixtures import mock_main_flow as mmf
        monkeypatch.setitem(sys.modules, "data_source.thsdk_l2",
                            mmf.fake_thsdk_l2_module())
    """
    mod = types.ModuleType("data_source.thsdk_l2")
    mod.compute_main_flow = None     # 调用方在测试里 patch 成 MagicMock
    mod.get_auction_anomaly = None
    return mod


def fake_auction_df() -> pd.DataFrame:
    """竞价异动 DataFrame(2026-08-24 v0.3.2 真实口径)。

    实测 thsdk call_auction_anomaly 返回 6 列:
    时间 / 价格 / 总金额 / 代码 / 名称 / 异动类型1
    - "价格" 列**不是价格**: 是异动幅度小数比例(对急速/大幅异动)或撤单率(对撤单类型)
      或占位 1.0(对试盘类型)。
    - "总金额" 列恒为 2147483648 (int32 上限占位垃圾), _to_records 已 skip。
    - "撤单率/量比" 列已不存在(数据源不提供), 对应 record 字段:
      * volume_ratio 固定 None
      * withdraw_rate 仅"涨停撤单/跌停撤单"类型填入(本 fixture 不含此类)
    """
    return pd.DataFrame(
        [
            {
                "时间": "09:25:00",
                "价格": 0.0335,            # 真实口径: 大幅高开 3.35% 比例
                "总金额": 2147483648,      # int32 上限占位垃圾
                "代码": "002361",
                "名称": "神剑股份",
                "异动类型1": "大幅高开",
            },
            {
                "时间": "09:25:00",
                "价格": -0.0178,           # 真实口径: 大幅低开 -1.78% 比例
                "总金额": 2147483648,      # int32 上限占位垃圾
                "代码": "600000",
                "名称": "浦发银行",
                "异动类型1": "大幅低开",
            },
        ]
    )


def fake_auction_df_with_legacy_cols() -> pd.DataFrame:
    """已废弃: 2026-08-24 v0.3.2 起, thsdk 实际不返回 高开幅度/撤单率/量比 列。

    保留此函数仅供历史对照/调试, 测试不再引用(若未来有人误改回旧口径,
    可参考本 fixture 还原数据结构)。
    """
    return pd.DataFrame(
        [
            {"代码": "002361", "名称": "神剑股份", "高开幅度": 3.38,
             "撤单率": 0.243, "量比": 2.5, "成交额": 5357.5},
        ]
    )
