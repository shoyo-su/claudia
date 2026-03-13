# Architecture

Deep internals reference for Central Brain.

---

## Module Map

```
central_brain/
├── cli.py          ── CLI entrypoint, subcommand dispatch, hook handlers
├── server.py       ── FastMCP server, 7 tool definitions, lazy-init singletons
├── db.py           ── SQLite + FTS5 + sqlite-vec, CRUD, dedup, schema migrations
├── search.py       ── Hybrid FTS5/vector search, RRF fusion, access tracking
├── extract.py      ── Transcript parser, LLM write gate, code intelligence integration
├── embedder.py     ── VoyageAI wrapper, graceful degradation
├── models.py       ── Pydantic models: Memory, Session, MemoryType, MemorySource
└── code_intel.py   ── Tree-sitter Python parsing, symbol extraction, code metadata
```

**Dependency flow:**

```
cli.py ──> server.py ──> db.py
  |            |           ^
  |            |           |
  |            +------> search.py
  |            |
  |            +------> embedder.py
  |
  +--------> extract.py ──> code_intel.py
  |              |
  |              +--------> models.py
  |
  +--------> db.py
  +--------> search.py
  +--------> embedder.py
  +--------> models.py
```

---

## Database Schema

Schema version: **2** (tracked in `schema_version` table).

### `memories` table

```sql
CREATE TABLE memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    memory_type TEXT NOT NULL DEFAULT 'insight',
    source TEXT NOT NULL DEFAULT 'manual',
    session_id TEXT,
    project TEXT,
    tags TEXT NOT NULL DEFAULT '[]',           -- JSON array
    importance INTEGER NOT NULL DEFAULT 3,
    created_at TEXT NOT NULL,                  -- ISO 8601 UTC
    updated_at TEXT NOT NULL,                  -- ISO 8601 UTC
    access_count INTEGER NOT NULL DEFAULT 0,
    superseded_by INTEGER REFERENCES memories(id),
    metadata TEXT NOT NULL DEFAULT '{}',       -- JSON object
    embedding BLOB                            -- float32[1024], added in v2
);
```

### `sessions` table

```sql
CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    project TEXT,
    started_at TEXT,
    ended_at TEXT,
    summary TEXT,
    transcript_path TEXT,
    memory_count INTEGER NOT NULL DEFAULT 0
);
```

### `memories_fts` (FTS5 virtual table)

```sql
CREATE VIRTUAL TABLE memories_fts USING fts5(
    content,
    tags,
    project,
    memory_type,
    content='memories',
    content_rowid='id',
    tokenize='porter unicode61'
);
```

Kept in sync with `memories` via three triggers:
- `memories_ai` — AFTER INSERT
- `memories_ad` — AFTER DELETE
- `memories_au` — AFTER UPDATE (delete + re-insert)

### `memories_vec` (sqlite-vec virtual table)

```sql
CREATE VIRTUAL TABLE memories_vec USING vec0(
    memory_id INTEGER PRIMARY KEY,
    embedding float[1024]
);
```

Created by `_ensure_vec_table()` — silently skipped if sqlite-vec is not loaded.

### `schema_version` table

```sql
CREATE TABLE schema_version (
    version INTEGER PRIMARY KEY
);
```

### Indexes

```sql
CREATE INDEX idx_memories_project ON memories(project);
CREATE INDEX idx_memories_type ON memories(memory_type);
CREATE INDEX idx_memories_importance ON memories(importance DESC);
CREATE INDEX idx_memories_superseded ON memories(superseded_by);
CREATE INDEX idx_memories_session ON memories(session_id);
```

---

## Search Architecture

Search uses a two-stage pipeline with Reciprocal Rank Fusion (RRF) to merge results from FTS5 and vector search.

### Stage 1: Parallel Retrieval

**FTS5 BM25 Search:**
- Query is cleaned: special characters stripped, reserved words (`AND`, `OR`, `NOT`, `NEAR`) excluded
- Each token is quoted; the last token gets a prefix wildcard (`"token"*`)
- Results ranked by BM25 score (negated FTS5 `rank` column)
- Filters: `superseded_by IS NULL`, optional `project` and `memory_type`

**Vector Cosine Search (when embedder available):**
- Query embedded via VoyageAI `voyage-3.5` (1024 dimensions)
- `memories_vec` searched with `embedding MATCH` (cosine distance)
- Fetches `3 * limit` candidates, post-filters by project/type
- Distance converted to similarity: `score = 1.0 - distance`

### Stage 2: RRF Fusion

When both FTS5 and vector results exist:

```
RRF_score(memory) = sum(1 / (K + rank_i)) for each result list i
```

Where `K = 60` (standard RRF constant). Results are sorted by RRF score descending.

### Fallback Chain

1. FTS5 + vector with RRF fusion (full capability)
2. FTS5 only (no `VOYAGE_API_KEY` or embedding failure)
3. Recent memories by importance (empty query)

### Access Count Bumping

