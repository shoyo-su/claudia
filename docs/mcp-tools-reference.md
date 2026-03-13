# MCP Tools Reference

These are the 7 tools available to Claude during a session. Most of the time, memory extraction and injection happen automatically via hooks. These tools are for when Claude (or you) want to explicitly interact with the memory system — saving something important mid-session, searching for past context, or managing existing memories.

---

## `remember`

Store a memory. This is what Claude calls when it wants to explicitly save something — a decision, a preference correction, an error root cause.

Automatically deduplicates: if a substantially similar memory already exists (same type, >50% word overlap or vector distance <0.15), the existing memory is returned instead and its importance is bumped if the new one scored higher.

### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `content` | `str` | Yes | — | What should be remembered |
| `memory_type` | `str` | No | `"insight"` | One of: `insight`, `decision`, `pattern`, `error`, `preference`, `todo`, `open_loop` |
| `project` | `str \| null` | No | `null` | Project this relates to (e.g., "my-api") |
| `tags` | `list[str] \| null` | No | `null` | Keywords for categorization |
| `importance` | `int` | No | `3` | 1 (trivial) to 5 (critical) |
| `session_id` | `str \| null` | No | `null` | Links the memory to a session. Sets source to `"session"` instead of `"manual"`. |

### Example Response

```json
{
  "status": "stored",
  "id": 42,
  "content": "The payment webhook handler needs exponential backoff on 429 responses from Stripe",
  "memory_type": "error"
}
```

If a duplicate was found, the existing memory is enriched (tags unioned, importance bumped, metadata merged) and returned — no new row is created. Note: the `remember` tool uses `llm_merge=False`, so it applies deterministic merging (union tags, max importance, merge metadata) but does not call the LLM to merge content. This avoids blocking the tool response on a background LLM call. Auto-extracted memories from hooks use full LLM-powered merging — see [Architecture: Deduplication & Enrichment](architecture.md#deduplication--enrichment) for details.

---

## `recall`

Search for memories. Uses hybrid FTS5 + vector search with Reciprocal Rank Fusion when VoyageAI is configured, FTS5-only otherwise. With an empty query, returns recent memories sorted by importance.

This is useful when Claude wants to check if something was discussed before, or when you ask "have we dealt with this pattern before?"

### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `query` | `str` | No | `""` | Natural language search. Empty = return recent memories. |
| `project` | `str \| null` | No | `null` | Filter to a specific project |
| `memory_type` | `str \| null` | No | `null` | Filter by type |
| `limit` | `int` | No | `10` | Max results |

### Example Response

```json
[
  {
    "id": 42,
    "content": "The payment webhook handler needs exponential backoff on 429 responses from Stripe",
    "memory_type": "error",
    "project": "my-api",
    "tags": ["stripe", "webhooks", "retry"],
    "importance": 5,
    "score": 0.0328,
    "created_at": "2026-03-10T14:30:00+00:00"
  }
]
```

`score` is the RRF fusion score (or BM25 score for FTS5-only). Higher means more relevant. Every returned memory gets its `access_count` bumped.

---

## `forget`

Remove a memory. Two modes:

- **Supersede** (soft delete) — Pass `superseded_by` to mark the memory as replaced by a newer one. The old memory stays in the database but is excluded from search. Use this when a memory is outdated.
- **Delete** (hard delete) — Omit `superseded_by` to permanently remove it. Use this when a memory is just wrong.

### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `memory_id` | `int` | Yes | — | ID of the memory to remove |
| `superseded_by` | `int \| null` | No | `null` | ID of the replacement memory (soft delete) |

### Example Responses

```json
{"status": "superseded", "id": 42, "superseded_by": 43}
```

```json
{"status": "deleted", "id": 42}
```

```json
{"status": "not_found", "id": 999}
```

---

## `get_memory_by_id`

Fetch a specific memory with all its fields — including metadata (code intelligence data), access count, and superseded status. Useful for inspecting a memory before updating or superseding it.

### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `memory_id` | `int` | Yes | — | The memory ID |

### Example Response

```json
{
  "id": 42,
  "content": "The payment webhook handler needs exponential backoff on 429 responses from Stripe",
  "memory_type": "error",
  "source": "session",
  "project": "my-api",
  "tags": ["stripe", "webhooks", "retry"],
  "importance": 5,
  "access_count": 12,
  "created_at": "2026-03-10T14:30:00+00:00",
  "updated_at": "2026-03-12T09:15:00+00:00",
  "superseded_by": null,
  "metadata": {
    "code_intel": {
      "functions": ["handle_webhook", "process_payment_event"],
      "classes": [],
      "imports": ["stripe"],
      "language": "python"
    }
  }
}
```

This call increments `access_count`.

---

## `update_memory_tool`

Update a memory's content, tags, or importance. Only the fields you pass are changed — omitted fields stay as they are. If content is updated, the embedding is automatically regenerated.

Useful when Claude refines its understanding — "actually, the issue wasn't just 429s, it was also 503s" — and wants to update the memory rather than creating a new one.

### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `memory_id` | `int` | Yes | — | The memory ID |
| `content` | `str \| null` | No | `null` | New content text |
| `tags` | `list[str] \| null` | No | `null` | New tags (replaces existing) |
| `importance` | `int \| null` | No | `null` | New importance (1-5) |

### Example Response

```json
{
  "status": "updated",
  "id": 42,
  "content": "The payment webhook handler needs exponential backoff on 429 and 503 responses from Stripe",
  "tags": ["stripe", "webhooks", "retry", "error-handling"],
  "importance": 5
}
```

---

## `list_recent_sessions`

List recent Claude Code sessions. Shows when each session ran, which project it was in, and how many memories were extracted. Useful for understanding extraction activity or debugging missing memories.

### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `limit` | `int` | No | `10` | Max sessions |

### Example Response

```json
[
  {
    "session_id": "abc-123-def",
    "project": "my-api",
    "started_at": "2026-03-12T10:00:00+00:00",
    "ended_at": "2026-03-12T11:30:00+00:00",
    "summary": null,
    "memory_count": 5
  }
]
```

Sessions with `ended_at = null` are either still active or are ghost sessions created by the background extraction subprocess (harmless).

---

## `brain_stats`

A dashboard view of the memory system. Shows total memory count broken down by type, the most frequently accessed memories, and the most recent additions.

### Parameters

None.

### Example Response

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
      "content": "Never use time.sleep() in async handlers — blocks the event loop...",
      "access_count": 23
    }
  ],
  "recent": [
    {
      "id": 142,
      "content": "Chose Celery over RQ because we need task chaining for payment...",
      "memory_type": "decision",
      "created_at": "2026-03-12T11:30:00+00:00"
    }
  ]
}
```

Only non-superseded memories are counted. `most_accessed` returns up to 5, `recent` up to 10. Content is truncated to 100 characters.
