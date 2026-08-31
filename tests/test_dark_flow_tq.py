"""md_dark_flow_tq 读取测试(通达信 L2 暗盘资金, ZCode TQ4 采集)。"""
import json

import pytest


def _write_darkflow(tmp_path, name, payload):
    d = tmp_path / "darkflow"
    d.mkdir(exist_ok=True)
    (d / name).write_text(json.dumps(payload), encoding="utf-8")


def test_md_dark_flow_tq_exact(tmp_path, monkeypatch):
    from src.core.marketdata_client import md_dark_flow_tq

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    _write_darkflow(tmp_path, "002361.json", {"symbol": "002361", "xl_net": -123.4})
    data = md_dark_flow_tq("002361")
    assert data == {"symbol": "002361", "xl_net": -123.4}


def test_md_dark_flow_tq_latest_date(tmp_path, monkeypatch):
    from src.core.marketdata_client import md_dark_flow_tq

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    _write_darkflow(tmp_path, "002361_20260827.json", {"date": "20260827"})
    _write_darkflow(tmp_path, "002361_20260828.json", {"date": "20260828"})
    data = md_dark_flow_tq("002361")
    assert data["date"] == "20260828"


def test_md_dark_flow_tq_missing(tmp_path, monkeypatch):
    from src.core.marketdata_client import md_dark_flow_tq

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert md_dark_flow_tq("000001") is None


def test_md_dark_flow_tq_bad_json(tmp_path, monkeypatch):
    from src.core.marketdata_client import md_dark_flow_tq

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    d = tmp_path / "darkflow"
    d.mkdir(exist_ok=True)
    (d / "002361.json").write_text("{bad json", encoding="utf-8")
    assert md_dark_flow_tq("002361") is None
