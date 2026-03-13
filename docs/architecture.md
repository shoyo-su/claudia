# Architecture

This document explains *how* Central Brain works and *why* it's built the way it is.

---

## Overview

Central Brain is 8 Python modules, a SQLite database, and no external services (VoyageAI is optional). The design priorities are:

1. **Zero-config by default** — Works with just `pip install` and a SQLite file. No Redis, no Postgres, no Docker.
2. **Graceful degradation** — Every optional capability (vector search, code intelligence, embeddings) fails silently and falls back to simpler methods.
3. **Local-first** — All data stays on your machine. The only network call is to VoyageAI for embeddings, and that's opt-in.

## Modules

```
src/central_brain/
├── cli.py        ── Entry point. Dispatches subcommands and hook handlers.
├── server.py     ── FastMCP server. Defines the 7 tools Claude sees.
├── db.py         ── All SQLite operations: CRUD, dedup, schema, migrations.
├── search.py     ── Hybrid search: FTS5 + vector + RRF fusion.
├── extract.py    ── Reads transcripts, calls LLM write gate, stores memories.
├── embedder.py   ── VoyageAI wrapper. Returns None if unavailable.
├── models.py     ── Pydantic data models: Memory, Session, MemoryType.
└── code_intel.py ── Tree-sitter parser. Extracts Python symbols from code blocks.
```

**How they connect:**

`cli.py` is the only entry point — it either starts the MCP server (which loads `server.py`) or handles a hook/CLI command directly. The server and hook handlers both use `db.py` for storage and `search.py` for retrieval. `extract.py` orchestrates the memory extraction pipeline, pulling in `code_intel.py` for code parsing. `embedder.py` is used wherever embeddings are needed (insert, update, search) and is always accessed through a `get_embedder()` factory that returns `None` when VoyageAI is unavailable.

Every module that depends on an optional capability wraps it in a try/except at import time and exposes a boolean flag or factory function. This pattern means a missing `VOYAGE_API_KEY` or uninstalled `tree-sitter` never causes an import error.

---

## Database

Central Brain uses a single SQLite file at `~/.central-brain/memory.db`. The choice of SQLite was deliberate — it's zero-config, ships with Python, handles concurrent reads via WAL mode, and with the `sqlite-vec` extension, supports vector similarity search.

### Schema (version 2)

**`memories`** — The core table. Each row is one memory.

```sql
CREATE TABLE memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,                         -- The actual memory text
    memory_type TEXT NOT NULL DEFAULT 'insight',   -- insight/decision/pattern/error/preference/todo/open_loop
    source TEXT NOT NULL DEFAULT 'manual',         -- 'manual' (via MCP tool) or 'session' (auto-extracted)
    session_id TEXT,                               -- Which session this came from (if auto-extracted)
    project TEXT,                                  -- Project scope (derived from cwd directory name)
    tags TEXT NOT NULL DEFAULT '[]',               -- JSON array of keyword tags
    importance INTEGER NOT NULL DEFAULT 3,         -- 1-5, higher = more critical
    created_at TEXT NOT NULL,                      -- ISO 8601 UTC
    updated_at TEXT NOT NULL,                      -- ISO 8601 UTC
    access_count INTEGER NOT NULL DEFAULT 0,       -- Bumped every time this memory is returned in a search
    superseded_by INTEGER REFERENCES memories(id), -- Soft delete: points to the newer version
    metadata TEXT NOT NULL DEFAULT '{}',           -- JSON object (holds code_intel data, etc.)
    embedding BLOB                                 -- float32[1024] vector, NULL if embedder unavailable
);
```

**Why `access_count`?** It surfaces the most-used memories in `brain_stats`, giving you a sense of which memories Claude actually relies on. It also acts as a natural relevance signal — frequently accessed memories are probably important.

**Why `superseded_by` instead of just deleting?** When a memory is outdated (say, a decision was reversed), you want to mark it as replaced rather than destroy the audit trail. Superseded memories are excluded from search results but remain in the database.

