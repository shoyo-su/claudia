# Hooks & Extraction

How Central Brain uses Claude Code hooks for automatic memory accumulation.

---

## Overview

Claude Code supports lifecycle hooks — shell commands that run at specific points during a session. Central Brain uses three hooks to automatically:

1. **Inject relevant memories** at session start
2. **Extract memories** before context compaction
3. **Extract memories** when a session ends

This creates a self-reinforcing memory loop: each session both consumes and produces memories, building up context over time without manual intervention.

---

## Hook Configuration

The installer (`install.sh`) configures all hooks automatically. To set them up manually:

MCP server config — add to `~/.claude/.mcp.json`:

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

Hooks — add to `~/.claude/settings.json`:

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

Each hook entry uses the `matcher`/`hooks` structure. An empty `matcher` means the hook runs for all projects. The installer deduplicates entries on re-run.

---

## SessionStart Hook

**Command:** `central-brain hook-session-start`
**Behavior:** Blocking (runs before session begins)

### Input (stdin)

Claude Code sends JSON:

```json
{
  "session_id": "abc-123-def",
  "cwd": "/Users/you/your-project",
  "transcript_path": "/Users/you/.claude/sessions/abc-123.jsonl"
}
```

### What It Does

1. Records the session in the `sessions` table
2. Derives `project` from the working directory name (`Path(cwd).name`)
3. Searches for relevant memories:
   - **Project-specific memories** — FTS5 search filtered to the current project (up to 10)
   - **Open loops** — All `open_loop` type memories (up to 5)
   - **High-importance memories** — All memories with `importance >= 4` (up to 10, deduped against above)
4. Outputs `additionalContext` that gets injected into the system prompt

### Output (stdout)

```json
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "# Central Brain — Session Memory\n\n## Relevant memories for this project:\n- [pattern] ..."
  }
}
```

If no relevant memories are found, outputs `{}`.

---

## PreCompact Hook

**Command:** `central-brain hook-pre-compact`
**Behavior:** Blocking (runs inline before compaction)

Triggered when Claude Code is about to compact the conversation context. This is a good extraction point because the full transcript is available and may be trimmed during compaction.

### What It Does

Runs the full extraction pipeline **inline** (blocking):

1. Parses the transcript JSONL file
2. Extracts code intelligence via tree-sitter
3. Sends transcript to LLM write gate (`claude --print`)
4. Stores extracted memories with auto-dedup and embedding

Since the session is still active, the extraction runs synchronously. Expect this to take 30-120 seconds depending on transcript length.

---

## SessionEnd Hook

**Command:** `central-brain hook-stop`
**Event:** `SessionEnd`
**Behavior:** Non-blocking (forks to background)

Triggered when the Claude Code session ends.

### What It Does

1. Checks re-entrancy guards (see below)
2. Spawns a **detached background subprocess** via `central-brain extract-async`
3. Returns immediately so the session can exit cleanly

The background subprocess:
- Runs the full extraction pipeline
- Sets `ended_at` on the session record
- Updates `memory_count`
- Logs output to `~/.central-brain/extract.log`

### Re-entrancy Guard

The extraction pipeline calls `claude --print`, which is itself a Claude Code invocation. Without guards, this would trigger another stop hook, creating infinite recursion.

Two layers of protection:

1. **Environment variable:** `CENTRAL_BRAIN_STOP_HOOK_ACTIVE` is set in the current process before calling `_extract_from_hook`. The extraction subprocess also sets this in the `claude --print` child's environment.

2. **Hook input field:** Claude Code sends `stop_hook_active: true` in the hook's stdin JSON when the hook invocation itself triggered the stop. The hook checks this and exits early.

---

## LLM Write Gate