All memories returned by `recall`, `fts5_search`, `get_memory_by_id`, and `update_memory` have their `access_count` incremented. This enables the "most accessed" view in `brain_stats`.

---

## Deduplication

Three-tier strategy applied on every `insert_memory` call:

### Tier 1: FTS5 Fuzzy Match

- Takes the first 8 words of the new memory's content
- Quotes each word (>2 chars) for FTS5 matching
- Filters to same `memory_type` and `superseded_by IS NULL`
- Returns up to 5 candidates

### Tier 2: Word Overlap

For each FTS5 candidate:
- Computes Jaccard-like overlap: `|intersection| / |union|`
- Threshold: **>0.5** (more than 50% word overlap)
- If matched, returns the existing memory (dedup hit)

### Tier 3: Vector Distance

If word overlap didn't match and an embedder is available:
- Embeds the new memory's content
- Queries `memories_vec` for the 3 nearest neighbors
- Threshold: **distance < 0.15**
- Checks same `memory_type` and not superseded

When a duplicate is found, the existing memory's importance is bumped (if the new one is higher) and its access count is incremented.

---

## Memory Lifecycle

```
                    insert_memory()
                         |
                   [Dedup check]
                    /          \
              duplicate       new
                |               |
          bump importance   INSERT into memories
          bump access_count     |
          return existing   embed_and_store()
                                |
                           INSERT into memories_vec
                                |
                           return new memory
                                |
                    +-----------+-----------+
                    |           |           |
               recall()    update()    forget()
                    |           |           |
              bump access   re-embed    supersede or
                            if content  hard delete
                            changed
```

---

## Extraction Pipeline

Triggered by the Stop hook (background) and PreCompact hook (inline).

### Steps

1. **Parse transcript** — Read JSONL file, extract `user` and `assistant` messages, skip tool results
2. **Truncate** — Keep last 80,000 characters if transcript exceeds limit
3. **Code intelligence** — Extract Python code blocks (fenced `` ```python `` and heuristic-detected), parse with tree-sitter, build symbol summaries
4. **LLM write gate** — Send transcript + code summary to `claude --print` with extraction prompt
5. **Parse response** — Extract JSON array from LLM output
6. **Filter** — Discard memories with importance < 3
7. **Store** — Insert each memory with auto-dedup and auto-embedding
8. **Update session** — Set `ended_at`, update `memory_count`

### Environment Handling

The extraction subprocess:
- Unsets `CLAUDECODE` to avoid nested session detection
- Sets `CENTRAL_BRAIN_STOP_HOOK_ACTIVE=1` so child hooks skip extraction
- Runs with `timeout=120` seconds

### Importance Scoring Guide (from extraction prompt)

| Score | Meaning | Action |
|-------|---------|--------|
| 5 | Critical decision or error that MUST inform future sessions | Store |
| 4 | Important pattern or preference that changes behavior | Store |
| 3 | Useful context that might help future sessions | Store |
| 2 | Minor detail | Exclude |
| 1 | Trivial | Exclude |

---

## Concurrency & Safety

### WAL Mode

SQLite is configured with Write-Ahead Logging:
```sql
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
PRAGMA foreign_keys=ON;
```

WAL allows concurrent reads while a write is in progress. `busy_timeout=5000` (5 seconds) causes writers to retry rather than immediately fail on lock contention.

### Re-entrancy Guards

The stop hook spawns a `claude --print` subprocess for extraction. That subprocess could trigger its own stop hook, creating infinite recursion. Two guards prevent this:

1. **Environment variable** — `CENTRAL_BRAIN_STOP_HOOK_ACTIVE` is set in the extraction subprocess's environment
2. **Hook input field** — `stop_hook_active` in the hook's stdin JSON (set by Claude Code when the stop hook itself triggered the invocation)

### Background Extraction

The stop hook forks extraction into a detached subprocess (`start_new_session=True` via `subprocess.Popen`) so it survives Claude Code session exit. The detached process:
- Has no stdin/stdout ties to the parent
- Logs to `~/.central-brain/extract.log`
- Is invoked via `central-brain extract-async` CLI command

---

## Key Constants

| Constant | Value | Source |
|----------|-------|--------|
| RRF K | 60 | `search.py` |
| Vector dimensions | `float[1024]` | `embedder.py` |
| Embedding model | `voyage-3.5` | `embedder.py` |
| Word overlap threshold | >0.5 | `db.py` |
| Vector dedup threshold | <0.15 | `db.py` |
| FTS5 tokenizer | `porter unicode61` | `db.py` |
| Max transcript chars | 80,000 | `extract.py` |
| Extraction timeout | 120s | `extract.py` |
| Importance filter | >=3 | `extract.py` |
| DB path | `~/.central-brain/memory.db` | `db.py` |
| Log path | `~/.central-brain/extract.log` | `cli.py` |
| Schema version | 2 | `db.py` |
| SQLite busy_timeout | 5000ms | `db.py` |
| Max backfill batch size | 128 | `cli.py` |
| Max code summary chars | 2000 | `code_intel.py` |
| Max symbols in summary | 50 | `code_intel.py` |
