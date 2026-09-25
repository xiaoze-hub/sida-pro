"""TQ 打新日历 / 可转债条款 / 板块归属 —— 解析与对话工具测试。

CI 无 TQ 网关, 全部 monkeypatch 假源(与 test_tq_formula_scan.py 同惯例)。

覆盖两个**会造成错误信息**的行为:
- `kzz_info` 原实现判 `isinstance(v, dict)` 而真实响应是 list → **恒返 {}**(静默空,
  用户问"强赎价多少"永远得到"查不到")。
- 客户端常用 `0` 表示"字段还没出"(申购价/申购上限/市盈率), 工具必须说成**未披露/待定**,
  否则会被读成真实值 0 元、0 倍市盈率(用户对编造数字敏感)。
"""

from __future__ import annotations

import pytest

from marketdata.vendors import tq as tqmod

# ─────────────────────────── vendor 层 ───────────────────────────


def test_ipo_info_normalizes_rows(monkeypatch):
    """真实响应: 直接 list[dict], 字段全字符串。"""
    monkeypatch.setattr(tqmod, "_rpc", lambda m, p, **kw: [
        {"Code": "301660.SZ", "Name": "粤芯半导体", "SGDate": "20260924",
         "SGPrice": "12.01", "SGCode": "301660", "MaxSG": "12.80", "PE_Issue": "0.00"},
    ])
    rows = tqmod.ipo_info(0, 0)
    assert len(rows) == 1
    r = rows[0]
    assert r["code"] == "301660.SZ" and r["name"] == "粤芯半导体"
    assert r["sg_date"] == "20260924"
    assert r["sg_price"] == pytest.approx(12.01)
    assert r["max_sg"] == pytest.approx(12.80)
    assert r["pe_issue"] == 0.0          # 0 = 未披露, 原样保留由消费侧解释
    assert r["ipo_type"] == 0 and r["ipo_type_cn"] == "新股"


def test_ipo_info_tolerates_wrapped_and_empty(monkeypatch):
    """某些版本可能套一层 Value; 空壳行(无 Code)必须被过滤掉。"""
    monkeypatch.setattr(tqmod, "_rpc", lambda m, p, **kw: {
        "ErrorId": "0",
        "Value": [{"Name": "无代码行"}, {"Code": "301718.SZ", "Name": "通则康威"}],
    })
    rows = tqmod.ipo_info(1, 1)
    assert [r["code"] for r in rows] == ["301718.SZ"]
    assert rows[0]["ipo_type_cn"] == "新发债"

    monkeypatch.setattr(tqmod, "_rpc", lambda m, p, **kw: {"ErrorId": "2", "Error": "x"})
    assert tqmod.ipo_info() == []        # 非 list/dict 包裹 → 空, 不炸


def test_ipo_info_passes_filters_through(monkeypatch):
    seen = {}

    def _fake(m, p, **kw):
        seen.update(p)
        return []

    monkeypatch.setattr(tqmod, "_rpc", _fake)
    tqmod.ipo_info(2, 1)
    assert seen == {"ipo_type": 2, "ipo_date": 1}


def test_kzz_info_takes_first_row_from_list(monkeypatch):
    """回归: 响应是 result.Value = [ {...} ], 旧实现恒返 {}。"""
    monkeypatch.setattr(tqmod, "_rpc", lambda m, p, **kw: [
        {"KZZCode": "128136", "KZZName": "立讯转债", "ZGPrice": "52.00",
         "ForceRedeem": "72.370", "HSScore": "AA+"},
    ])
    d = tqmod.kzz_info("128136.SZ")
    assert d["KZZName"] == "立讯转债"
    assert d["ForceRedeem"] == "72.370"


def test_kzz_info_handles_dict_and_empty(monkeypatch):
    monkeypatch.setattr(tqmod, "_rpc", lambda m, p, **kw: {"KZZCode": "110059"})
    assert tqmod.kzz_info("110059.SH")["KZZCode"] == "110059"

    monkeypatch.setattr(tqmod, "_rpc", lambda m, p, **kw: [])
    assert tqmod.kzz_info("110059.SH") == {}


# ─────────────────────────── 对话工具层 ───────────────────────────

