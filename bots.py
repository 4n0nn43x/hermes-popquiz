"""Model players (SPEC section 7): ask each model the current question through an injected LlmCall.

The model sees only the question prompt and the labelled choices (never answer_index, quote or
explanation). Open book: the document comes first (stable prefix, cache friendly), then the question.

Choices made where the SPEC is silent:
- ``ANSWER_SCHEMA`` is the 4-letter schema; ``answer_schema(n)`` trims the enum to ``n`` choices and is
  what ``ask_bot`` sends.
- The question block of ``input_text`` has a fixed layout (``QUESTION:`` then ``CHOICES:`` with
  ``A. ...`` lines, always last) so ``parse_input`` can recover (mode, prompt, choices) for the cassette.
- Exceptions: class name ``PluginLlmTrustError`` -> "trust_denied"; a provider refusal before any
  generation (HTTP 4xx except 408, or ``insufficient_credits`` in the message; Hermes re-raises the
  provider's own error when its fallbacks are exhausted) -> "refused"; ``ValueError`` (Hermes raises it
  on JSON that fails schema validation) -> "invalid_json"; anything else -> "error".
- The answer is read from ``reply.parsed["choice"]`` (letter, case-insensitive, or 0-based int); if
  ``parsed`` is None the text is parsed as JSON, then searched for ``"choice": "X"``.
- Fallback detection ignores provider prefix, case and a trailing date snapshot suffix
  (``-20261104`` or ``-2026-11-04``); an empty served model is not a fallback.
- Cost is priced on the served model when the price list knows it, else on the requested model.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .model import BotSpec, Mode, Question

LETTERS = ["A", "B", "C", "D"]
DOC_MARKER = "DOCUMENT:"
QUESTION_MARKER = "QUESTION:"
CHOICES_MARKER = "CHOICES:"

INSTRUCTIONS = (
    "You are a player in a live multiple-choice quiz. Pick the single best answer. "
    "When a document is provided, answer from the document. "
    'Reply with JSON only, exactly like {"choice": "A"}, using one of the allowed letters.'
)


@dataclass
class LlmReply:
    parsed: dict | None
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


LlmCall = Callable[[BotSpec, str, str, dict, float], Awaitable[LlmReply]]
#        (spec, instructions, input_text, json_schema, timeout_s)


@dataclass
class BotResult:
    bot_id: str
    choice: int | None
    latency_ms: int
    served_model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: float
    error: str | None  # None | "timeout" | "invalid_json" | "fallback" | "trust_denied" | "refused" | "budget" | "error"

    def meta(self) -> dict:
        """Meta dict for ``Game.submit_bot_answer``."""
        return {
            "served_model": self.served_model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cost_usd": self.cost_usd,
            "error": self.error,
        }


def answer_schema(n: int) -> dict:
    n = max(2, min(4, int(n)))
    return {
        "type": "object",
        "properties": {"choice": {"type": "string", "enum": LETTERS[:n]}},
        "required": ["choice"],
    }


ANSWER_SCHEMA = answer_schema(4)


def question_block(prompt: str, choices: list[str]) -> str:
    lines = [QUESTION_MARKER, prompt.strip(), "", CHOICES_MARKER]
    lines += [f"{LETTERS[i]}. {c.strip()}" for i, c in enumerate(choices)]
    return "\n".join(lines)


def build_prompt(question: Question, doc_text: str, mode: Mode) -> tuple[str, str]:
    """Return (instructions, input_text). Open book: document first, then the question block."""
    block = question_block(question.prompt, question.choices)
    allowed = ", ".join(LETTERS[: len(question.choices)])
    instructions = f"{INSTRUCTIONS} Allowed letters: {allowed}."
    if Mode(mode) == Mode.OPEN_BOOK:
        return instructions, f"{DOC_MARKER}\n<<<\n{doc_text}\n>>>\n\n{block}"
    return instructions, block


def parse_input(input_text: str) -> tuple[str, str, list[str]]:
    """Recover (mode, prompt, choices) from an ``input_text`` built by ``build_prompt``."""
    mode = Mode.OPEN_BOOK.value if input_text.startswith(DOC_MARKER) else Mode.CLOSED_BOOK.value
    start = input_text.rfind(QUESTION_MARKER + "\n")
    block = input_text[start + len(QUESTION_MARKER) + 1:] if start >= 0 else input_text
    prompt_part, _, choices_part = block.partition("\n\n" + CHOICES_MARKER + "\n")
    choices = []
    for line in choices_part.splitlines():
        m = re.match(r"^([A-D])\. (.*)$", line)
        if m:
            choices.append(m.group(2))
    return mode, prompt_part.strip(), choices


def _strip_model(model: str) -> str:
    name = (model or "").strip().casefold().rsplit("/", 1)[-1]
    return re.sub(r"-(\d{8}|\d{4}-\d{2}-\d{2})$", "", name)


def same_model(requested: str, served: str) -> bool:
    if not served:
        return True
    return _strip_model(requested) == _strip_model(served)


def parse_choice(reply: LlmReply, n: int) -> int | None:
    data: Any = reply.parsed
    if not isinstance(data, dict):
        data = None
        text = (reply.text or "").strip()
        if text:
            fenced = re.search(r"\{.*\}", text, re.S)
            try:
                data = json.loads(fenced.group(0) if fenced else text)
            except (ValueError, TypeError):
                m = re.search(r'"choice"\s*:\s*"([A-Da-d])"', text)
                data = {"choice": m.group(1)} if m else None
    if not isinstance(data, dict) or "choice" not in data:
        return None
    value = data["choice"]
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value < n else None
    if isinstance(value, str):
        letter = value.strip().rstrip(".").upper()
        if letter in LETTERS[:n]:
            return LETTERS.index(letter)
    return None


def refused(exc: BaseException) -> bool:
    """The provider said no before generating anything (e.g. ``insufficient_credits_for_paid_model``,
    a 429 rate limit): nothing was billed. 408 is left out: the request may have started."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and 400 <= status < 500 and status != 408:
        return True
    return "insufficient_credits" in str(exc)


