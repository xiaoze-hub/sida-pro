"""数据域目录（data_catalog.yaml）与覆盖率生成器的回归防护。

三块：
1. 目录本身可解析、结构合法（26 域 / id 唯一 / 必填字段 / level / scope 枚举）；
2. 生成器能真跑通（直接 import 调函数，不 subprocess），路由/表规模符合平台现状；
3. 「域引用了不存在的表」这条分支真的能报出来，并且 --check 会以 exit 1 拦截
   —— 这是目录写错时唯一能自动挡住的地方，必须有负例覆盖。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
GEN_PATH = REPO_ROOT / "scripts" / "gen_data_catalog.py"
CATALOG_PATH = REPO_ROOT / "src" / "harness" / "data_catalog.yaml"

REQUIRED_FIELDS = ("id", "label", "level", "scope", "params", "source", "desc")
VALID_LEVELS = {"L0", "L1", "L2"}
VALID_SCOPES = {"market", "symbol", "symbol_or_market", "user", "admin"}
EXPECTED_DOMAINS = 26


@pytest.fixture(scope="module")
def gen():
    """把 scripts/gen_data_catalog.py 当模块加载（scripts 不是包，故走 importlib）。"""
    assert GEN_PATH.exists(), f"生成器缺失: {GEN_PATH}"
    spec = importlib.util.spec_from_file_location("gen_data_catalog", GEN_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["gen_data_catalog"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def catalog_raw() -> dict:
    return yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def report(gen):
    """跑一遍真实仓库的生成流程，不落盘。"""
    return gen.generate(write=False)


def _write_catalog(tmp_path: Path, domains: list[dict]) -> Path:
    p = tmp_path / "catalog_fake.yaml"
    p.write_text(yaml.safe_dump({"version": 1, "domains": domains}, allow_unicode=True), encoding="utf-8")
    return p


def _domain(**kw) -> dict:
    base = {
        "id": "test.demo",
        "label": "测试域",
        "level": "L0",
        "scope": "market",
        "params": {},
        "source": "",
        "desc": "测试用",
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# 1. 目录结构
# ---------------------------------------------------------------------------
def test_catalog_file_exists_and_is_mapping(catalog_raw):
    assert isinstance(catalog_raw, dict), "data_catalog.yaml 顶层必须是 mapping"
    assert isinstance(catalog_raw.get("domains"), list)


def test_catalog_has_26_domains(catalog_raw):
    assert len(catalog_raw["domains"]) == EXPECTED_DOMAINS


def test_domain_ids_globally_unique(gen, catalog_raw):
    domains = gen.load_catalog(CATALOG_PATH)
    ids = [d.id for d in domains]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    assert not dup, f"域 id 重复: {dup}"
    assert all(ids), "存在空 id"


def test_required_fields_level_and_scope(gen, catalog_raw):
    problems = []
    for raw in catalog_raw["domains"]:
        did = raw.get("id", "<无 id>")
        for f in REQUIRED_FIELDS:
            if f not in raw:
                problems.append(f"{did}: 缺 {f}")
        if raw.get("level") not in VALID_LEVELS:
            problems.append(f"{did}: level={raw.get('level')!r}")
        if raw.get("scope") not in VALID_SCOPES:
            problems.append(f"{did}: scope={raw.get('scope')!r}")
        if not isinstance(raw.get("params"), dict):
            problems.append(f"{did}: params 必须是 mapping")
    assert not problems, "目录结构问题:\n" + "\n".join(problems)


def test_validate_catalog_accepts_real_catalog(gen):
    """validate_catalog 对现行目录必须零问题（否则它就成了噪声门禁）。"""
    assert gen.validate_catalog(gen.load_catalog(CATALOG_PATH)) == []


def test_validate_catalog_flags_bad_level_and_missing_field(gen, tmp_path):
    bad = _write_catalog(
        tmp_path,
        [
            _domain(id="ok.domain", source="board_daily"),
            _domain(id="bad.level", level="L9"),
            {k: v for k, v in _domain(id="missing.desc").items() if k != "desc"},
        ],
    )
    problems = gen.validate_catalog(gen.load_catalog(bad))
    joined = "\n".join(problems)
    assert "bad.level" in joined and "L9" in joined
    assert "missing.desc" in joined and "desc" in joined


# ---------------------------------------------------------------------------
# 2. 生成器跑通 + 规模符合现状
# ---------------------------------------------------------------------------
def test_routes_parsed_at_least_400(gen, report):
    assert len(report.routes) >= 400
    # 每条路由都应有方法/路径/文件/行号
    sample = report.routes[0]
    assert sample.method in {"GET", "POST", "PUT", "DELETE", "PATCH"}
    # 少数 @router.get("") 依赖 include_router 前缀, 路径为空串, 属合法写法
    assert isinstance(sample.path, str)
    rooted = sum(1 for r in report.routes if r.path.startswith("/"))
    assert rooted / len(report.routes) > 0.9
    assert sample.file.startswith("src/web/api/")
    assert sample.line > 0


def test_tables_parsed_at_least_100(gen, report):
    assert len(report.tables) >= 100
    # hypertable 坑: klines 只在 migrations 里 CREATE TABLE，ORM 没建模 —— 不许漏
    assert "klines" in report.tables
    assert report.tables["klines"].sources == ("migrations",)
    assert not report.tables["klines"].in_models


def test_generate_returns_consistent_report(gen, report):
    assert len(report.domains) == EXPECTED_DOMAINS
    covered = report.covered_tables
    assert covered <= set(report.tables), "covered 里出现了不存在的表"
    assert set(report.uncovered_tables) == set(report.tables) - covered
    assert 0.0 < report.coverage_ratio <= 1.0
    assert len(report.implemented_domains) == len([d for d in report.domains if d.tables])


def test_report_markdown_has_three_sections(report):
    md = report.markdown
    assert md.startswith("# 数据域目录覆盖率报告")
    assert "## ① 域覆盖率" in md
    assert "## ② 未被任何域覆盖的表" in md
    assert "## ③ 域引用了不存在的表" in md
    # 概览必须把关键数字落到纸面
    assert f"| API 路由（`src/web/api/*.py` 装饰器） | **{len(report.routes)}** |" in md
    assert f"| 平台表（models ∪ migrations） | **{len(report.tables)}** |" in md


def test_generate_writes_file(gen, tmp_path):
    out = tmp_path / "coverage.md"
    gen.generate(out_path=out, write=True)
    assert out.exists()
    assert out.read_text(encoding="utf-8").lstrip().startswith("# 数据域目录覆盖率报告")


def test_source_tokens_handle_slash_and_parens(gen):
    # 「两侧带空白的斜杠」才是分隔符: market/phase 是路径提示, 不能被拆成两张表
    toks = gen.parse_source_tokens("market_sentiment_daily / market/phase")
    assert toks == ["market_sentiment_daily", "market/phase"]
    # 括号说明 / 反引号 / 行尾注释都要剥掉
    toks = gen.parse_source_tokens("limit_up_events（涨停池） + `board_daily` # 主源")
    assert toks == ["limit_up_events", "board_daily"]


def test_resolve_marks_route_hint_not_error(gen, report):
    """含 / 的 token 记为 route_hints，不进 unmatched（否则 market/phase 会误报）。"""
    sent = next(d for d in report.domains if d.id == "market.sentiment")
    assert sent.route_hints == ["market/phase"]
    assert sent.unmatched == []
    assert sent.tables == ["market_sentiment_daily"]


def test_current_catalog_broken_domains_are_limited(gen, report):
    """现今目录只允许这 3 个已知问题（source 里写成了模块名而非表名）。

    修掉之后本断言仍成立（子集），新增写错才会红。
    """
    known = {"stock.chip", "stock.flow", "stock.intraday"}
    assert {d.id for d in report.broken_domains} <= known


# ---------------------------------------------------------------------------
# 3. 负例：目录引用了不存在的表必须被识别 + --check exit 1
# ---------------------------------------------------------------------------
def test_resolve_domains_detects_missing_table(gen, tmp_path):
    fake = _write_catalog(
        tmp_path,
        [
            _domain(id="ok.real", source="board_daily"),
            _domain(id="bad.ghost", source="board_daily / no_such_table_xyz"),
        ],
    )
    rep = gen.generate(catalog_path=fake, write=False)
    broken = {d.id: d.unmatched for d in rep.broken_domains}
    assert broken.get("bad.ghost") == ["no_such_table_xyz"]
    assert "ok.real" not in broken
    # 报告里要能肉眼看到
    assert "no_such_table_xyz" in rep.markdown
    assert "表不存在" in rep.markdown
    # 真表仍然算命中
    ghost = next(d for d in rep.domains if d.id == "bad.ghost")
    assert ghost.tables == ["board_daily"]


def test_check_mode_exit_codes(gen, tmp_path, capsys):
    clean = _write_catalog(tmp_path, [_domain(id="ok.real", source="board_daily / klines")])
    assert gen.main(["--check", "--catalog", str(clean), "--out", str(tmp_path / "a.md")]) == 0

    dirty = _write_catalog(tmp_path, [_domain(id="bad.ghost", source="no_such_table_xyz")])
    assert gen.main(["--check", "--catalog", str(dirty), "--out", str(tmp_path / "b.md")]) == 1
    err = capsys.readouterr().err
    assert "bad.ghost" in err and "no_such_table_xyz" in err


def test_module_name_in_source_is_diagnosed(gen, tmp_path):
    """catalog 把 src/core/*.py 当表写是真实踩过的坑，报告要给出模块路径提示。"""
    fake = _write_catalog(
        tmp_path, [_domain(id="stock.chip", source="chip_distribution / stock_factor_daily")]
    )
    rep = gen.generate(catalog_path=fake, write=False)
    assert rep.broken_domains and rep.broken_domains[0].unmatched == ["chip_distribution"]
    assert "src/core/chip_distribution.py" in rep.markdown
