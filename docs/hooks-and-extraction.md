# Hooks & Extraction

This document covers how Central Brain automatically accumulates memories from your Claude Code sessions without any manual effort.

---

## Why Automatic Extraction?

The most useful memories are ones you don't have to think about saving. When you spend an hour debugging a SQLAlchemy migration, the important takeaway — "the migration failed because of a missing `nullable=False` column in the ORM model that didn't match the existing schema" — is buried in a long conversation. You're unlikely to stop and manually tag it.

Central Brain hooks into Claude Code's lifecycle to extract these insights automatically. Every session produces memories. Every new session starts with the relevant ones pre-loaded.

---

## The Three Hooks

Central Brain uses three Claude Code lifecycle hooks. The installer (`install.sh`) configures all three automatically. Here's what each one does and why it exists.

### SessionStart — Inject context

**When:** Before every session starts
**What it does:** Searches the memory database and injects relevant memories into Claude's system prompt

This is what makes the memory *useful*. Without injection, memories would accumulate but never be seen. The hook searches for:

- **Project-specific memories** (up to 10) — Filtered to the current working directory name. If you're in `~/projects/my-api`, it searches for memories tagged with project "my-api".
- **Open loops** (up to 5) — Any `open_loop` type memory, regardless of project. These are unfinished tasks or questions that should carry forward.
- **High-importance memories** (up to 10) — Anything scored importance >= 4, regardless of project. Critical errors and preferences should follow you everywhere.

The output is a markdown block that Claude sees as system context:

```
# Central Brain — Session Memory

## Relevant memories for this project:
- [error] sqlite-vec requires conn.enable_load_extension(True) before loading
- [pattern] All API endpoints use the service-repository pattern

## Open loops (unresolved from previous sessions):
- Phase 3 planning started but not yet implemented

## Important memories:
- [preference] Never mock the database in integration tests
```

