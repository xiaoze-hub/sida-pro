"""AI 排序不能将一个候选的理由贴到另一标的。"""
import asyncio

from src.web.api import dashboard


class FakeAI:
    async def chat(self, *args, **kwargs):
        return '0|90|蓝色光标 MACD 金叉\n1|80|贵州茅台 MACD 金叉'


def test_cross_symbol_ai_reason_is_not_published(monkeypatch):
    monkeypatch.setattr(dashboard, '_get_ai_client', lambda *args: FakeAI())
    candidates = [
        dashboard.CurateCandidate(type='opportunity', name='贵州茅台', symbol='600519', signal='原始贵州茅台信号'),
        dashboard.CurateCandidate(type='opportunity', name='蓝色光标', symbol='300058', signal='原始蓝色光标信号'),
    ]
    result = asyncio.run(dashboard.curate_today(dashboard.CurateRequest(candidates=candidates), None))
    assert result['items'][0]['why'] == '原始贵州茅台信号'
    assert result['items'][1]['why'] == '原始蓝色光标信号'
