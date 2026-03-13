"""CLI entrypoints for Central Brain — serve, search, and hook handlers."""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from central_brain.db import (
    DEFAULT_DB_PATH,
    get_db,
    get_session_start_summary,
    init_db,
    insert_memory,
    store_embedding,
    update_session_memory_count,
    upsert_session,
)
from central_brain.embedder import get_embedder
from central_brain.extract import extract_memories_via_llm, parse_transcript
from central_brain.models import Memory, MemorySource, Session
from central_brain.search import hybrid_search


def main():
    """Main CLI entrypoint — dispatches subcommands."""
    if len(sys.argv) < 2:
        print("Usage: central-brain <command>", file=sys.stderr)
        print("Commands: serve, search, backfill-embeddings, hook-session-start, hook-pre-compact, hook-stop", file=sys.stderr)
        sys.exit(1)

    cmd = sys.argv[1]
    handlers = {
        "serve": cmd_serve,
        "search": cmd_search,
        "backfill-embeddings": cmd_backfill_embeddings,
        "extract-async": cmd_extract_async,
        "hook-session-start": hook_session_start,
        "hook-pre-compact": hook_pre_compact,
        "hook-stop": hook_stop,
    }

    handler = handlers.get(cmd)
    if not handler:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)

    handler()


def cmd_serve():
    """Start the MCP server via stdio."""
    from central_brain.server import mcp
    mcp.run(transport="stdio")


def cmd_search():
    """CLI search interface — uses hybrid search when embedder is available."""
    if len(sys.argv) < 3:
        print("Usage: central-brain search <query> [--project <name>] [--type <type>]", file=sys.stderr)
        sys.exit(1)

    query = sys.argv[2]
    project = None
    memory_type = None

    args = sys.argv[3:]
    i = 0
    while i < len(args):
        if args[i] == "--project" and i + 1 < len(args):
            project = args[i + 1]
            i += 2
        elif args[i] == "--type" and i + 1 < len(args):
            memory_type = args[i + 1]
            i += 2
        else:
            i += 1

    conn = get_db(DEFAULT_DB_PATH)
    init_db(conn)

    embedder = get_embedder()
    results = hybrid_search(conn, query, embedder=embedder, project=project, memory_type=memory_type, limit=20)
    conn.close()

    mode = "hybrid" if embedder else "FTS5-only"
    print(f"({mode} search, {len(results)} results)\n", file=sys.stderr)

    for r in results:
        m = r.memory
        print(f"[{m.id}] ({m.memory_type.value}, importance={m.importance}, score={r.score:.4f}) {m.content[:120]}")
        if m.tags:
            print(f"     tags: {', '.join(m.tags)}")
        print()


def cmd_backfill_embeddings():
    """Backfill embeddings for all memories that don't have one yet."""
    conn = get_db(DEFAULT_DB_PATH)
    init_db(conn)

    embedder = get_embedder()
    if embedder is None:
        print("ERROR: Embedder unavailable. Set VOYAGE_API_KEY and install voyageai.", file=sys.stderr)
        sys.exit(1)

    # Find memories without embeddings
    rows = conn.execute(
        "SELECT id, content FROM memories WHERE embedding IS NULL AND superseded_by IS NULL"
    ).fetchall()

    if not rows:
        print("All memories already have embeddings.", file=sys.stderr)
        conn.close()
        return

    print(f"Backfilling {len(rows)} memories...", file=sys.stderr)

    batch_size = 128
    total = 0

    for batch_start in range(0, len(rows), batch_size):
        batch = rows[batch_start:batch_start + batch_size]
        texts = [row["content"] for row in batch]
        ids = [row["id"] for row in batch]

        try:
            embeddings = embedder.embed(texts)
        except Exception as e:
            print(f"ERROR embedding batch at offset {batch_start}: {e}", file=sys.stderr)
            continue

        for memory_id, embedding in zip(ids, embeddings):
            store_embedding(conn, memory_id, embedding)
            total += 1

        print(f"  Embedded {total}/{len(rows)}", file=sys.stderr)

        # Rate limit between batches
        if batch_start + batch_size < len(rows):
            time.sleep(1)

    conn.close()
    print(f"Done. Embedded {total} memories.", file=sys.stderr)


