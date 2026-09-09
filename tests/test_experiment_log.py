"""B6.7: 实验日志(追加写 JSONL) + 复跑参数还原。"""

from __future__ import annotations

from src.core.experiment_log import (
    find_experiment,
    list_experiments,
    log_experiment,
    reproduce_command,
)


def test_log_and_list_roundtrip(tmp_path):
    p = tmp_path / "exp.jsonl"
    r1 = log_experiment("bt_ma_cross", params={"holding_days": 5},
                        metrics={"sharpe": 1.2}, data_fingerprint="abc123", path=p)
    r2 = log_experiment("bt_ma_cross", params={"holding_days": 10},
                        metrics={"sharpe": 0.8}, path=p)
    assert r1["run_id"] != r2["run_id"]

    rows = list_experiments(path=p)
    assert [r["run_id"] for r in rows] == [r2["run_id"], r1["run_id"]]  # 倒序
    assert list_experiments(name="bt_ma_cross", path=p) == rows
    assert list_experiments(name="other", path=p) == []


def test_find_and_reproduce(tmp_path):
    p = tmp_path / "exp.jsonl"
    rec = log_experiment("bt_x", params={"stop_pct": 0.08}, metrics={"mdd": 0.2},
                         data_fingerprint="fp1", path=p)
    assert find_experiment(rec["run_id"], path=p)["name"] == "bt_x"
    cmd = reproduce_command(rec["run_id"], path=p)
    assert cmd["params"] == {"stop_pct": 0.08}
    assert cmd["expected_data_fingerprint"] == "fp1"
    assert "指纹" in cmd["hint"]
    assert reproduce_command("nope", path=p) is None


def test_missing_file_and_bad_lines(tmp_path):
    p = tmp_path / "exp.jsonl"
    assert list_experiments(path=p) == []  # 文件不存在
    p.write_text('{"run_id":"a","name":"x"}\nnot-json\n', encoding="utf-8")
    rows = list_experiments(path=p)
    assert len(rows) == 1 and rows[0]["run_id"] == "a"
