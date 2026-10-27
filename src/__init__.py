"""SIDA 源码包入口。

2026-10-27：品牌重命名 SIDA_* → SIDA_*。这里在包导入时补一次环境变量别名，
让「代码已改名、部署 env 还是旧名」的过渡期不炸。见 src/core/env_alias.py。
"""
try:  # 绝不因为兼容层失败而拖垮导入
    from src.core.env_alias import alias_legacy_env as _alias_legacy_env
    _alias_legacy_env()
except Exception:  # noqa: BLE001
    pass
