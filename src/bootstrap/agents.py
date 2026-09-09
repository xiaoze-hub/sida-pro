"""Agent 注册表 + 装饰器自动发现(W3.2/D1)。

约定变更: 新增一个 agent 只需两步 ——
1. 在 src/agents/ 下新建文件, 实现类上加 @register_agent("<name>") 即完成注册
   (create_app 启动时 discover_agents() 自动发现, 不再改 server.py 的手工 dict);
2. 配置种子在 src/core/agent_catalog.py 的 AGENT_SEED_SPECS 加条目
   (seed_agents() 启动时自动播种, 无需改代码其他位置)。
"""

from __future__ import annotations

import logging

from src.core.agent_catalog import AGENT_KIND_WORKFLOW, AGENT_SEED_SPECS
from src.web.database import SessionLocal
from src.web.models import AgentConfig

logger = logging.getLogger("server")

# Agent 注册表: name -> 实现类。仅由 @register_agent 装饰器写入。
AGENT_REGISTRY: dict[str, type] = {}


def register_agent(name: str):
    """类装饰器: 把 agent 实现类注册进 AGENT_REGISTRY。"""

    def _deco(cls):
        AGENT_REGISTRY[name] = cls
        return cls

    return _deco


def discover_agents() -> None:
    """自动发现 src.agents 下全部顶层模块/子包, 装饰器在 import 时自注册。

    幂等: importlib 模块缓存保证模块体只执行一次, 重复调用不重复注册。
    """
    import importlib
    import pkgutil

    import src.agents as _pkg

    for m in pkgutil.iter_modules(_pkg.__path__):
        if m.name.startswith("_"):
            continue
        importlib.import_module(f"src.agents.{m.name}")


def seed_agents():
    """初始化内置 Agent 配置"""
    db = SessionLocal()
    for spec in AGENT_SEED_SPECS:
        existing = db.query(AgentConfig).filter(AgentConfig.name == spec.name).first()
        if not existing:
            db.add(
                AgentConfig(
                    name=spec.name,
                    display_name=spec.display_name,
                    description=spec.description,
                    kind=spec.kind,
                    visible=spec.visible,
                    lifecycle_status=spec.lifecycle_status,
                    replaced_by=spec.replaced_by,
                    display_order=spec.display_order,
                    enabled=spec.enabled,
                    schedule=spec.schedule,
                    execution_mode=spec.execution_mode,
                    config=spec.config or {},
                )
            )
        else:
            # 始终同步 execution_mode（确保代码中的定义生效）
            existing.execution_mode = spec.execution_mode or "batch"
            # 同步 display_name 和 description
            existing.display_name = spec.display_name or existing.display_name
            existing.description = spec.description or existing.description
            existing.kind = spec.kind
            existing.visible = bool(spec.visible)
            existing.lifecycle_status = spec.lifecycle_status or "active"
            existing.replaced_by = spec.replaced_by or ""
            existing.display_order = int(spec.display_order or 0)

            # capability 强制不参与调度，避免旧配置继续触发。
            if spec.kind != AGENT_KIND_WORKFLOW:
                existing.enabled = False
                existing.schedule = ""

            # 仅在用户未配置时补齐默认 config
            if spec.config and (not existing.config):
                existing.config = spec.config
            # 对已存在配置做“向前兼容”的字段补齐（不覆盖用户已有值）
            if existing.name == "intraday_monitor":
                cfg = existing.config or {}
                if isinstance(cfg, dict) and "event_only" not in cfg:
                    cfg["event_only"] = True
                    existing.config = cfg

    db.commit()
    db.close()
