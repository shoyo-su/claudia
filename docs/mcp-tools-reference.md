# MCP Tools Reference

Full API reference for Central Brain's 7 MCP tools.

---

## `remember`

Store a memory in the central brain. Automatically deduplicates against existing memories using FTS5 fuzzy matching, word overlap (>50%), and vector distance (<0.15). If a duplicate is found, the existing memory's importance is bumped if the new one is higher.

### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `content` | `str` | Yes | — | The memory content — what should be remembered |
| `memory_type` | `str` | No | `"insight"` | One of: `insight`, `decision`, `pattern`, `error`, `preference`, `todo`, `open_loop` |
| `project` | `str \| null` | No | `null` | Project name/path this memory relates to |
| `tags` | `list[str] \| null` | No | `null` | Keywords for categorization |
| `importance` | `int` | No | `3` | 1-5 score (5 = critical) |
| `session_id` | `str \| null` | No | `null` | Session this memory came from. If provided, source is set to `"session"`; otherwise `"manual"` |

### Response

```json
{
  "status": "stored",
  "id": 42,
  "content": "User prefers snake_case for Python variables",
  "memory_type": "preference"
}
```

### Notes

- When a duplicate is detected, the existing memory is returned instead of creating a new one.
- Embeddings are automatically generated and stored if `VOYAGE_API_KEY` is available.

---

## `recall`

Search memories using hybrid FTS5 + vector search with Reciprocal Rank Fusion (RRF). When the query is empty, returns recent memories sorted by importance.

### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `query` | `str` | No | `""` | Natural language search query. Empty returns recent memories. |
| `project` | `str \| null` | No | `null` | Filter results to a specific project |
| `memory_type` | `str \| null` | No | `null` | Filter by type (`insight`, `decision`, `pattern`, `error`, `preference`, `todo`, `open_loop`) |
| `limit` | `int` | No | `10` | Maximum number of results to return |

### Response

```json
[
  {
    "id": 42,
    "content": "User prefers snake_case for Python variables",
    "memory_type": "preference",
    "project": "my-project",
    "tags": ["python", "style"],
    "importance": 4,
    "score": 0.0328,
    "created_at": "2026-03-10T14:30:00+00:00"
  }
]
```

### Notes

- `score` is the RRF fusion score when both FTS5 and vector results are available, or the BM25 score for FTS5-only.
- Access counts are bumped for all returned memories.
- With an empty query, results are ordered by `importance DESC, created_at DESC`.

---

## `forget`

Supersede or permanently delete a memory.

### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `memory_id` | `int` | Yes | — | ID of the memory to forget |
| `superseded_by` | `int \| null` | No | `null` | If provided, marks the memory as superseded by this ID (soft delete). Otherwise, hard deletes. |

### Response

**When superseded:**
```json
{
  "status": "superseded",
  "id": 42,
  "superseded_by": 43
}
```

**When deleted:**
```json
{
  "status": "deleted",
  "id": 42
}
```

**When not found:**
```json
{
  "status": "not_found",
  "id": 999
}
```

### Notes

- Superseded memories are excluded from search results but remain in the database.
- Prefer superseding over deleting when a memory is being replaced by an updated version.

---

## `get_memory_by_id`

Fetch a specific memory by its ID. Returns all fields including metadata.

### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `memory_id` | `int` | Yes | — | The memory ID to retrieve |

### Response

```json
{
  "id": 42,
  "content": "User prefers snake_case for Python variables",
  "memory_type": "preference",
  "source": "manual",
  "project": "my-project",
  "tags": ["python", "style"],
  "importance": 4,
  "access_count": 7,
  "created_at": "2026-03-10T14:30:00+00:00",
  "updated_at": "2026-03-12T09:15:00+00:00",
  "superseded_by": null,
  "metadata": {}
}
```

**When not found:**
```json
{
  "error": "not_found",
  "id": 999
}
```

### Notes

- This call increments the memory's `access_count`.

---

## `update_memory_tool`

Update an existing memory's content, tags, or importance. Only the provided fields are updated; others remain unchanged.

### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `memory_id` | `int` | Yes | — | The memory ID to update |
| `content` | `str \| null` | No | `null` | New content text |
| `tags` | `list[str] \| null` | No | `null` | New tags list (replaces existing) |
| `importance` | `int \| null` | No | `null` | New importance score (1-5) |

### Response

```json
{
  "status": "updated",
  "id": 42,
  "content": "User prefers snake_case for all Python identifiers",
  "tags": ["python", "style", "naming"],
  "importance": 5
}
```

**When not found:**
```json
{
  "error": "not_found",
  "id": 999
}
```

### Notes

- If `content` is updated, the embedding is automatically regenerated.
- The `updated_at` timestamp is refreshed on any update.
- This call also increments `access_count` (via the internal `get_memory` call).

---

## `list_recent_sessions`

List recent Claude Code sessions with their summaries and memory counts.

### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `limit` | `int` | No | `10` | Maximum number of sessions to return |

### Response

```json
[
  {
    "session_id": "abc-123-def",
    "project": "central-brain",
    "started_at": "2026-03-12T10:00:00+00:00",
    "ended_at": "2026-03-12T11:30:00+00:00",
    "summary": null,
    "memory_count": 5
  }
]
```

### Notes

- Sessions are ordered by `started_at DESC`.
- Sessions with `ended_at = null` may indicate the session is still active or the stop hook didn't fire (e.g., ghost sessions from background extraction subprocesses).

---

## `brain_stats`

Get statistics about the central brain — total memory counts, breakdown by type, most accessed, and recent additions.

### Parameters

None.

### Response

```json
{
  "total_memories": 142,
  "by_type": {
    "insight": 45,
    "decision": 28,
    "pattern": 32,
    "error": 15,
    "preference": 12,
    "todo": 6,
    "open_loop": 4
  },
  "total_sessions": 37,
  "most_accessed": [
    {
      "id": 12,
      "content": "Affiliates use a single hardcoded campaign 'affiliate-referral-v1'...",
      "access_count": 23
    }
  ],
  "recent": [
    {
      "id": 142,
      "content": "Central Brain Phase 2 is complete...",
      "memory_type": "decision",
      "created_at": "2026-03-12T11:30:00+00:00"
    }
  ]
}
```

### Notes

- Only non-superseded memories are counted.
- `most_accessed` returns up to 5 memories, `recent` returns up to 10.
- Content in `most_accessed` and `recent` is truncated to 100 characters.
