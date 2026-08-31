"""Filesystem paths used by the forecast engine."""
from __future__ import annotations

import os


def resolve_forecast_db_path() -> str:
    """Resolve and prepare the persistent forecast-history database path."""
    configured = os.getenv("FORECAST_DB_PATH", "~/.panwatch_forecast.db")
    path = os.path.abspath(os.path.expanduser(configured))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


FORECAST_DB_PATH = resolve_forecast_db_path()
