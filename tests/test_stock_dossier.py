import pytest
from src.agents.chat.stock_dossier_aggregator import format_stock_dossier_text


def test_format_stock_dossier_text():
    mock_dossier = {
        "symbol": "600111",
        "market": "CN",
        "quote": "北方稀土 现价: 35.23 涨跌幅: -0.25%",
        "technical": "日K处于均线震荡收敛区间，量比 0.56",
        "main_intent": "[数据源: 腾讯逐笔·主力意图口径] 洗盘吸筹↑，大单净额 -552.05万",
        "chips": "[数据源: 自算CYQ·120日底仓沉淀] 底仓成本(COST50)：40.16 元 ｜ 筹码峰：39.06 元",
        "decision_pioneer": "[数智决策] GS区间: S区(偏空) | 机构活跃度: 2.14(强于生命线)",
        "broker_reports": [
            {
                "publish_date": "2026-10-07",
                "title": "北方稀土深度研究报告：轻稀土龙头",
                "org_name": "智研科信",
                "rating": "买入",
                "author": "张三",
            }
        ],
    }

    text = format_stock_dossier_text(mock_dossier)
    assert "SIDA-Pro 标的客观全景档案 · 600111" in text
    assert "第一维：实时行情与盘口态势" in text
    assert "第三维：主力意图与真金证据" in text
    assert "第四维：底仓筹码沉淀与获利带" in text
    assert "第五维：数智决策三指标" in text
    assert "第六维：券商机构研报" in text
    assert "北方稀土深度研究报告" in text
    assert "COST50" in text
