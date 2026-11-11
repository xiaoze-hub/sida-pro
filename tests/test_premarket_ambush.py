"""盘前潜伏表解析与信号明细（2026-11-11）。

样本取自**生产真实报告**（2026-11-11 09:11 盘前分析正文），不是手搓的理想表格 ——
LLM 会把妖股标记写成 `金牛化工**【妖股先锋·极妖】**`、会在同一份正文里放多张表，
解析必须扛得住，且解析不到时明确返回空（页面显示「未解析到」）。
"""

from __future__ import annotations

from src.core import premarket_ambush as pa

REAL_REPORT = """
【盘前分析】北方稀土(600111)

0. 涨价题材扫描（按供给链排序）

① PCB/覆铜板·端侧AI硬件（A级，今日最优） 涨价主体：PCB/覆铜板及电子细分品。

0. 事件驱动前瞻（双轨制 · 潜伏表）
| 事件 | 受益板块 | 代码 | 名称 | 受益逻辑 | 强度 | 埋伏方式 | 分批计划 | 止损 | 目标 | 证伪条件 |
|---|---|---|---|---|---|---|---|---|---|---|
| OpenAI端侧拉货+AI眼镜+存储成本松绑 | 覆铜板/PCB | 600183 | 生益科技 | 覆铜板龙头，涨价直接传导、AI服务器+端侧双需求 | A | 回调埋伏（回踩5日线） | 首批1/3，站稳5日线加1/3 | 跌破10日线或-5% | 前高上方+10% | 12月提价函落空、存储转跌 |
| 电子细分12月结构性涨价 | MLCC/被动元件 | 000636 | 风华高科 | 龙头资本开支+折旧下降，提价弹性大 | A- | 突破确认（放量过平台） | 首批1/4，提价公告落地加1/3 | -5%或跌回平台下沿 | 平台高度1:1 | 龙头否认提价、量能不配合 |
| 化工品8个月来首次提价（品种待确认） | 化学制品 | 600722 | 金牛化工**【妖股先锋·极妖】** | 提价+24次封板股性记忆 | B+ | 竞价观察（高开>2%且量能配合） | 首批1/4 | -5% | +8~10% | 提价品种与公司主营无关 |
| 土耳其钢厂爆炸+汽零主线 | 钢帘线/汽零 | 603278 | 大业股份**【妖股先锋·极妖 87.5】** | 海外钢供给扰动+题材共振，股性最强 | B+ | 回调埋伏（回踩5日线不破） | 首批1/3，封板确认加1/4 | 跌破5日线 | +10% | 钢价未动、板块无扩散 |
| 闽发铝业7板带动铝加工 | 铝加工 | 001400 | 江顺科技**【妖股先锋·极妖】** | 铝挤压模具，低位铝链妖股 | B | 竞价观察 | 1/4 | -5% | +8% | 闽发炸板、铝价回落 |

埋伏要点：优先级 PCB/覆铜板(A) > 铝加工/钢帘线妖股(B+)。

0.7 今日不碰清单
| 方向 | 理由 |
|---|---|
| 闽发铝业及4板以上高位连板 | 梯队断层 |
"""


def test_parses_real_ambush_table():
    rows = pa.parse_ambush_table(REAL_REPORT)
    assert [r["code"] for r in rows] == ["600183", "000636", "600722", "603278", "001400"]
    first = rows[0]
    assert first["name"] == "生益科技"
    assert first["sector"] == "覆铜板/PCB"
    assert first["strength"] == "A"
    assert first["stop"] == "跌破10日线或-5%"
    assert first["tags"] == []


def test_extracts_demon_tags_and_strips_them_from_name():
    rows = {r["code"]: r for r in pa.parse_ambush_table(REAL_REPORT)}
    assert rows["600722"]["name"] == "金牛化工"
    assert any("妖股先锋" in t for t in rows["600722"]["tags"])
    assert rows["603278"]["name"] == "大业股份"
    assert any("87.5" in t for t in rows["603278"]["tags"])


def test_stops_at_first_non_table_after_rows():
    """表格后面紧跟的文字不能被当成行（'埋伏要点：...' 不是表格）。"""
    rows = pa.parse_ambush_table(REAL_REPORT)
    assert all(r["code"].isdigit() and len(r["code"]) == 6 for r in rows)
    assert len(rows) == 5


