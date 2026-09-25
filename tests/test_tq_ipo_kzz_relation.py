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


# ───────── 交易日判定: 必须走统一日历, 不能按 weekday 近似 ─────────


def test_recent_trading_days_are_real_trading_days():
    """回归: 旧实现按 `weekday()<5` 倒推, 会把**周中假日**当交易日。

    2026-09-25 是中秋节(周五休市) —— 在旧实现下这个断言必挂。
    这里只用性质断言(不固定"今天"), 任意日期运行都成立:
    返回的每一天都必须是日历认可的交易日, 且严格递减、格式为 YYYYMMDD。
    """
    from src.core.trading_calendar import is_trading_day
    from src.core.tq_formula_signal_scheduler import _recent_trading_days

    days = _recent_trading_days(6)
    assert len(days) == 6
    for d in days:
        assert len(d) == 8 and d.isdigit(), d
        assert is_trading_day(f"{d[:4]}-{d[4:6]}-{d[6:8]}"), f"{d} 不是交易日"
    assert days == sorted(days, reverse=True)
    assert len(set(days)) == 6


def test_market_day_uses_calendar_not_weekday():
    """`_is_market_day` 必须是日历判定: 中秋节(2026-09-25, 周五)在日历里是休市日。"""
    from datetime import date

    from src.core.trading_calendar import is_trading_day
    from src.core.tq_formula_signal_scheduler import _is_market_day

    assert is_trading_day(date(2026, 9, 25)) is False   # 中秋, 周五
    assert is_trading_day(date(2026, 9, 24)) is True    # 节前最后交易日
    assert isinstance(_is_market_day(), bool)           # 不抛异常


# ───────── 日志如实性: 会被重试的中间失败不许留下假警报 (2026-09-25 生产验证实录) ─────────


def test_kzz_self_heal_leaves_no_false_warning(monkeypatch, caplog):
    """裸码失败 → 补后缀成功时, **不得**打 WARNING。

    生产实录: 一次**成功**的 `get_kzz_terms 128136` 在日志里留下
    `chat TQ 工具 kzz_info 失败: ... codestr error:128136` —— 读日志的人据此误判工具坏了。
    """
    import asyncio
    import logging

    calls: list[str] = []

    def _fake(code):
        calls.append(code)
        if code.endswith(".SZ"):
            return {"KZZCode": "128136", "ZGPrice": "55.670", "HSScore": "AA+"}
        raise RuntimeError(f"TQ get_kzz_info ErrorId=2: codestr error:{code}")

    monkeypatch.setattr(tqmod, "kzz_info", _fake)
    with caplog.at_level(logging.WARNING):
        text = asyncio.run(registry._tool_get_kzz_terms(None, {"code": "128136"}, None))

    assert "55.670" in text and "128136.SZ" in text      # 自愈成功
    assert calls == ["128136", "128136.SZ"]              # 先裸码, 再补后缀
    assert [r for r in caplog.records if "chat TQ 工具" in r.getMessage()] == []


def test_kzz_all_candidates_failed_logs_exactly_one_warning(monkeypatch, caplog):
    """全部候选都失败时: 只打一条 WARNING, 且内容含所有试过的代码(日志与结论一致)。"""
    import asyncio
    import logging

    def _boom(code):
        raise RuntimeError(f"TQ get_kzz_info ErrorId=2: codestr error:{code}")

    monkeypatch.setattr(tqmod, "kzz_info", _boom)
    with caplog.at_level(logging.WARNING):
        text = asyncio.run(registry._tool_get_kzz_terms(None, {"code": "123456"}, None))

    assert "取不到该数据" in text and "不要用其它来源猜测替代" in text
    ours = [r for r in caplog.records if "chat TQ 工具" in r.getMessage()]
    assert len(ours) == 1
    assert "123456.SZ" in ours[0].getMessage() and "123456.SH" in ours[0].getMessage()


# ────────────── 第二批: search_symbols / get_index_etfs / download_client_data ──────────────


def test_norm_down_time_always_string_yyyymmdd():
    """客户端不收 int: 实测传 int 20260924 → ErrorId=10 RPC处理异常。必须归一成字符串。"""
    from datetime import date, datetime

    from marketdata.vendors.tq import _norm_down_time

    assert _norm_down_time("20260924") == "20260924"
    assert _norm_down_time(20260924) == "20260924"          # int → str(关键修复)
    assert _norm_down_time("2026-09-24") == "20260924"
    assert _norm_down_time("2026-09-24 00:00:00") == "20260924"
    assert _norm_down_time(date(2026, 9, 24)) == "20260924"
    assert _norm_down_time(datetime(2026, 9, 24, 15, 30)) == "20260924"
    assert _norm_down_time("") == ""                        # type 3/4 忽略该字段
    assert _norm_down_time("2026") == "2026"                # 归一不了就原样交客户端报错, 不猜


