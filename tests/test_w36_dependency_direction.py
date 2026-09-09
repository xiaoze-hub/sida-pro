"""W3.6/D5+D6 依赖方向静态门禁(源码级断言, 2026-09-09)。

背景(风险整改方案 D5): 8000(主服务)=编排方, 8010(forecast_server)=纯计算
服务, 依赖必须单向 8000→8010; 8010 侧不得再"探测网关/回调拉配置"。本文件
用源码扫描把方向约束固化成 CI 门禁(权威依赖图见 docs/dependency_direction.md):

  1. forecast_lib/ 禁止出现旧环境变量名 PANWATCH_URL(已更名 SIDA_MAIN_API_URL);
  2. forecast_lib/ 禁止硬编码网关 IP 兜底(172.17.0.1/172.18.0.1/10.8.0.1);
  3. forecast_lib/ 禁止内联硬编码调 8000 拉配置;
  4. forecast_lib/ 与 forecast_server.py 禁止 import src.*; src/ 禁止
     import forecast_lib(编排走 HTTP, 不走进程内 import);
  5. DDE 端点唯一化: /api/thsdk/ext/dde 已删, 唯一入口 /api/thsdk/dde;
  6. compose 预测引擎环境变量用新名 SIDA_MAIN_API_URL。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORECAST_LIB = ROOT / "forecast_lib"

_BANNED_LEGACY_ENV = re.compile(r"PANWATCH_URL")
_BANNED_GATEWAY_IP = re.compile(r"172\.17\.0\.1|172\.18\.0\.1|10\.8\.0\.1")
_BANNED_INLINE_8000 = re.compile(r"""requests\.get\(\s*f?["']http://[^"']*8000""")
_BANNED_SRC_IMPORT = re.compile(r"^\s*(?:from|import)\s+src\b", re.MULTILINE)
_BANNED_FORECAST_LIB_IMPORT = re.compile(r"^\s*(?:from|import)\s+forecast_lib\b", re.MULTILINE)


def _py_files(path: Path) -> list[Path]:
    return sorted(p for p in path.rglob("*.py") if "__pycache__" not in p.parts)


def _hits(files: list[Path], pattern: re.Pattern) -> list[str]:
    return [p.name for p in files if pattern.search(p.read_text(encoding="utf-8"))]


def test_forecast_lib_no_legacy_panwatch_env():
    files = _py_files(FORECAST_LIB)
    assert _hits(files, _BANNED_LEGACY_ENV) == [], (
        "forecast_lib 禁止出现旧环境变量名(已更名 SIDA_MAIN_API_URL, D5)"
    )


def test_forecast_lib_no_gateway_ip_fallback():
    files = _py_files(FORECAST_LIB)
    assert _hits(files, _BANNED_GATEWAY_IP) == [], (
        "forecast_lib 禁止硬编码网关 IP 探测兜底(D5 已删 _detect_panwatch_url)"
    )


def test_forecast_lib_no_inline_hardcoded_8000_call():
    files = _py_files(FORECAST_LIB)
    assert _hits(files, _BANNED_INLINE_8000) == [], (
        "forecast_lib 禁止内联硬编码 http://*:8000 拉配置(地址只从 SIDA_MAIN_API_URL 取)"
    )


def test_engine_side_does_not_import_src():
    targets = _py_files(FORECAST_LIB) + [ROOT / "forecast_server.py"]
    assert _hits(targets, _BANNED_SRC_IMPORT) == [], (
        "8010 侧(forecast_lib + forecast_server)禁止 import src.*(依赖只允许 8000→8010)"
    )


def test_main_side_does_not_import_forecast_lib():
    files = _py_files(ROOT / "src")
    assert _hits(files, _BANNED_FORECAST_LIB_IMPORT) == [], (
        "8000 编排侧禁止进程内 import forecast_lib(编排走 HTTP POST /predict)"
    )


def test_forecast_proxy_uses_post_push():
    text = (ROOT / "src" / "web" / "api" / "forecast.py").read_text(encoding="utf-8")
    assert "/predict" in text and "llm_config" in text, (
        "8000→8010 编排必须经 POST /predict 请求体推送 llm_config(D5)"
    )


def test_dde_endpoint_unique():
    ext_text = (ROOT / "src" / "web" / "api" / "thsdk_ext.py").read_text(encoding="utf-8")
    extd_text = (ROOT / "src" / "web" / "api" / "thsdk_extended.py").read_text(encoding="utf-8")
    assert '@router.get("/dde/' not in ext_text, "/api/thsdk/ext/dde 已删(D5+D6 端点唯一化)"
    assert "/dde/{symbol}" in extd_text, "/api/thsdk/dde/{symbol} 是 DDE 唯一入口"


def test_compose_forecast_env_renamed():
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "SIDA_MAIN_API_URL" in text, "compose 预测引擎主服务地址须用新名 SIDA_MAIN_API_URL"
    assert "PANWATCH_URL=" not in text, "compose 禁止再出现旧环境变量名 PANWATCH_URL=（D5）"


def test_dependency_direction_doc_exists():
    doc = ROOT / "docs" / "dependency_direction.md"
    assert doc.exists(), "缺 docs/dependency_direction.md(W3.6/D6 依赖方向权威图)"
    text = doc.read_text(encoding="utf-8")
    assert "mermaid" in text.lower(), "依赖方向文档须含 mermaid 拓扑图"
    assert "8000" in text and "8010" in text, "依赖方向文档须描述 8000/8010 双侧"
