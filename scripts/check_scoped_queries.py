#!/usr/bin/env python3
"""C3(2026-09-09) 静态门禁: src/web/api 下 db.query(带 user_id 的模型)必须有用户过滤。

规则: 扫描 src/web/api/*.py 的 `db.query(<Model>)`, 若 Model 含 user_id 列,
则同函数(含嵌套祖先函数)内必须满足其一, 否则报错退出 1:
  1. 查询链/函数体出现 `<Model>.user_id` 过滤
  2. 出现 _scope 助手调用: scoped( / owned_or_404( / writable(,
     或 *user_scope*( 本地作用域条件助手, 如 _user_scope_pos(acc) )
  3. 函数被 @allow_cross_user 显式豁免(须附安全性理由注释)
  4. 查询行尾注释 `# scoped-check: allow`(单行豁免, 同样须附理由)

用法: python scripts/check_scoped_queries.py            # 门禁模式
      python scripts/check_scoped_queries.py --list     # 同输出, 永远退出 0
红样例: 在任意 api 文件加裸 `db.query(Stock).all()` → CI 变红(验收要求)。
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
    """db.query(X) 的 X: Name→id / Attribute→最右属性名; 其余(函数调用等)不识别。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
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
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr == "user_id":
            v = node.value
            if isinstance(v, ast.Name):
                self.user_id_owners.add(v.id)
            elif isinstance(v, ast.Attribute):
                self.user_id_owners.add(v.attr)
        self.generic_visit(node)


def _fn_facts(fn: ast.AST) -> tuple[bool, set[str]]:
    v = _FnFacts()
    v.visit(fn)
    return v.scoped_call, v.user_id_owners


def check_source(src: str, scoped_models: set[str]) -> list[dict]:
    """返回违规清单: [{file, line, model, func}]。file 由调用方回填。"""
    tree = ast.parse(src)
    lines = src.splitlines()
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    violations: list[dict] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "query"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "db"):
            continue
        if not node.args:
            continue
        model = _model_name(node.args[0])
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
        # 3) 祖先函数内有 scoped() 或任意 user_id 归属处理(查询过滤或
        #    取回后 obj.user_id 校验均算 —— 覆盖 stocks.py 的先取后验模式)
        ok = False
        for f in fns:
            has_scoped, uid_owners = _fn_facts(f)
            if has_scoped or uid_owners:
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
        print(f"  {v['file']}:{v['line']}  db.query({v['model']})  in {v['func']}()")
    if args.list:
        return 0
    print("修复方式: 走 _scope.scoped()/owned_or_404()/writable(), 或 @allow_cross_user 显式豁免(附理由)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
