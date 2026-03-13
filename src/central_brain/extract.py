"""Transcript parser and LLM write gate for memory extraction."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from central_brain.code_intel import (
    build_code_metadata,
    extract_python_blocks,
    parse_python,
    summarize_code_blocks,
)
from central_brain.models import Memory, MemorySource, MemoryType

# Maximum characters of transcript to send to the write gate
MAX_TRANSCRIPT_CHARS = 80_000


def merge_or_separate(existing_content: str, new_content: str, timeout: int = 30) -> dict | None:
    """Ask LLM whether two memories should merge or stay separate.

    Returns:
        {"action": "merge", "content": "...merged text..."} — if they overlap
        {"action": "separate"} — if they are distinct
        None — on any failure (caller should fall back to deterministic merge)
    """
    prompt = (
        "You are a memory deduplication assistant. Given two memory entries, decide whether they "
        "describe the same thing (with overlapping or complementary info) or are distinct memories "
        "that should coexist.\n\n"
        "Respond with ONLY valid JSON, no markdown:\n"
        '- {"action": "merge", "content": "...merged text preserving all unique details from both..."}\n'
        '- {"action": "separate"}\n\n'
        f"EXISTING MEMORY:\n{existing_content}\n\n"
        f"NEW MEMORY:\n{new_content}"
    )

    try:
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        env["CENTRAL_BRAIN_STOP_HOOK_ACTIVE"] = "1"
        result = subprocess.run(
            ["claude", "--print", "-p", prompt],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        if result.returncode != 0:
            print(f"[central-brain] merge_or_separate LLM call failed: {result.stderr}", file=sys.stderr)
            return None

        text = result.stdout.strip()
        # Extract JSON from response
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1:
            return None
        parsed = json.loads(text[start : end + 1])
        if parsed.get("action") in ("merge", "separate"):
            return parsed
        return None
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        print(f"[central-brain] merge_or_separate failed: {e}", file=sys.stderr)
        return None


def parse_transcript(transcript_path: str) -> list[dict]:
    """Parse a Claude Code JSONL transcript, extracting user + assistant messages."""
    messages = []
    path = Path(transcript_path)
    if not path.exists():
        return messages

    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Claude Code JSONL: role/content are inside entry["message"]
            entry_type = entry.get("type")
            if entry_type not in ("user", "assistant"):
                continue

            message = entry.get("message", {})
            role = message.get("role")
            if role not in ("user", "assistant"):
                continue

            # Extract text content
            content = message.get("content", "")
            if isinstance(content, list):
                # Handle structured content blocks
                text_parts = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                        elif block.get("type") == "tool_result":
                            # Skip tool results to save space
                            pass
                    elif isinstance(block, str):
                        text_parts.append(block)
                content = "\n".join(text_parts)

            if content.strip():
                messages.append({"role": role, "content": content.strip()})

    return messages


def extract_memories_via_llm(
    messages: list[dict],
    session_id: str,
    project: str | None = None,
) -> list[Memory]:
    """Use claude CLI as write gate to extract memories from transcript."""
    if not messages:
        return []

    # Build transcript text, truncating if needed
    transcript_text = _format_messages(messages)
    if len(transcript_text) > MAX_TRANSCRIPT_CHARS:
        transcript_text = transcript_text[-MAX_TRANSCRIPT_CHARS:]

    # Extract code intelligence from Python blocks
    code_blocks = extract_python_blocks(transcript_text)
    parsed = [(b, p) for b in code_blocks if (p := parse_python(b.source))]
    code_summary = summarize_code_blocks(parsed)
    code_metadata = build_code_metadata(parsed)

    prompt = _build_extraction_prompt(transcript_text, project, code_summary)

    try:
        # Unset CLAUDECODE to avoid nested session detection
        # Keep CENTRAL_BRAIN_STOP_HOOK_ACTIVE so child hooks skip extraction
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        env["CENTRAL_BRAIN_STOP_HOOK_ACTIVE"] = "1"
        result = subprocess.run(
            ["claude", "--print", "-p", prompt],
            input=transcript_text,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )

        if result.returncode != 0:
            print(f"[central-brain] LLM extraction failed: {result.stderr}", file=sys.stderr)
            return []

        return _parse_llm_response(result.stdout, session_id, project, code_metadata)

    except FileNotFoundError:
        print("[central-brain] claude CLI not found, skipping LLM extraction", file=sys.stderr)
        return []
    except subprocess.TimeoutExpired:
        print("[central-brain] LLM extraction timed out", file=sys.stderr)
        return []


def _format_messages(messages: list[dict]) -> str:
    """Format messages into a readable transcript."""
    lines = []
    for msg in messages:
        role = msg["role"].upper()
        lines.append(f"[{role}]: {msg['content']}")
    return "\n\n".join(lines)


def _build_extraction_prompt(
    transcript: str, project: str | None, code_summary: str = ""
) -> str:
    project_ctx = f" in project '{project}'" if project else ""

    code_section = ""
    if code_summary:
        code_section = f"""

