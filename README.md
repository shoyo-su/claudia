# Central Brain

**MCP Memory Server for Claude Code**

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-compatible-purple.svg)](https://modelcontextprotocol.io)

Central Brain gives Claude Code persistent memory across sessions. It stores insights, decisions, errors, and patterns in a local SQLite database, retrieves them using hybrid FTS5 + vector search, and automatically extracts new memories from session transcripts via an LLM write gate. When vector embeddings aren't available, it gracefully degrades to FTS5-only search.

---

## Quick Start

### One-Line Install

```bash
./install.sh
```

This handles everything: installs `uv` if needed, finds Python 3.11+, installs `central-brain` as a CLI tool, configures the MCP server and hooks in Claude Code, and optionally prompts for a VoyageAI API key.

Also works via curl:

```bash
curl -fsSL https://raw.githubusercontent.com/ajitesh-bhalerao/central-brain/main/install.sh | bash
```

### Manual Install

If you prefer to set things up yourself:

**Prerequisites:** Python 3.11+, [uv](https://docs.astral.sh/uv/), [Claude Code](https://docs.anthropic.com/en/docs/claude-code)

```bash
cd central-brain
uv tool install -e .
```

Add to `~/.claude/.mcp.json`:

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

### Optional: Enable Vector Search

```bash
export VOYAGE_API_KEY="your-key-here"
```

Add this to your `~/.zshrc` (or `~/.bashrc`) with the `export` keyword so subprocesses can access it. The installer will prompt for this interactively.

### Verify

```bash
central-brain search "test"
```

---

## How It Works

```
Session Start                                            Next Session
     |                                                        |
     v                                                        v
 [SessionStart Hook]                                  [SessionStart Hook]
     |                                                        |
     |  Injects relevant memories                             |  Recalls stored memories
     |  as additionalContext                                  |  from previous sessions
     v                                                        v
 [Claude Code Session]                                [Claude Code Session]
     |                                                        |
     |  MCP tools available:                                  |
     |  remember, recall, forget,                             |
     |  get_memory_by_id, update_memory_tool,                 |
     |  list_recent_sessions, brain_stats                     |
     v                                                        |
 [SessionEnd Hook]                                               |
     |                                                        |
     |  Parses transcript                                     |
     |  Extracts code intelligence (tree-sitter)              |
     |  LLM write gate (claude --print)                       |
     |  Filters importance >= 3                               |
     v                                                        |
 [SQLite DB]  -------- memories with embeddings ------------>-+
   ~/.central-brain/memory.db
```

---

## MCP Tools

| Tool | Description | Key Parameters |
|------|-------------|----------------|
| `remember` | Store a new memory | `content`, `memory_type`, `project`, `tags`, `importance` (1-5) |
| `recall` | Hybrid search for memories | `query`, `project`, `memory_type`, `limit` |
| `forget` | Delete or supersede a memory | `memory_id`, `superseded_by` |
| `get_memory_by_id` | Fetch a specific memory | `memory_id` |
| `update_memory_tool` | Update content, tags, or importance | `memory_id`, `content`, `tags`, `importance` |
| `list_recent_sessions` | List recent sessions with summaries | `limit` |
| `brain_stats` | Memory counts, most accessed, recent | — |

See [docs/mcp-tools-reference.md](docs/mcp-tools-reference.md) for full API documentation.

---

## Memory Types

| Type | Description |
|------|-------------|
| `insight` | General observations and learnings |
| `decision` | Choices made and their rationale |
| `pattern` | Recurring patterns in code or workflows |
| `error` | Errors encountered and their root causes |
| `preference` | User preferences and corrections |
| `todo` | Tasks mentioned but not yet completed |
| `open_loop` | Unfinished work or open questions |

---

## CLI Commands

| Command | Description |
|---------|-------------|
| `central-brain serve` | Start the MCP server (stdio transport) |
| `central-brain search <query>` | Search memories from the command line (`--project`, `--type` filters) |
| `central-brain backfill-embeddings` | Generate embeddings for memories that don't have them yet |
| `central-brain extract-async` | Background extraction entrypoint (used internally by stop hook) |
| `central-brain hook-session-start` | SessionStart hook handler |
| `central-brain hook-pre-compact` | PreCompact hook handler |
| `central-brain hook-stop` | SessionEnd hook handler |

---

## Configuration

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `VOYAGE_API_KEY` | No | VoyageAI API key for vector embeddings. Without it, search uses FTS5 only. |

### File Paths

| Path | Description |
|------|-------------|
| `~/.central-brain/memory.db` | SQLite database (created automatically) |
| `~/.central-brain/extract.log` | Background extraction log output |
| `~/.claude/.mcp.json` | MCP server configuration |
| `~/.claude/settings.json` | Claude Code settings including hooks |

---

## Graceful Degradation

Central Brain is designed to work at multiple capability levels:

| Component | Without It | Impact |
|-----------|-----------|--------|
| **VoyageAI** (`VOYAGE_API_KEY`) | FTS5-only search | No vector similarity — keyword matching still works well |
| **sqlite-vec** | No `memories_vec` table | Vector search disabled, FTS5 handles all queries |
| **tree-sitter** | No code intelligence | Extraction still works, just without structured symbol metadata |

---

## Hook Setup

The installer configures hooks automatically. To set them up manually, add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "",
        "hooks": [{ "type": "command", "command": "central-brain hook-session-start" }]
      }
    ],
    "PreCompact": [
      {
        "matcher": "",
        "hooks": [{ "type": "command", "command": "central-brain hook-pre-compact" }]
      }
    ],
    "SessionEnd": [
      {
        "matcher": "",
        "hooks": [{ "type": "command", "command": "central-brain hook-stop" }]
      }
    ]
  }
}
```

See [docs/hooks-and-extraction.md](docs/hooks-and-extraction.md) for details on how each hook works.

---

## Documentation

- [MCP Tools Reference](docs/mcp-tools-reference.md) — Full API reference for all 7 tools
- [Architecture](docs/architecture.md) — Database schema, search internals, extraction pipeline
- [Hooks & Extraction](docs/hooks-and-extraction.md) — Hook setup and automatic memory extraction
- [Contributing](CONTRIBUTING.md) — Development setup and contribution guide
