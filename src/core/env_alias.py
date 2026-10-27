"""环境变量前缀兼容层（2026-10-27 品牌重命名 PANWATCH_* → SIDA_*）。

背景：生产容器的 env 里仍然是 `PANWATCH_URL / PANWATCH_DB / PANWATCH_DATA …`
（这些是部署脚本写进去的，改名必须与镜像发布同步，否则容器起不来）。
为了做到「代码里彻底清掉 panwatch，但线上不中断」，这里在进程启动时做一次**双向别名**：

    PANWATCH_X  ──▶  SIDA_X       （旧部署还没更新时，新代码照样读到）
    SIDA_X      ──▶  PANWATCH_X   （万一还有没改到的地方读旧名，也不至于空值）

即：**只补缺，不覆盖**——已经显式设置的那一侧永远优先。
等部署脚本全量切到 `SIDA_*` 且观察一个发版周期后，这个模块可以删掉。

调用点：`src/__init__.py`（任何 `import src.*` 都会先跑到），这是唯一能覆盖
server / 迁移脚本 / 预测引擎 / 测试四类入口的choke point。
"""
from __future__ import annotations

import os

LEGACY_PREFIX = "PANWATCH_"
CURRENT_PREFIX = "SIDA_"

_applied = False


def alias_legacy_env() -> None:
    """把旧前缀环境变量双向补齐到新前缀（只补缺，不覆盖）。幂等。"""
    global _applied
    if _applied:
        return
    _applied = True

    for key, value in list(os.environ.items()):
        if key.startswith(LEGACY_PREFIX):
            target = CURRENT_PREFIX + key[len(LEGACY_PREFIX):]
            os.environ.setdefault(target, value)
        elif key.startswith(CURRENT_PREFIX):
            target = LEGACY_PREFIX + key[len(CURRENT_PREFIX):]
            os.environ.setdefault(target, value)


def env(name: str, default: str | None = None) -> str | None:
    """读一个新前缀变量：`env("DATA_DIR")` → SIDA_DATA_DIR，缺失回落到默认值。

    新代码请用它，别再手写 `os.getenv("SIDA_...")` —— 前缀只在这一个文件里出现。
    """
    alias_legacy_env()
    return os.getenv(CURRENT_PREFIX + name, default)
