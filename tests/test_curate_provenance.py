"""AI 排序不能将一个候选的理由贴到另一标的。

handler 自 2026-11-08 起要求 user(模型权限/BYOK 归属), 直调时需显式传入。
"""
import asyncio
from typing import cast

from src.web.api import dashboard
from src.web.models import User


class _User:
    id = "u-curate-provenance"
    username = "curate_provenance"
    role = "member"
    permissions = {"model_access": {"mode": "inherit", "model_ids": []}}


class FakeAI:
    async def chat(self, *args, **kwargs):
        return '0|90|蓝色光标 MACD 金叉\n1|80|贵州茅台 MACD 金叉'


def test_cross_symbol_ai_reason_is_not_published(monkeypatch):
    monkeypatch.setattr(dashboard, '_get_ai_client', lambda *args, **kwargs: FakeAI())
    candidates = [
        dashboard.CurateCandidate(type='opportunity', name='贵州茅台', symbol='600519', signal='原始贵州茅台信号'),
        dashboard.CurateCandidate(type='opportunity', name='蓝色光标', symbol='300058', signal='原始蓝色光标信号'),
    ]
    result = asyncio.run(
        dashboard.curate_today(
            dashboard.CurateRequest(candidates=candidates), None, user=cast(User, _User())
        )
    )
    assert result['items'][0]['why'] == '原始贵州茅台信号'
    assert result['items'][1]['why'] == '原始蓝色光标信号'
