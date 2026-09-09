"""src.bootstrap — W3.2/D1 从 server.py 拆出的启动装配层。

模块划分:
- app.py        FastAPI 装配(create_app: agent 自动发现 + lifespan + SPA 兜底)
- agents.py     AGENT_REGISTRY 注册表 + @register_agent 装饰器 + 自动发现 + seed_agents
- datasources.py 数据源种子/对账、策略目录、示例股票等 seed_*
- runtime.py    调度器全局实例 + Agent 触发/上下文构建(存量 `from server import X` 转发目标)
- startup.py    lifespan(启动门禁 → init_db → 自检 → 种子 → 调度器编排)
- env.py        代理/SSL/日志/Playwright 进程环境(本模块)
"""
