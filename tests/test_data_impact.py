"""P2-3 钉子: 影响面映射要么写实、要么如实说"未登记"。"""
from __future__ import annotations

from src.core.data_capabilities import DATASET_LABELS
from src.core.data_impact import DATA_IMPACT, impact_of


def test_every_dataset_has_an_impact_entry():
    """每个已登记数据集都必须有影响面 —— 新增数据集忘了补, 这里红。"""
    missing = sorted(set(DATASET_LABELS) - set(DATA_IMPACT))
    assert missing == [], f"这些数据集还没有影响面条目: {missing}"


def test_no_orphan_impact_entries():
    """反过来: 影响面里不该有"已经不存在的数据集"(防改名后留垃圾)。"""
    orphans = sorted(set(DATA_IMPACT) - set(DATASET_LABELS))
    assert orphans == [], f"影响面里有未登记的数据集: {orphans}"


def test_unmapped_is_honest_not_silent():
    """未登记 ≠ 没影响: 必须显式说"未登记", 不给空话也不冒充正常。"""
    r = impact_of("does_not_exist")
    assert r["pages"] == []
    assert "未登记" in r["effect"]
    assert "不代表没影响" in r["effect"]


def test_entries_are_actionable():
    """每条都要说清: 受影响页面 + 降级时的实际表现(不能是"可能受影响"这种废话)。"""
    for key, v in DATA_IMPACT.items():
        assert v.get("pages"), f"{key} 没列受影响页面"
        eff = str(v.get("effect") or "")
        assert len(eff) >= 8, f"{key} 的 effect 太含糊: {eff!r}"
        for vague in ("可能受影响", "或有问题", "视情况"):
            assert vague not in eff, f"{key} 用了没信息量的措辞: {vague}"
        assert str(v.get("fallback") or ""), f"{key} 没写是否有替代源"


def test_key_datasets_say_what_we_know():
    """抽查几条"我们确实知道"的口径, 防被改写成模糊话。"""
    kb = impact_of("kline")
    assert any("工作台" in p for p in kb["pages"])
    assert "三源" in kb["fallback"] or "tencent" in kb["fallback"]
    nb = impact_of("northbound")
    assert "已知缺口" in nb["effect"]      # 北向常无数据是已知事实, 要写出来
    ev = impact_of("events")
    assert "灰显" in ev["effect"]          # §12 兜底规范: 灰显不隐藏
