"""国内数据网关地址的唯一事实源(2026-09-26)。

背景: 原地址 115.190.177.213:8100 硬编码在多处(审计 docs/audit_report_20260915.md 第39条
就点过"5 处硬编码基础设施 IP"), 该机器 2026-09-26 整机失联 —— 22/8100 端口均超时, 且不在
Lighthouse 账号的上海/广州/北京/香港实例里, 判断为 CVM 或已释放。已迁到 101.35.244.238:8100:
systemd 常驻(Restart=always + 开机自启), 源码与端点契约见 skill
`cn-data-gateway-and-release`(scripts/cn_gateway_full.py / references/cn-gateway-endpoints.md)。

凡取网关数据一律经本模块, 不要再写死 IP —— 否则下次换机又会"改一处漏四处"。
"""
from __future__ import annotations

import os

#: 网关根地址(可用 CN_GATEWAY_BASE 覆盖, 供测试/多环境)
CN_GATEWAY_BASE = os.getenv("CN_GATEWAY_BASE", "http://101.35.244.238:8100").rstrip("/")


def gateway_url(path: str) -> str:
    """拼网关 URL。path 形如 cn/market-overview。"""
    return f"{CN_GATEWAY_BASE}/{path.lstrip('/')}"