def _cost(pricing: Any, spec: BotSpec, served: str, reply: LlmReply) -> float:
    if pricing is None:
        return 0.0
    model = served if served and pricing.price_for(served) is not None else spec.model
    try:
        return float(pricing.cost(model, reply))
    except Exception:  # noqa: BLE001 - pricing must never break a game
        return 0.0


async def ask_bot(spec: BotSpec, question: Question, doc_text: str, mode: Mode, llm_call: LlmCall,
                  pricing: Any, timeout_s: float = 20.0) -> BotResult:
    instructions, input_text = build_prompt(question, doc_text, mode)
    schema = answer_schema(len(question.choices))
    started = time.perf_counter()

    def result(error: str | None, reply: LlmReply | None = None, choice: int | None = None) -> BotResult:
        latency_ms = int(round((time.perf_counter() - started) * 1000))
        served = (reply.model if reply else "") or ""
        return BotResult(
            bot_id=spec.id, choice=choice, latency_ms=latency_ms, served_model=served or spec.model,
            input_tokens=int(reply.input_tokens) if reply else 0,
            output_tokens=int(reply.output_tokens) if reply else 0,
            cache_read_tokens=int(reply.cache_read_tokens) if reply else 0,
            cache_write_tokens=int(reply.cache_write_tokens) if reply else 0,
            cost_usd=_cost(pricing, spec, served, reply) if reply else 0.0,
            error=error,
        )

    try:
        reply = await asyncio.wait_for(llm_call(spec, instructions, input_text, schema, timeout_s), timeout_s + 1)
    except (asyncio.TimeoutError, TimeoutError):
        return result("timeout")
    except Exception as exc:  # noqa: BLE001 - every failure becomes a scored "no answer"
        if type(exc).__name__ == "PluginLlmTrustError":
            return result("trust_denied")
        if refused(exc):
            return result("refused")
        if isinstance(exc, ValueError):
            return result("invalid_json")
        return result("error")
    if not isinstance(reply, LlmReply):
        return result("error")
    choice = parse_choice(reply, len(question.choices))
    if choice is None:
        return result("invalid_json", reply)
    error = None if same_model(spec.model, reply.model) else "fallback"
    return result(error, reply, choice)


async def ask_bots(specs: list[BotSpec], question: Question, doc_text: str, mode: Mode, llm_call: LlmCall,
                   pricing: Any, timeout_s: float = 20.0) -> list[BotResult]:
    """Ask every bot in parallel. Never raises: any failure becomes a BotResult with error "error"."""
    outcomes = await asyncio.gather(
        *(ask_bot(s, question, doc_text, mode, llm_call, pricing, timeout_s) for s in specs),
        return_exceptions=True,
    )
    results: list[BotResult] = []
    for spec, out in zip(specs, outcomes, strict=True):
        if isinstance(out, BotResult):
            results.append(out)
        else:
            results.append(BotResult(spec.id, None, 0, spec.model, 0, 0, 0, 0, 0.0, "error"))
    return results
