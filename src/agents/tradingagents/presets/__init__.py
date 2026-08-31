"""Swarm Preset 路由层 — 8 个 A 股可用的智能体团队预设。

✨ 借鉴 HKUDS/Vibe-Trading 的 30 个 swarm preset 概念 (30k stars, MIT)
  Vibe-Trading 30 个 preset 中过滤非 A 股相关(衍生品/加密/外汇/可转债等),
  只保留 8 个 A 股直接可用的。

每个 preset 包含:
  - agents[]: 多 agent 角色 + system_prompt(A 股术语适配)
  - debate_rounds: 辩论轮数(0=只分析, 1=1 轮辩论, 2=2 轮辩论)
  - selected_analysts: TradingAgents 4 类分析师选择
    (market / social / news / fundamentals)

调用:
  list_presets()        -> List[PresetInfo]
  get_preset(name)      -> PresetConfig | None
  run_preset(name, target, market="CN", on_progress=None) -> dict
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

PRESETS_DIR = Path(__file__).parent


@dataclass
class AgentSpec:
    """单个 agent 角色定义(借 Vibe-Trading 的 agents[] 数组思路)。"""

    id: str
    role: str
    system_prompt: str


@dataclass
class PresetConfig:
    """单个 preset 的完整配置。"""

    name: str
    title: str
    description: str
    agents: list[AgentSpec] = field(default_factory=list)
    debate_rounds: int = 1
    selected_analysts: list[str] = field(default_factory=lambda: ["market", "social", "news", "fundamentals"])
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "agents": [{"id": a.id, "role": a.role} for a in self.agents],
            "debate_rounds": self.debate_rounds,
            "selected_analysts": self.selected_analysts,
            "metadata": self.metadata,
        }


def _load_yaml(name: str) -> Optional[PresetConfig]:
    """从 yaml 文件读 preset 配置。"""
    path = PRESETS_DIR / f"{name}.yaml"
    if not path.exists():
        logger.warning(f"preset 文件不存在: {path}")
        return None
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"preset yaml 解析失败: {path} | {e}")
        return None

    agents = [
        AgentSpec(id=a["id"], role=a.get("role", a["id"]), system_prompt=a.get("system_prompt", ""))
        for a in raw.get("agents", [])
    ]
    return PresetConfig(
        name=raw["name"],
        title=raw.get("title", raw["name"]),
        description=raw.get("description", ""),
        agents=agents,
        debate_rounds=raw.get("debate_rounds", 1),
        selected_analysts=raw.get("selected_analysts", ["market", "social", "news", "fundamentals"]),
        metadata=raw.get("metadata", {}),
    )


def list_presets() -> list[PresetConfig]:
    """列出所有 preset。"""
    out = []
    for p in sorted(PRESETS_DIR.glob("*.yaml")):
        cfg = _load_yaml(p.stem)
        if cfg:
            out.append(cfg)
    return out


def get_preset(name: str) -> Optional[PresetConfig]:
    """按名字取 preset。"""
    return _load_yaml(name)


def build_swarm_context(name: str) -> str:
    """把 preset 的 agents[].system_prompt 拼成一段 swarm context。

    实际运行 TradingAgents 时,把这段文本注入到 system prompt 里,
    让 4 类分析师(market/social/news/fundamentals)各自扮演对应角色。
    """
    cfg = get_preset(name)
    if not cfg:
        return ""
    return "\n\n".join(
        f"## 角色: {a.role}\n{a.system_prompt}" for a in cfg.agents
    )


def run_preset(
    name: str,
    target: str,
    market: str = "CN",
    context: Any = None,  # AgentContext(由 API 层注入,自带 ai_client / notifier / config)
    data: dict | None = None,  # 给 TA 的 data dict(stock / quote / preset_meta)
) -> dict:
    """运行一个 preset(借 TradingAgents 的 4 类分析师 + 辩论机制)。

    ⚠️ 此函数**不自己造 AgentContext**——需要 caller 注入。
      详见 src/web/api/presets.py 的 POST /api/agents/presets/{name}/run 实现。
    """
    cfg = get_preset(name)
    if not cfg:
        return {"success": False, "error": f"preset 不存在: {name}"}

    if context is None or data is None:
        return {
            "success": False,
            "error": "必须传入 context (AgentContext) + data (含 stock/quote)",
        }

    # 懒导入避免循环依赖
    from src.agents.tradingagents.agent import TradingAgentsAgent

    agent = TradingAgentsAgent(
        analyst_types=cfg.selected_analysts,
        debate_rounds=cfg.debate_rounds,
    )

    # 把 preset meta 注入 data(供 TA agent 内部读到)
    data = dict(data or {})
    data["preset_meta"] = {
        "preset_name": name,
        "preset_title": cfg.title,
        "preset_agents": [{"id": a.id, "role": a.role} for a in cfg.agents],
        "swarm_context": build_swarm_context(name),
    }

    try:
        import asyncio
        result = asyncio.run(agent.analyze(context, data))
        # 注入 preset 元信息到结果
        if hasattr(result, "raw_data") and result.raw_data is not None:
            try:
                result.raw_data["preset"] = cfg.to_dict()
            except Exception:
                pass
        # 序列化: AnalysisResult → dict
        if hasattr(result, "__dict__"):
            serialized = {k: v for k, v in result.__dict__.items()}
        else:
            serialized = str(result)
        return {
            "success": True,
            "preset": name,
            "target": target,
            "market": market,
            "result": serialized,
        }
    except Exception as e:
        logger.exception(f"preset {name} 运行失败")
        return {"success": False, "preset": name, "error": str(e)}
