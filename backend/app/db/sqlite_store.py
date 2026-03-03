"""SQLite-backed conversation persistence."""

import asyncio
import aiosqlite
import json
import uuid
from datetime import datetime, timezone

from app.config import settings

_db_path = str(settings.sqlite_path)
_db: aiosqlite.Connection | None = None
_db_init_lock = asyncio.Lock()


async def _get_db() -> aiosqlite.Connection:
    global _db
    if _db is not None:
        return _db

    async with _db_init_lock:
        if _db is not None:
            return _db

        conn = await aiosqlite.connect(_db_path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.execute("PRAGMA busy_timeout=5000")
        _db = conn
        return conn


async def init_sqlite() -> None:
    """Create tables if they don't exist."""
    db = await _get_db()
    await db.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT 'New conversation',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            charts TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (conversation_id) REFERENCES conversations(id)
        )
    """)
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_conversations_updated_at "
        "ON conversations(updated_at)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_conversation_created_at "
        "ON messages(conversation_id, created_at)"
    )
    await db.commit()


async def close_sqlite() -> None:
    global _db
    async with _db_init_lock:
        if _db is not None:
            await _db.close()
            _db = None


async def create_conversation(title: str = "New conversation") -> dict:
    now = datetime.now(timezone.utc).isoformat()
    conv_id = str(uuid.uuid4())
    db = await _get_db()
    await db.execute(
        "INSERT INTO conversations (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (conv_id, title, now, now),
    )
    await db.commit()
    return {"id": conv_id, "title": title, "created_at": now, "updated_at": now}


async def list_conversations() -> list[dict]:
    db = await _get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM conversations ORDER BY updated_at DESC"
    )
    return [dict(r) for r in rows]


async def get_conversation(conv_id: str) -> dict | None:
    db = await _get_db()
    cursor = await db.execute(
        "SELECT * FROM conversations WHERE id = ?", (conv_id,)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def update_conversation_title(conv_id: str, title: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    db = await _get_db()
    await db.execute(
        "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
        (title, now, conv_id),
    )
    await db.commit()


async def delete_conversation(conv_id: str) -> None:
    db = await _get_db()
    await db.execute("DELETE FROM messages WHERE conversation_id = ?", (conv_id,))
    await db.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
    await db.commit()


async def add_message(
    conversation_id: str, role: str, content: str, charts: list | None = None
) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    msg_id = str(uuid.uuid4())
    charts_json = json.dumps(charts) if charts else None
    db = await _get_db()
    await db.execute(
        "INSERT INTO messages (id, conversation_id, role, content, charts, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (msg_id, conversation_id, role, content, charts_json, now),
    )
    await db.execute(
        "UPDATE conversations SET updated_at = ? WHERE id = ?",
        (now, conversation_id),
    )
    await db.commit()
    return {
        "id": msg_id,
        "conversation_id": conversation_id,
        "role": role,
        "content": content,
        "charts": charts,
        "created_at": now,
    }


async def get_messages(conversation_id: str) -> list[dict]:
    db = await _get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at",
        (conversation_id,),
    )
    result = []
    for r in rows:
        d = dict(r)
        d["charts"] = json.loads(d["charts"]) if d["charts"] else None
        result.append(d)
    return result