The core extraction mechanism uses `claude --print` (Claude Code's non-interactive mode) to analyze transcripts and produce structured memories.

### How It Works

```
Transcript text (last 80K chars)
     +
Code summary (tree-sitter symbols)
     |
     v
[claude --print -p <extraction_prompt>]
     |
     v
JSON array of memories
     |
     v
Filter: importance >= 3
     |
     v
Store with auto-dedup + auto-embed
```

### Extraction Prompt

The prompt instructs the LLM to:

- Extract memories as JSON objects with `content`, `memory_type`, `tags`, `importance`
- Focus on decisions (and why), errors (and root causes), preferences, patterns, open loops, TODOs
- Exclude routine operations, info already in code/git, temporary debugging context
- Only include memories scoring importance >= 3
- Include relevant function/class names when code structure is detected

### Importance Scoring

| Score | Meaning | Example |
|-------|---------|---------|
| 5 | Critical — MUST inform future sessions | "Never run migrations without backup — lost prod data on 2026-03-01" |
| 4 | Important — changes behavior | "User prefers snake_case for all Python" |
| 3 | Useful context | "The auth middleware uses Redis for session storage" |
| 2 | Minor detail (excluded) | "Renamed a variable" |
| 1 | Trivial (excluded) | "Read a file" |

### Environment

The `claude --print` subprocess runs with:
- `CLAUDECODE` unset — prevents nested session detection
- `CENTRAL_BRAIN_STOP_HOOK_ACTIVE=1` — prevents child hooks from re-extracting
- `timeout=120` seconds

---

## Code Intelligence

When transcripts contain Python code blocks, tree-sitter is used to extract structured symbols before sending to the LLM.

### Detection

Code blocks are found via:
1. Fenced blocks tagged as Python (`` ```python `` or `` ```py ``)
2. Untagged fenced blocks matching Python heuristics (`def `, `class `, `import `, `from X import`)

### Extraction

For each detected block, tree-sitter parses:
- **Functions** — name, parameters, decorators, line range
- **Classes** — name, base classes, method names
- **Imports** — module, imported names

### Output

The symbol summary is injected into the extraction prompt:

```
Code structure found in transcript:
- Function: extract_memories_via_llm(messages, session_id, project)
- Class: VoyageEmbedder (methods: embed, embed_single)
- Imports: voyageai, numpy
```

Structured metadata is stored in `Memory.metadata.code_intel`:

```json
{
  "code_intel": {
    "functions": ["extract_memories_via_llm"],
    "classes": ["VoyageEmbedder"],
    "imports": ["voyageai", "numpy"],
    "language": "python"
  }
}
```

---

## Troubleshooting

### `claude` CLI not found

The extraction pipeline requires the `claude` CLI to be on `PATH`. If you installed Claude Code via npm, ensure the npm bin directory is in your shell's `PATH`.

```
[central-brain] claude CLI not found, skipping LLM extraction
```

### No transcript path

The hook received input without a `transcript_path` field. This can happen with older Claude Code versions.

```
[central-brain] No transcript path in stop hook input
```

### Extraction timeout

The LLM write gate has a 120-second timeout. Very long transcripts (near the 80K char limit) may take longer. If this happens frequently, the extraction still completes for the PreCompact hook (which runs earlier with a shorter transcript).

```
[central-brain] LLM extraction timed out
```

### Ghost sessions (ended_at = NULL)

Background extraction spawns a `claude --print` subprocess that triggers the SessionStart hook, creating session records with `ended_at = NULL`. These are harmless but visible in `list_recent_sessions`. They're an artifact of the re-entrancy pattern.

### Missing VOYAGE_API_KEY

If `VOYAGE_API_KEY` is set in `~/.zshrc` but not exported, subprocesses (hooks, background extraction) can't see it. Make sure to use:

```bash
export VOYAGE_API_KEY="your-key-here"
```

Not just:

```bash
VOYAGE_API_KEY="your-key-here"  # Missing export — won't work in hooks
```

### Checking extraction logs

Background extraction output goes to:

```bash
cat ~/.central-brain/extract.log
```

This includes both stdout and stderr from the `central-brain extract-async` process.
