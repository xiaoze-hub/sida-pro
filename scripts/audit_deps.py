#!/usr/bin/env python3
"""依赖漏洞扫描脚本 (tier2-stability 2026-09-18)

用途:
- 读 requirements.txt / requirements-lock.txt / frontend/package.json
- 对比内置简化漏洞库(已知 CVE / 最低安全版本) + 版本过旧启发
- 输出报告: 包名 / 当前版本 / 建议版本 / 漏洞等级

用法:
  python scripts/audit_deps.py              # 人类可读报告
  python scripts/audit_deps.py --json       # JSON 输出(供 CI 解析)
  python scripts/audit_deps.py --fail-on high   # 存在 high/critical 时 exit 1

说明:
- 本脚本是"简化离线扫描", 不联网查 OSV/PyPI; 生产 CI 应并行跑
  `pip-audit` / `npm audit` 作权威源(见 .github/workflows/audit.yml)。
- 内置库覆盖常见高危包历史 CVE 的"最低安全版本", 版本比较用简单 tuple 数值比较。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent

# ── 简化漏洞库: 包名 → [(最低安全版本, 等级, CVE/说明), ...] ──────────────
# 只收录"低于该版本即视为有问题"的条目; 无条目则只做过旧启发。
KNOWN_VULNS: dict[str, list[tuple[str, str, str]]] = {
    # Python
    "requests": [("2.32.0", "medium", "CVE-2024-35195 session 跨请求泄露")],
    "urllib3": [("1.26.19", "high", "CVE-2024-37891 proxy-authorization 泄露"), ("2.2.2", "high", "CVE-2024-37891")],
    "pillow": [("10.3.0", "high", "CVE-2024-28219 buffer overflow"), ("11.0.0", "medium", "多个历史 CVE 收敛")],
    "cryptography": [("42.0.4", "high", "CVE-2024-26130 NULL deref / 历史高危")],
    "pyjwt": [("2.4.0", "high", "CVE-2022-29217 算法混淆")],
    "jinja2": [("3.1.4", "high", "CVE-2024-34064 xmlattr XSS / 历史")],
    "werkzeug": [("3.0.3", "high", "CVE-2024-34069 debugger RCE 风险")],
    "flask": [("2.2.5", "medium", "历史安全修复")],
    "django": [("4.2.11", "high", "CVE-2024-24680 等")],
    "sqlalchemy": [("2.0.0", "low", "1.x 已停止安全支持, 建议升 2.x")],
    "pyyaml": [("5.4", "high", "CVE-2020-14343 unsafe load")],
    "certifi": [("2023.7.22", "high", "CVE-2023-37920 e-Tugra 根证书移除")],
    "setuptools": [("65.5.1", "high", "CVE-2022-40897 ReDoS")],
    "wheel": [("0.38.1", "medium", "CVE-2022-40898")],
    "tornado": [("6.4.1", "high", "CVE-2024-52804 open redirect 等")],
    "aiohttp": [("3.9.4", "high", "CVE-2024-30251 历史多项")],
    "httpx": [("0.27.0", "low", "旧版本 SSL 验证边角问题")],
    "fastapi": [("0.109.1", "medium", "CVE-2024-24762 ReDoS 路径")],
    "starlette": [("0.36.2", "medium", "CVE-2024-24762 multipart ReDoS")],
    "uvicorn": [("0.29.0", "medium", "CVE-2024-24762 相关修复链")],
    "redis": [("4.6.0", "medium", "旧客户端 ACL/错误处理修复")],
    "python-multipart": [("0.0.18", "high", "CVE-2024-53981 DoS")],
    "pypdf": [("4.2.0", "medium", "历史解析漏洞修复")],
    "numpy": [("1.22.0", "medium", "CVE-2021-41495/41496 历史")],
    "scikit-learn": [("1.2.0", "low", "旧版本 pickle 加载风险提示")],
    "playwright": [("1.40.0", "low", "建议保持较新以获得浏览器安全补丁")],
    "openai": [("1.30.0", "low", "旧 SDK 不再接收安全更新")],
    # npm 常见包
    "vite": [("5.4.12", "high", "CVE-2024-45811 / server.fs.deny 绕过系列")],
    "esbuild": [("0.24.0", "medium", "dev server CORS 任意源")],
    "react": [("18.3.1", "medium", "CVE-2024-43796 XSS 相关")],
    "react-dom": [("18.3.1", "medium", "CVE-2024-43796 XSS 相关")],
    "brace-expansion": [("2.0.1", "medium", "CVE-2025-5889 ReDoS")],
    "nanoid": [("3.3.8", "medium", "CVE-2024-55565 生成可预测")],
    "qs": [("6.11.0", "medium", "原型污染历史")],
    "tough-cookie": [("4.1.3", "medium", "CVE-2023-26136 原型污染")],
    "semver": [("7.5.2", "medium", "CVE-2022-25883 ReDoS")],
    "ws": [("8.17.1", "medium", "DoS 修复")],
    "axios": [("1.7.4", "high", "CVE-2024-39338 SSRF")],
    "lodash": [("4.17.21", "high", "原型污染多项")],
    "minimist": [("1.2.6", "medium", "原型污染")],
}

# 未钉死版本(仅 >x / ^x / * / latest)视为信息级风险
_UNPINNED_MARKERS = re.compile(r"^[\^~><=\s*]|latest|^\*$", re.I)

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


@dataclass
class Finding:
    package: str
    ecosystem: str  # pypi | npm
    current_version: str
    recommended_version: str
    severity: str
    reason: str
    source_file: str


@dataclass
class AuditReport:
    findings: list[Finding] = field(default_factory=list)
    scanned: dict[str, int] = field(default_factory=dict)

    def highest_severity(self) -> str | None:
        if not self.findings:
            return None
        return min(self.findings, key=lambda f: SEVERITY_ORDER.get(f.severity, 9)).severity

    def has_at_least(self, level: str) -> bool:
        threshold = SEVERITY_ORDER.get(level, 9)
        return any(SEVERITY_ORDER.get(f.severity, 9) <= threshold for f in self.findings)


def _parse_version(raw: str) -> tuple[int, ...]:
    """把 '1.2.3.post1' / '2.32.0' 解析成可比较 tuple; 失败返回 (0,)。"""
    parts = re.findall(r"\d+", raw or "")
    if not parts:
        return (0,)
    return tuple(int(p) for p in parts[:4])


def _version_lt(a: str, b: str) -> bool:
    """a < b (按数值段比较, 长度不足补 0)。"""
    va, vb = _parse_version(a), _parse_version(b)
    n = max(len(va), len(vb))
    va = va + (0,) * (n - len(va))
    vb = vb + (0,) * (n - len(vb))
    return va < vb


def _clean_req_line(line: str) -> tuple[str, str] | None:
    """解析 requirements 行 → (name, version_spec_or_empty)。跳过注释/空行/本地路径。"""
    line = line.strip()
    if not line or line.startswith("#") or line.startswith("-"):
        return None
    # 去掉环境标记
    line = line.split(";", 1)[0].strip()
    # git+ URL 依赖
    if " @ " in line:
        name, _spec = line.split(" @ ", 1)
        return name.strip(), ""
    # name==ver / name>=ver
    m = re.match(r"^([A-Za-z0-9_.\-]+)\s*(==|>=|<=|~=|>|<)?\s*([^;#]*)", line)
    if not m:
        return None
    name = m.group(1)
    op = m.group(2) or ""
    ver = (m.group(3) or "").strip()
    if op == "==" and ver:
        return name, ver
    # >= 约束: 把下界当作"当前声明版本"参与过旧检查
    if op in (">=", "~=") and ver:
        return name, ver
    return name, ver or ""


def scan_python_file(path: Path, ecosystem_label: str = "pypi") -> list[tuple[str, str, str]]:
    if not path.exists():
        return []
    out: list[tuple[str, str, str]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parsed = _clean_req_line(line)
        if parsed:
            out.append((parsed[0], parsed[1], str(path.relative_to(REPO_ROOT))))
    return out


def scan_frontend_package_json(path: Path) -> list[tuple[str, str, str]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    out: list[tuple[str, str, str]] = []
    rel = str(path.relative_to(REPO_ROOT))
    for section in ("dependencies", "devDependencies"):
        deps = data.get(section) or {}
        for name, spec in deps.items():
            # 去掉 ^ ~ 等 range 前缀, 取数字版本
            version = re.sub(r"^[\^~>=<\s]+", "", str(spec)).strip()
            version = version.split(" ")[0]
            out.append((name, version, rel))
    return out


def check_package(name: str, version: str, source_file: str, ecosystem: str) -> list[Finding]:
    findings: list[Finding] = []
    key = name.lower().replace("_", "-")
    # PyPI 常见别名
    aliases = {key, key.replace("-", "_"), key.replace("_", "-")}
    rules: list[tuple[str, str, str]] = []
    for alias in aliases:
        rules.extend(KNOWN_VULNS.get(alias, []))

    # 取"低于任一最低安全版本"的最严格规则
    if version and rules:
        violated = [(min_ver, sev, reason) for min_ver, sev, reason in rules if _version_lt(version, min_ver)]
        if violated:
            # 选最高严重度的那条
            violated.sort(key=lambda r: SEVERITY_ORDER.get(r[1], 9))
            min_ver, sev, reason = violated[0]
            findings.append(
                Finding(
                    package=name,
                    ecosystem=ecosystem,
                    current_version=version,
                    recommended_version=min_ver,
                    severity=sev,
                    reason=reason,
                    source_file=source_file,
                )
            )

    # 未钉死版本 → info
    if not version or _UNPINNED_MARKERS.match(version) or version in ("", "*"):
        findings.append(
            Finding(
                package=name,
                ecosystem=ecosystem,
                current_version=version or "(unpinned)",
                recommended_version="pin exact version",
                severity="info",
                reason="版本未钉死, 供应链可漂移",
                source_file=source_file,
            )
        )
    return findings


def run_audit() -> AuditReport:
    report = AuditReport()

    python_pkgs: list[tuple[str, str, str]] = []
    for rel in ("requirements.txt", "requirements-lock.txt"):
        path = REPO_ROOT / rel
        pkgs = scan_python_file(path)
        report.scanned[rel] = len(pkgs)
        python_pkgs.extend(pkgs)

    # lock 优先: 同名包取 lock 的精确版本
    locked: dict[str, tuple[str, str]] = {}
    loose: dict[str, tuple[str, str]] = {}
    for name, ver, src in python_pkgs:
        key = name.lower()
        if "lock" in src:
            locked[key] = (ver, src)
        else:
            loose.setdefault(key, (ver, src))
    merged = {**loose, **locked}
    for key, (ver, src) in merged.items():
        # 恢复展示名(用 key 即可)
        report.findings.extend(check_package(key, ver, src, "pypi"))

    npm_path = REPO_ROOT / "frontend" / "package.json"
    npm_pkgs = scan_frontend_package_json(npm_path)
    report.scanned["frontend/package.json"] = len(npm_pkgs)
    for name, ver, src in npm_pkgs:
        report.findings.extend(check_package(name, ver, src, "npm"))

    report.findings.sort(key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.package))
    return report


def format_text(report: AuditReport) -> str:
    lines: list[str] = []
    lines.append("=== 依赖漏洞扫描报告 (simplified offline) ===")
    for src, n in report.scanned.items():
        lines.append(f"已扫描 {src}: {n} 个包")
    if not report.findings:
        lines.append("未发现问题(内置简化库口径; 建议仍跑 pip-audit / npm audit 交叉验证)")
        return "\n".join(lines)
    lines.append("")
    lines.append(f"{'等级':<8} {'生态':<6} {'包名':<28} {'当前':<16} {'建议':<16} 原因")
    lines.append("-" * 110)
    for f in report.findings:
        lines.append(
            f"{f.severity:<8} {f.ecosystem:<6} {f.package:<28} {f.current_version:<16} "
            f"{f.recommended_version:<16} {f.reason}"
        )
    lines.append("")
    lines.append(f"合计: {len(report.findings)} 条; 最高严重度: {report.highest_severity()}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SIDA 依赖漏洞扫描(简化离线)")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument(
        "--fail-on",
        choices=["none", "critical", "high", "medium", "low", "info"],
        default="none",
        help="存在不低于该等级的 finding 时 exit 1",
    )
    args = parser.parse_args(argv)

    report = run_audit()
    payload = {
        "scanned": report.scanned,
        "highest_severity": report.highest_severity(),
        "findings": [asdict(f) for f in report.findings],
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(format_text(report))

    if args.fail_on != "none" and report.has_at_least(args.fail_on):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
