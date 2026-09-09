"""中立路径解析(KI-039 第二阶段, 2026-09-09)。

从 `src/web/api/reports.py` 下沉: 报告中心根目录解析是**数据落盘位置**,
不是 Web 关注点 —— core 的 report_generator 也要用同一处解析。

约定:
- `HERMES_HOME`(或 `CRON_OUTPUT_DIR`)指定主机侧 hermes 挂载点, 默认 /hermes;
- 不可写时退到 `DATA_DIR`(容器持久卷 /app/data), 保证盘前/盘后报告仍能落盘。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

HERMES_HOME = Path(
    os.environ.get("HERMES_HOME")
    or os.environ.get("CRON_OUTPUT_DIR")
    or "/hermes"  # 推荐挂载点
)


def _pick_report_root() -> Path:
    preferred = HERMES_HOME / "cron" / "output"
    try:
        preferred.mkdir(parents=True, exist_ok=True)
        return preferred
    except OSError as e:
        fallback = Path(os.environ.get("DATA_DIR") or "/app/data") / "cron" / "output"
        try:
            fallback.mkdir(parents=True, exist_ok=True)
        except OSError:
            return preferred
        logger.warning("报告目录 %s 不可用(%s), 改用 %s", preferred, e, fallback)
        return fallback


CRON_OUTPUT_DIR = _pick_report_root()
