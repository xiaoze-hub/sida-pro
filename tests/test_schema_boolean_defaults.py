"""新 PG 建库不得把布尔默认值编译为整数；SQLite 原生 INSERT 也保留默认值。"""

import re

import pytest
from sqlalchemy import MetaData, create_engine, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from src.db.models import ChatConversation, User


@pytest.mark.parametrize(
    ("model", "column"), [(User, "is_deleted"), (ChatConversation, "pinned")]
)
def test_boolean_defaults_compile_for_postgres(model: type, column: str) -> None:
    ddl = str(CreateTable(model.__table__).compile(dialect=postgresql.dialect()))
    assert re.search(rf"\b{column} BOOLEAN DEFAULT false NOT NULL", ddl)


def test_raw_inserts_keep_sqlite_boolean_defaults() -> None:
    engine = create_engine("sqlite://")
    metadata = MetaData()
    User.__table__.to_metadata(metadata)
    ChatConversation.__table__.to_metadata(metadata)
    metadata.create_all(engine)
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO users (id, username, password_hash, role, is_active, token_version) "
                "VALUES ('local-test', 'local-test', 'test-only', 'member', 1, 1)"
            ))
            conn.execute(text("INSERT INTO chat_conversations (id) VALUES (1)"))
            assert conn.execute(text("SELECT is_deleted FROM users")).scalar_one() == 0
            assert conn.execute(text("SELECT pinned FROM chat_conversations")).scalar_one() == 0
    finally:
        engine.dispose()
