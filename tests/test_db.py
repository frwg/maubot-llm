import tempfile
from pathlib import Path

import pytest
from mautrix.util.async_db import Connection, Database, UpgradeTable

from maubot_llm import db


@pytest.mark.asyncio
async def test_context_scopes_limits_and_deduplication():
    with tempfile.TemporaryDirectory() as temp_dir:
        database = Database.create(
            f"sqlite:///{Path(temp_dir, 'test.sqlite3').resolve()}",
            upgrade_table=db.upgrade_table,
        )
        await database.start()
        try:
            room = db.Room()
            room.room_id = "!room:example.org"
            await db.upsert_room(database, room)
            saved_room = await db.fetch_room(database, room.room_id)
            assert saved_room.room_id == room.room_id
            assert saved_room.mode is None

            for index in range(4):
                await db.append_context(
                    database,
                    room.room_id,
                    "user",
                    f"message {index}",
                    scope_id="thread:$thread",
                    max_messages=3,
                )
            await db.append_context(
                database,
                room.room_id,
                "user",
                "private",
                scope_id="dm:@alice:example.org",
            )

            assert await db.fetch_context(
                database, room.room_id, scope_id="thread:$thread"
            ) == [
                {"role": "user", "content": "message 1"},
                {"role": "user", "content": "message 2"},
                {"role": "user", "content": "message 3"},
            ]
            assert await db.fetch_context(
                database, room.room_id, scope_id="dm:@alice:example.org"
            ) == [{"role": "user", "content": "private"}]
            assert await db.fetch_context(database, room.room_id) == []

            assert await db.claim_event(database, "$event")
            assert not await db.claim_event(database, "$event")
            await db.clear_context(database, room.room_id, "dm:@alice:example.org")
            assert (
                await db.fetch_context(
                    database, room.room_id, scope_id="dm:@alice:example.org"
                )
                == []
            )
            await db.clear_context(database, room.room_id)
            assert (
                await db.fetch_context(
                    database, room.room_id, scope_id="thread:$thread"
                )
                == []
            )
        finally:
            await database.stop()


@pytest.mark.asyncio
async def test_v2_migration_preserves_existing_room_context():
    with tempfile.TemporaryDirectory() as temp_dir:
        uri = f"sqlite:///{Path(temp_dir, 'legacy.sqlite3').resolve()}"
        legacy_upgrade = UpgradeTable()

        @legacy_upgrade.register(description="legacy v1")
        async def create_legacy_schema(conn: Connection) -> None:
            await conn.execute(
                """CREATE TABLE rooms (
                    id TEXT NOT NULL PRIMARY KEY,
                    backend TEXT,
                    model TEXT,
                    system_prompt TEXT
                )"""
            )
            await conn.execute(
                """CREATE TABLE context_entries (
                    room_id TEXT NOT NULL,
                    seq_num INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    PRIMARY KEY (room_id, seq_num),
                    FOREIGN KEY (room_id) REFERENCES rooms(id)
                )"""
            )

        legacy_db = Database.create(uri, upgrade_table=legacy_upgrade)
        await legacy_db.start()
        await legacy_db.execute(
            "INSERT INTO rooms(id) VALUES ($1)", "!legacy:example.org"
        )
        await legacy_db.execute(
            """INSERT INTO context_entries(room_id, seq_num, role, content)
               VALUES ($1, 1, 'user', 'preserve this')""",
            "!legacy:example.org",
        )
        await legacy_db.stop()

        upgraded_db = Database.create(uri, upgrade_table=db.upgrade_table)
        await upgraded_db.start()
        try:
            assert await db.fetch_context(upgraded_db, "!legacy:example.org") == [
                {"role": "user", "content": "preserve this"}
            ]
            assert (
                await upgraded_db.fetchval(
                    "SELECT seq_num FROM context_counters WHERE room_id=$1",
                    "!legacy:example.org",
                )
                == 1
            )
        finally:
            await upgraded_db.stop()