registry = pytest.importorskip("src.agents.chat.registry")


def test_tool_ipo_calendar_marks_undisclosed(monkeypatch):
    """0 必须显示为 待定/未披露 —— 不能出现 "申购价 0 元"。"""
    monkeypatch.setattr(tqmod, "ipo_info", lambda t=2, d=1: [
        {"code": "301718.SZ", "name": "通则康威", "sg_date": "20261009",
         "sg_price": 0.0, "sg_code": "301718", "max_sg": 0.0, "pe_issue": 0.0,
         "ipo_type": 0, "ipo_type_cn": "新股"},
        {"code": "301660.SZ", "name": "粤芯半导体", "sg_date": "20260924",
         "sg_price": 12.01, "sg_code": "301660", "max_sg": 12.8, "pe_issue": 0.0,
         "ipo_type": 0, "ipo_type_cn": "新股"},
    ])
    import asyncio

    text = asyncio.run(registry._tool_get_ipo_calendar(None, {"kind": "new"}, None))
    assert "通则康威" in text and "2026-10-09" in text
    assert "申购价 待定" in text         # sg_price=0 → 待定
    assert "申购价 0 元" not in text
    assert "发行市盈率 未披露" in text
    assert "12.01" in text


def test_tool_ipo_calendar_empty_and_unavailable(monkeypatch):
    import asyncio

    monkeypatch.setattr(tqmod, "ipo_info", lambda t=2, d=1: [])
    text = asyncio.run(registry._tool_get_ipo_calendar(None, {}, None))
    assert "没有" in text and "通达信" in text

    def _boom(*a, **kw):
        raise RuntimeError("TQ 网关不可达")

    monkeypatch.setattr(tqmod, "ipo_info", _boom)
    text = asyncio.run(registry._tool_get_ipo_calendar(None, {}, None))
    assert "取不到" in text and "不要用其它来源猜测替代" in text


def test_tool_kzz_terms_self_heals_suffix(monkeypatch):
    """裸码在客户端报 codestr error → 工具要自动补后缀再试, 而不是直接说查不到。"""
    calls = []

    def _fake(code):
        calls.append(code)
        if code == "128136":
            raise RuntimeError('TQ get_kzz_info ErrorId=2: codestr error:128136')
        return {"KZZCode": "128136", "KZZName": "立讯转债", "ZGPrice": "52.00",
                "HSScore": "AA+", "ForceRedeem": "72.370"}

    monkeypatch.setattr(tqmod, "kzz_info", _fake)
    import asyncio

    text = asyncio.run(registry._tool_get_kzz_terms(None, {"code": "128136"}, None))
    assert calls == ["128136", "128136.SZ"]          # 先裸码后 .SZ, 命中即停
    assert "立讯转债" in text and "强赎触发价 72.370" in text


def test_tool_kzz_terms_requires_code():
    import asyncio

    text = asyncio.run(registry._tool_get_kzz_terms(None, {}, None))
    assert "请提供可转债代码" in text


def test_tool_stock_sectors_groups_by_type(monkeypatch):
    monkeypatch.setattr(tqmod, "relation", lambda code: [
        {"BlockCode": "881044.SH", "BlockName": "塑料", "BlockType": "行业", "GPNume": "95"},
        {"BlockCode": "880507.SH", "BlockName": "国防军工", "BlockType": "概念", "GPNume": "537"},
        {"BlockCode": "880224.SH", "BlockName": "安徽板块", "BlockType": "地区", "GPNume": "193"},
    ])
    import asyncio

    text = asyncio.run(registry._tool_get_stock_sectors(None, {"symbol": "002361"}, None))
    assert "002361.SZ" in text                        # 自动补后缀
    assert "行业: 塑料" in text and "概念: 国防军工" in text
    # 行业要排在概念前(输出顺序稳定)
    assert text.index("行业:") < text.index("概念:")


def test_tool_stock_sectors_empty_is_honest(monkeypatch):
    monkeypatch.setattr(tqmod, "relation", lambda code: [])
    import asyncio

    text = asyncio.run(registry._tool_get_stock_sectors(None, {"symbol": "002361"}, None))
    assert "没给出" in text
