"""事件驱动预期差引擎单元测试。

纯函数(parse_catalyst_reply / build_catalyst_prompt) + LLM 层
(analyze_event_catalyst)分离; mock AIClient.chat + EventsVendor, 不触网。
"""

import json

from src.core import event_catalyst_engine as ec

VALID_LLM_JSON = json.dumps(
    {
        "catalyst": "硅料停产涨价",
        "direction": "利好",
        "confidence": "高",
        "beneficiary_pool": ["通威股份", "大全能源", "合盛硅业"],
        "expectation_gap": {"level": "高", "note": "利好尚未充分反映在股价"},
        "reason": "上游停产导致供给收缩，硅料涨价预期强化，利好硅料龙头",
    },
    ensure_ascii=False,
)


# ---------- parse_catalyst_reply ----------

def test_parse_valid_json():
    result = ec.parse_catalyst_reply("600519", ["某事件"], VALID_LLM_JSON)
    assert result is not None
    assert result["catalyst"] == "硅料停产涨价"
    assert result["direction"] == "利好"
    assert result["confidence"] == "高"
    assert result["beneficiary_pool"] == ["通威股份", "大全能源", "合盛硅业"]
    assert result["expectation_gap"]["level"] == "高"
    assert result["expectation_gap"]["note"]
    assert result["reason"]


def test_parse_valid_json_with_fence():
    reply = "```json\n" + VALID_LLM_JSON + "\n```"
    result = ec.parse_catalyst_reply("600519", ["某事件"], reply)
    assert result is not None
    assert result["catalyst"] == "硅料停产涨价"


def test_parse_invalid_json():
    assert ec.parse_catalyst_reply("600519", ["某事件"], "这不是JSON") is None
    assert ec.parse_catalyst_reply("600519", ["某事件"], None) is None
    assert ec.parse_catalyst_reply("600519", ["某事件"], "") is None
    assert ec.parse_catalyst_reply("600519", ["某事件"], "[1, 2, 3]") is None


def test_parse_empty_fields_returns_none():
    # 空 catalyst
    bad = json.dumps(
        {
            "catalyst": "",
            "direction": "利好",
            "confidence": "高",
            "beneficiary_pool": ["A"],
            "expectation_gap": {"level": "高", "note": "x"},
            "reason": "r",
        }
    )
    assert ec.parse_catalyst_reply("600519", ["某事件"], bad) is None
    # 非法 direction
    bad2 = json.dumps(
        {
            "catalyst": "x",
            "direction": "暴涨",
            "confidence": "高",
            "beneficiary_pool": ["A"],
            "expectation_gap": {"level": "高", "note": "x"},
            "reason": "r",
        }
    )
    assert ec.parse_catalyst_reply("600519", ["某事件"], bad2) is None
    # beneficiary_pool 非 list
    bad3 = json.dumps(
        {
            "catalyst": "x",
            "direction": "利好",
            "confidence": "高",
            "beneficiary_pool": "A",
            "expectation_gap": {"level": "高", "note": "x"},
            "reason": "r",
        }
    )
    assert ec.parse_catalyst_reply("600519", ["某事件"], bad3) is None
    # expectation_gap 非法 level
    bad4 = json.dumps(
        {
            "catalyst": "x",
            "direction": "利好",
            "confidence": "高",
            "beneficiary_pool": ["A"],
            "expectation_gap": {"level": "巨大", "note": "x"},
            "reason": "r",
        }
    )
    assert ec.parse_catalyst_reply("600519", ["某事件"], bad4) is None


