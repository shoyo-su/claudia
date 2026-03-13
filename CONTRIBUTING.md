# Contributing to Central Brain

## Getting Started

```bash
git clone <repo-url>
cd central-brain
./install.sh
```

The installer handles Python version detection, `uv` installation, MCP/hook configuration, and optional VoyageAI setup. It's idempotent — safe to re-run.

For manual setup or if you just want to run the code without configuring hooks:

```bash
cd central-brain
uv sync
uv run central-brain search "test"  # Verify it works
```

For development, install in editable mode so code changes are reflected immediately:

```bash
uv tool install -e .
```

## Project Structure

```
src/central_brain/
├── cli.py        # Entry point — dispatches subcommands and hook handlers
├── server.py     # FastMCP server — defines the 7 MCP tools Claude sees
├── db.py         # All SQLite: CRUD, dedup, FTS5, sqlite-vec, migrations
├── search.py     # Hybrid search: FTS5 BM25 + vector similarity + RRF fusion
├── extract.py    # Reads transcripts, calls LLM write gate, stores memories
├── embedder.py   # VoyageAI wrapper — returns None when unavailable
├── models.py     # Pydantic data models (Memory, Session, MemoryType)
└── code_intel.py # Tree-sitter Python parser — extracts symbols from code blocks
```

The key pattern to understand: `cli.py` is the single entry point for everything. It either starts the MCP server (loading `server.py`) or handles hooks/CLI commands directly. Both paths use `db.py` for storage and `search.py` for retrieval. Optional capabilities (VoyageAI, sqlite-vec, tree-sitter) are always accessed through factory functions that return `None` when unavailable.

## Code Style

- **Type hints** — Every module starts with `from __future__ import annotations`. All functions have type annotations.
- **Pydantic v2** — Data models use Pydantic with `Field()` for defaults and validation.
- **Logging** — `logger = logging.getLogger(__name__)` per module. Use logging for internal diagnostics, `print(..., file=sys.stderr)` only for user-visible CLI output.
- **Graceful degradation** — Any feature depending on an optional service (VoyageAI, sqlite-vec, tree-sitter) must wrap the import in try/except and expose a boolean flag or factory. Never let an optional dependency cause an import error.

## Common Tasks

### Adding a New Memory Type

1. Add the value to `MemoryType` enum in `models.py`
2. Update `_build_extraction_prompt()` in `extract.py` — describe when the LLM should classify memories as this type
3. Update the `memory_type` parameter descriptions in `server.py` tool docstrings
4. Add a row to the Memory Types table in `README.md`

### Adding a New MCP Tool

1. Add a function with `@mcp.tool()` decorator in `server.py`
2. Use `_get_conn()` and `_get_embedder()` for lazy-init access to the database and embedder
3. Add any new database operations to `db.py`
4. Document the tool in `docs/mcp-tools-reference.md` and add it to the summary in `README.md`

### Adding a Database Migration

Schema version is tracked in the `schema_version` table (current: **v2**).

1. Increment `SCHEMA_VERSION` in `db.py`
2. Add a `_migrate_to_vN(conn)` function
3. Add the migration check in `init_db()`:
   ```python
   if current_version < N:
       _migrate_to_vN(conn)
   ```
4. If the migration depends on an optional extension (like sqlite-vec), use the `_ensure_*` pattern — a helper that silently skips creation if the extension isn't loaded, and gets called on every `init_db()` so it picks up the extension if installed later.

## Testing

There's no automated test suite yet — **this is a great contribution opportunity**.

Manual testing workflow:

```bash
# MCP server starts without errors
central-brain serve

# Search works (expect 0 results on fresh install)
central-brain search "test"
central-brain search "query" --project my-project --type error

# Embedding backfill runs (requires VOYAGE_API_KEY)
central-brain backfill-embeddings

# Hook handlers process stdin correctly
echo '{"session_id": "test-123", "cwd": "/tmp/my-project"}' | central-brain hook-session-start
```

Good test targets if you want to add a suite:
- `db.py` — CRUD operations, dedup logic (all three tiers), migration paths
- `search.py` — FTS5 query building, RRF fusion math, empty-query fallback
- `extract.py` — Transcript JSONL parsing, LLM response JSON parsing, importance filtering
- `code_intel.py` — Python block detection (tagged and heuristic), tree-sitter symbol extraction
- Integration tests with a temp SQLite database (no VoyageAI needed for FTS5-only tests)

## PR Guidelines

- **One thing per PR** — A bug fix, a feature, a refactor. Not all three.
- **Describe why** — The diff shows *what* changed. The PR description should explain *why*.
- **Update docs** — If your change affects what users see (new tool, changed behavior, new config), update the relevant docs.
- **Test manually** — Run through the manual testing workflow above before submitting.
