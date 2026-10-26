# -*- coding: utf-8 -*-
"""AI 看图形态影子层(src.core.ai_vision_pattern)测试(2026-10-14 主线一并行)。

覆盖: 视觉输出解析(容错) / 与规则对照(四组分桶) / 影子行构造(主形态=最高置信) /
本地渲染真出 PNG / 单票识别降级显式(无模型·调用失败·输出不可解析·K线不足) /
影子落库幂等 + 只读读取 / 编排(provider 注入) / 迁移门禁(_m195 + 版本连续)。

**全部合成 K 线 + mock, 禁真网络(禁真视觉模型调用)。**
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core import ai_vision_pattern as V  # noqa: E402


def B(o, h, l, c, v=100.0, d=None):
    bar = {"open": o, "high": h, "low": l, "close": c, "volume": v}
    if d:
        bar["date"] = d
    return bar


def _bars(n=12):
    # 温和上升序列(数值无关紧要, 只求可渲染/可识别)
    out = []
    p = 10.0
    for i in range(n):
        o = p
        c = p + 0.05
        out.append(B(o, c + 0.03, o - 0.03, c, 100 + i))
        p = c
    return out


# ─────────────── parse_vision_result ───────────────

def test_parse_valid_json():
    txt = '{"patterns":[{"name":"金针探底","confidence":0.8,"basis":"长下影"}],"summary":"x"}'
    r = V.parse_vision_result(txt)
    assert r is not None
    assert r["patterns"][0]["name"] == "金针探底"
    assert r["patterns"][0]["confidence"] == pytest.approx(0.8)
    assert r["summary"] == "x"


def test_parse_code_fenced_and_prose_wrapped():
    txt = "好的, 结果为:\n```json\n{\"patterns\":[{\"name\":\"红三兵\",\"confidence\":1.5,\"basis\":\"三连阳\"}]}\n```"
    r = V.parse_vision_result(txt)
    assert r is not None
    assert r["patterns"][0]["name"] == "红三兵"
    # 置信度越界 → 夹到 [0,1]
    assert r["patterns"][0]["confidence"] == pytest.approx(1.0)


def test_parse_single_object_form():
    r = V.parse_vision_result('{"name":"头肩顶","confidence":0.6,"basis":"左肩右肩"}')
    assert r is not None and r["patterns"][0]["name"] == "头肩顶"


def test_parse_no_patterns_and_garbage():
    assert V.parse_vision_result('{"patterns":[]}')["patterns"] == []
    assert V.parse_vision_result("完全不是 JSON") is None
    assert V.parse_vision_result("") is None
    assert V.parse_vision_result(None) is None
    # 缺 name 的项被剔除
    assert V.parse_vision_result('{"patterns":[{"confidence":0.5}]}')["patterns"] == []


# ─────────────── compare_with_rules ───────────────

def test_compare_four_groups():
    assert V.compare_with_rules(["红三兵"], ["红三兵", "金针探底"]) == V.COMPARE_AGREE
    assert V.compare_with_rules(["头肩顶"], ["红三兵"]) == V.COMPARE_CONFLICT
    assert V.compare_with_rules(["旗形"], []) == V.COMPARE_AI_ONLY
    assert V.compare_with_rules([], ["金针探底"]) == V.COMPARE_RULES_ONLY
    assert V.compare_with_rules([], []) == V.COMPARE_NONE
    # None/空串容错
    assert V.compare_with_rules(None, None) == V.COMPARE_NONE


# ─────────────── build_rows ───────────────

def test_build_rows_ok_agree_primary_highest_conf():
    rows = V.build_rows([
        {"symbol": "600519", "name": "贵州茅台", "status": "ok",
         "ai_patterns": [
             {"name": "红三兵", "confidence": 0.4, "basis": "a"},
             {"name": "金针探底", "confidence": 0.9, "basis": "b"},
         ],
         "rule_patterns": ["金针探底"], "model_name": "agnes-2.5-flash", "window": 60},
    ])
    assert len(rows) == 1
    r = rows[0]
    assert r["ai_pattern"] == "金针探底" and r["ai_confidence"] == pytest.approx(0.9)
    assert r["compare_status"] == V.COMPARE_AGREE
    assert r["status"] == "ok"
    assert json.loads(r["ai_all"])[0]["name"] == "红三兵"
    assert json.loads(r["rule_patterns"]) == ["金针探底"]


def test_build_rows_degraded_and_no_data_and_empty_symbol():
    rows = V.build_rows([
        {"symbol": "000001", "status": "degraded", "ai_patterns": [], "rule_patterns": ["红三兵"]},
        {"symbol": "000002", "status": "no_data"},
        {"symbol": "", "status": "ok"},  # 无码 → 剔除
    ])
    assert len(rows) == 2
    by = {r["symbol"]: r for r in rows}
    assert by["000001"]["compare_status"] == V.COMPARE_DEGRADED
    assert by["000001"]["ai_pattern"] is None
    assert by["000002"]["compare_status"] is None
    assert by["000002"]["status"] == "no_data"


# ─────────────── 渲染 ───────────────

def test_render_kline_png_writes_real_png(tmp_path):
    out = str(tmp_path / "k.png")
    got = V.render_kline_png(_bars(20), out)
    assert got == out
    data = Path(out).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"  # PNG 魔数
    assert len(data) > 200


def test_render_kline_png_insufficient_bars(tmp_path):
    assert V.render_kline_png([B(1, 2, 0.5, 1.5)], str(tmp_path / "x.png")) is None
    assert V.render_kline_png([], str(tmp_path / "y.png")) is None


# ─────────────── recognize_from_bars(降级显式) ───────────────

def test_recognize_no_data_when_too_few_bars():
    res = V.recognize_from_bars([B(1, 2, 0.5, 1.5)], vision_provider=lambda *a: "{}")
    assert res["status"] == "no_data" and res["ai_patterns"] == []


def test_recognize_degraded_when_no_vision_model():
    res = V.recognize_from_bars(_bars(10), vision_provider=None)
    assert res["status"] == "degraded" and res["ai_patterns"] == []
    assert "无视觉模型" in res["reason"]


def test_recognize_degraded_when_vision_raises():
    def boom(*a):
        raise RuntimeError("502")

    res = V.recognize_from_bars(_bars(10), vision_provider=boom)
    assert res["status"] == "degraded" and res["ai_patterns"] == []
    assert "视觉调用失败" in res["reason"]


def test_recognize_degraded_when_unparseable():
    res = V.recognize_from_bars(_bars(10), vision_provider=lambda *a: "我看不清")
    assert res["status"] == "degraded" and res["ai_patterns"] == []
    assert "不可解析" in res["reason"]


def test_recognize_ok_with_injected_vision(tmp_path):
    captured = {}

    def fake_render(bars, out_path):
        Path(out_path).write_bytes(b"\x89PNG\r\n\x1a\nfake")
        return out_path

    def fake_vision(image_path, system, user):
        captured["img"] = image_path
        captured["user"] = user
        return '{"patterns":[{"name":"旗形","confidence":0.55,"basis":"窄幅整理"}],"summary":"s"}'

    res = V.recognize_from_bars(
        _bars(10), vision_provider=fake_vision, render_provider=fake_render,
        model_name="agnes-2.5-flash",
    )
    assert res["status"] == "ok"
    assert res["ai_patterns"][0]["name"] == "旗形"
    assert res["model_name"] == "agnes-2.5-flash"
    assert captured["img"] and captured["user"]


# ─────────────── 落库幂等 + 只读读取 ───────────────

_TD = "20261014"
_SYMS = ("600000", "600001")


@pytest.fixture(scope="module", autouse=True)
def _ensure_table():
    """确保影子表存在(init_db 已跑迁移; 极端情况下显式补跑 _m195, 幂等)。"""
    from src.web.database import engine
    from src.web.migrations import _m195_ai_pattern_shadow

    with engine.begin() as conn:
        _m195_ai_pattern_shadow(conn)


@pytest.fixture()
def _cleanup():
    yield
    from sqlalchemy import text

    from src.web.database import SessionLocal

    db = SessionLocal()
    try:
        db.execute(text(f"DELETE FROM {V._TABLE} WHERE trade_date = :d"), {"d": _TD})
        db.commit()
    finally:
        db.close()


def _rows():
    return V.build_rows([
        {"symbol": _SYMS[0], "name": "甲", "status": "ok",
         "ai_patterns": [{"name": "红三兵", "confidence": 0.7, "basis": "三连阳"}],
         "rule_patterns": ["红三兵"], "model_name": "m", "window": 60},
        {"symbol": _SYMS[1], "name": "乙", "status": "ok",
         "ai_patterns": [{"name": "头肩顶", "confidence": 0.5, "basis": "双肩"}],
         "rule_patterns": ["金针探底"], "model_name": "m", "window": 60},
    ])


def test_mark_rows_idempotent_and_read(_cleanup):
    s1 = V.mark_rows(_TD, _rows(), write=True)
    assert s1["written"] == 2 and s1["ok"] == 2
    assert s1["by_compare"] == {V.COMPARE_AGREE: 1, V.COMPARE_CONFLICT: 1}
    # 幂等: 再写不翻倍
    V.mark_rows(_TD, _rows(), write=True)
    got = V.patterns_for_date(_TD)
    assert got["available"] is True and len(got["rows"]) == 2
    assert got["counts"][V.COMPARE_AGREE] == 1
    assert V.latest_date() is not None


def test_patterns_for_date_missing(_cleanup):
    got = V.patterns_for_date("19990101")
    assert got["available"] is False and got["rows"] == [] and got["note"]


# ─────────────── 编排 collect_and_mark ───────────────

def _universe(*syms):
    return lambda: list(syms)


def test_collect_and_mark_empty_pool():
    out = V.collect_and_mark("20261014", universe_provider=_universe(), write=False)
    assert out["ok"] is False and out["rows"] == 0


def test_collect_and_mark_all_degraded_ok_false(_cleanup):
    out = V.collect_and_mark(
        _TD,
        universe_provider=_universe(_SYMS[0], _SYMS[1]),
        kline_provider=lambda s: _bars(30),
        rule_provider=lambda bars: ["红三兵"],
        names_provider=lambda syms: {_SYMS[0]: "甲"},
        vision_provider=None, model_provider=lambda: None,
        write=True,
    )
    assert out["ok"] is False
    assert out["degraded"] == 2 and out["by_compare"][V.COMPARE_DEGRADED] == 2
    # 规则侧仍落库(便于"仅规则"分析)
    got = V.patterns_for_date(_TD)
    assert got["rows"][0]["rule_primary"] == "红三兵"


def test_collect_and_mark_ok_with_injected_vision(_cleanup):
    def fake_render(bars, out_path):
        Path(out_path).write_bytes(b"png")
        return out_path

    def fake_vision(image_path, system, user):
        return '{"patterns":[{"name":"红三兵","confidence":0.8,"basis":"x"}]}'

    seen_rules = {}

    def rule_provider(bars):
        seen_rules["called"] = True
        return ["红三兵"]

    out = V.collect_and_mark(
        _TD,
        universe_provider=_universe(_SYMS[0]),
        kline_provider=lambda s: _bars(30),
        rule_provider=rule_provider,
        names_provider=lambda syms: {_SYMS[0]: "甲"},
        render_provider=fake_render,
        vision_provider=fake_vision,
        write=True,
    )
    assert out["ok"] is True and out["ok"] == 1
    assert out["by_compare"] == {V.COMPARE_AGREE: 1}
    assert seen_rules.get("called") is True
    got = V.patterns_for_date(_TD)
    row = got["rows"][0]
    assert row["ai_pattern"] == "红三兵" and row["compare_status"] == V.COMPARE_AGREE
    assert row["rule_patterns"] == ["红三兵"]  # 读取端已解析 JSON


# ─────────────── 迁移门禁 ───────────────

def test_migration_195_registered_and_continuous():
    from src.web.migrations import MIGRATIONS

    versions = [m.version for m in MIGRATIONS]
    assert 195 in versions
    # 从 101 起连续无缺口(不写死上界 —— 新增迁移不应让本钉子变红)
    assert versions == list(range(101, max(versions) + 1))
    m195 = next(m for m in MIGRATIONS if m.version == 195)
    assert m195.name == "ai_pattern_shadow"
    assert m195.runner.__name__.startswith("_m195_")


def test_migration_195_creates_table():
    from sqlalchemy import text

    from src.web.database import engine

    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='ai_pattern_shadow'")
        ).fetchall()
    assert rows, "ai_pattern_shadow 表应由 _m195 创建"


# ─────────────── 只读 API ───────────────

def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.web.api import ai_pattern as api

    app = FastAPI()
    app.include_router(api.router, prefix="/api/ai-pattern")
    return TestClient(app)


def test_api_usage():
    body = _client().get("/api/ai-pattern").json()
    assert body["endpoint"] == "/api/ai-pattern"
    assert "影" in body["note"]


def test_api_shadow_no_data(monkeypatch):
    monkeypatch.setattr(
        V, "patterns_for_date",
        lambda d=None: {"available": False, "trade_date": None, "rows": [], "counts": {},
                        "note": "无数据"},
    )
    body = _client().get("/api/ai-pattern/shadow").json()
    assert body["available"] is False and body["note"]


# ─────────────── scene='vision' 默认绑定 agnes-3.0-flash(2026-10-15) ───────────────


def _mem_db():
    """独立内存 SQLite 会话(建全表, 含 ai_services/ai_models/ai_scene_bindings)。

    StaticPool: TestClient 在工作线程里跑端点, 需共享同一条内存连接, 否则新线程拿到
    空库(no such table)。
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from src.db import models as _m  # noqa: F401  注册 ORM
    from src.db.session import Base

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _seed_service(db, name="Agnes", base_url="https://api.agnes-ai.cn/v1", key="sk-vision"):
    from src.db.models import AIService

    svc = AIService(name=name, base_url=base_url, api_key=key)
    db.add(svc)
    db.commit()
    db.refresh(svc)
    return svc


