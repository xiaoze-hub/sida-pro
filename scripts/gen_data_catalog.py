#!/usr/bin/env python3
"""数据域目录生成器 + 覆盖率报告。

做三件事（全部静态解析，不连数据库、不 import src）：

1. 扫 ``src/web/api/*.py`` 的 ``@router.get/post/put/delete/patch`` 装饰器 → 全部路由；
   用 AST 而不是正则，避免注释/字符串里的假装饰器混进来，同时拿到行号与字面量路径。
2. 扫 ``src/db/models.py`` 的 ``__tablename__``（ORM 声明）+ ``src/web/migrations.py`` 的
   ``CREATE TABLE``（历史/时序表，如 TimescaleDB hypertable ``klines`` 只在 migrations 里）
   → 平台真实表清单。两者取并集：只认 models 会漏掉 43 张只存在于 migrations 的表。
3. 读 ``src/harness/data_catalog.yaml``，解析每个域 ``source`` 里的表名，核对是否真实存在，
   输出 ``docs/data-catalog-coverage.md``。

``source`` 的写法约定（解析规则）：
  - 表名之间用 ``/ + , 、 ; |`` 分隔，两侧空白可有可无：``a / b + c``；
  - 只有**两侧带空白的斜杠**算分隔符，所以 ``market_sentiment_daily / market/phase``
    里的 ``market/phase`` 不会被拆成两张表，而是识别为「路由/路径提示」；
  - 括号（中英文）里的说明、反引号、行尾 ``#`` 注释会被剥掉；
  - 解析出的 token 三类处理：命中真实表 → 表；含 ``/`` → 路由提示（不计错）；
    其余 → **目录写错**（--check 时 exit 1）。

用法::

    python scripts/gen_data_catalog.py            # 生成报告
    python scripts/gen_data_catalog.py --check    # CI 门禁：目录引用了不存在的表 → exit 1
    python scripts/gen_data_catalog.py --out -    # 打到 stdout，不写文件
"""

from __future__ import annotations

import argparse
import ast
import difflib
import re
import sys
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]

CATALOG_REL = "src/harness/data_catalog.yaml"
MODELS_REL = "src/db/models.py"
MIGRATIONS_REL = "src/web/migrations.py"
API_DIR_REL = "src/web/api"
OUT_REL = "docs/data-catalog-coverage.md"

DOMAIN_COUNT = 26
REQUIRED_FIELDS = ("id", "label", "level", "scope", "params", "source", "desc")
VALID_LEVELS = {"L0", "L1", "L2"}
VALID_SCOPES = {"market", "symbol", "symbol_or_market", "user", "admin"}

HTTP_METHODS = {"get", "post", "put", "delete", "patch"}

# SQL 里 CREATE TABLE 后面可能跟的关键字噪声，不是表名
_SQL_NOISE = {
    "if",
    "not",
    "exists",
    "table",
    "temporary",
    "temp",
    "unlogged",
    "only",
    "public",
}

_TABLENAME_RE = re.compile(r'__tablename__\s*=\s*["\']([A-Za-z_][A-Za-z0-9_]*)["\']')
_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+(?:TEMP(?:ORARY)?\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r'["\']?(?:public["\']?\.)?["\']?([A-Za-z_][A-Za-z0-9_]*)',
    re.IGNORECASE,
)

# source 里「两侧带空白的斜杠」才是分隔符，避免把 market/phase 拆开
_SOURCE_SPLIT_RE = re.compile(r"\s+/\s+|[+,，、;；|]+")
_PAREN_RE = re.compile(r"[（(][^）)]*[）)]")
_TOKEN_CLEAN_RE = re.compile(r"^[\s`'\"*·\-–—]+|[\s`'\"*·\-–—。.，,;；:：]+$")


@dataclass(frozen=True)
class Route:
    method: str
    path: str
    file: str
    line: int


@dataclass
class Table:
    name: str
    sources: tuple[str, ...]  # ("models",) / ("migrations",) / 两者

    @property
    def in_models(self) -> bool:
        return "models" in self.sources

    @property
    def note(self) -> str:
        return "仅 migrations 声明（ORM 未建模）" if not self.in_models else ""


