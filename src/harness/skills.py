"""技能手册(2026-10-20, P2)。

和 Hermes 的 skill 同一个思路, 但只有一件事: 把「这类问题该怎么读」写成可注入的
程序性知识, 按配方加载, 用完即走。

为什么不用提示词堆在 SYSTEM_PROMPT 里: 6 个配方 × 各自手册 = 全塞进去会挤掉行情数据,
而且互相干扰(竞价手册的"9:25 前不定型"对盘后复盘毫无意义)。
"""

from __future__ import annotations

import functools
import hashlib
import json
from pathlib import Path
from typing import Any

_SKILL_DIR = Path(__file__).with_name("skills")
_LOCK_PATH = Path(__file__).with_name("skills.lock.json")
MAX_CHARS = 1200


@functools.lru_cache(maxsize=16)
def load_skill(name: str) -> str:
    """读一篇手册; 不存在返回空串(不抛, 配方可渐进加手册)。"""
    if not name or "/" in name or ".." in name:
        return ""
    path = _SKILL_DIR / f"{name}.md"
    try:
        text = path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""
    return text[:MAX_CHARS]


def skills_block(names: tuple[str, ...] | list[str] | None) -> str:
    """把多篇手册拼成一段可注入文本。"""
    if not names:
        return ""
    parts = [load_skill(n) for n in names]
    parts = [p for p in parts if p]
    return "\n\n".join(parts)


def verify_lock() -> list[str]:
    """校验 skills.lock.json 与磁盘实际 md 的一致性。
    
    返回问题清单（空=通过）：
    - lock 缺失的 md
    - md 改了但 lock 未更新（sha1 或 chars 不符）
    - 磁盘上没有但 lock 里有的
    """
    issues: list[str] = []
    
    # 读 lock 文件
    lock_data: dict[str, Any] = {}
    if _LOCK_PATH.exists():
        try:
            lock_data = json.loads(_LOCK_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            return [f"skills.lock.json 解析失败: {e}"]
    else:
        return ["skills.lock.json 文件不存在"]
    
    disk_files = {}
    if _SKILL_DIR.exists() and _SKILL_DIR.is_dir():
        for p in _SKILL_DIR.glob("*.md"):
            name = p.stem
            try:
                content = p.read_bytes()
                text = content.decode("utf-8")
                sha = hashlib.sha1(content).hexdigest()
                chars = len(text)
                disk_files[name] = {"sha1": sha, "chars": chars}
            except Exception as e:
                issues.append(f"读取手册文件 {p.name} 失败: {e}")
                
    # 检查磁盘有但 lock 没有，或者 hash/chars 不一致
    for name, info in disk_files.items():
        if name not in lock_data:
            issues.append(f"手册 '{name}' 存在于磁盘但未记录在 skills.lock.json 中")
        else:
            locked = lock_data[name]
            if locked.get("sha1") != info["sha1"] or locked.get("chars") != info["chars"]:
                issues.append(f"手册 '{name}' 内容已更改但 lock 未更新 (disk sha1={info['sha1'][:8]}, lock sha1={str(locked.get('sha1'))[:8]})")
                
    # 检查 lock 有但磁盘没有
    for name in lock_data:
        if name not in disk_files:
            issues.append(f"手册 '{name}' 存在于 skills.lock.json 但磁盘上对应的 .md 文件不存在")
            
    return issues