def test_scene_default_model_name_constant():
    """vision 场景默认模型名 = agnes-3.0-flash(ai_client 与本题分析层同源)。"""
    from src.core.ai_client import SCENE_DEFAULT_MODEL_NAMES

    assert SCENE_DEFAULT_MODEL_NAMES["vision"] == "agnes-3.0-flash"
    assert V.DEFAULT_VISION_MODEL == "agnes-3.0-flash"


def test_resolve_global_model_prefers_scene_default_name():
    """未显式绑定时, vision 场景按名回落 agnes-3.0-flash(优先于全局默认模型)。"""
    from src.core.ai_client import _resolve_global_model
    from src.db.models import AIModel

    db = _mem_db()
    svc = _seed_service(db)
    db.add(AIModel(name="deepseek", service_id=svc.id, model="deepseek-chat", is_default=True))
    db.add(AIModel(name="agnes-3.0-flash", service_id=svc.id, model="agnes-3.0-flash"))
    db.commit()

    assert _resolve_global_model(db, "vision").model == "agnes-3.0-flash"
    # 其他场景不受影响: 仍取全局默认模型
    assert _resolve_global_model(db, "chat").model == "deepseek-chat"


def test_resolve_global_model_falls_back_when_scene_default_absent():
    """模型池没有 agnes-3.0-flash → vision 仍回落全局默认模型(不报错)。"""
    from src.core.ai_client import _resolve_global_model
    from src.db.models import AIModel

    db = _mem_db()
    svc = _seed_service(db, name="X", base_url="http://x")
    db.add(AIModel(name="deepseek", service_id=svc.id, model="deepseek-chat", is_default=True))
    db.commit()

    assert _resolve_global_model(db, "vision").model == "deepseek-chat"


