"""测试用的最小 SQLAlchemy 查询面（非测试文件，供 test_*.py 复用）。

为什么不 mock `score_run` 内部：要验证的是「真的按 run_id 过滤、真的按 seq 排序」，
所以这里用 SQLAlchemy 表达式本身（`BinaryExpression.left.key` / `.operator`）做求值，
而不是把查询逻辑从被测代码里挖掉。只支持本项目用到的四种形态：
`==` / `>=` / `.in_()` / `.desc()`。

⚠️ 这不是 ORM 模拟器：只覆盖 `query(Model).filter().order_by().limit().all()` 与
`one_or_none()`，覆盖面上限就是被测模块用到的那几行。
"""

from __future__ import annotations

from src.db.models import CopilotRun, CopilotStep


def _row_value(row, key):
    return getattr(row, key, None)


def _match(row, expr) -> bool:
    left = getattr(expr, "left", None)
    key = getattr(left, "key", None)
    if key is None:                                  # 非列比较（不该出现）→ 视为通过
        return True
    val = _row_value(row, key)
    op = getattr(getattr(expr, "operator", None), "__name__", "")
    right = getattr(expr, "right", None)
    rval = getattr(right, "value", right)
    if op == "eq":
        return val == rval
    if op == "ge":
        return val is not None and rval is not None and val >= rval
    if op in ("in_op", "in_"):
        return val in list(rval or [])
    raise AssertionError(f"FakeQuery 不支持的过滤运算符: {op}")


class FakeQuery:
    def __init__(self, rows, model):
        self._rows = list(rows)
        self._model = model
        self._preds: list = []
        self._order: list = []
        self._limit: int | None = None

    def filter(self, *exprs):
        self._preds.extend(exprs)
        return self

    def order_by(self, *exprs):
        self._order.extend(exprs)
        return self

    def limit(self, n: int):
        self._limit = int(n)
        return self

    def all(self):
        rows = [r for r in self._rows if all(_match(r, p) for p in self._preds)]
        for expr in reversed(self._order):
            modifier = getattr(getattr(expr, "modifier", None), "__name__", "")
            key = getattr(getattr(expr, "element", expr), "key", None)
            if key is None:
                continue
            rows.sort(key=lambda r, k=key: (_row_value(r, k) is None, _row_value(r, k)),
                      reverse=(modifier == "desc_op"))
        if self._limit is not None:
            rows = rows[: self._limit]
        return rows

    def one_or_none(self):
        rows = self.all()
        if not rows:
            return None
        assert len(rows) == 1, f"one_or_none 命中 {len(rows)} 行"
        return rows[0]

    def first(self):
        rows = self.all()
        return rows[0] if rows else None


class FakeTraceDB:
    """`db.query(Model)` 的最小实现；传进来的行就是 ORM 行的替身（SimpleNamespace 即可）。"""

    def __init__(self, runs=(), steps=()):
        self.tables = {CopilotRun: list(runs), CopilotStep: list(steps)}

    def query(self, model):
        return FakeQuery(self.tables.get(model, []), model)
