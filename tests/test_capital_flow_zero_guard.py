"""资金流采集修复测试: P0 全0未初始化回退 + 悟道已移除。"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

import pytest
from src.collectors import capital_flow_collector as cfc


class TestDirectFlowZeroGuard:
    """P0: 东财 f62=0 全 0 应视为未初始化返回 None, 而不是当有效数据。"""

    @pytest.fixture(autouse=True)
    def _fake_requests(self, monkeypatch):
        """requests 在 _fetch_direct_flow 函数内 import, 通过 sys.modules 注入假模块。"""
        import sys

        class FakeResp:
            status_code = 200
            def __init__(self, diff):
                self._diff = diff
            def json(self):
                return {"data": {"diff": self._diff}}

        class FakeRequests:
            def __init__(self):
                self.captured_diff = None
            def get(self, url, headers=None, timeout=None, **kw):
                return FakeResp(self.captured_diff)

        fake = FakeRequests()
        monkeypatch.setitem(sys.modules, "requests", fake)
        self._fake_req = fake

    def _run(self, diff):
        self._fake_req.captured_diff = diff
        return cfc._fetch_direct_flow("000001")

    def test_all_zero_returns_none(self):
        """f62/f184/f66/f72 全 0 → None(开盘初期数据未就绪)。"""
        assert self._run([{"f62": 0, "f184": 0, "f66": 0, "f72": 0,
                           "f78": 0, "f84": 0, "f14": "测试"}]) is None

    def test_partial_zero_still_valid(self):
        """只有部分字段为 0(如 f78/f84 盘中为 0)仍应返回数据, 不算未初始化。"""
        cf = self._run([{"f62": 158793496, "f184": 6.5, "f66": 77561979, "f72": 0,
                         "f78": 0, "f84": 0, "f14": "测试"}])
        assert cf is not None
        assert cf.main_net_inflow == 158793496.0

    def test_none_f62_returns_none(self):
        """f62 为 None → None(原有行为保持)。"""
        assert self._run([{"f62": None, "f14": "测试"}]) is None


class TestWudaoRemoved:
    """悟道已从资金流链路移除(9:15-10:30 限流)。"""

    def test_no_wudao_import_in_get_capital_flow(self):
        """get_capital_flow 不应再调用 WudaoMCPClient(函数体, 不含 docstring)。"""
        import inspect
        src = inspect.getsource(cfc.CapitalFlowCollector.get_capital_flow)
        # 去掉 docstring 和注释行, 只看实际代码
        body = src.split('"""')[-1] if '"""' in src else src
        body = "\n".join(l for l in body.split("\n") if not l.strip().startswith("#"))
        assert "WudaoMCPClient" not in body
        assert "wc.call_tool" not in body
        assert "intraday_main_flow" not in body

    def test_docstring_mentions_removal(self):
        """注释应说明悟道移除原因。"""
        import inspect
        src = inspect.getsource(cfc.CapitalFlowCollector.get_capital_flow)
        assert "9:15-10:30" in src or "限流" in src
