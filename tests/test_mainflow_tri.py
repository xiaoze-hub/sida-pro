"""明盘三源交叉验证单测(P1)。"""
from unittest.mock import patch

from src.core.mainflow_tri import judge_agree, triangulate


class TestJudgeAgree:
    def test_all_agree(self):
        r = judge_agree([3498.0, 3794.7, 3600.0])
        assert r["agree"] is True and r["n_ok"] == 3

    def test_sign_conflict(self):
        r = judge_agree([3498.0, -3794.7])
        assert r["agree"] is False

    def test_big_spread(self):
        r = judge_agree([100.0, 10000.0])
        assert r["agree"] is False and r["spread_pct"] > 50

    def test_single_source(self):
        r = judge_agree([100.0, None, None])
        assert r["agree"] is None and r["n_ok"] == 1

    def test_all_none(self):
        r = judge_agree([None, None])
        assert r["agree"] is None and r["consensus_wan"] is None


class TestTriangulate:
    def test_never_raises(self):
        with patch("src.core.mainflow_tri._tencent_net", return_value={"net_wan": 3498.0}), \
             patch("src.core.mainflow_tri._thsdk_net", side_effect=Exception("x")), \
             patch("src.core.mainflow_tri._tq_net", return_value=None):
            r = triangulate("002361")
        assert r["n_ok"] == 1 and r["sources"]["thsdk"] is None
