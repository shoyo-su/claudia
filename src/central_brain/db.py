"""SQLite + FTS5 + sqlite-vec database layer for Central Brain."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from central_brain.models import Memory, MemorySource, MemoryType, Session

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path.home() / ".central-brain" / "memory.db"
SCHEMA_VERSION = 2


def get_db(db_path: Path | None = None) -> sqlite3.Connection:
    """Open a WAL-mode SQLite connection with FTS5 and sqlite-vec."""
    path = db_path or DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")

    # Load sqlite-vec extension
    try:
        conn.enable_load_extension(True)
        import sqlite_vec
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except Exception as e:
        logger.debug("sqlite-vec not available: %s", e)

    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create tables and FTS5 index if they don't exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY
        );

        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            memory_type TEXT NOT NULL DEFAULT 'insight',
            source TEXT NOT NULL DEFAULT 'manual',
            session_id TEXT,
            project TEXT,
            tags TEXT NOT NULL DEFAULT '[]',
            importance INTEGER NOT NULL DEFAULT 3,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            access_count INTEGER NOT NULL DEFAULT 0,
            superseded_by INTEGER REFERENCES memories(id),
            metadata TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            project TEXT,
            started_at TEXT,
            ended_at TEXT,
            summary TEXT,
            transcript_path TEXT,
            memory_count INTEGER NOT NULL DEFAULT 0
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
            content,
            tags,
            project,
            memory_type,
            content='memories',
            content_rowid='id',
            tokenize='porter unicode61'
        );

        -- Sync triggers for FTS5
        CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
            INSERT INTO memories_fts(rowid, content, tags, project, memory_type)
            VALUES (new.id, new.content, new.tags, new.project, new.memory_type);
        END;

        CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, content, tags, project, memory_type)
            VALUES ('delete', old.id, old.content, old.tags, old.project, old.memory_type);
        END;

        CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, content, tags, project, memory_type)
            VALUES ('delete', old.id, old.content, old.tags, old.project, old.memory_type);
            INSERT INTO memories_fts(rowid, content, tags, project, memory_type)
            VALUES (new.id, new.content, new.tags, new.project, new.memory_type);
        END;

        CREATE INDEX IF NOT EXISTS idx_memories_project ON memories(project);
        CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(memory_type);
        CREATE INDEX IF NOT EXISTS idx_memories_importance ON memories(importance DESC);
        CREATE INDEX IF NOT EXISTS idx_memories_superseded ON memories(superseded_by);
        CREATE INDEX IF NOT EXISTS idx_memories_session ON memories(session_id);
    """)

    # Schema versioning and migrations
    existing = conn.execute("SELECT version FROM schema_version").fetchone()
    current_version = existing["version"] if existing else 0

    if current_version < 1:
        conn.execute("INSERT OR REPLACE INTO schema_version (version) VALUES (?)", (1,))

    if current_version < 2:
        _migrate_to_v2(conn)
    else:
        # Ensure vec table exists even if v2 migration ran without sqlite-vec loaded
        _ensure_vec_table(conn)

    conn.commit()


def _migrate_to_v2(conn: sqlite3.Connection) -> None:
    """Add embedding column and vec0 virtual table for vector search."""
    # Add embedding BLOB column if not present
    columns = {row[1] for row in conn.execute("PRAGMA table_info(memories)").fetchall()}
    if "embedding" not in columns:
        conn.execute("ALTER TABLE memories ADD COLUMN embedding BLOB")

    # Create vec0 virtual table — may fail if sqlite-vec isn't loaded
    _ensure_vec_table(conn)

    conn.execute("UPDATE schema_version SET version = 2")