@dataclass
class Domain:
    id: str
    label: str = ""
    level: str = ""
    scope: str = ""
    params: dict = field(default_factory=dict)
    source: str = ""
    desc: str = ""
    raw: dict = field(default_factory=dict)
    tables: list[str] = field(default_factory=list)  # 命中真实表
    route_hints: list[str] = field(default_factory=list)  # source 里的路径提示
    unmatched: list[str] = field(default_factory=list)  # 引用了不存在的表

    @property
    def implemented(self) -> bool:
        return bool(self.tables)


@dataclass
class Report:
    routes: list[Route]
    tables: dict[str, Table]
    domains: list[Domain]
    models_tables: set[str]
    migrations_tables: set[str]
    markdown: str = ""

    # ---- 派生指标 -------------------------------------------------------
    @property
    def covered_tables(self) -> set[str]:
        return {t for d in self.domains for t in d.tables}

    @property
    def uncovered_tables(self) -> list[str]:
        return sorted(set(self.tables) - self.covered_tables)

    @property
    def broken_domains(self) -> list[Domain]:
        return [d for d in self.domains if d.unmatched]

    @property
    def implemented_domains(self) -> list[Domain]:
        return [d for d in self.domains if d.implemented]

    @property
    def coverage_ratio(self) -> float:
        total = len(self.domains) or 1
        return len(self.implemented_domains) / total

    @property
    def table_coverage_ratio(self) -> float:
        total = len(self.tables) or 1
        return len(self.covered_tables & set(self.tables)) / total


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
def parse_routes(api_dir: Path | str) -> list[Route]:
    """AST 解析路由装饰器。非字面量路径（变量）会被跳过并计数到 stderr 汇总。"""
    api_dir = Path(api_dir)
    routes: list[Route] = []
    skipped: list[str] = []
    for py in sorted(api_dir.glob("*.py")):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        except SyntaxError as exc:  # 坏文件不该让整个报告失败
            skipped.append(f"{py.name}: SyntaxError {exc}")
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
                    continue
                if dec.func.attr not in HTTP_METHODS:
                    continue
                if not dec.args:
                    continue
                first = dec.args[0]
                if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                    skipped.append(f"{py.name}:{getattr(dec, 'lineno', '?')} 非字面量路径")
                    continue
                routes.append(
                    Route(
                        method=dec.func.attr.upper(),
                        path=first.value,
                        file=f"{API_DIR_REL}/{py.name}" if api_dir.name == "api" else str(py),
                        line=dec.lineno,
                    )
                )
    if skipped:
        print(f"[warn] 跳过 {len(skipped)} 处无法静态解析的路由: {skipped[:5]}", file=sys.stderr)
    return routes


def parse_models_tables(models_path: Path | str) -> set[str]:
    text = Path(models_path).read_text(encoding="utf-8")
    return set(_TABLENAME_RE.findall(text))


def parse_migrations_tables(migrations_path: Path | str) -> set[str]:
    text = Path(migrations_path).read_text(encoding="utf-8")
    names: set[str] = set()
    for name in _CREATE_TABLE_RE.findall(text):
        low = name.lower()
        if low in _SQL_NOISE or low.endswith("__new"):
            continue
        names.add(low)
    return names


def parse_tables(models_path: Path | str, migrations_path: Path | str) -> dict[str, Table]:
    models = parse_models_tables(models_path)
    migrations = parse_migrations_tables(migrations_path)
    tables: dict[str, Table] = {}
    for name in models | migrations:
        src = tuple(s for s, ok in (("models", name in models), ("migrations", name in migrations)) if ok)
        tables[name] = Table(name=name, sources=src)
    return tables