def hook_session_start():
    """SessionStart hook — present context menu instead of auto-loading all memories.

    Reads hook input from stdin (JSON with session info).
    Outputs JSON with additionalContext containing a lightweight menu
    that lets the user choose what context to load.
    """
    # Parse hook input
    try:
        hook_input = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, EOFError):
        hook_input = {}

    session_id = hook_input.get("session_id", str(uuid.uuid4()))
    cwd = hook_input.get("cwd", os.getcwd())
    project = Path(cwd).name

    conn = get_db(DEFAULT_DB_PATH)
    init_db(conn)

    # Record session start
    session = Session(
        session_id=session_id,
        project=project,
        started_at=datetime.now(timezone.utc),
        transcript_path=hook_input.get("transcript_path"),
    )
    upsert_session(conn, session)

    # Get lightweight summary for the trigger instruction
    summary = get_session_start_summary(conn, project=project)
    conn.close()

    if summary["total_memories"] == 0:
        print(json.dumps({}))
        return

    # Build preview strings
    def _preview_line(items: list[str]) -> str:
        return ", ".join(items) if items else "none"

    top_preview = _preview_line(summary["top_accessed_previews"])
    loop_preview = _preview_line(summary["open_loop_previews"])

    # Minimal trigger instruction — no memories loaded into context
    additional_context = (
        "# Central Brain\n"
        f"Project: {project} — {summary['total_memories']} memories available. "
        "User can type \"jarvis\" to activate memory recall.\n\n"
        "RULES:\n"
        "- If the user's message is or contains \"jarvis\", present this menu EXACTLY:\n\n"
        f"  **Brain** ({project}) — {summary['total_memories']} memories\n\n"
        f"  1. **Most used** ({summary['project_memory_count']}) — {top_preview}\n"
        f"  2. **Open loops** ({summary['open_loop_count']}) — {loop_preview}\n"
        f"  3. **Search** — search memories by keyword\n\n"
        "  Then wait for their pick.\n\n"
        f"- Handle picks: 1 → call `recall_frequent` tier=\"top\" project=\"{project}\" | "
        f"2 → call `recall` memory_type=\"open_loop\" project=\"{project}\" | "
        f"3 → ask keyword, then call `recall` with project=\"{project}\"\n"
        "- IMPORTANT: Always pass project=\"" + project + "\" to every recall/recall_frequent call.\n"
        "- If the user does NOT say \"jarvis\", behave completely normally. "
        "Do not mention memories, do not show this menu, do not load any context."
    )

    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": additional_context,
        }
    }
    print(json.dumps(output))


def hook_pre_compact():
    """PreCompact hook — extract memories from transcript before compaction."""
    _extract_from_hook("pre-compact")


def hook_stop():
    """Stop hook — extract memories from transcript and finalize session.

    Guards against re-entrancy (stop hook calling claude which triggers another stop).
    """
    # Re-entrancy guard: check both env var (same process) and hook input (cross-process)
    if os.environ.get("CENTRAL_BRAIN_STOP_HOOK_ACTIVE"):
        return

    os.environ["CENTRAL_BRAIN_STOP_HOOK_ACTIVE"] = "1"
    try:
        _extract_from_hook("stop")
    finally:
        os.environ.pop("CENTRAL_BRAIN_STOP_HOOK_ACTIVE", None)