**`sessions`** — Tracks Claude Code sessions for the `list_recent_sessions` tool.

```sql
CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    project TEXT,
    started_at TEXT,
    ended_at TEXT,
    summary TEXT,
    transcript_path TEXT,
    memory_count INTEGER NOT NULL DEFAULT 0  -- How many memories were extracted from this session
);
```

**`memories_fts`** — FTS5 virtual table for full-text search. Uses porter stemming so "retrying" matches "retry", and unicode61 tokenizer for non-ASCII support. Synced with `memories` via three triggers (insert, delete, update).

```sql
CREATE VIRTUAL TABLE memories_fts USING fts5(
    content, tags, project, memory_type,
    content='memories', content_rowid='id',
    tokenize='porter unicode61'
);
```

**`memories_vec`** — sqlite-vec virtual table for vector similarity. Created only if the sqlite-vec extension is loaded. Stores 1024-dimensional float32 vectors (VoyageAI `voyage-3.5` output).

```sql
CREATE VIRTUAL TABLE memories_vec USING vec0(
    memory_id INTEGER PRIMARY KEY,
    embedding float[1024]
);
```

**Indexes** — On `project`, `memory_type`, `importance DESC`, `superseded_by`, and `session_id`. These cover the filter clauses used in search and the SessionStart hook's high-importance query.

### Migrations

Schema version is tracked in a `schema_version` table. `init_db()` checks the current version and runs migrations sequentially. Version 2 added the `embedding` BLOB column and `memories_vec` table. Migrations handle the case where sqlite-vec isn't loaded — `_ensure_vec_table()` is called on every `init_db()` so the vector table gets created if you install sqlite-vec later.

---

## Search

Search is the most complex subsystem. The goal: find memories that are relevant to a query, even when the query and the memory use different words to describe the same thing.

### Why hybrid search?

Full-text search (FTS5) is great at exact keyword matches. If you search "webhook retry", it finds memories containing "webhook" and "retry". But it misses "429 backoff logic" — same concept, different words.

Vector search (cosine similarity on embeddings) catches semantic matches. "webhook retry" and "429 backoff logic" end up close together in embedding space. But it can miss exact matches that FTS5 would rank highly.

Combining both catches more relevant results than either alone.

### How it works

1. **FTS5 retrieval** — The query is tokenized, special characters stripped, FTS5 reserved words excluded. Each token is quoted for exact matching. The last token gets a prefix wildcard (`"retry"*` matches "retrying", "retries"). Results are ranked by BM25.

2. **Vector retrieval** (if embedder available) — The query is embedded via VoyageAI. The `memories_vec` table is searched by cosine distance. 3x more candidates than the limit are fetched to allow for post-filtering by project/type.

3. **RRF fusion** — Both result lists are merged using Reciprocal Rank Fusion. For each memory that appears in either list, its RRF score is:
   ```
   score = sum(1 / (60 + rank_in_list)) across all lists it appears in
   ```
   K=60 is the standard RRF constant. A memory that ranks #1 in both lists gets the highest score. A memory that ranks #1 in one and doesn't appear in the other still scores well.

4. **Fallback** — If no embedder is available, FTS5 results are returned directly. If the query is empty, recent memories are returned sorted by importance.

### Access tracking

Every memory returned by a search gets its `access_count` bumped. This is a deliberate side effect — it means `brain_stats` can show which memories Claude actually uses, not just which ones exist.

---

## Deduplication & Enrichment

Sessions often produce similar memories. Without dedup, you'd accumulate near-identical entries. But simple dedup (discard the new one) loses information — the new memory might add details the original didn't have.

Central Brain solves this with a two-phase approach: **detect** duplicates, then **enrich** the existing memory with information from the new one.

### Phase 1: Duplicate Detection

Every `insert_memory` call runs a 3-tier check:

**Tier 1 — FTS5 candidate generation.** Take the first 8 words of the new memory, search for them in the FTS5 index, filtered to the same `memory_type` and not superseded. This is cheap and narrows the search space to at most 5 candidates.

