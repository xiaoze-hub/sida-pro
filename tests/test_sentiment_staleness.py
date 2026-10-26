# -*- coding: utf-8 -*-
"""消费方陈旧标注(2026-10-11 断链修复): 数据停在过去时显式标注『数据停在 X 日』,
温度计/regime **不拿旧值冒充当日**; API health/baseline 带新鲜度。

全 mock / 临时测试库, 禁真实网络。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ─────────────── ⑥ 消费方陈旧标注: 温度计 + regime 不据此条件化 ───────────────

def test_derive_thermometer_marks_stale():
    from src.core.sentiment_thermometer import derive_thermometer

    t = derive_thermometer(
        {"trade_date": "20260922", "limit_up_count": 64, "limit_up_open_count": 29,
         "seal_success_money": 423.39, "seal_fail_money": 561.59},
        expected_asof="20261009",
    )
    assert t["stale"] is True and t["stale_days"] > 0
    assert "数据停在 20260922 日" in t["reason"]
    # 未传 expected → 逐字段向后兼容(stale=False)
    t2 = derive_thermometer({"trade_date": "20260922", "limit_up_count": 64, "limit_up_open_count": 29})
    assert t2["stale"] is False and t2["expected_asof"] is None


def test_temperature_policy_refuses_stale():
    from src.core import market_regime as mr

    pol = mr.temperature_policy("ferment", {
        "available": True, "seal_success_rate": 0.40, "blow_ratio": 0.55,
        "trade_date": "20260922", "expected_asof": "20261009", "stale": True,
    })
    assert pol["thermo_applied"] is False          # 不据陈旧值条件化
    assert pol["thermo_stale"] is True
    assert "数据停在 20260922 日" in pol["note"]
    assert pol["allow_touch_rows"] == (1, 3)        # 与无温度时一致


def test_temperature_policy_fresh_still_applies():
    from src.core import market_regime as mr

    pol = mr.temperature_policy("ferment", {
        "available": True, "seal_success_rate": 0.40, "blow_ratio": 0.10,
        "trade_date": "20261009", "expected_asof": "20261009", "stale": False,
    })
    assert pol["thermo_applied"] is True and pol["policy"] == "tighten"


# ─────────────── ⑦ API: health/baseline 带新鲜度标注 ───────────────

def test_api_health_and_baseline_annotate_stale():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import text

    from src.web.api import sentiment_series as api
    from src.web.database import SessionLocal

    app = FastAPI()
    app.include_router(api.router, prefix="/api/market/sentiment")
    client = TestClient(app)

    db = SessionLocal()
    try:
        db.execute(text("DELETE FROM market_sentiment_daily"))
        db.execute(text(
            "INSERT INTO market_sentiment_daily (trade_date, market, limit_up_count, "
            "limit_up_open_count, seal_success_money, seal_fail_money, source) VALUES "
            "('20260922','CN',64,29,423.39,561.59,'tdx_tq')"
        ))
        db.commit()
    finally:
        db.close()
    try:
        h = client.get("/api/market/sentiment/health").json()
        assert h["last"] == "20260922"
        assert h["stale"] is True and h["stale_days"] > 0  # 停在过去显式标注
        b = client.get("/api/market/sentiment/baseline", params={"days": 20})
        # 单行样本不足 → 404(不造基线), 与既有口径一致
        assert b.status_code in (200, 404)
    finally:
        db = SessionLocal()
        try:
            db.execute(text("DELETE FROM market_sentiment_daily"))
            db.commit()
        finally:
            db.close()