def load_catalog(catalog_path: Path | str) -> list[Domain]:
    """读 yaml → Domain 列表。字段缺失/类型不对时保留原值，由 validate_catalog 报错。"""
    data = yaml.safe_load(Path(catalog_path).read_text(encoding="utf-8")) or {}
    raw_domains = data.get("domains") or []
    if not isinstance(raw_domains, list):
        raise ValueError("data_catalog.yaml: domains 必须是 list")
    out: list[Domain] = []
    for item in raw_domains:
        if not isinstance(item, dict):
            raise ValueError(f"data_catalog.yaml: 域条目必须是 mapping, got {type(item).__name__}")
        params = item.get("params")
        out.append(
            Domain(
                id=str(item.get("id") or ""),
                label=str(item.get("label") or ""),
                level=str(item.get("level") or ""),
                scope=str(item.get("scope") or ""),
                params=params if isinstance(params, dict) else {},
                source=str(item.get("source") or ""),
                desc=str(item.get("desc") or ""),
                raw=item,
            )
        )
    return out


def validate_catalog(domains: list[Domain]) -> list[str]:
    """目录自身的结构校验（不含表存在性）。返回问题清单，空 = 合格。"""
    problems: list[str] = []
    seen: dict[str, int] = {}
    for d in domains:
        for f in REQUIRED_FIELDS:
            if f not in d.raw:
                problems.append(f"{d.id or '<无 id>'}: 缺必填字段 {f}")
        if not d.id:
            problems.append("<空 id>: id 不能为空")
            continue
        if d.id in seen:
            problems.append(f"{d.id}: id 重复")
        seen[d.id] = seen.get(d.id, 0) + 1
        if not re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*", d.id):
            problems.append(f"{d.id}: id 只允许小写字母/数字/下划线/点（<域> 或 <域>.<子域>）")
        if d.level not in VALID_LEVELS:
            problems.append(f"{d.id}: level={d.level!r} 不在 {sorted(VALID_LEVELS)}")
        if d.scope not in VALID_SCOPES:
            problems.append(f"{d.id}: scope={d.scope!r} 不在 {sorted(VALID_SCOPES)}")
        if not d.label:
            problems.append(f"{d.id}: label 为空")
        if not d.desc:
            problems.append(f"{d.id}: desc 为空")
    return problems


def parse_source_tokens(source: str) -> list[str]:
    """把 source 拆成 token 列表（保留顺序、去重、去空）。"""
    if not source:
        return []
    cleaned = _PAREN_RE.sub(" ", source)
    cleaned = cleaned.split("#", 1)[0]
    tokens: list[str] = []
    for chunk in _SOURCE_SPLIT_RE.split(cleaned):
        tok = _TOKEN_CLEAN_RE.sub("", chunk or "")
        if tok and tok not in tokens:
            tokens.append(tok)
    return tokens


def _looks_like_route(token: str) -> bool:
    return "/" in token


def _module_hint(token: str, repo_root: Path) -> str:
    """token 是否其实是个 Python 模块名（catalog 把模块当表写的常见笔误）。"""
    stem = token.rsplit(".", 1)[0]
    for base in ("src/core", "src/collectors", "src/harness", "src/web/api"):
        if (repo_root / base / f"{stem}.py").exists():
            return f"{base}/{stem}.py"
    return ""


def resolve_domains(domains: list[Domain], tables: dict[str, Table], repo_root: Path | str) -> None:
    """就地填充每个域的 tables / route_hints / unmatched。"""
    repo_root = Path(repo_root)
    lower = {k.lower(): k for k in tables}
    for d in domains:
        d.tables, d.route_hints, d.unmatched = [], [], []
        for tok in parse_source_tokens(d.source):
            key = tok.lower()
            if key in lower:
                if lower[key] not in d.tables:
                    d.tables.append(lower[key])
            elif _looks_like_route(tok):
                d.route_hints.append(tok)
            else:
                d.unmatched.append(tok)