**Tier 2 — Word overlap.** For each candidate, compute `|words_in_common| / |all_unique_words|`. If this exceeds 0.5 (50% overlap), it's a duplicate. This catches near-verbatim duplicates and minor rephrasing.

**Tier 3 — Vector distance.** If word overlap didn't catch it and an embedder is available, embed the new memory and check the 3 nearest vectors. If any has distance < 0.15, it's a semantic duplicate — same meaning, completely different words.

### Phase 2: LLM-Powered Enrichment

When a duplicate is found, `_enrich_memory()` merges the two memories intelligently instead of just discarding the new one:

1. **Tags** — Union of both tag sets
2. **Importance** — Max of both scores
3. **Metadata** — Deep merge via `_merge_metadata()`. For `code_intel`, inner lists (functions, classes, imports) are unioned with deduplication. Other keys use new-overrides-old.
4. **Content** — The core of the enrichment. Calls `merge_or_separate()` in `extract.py`, which sends both memories to `claude --print`:

The LLM is asked: "Do these describe the same thing, or are they distinct?" It responds with:
- `{"action": "merge", "content": "...merged text..."}` — The two overlap. The merged text preserves all unique details from both. Example: "webhook drops 429s" + "webhook also drops 503s" → "webhook drops 429 and 503 responses — needs backoff for both".
- `{"action": "separate"}` — They're actually distinct despite textual similarity. `_enrich_memory` returns `None`, and `insert_memory` falls through to create a new row.

**Safeguards:**
- **Enrichment cap** — `metadata.enrichment_count` tracks merges. After 5, LLM merge is skipped (prevents memories from growing endlessly).
- **Content size cap** — If existing content exceeds 1000 characters, LLM merge is skipped.
- **Timeout** — `merge_or_separate` has a 30-second timeout, much shorter than extraction's 120s.
- **Graceful fallback** — If the LLM call fails, the deterministic merge still applies (bump importance, union tags, merge metadata, leave content unchanged).
- **`llm_merge` flag** — The `remember` MCP tool passes `llm_merge=False` so explicit tool calls don't block on a background LLM call. Auto-extracted memories from hooks use `llm_merge=True`.

### Access tracking

After enrichment, the existing memory's `access_count` is bumped. This reflects that the topic kept coming up, even though no new row was created.

---

## Extraction Pipeline

This is the system that makes Central Brain zero-effort — you don't have to explicitly save memories.

### Triggers

Extraction runs at two points:
- **PreCompact hook** — Runs inline (blocking) before Claude Code compacts the context. The session is still active, so it blocks for 30-120 seconds.
- **SessionEnd hook** — Runs as a detached background process so the session can exit immediately. Logs to `~/.central-brain/extract.log`.

### Steps

