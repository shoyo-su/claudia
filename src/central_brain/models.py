"""Pydantic models for Central Brain."""

from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class MemoryType(str, Enum):
    INSIGHT = "insight"
    DECISION = "decision"
    PATTERN = "pattern"
    ERROR = "error"
    PREFERENCE = "preference"
    TODO = "todo"
    OPEN_LOOP = "open_loop"


class MemorySource(str, Enum):
    SESSION = "session"
    MANUAL = "manual"


class Memory(BaseModel):
    id: int | None = None
    content: str
    memory_type: MemoryType = MemoryType.INSIGHT
    source: MemorySource = MemorySource.MANUAL
    session_id: str | None = None
    project: str | None = None
    tags: list[str] = Field(default_factory=list)
    importance: int = Field(default=3, ge=1, le=5)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    access_count: int = 0
    superseded_by: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def tags_json(self) -> str:
        return json.dumps(self.tags)

    def metadata_json(self) -> str:
        return json.dumps(self.metadata)


class Session(BaseModel):
    session_id: str
    project: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    summary: str | None = None
    transcript_path: str | None = None
    memory_count: int = 0


class MemorySearchResult(BaseModel):
    memory: Memory
    score: float = 0.0


class BrainStats(BaseModel):
    total_memories: int = 0
    by_type: dict[str, int] = Field(default_factory=dict)
    total_sessions: int = 0
    most_accessed: list[Memory] = Field(default_factory=list)
    recent: list[Memory] = Field(default_factory=list)
