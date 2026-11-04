from __future__ import annotations

import time
from typing import Any

from mautrix.util.async_db import Connection, Database, UpgradeTable

upgrade_table = UpgradeTable()


@upgrade_table.register(description="v1")
async def upgrade_v1(conn: Connection) -> None:
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


@upgrade_table.register(description="Add context scopes and event deduplication")
async def upgrade_v2(conn: Connection) -> None:
    await conn.execute(
        "ALTER TABLE context_entries ADD COLUMN scope_id TEXT NOT NULL DEFAULT ''"
    )
    await conn.execute(
        "ALTER TABLE context_entries ADD COLUMN created_at DOUBLE PRECISION NOT NULL DEFAULT 0"
    )
    await conn.execute(
        "UPDATE context_entries SET created_at=$1 WHERE created_at=0", time.time()
    )
    await conn.execute("ALTER TABLE rooms ADD COLUMN mode TEXT")
    await conn.execute(
        """CREATE TABLE context_counters (
            room_id TEXT NOT NULL PRIMARY KEY,
            seq_num INTEGER NOT NULL
        )"""
    )
    await conn.execute(
        """INSERT INTO context_counters(room_id, seq_num)
           SELECT r.id, COALESCE(MAX(e.seq_num), 0)
           FROM rooms r LEFT JOIN context_entries e ON e.room_id=r.id
           GROUP BY r.id
           ON CONFLICT (room_id) DO NOTHING"""
    )
    await conn.execute(
        """CREATE TABLE processed_events (
            event_id TEXT NOT NULL PRIMARY KEY,
            created_at DOUBLE PRECISION NOT NULL
        )"""
    )
    await conn.execute(
        "CREATE INDEX context_entries_scope_idx ON context_entries(room_id, scope_id, seq_num)"
    )
    await conn.execute(
        "CREATE INDEX context_entries_expiry_idx ON context_entries(created_at)"
    )
    await conn.execute(
        "CREATE INDEX processed_events_expiry_idx ON processed_events(created_at)"
    )


class Room:
    def __init__(self) -> None:
        self.room_id: str | None = None
        self.backend: str | None = None
        self.model: str | None = None
        self.system_prompt: str | None = None
        self.mode: str | None = None


async def fetch_room(db: Database, room_id: str) -> Room | None:
    row = await db.fetchrow(
        "SELECT id, backend, model, system_prompt, mode FROM rooms WHERE id=$1",
        room_id,
    )
    if not row:
        return None
    room = Room()
    room.room_id = room_id
    room.backend = row["backend"]
    room.model = row["model"]
    room.system_prompt = row["system_prompt"]
    room.mode = row["mode"]
    return room


async def upsert_room(db: Database, room: Room) -> None:
    await db.execute(
        """INSERT INTO rooms(id, backend, model, system_prompt, mode)
           VALUES ($1, $2, $3, $4, $5)
           ON CONFLICT (id) DO UPDATE SET backend=excluded.backend,
               model=excluded.model, system_prompt=excluded.system_prompt,
               mode=excluded.mode""",
        room.room_id,
        room.backend,
        room.model,
        room.system_prompt,
        room.mode,
    )


async def fetch_context(
    db: Database,
    room_id: str,
    scope_id: str = "",
    max_messages: int | None = None,
    ttl_seconds: int | None = None,
) -> list[dict[str, Any]]:
    if ttl_seconds:
        await db.execute(
            "DELETE FROM context_entries WHERE room_id=$1 AND scope_id=$2 AND created_at<$3",
            room_id,
            scope_id,
            time.time() - ttl_seconds,
        )
    query = (
        "SELECT role, content FROM context_entries "
        "WHERE room_id=$1 AND scope_id=$2 ORDER BY seq_num"
    )
    params: list[Any] = [room_id, scope_id]
    if max_messages is not None:
        query += " LIMIT $3"
        params.append(max_messages)
        rows = await db.fetch(query, *params)
        return [{"role": row["role"], "content": row["content"]} for row in rows]
    rows = await db.fetch(query, *params)
    return [{"role": row["role"], "content": row["content"]} for row in rows]


async def append_context(
    db: Database,
    room_id: str,
    role: str,
    content: str,
    scope_id: str = "",
    max_messages: int = 40,
) -> None:
    now = time.time()
    seq_num = await db.fetchval(
        """INSERT INTO context_counters(room_id, seq_num) VALUES ($1, 1)
           ON CONFLICT (room_id) DO UPDATE
           SET seq_num=context_counters.seq_num+1
           RETURNING seq_num""",
        room_id,
    )
    await db.execute(
        """INSERT INTO context_entries(room_id, seq_num, role, content, scope_id, created_at)
           VALUES ($1, $2, $3, $4, $5, $6)""",
        room_id,
        seq_num,
        role,
        content,
        scope_id,
        now,
    )
    await db.execute(
        """DELETE FROM context_entries
           WHERE room_id=$1 AND scope_id=$2 AND seq_num NOT IN (
               SELECT seq_num FROM context_entries
               WHERE room_id=$1 AND scope_id=$2
               ORDER BY seq_num DESC LIMIT $3
           )""",
        room_id,
        scope_id,
        max_messages,
    )


async def clear_context(
    db: Database, room_id: str, scope_id: str | None = None
) -> None:
    if scope_id is None:
        await db.execute("DELETE FROM context_entries WHERE room_id=$1", room_id)
    else:
        await db.execute(
            "DELETE FROM context_entries WHERE room_id=$1 AND scope_id=$2",
            room_id,
            scope_id,
        )


async def prune_expired_contexts(db: Database, ttl_seconds: int) -> None:
    if ttl_seconds > 0:
        await db.execute(
            "DELETE FROM context_entries WHERE created_at<$1",
            time.time() - ttl_seconds,
        )


async def claim_event(db: Database, event_id: str) -> bool:
    inserted = await db.fetchval(
        """INSERT INTO processed_events(event_id, created_at) VALUES ($1, $2)
           ON CONFLICT (event_id) DO NOTHING
           RETURNING event_id""",
        event_id,
        time.time(),
    )
    await db.execute(
        "DELETE FROM processed_events WHERE created_at<$1", time.time() - 7 * 86400
    )
    return inserted is not None
