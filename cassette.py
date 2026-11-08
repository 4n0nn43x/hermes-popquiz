"""Record and replay model answers (SPEC section 7) for CI and the zero-cost demo.

``CassetteLlm`` is an ``LlmCall``. Replay never touches the network; record wraps a real ``inner`` call.

Key: ``cassette_key(model, mode, prompt, choices)`` = sha256 hex of the JSON array
``[model, mode, prompt, choices]``; (mode, prompt, choices) are recovered from ``input_text`` with
``bots.parse_input`` (the document itself is not part of the key).

File format (UTF-8 JSON)::

    {
      "version": 1,
      "note": "free text",
      "entries": {
        "<key>": {
          "model": "anthropic/claude-opus-5.5",      # requested model (spec.model)
          "mode": "open_book",
          "prompt": "How long ...?",
          "choices": ["7 days", "30 days"],
          "latency_ms": 8420,                         # replay sleeps latency_ms / 1000 / speed
          "error": null,                              # null | "timeout" | "trust_denied" | "invalid_json" | "error"
          "reply": {"parsed": {"choice": "B"}, "text": "{\\"choice\\": \\"B\\"}",
                    "model": "anthropic/claude-opus-5.5", "input_tokens": 2310,
                    "output_tokens": 412, "cache_read_tokens": 0, "cache_write_tokens": 0}
        }
      }
    }

Replay of a recorded error: "timeout" raises ``TimeoutError`` after the sleep, "trust_denied" raises
``PluginLlmTrustError``, "error" raises ``RuntimeError``; "invalid_json" returns the recorded reply
(parsed null). A missing key returns a reply with ``parsed=None`` (scored as invalid) and logs a warning.
``speed <= 0`` disables the sleep. Record mode writes the file after every call (atomic replace).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

from .bots import LlmCall, LlmReply, parse_input
from .model import BotSpec

log = logging.getLogger(__name__)

VERSION = 1


class PluginLlmTrustError(Exception):
    """Replayed trust refusal (same class name as the Hermes error, so bots map it to trust_denied)."""


def cassette_key(model: str, mode: str, prompt: str, choices: list[str]) -> str:
    payload = json.dumps([model, mode, prompt, list(choices)], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def reply_from_dict(data: dict, default_model: str) -> LlmReply:
    def num(name: str) -> int:
        try:
            return max(0, int(data.get(name) or 0))
        except (TypeError, ValueError):
            return 0

    parsed = data.get("parsed")
    return LlmReply(
        parsed=dict(parsed) if isinstance(parsed, dict) else None,
        text=str(data.get("text") or ""),
        model=str(data.get("model") or default_model),
        input_tokens=num("input_tokens"), output_tokens=num("output_tokens"),
        cache_read_tokens=num("cache_read_tokens"), cache_write_tokens=num("cache_write_tokens"),
    )


class CassetteLlm:
    def __init__(self, path: str | os.PathLike, mode: str = "replay", inner: LlmCall | None = None,
                 speed: float = 1.0) -> None:
        if mode not in ("replay", "record"):
            raise ValueError("mode must be 'replay' or 'record'")
        if mode == "record" and inner is None:
            raise ValueError("record mode needs an inner LlmCall")
        self.path = Path(path)
        self.mode = mode
        self.inner = inner
        self.speed = float(speed)
        self.entries: dict[str, dict] = {}
        self.note = ""
        self.misses: list[str] = []
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.entries = dict(data.get("entries") or {})
            self.note = str(data.get("note") or "")
        elif mode == "replay":
            raise FileNotFoundError(str(self.path))

    def __len__(self) -> int:
        return len(self.entries)

    @property
    def simulated(self) -> bool:
        """Replay makes no model call: the report labels its spend as simulated."""
        return self.mode == "replay"

    def key_for(self, spec: BotSpec, input_text: str) -> str:
        mode, prompt, choices = parse_input(input_text)
        return cassette_key(spec.model, mode, prompt, choices)

    async def __call__(self, spec: BotSpec, instructions: str, input_text: str, json_schema: dict,
                       timeout_s: float) -> LlmReply:
        if self.mode == "record":
            return await self._record(spec, instructions, input_text, json_schema, timeout_s)
        return await self._replay(spec, input_text, timeout_s)

    async def _replay(self, spec: BotSpec, input_text: str, timeout_s: float) -> LlmReply:
        key = self.key_for(spec, input_text)
        entry = self.entries.get(key)
        if entry is None:
            self.misses.append(key)
            log.warning("cassette miss for %s (key %s): answer counted invalid", spec.model, key[:12])
            return LlmReply(parsed=None, text="", model=spec.model)
        if self.speed > 0:
            await asyncio.sleep(max(0.0, float(entry.get("latency_ms") or 0)) / 1000.0 / self.speed)
        error = entry.get("error")
        if error == "timeout":
            raise TimeoutError("recorded timeout")
        if error == "trust_denied":
            raise PluginLlmTrustError("recorded trust refusal")
        if error == "error":
            raise RuntimeError("recorded provider error")
        return reply_from_dict(entry.get("reply") or {}, spec.model)

    async def _record(self, spec: BotSpec, instructions: str, input_text: str, json_schema: dict,
                      timeout_s: float) -> LlmReply:
        assert self.inner is not None
        mode, prompt, choices = parse_input(input_text)
        key = cassette_key(spec.model, mode, prompt, choices)
        entry: dict = {"model": spec.model, "mode": mode, "prompt": prompt, "choices": choices,
                       "latency_ms": 0, "error": None, "reply": None}
        started = time.perf_counter()
        try:
            reply = await self.inner(spec, instructions, input_text, json_schema, timeout_s)
        except BaseException as exc:
            entry["latency_ms"] = int(round((time.perf_counter() - started) * 1000))
            if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
                entry["error"] = "timeout"
            elif type(exc).__name__ == "PluginLlmTrustError":
                entry["error"] = "trust_denied"
            elif isinstance(exc, ValueError):
                entry["error"] = "invalid_json"
                entry["reply"] = {"parsed": None, "text": "", "model": spec.model}
            elif isinstance(exc, Exception):
                entry["error"] = "error"
            else:
                raise
            self.entries[key] = entry
            self.save()
            raise
        entry["latency_ms"] = int(round((time.perf_counter() - started) * 1000))
        entry["reply"] = asdict(reply)
        self.entries[key] = entry
        self.save()
        return reply

    def save(self) -> None:
        data = {"version": VERSION, "note": self.note, "entries": self.entries}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".cassette-", suffix=".json", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=1, sort_keys=True)
                fh.write("\n")
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
