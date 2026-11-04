"""前端 × 后端 **真实 payload 契约**测试: `GET /admin/analytics/usage-trends`(2026-11-04)。

背景: 后端返回 `qps.peak_per_sec`, 前端首版读的是 `qps.peak_qps` —— 双方各自的
单测都绿(前端 mock 了一份不存在的响应), 但线上峰值永远是 `--`。本文件把
**后端实测响应**落成 fixture, 前端 `admin-usage-trends-contract.test.tsx`
直接渲染同一份 fixture, 任何一侧改字段名都会红:

- 后端: `test_fixture与真实响应结构一致` 用真实 HTTP 响应逐层比对 fixture 的字段名/类型;
- 前端: 渲染 fixture 断言 QPS 峰值、每日序列、Top Skills/IP 全部上屏(不是 `--`)。

fixture 不是手写的, 由 `test_重生契约fixture` 从真实端点导出:

    UPDATE_USAGE_TRENDS_FIXTURE=1 \\
      python -m pytest tests/test_usage_trends_contract.py::test_重生契约fixture -q

重导出后需同时跑前端契约用例确认仍绿(fixture 值会随导出日期的日期序列变化)。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.core.client_ip import UNKNOWN_IP
from src.core.timezone import db_now_naive
from src.db.models import SkillApiKey, SkillUsage, User
from src.web.api import skills_gateway as gw
from src.web.database import SessionLocal

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "frontend" / "tests" / "fixtures" / "usage-trends.sample.json"
ENDPOINT = "/api/admin/analytics/usage-trends"

#: 契约里必须存在的 qps 字段(名字即协议, 少一个前端就有一格显示 `--`)。
QPS_KEYS = {"current_10s", "avg_60s", "peak_per_sec", "peak_second", "samples_10s", "samples_60s"}
DAILY_KEYS = {"date", "new_keys", "total_calls"}
SKILL_KEYS = {"skill_name", "display_name", "known", "count"}
IP_KEYS = {"ip", "count"}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@pytest.fixture(scope="module", autouse=True)
def _strict_admin_mode():
    """生产默认是严格模式, 契约必须在同一鉴权口径下取数。"""
    old = os.environ.get("STRICT_ADMIN_TOKEN")
    os.environ["STRICT_ADMIN_TOKEN"] = "1"
    yield
    if old is None:
        os.environ.pop("STRICT_ADMIN_TOKEN", None)
    else:
        os.environ["STRICT_ADMIN_TOKEN"] = old


@pytest.fixture(scope="module")
def env():
    from src.core.auth_tokens import create_admin_token, create_token
    from src.web.app import app

    suffix = uuid.uuid4().hex[:8]
    owner_id = str(uuid.uuid4())
    raw_key = "sk_" + secrets.token_urlsafe(32)

    db = SessionLocal()
    try:
        owner = User(
            id=owner_id, username=f"ctr_{suffix}", password_hash="x",
            role="owner", is_active=True, token_version=1,
        )
        db.add(owner)
        db.commit()
        key = SkillApiKey(
            key_hash=gw._hash_key(raw_key),
            key_prefix=raw_key[:11],
            owner_label="contract-test",
            tier="free",
            status="active",
            daily_limit=100,
            user_id=None,
        )
        db.add(key)
        db.commit()
        key_id = key.id
        tok = {
            "owner_user": create_token(owner).token,
            "owner_admin": create_admin_token(owner).token,
        }
    finally:
        db.close()

    with TestClient(app) as client:
        yield {"client": client, "tok": tok, "key_id": key_id}

    db = SessionLocal()
    try:
        db.query(SkillUsage).filter(SkillUsage.api_key_id == key_id).delete(synchronize_session=False)
        db.query(SkillApiKey).filter(SkillApiKey.id == key_id).delete(synchronize_session=False)
        db.query(User).filter(User.id == owner_id).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def _add_usage(key_id: int, *, skill: str, ip: str, at: datetime) -> None:
    db = SessionLocal()
    try:
        db.add(
            SkillUsage(
                api_key_id=key_id, skill_name=skill, status_code=200, duration_ms=1,
                channel="api", client_ip=ip, created_at=at,
            )
        )
        db.commit()
    finally:
        db.close()


def _seed(env) -> None:
    """造一份能覆盖契约全部分支的数据: 已收录/未收录 skill、合法/脏 IP、同秒突刺。"""
    now = db_now_naive().replace(microsecond=0)
    burst = now - timedelta(seconds=3)
    for _ in range(5):  # 同一秒 5 次 → peak_per_sec >= 5
        _add_usage(env["key_id"], skill="get_stock_quote", ip="203.0.113.7", at=burst)
    _add_usage(env["key_id"], skill="get_stock_quote", ip="203.0.113.7", at=now - timedelta(minutes=5))
    _add_usage(env["key_id"], skill="zz_probe_skill", ip="203.0.113.8", at=now - timedelta(minutes=6))
    _add_usage(env["key_id"], skill="zz_probe_skill", ip="not-an-ip", at=now - timedelta(minutes=7))


def _call(env, *, days: int = 7):
    return env["client"].get(
        ENDPOINT,
        params={"days": days},
        headers={
            "Authorization": f"Bearer {env['tok']['owner_user']}",
            "X-Admin-Token": env["tok"]["owner_admin"],
        },
    )


def _data(resp) -> dict:
    body = resp.json()
    assert body.get("code") == 0, body
    return body["data"]


def _kind(value) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "dict"
    return type(value).__name__


def _assert_same_shape(live, fixture, path: str) -> None:
    """live 的字段名/类型必须与 fixture 一致; live 为 None 视为"这次没数据"放过。"""
    assert _kind(live) == _kind(fixture), f"{path}: 类型 {_kind(live)} != {_kind(fixture)}"
    if isinstance(live, dict):
        assert set(live) == set(fixture), (
            f"{path}: 字段集不一致, 多={sorted(set(live) - set(fixture))} "
            f"少={sorted(set(fixture) - set(live))}"
        )
        for key in live:
            _assert_same_shape(live[key], fixture[key], f"{path}.{key}")
    elif isinstance(live, list) and live and fixture:
        for i, item in enumerate(live[:3]):
            _assert_same_shape(item, fixture[0], f"{path}[{i}]")


def test_重生契约fixture(env):
    """显式重生 `frontend/tests/fixtures/usage-trends.sample.json`(平时跳过)。"""
    if os.getenv("UPDATE_USAGE_TRENDS_FIXTURE") != "1":
        pytest.skip("只在 UPDATE_USAGE_TRENDS_FIXTURE=1 时重生 fixture")

    _seed(env)
    body = _data(_call(env))
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert FIXTURE.is_file()


def test_fixture是后端真实响应且结构一致(env):
    """fixture 必须是真实端点导出的形状 —— 后端改字段名, 这里先红。"""
    assert FIXTURE.is_file(), (
        f"缺少契约 fixture {FIXTURE}; 先跑 UPDATE_USAGE_TRENDS_FIXTURE=1 pytest "
        "tests/test_usage_trends_contract.py::test_重生契约fixture"
    )
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

    # fixture 自己先要符合协议: 峰值字段就是 peak_per_sec(不是 peak_qps)
    assert set(fixture["qps"]) == QPS_KEYS, fixture["qps"]
    assert "peak_qps" not in fixture["qps"], "peak_qps 不是后端字段, 不许回到契约里"
    assert isinstance(fixture["qps"]["peak_per_sec"], int)
    assert fixture["qps"]["peak_per_sec"] >= 5, "fixture 应覆盖同秒突刺"
    assert all(set(row) == DAILY_KEYS for row in fixture["daily_trend"])
    assert all(set(row) == SKILL_KEYS for row in fixture["top_skills"])
    assert all(set(row) == IP_KEYS for row in fixture["top_ips"])
    # fixture 必须同时含已收录与未收录 skill, 前端才测得到 known 分支
    assert {row["known"] for row in fixture["top_skills"]} == {True, False}
    assert any(row["ip"] == UNKNOWN_IP for row in fixture["top_ips"])

    _seed(env)
    live = _data(_call(env))
    assert set(live) == set(fixture), (
        f"响应顶层字段漂移: 多={sorted(set(live) - set(fixture))} "
        f"少={sorted(set(fixture) - set(live))}"
    )
    _assert_same_shape(live, fixture, "data")

    # 与前端渲染直接相关的取值断言(结构一致还不够, 语义也要对)
    assert isinstance(live["qps"]["peak_per_sec"], int) and live["qps"]["peak_per_sec"] >= 5
    assert live["qps"]["current_10s"] == round(live["qps"]["samples_10s"] / 10.0, 2)
    assert live["qps"]["avg_60s"] == round(live["qps"]["samples_60s"] / 60.0, 2)
    assert live["total_calls_in_window"] == sum(row["total_calls"] for row in live["daily_trend"])


def test_daily_trend日期字段与窗口一致(env):
    """日期口径审计: `date` 是 `YYYY-MM-DD` 文本, 连续自然日, 且与 window 首尾和今天对齐。

    前端按后端给的 `date` 原样上屏, 不做本地时区重算 —— 所以这里钉住格式与顺序,
    避免哪天改成 datetime/UTC 字符串后前端显示成乱码或错一天。
    """
    for days in (7, 30):
        body = _data(_call(env, days=days))
        dates = [row["date"] for row in body["daily_trend"]]
        assert all(DATE_RE.match(d) for d in dates), f"日期不是 YYYY-MM-DD: {dates[:3]}"
        parsed = [datetime.strptime(d, "%Y-%m-%d") for d in dates]
        assert len(parsed) == days
        assert all(b - a == timedelta(days=1) for a, b in zip(parsed, parsed[1:])), "日期不连续"
        today = db_now_naive().date()
        assert parsed[-1].date() == today, f"末桶不是今天: {dates[-1]}"
        assert body["window"] == {"start": dates[0], "end": dates[-1]}
        assert all(isinstance(row["new_keys"], int) and isinstance(row["total_calls"], int) for row in body["daily_trend"])


def test_严格模式admin_token可由Cookie携带(env):
    """生产严格模式下, fetchAPI 只能带 Cookie(`credentials:'include'`), 不能读 httpOnly token。

    前端从不发 `X-Admin-Token`; 若严格模式只认 header, 管理台用量页必然 403。
    这里用"用户 Bearer + admin Cookie, 无 X-Admin-Token"复现浏览器真实传输。
    """
    env["client"].cookies.clear()
    # 只有用户身份、没有 admin cookie → 401(不能靠用户 token 顶管理员)
    r = env["client"].get(ENDPOINT, params={"days": 7}, headers={
        "Authorization": f"Bearer {env['tok']['owner_user']}",
    })
    assert r.status_code == 401, f"缺 admin 身份竟然放行: {r.status_code} {r.text[:160]}"

    env["client"].cookies.set("sida_admin_token", env["tok"]["owner_admin"])
    r = env["client"].get(ENDPOINT, params={"days": 7}, headers={
        "Authorization": f"Bearer {env['tok']['owner_user']}",
    })
    assert r.status_code == 200, f"Cookie 携带 admin token 被拒(fetchAPI 无法用 header): {r.status_code} {r.text[:200]}"
    assert _data(r)["days"] == 7
    env["client"].cookies.clear()