def test_resolve_vision_model_default_binds_agens():
    """未显式绑定 → 解析到 agnes-3.0-flash(存量 capabilities 未标注也信任默认名)。"""
    from src.db.models import AIModel

    db = _mem_db()
    svc = _seed_service(db)
    db.add(AIModel(name="agnes-3.0-flash", service_id=svc.id, model="agnes-3.0-flash", capabilities=""))
    db.commit()

    assert V.resolve_vision_model(db) == (
        "https://api.agnes-ai.cn/v1", "sk-vision", "agnes-3.0-flash",
    )


def test_resolve_vision_model_non_vision_fallback_degrades():
    """无默认视觉模型、仅非视觉模型 → None(显式降级语义保持)。"""
    from src.db.models import AIModel

    db = _mem_db()
    svc = _seed_service(db, name="DeepSeek", base_url="http://ds")
    db.add(AIModel(name="deepseek", service_id=svc.id, model="deepseek-chat",
                   is_default=True, capabilities="chat"))
    db.commit()

    assert V.resolve_vision_model(db) is None


def test_resolve_vision_model_explicit_binding_wins_over_default():
    """显式绑定(AISceneBinding) 优先于按名默认; 且必须是 vision 能力模型。"""
    from src.db.models import AIModel, AISceneBinding

    db = _mem_db()
    svc = _seed_service(db, name="S", base_url="http://s")
    db.add(AIModel(name="agnes-3.0-flash", service_id=svc.id, model="agnes-3.0-flash"))
    vl = AIModel(name="qwen-vl", service_id=svc.id, model="qwen-vl", capabilities="chat,vision")
    db.add(vl)
    db.commit()
    db.refresh(vl)
    db.add(AISceneBinding(scene="vision", model_id=vl.id))
    db.commit()

    assert V.resolve_vision_model(db) == ("http://s", "sk-vision", "qwen-vl")


