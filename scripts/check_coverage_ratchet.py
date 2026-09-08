#!/usr/bin/env python3
"""W2.5/E5 覆盖率棘轮: 行覆盖率只许升不许降, 降了 CI 红。

- 基线: scripts/coverage-baseline.json 的 line_coverage 字段。
- 基线口径: build-push-acr.yml gates 的 pytest 子集
  (python 3.11 + requirements-lock.txt 精确版本, tests/ -m "not network",
   --cov=src)。其它工作流测试子集不同, 不作为基线依据。
- 每波结束手动上调一次基线(把基线改成当波 CI 实测值); 下调=退步, 禁止。
- 故意删测试/写裸代码导致覆盖率跌破基线 → exit 1 拦截。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE_FILE = ROOT / "scripts" / "coverage-baseline.json"


def main() -> int:
    cov_file = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "coverage.json"
    if not cov_file.exists():
        print(f"[E5] 找不到覆盖率报告 {cov_file} (pytest --cov-report=json:coverage.json 未生成?)")
        return 1
    try:
        with BASELINE_FILE.open(encoding="utf-8") as f:
            baseline = json.load(f)["line_coverage"]
    except Exception as e:
        print(f"[E5] 读基线失败 {BASELINE_FILE}: {e}")
        return 1
    try:
        current = json.loads(cov_file.read_text(encoding="utf-8"))["totals"]["percent_covered"]
    except Exception as e:
        print(f"[E5] 解析覆盖率报告失败: {e}")
        return 1

    current = round(float(current), 2)
    print(f"[E5] 行覆盖率: 当前 {current}% / 基线 {baseline}%")
    if current < baseline:
        print(f"[E5] RED: 覆盖率 {current}% 低于基线 {baseline}% —— 只降不升就是退步。")
        print("     补测试, 或确属测试冗余清理后按流程上调基线(禁止为过门禁偷偷下调)。")
        return 1
    if current >= baseline + 3:
        print(f"[E5] 提示: 覆盖率比基线高 {round(current - baseline, 2)}%, "
              f"波次结束时记得上调 baseline(scripts/coverage-baseline.json)。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
