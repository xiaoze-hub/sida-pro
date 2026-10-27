"""Filesystem paths used by the forecast engine."""
from __future__ import annotations

import os


def resolve_forecast_db_path() -> str:
    """Resolve and prepare the persistent forecast-history database path.

    2026-10-27 改名：默认文件名 `~/.sida_forecast.db`。若新文件不存在而旧的
    `~/.panwatch_forecast.db` 在，则**复制**一份过去（旧文件保留作备份），
    否则预测引擎会以为历史全丢、从零重建。
    """
    configured = os.getenv("FORECAST_DB_PATH", "~/.sida_forecast.db")
    path = os.path.abspath(os.path.expanduser(configured))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    legacy = os.path.join(os.path.dirname(path) or ".", ".panwatch_forecast.db")
    if not os.path.exists(path) and os.path.exists(legacy):
        import shutil

        try:
            shutil.copy2(legacy, path)
        except OSError:
            pass
    return path


FORECAST_DB_PATH = resolve_forecast_db_path()