**Configuration:**

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "",
        "hooks": [{ "type": "command", "command": "central-brain hook-session-start" }]
      }
    ]
  }
}
```

The empty `matcher` means it runs for all projects. The hook reads JSON from stdin (session_id, cwd, transcript_path) and writes JSON to stdout with `additionalContext`.

### PreCompact — Extract before context loss

**When:** Before Claude Code compacts the conversation context
**What it does:** Runs the full extraction pipeline inline

Context compaction happens when the conversation gets long. Claude Code summarizes older messages to make room. This is the last chance to extract memories from the full conversation before parts of it are lost.

This hook runs **synchronously** (30-120 seconds) because the session is still active and needs the extraction to complete before compaction proceeds.

**Configuration:**

```json
{
  "hooks": {
    "PreCompact": [
      {
        "matcher": "",
        "hooks": [{ "type": "command", "command": "central-brain hook-pre-compact" }]
      }
    ]
  }
}
```

### SessionEnd — Extract when you're done

**When:** When the Claude Code session ends
**What it does:** Spawns a background process to extract memories from the full transcript

This is the primary extraction point. By the time a session ends, the transcript contains everything worth remembering. But the extraction takes 30-120 seconds (it calls `claude --print` under the hood), so it can't block session exit.

The hook spawns a **completely detached subprocess** (`start_new_session=True`) that survives after Claude Code exits. The subprocess:
- Runs `central-brain extract-async --session-id ... --project ... --transcript ...`
- Logs to `~/.central-brain/extract.log`
- Has no stdin/stdout connection to the parent process

**Configuration:**

```json
{
  "hooks": {
    "SessionEnd": [
      {
        "matcher": "",
        "hooks": [{ "type": "command", "command": "central-brain hook-stop" }]
      }
    ]
  }
}
```

---

## The Extraction Pipeline

Both PreCompact and SessionEnd hooks run the same extraction pipeline. Here's what happens step by step:

### 1. Parse the transcript

Claude Code writes session transcripts as JSONL files. Each line is a message with a role (user/assistant) and content. The parser extracts text from these, skipping tool results (they're large and noisy).

### 2. Code intelligence

Before sending the transcript to the LLM, Central Brain scans it for Python code. It looks for:

- Fenced code blocks tagged as Python (`` ```python `` or `` ```py ``)
- Untagged fenced blocks that look like Python (contain `def `, `class `, `import `, or `from X import`)

Each detected block is parsed with [tree-sitter](https://tree-sitter.github.io/) to extract:
- **Functions** — name, parameters, decorators
- **Classes** — name, base classes, method list
- **Imports** — module name, imported symbols

This produces a summary that gets injected into the extraction prompt:

```
Code structure found in transcript:
- Function: extract_memories_via_llm(messages, session_id, project)
- Class: VoyageEmbedder (methods: embed, embed_single)
- Imports: voyageai, numpy, sqlite_vec
```

And structured metadata that gets stored on each extracted memory:

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

If tree-sitter isn't installed, this step is skipped silently.

### 3. LLM write gate

The transcript (last 80,000 characters) and code summary are sent to `claude --print` — Claude Code's non-interactive mode. The extraction prompt asks the LLM to:

- Produce a JSON array of memories, each with `content`, `memory_type`, `tags`, and `importance`
- Focus on decisions (and *why*), errors (and root causes), preferences, patterns, open loops, TODOs
- Reference specific function/class names when code structure is available
- Exclude routine operations, info already in code/git, temporary debugging context
- Only include items scoring importance >= 3

### 4. Filter and store

The LLM response is parsed as JSON. Each item with importance < 3 is discarded. Surviving memories are inserted with full deduplication and auto-embedding. The session record is updated with `ended_at` and `memory_count`.

### Re-entrancy protection

There's a subtle problem: the extraction pipeline calls `claude --print`, which is itself a Claude Code invocation. That invocation could trigger a SessionEnd hook, which would run another extraction, which would call `claude --print` again... infinite recursion.

Two guards prevent this:

1. **Environment variable** — The extraction process sets `CENTRAL_BRAIN_STOP_HOOK_ACTIVE=1` before calling `claude --print`. The SessionEnd hook checks this and exits immediately.

2. **Hook input** — Claude Code passes `stop_hook_active: true` in the hook's stdin JSON when the stop hook itself triggered the invocation. The hook checks this as a cross-process guard.

---

## Troubleshooting

### Extraction isn't producing any memories

Check the extraction log:
```bash
cat ~/.central-brain/extract.log
```

Common causes:
- **No transcript path** — Older Claude Code versions may not pass `transcript_path` in hook input
- **Empty transcript** — Very short sessions may not have enough content to extract from
- **LLM returned `[]`** — The session genuinely had nothing worth remembering

### `claude` CLI not found

```
[central-brain] claude CLI not found, skipping LLM extraction
```

The extraction pipeline requires the `claude` CLI on `PATH`. If you installed Claude Code via npm, ensure the npm bin directory is in your `PATH`.

### Extraction timeout

```
[central-brain] LLM extraction timed out
```

The LLM write gate has a 120-second timeout. Very long transcripts may need more time. The PreCompact hook (which runs earlier with a shorter transcript) usually succeeds even when SessionEnd times out.

### Ghost sessions

`list_recent_sessions` shows sessions with `ended_at = null`. These are created when background extraction spawns a `claude --print` subprocess, which triggers a SessionStart hook. They're harmless artifacts of the re-entrancy pattern.

### VOYAGE_API_KEY not visible to hooks

If `VOYAGE_API_KEY` is in your `~/.zshrc` but hooks can't see it:

```bash
# Wrong — not exported, subprocesses can't see it
VOYAGE_API_KEY="your-key"

# Right — exported to subprocess environment
export VOYAGE_API_KEY="your-key"
```

### Checking what was injected at session start

The SessionStart hook's output is visible in Claude Code's system context. You can also test it manually:

```bash
echo '{"session_id": "test", "cwd": "'$(pwd)'"}' | central-brain hook-session-start
```

This shows exactly what would be injected for the current directory.
