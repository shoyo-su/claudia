"""FastMCP server for Central Brain — memory tools exposed via MCP."""

from __future__ import annotations

from fastmcp import FastMCP

from central_brain.db import (
    DEFAULT_DB_PATH,
    delete_memory,
    get_db,
    get_memory,
    get_stats,
    init_db,
    insert_memory,
    list_sessions,
    supersede_memory,
    update_memory,
)
from central_brain.embedder import get_embedder
from central_brain.models import Memory, MemorySource, MemoryType
from central_brain.search import hybrid_search

mcp = FastMCP("central-brain")

# Module-level singletons (lazy init)
_conn = None
_embedder = None
_embedder_initialized = False


def _get_conn():
    global _conn
    if _conn is None:
        _conn = get_db(DEFAULT_DB_PATH)
        init_db(_conn)
    return _conn


def _get_embedder():
    global _embedder, _embedder_initialized
    if not _embedder_initialized:
        _embedder = get_embedder()
        _embedder_initialized = True
    return _embedder


@mcp.tool()
def remember(
    content: str,
    memory_type: str = "insight",
    project: str | None = None,
    tags: list[str] | None = None,
    importance: int = 3,
    session_id: str | None = None,
) -> dict:
    """Store a memory in the central brain.

    Args:
        content: The memory content — what should be remembered
        memory_type: One of: insight, decision, pattern, error, preference, todo, open_loop
        project: Project name/path this memory relates to
        tags: Keywords for categorization
        importance: 1-5 score (5 = critical)
        session_id: Optional session this memory came from
    """
    conn = _get_conn()
    embedder = _get_embedder()
    mem = Memory(
        content=content,
        memory_type=MemoryType(memory_type),
        source=MemorySource.MANUAL if not session_id else MemorySource.SESSION,
        session_id=session_id,
        project=project,
        tags=tags or [],
        importance=importance,
    )
    result = insert_memory(conn, mem, embedder=embedder)
    return {
        "status": "stored",
        "id": result.id,
        "content": result.content,
        "memory_type": result.memory_type.value,
    }


@mcp.tool()
def recall(
    query: str = "",
    project: str | None = None,
    memory_type: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """Search memories by query with optional filters. Uses hybrid FTS5 + vector search.

    Args:
        query: Natural language search query (empty = recent memories)
        project: Filter to specific project
        memory_type: Filter by type (insight/decision/pattern/error/preference/todo/open_loop)
        limit: Max results to return
    """
    conn = _get_conn()
    embedder = _get_embedder()
    results = hybrid_search(conn, query, embedder=embedder, project=project, memory_type=memory_type, limit=limit)
    return [
        {
            "id": r.memory.id,
            "content": r.memory.content,
            "memory_type": r.memory.memory_type.value,
            "project": r.memory.project,
            "tags": r.memory.tags,
            "importance": r.memory.importance,
            "score": r.score,
            "created_at": str(r.memory.created_at) if r.memory.created_at else None,
        }
        for r in results
    ]


@mcp.tool()
def forget(memory_id: int, superseded_by: int | None = None) -> dict:
    """Supersede or delete a memory.

    Args:
        memory_id: ID of the memory to forget
        superseded_by: If provided, marks as superseded rather than deleting
    """
    conn = _get_conn()
    if superseded_by:
        supersede_memory(conn, memory_id, superseded_by)
        return {"status": "superseded", "id": memory_id, "superseded_by": superseded_by}
    else:
        deleted = delete_memory(conn, memory_id)
        return {"status": "deleted" if deleted else "not_found", "id": memory_id}


@mcp.tool()
def get_memory_by_id(memory_id: int) -> dict:
    """Fetch a specific memory by its ID.

    Args:
        memory_id: The memory ID to retrieve
    """
    conn = _get_conn()
    mem = get_memory(conn, memory_id)
    if not mem:
        return {"error": "not_found", "id": memory_id}
    return {
        "id": mem.id,
        "content": mem.content,
        "memory_type": mem.memory_type.value,
        "source": mem.source.value,
        "project": mem.project,
        "tags": mem.tags,
        "importance": mem.importance,
        "access_count": mem.access_count,
        "created_at": str(mem.created_at) if mem.created_at else None,
        "updated_at": str(mem.updated_at) if mem.updated_at else None,
        "superseded_by": mem.superseded_by,
        "metadata": mem.metadata,
    }


@mcp.tool()
def update_memory_tool(
    memory_id: int,
    content: str | None = None,
    tags: list[str] | None = None,
    importance: int | None = None,
) -> dict:
    """Update an existing memory's content, tags, or importance.

    Args:
        memory_id: The memory ID to update
        content: New content (optional)
        tags: New tags (optional)
        importance: New importance score 1-5 (optional)
    """
    conn = _get_conn()
    embedder = _get_embedder()
    mem = update_memory(conn, memory_id, content=content, tags=tags, importance=importance, embedder=embedder)
    if not mem:
        return {"error": "not_found", "id": memory_id}
    return {
        "status": "updated",
        "id": mem.id,
        "content": mem.content,
        "tags": mem.tags,
        "importance": mem.importance,
    }


@mcp.tool()
def list_recent_sessions(limit: int = 10) -> list[dict]:
    """List recent Claude Code sessions with their summaries.

    Args:
        limit: Max sessions to return
    """
    conn = _get_conn()
    sessions = list_sessions(conn, limit)
    return [
        {
            "session_id": s.session_id,
            "project": s.project,
            "started_at": str(s.started_at) if s.started_at else None,
            "ended_at": str(s.ended_at) if s.ended_at else None,
            "summary": s.summary,
            "memory_count": s.memory_count,
        }
        for s in sessions
    ]


@mcp.tool()
def brain_stats() -> dict:
    """Get statistics about the central brain — memory counts, most accessed, recent additions."""
    conn = _get_conn()
    stats = get_stats(conn)
    return {
        "total_memories": stats["total_memories"],
        "by_type": stats["by_type"],
        "total_sessions": stats["total_sessions"],
        "most_accessed": [
            {"id": m.id, "content": m.content[:100], "access_count": m.access_count}
            for m in stats["most_accessed"]
        ],
        "recent": [
            {"id": m.id, "content": m.content[:100], "memory_type": m.memory_type.value, "created_at": str(m.created_at)}
            for m in stats["recent"]
        ],
    }