# ---------------------------------------------------------------------------
# 候选域推荐（启发式，仅供人工归类时少翻代码）
# ---------------------------------------------------------------------------
CANDIDATE_RULES: tuple[tuple[str, str], ...] = (
    (r"^(kline|adj_factor)", "stock.kline"),
    (r"^(auction|_?auction)", "auction.flow"),
    (r"^(limit_up|limit_strength|ladder|seal_quality|broken_limit)", "limit.pool"),
    (r"^(dragon_tiger|lhb)", "lhb.daily"),
    (r"^(theme|board|mainline|sector|concept)", "theme.mood"),
    (r"^(market_sentiment|sentiment|phase|emotion|regime)", "market.sentiment"),
    (r"^(breadth|market_flow)", "market.breadth"),
    (r"^(index_daily|market_index)", "market.index"),
    (r"^(chip|price_distribution)", "stock.chip"),
    (r"(factor|_ic_)", "stock.factor"),
    (r"^(quote|tick|intraday|orderbook|img_orderbook|snapshot|l2_)", "stock.intraday"),
    (r"(flow|fund|cancel_rate|dde|turnover)", "stock.flow"),
    (r"^(news|report|research|announce|more_info|cls_)", "stock.dossier"),
    (r"(finance|shareholder|dividend|margin|fundamental|valuation)", "stock.fundamental"),
    (r"^(strategy|signal|backtest|demon|pioneer|recipe)", "strategy.signal"),
    (r"^(watchlist|stock_pool|universe)", "watchlist"),
    (r"^(portfolio|position|paper_trading|risk)", "portfolio.risk"),
    (r"^(notify|alert|throttle|channel|job|schedule|cron)", "notify.task"),
    (
        r"^(app_|system_|datasource|setting|config|secret|audit|log|llm_usage|health|schema_migration)",
        "ops.config",
    ),
    (r"^(user|auth|session|invite|pro_|tier|billing|subscription)", "ops.config"),
    # 注意: chat/copilot/agent/skill/decision 这些表在目录里**没有对应域**, 故意不给候选,
    # 让它们以「（待归类）」出现, 暴露目录缺口而不是硬塞进 ops.config。
)


def suggest_domains(table: str, valid_ids: set[str]) -> list[str]:
    hits: list[str] = []
    for pattern, domain_id in CANDIDATE_RULES:
        if domain_id in valid_ids and domain_id not in hits and re.search(pattern, table):
            hits.append(domain_id)
        if len(hits) >= 2:
            break
    return hits


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
def _cell(text: str, limit: int = 0) -> str:
    s = str(text).replace("|", "\\|").replace("\n", " ")
    if limit and len(s) > limit:
        s = s[: limit - 1] + "…"
    return s


def _pct(numer: int, denom: int) -> str:
    return f"{numer / denom * 100:.1f}%" if denom else "n/a"


