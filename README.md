# Central Brain

**MCP Memory Server for Claude Code**

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-compatible-purple.svg)](https://modelcontextprotocol.io)

## The Problem

Every time you start a new Claude Code session, Claude starts from zero. It doesn't know that you spent 2 hours debugging a SQLAlchemy migration yesterday, that your team never mocks the database in integration tests, or that the `payment_service` has a quirky retry pattern you've explained three times already.

You end up re-explaining the same context, re-discovering the same gotchas, and watching Claude make the same mistakes it made last week.

## What Central Brain Does

Central Brain gives Claude Code a persistent memory that survives across sessions. Here's what that looks like in practice:

**Session 1** — You spend an hour debugging a tricky issue:

```
You: Why is the webhook handler dropping events?
Claude: [investigates] The handler isn't retrying on 429 responses.
        The Stripe SDK needs exponential backoff configured...
You: Also, never use time.sleep() in async handlers — we got burned
     by that blocking the event loop last month.
```

**Session 2** (next day, different task) — Claude already knows:

```
# Central Brain — Session Memory

## Relevant memories for this project:
- [error] Webhook handler was dropping Stripe events due to missing
  retry logic on 429 responses. Fixed with exponential backoff.
- [preference] Never use time.sleep() in async handlers — blocks
  the event loop. Use asyncio.sleep() instead.
- [open_loop] Payment reconciliation job still needs error alerting.
```

This context is injected *automatically* — you didn't tag anything, didn't write any notes. Central Brain extracted those memories from your session transcript, scored them by importance, and surfaced the relevant ones when you started a new session in the same project.

## How It Works

Central Brain runs as an [MCP server](https://modelcontextprotocol.io) alongside Claude Code, connected through three lifecycle hooks:

**1. Session starts** — The SessionStart hook searches the memory database for memories related to your current project, any unfinished work (open loops), and high-importance memories. These get injected into Claude's system prompt as `additionalContext`, so Claude has context before you even type anything.

**2. During the session** — Claude can use 7 MCP tools to explicitly store, search, update, or delete memories. For example, Claude might call `remember` to save a decision you just made, or `recall` to search for how you handled something similar before.

**3. Session ends** — The SessionEnd hook reads the full session transcript, runs it through an LLM write gate (`claude --print`), and extracts memories worth keeping. A tree-sitter code intelligence layer parses any Python code in the transcript so memories carry structured context — which functions were discussed, which classes were modified, which imports matter.

The extraction is selective: an importance scoring system (1-5) filters out noise. Routine file reads (importance 1) get dropped. A critical error root cause (importance 5) gets stored. The threshold is 3.

Everything is stored locally in a SQLite database at `~/.central-brain/memory.db`. Search uses hybrid retrieval — FTS5 full-text search for keyword matching and VoyageAI vector embeddings for semantic similarity — fused together with Reciprocal Rank Fusion. If you don't have a VoyageAI API key, it falls back to FTS5-only, which still works well.

## Quick Start

### One-Line Install

```bash
./install.sh
```

This finds Python 3.11+, installs `uv` if needed, installs `central-brain` as a CLI tool, configures the MCP server and all three hooks in Claude Code, and optionally prompts for a VoyageAI API key. Safe to re-run — it's idempotent.

Also works via curl:

```bash
curl -fsSL https://raw.githubusercontent.com/shoyo-su/claudia/main/install.sh | bash
```

### Manual Install

If you prefer to set things up yourself:

**Prerequisites:** Python 3.11+, [uv](https://docs.astral.sh/uv/), [Claude Code](https://docs.anthropic.com/en/docs/claude-code)

```bash
cd central-brain
uv tool install -e .
```

Add the MCP server to `~/.claude/.mcp.json`:

```json
{
  "mcpServers": {
    "central-brain": {
      "command": "central-brain",
      "args": ["serve"]
    }
  }
}
```

Add hooks to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [
      { "matcher": "", "hooks": [{ "type": "command", "command": "central-brain hook-session-start" }] }
    ],
    "PreCompact": [
      { "matcher": "", "hooks": [{ "type": "command", "command": "central-brain hook-pre-compact" }] }
    ],
    "SessionEnd": [
      { "matcher": "", "hooks": [{ "type": "command", "command": "central-brain hook-stop" }] }
    ]
  }
}
```

### Enable Vector Search (Optional)

Add to your `~/.zshrc` or `~/.bashrc`:

```bash
export VOYAGE_API_KEY="your-key-here"
```

The `export` keyword is important — without it, subprocesses (hooks, background extraction) can't see the key. Without VoyageAI, everything still works using FTS5 keyword search.

### Verify

```bash
central-brain search "test"
```

You should see `(hybrid search, 0 results)` or `(FTS5-only search, 0 results)` — both mean it's working.

---

## Memory Types

Central Brain categorizes memories so they can be filtered and prioritized:

| Type | What it captures | Example |
|------|-----------------|---------|
| `insight` | General learnings about the codebase | "The auth middleware stores sessions in Redis, not the database" |
| `decision` | Choices made and *why* | "Chose Celery over RQ for task queue because we need task chaining" |
| `pattern` | Recurring patterns worth noting | "All API endpoints follow the service-repository pattern in this project" |
| `error` | Bugs and their root causes | "sqlite-vec requires conn.enable_load_extension(True) *before* loading" |
| `preference` | How the user likes things done | "Never use time.sleep() in async handlers — use asyncio.sleep()" |
| `todo` | Tasks mentioned but not completed | "Need to add retry logic to the webhook consumer" |
| `open_loop` | Unfinished work or open questions | "Phase 3 planning started but not yet implemented" |

These types are assigned automatically during extraction. The LLM write gate decides the type based on the conversation content.

## MCP Tools

During a session, Claude has access to these tools:

| Tool | What it does |
|------|-------------|
| `remember` | Store a memory — with content, type, tags, importance (1-5), and optional project scope |
| `recall` | Search memories using natural language. Combines keyword + semantic search. Filter by project or type. |
| `forget` | Delete a memory, or mark it as superseded by a newer one (soft delete) |
| `get_memory_by_id` | Fetch full details of a specific memory, including metadata and access count |
| `update_memory_tool` | Update a memory's content, tags, or importance. Re-embeds automatically if content changes. |
| `list_recent_sessions` | See recent Claude Code sessions — when they ran, which project, how many memories extracted |
| `brain_stats` | Dashboard: total memories by type, most-accessed memories, recent additions |

Most of the time you don't need to use these directly — the automatic extraction and injection handles the common case. But they're useful when Claude wants to explicitly save something important mid-session, or when you want to search your memory from a new session.

See [docs/mcp-tools-reference.md](docs/mcp-tools-reference.md) for full parameter and response documentation.

## Code Intelligence

When Central Brain extracts memories from a session transcript, it doesn't just look at the conversation text — it parses the code too.

Any Python code blocks in the transcript (fenced with `` ```python `` or detected via heuristics like `def `, `class `, `import `) are run through a [tree-sitter](https://tree-sitter.github.io/) parser. This extracts structured symbols: function names and their parameters, class hierarchies, and import graphs.

This serves two purposes:

1. **Better extraction** — The LLM write gate receives a code structure summary alongside the transcript, so it can produce memories that reference specific functions and classes instead of vague descriptions.

2. **Richer metadata** — Extracted memories carry a `code_intel` metadata block:
   ```json
   {
     "code_intel": {
       "functions": ["extract_memories_via_llm", "parse_transcript"],
       "classes": ["VoyageEmbedder"],
       "imports": ["voyageai", "tree_sitter"],
       "language": "python"
     }
   }
   ```

If tree-sitter isn't available, extraction still works — just without the structured symbol context.

## Search

Central Brain uses two search strategies and merges their results:

**FTS5 full-text search** — SQLite's built-in search engine with BM25 ranking and porter stemming. Good at exact keyword matches. "webhook retry logic" will find memories containing those exact words even if spelled slightly differently (stemming handles "retrying", "retries", etc).

**Vector similarity search** — Each memory is embedded as a 1024-dimensional vector using VoyageAI's `voyage-3.5` model. Good at semantic matches. "how do we handle rate limiting" can find a memory about "429 response backoff" even though they share no keywords.

Results from both are merged using [Reciprocal Rank Fusion](https://plg.uwaterloo.ca/~gvcormac/cormacksigir09-rrf.pdf) (RRF, K=60), which combines ranked lists without needing to normalize scores across different systems.

When you search from the CLI:

```bash
central-brain search "webhook error handling" --project my-api --type error
```

You see hybrid or FTS5-only mode, depending on whether VoyageAI is configured.

## Deduplication

Sessions often produce similar insights. Central Brain prevents duplicates with a 3-tier check on every insert:

1. **FTS5 fuzzy match** — Search for the first 8 words of the new memory, filtered to the same memory type
2. **Word overlap** — If any candidate shares >50% of words (Jaccard similarity), it's a duplicate
3. **Vector distance** — If the above didn't catch it, check if any existing memory has vector distance <0.15 (very semantically similar)

When a duplicate is found, the new memory is *not* inserted. Instead, the existing memory's importance is bumped if the new one scored higher. This keeps the memory count clean while still reflecting that a topic keeps coming up.

## Graceful Degradation

Central Brain works at three capability tiers — you get the best experience available without hard failures:

| Without this | What happens | What still works |
|-------------|-------------|-----------------|
| VoyageAI API key | No vector embeddings | FTS5 keyword search handles all queries |
| sqlite-vec extension | No vector table | Same as above — FTS5 only |
| tree-sitter | No code structure parsing | Memories still extracted, just without function/class metadata |

## CLI Reference

```bash
central-brain serve                    # Start MCP server (stdio transport)
central-brain search <query>           # Search from command line
central-brain search <q> --project X   # Filter by project
central-brain search <q> --type error  # Filter by memory type
central-brain backfill-embeddings      # Generate embeddings for old memories
central-brain hook-session-start       # SessionStart hook handler
central-brain hook-pre-compact         # PreCompact hook handler
central-brain hook-stop                # SessionEnd hook handler
central-brain extract-async            # Background extraction (internal)
```

## Configuration

| Path | Purpose |
|------|---------|
| `~/.central-brain/memory.db` | SQLite database — all memories and sessions |
| `~/.central-brain/extract.log` | Background extraction log (from SessionEnd hook) |
| `~/.claude/.mcp.json` | MCP server configuration |
| `~/.claude/settings.json` | Claude Code settings with hook definitions |

| Env Variable | Required | Purpose |
|-------------|----------|---------|
| `VOYAGE_API_KEY` | No | Enables vector embeddings for semantic search |

---

## Further Reading

- [Architecture](docs/architecture.md) — Database schema, search internals, extraction pipeline, concurrency model
- [Hooks & Extraction](docs/hooks-and-extraction.md) — How automatic memory extraction works, the LLM write gate, troubleshooting
- [MCP Tools Reference](docs/mcp-tools-reference.md) — Full API docs for all 7 tools with parameters, responses, and examples
- [Contributing](CONTRIBUTING.md) — Development setup, code style, how to add tools/types/migrations
