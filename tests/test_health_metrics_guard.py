"""health/metrics 防护回归(2026-09-08 T9 审计修复)。

- /api/metrics 曾未鉴权且限流豁免, 泄露路径/状态码/AI 调用/数据源故障全量指标;
  收紧为仅内网/回环来源或持服务 token 者可读。
- /api/health 的 redis 组件曾回显完整 REDIS_URL(可能带密码), 改为只回显 host 段。
"""
from __future__ import annotations

from src.web.api.health import _request_from_internal, metrics
from src.web.cache.biz_cache import _mask_url


class _FakeRequest:
    def __init__(self, host: str | None, headers: dict | None = None):
        self.headers = headers or {}

        class _C:
            pass

        if host is None:
            self.client = None
        else:
            self.client = _C()
            self.client.host = host


def test_internal_sources_allowed():
    """容器内网/回环来源放行(Prometheus 同 compose 网络抓取)。"""
    assert _request_from_internal(_FakeRequest("172.18.0.5"))
    assert _request_from_internal(_FakeRequest("127.0.0.1"))
    assert _request_from_internal(_FakeRequest("10.0.0.9"))
    assert _request_from_internal(_FakeRequest("::1"))


def test_external_source_rejected():
    """公网来源不放行; 无 client 信息不放行。"""
    assert not _request_from_internal(_FakeRequest("93.184.216.34"))
    assert not _request_from_internal(_FakeRequest(None))


def test_mask_url_strips_credentials():
    """连接串脱敏: 凭据被去掉, host:port 保留。"""
    assert _mask_url("redis://:s3cret@10.0.0.5:6379/0") == "redis://10.0.0.5:6379"
    assert _mask_url("postgresql+psycopg2://sida:pw@pg:5432/sida") == "postgresql+psycopg2://pg:5432"
    assert _mask_url("redis://localhost:6379/0") == "redis://localhost:6379"
    assert _mask_url(None) is None


def test_metrics_endpoint_denies_external():
    """公网匿名请求 /metrics → 403。"""

    import asyncio

    resp = asyncio.run(metrics(_FakeRequest("93.184.216.34")))
    assert resp.status_code == 403


def test_metrics_endpoint_allows_internal():
    """内网请求 /metrics → 200(或 prometheus_client 未装时 503)。"""

    import asyncio

    resp = asyncio.run(metrics(_FakeRequest("172.18.0.5")))
    assert resp.status_code in (200, 503)