def _extract_from_hook(hook_name: str):
    """Common extraction logic for hooks.

    For the stop hook, forks extraction into a detached background process
    so it survives Claude Code session exit. PreCompact runs inline since
    the session is still active.
    """
    try:
        raw = sys.stdin.read()
        hook_input = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, EOFError):
        hook_input = {}

    # Cross-process re-entrancy guard: Claude Code sends stop_hook_active=true
    # when the stop hook itself triggered this invocation
    if hook_input.get("stop_hook_active"):
        print(f"[central-brain] {hook_name}: skipping (stop_hook_active)", file=sys.stderr)
        return

    session_id = hook_input.get("session_id", "unknown")
    cwd = hook_input.get("cwd", os.getcwd())
    project = Path(cwd).name
    transcript_path = hook_input.get("transcript_path")

    print(f"[central-brain] {hook_name}: session={session_id}, project={project}, transcript={transcript_path}", file=sys.stderr)

    if not transcript_path:
        print(f"[central-brain] No transcript path in {hook_name} hook input", file=sys.stderr)
        return

    if hook_name == "stop":
        # Fork into background so extraction survives session exit
        _spawn_background_extraction(session_id, project, transcript_path)
    else:
        # PreCompact runs inline — session is still active
        _run_extraction(hook_name, session_id, project, transcript_path)


def _spawn_background_extraction(session_id: str, project: str, transcript_path: str) -> None:
    """Spawn a detached subprocess for extraction that survives parent exit."""
    import subprocess

    # Use central-brain CLI itself with a new subcommand
    cmd = [
        "central-brain", "extract-async",
        "--session-id", session_id,
        "--project", project,
        "--transcript", transcript_path,
    ]

    # Detach: new session, no stdin/stdout ties to parent
    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=open(os.path.expanduser("~/.central-brain/extract.log"), "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        print(f"[central-brain] stop: spawned background extraction for {session_id}", file=sys.stderr)
    except Exception as e:
        print(f"[central-brain] stop: failed to spawn background extraction: {e}", file=sys.stderr)


def _run_extraction(hook_name: str, session_id: str, project: str, transcript_path: str) -> None:
    """Run extraction inline (blocking)."""
    conn = get_db(DEFAULT_DB_PATH)
    init_db(conn)

    # Initialize embedder for auto-embedding during insert
    embedder = get_embedder()

    # Parse transcript
    messages = parse_transcript(transcript_path)
    if not messages:
        conn.close()
        return

    # Extract memories via LLM write gate
    memories = extract_memories_via_llm(messages, session_id, project)

    # Store extracted memories (with auto-embedding)
    stored_count = 0
    for mem in memories:
        insert_memory(conn, mem, embedder=embedder)
        stored_count += 1

    # Update session record
    session = Session(
        session_id=session_id,
        project=project,
        ended_at=datetime.now(timezone.utc) if hook_name == "stop" else None,
    )
    upsert_session(conn, session)
    if stored_count > 0:
        update_session_memory_count(conn, session_id)

    conn.close()

    print(
        f"[central-brain] {hook_name}: extracted {stored_count} memories from session {session_id}",
        file=sys.stderr,
    )


def cmd_extract_async():
    """Background extraction entrypoint — called by the detached stop hook subprocess."""
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--transcript", required=True)
    args = parser.parse_args(sys.argv[2:])

    print(f"[central-brain] extract-async: starting for session {args.session_id}")

    conn = get_db(DEFAULT_DB_PATH)
    init_db(conn)
    embedder = get_embedder()

    messages = parse_transcript(args.transcript)
    if not messages:
        print(f"[central-brain] extract-async: no messages in transcript")
        conn.close()
        return

    memories = extract_memories_via_llm(messages, args.session_id, args.project)

    stored_count = 0
    for mem in memories:
        insert_memory(conn, mem, embedder=embedder)
        stored_count += 1

    # Finalize session
    session = Session(
        session_id=args.session_id,
        project=args.project,
        ended_at=datetime.now(timezone.utc),
    )
    upsert_session(conn, session)
    if stored_count > 0:
        update_session_memory_count(conn, args.session_id)

    conn.close()
    print(f"[central-brain] extract-async: extracted {stored_count} memories from session {args.session_id}")
