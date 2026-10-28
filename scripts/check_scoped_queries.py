#!/usr/bin/env python3
"""C3(2026-09-09) 静态门禁: src/web/api 下 user-scoped 查询必须有用户过滤。

规则: 扫描 src/web/api/*.py, 命中以下任一"查询点"且被查模型含 user_id 列时,
同函数(含嵌套祖先函数)内必须满足其一, 否则报错退出 1:
  1. 查询链/函数体出现 `<Model>.user_id` 过滤(查询过滤或取回后 obj.user_id 校验均算)
  2. 出现 _scope 助手调用: scoped( / owned_or_404( / writable(,
     或 *user_scope*( 本地作用域条件助手, 如 _user_scope_pos(acc) )
  3. 在 if/while/assert 条件里调用**本文件定义的**作用域名判据函数
     (名字含 "scope", 如 run_read_scope_ok / run_cancel_scope_ok / *_scope_filter),
     且该判据函数体内确有 user_id 归属处理或 _scope 助手调用 —— 见
     `_scope_evidence()`: 只跟同文件定义(跨文件的判据无法静态验证),
     且必须有硬证据(把函数改名成 *_scope_ok 而体内不做归属处理, 依然算未过滤)
  4. 函数被 @allow_cross_user 显式豁免(须附安全性理由注释)
  5. 查询行尾注释 `# scoped-check: allow`(单行豁免, 同样须附理由)

为什么要 3: 归属判据常常收在 `xxx_scope_ok(row, user)` 这种小函数里, 查询点所在
函数只写 `if not xxx_scope_ok(row, user): 404`。只按本函数体扫会把「已经过统一
判据」的站点误报, 逼人写重命名式豁免 —— 那才是真漏洞。

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


class _FnFacts(ast.NodeVisitor):
    """收集一个函数体内的关键事实: _scope 助手调用与 <X>.user_id 引用名。"""

    # _scope.py 统一助手(按名识别, 兼容 from-import 与模块限定两种写法)
    SCOPE_HELPERS = {"scoped", "owned_or_404", "writable"}

    def __init__(self) -> None:
        self.scoped_call = False
        self.user_id_owners: set[str] = set()
        self.getattr_user_id = False

    def visit_Call(self, node: ast.Call) -> None:
        f = node.func
        if isinstance(f, ast.Name):
            name = f.id
        elif isinstance(f, ast.Attribute):
            name = f.attr
        else:
            name = None
        if name is not None and (
            name in self.SCOPE_HELPERS or "user_scope" in name
        ):
            self.scoped_call = True
        # `getattr(row, "user_id", None)`: 归属读取的一种写法(列名在字符串里),
        # 与 `row.user_id` 同算硬证据 —— 只看 Attribute 会把 run_read_scope_ok
        # 这类"经 getattr 归一后比较"的判据判成没有归属处理。
        if (
            name == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "user_id"
        ):
            self.getattr_user_id = True
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr == "user_id":
            v = node.value
            if isinstance(v, ast.Name):
                self.user_id_owners.add(v.id)
            elif isinstance(v, ast.Attribute):
                self.user_id_owners.add(v.attr)
        self.generic_visit(node)


def _fn_has_evidence(fn: ast.AST) -> bool:
    """函数体内是否出现**硬证据**: _scope 助手调用 / `<X>.user_id` / getattr(..., "user_id")。"""
    v = _FnFacts()
    v.visit(fn)
    return v.scoped_call or bool(v.user_id_owners) or v.getattr_user_id


def _call_names(fn: ast.AST) -> set[str]:
    """函数体里调用到的名字(foo( / a.foo() 都取 foo)。"""
    out: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            n = _call_name(node)
            if n is not None:
                out.add(n)
    return out


# 判据链只沿"归属/作用域"命名的本地助手往下跟: scope 是作用域判据, owner 是
# 归属判据(如 _run_owner_id)。跟任意名字等于给"随手调用一下"开后门。
_FOLLOW_HINTS = ("scope", "owner")


def _guarded_call_names(fn: ast.AST) -> set[str]:
    """函数体里**出现在 if/while/assert 条件中**的调用名 —— 判据要真的用于拦截。

    只看条件位置(而不是随意一次调用): `if not run_cancel_scope_ok(row, user): 404`
    是拦, `run_cancel_scope_ok(row, user)` 单独一行什么也没拦, 不该算数。
    """
    out: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, (ast.If, ast.While)):
            test = node.test
        elif isinstance(node, ast.Assert):
            test = node.test
        else:
            continue
        for sub in ast.walk(test):
            if isinstance(sub, ast.Call):
                n = _call_name(sub)
                if n is not None:
                    out.add(n)
    return out


def _is_followable(name: str) -> bool:
    return any(h in name for h in _FOLLOW_HINTS)


def _predicate_evidence(
    fn: ast.AST,
    local_fns: dict[str, ast.AST],
    memo: dict[int, bool] | None = None,
) -> bool:
    """判据函数**自身或它转手调用的归属助手**里是否有硬证据(可跨多层, 环安全)。"""
    memo = {} if memo is None else memo
    key = id(fn)
    if key in memo:
        return memo[key]
    memo[key] = False  # 环保护: 递归回自身时先按 False 处理, 不会无限展开
    ok = _fn_has_evidence(fn)
    if not ok:
        for name in _call_names(fn):
            if not _is_followable(name):
                continue
            callee = local_fns.get(name)
            if callee is not None and _predicate_evidence(callee, local_fns, memo):
                ok = True
                break
    memo[key] = ok
    return ok


def _scope_evidence(
    fn: ast.AST,
    local_fns: dict[str, ast.AST],
    memo: dict[int, bool] | None = None,
) -> bool:
    """函数(或其守卫的本地归属判据)是否**真的**做了归属处理。

    判定顺序:
      1. 本函数体自带硬证据(_scope 助手 / user_id 引用 / getattr(x, "user_id"));
      2. 否则看它**在 if/while/assert 条件里**调用的本文件判据函数(名字含
         scope/owner), 并沿该判据的转手调用链找硬证据。

    这三条限制保证它不是"改个名就绿"的后门:
      - 只跟同文件定义: import 来的判据无法静态验证, 不认;
      - 调用点必须在条件守卫里, 且名字要像判据: 至少真的是在拦;
      - 判据链里必须有硬证据: 空壳 `def foo_scope_ok(row, user): return True`
        依旧算未过滤(一路红到调用方)。
    """
    if _fn_has_evidence(fn):
        return True
    memo = {} if memo is None else memo
    for name in _guarded_call_names(fn):
        if not _is_followable(name):
            continue
        callee = local_fns.get(name)
        if callee is not None and _predicate_evidence(callee, local_fns, memo):
            return True
    return False


def check_source(src: str, scoped_models: set[str]) -> list[dict]:
    """返回违规清单: [{file, line, model, func}]。file 由调用方回填。"""
    tree = ast.parse(src)
    lines = src.splitlines()
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    violations: list[dict] = []
    # 本文件内定义的函数(只有这些可被当作"本地作用域判据"跟进, 见 _scope_evidence)
    local_fns: dict[str, ast.AST] = {
        n.name: n for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        model = _query_site_model(node)
        if not model or model not in scoped_models:
            continue

        fns = _enclosing_functions(parents, node)
        scope_name = fns[0].name if fns else "<module>"

        # 1) 查询行注释豁免
        line_txt = lines[node.lineno - 1] if node.lineno <= len(lines) else ""
        if "scoped-check: allow" in line_txt:
            continue
        # 2) 任意祖先函数被 @allow_cross_user 豁免
        if any(
            any(_decorator_name(d) == "allow_cross_user" for d in f.decorator_list)
            for f in fns
        ):
            continue
        # 3) 祖先函数内有 scope 助手调用, 或任意 user_id 归属处理(查询过滤或
        #    取回后 obj.user_id 校验均算 —— 覆盖 stocks.py 的先取后验模式),
        #    或守卫了本文件定义的、体内确有归属证据的作用域判据函数
        #    (见 _scope_evidence: 不跟跨文件、不接受空壳判据)
        ok = False
        for f in fns:
            if _scope_evidence(f, local_fns):
                ok = True
                break
        if ok:
            continue
        violations.append({"file": "", "line": node.lineno, "model": model, "func": scope_name})
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
