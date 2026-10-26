"""国内数据网关存活监控(2026-09-27)。

背景: 网关 2026-09-26 整机失联, 而**没有任何存活监控** —— 直到用户看到
"大盘资金流·数据源调用失败" 才暴露。本模块补上这个盲区。

为什么探**数据路径**而不是 /health:
  网关进程活着、端口通, 但它到东财的上游被按出口 IP 风控拒掉时(`①` 实测:
  云服务器 IP 全挡、家宽高频也挡), `/health` 依然返回 200 —— 只探进程等于没探。
  所以探针直接打 `/cn/market-overview`(网关侧有 60s 缓存, 每 15 分钟探一次约等于
  每 15 分钟一次真实上游调用, 可接受), 要求 `total_main_flow` 必须存在。

分层: 本模块属 src/core, **不得** import src/web(B4.1 棘轮门禁)—— 只做探测与判定;
通知由装配层 src/bootstrap/runtime.py 发送。
"""
from __future__ import annotations

import json
import urllib.request

from src.core.cn_gateway import gateway_url

#: 连续失败达到该次数才告警 —— 东财风控本身有抖动, 单次失败不值得打扰
ALERT_AFTER_FAILURES = 2


def probe(timeout: float = 20.0) -> dict:
    """探测网关数据路径。返回 dict(ok / detail / ...), 不抛异常。"""
    url = gateway_url("cn/market-overview")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001 — 探针任何异常都算失败
        return {"ok": False, "url": url, "detail": f"{type(e).__name__}: {e}"[:160]}
    if not isinstance(data, dict):
        return {"ok": False, "url": url, "detail": "响应不是 JSON 对象"}
    if data.get("error"):
        # 网关自己的降级出口(东财风控 -> 502 body)
        return {"ok": False, "url": url, "detail": f"网关降级: {str(data['error'])[:120]}"}
    flow = data.get("total_main_flow")
    if flow is None:
        return {"ok": False, "url": url, "detail": "缺少 total_main_flow(资金字段缺失)"}
    return {"ok": True, "url": url, "detail": f"两市主力净流入 {flow} 亿",
            "total_main_flow": flow, "up_count": data.get("up_count"),
            "source": data.get("source")}


class GatewayWatch:
    """连续失败计数 + 状态跃迁判定 —— 只在"坏掉"和"恢复"各告警一次, 不刷屏。"""

    def __init__(self) -> None:
        self.failures = 0
        self.alarmed = False

    def observe(self, res: dict) -> str | None:
        """喂一次探测结果, 返回需要发送的告警文本; 无需告警返回 None。"""
        if res.get("ok"):
            self.failures = 0
            if self.alarmed:
                self.alarmed = False
                return f"国内数据网关已恢复: {res.get('detail')}"
            return None
        self.failures += 1
        if self.failures >= ALERT_AFTER_FAILURES and not self.alarmed:
            self.alarmed = True
            return ("国内数据网关连续 %d 次取数失败\n地址: %s\n原因: %s\n"
                    "影响: 大盘资金流会降级为「仅涨跌家数」(不编数字)" % (
                        self.failures, res.get("url"), res.get("detail")))
        return None