1. **Parse transcript** — Read the JSONL transcript file, extract user and assistant messages, skip tool results (they're noisy and large).

2. **Truncate** — If the transcript exceeds 80,000 characters, keep only the most recent portion. Earlier conversation is less relevant.

3. **Code intelligence** — Find Python code blocks in the transcript (fenced `` ```python `` blocks and untagged blocks detected via heuristics). Parse each with tree-sitter to extract function signatures, class hierarchies, and imports. Produce a one-line-per-symbol summary (capped at 2000 chars, 50 symbols).

4. **LLM write gate** — Send the transcript and code summary to `claude --print` with a detailed extraction prompt. The prompt asks the LLM to produce a JSON array of memories, each with content, type, tags, and importance.

5. **Filter** — Discard anything with importance < 3. This is the quality gate — routine observations (importance 1-2) are dropped, only genuinely useful context passes through.

6. **Store** — Insert each surviving memory with auto-dedup and auto-embedding. Duplicates are absorbed, not duplicated.

7. **Update session** — Mark the session as ended, record how many memories were extracted.

### The Write Gate Prompt

The extraction prompt is carefully designed to produce useful memories:
- **Focus on "why"** — Decisions should include rationale, not just the choice
- **Focus on root causes** — Errors should include what actually went wrong, not just symptoms
- **Include code context** — When code structure is detected, reference specific function/class names
- **Exclude noise** — Skip routine file reads, simple edits, info already in git history

### Importance Scoring

| Score | Meaning | Example |
|-------|---------|---------|
| 5 | Critical — must inform future sessions | "Never run migrations without backup — lost prod data" |
| 4 | Important — changes behavior | "User prefers snake_case for all Python identifiers" |
| 3 | Useful context | "Auth middleware uses Redis for session storage" |
| 2 | Minor detail (excluded) | "Renamed a variable for clarity" |
| 1 | Trivial (excluded) | "Read a configuration file" |

---

## Concurrency & Safety

### WAL mode

SQLite is opened with Write-Ahead Logging (`PRAGMA journal_mode=WAL`). This allows concurrent reads while a write is in progress — important because the MCP server might be querying while the background extraction process is inserting.

`busy_timeout=5000` (5 seconds) makes writers retry on lock contention instead of immediately failing. In practice, write contention is rare since there's usually only one writer at a time.

### Re-entrancy problem

The SessionEnd hook calls `claude --print` to run the LLM write gate. But `claude --print` is itself a Claude Code invocation — which could trigger *another* SessionEnd hook, creating infinite recursion.

Two guards prevent this:

1. **Environment variable** — `CENTRAL_BRAIN_STOP_HOOK_ACTIVE=1` is set in the extraction subprocess's environment. The hook checks this and exits early.

2. **Hook input** — Claude Code sends `stop_hook_active: true` in the hook's stdin JSON when the hook itself triggered the invocation.

### Background extraction

The SessionEnd hook needs to survive after Claude Code exits. It achieves this by spawning a completely detached process:

```python
subprocess.Popen(
    ["central-brain", "extract-async", ...],
    start_new_session=True,      # New process group
    stdin=subprocess.DEVNULL,    # No stdin
    stdout=open(log_path, "a"),  # Log to file
)
```

The hook returns immediately; the extraction runs independently. One side effect: the `claude --print` subprocess triggers a SessionStart hook, creating a "ghost session" with `ended_at = NULL`. These are harmless but visible in `list_recent_sessions`.

---

## Key Constants

| What | Value | Where | Why |
|------|-------|-------|-----|
| RRF K | 60 | `search.py` | Standard constant from the RRF paper |
| Embedding dimensions | 1024 | `embedder.py` | voyage-3.5 default output size |
| Embedding model | voyage-3.5 | `embedder.py` | Good quality-to-cost ratio for short text |
| Word overlap threshold | >0.5 | `db.py` | 50% overlap catches rephrasing without false positives |
| Vector dedup distance | <0.15 | `db.py` | Empirically tuned — catches semantic dupes without false positives |
| FTS5 tokenizer | porter unicode61 | `db.py` | Porter stemming + Unicode support |
| Max transcript | 80,000 chars | `extract.py` | Fits within claude --print context limits |
| Extraction timeout | 120s | `extract.py` | Long transcripts need time; 2min is a reasonable ceiling |
| Importance cutoff | >=3 | `extract.py` | Filters noise while keeping useful context |
| SQLite busy_timeout | 5000ms | `db.py` | 5s retry window for write contention |
| Max code summary | 2000 chars | `code_intel.py` | Keeps the extraction prompt manageable |
| Max symbols | 50 | `code_intel.py` | Prevents huge codebases from dominating the prompt |
| Merge timeout | 30s | `extract.py` | Shorter than extraction — merge is a simpler LLM call |
| Enrichment cap | 5 merges | `db.py` | Prevents memories from growing endlessly via repeated merges |
| Content size cap | 1000 chars | `db.py` | Large memories skip LLM merge to stay focused |
| Schema version | 2 | `db.py` | v1: base schema, v2: added embeddings + vec table |
