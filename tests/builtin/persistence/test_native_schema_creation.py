# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import Column, Index, Integer, MetaData, Table, create_engine, create_mock_engine, inspect, select
from sqlalchemy.dialects.mysql import dialect
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.builtin.artifacts.memory import EmbeddingProfile
from powercontext.builtin.persistence import schema
from powercontext.builtin.persistence.oceanbase.atomic_memory_index import OceanBaseAtomicMemoryIndex
from powercontext.builtin.persistence.oceanbase.topic_memory_index import OceanBaseTopicMemoryVectorIndex


@pytest.mark.parametrize("family", ["atomic-memory", "topic-memory"])
def test_relational_schema_creation_preserves_native_vector_index_ownership(family, monkeypatch):
    async def scenario():
        profile = EmbeddingProfile(
            profile_id="startup-test", model="test", dimension=3, distance="l2", normalization="unit"
        )
        index = (
            OceanBaseAtomicMemoryIndex(profile)
            if family == "atomic-memory"
            else OceanBaseTopicMemoryVectorIndex(profile)
        )
        statements = []
        sync = create_mock_engine("mysql://", lambda sql, *_a, **_k: statements.append(str(sql.compile(dialect=dialect()))))
        inspector = MagicMock()
        inspector.get_table_names.return_value = []
        inspector.has_table.return_value = False
        monkeypatch.setattr(schema, "inspect", lambda _connection: inspector)
        connection = AsyncMock(spec=AsyncConnection)
        connection.dialect = dialect()
        connection.run_sync.side_effect = lambda callback: callback(sync)
        await schema.create_tables(connection, index.tables)
        assert len(statements) == len(index.tables)
        assert all("VECTOR(3)" in sql for sql in statements)
        assert all("CREATE TABLE IF NOT EXISTS" in sql for sql in statements)
        # Native initializers own VECTOR DDL; table creation must not replace
        # it with an ordinary index or preempt the native race validation.
        assert not any("CREATE INDEX" in sql or "CREATE VECTOR INDEX" in sql for sql in statements)

    asyncio.run(scenario())


def test_relational_schema_creation_preserves_rows_and_regular_indexes():
    table = Table("startup_probe", MetaData(), Column("id", Integer, primary_key=True), Column("value", Integer))
    Index("ix_startup_probe_value", table.c.value)
    with create_engine("sqlite://").begin() as connection:
        schema._create_mysql_tables(connection, (table,))
        connection.execute(table.insert().values(id=1, value=42))
        schema._create_mysql_tables(connection, (table,))
        assert connection.execute(select(table)).all() == [(1, 42)]
        assert [index["name"] for index in inspect(connection).get_indexes(table.name)] == ["ix_startup_probe_value"]
