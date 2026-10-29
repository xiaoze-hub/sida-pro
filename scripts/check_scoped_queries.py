#!/usr/bin/env python3
"""C3(2026-09-09) 静态门禁: src/web/api 下 user-scoped 查询必须有用户过滤。

规则: 扫描 src/web/api/*.py, 命中以下任一"查询点"且被查模型含 user_id 列时,
查询本身或它取回的对象上必须带归属证据, 否则报错退出 1。

归属证据(必须与**这一条**查询相关, 2026-10-29 收口):
  A. 同一查询链的 filter/where/filter_by 实参里有 `<Model>.user_id` 比较;
  B. 同一查询链的 filter/where/filter_by 实参里有 _scope 助手调用:
     scoped( / owned_or_404( / writable( / *user_scope* 条件助手
     / 精确白名单 SCOPE_CLAUSE_HELPERS(如 analysis_history_scope_clause);
     助手必须带实参(空调用不算证据);
  C. 查询链本身被传给上述助手: scoped(db.query(X), user) / owned_or_404(q.first(), user);
  D. 取回对象级校验: `row = <查询>.first()`, 之后 `row.user_id` 或
     `scope_helper(row, ...)` 被用于判定(覆盖 stocks.py 的先取后验模式)。

豁免(与查询无关, 但显式且可审计):
  E. 所在函数(含嵌套祖先)被 @allow_cross_user 标注(须附安全性理由注释);
  F. 查询行尾注释 `# scoped-check: allow`(单行豁免, 同样须附理由)。

2026-10-29 独立实审阻断(B1): 上一版把任意 `*scope_clause` 名称当证据, 且证据
只按"函数体内出现过"判定 —— 调用后丢弃、写在 `if False:` 死分支、缺参调用、
或给**另一条查询**加过滤, 都能让裸全表查询通过。现在:
  - 名称恢复为原守卫(`SCOPE_HELPERS` + `user_scope` 子串), 新增助手只走**精确
    白名单**, 不做后缀/前缀泛匹配;
  - 证据必须落在**同一条查询链**(或该链取回的对象)上, 函数体别处的过滤不算;
  - 死分支(`if False:`)里的证据不算; 零实参的助手调用不算;
  - 反例见 tests/test_scoped_query_gate_round7.py。

查询点(2026-10-11 扩): 此前只识别 ORM 1.x `db.query(<Model>)`, 对 SQLAlchemy 2.0
`db.execute(select(<Model>)...)` 零覆盖 → presets.py 的 select() 漏 user_id 过滤
逃逸(两用户同标的 MultipleResultsFound / 命中他人标的)。现一并纳入。
`db.execute(text("..."))` 原生 SQL 的模型是字符串, AST 无法静态归因, 不在此判定;
src/web/api 现存 text() 查询经人工核对均只查共享行情表/独立预测库(无 user-scoped 表)。

用法: python scripts/check_scoped_queries.py            # 门禁模式
      python scripts/check_scoped_queries.py --list     # 同输出, 永远退出 0
红样例: 在任意 api 文件加裸 `db.query(Stock).all()` 或
        `db.execute(select(Stock).where(Stock.symbol=="600519"))` → CI 变红(验收要求)。
"""
from __future__ import annotations

import argparse
import ast
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
API_DIR = REPO_ROOT / "src" / "web" / "api"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# _scope.py 统一助手(按名识别, 兼容 from-import 与模块限定两种写法)
SCOPE_HELPERS = {"scoped", "owned_or_404", "writable"}

# 2026-10-29: 名称不含 "user_scope" 的归属条件助手 —— 只做**精确**白名单,
# 不做后缀泛匹配(上一版 `name.endswith("scope_clause")` 让任意新名字都能当证据)。
SCOPE_CLAUSE_HELPERS = {
    # src/core/analysis_history.py: 唯一的历史归属口径(本人 + owner 可见 NULL)
    "analysis_history_scope_clause",
}

# 链上承载"归属条件"的方法
CHAIN_FILTER_METHODS = {"filter", "filter_by", "where"}