def test_parse_beneficiary_pool_excludes_self_and_caps():
    payload = json.dumps(
        {
            "catalyst": "硅料涨价",
            "direction": "利好",
            "confidence": "中",
            "beneficiary_pool": ["600519", "sh600519", "通威股份", "大全能源", "合盛硅业", "特变电工", "协鑫科技", "双良节能"],
            "expectation_gap": {"level": "中", "note": "已部分反应"},
            "reason": "涨价利好硅料",
        },
        ensure_ascii=False,
    )
    result = ec.parse_catalyst_reply("600519", ["某事件"], payload)
    assert result is not None
    assert "600519" not in result["beneficiary_pool"]
    assert "sh600519" not in result["beneficiary_pool"]
    assert len(result["beneficiary_pool"]) <= 5


# ---------- build_catalyst_prompt ----------

def test_build_prompt_contains_titles_and_gap_instruction():
    events = ["硅料厂商集体停产检修", "多晶硅价格连续上调"]
    system_prompt, user_content = ec.build_catalyst_prompt("600519", events)
    assert "600519" in user_content
    for e in events:
        assert e in user_content
    assert "因果链" in system_prompt
    assert "JSON" in system_prompt
    assert "编造" in system_prompt
    assert "预期差" in system_prompt
    assert "追高" in system_prompt
    assert "低吸" in system_prompt


# ---------- analyze_event_catalyst ----------

def test_analyze_empty_events_returns_none_without_llm(monkeypatch):
    """空事件 → 直接 None, 绝不调用 LLM。"""
    monkeypatch.setattr(ec, "_fetch_today_events", lambda symbol: [])

    from src.core.ai_client import AIClient

    calls = []

    async def fake_chat(self, system_prompt, user_content, images=None, temperature=None):
        calls.append(1)
        return VALID_LLM_JSON

    monkeypatch.setattr(AIClient, "chat", fake_chat)
    assert ec.analyze_event_catalyst("600519") is None
    assert calls == []


def test_analyze_llm_exception_returns_none(monkeypatch):
    """LLM 抛异常 → 静默降级 None。"""
    monkeypatch.setattr(ec, "_fetch_today_events", lambda symbol: ["硅料厂商集体停产检修"])

    from src.core.ai_client import AIClient

    async def fake_chat(self, system_prompt, user_content, images=None, temperature=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(AIClient, "chat", fake_chat)
    assert ec.analyze_event_catalyst("600519") is None


def test_analyze_llm_invalid_json_returns_none(monkeypatch):
    """LLM 返回非法 JSON → 解析失败降级 None。"""
    monkeypatch.setattr(ec, "_fetch_today_events", lambda symbol: ["硅料厂商集体停产检修"])

    from src.core.ai_client import AIClient

    async def fake_chat(self, system_prompt, user_content, images=None, temperature=None):
        return "这不是JSON"

    monkeypatch.setattr(AIClient, "chat", fake_chat)
    assert ec.analyze_event_catalyst("600519") is None


def test_analyze_with_mocked_vendor_and_chat(monkeypatch):
    """端到端: mock _fetch_today_events + _build_catalyst_client → 返回结构化信号。

    注意: 不 mock AIClient.chat(它依赖 _build_catalyst_client 真实构造 AIClient,
    CI 无 AI_API_KEY env 时 AsyncOpenAI(api_key="") 构造抛异常 → 被 analyze 的
    except 吞掉返回 None)。直接 mock _build_catalyst_client 返回假 client,
    让本测试聚焦「LLM 调用 + 解析」核心逻辑, 不依赖时区/环境变量。
    """
    monkeypatch.setattr(ec, "_fetch_today_events", lambda symbol: ["硅料厂商集体停产检修"])

    class _FakeClient:
        async def chat(self, system_prompt, user_content, images=None, temperature=None):
            assert "硅料厂商集体停产检修" in user_content
            return VALID_LLM_JSON

    monkeypatch.setattr(ec, "_build_catalyst_client", lambda db=None: _FakeClient())
    result = ec.analyze_event_catalyst("600519")
    assert result is not None
    assert result["catalyst"] == "硅料停产涨价"
    assert result["direction"] == "利好"
    assert len(result["beneficiary_pool"]) == 3
