"""FTS5 BM25 + sqlite-vec hybrid search with Reciprocal Rank Fusion."""

from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict

import numpy as np

from central_brain.db import _row_to_memory
from central_brain.models import MemorySearchResult

logger = logging.getLogger(__name__)

RRF_K = 60  # Standard RRF constant


def hybrid_search(
    conn: sqlite3.Connection,
    query: str,
    embedder=None,
    project: str | None = None,
    memory_type: str | None = None,
    limit: int = 20,
) -> list[MemorySearchResult]:
    """Search using FTS5 + vector with RRF fusion. Falls back to FTS5-only."""
    if not query.strip():
        return _recent_memories(conn, project, memory_type, limit)

    # FTS5 results
    fts_results = _fts5_ranked(conn, query, project, memory_type, limit)

    # Vector results (if embedder available)
    vec_results = []
    if embedder is not None:
        vec_results = _vec_ranked(conn, query, embedder, project, memory_type, limit)

    if not vec_results:
        # No vector search — just return FTS5 results directly
        return _bump_access(conn, fts_results[:limit])

    # RRF fusion
    rrf_scores: dict[int, float] = defaultdict(float)
    all_memories: dict[int, MemorySearchResult] = {}

    for rank, r in enumerate(fts_results, start=1):
        mid = r.memory.id
        rrf_scores[mid] += 1.0 / (RRF_K + rank)
        all_memories[mid] = r

    for rank, r in enumerate(vec_results, start=1):
        mid = r.memory.id
        rrf_scores[mid] += 1.0 / (RRF_K + rank)
        if mid not in all_memories:
            all_memories[mid] = r

    # Sort by RRF score descending
    sorted_ids = sorted(rrf_scores.keys(), key=lambda mid: rrf_scores[mid], reverse=True)
    merged = []
    for mid in sorted_ids[:limit]:
        r = all_memories[mid]
        merged.append(MemorySearchResult(memory=r.memory, score=rrf_scores[mid]))

    return _bump_access(conn, merged)


def fts5_search(
    conn: sqlite3.Connection,
    query: str,
    project: str | None = None,
    memory_type: str | None = None,
    limit: int = 20,
) -> list[MemorySearchResult]:
    """Search memories using FTS5 with BM25 ranking (standalone, no vector)."""
    if not query.strip():
        return _recent_memories(conn, project, memory_type, limit)

    results = _fts5_ranked(conn, query, project, memory_type, limit)
    return _bump_access(conn, results)


# --- Internal rankers ---

def _fts5_ranked(
    conn: sqlite3.Connection,
    query: str,
    project: str | None,
    memory_type: str | None,
    limit: int,
) -> list[MemorySearchResult]:
    """Run FTS5 BM25 search and return ranked results."""
    fts_query = _build_fts_query(query)

    sql = """
        SELECT m.*, -rank as score
        FROM memories_fts fts
        JOIN memories m ON m.id = fts.rowid
        WHERE memories_fts MATCH ?
          AND m.superseded_by IS NULL
    """
    params: list = [fts_query]

    if project:
        sql += " AND m.project = ?"
        params.append(project)
    if memory_type:
        sql += " AND m.memory_type = ?"
        params.append(memory_type)

    sql += " ORDER BY score DESC LIMIT ?"
    params.append(limit)

    try:
        rows = conn.execute(sql, params).fetchall()
    except Exception:
        return []

    results = []
    for row in rows:
        mem = _row_to_memory(row)
        score = dict(row).get("score", 0.0)
        results.append(MemorySearchResult(memory=mem, score=score))
    return results


def _vec_ranked(
    conn: sqlite3.Connection,
    query: str,
    embedder,
    project: str | None,
    memory_type: str | None,
    limit: int,
) -> list[MemorySearchResult]:
    """Run vector cosine search via sqlite-vec."""
    try:
        vec = embedder.embed_single(query)
        blob = np.array(vec, dtype=np.float32).tobytes()
    except Exception as e:
        logger.debug("Vector embedding failed: %s", e)
        return []

    # Fetch more than limit to allow for post-filtering
    fetch_limit = limit * 3

    try:
        vec_rows = conn.execute(
            """SELECT memory_id, distance
               FROM memories_vec
               WHERE embedding MATCH ?
               ORDER BY distance
               LIMIT ?""",
            (blob, fetch_limit),
        ).fetchall()
    except Exception as e:
        logger.debug("Vec search failed: %s", e)
        return []

    results = []
    for vr in vec_rows:
        mem_row = conn.execute(
            "SELECT * FROM memories WHERE id = ? AND superseded_by IS NULL",
            (vr["memory_id"],),
        ).fetchone()
        if not mem_row:
            continue

        mem = _row_to_memory(mem_row)

        # Post-filter by project/type
        if project and mem.project != project:
            continue
        if memory_type and mem.memory_type.value != memory_type:
            continue

        # Convert distance to a similarity-like score (lower distance = better)
        results.append(MemorySearchResult(memory=mem, score=1.0 - vr["distance"]))
        if len(results) >= limit:
            break

    return results


# --- Helpers ---

def _bump_access(
    conn: sqlite3.Connection,
    results: list[MemorySearchResult],
) -> list[MemorySearchResult]:
    """Bump access counts for returned results."""
    if results:
        ids = [r.memory.id for r in results if r.memory.id]
        if ids:
            placeholders = ",".join("?" * len(ids))
            conn.execute(
                f"UPDATE memories SET access_count = access_count + 1 WHERE id IN ({placeholders})",
                ids,
            )
            conn.commit()
    return results


def _recent_memories(
    conn: sqlite3.Connection,
    project: str | None = None,
    memory_type: str | None = None,
    limit: int = 20,
) -> list[MemorySearchResult]:
    """Fall back to recent memories when no query provided."""
    sql = "SELECT * FROM memories WHERE superseded_by IS NULL"
    params: list = []

    if project:
        sql += " AND project = ?"
        params.append(project)
    if memory_type:
        sql += " AND memory_type = ?"
        params.append(memory_type)

    sql += " ORDER BY importance DESC, created_at DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    return [MemorySearchResult(memory=_row_to_memory(row), score=0.0) for row in rows]


def _build_fts_query(query: str) -> str:
    """Build an FTS5 query from natural language input."""
    special = set('*"(){}[]^~:-')
    tokens = []
    for word in query.split():
        cleaned = "".join(c if c not in special else " " for c in word)
        for part in cleaned.split():
            if part and part.upper() not in ("AND", "OR", "NOT", "NEAR"):
                tokens.append(f'"{part}"')

    if not tokens:
        return f'"{query}"'

    if len(tokens) == 1:
        bare = tokens[0].strip('"')
        return f'"{bare}"*'
    last_bare = tokens[-1].strip('"')
    return " ".join(tokens[:-1]) + f' "{last_bare}"*'
