import asyncio
import pytest
from src.harness import pipeline_market as pm

@pytest.mark.parametrize('text', [
    '暂无实时资讯数据（可能非交易时段或数据源未就绪）',
    '[市场排行] 取不到数据：上游不可用',
    '情绪周期: 涨停池数据获取失败, 暂无法判别短线情绪周期。',
])
def test_actual_failure_messages_are_unavailable(text):
    assert pm.tool_status(text) == 'unavailable'


def test_timeout_and_failure_visible_in_gap_note():
    run = pm.MarketRun(failed=1, errors=['get_sentiment_cycle:timeout'])
    assert 'get_sentiment_cycle' in run.missing_note()
