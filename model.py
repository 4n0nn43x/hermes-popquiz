"""Data model shared by every Pop Quiz module (SPEC section 3).

Plain dataclasses and enums, no I/O, no Hermes import.

Additions to the SPEC (backward compatible, documented here):
- ``Document.truncated`` (default False): set by ``ingest.load_document`` when the text was cut to
  ``max_chars`` ("truncate to max_chars and flag it", SPEC section 5).
- ``GameError.detail``: optional human readable hint; ``.code`` stays the contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class GameState(str, Enum):
    LOBBY = "lobby"
    QUESTION = "question"
    REVEAL = "reveal"
    LEADERBOARD = "leaderboard"
    FINISHED = "finished"


class Mode(str, Enum):
    OPEN_BOOK = "open_book"
    CLOSED_BOOK = "closed_book"


class GameError(Exception):
    """Error with a stable snake_case ``code`` (part of the contract) and an optional ``detail``."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass
class Question:
    index: int
    prompt: str
    choices: list[str]  # 2..4, distinct after normalization
    answer_index: int
    source_quote: str
    explanation: str = ""
    anchor_score: float = 1.0
    quote_offset: int = -1  # char offset in normalized doc, -1 unknown
    time_limit_s: int = 20


@dataclass
class Player:
    id: str  # short random id
    nickname: str
    token_hash: str  # sha256 hex of the player token; the token itself is never stored
    joined_at: float
    connected: bool = True
    score: int = 0
    streak: int = 0


@dataclass
class BotSpec:
    id: str  # "bot1".. stable
    label: str  # display name, e.g. "Claude Opus 5.5"
    provider: str  # "nous"
    model: str  # Portal id, e.g. "anthropic/claude-opus-5.5"


@dataclass
class Answer:
    actor_id: str  # player id, bot id, or "room"
    actor_kind: str  # "human" | "bot" | "room"
    q: int
    choice: int | None  # None = no answer / timeout / invalid
    received_at: float
    latency_ms: int
    correct: bool
    points: int
    meta: dict = field(default_factory=dict)  # bots: served_model, tokens, cost_usd, error


@dataclass
class Document:
    title: str
    text: str  # extracted plain text / markdown
    path: str
    sha256: str
    kind: str  # "txt" | "md" | "html" | "pdf" | "docx" | "epub"
    truncated: bool = False


DEFAULT_BOTS: tuple[BotSpec, ...] = (
    BotSpec(id="bot1", label="Claude Opus 5.5", provider="nous", model="anthropic/claude-opus-5.5"),
    BotSpec(id="bot2", label="DeepSeek V4 Flash", provider="nous", model="deepseek/deepseek-v4-flash"),
    BotSpec(id="bot3", label="Mercury 3", provider="nous", model="inception/mercury-3"),
)


# Free formation (`players: [free]`, `demo --live --free`): Portal models priced $0, checked on
# 2026-11-07 to answer the JSON schema. The Portal's free list changes: check /v1/models.
FREE_BOTS: tuple[BotSpec, ...] = (
    BotSpec(id="bot1", label="Solar Mini 4 (free)", provider="nous", model="upstage/solar-mini4:free"),
    BotSpec(id="bot2", label="LongCat 2.5 (free)", provider="nous", model="meituan/longcat-2.5:free"),
    BotSpec(id="bot3", label="Beam 1.0 (free)", provider="nous", model="reflection/beam-1.0-501b-a23b:free"),
)


def default_bots() -> list[BotSpec]:
    """Fresh copies of the 3 default model players (SPEC section 7)."""
    return [BotSpec(b.id, b.label, b.provider, b.model) for b in DEFAULT_BOTS]


def free_bots() -> list[BotSpec]:
    """Fresh copies of the free formation."""
    return [BotSpec(b.id, b.label, b.provider, b.model) for b in FREE_BOTS]