def test_no_table_returns_empty_not_fabricated():
    assert pa.parse_ambush_table("今日无事件驱动前瞻，只有指数点评。") == []
    assert pa.parse_ambush_table("") == []


def test_escaped_pipe_in_cell_is_kept():
    text = (
        "潜伏表\n| 事件 | 受益板块 | 代码 | 名称 | 受益逻辑 | 强度 |\n|---|---|---|---|---|---|\n"
        "| A \\| B | 化工 | 600183 | 生益科技 | x | A |\n"
    )
    rows = pa.parse_ambush_table(text)
    assert rows and rows[0]["event"] == "A | B"
    assert rows[0]["code"] == "600183"


def test_stop_price_only_when_numeric():
    assert pa._parse_stop_price("35.60") == 35.60
    assert pa._parse_stop_price("跌破10日线或-5%") is None
    assert pa._parse_stop_price("-5%") is None
    assert pa._parse_stop_price("") is None


def test_price_signals_counts_limit_up_streak():
    # 主板 10% 涨停：连续两根涨停后回落
    closes = [("2026-11-06", 10.0), ("2026-11-09", 11.0), ("2026-11-10", 12.1), ("2026-11-11", 12.2)]
    sig = pa.price_signals(closes, "600183")
    assert sig["close"] == 12.2
    assert sig["pct"] == 0.83
    assert sig["limit_up_streak"] == 0  # 最后一根没涨停 → 连板数 0（不是历史 2）
    closes2 = [("2026-11-09", 10.0), ("2026-11-10", 11.0), ("2026-11-11", 12.1)]
    sig2 = pa.price_signals(closes2, "600183")
    assert sig2["limit_up_streak"] == 2


def test_price_signals_uses_20pct_for_chinext():
    closes = [("2026-11-10", 10.0), ("2026-11-11", 11.0)]  # +10% 在创业板不算涨停
    assert pa.price_signals(closes, "300123")["limit_up_streak"] == 0
    closes_20 = [("2026-11-10", 10.0), ("2026-11-11", 12.0)]  # +20% 才算
    assert pa.price_signals(closes_20, "300123")["limit_up_streak"] == 1


def test_price_signals_insufficient_data_is_null():
    sig = pa.price_signals([], "600183")
    assert sig["close"] is None and sig["limit_up_streak"] is None
    assert sig["note"]


def test_build_signal_detail_keeps_missing_as_none():
    row = pa.parse_ambush_table(REAL_REPORT)[0]
    detail = pa.build_signal_detail(row, demon=None, price=None)
    assert detail["signals"]["demon"] is None
    assert detail["signals"]["price"] is None
    assert detail["stop_price"] is None  # 「跌破10日线或-5%」不是价位
    assert detail["code"] == "600183"

def test_insert_after_table_puts_block_right_after_rows():
    rows = pa.parse_ambush_table(REAL_REPORT)
    block = pa.build_signal_block(rows, {"600183": {"demon": {"score": 78.1, "grade": "妖", "n_sealed": 12, "max_streak": 4}, "price": None}})
    out = pa.insert_after_table(REAL_REPORT, block)
    lines = out.splitlines()
    idx_block = next(i for i, l in enumerate(lines) if "潜伏表 · 信号明细" in l)
    idx_last_row = next(i for i, l in enumerate(lines) if "001400" in l)  # 潜伏表最后一行
    assert idx_block > idx_last_row
    # 追加块之后正文其余内容不丢
    assert "0.7 今日不碰清单" in out
    # 明细块里以代码开头的行恰好一行（原表行的首列是「事件」，不会以代码开头）
    assert sum(1 for l in lines if l.startswith("| 600183 |")) == 1
    assert any("股性因子" in l for l in lines[idx_block:idx_block + 6])  # 明细块表头存在


def test_insert_after_table_no_table_returns_original():
    text = "今日只有指数点评。"
    assert pa.insert_after_table(text, "BLOCK") == text


def test_signal_block_marks_missing_as_no_data():
    rows = pa.parse_ambush_table(REAL_REPORT)[:1]
    block = pa.build_signal_block(rows, {})
    assert "无数据" in block
    assert "非 AI 生成" in block
