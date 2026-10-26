"""TQ JSON-RPC 直连消费者的失败可观测性(2026-10-08 可观测性审计 P1-6)。

## 问题

只有经 Engine 的 vendor 失败才 `emit_vendor_failure`(base.py), 而
`theme_mood` / `resonance_scan` / `tdx_boards` / `limit_pool_zdt` /
`market_sentiment_collector` / `tdx_dark_fund` 等**直连** `tq_rpc` 的消费者
完全不上报 —— 通达信链路断 3 天无人发现, 正是这个缺口(监控全黑)。

## 方案

不逐个改直连方(且核心文件另有人在改, 约定不碰), 而是**在 tq_rpc 外层包一层**:
`install_tq_rpc_guard()` 幂等地把 `marketdata.vendors.tq.tq_rpc` 换成带失败上报
的包装; 直连方用 `from marketdata.vendors.tq import tq_rpc` 时取到的是被包过的
版本(它们都在函数体内 import, 按调用时解析模块属性), 因此**一处覆盖全部直连方**,
包括本子代理无法改动的 theme_mood / resonance_scan。

失败后原样 re-raise(不吞异常), 由消费者原有的降级逻辑决定后续; 事件经
`emit_vendor_failure` 广播 → health.py 已注册的桥接 → `sida_datasource_failures_total`
指标 + datasource_failures 表(表自带 60s 限流)。

kind = 调用名(TQ 方法名), 但只允许**有界的**方法名集合, 其余归一为 "fetch",
防止 label 基数膨胀(与 health.py/datasource_failures.py 的归一白名单同源)。

进程级安装; health.py(app 启动导入)调用一次即可覆盖 web/调度进程。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: 允许作为 kind 的 TQ 方法名(有界白名单, 防 Prometheus label 基数膨胀)。
#: 覆盖直连消费者实际调用的方法 + 既有枚举; 未列出的一律归一为 "fetch"。
TQ_RPC_KINDS: tuple[str, ...] = (
    "get_stock_list",
    "get_market_data",
    "get_more_info",
    "get_zdt_data",
    "get_pricevol",
    "get_relation",
    "get_sector_list",
    "get_stock_list_in_sector",
    "get_gpjy_value",
    "get_scjy_value",
    "get_bkjy_value",
    "get_match_stkinfo",
    "get_trading_calendar",
    "formula_process_mul_zb",
    "refresh_kline",
    # 既有 kind 枚举(与 Engine 的 timeout/auth 一致)
    "fetch",
    "timeout",
    "auth",
    "parse",
)

_ALLOWED = frozenset(TQ_RPC_KINDS)


def tq_kind(method: object) -> str:
    """TQ 方法名 → 受控 kind(白名单外归一为 "fetch")。"""
    m = str(method or "").strip()
    return m if m in _ALLOWED else "fetch"


def install_tq_rpc_guard() -> bool:
    """幂等包装 `marketdata.vendors.tq.tq_rpc`: 失败 → emit_vendor_failure 后 re-raise。

    返回是否本次真的安装(已装过返回 False)。安装失败(import 异常)静默返回 False ——
    可观测性绝不反噬业务。
    """
    try:
        import marketdata.vendors.tq as tqmod
    except Exception as e:  # noqa: BLE001
        logger.debug("TQ 观测包装: 无法导入 tq vendor(%s), 跳过", e)
        return False

    if getattr(tqmod.tq_rpc, "_tq_observed", False):
        return False
    # 保留原始入口(包装内部走它, 避免自递归; 也便于测试替换)
    if not hasattr(tqmod, "_tq_rpc_orig"):
        tqmod._tq_rpc_orig = tqmod.tq_rpc

    _unset = object()

    def _observed(method, params, timeout=_unset):
        try:
            if timeout is _unset:
                return tqmod._tq_rpc_orig(method, params)
            return tqmod._tq_rpc_orig(method, params, timeout=timeout)
        except Exception:
            try:
                from marketdata.vendors.base import emit_vendor_failure

                emit_vendor_failure("tq", tq_kind(method))
            except Exception:  # noqa: BLE001 - 上报失败绝不反噬
                pass
            raise

    _observed._tq_observed = True
    tqmod.tq_rpc = _observed
    logger.info("TQ 观测包装已安装: 直连 tq_rpc 失败将进数据源失败指标")
    return True


def is_installed() -> bool:
    """测试/诊断用: 当前 tq_rpc 是否已被观测包装。"""
    try:
        import marketdata.vendors.tq as tqmod

        return bool(getattr(tqmod.tq_rpc, "_tq_observed", False))
    except Exception:  # noqa: BLE001
        return False
