"""实验日志(B6.7): 记录每次回测/研究的参数、数据指纹与指标, 支持事后复现。

落地形态刻意选 JSONL(追加写、无迁移、可 git diff), 与 B0.5 的 `input_hash` 配合:
- `log_experiment(...)`: 追加一条 run(自动生成 run_id/时间戳);
- `list_experiments(...)` / `find_experiment(run_id)`;
- `reproduce_command(run_id)`: 把当时参数还原成可直接复跑的 dict(含数据指纹校验提示)。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

_EXPERIMENTS_FILE = "experiments.jsonl"


def _log_path(path: str | os.PathLike | None = None) -> Path:
    if path is not None:
        return Path(path)
    base = os.environ.get("DATA_DIR") or "data"
    return Path(base) / _EXPERIMENTS_FILE


def log_experiment(
    name: str,
    *,
    params: dict | None = None,
    metrics: dict | None = None,
    data_fingerprint: str = "",
    notes: str = "",
    path: str | os.PathLike | None = None,
) -> dict:
    """追加一条实验记录, 返回完整记录(含 run_id)。"""
    record = {
        "run_id": uuid.uuid4().hex[:12],
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "name": str(name or "unnamed"),
        "params": params or {},
        "metrics": metrics or {},
        "data_fingerprint": str(data_fingerprint or ""),
        "notes": str(notes or ""),
    }
    p = _log_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return record


def list_experiments(
    *, limit: int = 20, name: str | None = None, path: str | os.PathLike | None = None
) -> list[dict]:
    """倒序列出实验记录(最近 limit 条)。"""
    p = _log_path(path)
    if not p.exists():
        return []
    out: list[dict] = []
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if name and rec.get("name") != name:
                continue
            out.append(rec)
    return list(reversed(out))[: max(1, int(limit))]


def find_experiment(run_id: str, *, path: str | os.PathLike | None = None) -> dict | None:
    for rec in list_experiments(limit=10_000, path=path):
        if rec.get("run_id") == run_id:
            return rec
    return None


def reproduce_command(run_id: str, *, path: str | os.PathLike | None = None) -> dict | None:
    """还原当时的复跑参数(供人工/脚本复现)。"""
    rec = find_experiment(run_id, path=path)
    if rec is None:
        return None
    return {
        "run_id": rec["run_id"],
        "name": rec["name"],
        "params": rec.get("params") or {},
        "expected_data_fingerprint": rec.get("data_fingerprint") or "",
        "expected_metrics": rec.get("metrics") or {},
        "hint": "若当前数据指纹与 expected_data_fingerprint 不一致, 结果不可直接对比",
    }
