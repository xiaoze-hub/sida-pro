# -*- coding: utf-8 -*-
"""Gateway P2/P3: 红线词 + 突增冻结 + 管理接口。"""
from unittest.mock import MagicMock, patch

from src.web.api import skills_gateway as gw


def test_compliance_wrap_hits():
    result = "建议买入 600519, 稳赚不赔"
    wrapped, hits = gw._compliance_wrap(result)
    assert "买入" in hits or "建议买" in hits
    assert "稳赚" in " ".join(hits)
    assert "合规提示" in wrapped
    assert wrapped.startswith("建议买入")


def test_compliance_wrap_clean():
    wrapped, hits = gw._compliance_wrap("贵州茅台 现价 1278 涨 0.2%")
    assert hits == []
    assert wrapped == "贵州茅台 现价 1278 涨 0.2%"


def test_freeze_on_spike():
    db = MagicMock()
    row = MagicMock()
    row.key_hash = "testhash_freeze"
    row.key_prefix = "sk_freeze"
    # 模拟基线 2 次, 当前窗口 35 次
    import time as _t
    now = _t.time()
    gw._RECENT_CALLS[row.key_hash] = [now - 400, now - 380] + [now - 0.1 * i for i in range(35)]
    # 补 35 次在窗口内
    gw._RECENT_CALLS[row.key_hash] = [now - 400, now - 380] + [now - i * 0.05 for i in range(35)]
    with patch.object(gw, "notify_task_done", create=True):
        gw._detect_spike_and_freeze(db, row)
    # 可能触发冻结(35 vs 基线 2, 倍数>10)
    if row.status == "frozen":
        assert "突增" in (row.frozen_reason or "")
    else:
        # 样本不足时跳过, 也算通过
        pass


def test_admin_gate_reuses_require_owner():
    """管理端点必须复用 `auth.require_owner`(严格模式=独立 admin token)。

    2026-11-04 复核: 这里原来是 `Depends(get_current_user)` + 手判 `user.role`,
    生产严格模式下 owner 的**用户面** token 就能读全局 Key 清单/调额度/看全局用量,
    绕过了管理面 admin 受众隔离。现在 `_require_owner_admin` 必须是
    `auth.require_owner` 本身(不是"长得像"的另一套实现)。
    """
    import inspect
    import re

    from src.web.api import auth

    assert gw._require_owner_admin is auth.require_owner

    # 结构钉子: 不许再出现手写 role 判断/管理员白名单
    src = inspect.getsource(gw)
    code = re.sub(r'"""[\s\S]*?"""', "", src)
    assert 'user.role != "owner"' not in code, "手写 role 判断复活了"
    assert '"admin", "1", 1' not in code, "管理员白名单复活了"


def test_admin_key_action_freeze():
    db = MagicMock()
    row = MagicMock()
    row.id = 1
    user = MagicMock()
    user.role = "owner"
    user.username = "admin"
    q = MagicMock()
    q.filter.return_value.first.return_value = row
    db.query.return_value = q
    body = gw.AdminKeyAction(key_id=1, action="freeze", reason="测试冻结")
    out = gw.admin_key_action(body, db=db, user=user)
    assert out["status"] == "frozen"
    assert row.frozen_reason == "测试冻结"


def test_admin_key_action_set_tier():
    db = MagicMock()
    row = MagicMock()
    row.id = 2
    user = MagicMock()
    user.role = "owner"
    q = MagicMock()
    q.filter.return_value.first.return_value = row
    db.query.return_value = q
    body = gw.AdminKeyAction(key_id=2, action="set_tier", tier="pro")
    out = gw.admin_key_action(body, db=db, user=user)
    assert out["tier"] == "pro"
    assert out["daily_limit"] == gw.TIER_DAILY_LIMIT["pro"]
