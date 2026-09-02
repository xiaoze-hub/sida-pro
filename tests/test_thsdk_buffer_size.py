"""v0.4.59 修复验证: buffer_size 注入 THSDKL2._query

策略: Mock THS 上下文, 检查 method() 调用是否拿到 buffer_size=THS_BUFFER_SIZE.
不依赖真实 thsdk 安装 (CI 环境可能没有).
"""
import sys, types
from unittest import mock

# 装一个最小 thsdk 包, 避免 import 失败
if "thsdk" not in sys.modules:
    fake = types.ModuleType("thsdk")
    class _THS:
        def __init__(self, cfg=None): self.cfg = cfg or {}
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def tick_super_level1(self, code, buffer_size=None, **kw):
            return types.SimpleNamespace(success=True, data=[{"time":1,"price":2}], error="", buffer_size_received=buffer_size)
    fake.THS = _THS
    sys.modules["thsdk"] = fake

import data_source.thsdk_l2 as M

def test_buffer_size_injected():
    """_query 必须把 THS_BUFFER_SIZE 传给 method()"""
    l2 = M.THSDKL2.__new__(M.THSDKL2)  # skip __init__
    # Mock 掉内部依赖
    l2._rate_limit = lambda: None
    l2._check_circuit = lambda: None
    l2._build_config = lambda: {"username":"u","password":"p"}
    l2._record_success = lambda: None
    r = l2._query("tick_super_level1", "USZA002361")
    assert hasattr(r, "buffer_size_received"), "method 应该被注入 buffer_size"
    assert r.buffer_size_received == M.THS_BUFFER_SIZE, f"应={M.THS_BUFFER_SIZE}, 实={r.buffer_size_received}"
    assert M.THS_BUFFER_SIZE == 8 * 1024 * 1024, "buffer size 应=8MB"
    print(f"✅ buffer_size 注入正确: {r.buffer_size_received // (1024*1024)} MB")

def test_buffer_size_module_constant():
    """THS_BUFFER_SIZE 是模块级常量 = 8MB"""
    assert hasattr(M, "THS_BUFFER_SIZE"), "THS_BUFFER_SIZE 必须存在"
    assert M.THS_BUFFER_SIZE == 8 * 1024 * 1024, f"应=8MB, 实={M.THS_BUFFER_SIZE//(1024*1024)}MB"
    print(f"✅ THS_BUFFER_SIZE = {M.THS_BUFFER_SIZE // (1024*1024)} MB")

if __name__ == "__main__":
    test_buffer_size_module_constant()
    test_buffer_size_injected()
    print("ALL OK")