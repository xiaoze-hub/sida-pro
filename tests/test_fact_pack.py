"""测试共享事实包 (fact_pack.py) 与技能锁 (skills.py / recipes.py)。"""

from __future__ import annotations

import os
import pytest
from datetime import datetime
from src.core.fact_pack import build_pack, to_prompt_block
from src.harness.skills import verify_lock
from src.harness.recipes import load_recipes


def test_fact_pack_id_stability():
    """测试同一 now 两次 build_pack → pack_id 相同；缺失标的进 missing 且无伪造数字。"""
    now = datetime(2026, 11, 3, 15, 0, 0)
    symbols = ["600519", "000001", "NONEXISTENT999"]
    
    pack1 = build_pack(symbols, market="CN", now=now)
    pack2 = build_pack(symbols, market="CN", now=now)
    
    assert pack1["pack_id"] == pack2["pack_id"], "同一 now 两次构建的 pack_id 必须相同"
    assert "NONEXISTENT999" in pack1["missing"], "取数失败标的必须进 missing"
    
    # 验证不编造数字 / 不补 0
    non_existent_data = pack1["symbols"].get("NONEXISTENT999")
    if non_existent_data:
        assert non_existent_data.get("price") is None, "取数失败不许伪造价格"


def test_fact_pack_prompt_block():
    """测试 to_prompt_block 生成规范文本。"""
    pack = build_pack(["600519"], market="CN")
    block = to_prompt_block(pack)
    assert "【共享事实包】" in block
    assert "600519" in block


def test_skills_lock_verification():
    """测试 verify_lock 校验逻辑。"""
    issues = verify_lock()
    assert issues == [], f"初始状态下锁校验应该全过，发现异常: {issues}"


def test_recipe_missing_skill_raises():
    """测试配方引用不存在的技能时加载报错。"""
    from src.harness.recipes import Recipe
    import src.harness.recipes as recipes_mod
    
    # 临时往 recipes.load_recipes 注入或模拟一个非法引用
    # 由于 load_recipes 有 lru_cache，我们测试 verify_lock 或直接测 recipes 检查逻辑
    # 我们也可以手动调用一次 load_recipes 的核心检查或检查 skills.lock.json 缺失校验
    
    # 我们在代码中已经写了: if lock_data and sk not in lock_data: raise ValueError(...)
    # 验证当 lock 中删掉某个技能时，load_recipes 会抛出异常
    from src.harness.skills import _LOCK_PATH
    lock_path = _LOCK_PATH
    if lock_path.exists():
        original_content = lock_path.read_text(encoding="utf-8")
        try:
            # 清空或移除 auction
            import json
            data = json.loads(original_content)
            if "auction" in data:
                del data["auction"]
                lock_path.write_text(json.dumps(data), encoding="utf-8")
                
            # 清除 load_recipes 缓存
            recipes_mod.load_recipes.cache_clear()
            
            with pytest.raises(ValueError, match="引用了不存在或未锁定的技能"):
                recipes_mod.load_recipes()
        finally:
            lock_path.write_text(original_content, encoding="utf-8")
            recipes_mod.load_recipes.cache_clear()
