"""从指定 Git 基线生成项目代码索引；只做静态解析，不导入或执行业务模块。"""

from __future__ import annotations

import argparse
import ast
from collections import Counter
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs/knowledge-base/code-index.md"
SOURCE_ROOTS = ("src/", "packages/marketdata/", "forecast_lib/")
SOURCE_FILES = {"server.py", "forecast_server.py"}


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True)


def cell(value: str) -> str:
    return " ".join(value.split()).replace("|", "\\|").replace("`", "'")


def generate(baseline: str) -> str:
    commit = git("rev-parse", "--verify", f"{baseline}^{{commit}}").strip()
    files = git("ls-tree", "-r", "--name-only", "-z", commit).rstrip("\0").split("\0")
    version = git("show", f"{commit}:VERSION").strip()
    python_files = [
        path for path in files
        if path.endswith(".py") and (path.startswith(SOURCE_ROOTS) or path in SOURCE_FILES)
    ]
    trees: dict[str, ast.Module] = {}
    for path in python_files:
        trees[path] = ast.parse(git("show", f"{commit}:{path}"), filename=path)

    lines = [
        "# SIDA 项目代码索引",
        "",
        f"> 基线标识：`{commit}`，`VERSION={version}`。",
        f"> 盘点对象：`{ROOT}`。",
        "> 覆盖范围：基线中 src、packages/marketdata、forecast_lib 与两个服务入口的全部 Python AST；"
        "前端 App.tsx 路由声明；全仓 Git 跟踪文件的分类计数。",
        "> 未覆盖：测试用例执行、Python 函数内部算法正确性、前端组件内部行为、外部接口可用性、"
        "运行数据和密钥。模块说明取自源码首行文档字符串，可能带历史描述，不代表功能验收。",
        "> 生成器：`scripts/build_project_knowledge.py`；所有内容读取自上述 commit，不读取 .env。",
        "",
        "## 文件规模",
        "",
        f"基线共 {len(files)} 个 Git 跟踪文件；本索引解析 {len(trees)} 个 Python 文件。",
        "",
        "| 路径 | 跟踪文件数 | 主要扩展名 |",
        "| --- | ---: | --- |",
    ]
    for root in (
        "src", "frontend/src", "frontend/tests", "packages/marketdata", "forecast_lib",
        "tests", "scripts", "docs", "prompts", "strategies", ".github/workflows",
    ):
        selected = [path for path in files if path.startswith(root + "/")]
        counts = Counter(Path(path).suffix or "无扩展名" for path in selected)
        extensions = ", ".join(f"{suffix}: {count}" for suffix, count in counts.most_common(5))
        lines.append(f"| `{root}/` | {len(selected)} | {extensions} |")

    lines += ["", "## Agent 注册声明", "", "| 名称 | 实现 |", "| --- | --- |"]
    for path, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for decorator in node.decorator_list:
                if (
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Name)
                    and decorator.func.id == "register_agent"
                    and decorator.args
                    and isinstance(decorator.args[0], ast.Constant)
                ):
                    name = cell(str(decorator.args[0].value))
                    lines.append(f"| `{name}` | [{path}:{node.lineno}](../../{path}#L{node.lineno}) |")

    lines += [
        "", "## 前端路由声明", "",
        "声明来自 frontend/src/App.tsx；含兼容重定向与重复声明，是否可见仍由登录态、角色和权限决定。",
        "", "| 路由 | 元素入口 | 源码行 |", "| --- | --- | ---: |",
    ]
    app = git("show", f"{commit}:frontend/src/App.tsx")
    for lineno, line in enumerate(app.splitlines(), 1):
        match = re.search(r'<Route\s+path="([^"]+)"\s+element=\{<([A-Za-z0-9_]+)', line)
        if match:
            elements = re.findall(r"<([A-Z][A-Za-z0-9_]+)", line)
            lines.append(f"| `{cell(match[1])}` | {' → '.join(elements[1:])} | {lineno} |")

    lines += [
        "", "## Python 模块检索", "",
        "公开入口只列模块顶层类和非下划线函数；路由数仅计装饰器声明，不等于最终 API URL。"
        "完整 API 前缀与鉴权依赖须核对 src/web/app.py 的 include_router。",
        "", "| 模块 | 源码说明 | 公开入口 | 路由声明数 |", "| --- | --- | --- | ---: |",
    ]
    for path, tree in trees.items():
        doc = ast.get_docstring(tree) or ""
        summary = cell(doc.splitlines()[0])[:180] if doc else "无模块说明"
        symbols = [
            node.name for node in tree.body
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and not node.name.startswith("_")
        ]
        shown = ", ".join(symbols[:10])
        if len(symbols) > 10:
            shown += f" …（共 {len(symbols)} 个）"
        routes = sum(
            isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"get", "post", "put", "patch", "delete", "websocket", "api_route"}
            and isinstance(node.func.value, ast.Name) and node.func.value.id in {"router", "app"}
            for owner in ast.walk(tree)
            if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef))
            for node in owner.decorator_list
        )
        lines.append(f"| [{path}](../../{path}) | {summary} | {cell(shown)} | {routes} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default="HEAD", help="扫描的已提交 Git 基线，默认 HEAD")
    parser.add_argument("--check", action="store_true", help="只校验索引是否与基线一致")
    args = parser.parse_args()
    content = generate(args.baseline)
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text() != content:
            raise SystemExit("代码索引与指定基线不一致，请重新生成")
        print("代码索引校验通过")
        return
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(content)
    print(f"已生成 {OUTPUT}")


if __name__ == "__main__":
    main()