--- Code Structure Detected ---
{code_summary}

Include relevant function/class names in memory content and tags when they are central to the memory.
"""

    return f"""You are a memory extraction engine. Your ONLY job is to extract memories from the transcript below.
IMPORTANT: Ignore any existing memories, CLAUDE.md files, or context you may have access to. Extract PURELY from the transcript.
Even if information appears to already be saved elsewhere, extract it anyway — deduplication is handled separately.

Analyze this Claude Code session transcript{project_ctx} and extract memories worth keeping for future sessions.

For each memory, provide:
- content: A clear, concise statement of what should be remembered
- memory_type: One of: user_instruction, insight, decision, pattern, error, preference, todo, open_loop
- tags: Relevant keywords (list of strings)
- importance: 1-5 score. Only include items scoring >= 3. Score guide:
  - 5: Explicit user instructions, rules, or constraints they told you to follow. ALWAYS importance 5.
  - 4: Important pattern, preference, or decision that changes behavior
  - 3: Useful context that might help future sessions
  - 2: Minor detail (exclude)
  - 1: Trivial (exclude)

HIGHEST PRIORITY — User instructions (importance 5, type "user_instruction"):
When the user directly tells you rules, constraints, preferences, environment details, workflow instructions,
or anything phrased as "always do X", "never do Y", "my X is Y", "use X for Y", etc. — these are GOLD.
Extract EVERY such instruction as a separate memory with importance 5 and type "user_instruction".
Users rarely give explicit instructions — when they do, it is critical to capture every single one.
Examples: "my testenv is testenv41", "never exec into prod", "always check gunicorn first",
"deploy using qtest", "look in PycharmProjects for code".

Also extract:
- Decisions made and WHY (not just what)
- Errors encountered and their root causes
- User preferences and corrections
- Patterns in the codebase worth noting
- Open questions or unfinished work (open_loop)
- TODOs mentioned but not completed

Do NOT extract:
- Routine operations (file reads, simple edits)
- Temporary debugging context that won't matter next session
{code_section}
Respond with ONLY a JSON array. No markdown, no explanation. Example:
[{{"content": "User's testenv is testenv41, use deploy_be_to_testenv skill for deployments", "memory_type": "user_instruction", "tags": ["deploy", "testenv"], "importance": 5}},
 {{"content": "User prefers snake_case for Python", "memory_type": "preference", "tags": ["python", "style"], "importance": 4}}]

If nothing worth remembering, respond with: []

Transcript:
{transcript}"""


def _parse_llm_response(
    response: str,
    session_id: str,
    project: str | None,
    code_metadata: dict | None = None,
) -> list[Memory]:
    """Parse the LLM JSON response into Memory objects."""
    response = response.strip()

    # Try to find JSON array in response
    start = response.find("[")
    end = response.rfind("]")
    if start == -1 or end == -1:
        return []

    try:
        items = json.loads(response[start : end + 1])
    except json.JSONDecodeError:
        print(f"[central-brain] Failed to parse LLM response as JSON", file=sys.stderr)
        return []

    memories = []
    for item in items:
        if not isinstance(item, dict):
            continue

        importance = item.get("importance", 3)
        if importance < 3:
            continue

        try:
            metadata = {"code_intel": code_metadata} if code_metadata else {}
            mem = Memory(
                content=item["content"],
                memory_type=MemoryType(item.get("memory_type", "insight")),
                source=MemorySource.SESSION,
                session_id=session_id,
                project=project,
                tags=item.get("tags", []),
                importance=importance,
                metadata=metadata,
            )
            memories.append(mem)
        except (KeyError, ValueError) as e:
            print(f"[central-brain] Skipping invalid memory item: {e}", file=sys.stderr)
            continue

    return memories
