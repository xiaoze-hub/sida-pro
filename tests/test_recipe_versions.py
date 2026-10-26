"""Agent 目录 / 配方版本（P2-2.1）。

钉住三件事：
  1. 版本是可复核的**内容指纹**，不是手写版本号（手写的必然忘记改）；
  2. 历史惰性记录且**幂等**（同一指纹只记一次，重复读不会刷出假版本）；
  3. 没有历史就说空列表，不编造"上一版"。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.core import recipe_versions as rv
from src.db.models import Base, RecipeVersion
from src.harness import recipes as R


@pytest.fixture()
def db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng)
    s = Session()
    yield s
    s.close()


def test_目录含版本指纹与工具明细():
    items = R.catalog()
    assert len(items) >= 10, "配方目录不该为空"
    first = items[0]
    for k in ("id", "label", "tools", "output_sections", "version", "updated_at"):
        assert k in first, f"目录缺字段 {k}"
    assert isinstance(first["version"], str) and len(first["version"]) == 8


def test_指纹可复核_同一文件算出同一个值():
    assert R.recipe_fingerprint("deep_diagnosis") == R.recipe_fingerprint("deep_diagnosis")


def test_不存在的配方指纹为None_不编造():
    assert R.recipe_fingerprint("no_such_recipe") is None
    assert R.recipe_updated_at("no_such_recipe") is None


def test_首次同步记录全部配方(db):
    changed = rv.sync_versions(db)
    assert len(changed) >= 10
    assert db.query(RecipeVersion).count() == len(changed)


def test_重复同步幂等_不刷假版本(db):
    rv.sync_versions(db)
    n1 = db.query(RecipeVersion).count()
    again = rv.sync_versions(db)
    assert again == [], "同一指纹不该重复记录"
    assert db.query(RecipeVersion).count() == n1


def test_历史上限与排序(db):
    for i in range(3):
        db.add(RecipeVersion(recipe_id="deep_diagnosis", fingerprint=f"fp{i}", snapshot="{}"))
    db.commit()
    hist = rv.history(db, "deep_diagnosis", limit=2)
    assert len(hist) == 2
    assert hist[0]["fingerprint"] == "fp2", "新→旧"


def test_没有历史返回空列表(db):
    assert rv.history(db, "never_seen_recipe") == []