# ─────────────── 影子任务标的圈定(自选池 ∪ 前一日有标记票) ───────────────


def test_shadow_universe_watchlist_plus_prev_marks_bare_dedup():
    syms = V.shadow_universe(
        "20261015",
        watchlist_provider=lambda: ["600000.SH", "600001.SZ"],
        marks_provider=lambda: ["600000", "300750.SZ", "300750"],
    )
    # 自选在前, 前一日标记票补充, 去后缀/去重
    assert syms == ["600000", "600001", "300750"]


def test_shadow_universe_tolerates_provider_failure():
    def boom():
        raise RuntimeError("db down")

    syms = V.shadow_universe(
        "20261015", watchlist_provider=lambda: ["600000.SH"], marks_provider=boom,
    )
    assert syms == ["600000"]


def test_collect_and_mark_default_universe_is_scoped(monkeypatch):
    """不注入 universe_provider → 默认走 shadow_universe(成本可控圈定)。"""
    monkeypatch.setattr(V, "shadow_universe", lambda td, **kw: ["600000"])
    seen: list[str] = []

    def kline_provider(s):
        seen.append(s)
        return _bars(30)

    out = V.collect_and_mark(
        _TD,
        kline_provider=kline_provider,
        rule_provider=lambda bars: [],
        names_provider=lambda syms: {},
        render_provider=lambda bars, out_path: (Path(out_path).write_bytes(b"png"), out_path)[1],
        vision_provider=lambda *a: '{"patterns":[]}',
        write=False,
    )
    assert seen == ["600000"]           # 只处理圈定池
    assert out["ok"] is True            # 视觉可用但判无形态 → ok(空形态)