def _ensure_vec_table(conn: sqlite3.Connection) -> None:
    """Create the vec0 virtual table if sqlite-vec is available and table doesn't exist."""
    # Check if table already exists
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='memories_vec'"
    ).fetchone()
    if exists:
        return

    try:
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS memories_vec USING vec0(
                memory_id INTEGER PRIMARY KEY,
                embedding float[1024]
            )
        """)
    except Exception as e:
        logger.warning("Could not create memories_vec table (sqlite-vec may not be loaded): %s", e)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_memory(row: sqlite3.Row) -> Memory:
    d = dict(row)
    # Remove extra columns not in the Memory model
    for extra in ("score", "rank", "embedding"):
        d.pop(extra, None)
    d["tags"] = json.loads(d.get("tags") or "[]")
    d["metadata"] = json.loads(d.get("metadata") or "{}")
    d["memory_type"] = MemoryType(d["memory_type"])
    d["source"] = MemorySource(d["source"])
    return Memory(**d)


def _row_to_session(row: sqlite3.Row) -> Session:
    return Session(**dict(row))


# --- Embedding helpers ---

def store_embedding(conn: sqlite3.Connection, memory_id: int, embedding: list[float]) -> None:
    """Store an embedding vector for a memory in both the blob column and vec0 table."""
    blob = np.array(embedding, dtype=np.float32).tobytes()
    conn.execute("UPDATE memories SET embedding = ? WHERE id = ?", (blob, memory_id))
    conn.execute(
        "INSERT OR REPLACE INTO memories_vec (memory_id, embedding) VALUES (?, ?)",
        (memory_id, blob),
    )
    conn.commit()


def embed_and_store(conn: sqlite3.Connection, memory_id: int, content: str, embedder) -> None:
    """Embed content and store the vector. Silently skips on failure."""
    if embedder is None:
        return
    try:
        vec = embedder.embed_single(content)
        store_embedding(conn, memory_id, vec)
    except Exception as e:
        logger.debug("Failed to embed memory %d: %s", memory_id, e)


# --- Memory CRUD ---

def insert_memory(conn: sqlite3.Connection, memory: Memory, dedup: bool = True, embedder=None) -> Memory | None:
    # Dedup: check if a similar memory already exists (same type, high content overlap)
    if dedup:
        existing = _find_duplicate(conn, memory, embedder=embedder)
        if existing:
            # Update importance if new one is higher, bump access count
            if memory.importance > existing.importance:
                conn.execute(
                    "UPDATE memories SET importance = ?, access_count = access_count + 1, updated_at = ? WHERE id = ?",
                    (memory.importance, _now(), existing.id),
                )
                conn.commit()
            return existing

    now = _now()
    cur = conn.execute(
        """INSERT INTO memories (content, memory_type, source, session_id, project,
           tags, importance, created_at, updated_at, access_count, superseded_by, metadata)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            memory.content,
            memory.memory_type.value,
            memory.source.value,
            memory.session_id,
            memory.project,
            memory.tags_json(),
            memory.importance,
            now,
            now,
            0,
            memory.superseded_by,
            memory.metadata_json(),
        ),
    )
    conn.commit()
    memory.id = cur.lastrowid
    memory.created_at = datetime.fromisoformat(now)
    memory.updated_at = datetime.fromisoformat(now)

    # Embed and store vector
    embed_and_store(conn, memory.id, memory.content, embedder)

    return memory


def _find_duplicate(conn: sqlite3.Connection, memory: Memory, embedder=None) -> Memory | None:
    """Check if a substantially similar memory exists using FTS5 + content comparison."""
    # Get key words from content for FTS search
    words = memory.content.split()[:8]
    # Quote each word and join for FTS5
    fts_tokens = " ".join(f'"{w}"' for w in words if len(w) > 2)
    if not fts_tokens:
        return None

    try:
        rows = conn.execute(
            """SELECT m.* FROM memories_fts fts
               JOIN memories m ON m.id = fts.rowid
               WHERE memories_fts MATCH ?
                 AND m.superseded_by IS NULL
                 AND m.memory_type = ?
               LIMIT 5""",
            (fts_tokens, memory.memory_type.value),
        ).fetchall()
    except Exception:
        return None

    # Check for high word overlap
    new_words = set(memory.content.lower().split())
    for row in rows:
        existing = _row_to_memory(row)
        existing_words = set(existing.content.lower().split())
        if not existing_words:
            continue
        overlap = len(new_words & existing_words) / max(len(new_words | existing_words), 1)
        if overlap > 0.5:
            return existing

    # Vector similarity check — catches semantic duplicates that word overlap misses
    if embedder is not None:
        try:
            vec = embedder.embed_single(memory.content)
            blob = np.array(vec, dtype=np.float32).tobytes()
            vec_rows = conn.execute(
                """SELECT memory_id, distance
                   FROM memories_vec
                   WHERE embedding MATCH ?
                   ORDER BY distance
                   LIMIT 3""",
                (blob,),
            ).fetchall()
            for vr in vec_rows:
                if vr["distance"] < 0.15:
                    candidate = conn.execute(
                        "SELECT * FROM memories WHERE id = ? AND superseded_by IS NULL AND memory_type = ?",
                        (vr["memory_id"], memory.memory_type.value),
                    ).fetchone()
                    if candidate:
                        return _row_to_memory(candidate)
        except Exception:
            pass  # Fall through — vector dedup is best-effort

    return None