def test_tool_search_symbols_labels_cross_market(monkeypatch):
    """同名跨市场必须标出市场 —— 否则 03750.HK 会被当成 A 股用。"""
    import asyncio

    monkeypatch.setattr(tqmod, "match_stkinfo", lambda kw: [
        {"Code": "300750.SZ", "Name": "宁德时代"},
        {"Code": "03750.HK", "Name": "宁德时代"},
        {"Code": "002361.OF", "Name": "国富恒瑞债券A"},
    ])
    text = asyncio.run(registry._tool_search_symbols(None, {"keyword": "宁德时代"}, None))
    assert "300750.SZ(深市)" in text and "03750.HK(港股)" in text and "002361.OF(场外基金)" in text
    assert "共 3 条" in text


def test_tool_search_symbols_empty_and_unavailable(monkeypatch):
    """查不到返回 None(不是 []) → 如实说没有, 不说成"没数据"。"""
    import asyncio

    monkeypatch.setattr(tqmod, "match_stkinfo", lambda kw: None)
    assert "没有匹配「xxxxxx」" in asyncio.run(
        registry._tool_search_symbols(None, {"keyword": "xxxxxx"}, None))

    def _boom(kw):
        raise RuntimeError("conn refused")

    monkeypatch.setattr(tqmod, "match_stkinfo", _boom)
    out = asyncio.run(registry._tool_search_symbols(None, {"keyword": "神剑"}, None))
    assert "取不到该数据" in out and "不要用其它来源猜测替代" in out
    assert asyncio.run(registry._tool_search_symbols(None, {}, None)).startswith("请提供检索关键词")


def test_tool_get_index_etfs_sort_premium_and_selfheal(monkeypatch):
    """按规模降序 + 折溢价率自带计算 + 裸码后缀自愈。"""
    import asyncio

    calls: list[str] = []

    def _fake(zs):
        calls.append(zs)
        if zs == "000300.SH":
            return [
                {"Code": "159925.SZ", "Name": "沪深300ETF南方", "Sz": "28.61",
                 "NowPrice": "4.591", "IOPV": "4.5889"},
                {"Code": "510300.SH", "Name": "沪深300ETF华泰", "Sz": "1200.00",
                 "NowPrice": "4.6000", "IOPV": "4.6000"},
                {"Code": "512999.SH", "Name": "无IOPV的ETF", "Sz": "5.00",
                 "NowPrice": "1.000", "IOPV": "0"},
            ]
        return []

    monkeypatch.setattr(tqmod, "trackzs_etf", _fake)
    text = asyncio.run(registry._tool_get_index_etfs(None, {"index_code": "000300"}, None))
    assert calls == ["000300.SH"]                       # 裸码 → 补 .SH 自愈成功
    assert "跟踪 000300.SH" in text and "共 3 只" in text
    # 规模降序: 1200 的应排在 28.61 前面
    assert text.index("510300.SH") < text.index("159925.SZ")
    assert "折溢价 +0.05%" in text                      # (4.591-4.5889)/4.5889 = +0.046%
    assert "折溢价 +0.00%" in text                      # 现价==IOPV
    assert "512999.SH" in text and "IOPV 0" in text     # IOPV=0 时不给折溢价数字(不编)
    assert text.count("折溢价") == 2


def test_tool_get_index_etfs_not_found_and_missing_arg(monkeypatch):
    import asyncio

    monkeypatch.setattr(tqmod, "trackzs_etf", lambda zs: [])
    out = asyncio.run(registry._tool_get_index_etfs(None, {"index_code": "930599.CSI"}, None))
    assert "没有指数「930599.CSI」的跟踪 ETF" in out and "试过" in out
    assert asyncio.run(registry._tool_get_index_etfs(None, {}, None)).startswith("请提供指数代码")


def test_tool_download_client_data_requires_date_and_code(monkeypatch):
    """类型 1/2/5 缺参 → 明说缺什么, **不能**默认成今天去下(客户端按年度取数)。"""
    import asyncio

    def _boom(**kw):
        raise AssertionError("缺参时不该调用 vendor")

    monkeypatch.setattr(tqmod, "download_file", _boom)
    out = asyncio.run(registry._tool_download_client_data(None, {"data_type": 5}, None))
    assert "需要指定日期" in out and "20260924" in out
    out2 = asyncio.run(registry._tool_download_client_data(
        None, {"data_type": 5, "date": "20260924"}, None))
    assert "需要指定证券代码" in out2
    out3 = asyncio.run(registry._tool_download_client_data(None, {"data_type": 9}, None))
    assert out3.startswith("请提供 data_type")


def test_tool_download_client_data_reports_client_receipt(monkeypatch):
    """如实转述客户端回执, 且**说明这是喂数据不是取数**。"""
    import asyncio

    seen: dict = {}

    def _fake(*, stock_code=None, down_time=None, down_type=None):
        seen.update(stock_code=stock_code, down_time=down_time, down_type=down_type)
        return {"ErrorId": "0", "Msg": "下载经营分析数据文件[2026]成功。", "run_id": "0"}

    monkeypatch.setattr(tqmod, "download_file", _fake)
    text = asyncio.run(registry._tool_download_client_data(
        None, {"data_type": 5, "stock_code": "688318.SH", "date": "20260924"}, None))
    assert seen == {"stock_code": "688318.SH", "down_time": "20260924", "down_type": 5}
    assert "下载经营分析数据文件[2026]成功。" in text
    assert "不是取数结果" in text and "PYPlugins" in text