def test_collect_and_mark_empty_scoped_universe_ok_false(monkeypatch):
    monkeypatch.setattr(V, "shadow_universe", lambda td, **kw: [])
    out = V.collect_and_mark(_TD, write=False)
    assert out["ok"] is False and out["rows"] == 0


# ─────────────── 场景绑定 API 暴露默认/当前绑定状态 ───────────────


def _providers_client(db):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.web.api import providers
    from src.web.database import get_db

    app = FastAPI()
    app.include_router(providers.router, prefix="/api/providers")
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def test_scene_bindings_expose_default_and_effective_model_name():
    from src.db.models import AIModel

    db = _mem_db()
    svc = _seed_service(db)
    db.add(AIModel(name="agnes-3.0-flash", service_id=svc.id, model="agnes-3.0-flash"))
    db.commit()
    client = _providers_client(db)

    body = client.get("/api/providers/scene-bindings").json()
    vis = next(b for b in body if b["scene"] == "vision")
    assert vis["default_model_name"] == "agnes-3.0-flash"
    assert vis["effective_model_name"] == "agnes-3.0-flash"
    assert vis["is_bound"] is False

    # 改成别的模型 → 显示当前绑定状态
    vl = AIModel(name="qwen-vl", service_id=svc.id, model="qwen-vl", capabilities="chat,vision")
    db.add(vl)
    db.commit()
    db.refresh(vl)
    r = client.put("/api/providers/scene-bindings/vision", json={"model_id": vl.id}).json()
    assert r["is_bound"] is True and r["model_name"] == "qwen-vl"
    assert r["default_model_name"] == "agnes-3.0-flash"

    # 解绑 → 回落默认 agnes-3.0-flash
    r2 = client.put("/api/providers/scene-bindings/vision", json={"model_id": None}).json()
    assert r2["is_bound"] is False and r2["effective_model_name"] == "agnes-3.0-flash"