def get_memory(conn: sqlite3.Connection, memory_id: int) -> Memory | None:
    row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
    if not row:
        return None
    # Bump access count
    conn.execute(
        "UPDATE memories SET access_count = access_count + 1 WHERE id = ?",
        (memory_id,),
    )
    conn.commit()
    return _row_to_memory(row)


def update_memory(
    conn: sqlite3.Connection,
    memory_id: int,
    content: str | None = None,
    tags: list[str] | None = None,
    importance: int | None = None,
    metadata: dict[str, Any] | None = None,
    embedder=None,
) -> Memory | None:
    existing = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
    if not existing:
        return None

    updates: list[str] = []
    params: list[Any] = []

    if content is not None:
        updates.append("content = ?")
        params.append(content)
    if tags is not None:
        updates.append("tags = ?")
        params.append(json.dumps(tags))
    if importance is not None:
        updates.append("importance = ?")
        params.append(importance)
    if metadata is not None:
        updates.append("metadata = ?")
        params.append(json.dumps(metadata))

    if not updates:
        return _row_to_memory(existing)

    updates.append("updated_at = ?")
    params.append(_now())
    params.append(memory_id)

    conn.execute(
        f"UPDATE memories SET {', '.join(updates)} WHERE id = ?",
        params,
    )
    conn.commit()

    # Re-embed if content changed
    if content is not None:
        embed_and_store(conn, memory_id, content, embedder)

    return get_memory(conn, memory_id)


def supersede_memory(conn: sqlite3.Connection, old_id: int, new_id: int) -> bool:
    conn.execute(
        "UPDATE memories SET superseded_by = ?, updated_at = ? WHERE id = ?",
        (new_id, _now(), old_id),
    )
    conn.commit()
    return True


def delete_memory(conn: sqlite3.Connection, memory_id: int) -> bool:
    cur = conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
    conn.commit()
    return cur.rowcount > 0


# --- Session CRUD ---

def upsert_session(conn: sqlite3.Connection, session: Session) -> Session:
    conn.execute(
        """INSERT INTO sessions (session_id, project, started_at, ended_at, summary, transcript_path, memory_count)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(session_id) DO UPDATE SET
             ended_at = COALESCE(excluded.ended_at, sessions.ended_at),
             summary = COALESCE(excluded.summary, sessions.summary),
             transcript_path = COALESCE(excluded.transcript_path, sessions.transcript_path),
             memory_count = COALESCE(excluded.memory_count, sessions.memory_count)""",
        (
            session.session_id,
            session.project,
            session.started_at.isoformat() if session.started_at else _now(),
            session.ended_at.isoformat() if session.ended_at else None,
            session.summary,
            session.transcript_path,
            session.memory_count,
        ),
    )
    conn.commit()
    return session


def get_session(conn: sqlite3.Connection, session_id: str) -> Session | None:
    row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    return _row_to_session(row) if row else None


def list_sessions(conn: sqlite3.Connection, limit: int = 20) -> list[Session]:
    rows = conn.execute(
        "SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [_row_to_session(r) for r in rows]


def update_session_memory_count(conn: sqlite3.Connection, session_id: str) -> None:
    count = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE session_id = ?", (session_id,)
    ).fetchone()[0]
    conn.execute(
        "UPDATE sessions SET memory_count = ? WHERE session_id = ?",
        (count, session_id),
    )
    conn.commit()


# --- Stats ---

def get_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    total = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE superseded_by IS NULL"
    ).fetchone()[0]

    type_rows = conn.execute(
        "SELECT memory_type, COUNT(*) as cnt FROM memories WHERE superseded_by IS NULL GROUP BY memory_type"
    ).fetchall()
    by_type = {r["memory_type"]: r["cnt"] for r in type_rows}

    total_sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

    most_accessed = conn.execute(
        "SELECT * FROM memories WHERE superseded_by IS NULL ORDER BY access_count DESC LIMIT 5"
    ).fetchall()

    recent = conn.execute(
        "SELECT * FROM memories WHERE superseded_by IS NULL ORDER BY created_at DESC LIMIT 10"
    ).fetchall()

    return {
        "total_memories": total,
        "by_type": by_type,
        "total_sessions": total_sessions,
        "most_accessed": [_row_to_memory(r) for r in most_accessed],
        "recent": [_row_to_memory(r) for r in recent],
    }
