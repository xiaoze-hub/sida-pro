"""P4 成熟化回归: 告警规则 ↔ 指标定义的双向锁定。

背景: deploy/prometheus-rules.yml 三条数据告警曾引用不存在的指标名,
一条都没响过。本测试保证规则里出现的 sida_* 指标在 health.py 里真实 emit。
改任一指标名, 此测试先红。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RULES = ROOT / "deploy" / "prometheus-rules.yml"
HEALTH = ROOT / "src" / "web" / "api" / "health.py"


def _rule_metrics() -> set[str]:
    text = RULES.read_text(encoding="utf-8")
    # expr 行里的 metric: 小写下划线 token, 排除 promQL 关键字/函数/label 名
    skip = {
        "sum", "by", "rate", "clamp_min", "increase", "humanizepercentage",
        "service", "status", "provider", "kind", "component", "job",
        "instance", "up", "for", "labels", "severity", "critical", "warning",
    }
    found = set(re.findall(r"[a-z][a-z0-9_]{2,}", text))
    return {m for m in found if m.startswith("sida_") and m not in skip}


def _emitted_metrics() -> set[str]:
    text = HEALTH.read_text(encoding="utf-8")
    return set(re.findall(r'"(sida_[a-z0-9_]+)"', text))


def test_rule_metrics_all_emitted():
    rule_m = _rule_metrics()
    emitted = _emitted_metrics()
    assert rule_m, "规则文件里一个 sida_* 指标都没解析到, 解析器先修"
    missing = rule_m - emitted
    assert not missing, f"告警引用了不存在的指标(死规则): {missing}"


def test_expected_alert_metrics_present():
    """四条数据告警的指标必须存在: 5xx率/数据源/DB/Redis。"""
    emitted = _emitted_metrics()
    assert "sida_http_requests_total" in emitted
    assert "sida_datasource_failures_total" in emitted
    assert "sida_health_component_status" in emitted


def test_component_status_records():
    """record_component_status 真实写 gauge(值 1/0), 非空函数。"""
    from src.web.api.health import _metrics, _init_metrics, record_component_status

    try:
        from prometheus_client import REGISTRY  # noqa
    except ImportError:
        import pytest

        pytest.skip("prometheus_client 未安装")
    _init_metrics()
    record_component_status("database", True)
    record_component_status("redis", False)
    assert _metrics.COMPONENT_STATUS is not None
    assert _metrics.COMPONENT_STATUS.labels(component="database")._value.get() == 1
    assert _metrics.COMPONENT_STATUS.labels(component="redis")._value.get() == 0
