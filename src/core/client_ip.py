"""客户端 IP 规范化（2026-11-04）。

管理后台用量监控把 `skill_usage.client_ip` 聚合成 Top IP。这条链上有两类脏值：

1. **入站伪造**：`X-Forwarded-For` 是请求方随便写的，只有直连 peer 是可信反代时
   才该采信（见 `src/web/middleware._get_client_ip`）；采信时也要校验，否则
   `X-Forwarded-For: <任意 200 字节垃圾>` 会原样落库、原样进报表。
2. **历史脏数据**：v242 之前的行 client_ip 全为 NULL，补齐过程中也可能混入
   带端口、带方括号或超长的字符串（列宽 VARCHAR(64)）。

统一收口在这里：

- 只接受合法 IPv4/IPv6 字面量，返回 `ipaddress` 的规范压缩写法；
- 超长（> ``MAX_IP_LEN``）直接不认，避免把列宽当输入上限；
- 认不出来一律返回 ``UNKNOWN_IP``（"unknown"）——报表里**保留"未知来源"这一档**，
  既不丢行，也不把它算到某个真实 IP 头上。

注意：这里只做"是不是一个 IP"，不做地理位置/运营商归属，也不做隐私脱敏
（管理端本来就是给 owner 看异常来源的）。
"""

from __future__ import annotations

import ipaddress

#: 与 `skill_usage.client_ip VARCHAR(64)` 对齐；比 IPv6 字面量上限（45）留了余量，
#: 但明确拒绝任何明显超长的输入。
MAX_IP_LEN = 64

#: 无法识别来源时的统一取值（不要用空串：空串在 SQL 聚合里和 NULL 混在一起）。
UNKNOWN_IP = "unknown"


def normalize_client_ip(raw: object) -> str:
    """把任意输入规范成合法 IP 字符串；不合法/超长/空 → ``"unknown"``。"""
    if raw is None:
        return UNKNOWN_IP
    try:
        text = str(raw).strip()
    except Exception:  # noqa: BLE001 — 脏对象 __str__ 抛错也不能拖垮记账
        return UNKNOWN_IP
    if not text or len(text) > MAX_IP_LEN:
        return UNKNOWN_IP
    try:
        return ipaddress.ip_address(text).compressed
    except ValueError:
        return UNKNOWN_IP
