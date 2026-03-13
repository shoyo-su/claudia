# Contributing to Central Brain

## Getting Started

The quickest way to get set up:

```bash
git clone <repo-url>
cd central-brain
./install.sh
```

The installer handles Python version detection, `uv` installation, MCP/hook configuration, and optional VoyageAI setup.

For development, you can also install manually:

```bash
cd central-brain
uv sync
```

Verify the install:

```bash
uv run central-brain serve   # Should start MCP server (ctrl-c to exit)
uv run central-brain search "test"  # Should run without errors
```

## Development Setup

Central Brain uses [uv](https://docs.astral.sh/uv/) as its package manager. For development, install in editable mode:

```bash
uv tool install -e .
```

This makes the `central-brain` command available globally and reflects code changes immediately.

## Project Structure

```
src/central_brain/
├── cli.py          # CLI entrypoint, subcommands, hook handlers
├── server.py       # FastMCP server with 7 MCP tool definitions
├── db.py           # SQLite + FTS5 + sqlite-vec, CRUD, dedup, migrations
├── search.py       # Hybrid search with RRF fusion
├── extract.py      # Transcript parsing, LLM write gate
├── embedder.py     # VoyageAI embedding wrapper
├── models.py       # Pydantic models (Memory, Session, MemoryType)
└── code_intel.py   # Tree-sitter Python parsing
```

## Code Style

- **Type hints everywhere** — use `from __future__ import annotations` at the top of every module
- **Pydantic v2** for data models
- **Logging** — use `logger = logging.getLogger(__name__)` per module, not print statements (except in CLI output)
- **Graceful degradation** — features that depend on optional services (VoyageAI, sqlite-vec, tree-sitter) must fail silently and fall back

## Adding a New Memory Type

1. Add the new value to `MemoryType` enum in `models.py`
2. Update the extraction prompt in `extract.py` (`_build_extraction_prompt`) to describe when the LLM should use this type
3. Update the `memory_type` parameter descriptions in `server.py` tool docstrings
4. Update the Memory Types table in `README.md`

## Adding a New MCP Tool

1. Define the tool function with `@mcp.tool()` decorator in `server.py`
2. Use the lazy-init pattern: call `_get_conn()` and `_get_embedder()` instead of accessing globals directly
3. Add any new DB operations to `db.py`
4. Document the tool in `docs/mcp-tools-reference.md`
5. Add a row to the MCP Tools summary table in `README.md`

## Database Migrations

The schema version is tracked in the `schema_version` table (current: **v2**).

To add a migration:

1. Increment `SCHEMA_VERSION` in `db.py`
2. Add a `_migrate_to_vN(conn)` function with the migration logic
3. Add the version check in `init_db()`:
   ```python
   if current_version < N:
       _migrate_to_vN(conn)
   ```
4. Handle the case where optional extensions (sqlite-vec) may not be available — use `_ensure_*` helper patterns

## Testing

Central Brain does not have an automated test suite yet. This is a great contribution opportunity.

Current manual testing workflow:

```bash
# Test MCP server
central-brain serve

# Test search
central-brain search "some query"
central-brain search "query" --project my-project --type pattern

# Test embedding backfill
central-brain backfill-embeddings

# Test hook handlers (pipe JSON to stdin)
echo '{"session_id": "test", "cwd": "/tmp"}' | central-brain hook-session-start
```

If you'd like to add tests, consider:
- Unit tests for `db.py` (CRUD, dedup logic, migrations)
- Unit tests for `search.py` (FTS5 query building, RRF fusion)
- Unit tests for `extract.py` (transcript parsing, LLM response parsing)
- Unit tests for `code_intel.py` (Python block detection, tree-sitter parsing)
- Integration tests with a temporary SQLite database

## PR Guidelines

- Keep PRs focused — one feature or fix per PR
- Describe **why**, not just what
- Update documentation for any user-facing changes
- Test manually with the workflow above before submitting