def user_scoped_models() -> set[str]:
    """运行时解析 ORM: 返回带 user_id 列的模型类名集合(避免手写清单漂移)。"""
    os.environ.setdefault("SIDA_ALLOW_SQLITE", "1")
    from src.web.database import Base  # noqa: PLC0415
    from src.web import models  # noqa: F401,PLC0415  确保全部模型注册进 metadata

    out = set()
    for mapper in Base.registry.mappers:
        cls = mapper.class_
        table = getattr(cls, "__table__", None)
        if table is not None and "user_id" in table.columns:
            out.add(cls.__name__)
    return out


def _model_name(node: ast.AST) -> str | None:
    """db.query(X) 的 X / select(X) 的 X: Name→id / Attribute→最右属性名; 其余(函数调用等)不识别。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _root_model_name(node: ast.AST) -> str | None:
    """取表达式的根模型名: select(Stock)→Stock, select(Stock.symbol)→Stock。

    沿 Attribute/Subscript 向左下剥到最左的 Name; 函数调用(Call)等无法可靠
    归因的一律 None(不误报)。
    """
    cur = node
    for _ in range(50):  # 防御性上限, 避免异常深链死循环
        if isinstance(cur, ast.Attribute):
            cur = cur.value
        elif isinstance(cur, ast.Subscript):
            cur = cur.value
        else:
            break
    if isinstance(cur, ast.Name):
        return cur.id
    return None


def _call_name(node: ast.AST) -> str | None:
    """取被调用对象的名字: foo(...)→foo, a.b(...)→b。"""
    if not isinstance(node, ast.Call):
        return None
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def _unwrap_select(node: ast.AST) -> ast.Call | None:
    """沿方法链(.where/.filter/.order_by/...)回剥到最底层的 select(...) 调用。

    db.execute(select(Stock).where(...)) 的实参是 Call(where)，需一路回剥到
    Call(select) 才能取到被查模型。不是 select() 链则返回 None。
    """
    cur = node
    for _ in range(100):
        if isinstance(cur, ast.Call) and isinstance(cur.func, ast.Attribute):
            cur = cur.func.value  # 剥掉一层方法调用
        else:
            break
    if isinstance(cur, ast.Call) and _call_name(cur) in {"select", "select_from"}:
        return cur
    return None


def _select_model(node: ast.AST) -> str | None:
    """从一个 select(...) 调用取被查模型名(取第一个实参的根模型)。"""
    if not isinstance(node, ast.Call) or not node.args:
        return None
    return _root_model_name(node.args[0])


def _query_site_model(node: ast.AST) -> str | None:
    """识别一个 user-scoped 查询点, 返回被查模型名; 不识别返回 None。

    覆盖两种 SQLAlchemy 写法:
      1. ORM 1.x/2.0:  db.query(<Model>) / db.query(<Model>).filter(...)
      2. ORM 2.0:      db.execute(select(<Model>)[.where(...)...])
    对 db.execute(text("...")) 之类原生 SQL: 模型无法从 AST 静态归因(字符串),
    不在此处判定 —— 由维护者人工核对(见模块 docstring)。
    """
    # 形态 1: db.query(Model)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "query"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "db"
        and node.args
    ):
        return _model_name(node.args[0])

    # 形态 2: db.execute(select(Model)...)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "execute"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in {"db", "session"}
        and node.args
    ):
        sel = _unwrap_select(node.args[0])
        if sel is not None:
            return _select_model(sel)

    return None


def _is_scope_helper_call(node: ast.AST) -> bool:
    """是"归属条件/归属判定"助手调用, 且带实参。

    零实参调用不是证据 —— `analysis_history_scope_clause()` 缺参只会在运行时
    TypeError, 静态上不能据此认为查询已过滤(独立实审 B1 反例之一)。
    """
    if not isinstance(node, ast.Call):
        return False
    name = _call_name(node)
    if name is None or not node.args:
        return False
    return (
        name in SCOPE_HELPERS
        or "user_scope" in name
        or name in SCOPE_CLAUSE_HELPERS
    )


def _decorator_name(dec: ast.AST) -> str | None:
    if isinstance(dec, ast.Name):
        return dec.id
    if isinstance(dec, ast.Attribute):
        return dec.attr
    if isinstance(dec, ast.Call):  # @allow_cross_user(reason="...")
        return _decorator_name(dec.func)
    return None


def _enclosing_functions(parents: dict[ast.AST, ast.AST], node: ast.AST) -> list[ast.AST]:
    """节点向上的全部函数祖先(嵌套函数逐层算, 任意一层有过滤即算已过滤)。"""
    out = []
    cur = parents.get(node)
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(cur)
        cur = parents.get(cur)
    return out


def _const_false(test: ast.AST) -> bool:
    return isinstance(test, ast.Constant) and not test.value


class _SourceIndex:
    """一份源文件上的父子索引 + 同函数变量绑定表(供"条件来自局部变量"解析)。"""

    def __init__(self, tree: ast.Module, src: str) -> None:
        self.src = src
        self.lines = src.splitlines()
        self.parents: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                self.parents[child] = node
        self._bindings: dict[ast.AST, dict[str, list[ast.AST]]] = {}

    def bindings(self, fn: ast.AST | None) -> dict[str, list[ast.AST]]:
        """函数体内 `name = <expr>` 的右值清单(模块级为 None)。"""
        if fn is None:
            return {}
        cached = self._bindings.get(fn)
        if cached is not None:
            return cached
        table: dict[str, list[ast.AST]] = {}
        for node in ast.walk(fn):
            targets: list[ast.AST] = []
            value = None
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
                value = node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets = [node.target]
                value = node.value
            elif isinstance(node, ast.NamedExpr):
                targets = [node.target]
                value = node.value
            if value is None:
                continue
            for tgt in targets:
                if isinstance(tgt, ast.Name):
                    table.setdefault(tgt.id, []).append(value)
        self._bindings[fn] = table
        return table

    def in_dead_branch(self, node: ast.AST) -> bool:
        """节点位于静态死代码里: `if False:` / `while False:` body、
        `A if False else B` 的 A、`False and <expr>` 的 expr、`True or <expr>` 的 expr。"""
        cur: ast.AST | None = node
        while cur is not None:
            par = self.parents.get(cur)
            if isinstance(par, (ast.If, ast.While)) and _const_false(par.test):
                if any(cur is stmt for stmt in par.body):
                    return True
            if isinstance(par, ast.IfExp) and _const_false(par.test) and cur is par.body:
                return True
            if isinstance(par, ast.BoolOp):
                values = par.values
                pos = next((i for i, v in enumerate(values) if v is cur), None)
                if pos is not None:
                    if isinstance(par.op, ast.And) and any(
                        _const_false(v) for v in values[:pos]
                    ):
                        return True
                    if isinstance(par.op, ast.Or) and any(
                        isinstance(v, ast.Constant) and bool(v.value)
                        for v in values[:pos]
                    ):
                        return True
            cur = par
        return False

    def call_chain(self, node: ast.AST) -> list[ast.Call]:
        """从查询/select 调用向上收集方法链(穿过 `db.query` 这类纯属性节点)。"""
        out: list[ast.Call] = [node] if isinstance(node, ast.Call) else []
        cur: ast.AST = node
        while True:
            par = self.parents.get(cur)
            if isinstance(par, ast.Attribute) and par.value is cur:
                cur = par
                continue
            if isinstance(par, ast.Call) and (
                par.func is cur
                or (isinstance(par.func, ast.Attribute) and par.func.value is cur)
            ):
                if isinstance(par, ast.Call):
                    out.append(par)
                cur = par
                continue
            break
        return out

    def scope_region(self, node: ast.Call) -> tuple[list[ast.Call], ast.Call]:
        """与这条查询相关的调用集合: 自身方法链 + (db.execute 时)内层 select 链。

        第二个返回值是外层链最末端的调用(赋值/助手包装判定都以它为准)。
        """
        region = self.call_chain(node)
        top = region[-1] if region else node
        seen = {id(c) for c in region}
        inner = None
        if isinstance(node.func, ast.Attribute) and node.func.attr == "execute" and node.args:
            inner = _unwrap_select(node.args[0])
        if inner is not None and inner is not node:
            for call in self.call_chain(inner):
                if id(call) not in seen:
                    seen.add(id(call))
                    region.append(call)
        return region, top

    def filter_conditions(self, region: list[ast.Call]) -> list[ast.AST]:
        """查询链上的 filter/where/filter_by 实参(即该查询的归属条件位置)。"""
        out: list[ast.AST] = []
        for call in region:
            func = call.func
            if not isinstance(func, ast.Attribute) or func.attr not in CHAIN_FILTER_METHODS:
                continue
            out.extend(call.args)
            out.extend(kw.value for kw in call.keywords)
        return out

    def expr_has_evidence(self, expr: ast.AST, fn: ast.AST | None, depth: int = 0) -> bool:
        """表达式里是否含归属证据(直接写 user_id / 调归属助手 / 引用局部条件变量)。"""
        if depth > 4 or self.in_dead_branch(expr):
            return False
        if isinstance(expr, ast.Attribute) and expr.attr == "user_id":
            return True
        if isinstance(expr, ast.Call) and _is_scope_helper_call(expr):
            return True
        if isinstance(expr, ast.Name):
            for value in self.bindings(fn).get(expr.id, []):
                if self.expr_has_evidence(value, fn, depth + 1):
                    return True
            return False
        for child in ast.iter_child_nodes(expr):
            if self.expr_has_evidence(child, fn, depth + 1):
                return True
        return False

    def result_names(self, top: ast.Call | None, fn: ast.AST | None) -> set[str]:
        """查询结果被赋给的局部名(含 `for row in rows` 的解包一层)。"""
        names: set[str] = set()
        if top is None:
            return names
        par = self.parents.get(top)
        targets: list[ast.AST] = []
        if isinstance(par, ast.Assign):
            targets = list(par.targets)
        elif isinstance(par, (ast.AnnAssign, ast.NamedExpr)):
            targets = [par.target]
        for tgt in targets:
            if isinstance(tgt, ast.Name):
                names.add(tgt.id)
            elif isinstance(tgt, (ast.Tuple, ast.List)):
                names.update(e.id for e in tgt.elts if isinstance(e, ast.Name))
        # rows = q.all(); for row in rows: row.user_id / helper(row, ...)
        for name in list(names):
            for node in ast.walk(fn) if fn is not None else []:
                if isinstance(node, (ast.For, ast.comprehension)) and isinstance(node.iter, ast.Name):
                    if node.iter.id == name:
                        if isinstance(node.target, ast.Name):
                            names.add(node.target.id)
        return names

    def object_verified(self, name: str, fn: ast.AST | None) -> bool:
        """取回的行对象是否真的被归属判定过: `obj.user_id` 或 helper(obj, ...)。"""
        if fn is None:
            return False
        for node in ast.walk(fn):
            if self.in_dead_branch(node):
                continue
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "user_id"
                and isinstance(node.value, ast.Name)
                and node.value.id == name
            ):
                return True
            if isinstance(node, ast.Call) and _is_scope_helper_call(node):
                if any(isinstance(a, ast.Name) and a.id == name for a in node.args):
                    return True
        return False

    def _rebound_to_other_query(
        self, name: str, fn: ast.AST | None, origin: ast.AST | None
    ) -> bool:
        """同名变量是否在函数里被重新赋成**另一条**查询(`q = db.query(Y)`)。"""
        if fn is None:
            return False
        for node in ast.walk(fn):
            if not isinstance(node, ast.Assign):
                continue
            if not any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                continue
            if origin is not None and node.value is origin:
                continue  # 就是这次查询本身的绑定
            if isinstance(node.value, ast.Call) and _query_site_model(node.value):
                return True
        return False

    def var_chained_conditions(
        self, names: set[str], fn: ast.AST | None, origin: ast.AST | None = None
    ) -> list[ast.AST]:
        """`q = db.query(X)` 之后 `q = q.filter(...)` / `q.filter(...)` 的过滤条件。

        同一查询对象上的追加过滤(常见的 `if user is not None: q = q.filter(...)`),
        只有变量确实是这条查询时才认; 变量被重绑到另一条查询则不算证据。
        """
        out: list[ast.AST] = []
        if fn is None:
            return out
        for name in names:
            if self._rebound_to_other_query(name, fn, origin):
                continue
            for node in ast.walk(fn):
                if self.in_dead_branch(node):
                    continue
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not isinstance(func, ast.Attribute) or func.attr not in CHAIN_FILTER_METHODS:
                    continue
                if not (isinstance(func.value, ast.Name) and func.value.id == name):
                    continue
                out.extend(node.args)
                out.extend(kw.value for kw in node.keywords)
        return out


def check_source(src: str, scoped_models: set[str]) -> list[dict]:
    """返回违规清单: [{file, line, model, func}]。file 由调用方回填。"""
    tree = ast.parse(src)
    idx = _SourceIndex(tree, src)

    violations: list[dict] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        model = _query_site_model(node)
        if not model or model not in scoped_models:
            continue

        fns = _enclosing_functions(idx.parents, node)
        scope_name = fns[0].name if fns else "<module>"
        fn = fns[0] if fns else None

        # 1) 查询行注释豁免(允许写在查询首行, 或紧邻的上一行 —— 理由通常较长)
        here = idx.lines[node.lineno - 1] if node.lineno <= len(idx.lines) else ""
        above = idx.lines[node.lineno - 2] if 2 <= node.lineno <= len(idx.lines) else ""
        if "scoped-check: allow" in here or "scoped-check: allow" in above:
            continue
        # 2) 任意祖先函数被 @allow_cross_user 豁免
        if any(
            any(_decorator_name(d) == "allow_cross_user" for d in f.decorator_list)
            for f in fns
        ):
            continue

        region, top = idx.scope_region(node)
        ok = False

        # 3a) 本条查询链的 filter/where 上带归属条件
        for cond in idx.filter_conditions(region):
            if idx.expr_has_evidence(cond, fn):
                ok = True
                break
        # 3a+) 同一查询变量上的追加过滤(q = db.query(X); q = q.filter(...))
        names = idx.result_names(top, fn)
        if not ok:
            for cond in idx.var_chained_conditions(names, fn, top):
                if idx.expr_has_evidence(cond, fn):
                    ok = True
                    break
        # 3b) 整条查询(或它的结果行)被交给归属助手判定
        if not ok and region:
            for call in region:
                par = idx.parents.get(call)
                if (
                    isinstance(par, ast.Call)
                    and _is_scope_helper_call(par)
                    and any(call is a for a in par.args)
                    and not idx.in_dead_branch(call)
                ):
                    ok = True
                    break
        # 3c) 取回对象后做归属校验(row.user_id / helper(row, ...))
        if not ok:
            for name in names:
                if idx.object_verified(name, fn):
                    ok = True
                    break

        if ok:
            continue
        violations.append(
            {"file": "", "line": node.lineno, "model": model, "func": scope_name}
        )
    return violations


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="只列出, 不当失败(存量盘点用)")
    args = ap.parse_args()

    models = user_scoped_models()
    violations: list[dict] = []
    for path in sorted(API_DIR.glob("*.py")):
        for v in check_source(path.read_text(encoding="utf-8"), models):
            v["file"] = str(path.relative_to(REPO_ROOT))
            violations.append(v)

    if not violations:
        print("check_scoped_queries: OK — src/web/api 无未过滤的 user-scoped 查询")
        return 0
    print(f"check_scoped_queries: {len(violations)} 处未过滤的 user-scoped 查询:")
    for v in violations:
        print(f"  {v['file']}:{v['line']}  user-scoped 查询 {v['model']}  in {v['func']}()")
    if args.list:
        return 0
    print("修复方式: 走 _scope.scoped()/owned_or_404()/writable(), 或 @allow_cross_user 显式豁免(附理由)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
