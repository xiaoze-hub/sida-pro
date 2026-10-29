"""七审回归(2026-10-29): 作用域静态门禁不得被"名义过滤"放行。

独立实审 B1: 上一版 `check_scoped_queries.py` 把任意 `*scope_clause` 名称
当归属证据, 且证据只按"函数体内出现过"判定。于是下列写法都能让全表查询通过:

  - `analysis_history_scope_clause(uid)` 调完就丢(无关调用);
  - 写在 `if False:` 死分支里;
  - 缺参调用 `analysis_history_scope_clause()`;
  - 给**另一条**查询加了过滤, 目标查询仍然全表。

现在口径: 名称恢复原守卫(SCOPE_HELPERS + `user_scope` 子串)+ 精确白名单;
证据必须落在**同一条查询链**(或该链取回的对象)上; 死分支/缺参不算证据。
本文件把每条反例钉成红样例, 正样例(同链/同变量追加过滤/对象级校验)钉成绿。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from check_scoped_queries import API_DIR, check_source  # noqa: E402

MODELS = {"Stock"}


def _violations(src: str) -> list[dict]:
    return check_source(src, MODELS)


# ───────────────────────── 反例: 必须抓红 ─────────────────────────


def test_helper_called_then_discarded_still_flagged():
    """无关调用: 助手调完就丢, 查询本身没有任何过滤。"""
    src = (
        "from src.core.analysis_history import analysis_history_scope_clause\n"
        "def bad(db, user):\n"
        "    analysis_history_scope_clause(user.id, user.role)\n"
        "    return db.query(Stock).all()\n"
    )
    v = _violations(src)
    assert len(v) == 1 and v[0]["model"] == "Stock" and v[0]["func"] == "bad"


def test_helper_in_dead_branch_still_flagged():
    """死分支: 过滤写在 `if False:` 里, 运行时从不执行。"""
    src = (
        "from src.web.api._scope import scoped\n"
        "def bad(db, user):\n"
        "    if False:\n"
        "        db.query(Stock).filter(Stock.user_id == user.id).all()\n"
        "    return db.query(Stock).all()\n"
    )
    v = _violations(src)
    # 死分支里的那条查询本身也没过滤(不算证据), 所以两条都报 —— 关键是没有一条被豁免。
    assert {x["line"] for x in v} == {4, 5}, v


def test_dead_branch_object_check_still_flagged():
    """死分支的对象级校验同样不算证据。"""
    src = (
        "def bad(db, user):\n"
        "    row = db.query(Stock).filter(Stock.id == 1).first()\n"
        "    if False:\n"
        "        if row.user_id != user.id:\n"
        "            raise SystemExit\n"
        "    return row\n"
    )
    v = _violations(src)
    assert len(v) == 1, v


def test_missing_args_helper_still_flagged():
    """缺参调用: 助手没有实参 → 运行时会 TypeError, 静态上不算"已过滤"。"""
    src = (
        "from src.core.analysis_history import analysis_history_scope_clause\n"
        "def bad(db, user):\n"
        "    return db.query(Stock).filter(\n"
        "        analysis_history_scope_clause()\n"
        "    ).all()\n"
    )
    v = _violations(src)
    assert len(v) == 1, v


def test_filter_for_another_query_still_flagged():
    """为另一条查询过滤: 只有被过滤的那条算已过滤, 裸查询必须报。"""
    src = (
        "from src.web.api._scope import scoped\n"
        "def bad(db, user):\n"
        "    scoped(db.query(Stock), user).all()\n"
        "    return db.query(Stock).all()\n"
    )
    v = _violations(src)
    # line 1 是 import: 被 scoped() 包住的查询在 line 3 已过滤; 裸的 line 4 必须报。
    assert {x["line"] for x in v} == {4}, v


def test_other_query_user_id_filter_does_not_cover_naked_query():
    """同函数里另一条查询带 user_id 过滤, 不能替裸查询背书。"""
    src = (
        "def bad(db, user):\n"
        "    db.query(Stock).filter(Stock.user_id == user.id).all()\n"
        "    return db.query(Stock).all()\n"
    )
    v = _violations(src)
    assert len(v) == 1, v
    assert v[0]["line"] == 3


def test_arbitrary_scope_clause_name_not_trusted():
    """名称放宽已撤销: 自造 `*_scope_clause` 名字不再算证据(精确白名单之外)。"""
    src = (
        "def bad(db, user):\n"
        "    return db.query(Stock).filter(evil_scope_clause(user.id)).all()\n"
    )
    v = _violations(src)
    assert len(v) == 1, v


def test_boolop_short_circuit_filter_still_flagged():
    """`False and <过滤>` 永不生效, 不是证据。"""
    src = (
        "def bad(db, user):\n"
        "    return db.query(Stock).filter(\n"
        "        False and (Stock.user_id == user.id)\n"
        "    ).all()\n"
    )
    v = _violations(src)
    assert len(v) == 1, v


# ───────────────────────── 正例: 必须通过 ─────────────────────────


def test_scope_clause_on_same_chain_passes():
    src = (
        "from src.core.analysis_history import analysis_history_scope_clause\n"
        "def good(db, user):\n"
        "    return db.query(Stock).filter(\n"
        "        analysis_history_scope_clause(user.id, user.role)\n"
        "    ).all()\n"
    )
    assert _violations(src) == []


def test_scope_clause_appended_to_same_query_variable_passes():
    """`q = db.query(X); q = q.filter(...)`: 同一查询对象的追加过滤。"""
    src = (
        "from src.core.analysis_history import analysis_history_scope_clause\n"
        "def good(db, user):\n"
        "    q = db.query(Stock)\n"
        "    if user is not None:\n"
        "        q = q.filter(analysis_history_scope_clause(user.id, user.role))\n"
        "    return q.all()\n"
    )
    assert _violations(src) == []


def test_dead_branch_append_to_query_variable_still_flagged():
    """同一变量但追加过滤写在死分支里 → 不算。"""
    src = (
        "def bad(db, user):\n"
        "    q = db.query(Stock)\n"
        "    if False:\n"
        "        q = q.filter(Stock.user_id == user.id)\n"
        "    return q.all()\n"
    )
    v = _violations(src)
    assert len(v) == 1, v


def test_object_level_check_passes():
    src = (
        "from src.web.api._scope import owned_or_404\n"
        "def good(db, user):\n"
        "    row = db.query(Stock).filter(Stock.id == 1).first()\n"
        "    return owned_or_404(row, user, '自选')\n"
    )
    assert _violations(src) == []


def test_inline_allow_comment_requires_marker():
    src = (
        "def ok(db, user):\n"
        "    # scoped-check: allow — 该表由上游按 user 过滤后取 id\n"
        "    return db.query(Stock).filter(Stock.id.in_([1])).all()\n"
    )
    assert _violations(src) == []


def test_repo_api_dir_is_green():
    """仓库现状: src/web/api 下不得有本文档口径下的漏过滤查询。"""
    import check_scoped_queries as csq

    models = csq.user_scoped_models()
    bad: list[str] = []
    for path in sorted(API_DIR.glob("*.py")):
        for v in check_source(path.read_text(encoding="utf-8"), models):
            bad.append(f"{path.name}:{v['line']} {v['model']} in {v['func']}")
    assert bad == []