def render_markdown(report: Report, catalog_rel: str = CATALOG_REL) -> str:
    domains = report.domains
    n_dom = len(domains)
    n_impl = len(report.implemented_domains)
    n_routes = len(report.routes)
    n_tables = len(report.tables)
    covered = report.covered_tables & set(report.tables)
    uncovered = report.uncovered_tables
    broken = report.broken_domains
    valid_ids = {d.id for d in domains}

    L: list[str] = []
    L.append("# 数据域目录覆盖率报告")
    L.append("")
    L.append(
        "> 本文件由 `scripts/gen_data_catalog.py` 静态扫描生成，**请勿手改**。"
        f"复现：`python scripts/gen_data_catalog.py`；门禁：`python scripts/gen_data_catalog.py --check`。"
    )
    L.append("")
    L.append("## 总览")
    L.append("")
    L.append("| 指标 | 数值 |")
    L.append("| --- | --- |")
    L.append(f"| API 路由（`src/web/api/*.py` 装饰器） | **{n_routes}** |")
    L.append(f"| 平台表（models ∪ migrations） | **{n_tables}** |")
    L.append(f"| 　其中 `models.py` `__tablename__` | {len(report.models_tables)} |")
    L.append(
        f"| 　仅 `migrations.py` 声明（含 hypertable） | {len(report.migrations_tables - report.models_tables)} |"
    )
    L.append(f"| 目录域总数（`{catalog_rel}`） | **{n_dom}** |")
    L.append(f"| **有实现的域** | **{n_impl} / {n_dom} = {_pct(n_impl, n_dom)}** |")
    L.append(f"| source 非空但零命中表的域 | {len([d for d in domains if d.source and not d.tables])} |")
    L.append(f"| 域已触达的表 | {len(covered)} / {n_tables} = {_pct(len(covered), n_tables)} |")
    L.append(f"| 未被任何域覆盖的表 | {len(uncovered)} |")
    L.append(
        f"| 域引用了不存在的表（目录写错） | {len(broken)} 个域 / {sum(len(d.unmatched) for d in broken)} 处 |"
    )
    L.append("")

    # ---- ① 域覆盖率
    L.append(f"## ① 域覆盖率：{n_impl} / {n_dom}（{_pct(n_impl, n_dom)}）")
    L.append("")
    L.append("按 level 拆分：")
    L.append("")
    L.append("| level | 已实现 / 总数 | 覆盖率 |")
    L.append("| --- | --- | --- |")
    for lv in ("L0", "L1", "L2"):
        grp = [d for d in domains if d.level == lv]
        if grp:
            ok = len([d for d in grp if d.implemented])
            L.append(f"| {lv} | {ok} / {len(grp)} | {_pct(ok, len(grp))} |")
    L.append("")
    L.append("| # | 域 id | 名称 | level | scope | 状态 | 引用表 | 未匹配 |")
    L.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for i, d in enumerate(domains, 1):
        state = (
            "✅ 已实现"
            if d.implemented
            else ("⚠️ source 有值但零命中" if d.source else "⬜ 待接（source 为空）")
        )
        tbls = ", ".join(f"`{t}`" for t in d.tables) or "—"
        miss = ", ".join(f"`{u}`" for u in d.unmatched) or "—"
        L.append(
            f"| {i} | `{_cell(d.id)}` | {_cell(d.label)} | {_cell(d.level)} | {_cell(d.scope)} "
            f"| {state} | {_cell(tbls, 120)} | {_cell(miss, 80)} |"
        )
    L.append("")
    L.append(
        "口径：「已实现」= source 里至少解析出一张**真实存在**的表；source 为空或只写了模块名/路径都算待接。"
    )
    L.append("")

    # ---- ② 未覆盖表
    L.append(f"## ② 未被任何域覆盖的表：{len(uncovered)} 张")
    L.append("")
    if not uncovered:
        L.append("（无）")
    else:
        L.append("候选域为按表名前缀/关键词的启发式建议，仅作归类参考。")
        L.append("")
        L.append("| # | 表名 | 定义来源 | 候选域 | 备注 |")
        L.append("| --- | --- | --- | --- | --- |")
        rows = []
        for name in uncovered:
            t = report.tables[name]
            sug = suggest_domains(name, valid_ids)
            rows.append(
                (sug[0] if sug else "（待归类）", name, t, ", ".join(f"`{s}`" for s in sug) or "（待归类）")
            )
        rows.sort(key=lambda r: (r[0], r[1]))
        for i, (_key, name, t, sug) in enumerate(rows, 1):
            L.append(
                f"| {i} | `{_cell(name)}` | {_cell('/'.join(t.sources))} | {sug} | {_cell(t.note) or '—'} |"
            )
    L.append("")

    # ---- ③ 目录写错
    L.append(f"## ③ 域引用了不存在的表：{len(broken)} 个域")
    L.append("")
    if not broken:
        L.append("（无，`--check` 通过）")
    else:
        L.append("以下 token 既不是 models 里的 `__tablename__`，也不是 migrations 的 `CREATE TABLE`。")
        L.append("")
        L.append("| 域 id | source 原文 | 未解析 token | 诊断 |")
        L.append("| --- | --- | --- | --- |")
        for d in broken:
            for tok in d.unmatched:
                mod = _module_hint(tok, REPO_ROOT)
                close = difflib.get_close_matches(tok, list(report.tables), n=1, cutoff=0.6)
                if mod:
                    diag = f"疑似模块名（`{mod}`），非表"
                elif close:
                    diag = f"表不存在；是否想写 `{close[0]}`？"
                else:
                    diag = "表不存在（models/migrations 均未声明）"
                L.append(f"| `{_cell(d.id)}` | `{_cell(d.source, 80)}` | `{_cell(tok)}` | {_cell(diag)} |")
    L.append("")

    # ---- ④ 路由侧导航（启发式）
    L.append("## ④ 附：路由 → 域 启发式映射（导航用，不计入门禁）")
    L.append("")
    L.append("按路由路径关键词粗匹配，用于回答「某个域背后大概有哪些 API」；不是精确归属。")
    L.append("")
    L.append("| 域 id | 匹配路由数 | 示例路径 |")
    L.append("| --- | --- | --- |")
    counts: dict[str, list[str]] = {}
    for r in report.routes:
        hit = suggest_domains(r.path.strip("/").replace("/", "_"), valid_ids)
        if hit:
            counts.setdefault(hit[0], []).append(f"{r.method} {r.path}")
    for did, paths in sorted(counts.items(), key=lambda kv: -len(kv[1])):
        L.append(f"| `{_cell(did)}` | {len(paths)} | {_cell(', '.join(paths[:3]), 100)} |")
    mapped = sum(len(v) for v in counts.values())
    L.append("")
    L.append(
        f"可粗映射到域的路由：{mapped} / {n_routes} = {_pct(mapped, n_routes)}；其余属认证/运维/未归类。"
    )
    L.append("")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def generate(
    catalog_path: Path | str | None = None,
    models_path: Path | str | None = None,
    migrations_path: Path | str | None = None,
    api_dir: Path | str | None = None,
    out_path: Path | str | None = None,
    repo_root: Path | str | None = None,
    write: bool = True,
) -> Report:
    """跑一遍全流程，返回 Report（markdown 已渲染）。write=False 时只算不落盘。"""
    root = Path(repo_root) if repo_root else REPO_ROOT
    catalog_path = Path(catalog_path) if catalog_path else root / CATALOG_REL
    models_path = Path(models_path) if models_path else root / MODELS_REL
    migrations_path = Path(migrations_path) if migrations_path else root / MIGRATIONS_REL
    api_dir = Path(api_dir) if api_dir else root / API_DIR_REL

    domains = load_catalog(catalog_path)
    tables = parse_tables(models_path, migrations_path)
    routes = parse_routes(api_dir)
    resolve_domains(domains, tables, root)

    report = Report(
        routes=routes,
        tables=tables,
        domains=domains,
        models_tables=parse_models_tables(models_path),
        migrations_tables=parse_migrations_tables(migrations_path),
    )
    try:
        catalog_rel = str(catalog_path.resolve().relative_to(root.resolve()))
    except ValueError:
        catalog_rel = str(catalog_path)
    report.markdown = render_markdown(report, catalog_rel=catalog_rel)

    if write:
        target = Path(out_path) if out_path else root / OUT_REL
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(report.markdown + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成数据域目录覆盖率报告（静态解析，不连库）")
    ap.add_argument("--check", action="store_true", help="有域引用不存在的表时 exit 1（CI 门禁用）")
    ap.add_argument("--out", default=str(REPO_ROOT / OUT_REL), help="输出路径，'-' 表示打到 stdout")
    ap.add_argument("--repo", default=str(REPO_ROOT), help="仓库根目录")
    ap.add_argument(
        "--catalog", default=None, help="目录 yaml 路径（默认 <repo>/src/harness/data_catalog.yaml）"
    )
    ap.add_argument("--verbose", "-v", action="store_true", help="打印结构校验问题")
    args = ap.parse_args(argv)

    root = Path(args.repo)
    to_stdout = args.out == "-"
    report = generate(
        catalog_path=args.catalog,
        repo_root=root,
        out_path=None if to_stdout else args.out,
        write=not to_stdout,
    )

    if to_stdout:
        print(report.markdown)

    problems = validate_catalog(report.domains)
    if args.verbose or problems:
        for p in problems:
            print(f"[catalog] {p}", file=sys.stderr)

    n_impl, n_dom = len(report.implemented_domains), len(report.domains)
    print(
        f"路由 {len(report.routes)} / 表 {len(report.tables)} / 域 {n_dom} / "
        f"已实现域 {n_impl} ({_pct(n_impl, n_dom)}) / 未覆盖表 {len(report.uncovered_tables)} / "
        f"目录写错 {len(report.broken_domains)} 域"
    )

    if args.check and report.broken_domains:
        for d in report.broken_domains:
            print(
                f"[check][FAIL] {d.id}: 引用了不存在的表 {d.unmatched}（source={d.source!r}）",
                file=sys.stderr,
            )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
