"""Hermes-facing glue (SPEC section 9): settings, model calls through ``ctx.llm``, price cache, document
roots, the chat progress card, report delivery, and the ``PopQuizApp`` that backs the 4 tools and ``/popquiz``.

Only public Hermes surfaces are used: the ``ctx`` facade handed to ``register()`` and the public modules
``hermes_constants`` and ``hermes_cli.config`` (both imported lazily, both optional). Every setting is
read at call time, never in ``register()``.

Threads: tool handlers and the slash command run on Hermes threads; the game runs on the QuizServer
thread (its own loop). ``on_event`` is called on the server thread, so chat edits and report injection
are handed to one small worker thread and never block the game loop. The calling session's
``contextvars`` are captured when the quiz is created so a later injection from the worker still
targets that session.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import functools
import json
import logging
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .anchor import validate_questions
from .bots import LlmReply
from .model import BotSpec, GameError, Mode, default_bots

log = logging.getLogger(__name__)

PLUGIN_DIR = Path(__file__).resolve().parent
SAMPLE_DIR = PLUGIN_DIR / "sample"
MAX_ATTEMPTS = 3
MIN_QUESTIONS = 3
MAX_QUESTIONS = 20
CARD_MIN_INTERVAL_S = 2.0
PRICE_CACHE_KEY = "pricing_cache_v1"
PRICE_CACHE_TTL_S = 24 * 3600
CAPABILITIES = ("llm.model_override", "llm.provider_override")

DEFAULTS: dict[str, Any] = {
    "host": "127.0.0.1",
    "port": 8765,
    "public_url": "",
    "players": None,  # None = model.default_bots()
    "budget_usd": 1.0,
    "time_limit_s": 20,
    "mode": "open_book",
    "auto_advance": False,
}


# ---------------------------------------------------------------------------------------------- settings

def _as_int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, out))


def _as_float(value: Any, default: float, lo: float, hi: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if out != out:
        return default
    return max(lo, min(hi, out))


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return default


def read_settings(ctx: Any) -> dict:
    """Read every setting now (plugins.entries.popquiz.settings.*), clamp it, fill defaults."""
    def get(key: str) -> Any:
        try:
            return ctx.get_config(key, DEFAULTS[key])
        except Exception:  # noqa: BLE001 - a broken config never breaks a quiz
            return DEFAULTS[key]

    mode = str(get("mode") or "open_book").strip().lower()
    return {
        "host": str(get("host") or "127.0.0.1").strip() or "127.0.0.1",
        "port": _as_int(get("port"), 8765, 0, 65535),
        "public_url": str(get("public_url") or "").strip(),
        "players": parse_players(get("players")),
        "budget_usd": _as_float(get("budget_usd"), 1.0, 0.0, 50.0),
        "time_limit_s": _as_int(get("time_limit_s"), 20, 5, 120),
        "mode": mode if mode in ("open_book", "closed_book") else "open_book",
        "auto_advance": _as_bool(get("auto_advance"), False),
    }


def parse_players(raw: Any) -> list[BotSpec]:
    """``players`` setting -> BotSpec list (max 4). Bad entries are skipped; nothing valid -> defaults."""
    if not isinstance(raw, list) or not raw:
        return default_bots()
    out: list[BotSpec] = []
    for item in raw[:4]:
        if isinstance(item, str) and item.strip():
            item = {"model": item.strip()}
        if not isinstance(item, dict):
            continue
        model = str(item.get("model") or "").strip()
        if not model:
            continue
        label = str(item.get("label") or model.rsplit("/", 1)[-1]).strip()[:32]
        provider = str(item.get("provider") or "nous").strip()
        out.append(BotSpec(id=f"bot{len(out) + 1}", label=label, provider=provider, model=model))
    return out or default_bots()


# ---------------------------------------------------------------------------------------------- models

def capabilities_granted(ctx: Any) -> bool:
    """Both overrides are needed: a Portal model id without provider="nous" would go to the main provider."""
    try:
        return all(bool(ctx.has_capability(cap)) for cap in CAPABILITIES)
    except Exception:  # noqa: BLE001 - unknown -> fail closed
        return False


def main_model_name() -> str:
    """The user's main model id from config.yaml (public ``hermes_cli.config``), or "main"."""
    try:
        from hermes_cli.config import load_config
        model = (load_config() or {}).get("model")
    except Exception:  # noqa: BLE001
        return "main"
    if isinstance(model, dict):
        model = model.get("default") or model.get("model")
    return str(model).strip() if isinstance(model, str) and model.strip() else "main"


def resolve_bots(ctx: Any, settings: dict) -> tuple[list[BotSpec], bool]:
    """(bots, degraded). Degraded = overrides not granted: one bot, the user's main model."""
    if capabilities_granted(ctx):
        return list(settings["players"]), False
    name = main_model_name()
    label = ("Main model: " + name.rsplit("/", 1)[-1])[:32] if name != "main" else "Your main model"
    return [BotSpec(id="bot1", label=label, provider="", model=name)], True


def make_llm_call(ctx: Any, executor: concurrent.futures.Executor | None = None) -> Callable:
    """Return an ``LlmCall`` backed by the SYNC ``ctx.llm.complete_structured``, run in a worker thread so
    it works from the server loop whatever loop ``ctx.llm`` binds to. A bot with an empty provider is the
    main-model bot of the degraded mode: no override is sent."""

    def call_sync(spec: BotSpec, instructions: str, input_text: str, json_schema: dict, timeout_s: float):
        kwargs: dict[str, Any] = {
            "instructions": instructions,
            "input": [{"type": "text", "text": input_text}],
            "json_schema": json_schema,
            "schema_name": "popquiz_answer",
            "timeout": float(timeout_s),
            "purpose": "popquiz model player",
        }
        if spec.provider:
            kwargs["provider"] = spec.provider
            kwargs["model"] = spec.model
        result = ctx.llm.complete_structured(**kwargs)
        usage = getattr(result, "usage", None)
        served = str(getattr(result, "model", "") or "")
        if not spec.provider and served:
            served = spec.model if served.rsplit("/", 1)[-1] in spec.model else served
        return LlmReply(
            parsed=getattr(result, "parsed", None), text=str(getattr(result, "text", "") or ""), model=served,
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            cache_read_tokens=int(getattr(usage, "cache_read_tokens", 0) or 0),
            cache_write_tokens=int(getattr(usage, "cache_write_tokens", 0) or 0),
        )

    async def llm_call(spec: BotSpec, instructions: str, input_text: str, json_schema: dict,
                       timeout_s: float) -> LlmReply:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            executor, functools.partial(call_sync, spec, instructions, input_text, json_schema, timeout_s))

    llm_call.call_sync = call_sync  # type: ignore[attr-defined]  (used by scripts and tests)
    return llm_call


# ---------------------------------------------------------------------------------------------- pricing

def load_pricing_cached(ctx: Any, models: list[str], *, now: Callable[[], float] = time.time,
                        fetch: Callable[..., Any] | None = None):
    """Prices for ``models``: ctx.state cache (24 h), else the public catalog, else the embedded table."""
    from . import pricing as pricing_mod

    state = getattr(ctx, "state", None)
    wanted = sorted({m for m in models if m} | set(pricing_mod._FALLBACK_PER_M))
    try:
        cached = state.get(PRICE_CACHE_KEY) if state is not None else None
    except Exception:  # noqa: BLE001
        cached = None
    if isinstance(cached, dict) and now() - float(cached.get("fetched_at") or 0) < PRICE_CACHE_TTL_S:
        prices = cached.get("prices") or {}
        if all(m in prices for m in models if m and m != "main"):
            return _pricing_from_cache(pricing_mod, prices, "catalog-cache", cached.get("as_of", ""))
    data = (fetch or pricing_mod.fetch_catalog)(timeout=5)
    if data is not None:
        full = pricing_mod.Pricing.from_catalog(data)
        if len(full):
            subset = {m: _price_row(full.price_for(m)) for m in wanted if full.price_for(m) is not None}
            stamp = time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(now()))
            try:
                if state is not None:
                    state.set(PRICE_CACHE_KEY, {"fetched_at": now(), "as_of": stamp, "prices": subset})
            except Exception:  # noqa: BLE001 - cache is best effort
                log.debug("popquiz: price cache write failed", exc_info=True)
            return full
    return pricing_mod.Pricing.fallback()


def _price_row(p: Any) -> list[float]:
    return [p.prompt, p.completion, p.input_cache_read, p.input_cache_write]


def _pricing_from_cache(pricing_mod: Any, rows: dict, source: str, as_of: str):
    prices = {m: pricing_mod.ModelPrice(m, *[float(x) for x in row[:4]]) for m, row in rows.items()
              if isinstance(row, list) and len(row) >= 4}
    return pricing_mod.Pricing(prices, source=source, as_of=as_of)


# ---------------------------------------------------------------------------------------------- documents

def hermes_home() -> Path | None:
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home()).resolve()
    except Exception:  # noqa: BLE001
        return None


def document_cache_dirs(home: Path | None) -> list[Path]:
    """Where the gateway caches documents users send (gateway/platforms/base.py DOCUMENT_CACHE_DIR =
    get_hermes_dir("cache/documents", "document_cache")); both layouts are allowed."""
    if home is None:
        return []
    return [home / "cache" / "documents", home / "document_cache"]


def session_cwd(ctx: Any) -> str:
    try:
        return str(ctx.session_cwd() or "")
    except Exception:  # noqa: BLE001
        return ""


def allowed_roots(ctx: Any) -> list[Path]:
    roots: list[Path] = []
    roots.extend(document_cache_dirs(hermes_home()))
    cwd = session_cwd(ctx)
    if cwd and Path(cwd).resolve() != Path("/"):
        roots.append(Path(cwd).resolve())
    roots.append(SAMPLE_DIR)
    return roots


def resolve_doc_path(doc_path: str, ctx: Any) -> str:
    """A relative path is relative to the session folder (the gateway process cwd is somewhere else)."""
    path = Path(str(doc_path)).expanduser()
    cwd = session_cwd(ctx)
    if not path.is_absolute() and cwd:
        path = Path(cwd) / path
    return str(path)


def check_document_path(doc_path: str, roots: list[Path]) -> None:
    """Extra guard on top of ingest: no hidden files, nothing inside the Hermes home except its document
    cache (a session cwd can be the Hermes home; its .env and auth files must never become a quiz)."""
    real = Path(str(doc_path)).expanduser().resolve()
    if real.name.startswith("."):
        raise GameError("path_not_allowed", "hidden files are not allowed")
    home = hermes_home()
    if home is not None and real.is_relative_to(home):
        if not any(real.is_relative_to(d.resolve()) for d in document_cache_dirs(home)) \
                and not real.is_relative_to(PLUGIN_DIR):
            raise GameError("path_not_allowed", "files inside the Hermes home are not allowed (except the "
                                                "document cache)")


# ---------------------------------------------------------------------------------------------- chat card

class ChatCard:
    """One progress message in the conversation, edited in place at most every ``interval`` seconds.
    ``post`` must run in the agent's turn (session bound). If posting is unavailable (classic CLI, no
    gateway) ``active`` is False and callers return the text instead. Edits run on ``submit`` (a worker)."""

    def __init__(self, ctx: Any, *, interval: float = CARD_MIN_INTERVAL_S, clock: Callable[[], float] = time.monotonic,
                 submit: Callable[[Callable[[], None]], Any] | None = None,
                 timer: Callable[[float, Callable[[], None]], Any] | None = None):
        self.ctx = ctx
        self.interval = interval
        self.clock = clock
        self.submit = submit or (lambda fn: fn())
        self.timer = timer or _start_timer
        self.handle: Any = None
        self.error: str | None = None
        self.last_text = ""
        self.edits = 0
        self._last_edit = -1e9
        self._pending: str | None = None
        self._timer_armed = False
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        return self.handle is not None and bool(getattr(self.handle, "ok", False))

    def post(self, text: str) -> bool:
        self.last_text = text
        try:
            handle = self.ctx.post_message(text)
        except Exception as exc:  # noqa: BLE001 - post_message never raises in v0.23.0, be safe anyway
            self.handle, self.error = None, f"post_failed: {exc}"[:200]
            return False
        if getattr(handle, "ok", False):
            self.handle, self.error = handle, None
            self._last_edit = self.clock()
            return True
        self.handle, self.error = None, str(getattr(handle, "error", None) or "unavailable")
        return False

    def update(self, text: str, *, force: bool = False) -> None:
        if not self.active or text == self.last_text:
            return
        with self._lock:
            wait = self.interval - (self.clock() - self._last_edit)
            if force or wait <= 0:
                self._pending = None
                self._last_edit = self.clock()
                self.last_text = text
                send = text
            else:
                self._pending = text
                if self._timer_armed:
                    return
                self._timer_armed = True
                self.timer(wait, self._flush)
                return
        self.submit(lambda: self._edit(send))

    def _flush(self) -> None:
        with self._lock:
            self._timer_armed = False
            text, self._pending = self._pending, None
            if text is None or text == self.last_text:
                return
            self._last_edit = self.clock()
            self.last_text = text
        self.submit(lambda: self._edit(text))

    def _edit(self, text: str) -> None:
        try:
            self.handle.edit(text)
            self.edits += 1
        except Exception:  # noqa: BLE001
            log.debug("popquiz: card edit failed", exc_info=True)


def _start_timer(delay: float, fn: Callable[[], None]) -> Any:
    t = threading.Timer(max(0.0, delay), fn)
    t.daemon = True
    t.start()
    return t


def card_text(status: dict, *, degraded: bool = False) -> str:
    state = status.get("state", "idle")
    room = status.get("room", "")
    title = status.get("title") or "Pop Quiz"
    urls = status.get("urls") or {}
    lines = [f"Pop Quiz {room}: {title}"]
    if state == "lobby":
        lines.append(f"Lobby: {status.get('players', 0)} player(s) in. Waiting for the host to start.")
    elif state in ("question", "reveal", "leaderboard"):
        lines.append(f"Question {status.get('question', 0)}/{status.get('total', 0)} ({state}), "
                     f"{status.get('players', 0)} player(s)")
    elif state == "finished":
        lines.append(f"Finished: {status.get('players', 0)} player(s), {status.get('total', 0)} questions.")
    lines.append(f"Model spend: ${float(status.get('spent_usd') or 0):.4f} of ${float(status.get('budget_usd') or 0):.2f}")
    if degraded:
        lines.append("Models: main model only (grant llm.model_override and llm.provider_override for more).")
    if urls.get("screen"):
        lines.append(f"Screen: {urls['screen']}")
    if urls.get("join") and state != "finished":
        lines.append(f"Join: {urls['join']}" + ("" if status.get("lan_open") else " (loopback; open to phones first)"))
    if state == "finished" and urls.get("report"):
        lines.append(f"Report: {urls['report']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------- app

class PopQuizApp:
    """State behind the tools and the command: drafts, the one QuizServer, the card, report delivery."""

    def __init__(self, ctx: Any, *, server_factory: Callable[..., Any] | None = None,
                 llm_call: Callable | None = None, bots: list[BotSpec] | None = None,
                 settings_override: dict | None = None, use_card: bool = True,
                 pricing_loader: Callable[..., Any] | None = None, inject: bool = True):
        self.ctx = ctx
        self._server_factory = server_factory
        self._llm_call_override = llm_call
        self._bots_override = bots
        self._settings_override = dict(settings_override or {})
        self._use_card = use_card
        self._inject_enabled = inject
        self._pricing_loader = pricing_loader
        self.server: Any = None
        self.drafts: dict[str, dict] = {}
        self.card: ChatCard | None = None
        self.degraded = False
        self.report_delivered: str | None = None  # "injected" | "pending" | None
        self.events: list[dict] = []
        self.event_listeners: list[Callable[[dict], None]] = []
        self._session_ctx: contextvars.Context | None = None
        self._worker: concurrent.futures.ThreadPoolExecutor | None = None
        self._llm_pool: concurrent.futures.ThreadPoolExecutor | None = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ helpers

    def settings(self) -> dict:
        s = read_settings(self.ctx)
        s.update(self._settings_override)
        return s

    def _submit(self, fn: Callable[[], None]) -> None:
        if self._worker is None:
            self._worker = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="popquiz-chat")
        session_ctx = self._session_ctx
        self._worker.submit(session_ctx.run, fn) if session_ctx is not None else self._worker.submit(fn)

    def _ensure_server(self, settings: dict, llm_call: Callable | None, pricing: Any) -> Any:
        srv = self.server
        if srv is not None and getattr(srv, "running", False):
            srv.llm_call, srv.pricing = llm_call, pricing
            return srv
        if self._server_factory is not None:
            factory = self._server_factory
        else:
            from .server import QuizServer as factory
        srv = factory(host=settings["host"], port=settings["port"], llm_call=llm_call, pricing=pricing,
                      on_event=self.on_event, public_url=settings["public_url"] or None)
        srv.start()
        self.server = srv
        return srv

    # ------------------------------------------------------------------ tool: popquiz_create

    def create(self, args: dict) -> dict:
        args = args if isinstance(args, dict) else {}
        questions = args.get("questions")
        if not isinstance(questions, list) or not questions:
            return {"error": "bad_arguments", "detail": "questions must be a non-empty array"}
        if len(questions) > MAX_QUESTIONS:
            return {"error": "too_many_questions", "detail": f"at most {MAX_QUESTIONS} questions"}
        draft_id = str(args.get("draft_id") or "").strip()
        with self._lock:
            draft = self.drafts.get(draft_id) if draft_id else None
            if draft_id and draft is None:
                return {"error": "unknown_draft", "detail": "start again without draft_id and with doc_path"}
            if draft is None:
                doc_path = str(args.get("doc_path") or "").strip()
                if not doc_path:
                    return {"error": "bad_arguments", "detail": "doc_path is required for a new quiz"}
                try:
                    doc = self._load(doc_path)
                except GameError as exc:
                    return {"error": exc.code, "detail": getattr(exc, "detail", "") or str(exc)}
                mode = str(args.get("mode") or self.settings()["mode"])
                draft_id = "d" + secrets.token_hex(3)
                draft = {"doc": doc, "slots": [], "pending": [], "attempt": 0,
                         "title": str(args.get("title") or doc.title or "Pop Quiz")[:120],
                         "mode": mode if mode in ("open_book", "closed_book") else "open_book"}
                self.drafts[draft_id] = draft
            draft["attempt"] += 1
            v = validate_questions(questions, draft["doc"].text)
            accepted_by_index = {q.index: q for q in v.accepted}
            slots, pending = draft["slots"], draft["pending"]
            if draft["attempt"] == 1:
                for i in range(len(questions)):
                    slots.append(accepted_by_index.get(i))
                    if i not in accepted_by_index:
                        pending.append(i)
            else:
                # Fixed questions fill the refused slots in order; extra ones are appended.
                still: list[int] = []
                for i in range(len(questions)):
                    q = accepted_by_index.get(i)
                    slot = pending.pop(0) if pending else None
                    if slot is None:
                        slots.append(q)
                        if q is None:
                            still.append(len(slots) - 1)
                    else:
                        slots[slot] = q
                        if q is None:
                            still.append(slot)
                pending[:0] = still
                pending.sort()
            kept = [q for q in slots if q is not None]
            if v.rejected and draft["attempt"] < MAX_ATTEMPTS:
                return {
                    "status": "needs_fix", "draft_id": draft_id, "accepted": sorted(accepted_by_index),
                    "rejected": v.rejected, "warnings": v.warnings, "kept": len(kept),
                    "attempt": draft["attempt"], "max_attempts": MAX_ATTEMPTS,
                    "next": ("Fix ONLY the rejected questions: copy source_quote word for word from the document "
                             "(closest_passage shows the nearest text found), keep numbers identical, then call "
                             f"popquiz_create again with draft_id=\"{draft_id}\" and only the fixed questions, "
                             "in the same order as rejected."),
                }
            dropped = len(pending)
            if len(kept) < MIN_QUESTIONS:
                if v.rejected:
                    return {"status": "needs_fix" if draft["attempt"] < MAX_ATTEMPTS else "failed",
                            "draft_id": draft_id, "rejected": v.rejected, "kept": len(kept),
                            "attempt": draft["attempt"], "max_attempts": MAX_ATTEMPTS,
                            "detail": f"at least {MIN_QUESTIONS} anchored questions are needed"}
                return {"status": "needs_more", "draft_id": draft_id, "kept": len(kept),
                        "attempt": draft["attempt"], "max_attempts": MAX_ATTEMPTS,
                        "detail": f"at least {MIN_QUESTIONS} anchored questions are needed; send more with this draft_id"}
            out = self._start_game(draft, kept)
            if "error" not in out:
                self.drafts.pop(draft_id, None)
                out["draft_id"] = draft_id
                out["attempt"] = draft["attempt"]
                if v.warnings:
                    out["warnings"] = v.warnings
                if dropped:
                    out["dropped_after_max_attempts"] = dropped
            return out

    def _load(self, doc_path: str):
        from .ingest import load_document

        roots = allowed_roots(self.ctx)
        doc_path = resolve_doc_path(doc_path, self.ctx)
        check_document_path(doc_path, roots)
        return load_document(doc_path, roots)

    def _start_game(self, draft: dict, questions: list) -> dict:
        settings = self.settings()
        for q in questions:
            q.time_limit_s = settings["time_limit_s"]
        if self._bots_override is not None:
            bots, self.degraded = list(self._bots_override), False
        else:
            bots, self.degraded = resolve_bots(self.ctx, settings)
        if self._llm_call_override is not None:
            llm_call = self._llm_call_override
        else:
            if self._llm_pool is None:
                self._llm_pool = concurrent.futures.ThreadPoolExecutor(max_workers=8,
                                                                       thread_name_prefix="popquiz-llm")
            llm_call = make_llm_call(self.ctx, self._llm_pool)
        loader = self._pricing_loader or (lambda models: load_pricing_cached(self.ctx, models))
        pricing = loader([b.model for b in bots])
        try:
            srv = self._ensure_server(settings, llm_call, pricing)
        except GameError as exc:
            return {"error": exc.code, "detail": f"cannot listen on {settings['host']} port {settings['port']}; "
                                                 "change plugins.entries.popquiz.settings.port"}
        mode = Mode.CLOSED_BOOK if draft["mode"] == "closed_book" else Mode.OPEN_BOOK
        doc = draft["doc"]
        if draft.get("title"):
            doc.title = draft["title"]
        info = srv.create_game(questions=questions, bots=bots, doc=doc, mode=mode, budget_usd=settings["budget_usd"],
                               auto_advance=settings["auto_advance"])
        self.report_delivered = None
        self._session_ctx = contextvars.copy_context()
        urls = dict(info.get("urls") or {})
        result = {
            "status": "ready", "room": info.get("room"), "questions": len(questions), "mode": mode.value,
            "urls": {k: urls[k] for k in ("screen", "join", "host") if k in urls},
            "players": list(info.get("players") or []), "estimated_cost_usd": info.get("estimated_cost_usd"),
            "budget_usd": settings["budget_usd"], "degraded": self.degraded,
            "next": ("Tell the host: open the screen URL on the projector, then /popquiz (or popquiz_control) "
                     "open_lan so phones can join, then start. The host URL works only on this machine. "
                     "When the game ends the report comes back into this conversation."),
        }
        if self.degraded:
            result["note"] = ("Model overrides are not granted, so only your main model plays. Grant "
                              "llm.model_override and llm.provider_override to the popquiz plugin for 3 models.")
        self.card = None
        if self._use_card:
            card = ChatCard(self.ctx, submit=self._submit)
            text = card_text({**srv.status(), "urls": urls}, degraded=self.degraded)
            if card.post(text):
                self.card = card
                result["card"] = "posted"
            else:
                result["card"] = f"unavailable ({card.error}); show the URLs to the user"
        return result

    # ------------------------------------------------------------------ tools: control, status, report

    def control(self, action: str) -> dict:
        action = str(action or "").strip().lower()
        alias = {"open": "open_lan", "lan": "open_lan", "stop": "end", "finish": "end"}
        action = alias.get(action, action)
        if action not in ("open_lan", "start", "next", "skip", "end"):
            return {"error": "bad_action", "detail": "use open_lan, start, next, skip or end"}
        srv = self.server
        if srv is None or not getattr(srv, "running", False):
            return {"error": "no_game", "detail": "create a quiz first with popquiz_create"}
        try:
            st = srv.host_command(action)
        except GameError as exc:
            return {"error": exc.code, "detail": getattr(exc, "detail", "")}
        self._refresh_card(st)
        return {"ok": True, "action": action, "status": _public_status(st)}

    def status(self) -> dict:
        srv = self.server
        if srv is None or not getattr(srv, "running", False):
            return {"state": "idle", "detail": "no quiz running"}
        st = _public_status(srv.status())
        st["degraded"] = self.degraded
        st["report"] = self.report_delivered
        return st

    def report_markdown(self) -> str | None:
        srv = self.server
        rep = srv.report() if srv is not None else None
        return getattr(rep, "markdown", None) if rep is not None else None

    def report(self) -> dict:
        md = self.report_markdown()
        if not md:
            st = self.status()
            return {"error": "not_finished", "detail": "the report exists once the game is finished",
                    "state": st.get("state")}
        url = ((self.server.urls() or {}).get("report") if self.server is not None else None)
        return {"report_markdown": md, "report_url": url,
                "next": "Summarize what the room misunderstood and propose remediation (doc fixes, a follow-up quiz)."}

    # ------------------------------------------------------------------ server events (server thread)

    def on_event(self, event: dict) -> None:
        self.events.append(event)
        for listener in list(self.event_listeners):
            try:
                listener(event)
            except Exception:  # noqa: BLE001
                log.debug("popquiz: event listener failed", exc_info=True)
        t = event.get("t")
        srv = self.server
        if t == "stopped" or srv is None:
            return
        try:
            st = srv.status()
        except Exception:  # noqa: BLE001
            return
        if self.card is not None:
            self.card.update(card_text(st, degraded=self.degraded), force=(t == "finished"))
        if t == "finished":
            self._submit(self._deliver_report)

    def _refresh_card(self, st: dict) -> None:
        if self.card is not None and isinstance(st, dict) and st.get("state"):
            self.card.update(card_text(st, degraded=self.degraded))

    def _deliver_report(self) -> None:
        md = self.report_markdown()
        if not md:
            return
        text = ("[Pop Quiz finished. Report below. Explain what the room misunderstood and propose "
                "remediation.]\n\n" + md)
        ok = False
        if self._inject_enabled:
            try:
                ok = bool(self.ctx.inject_message(text, role="user"))
            except Exception:  # noqa: BLE001
                ok = False
        self.report_delivered = "injected" if ok else "pending"
        if not ok and self.card is not None and self.card.active:
            self.card.update(self.card.last_text + "\nReport ready: ask me for the quiz report (popquiz_report).",
                             force=True)

    # ------------------------------------------------------------------ slash command

    def command(self, raw_args: str = "") -> str:
        arg = (raw_args or "").strip().lower().split()
        word = arg[0] if arg else ""
        if not word:
            options = self.menu()
            if not options:
                return ("No quiz yet. Send a document and ask for a pop quiz: the agent writes the questions "
                        "and calls popquiz_create.")
            st = self.status()
            question = (f"Pop Quiz {st.get('room', '')}: {st.get('state')}, "
                        f"{st.get('players', 0)} player(s). What next?")
            try:
                picked = self.ctx.ask_choice(question, options)
            except Exception:  # noqa: BLE001
                picked = None
            if not picked:
                return "No choice made. Usage: /popquiz [open|start|next|skip|end|status|report]"
            word = picked
        if word in ("status", "s"):
            return _status_line(self.status())
        if word == "report":
            md = self.report_markdown()
            return md or "The report is available once the game is finished."
        out = self.control(word)
        if "error" in out:
            return f"Pop Quiz: {out['error']} ({out.get('detail', '')})"
        line = _status_line(out["status"])
        if out["action"] == "open_lan":
            join = (out["status"].get("urls") or {}).get("join", "")
            line += f"\nPhones can join at {join}"
        return line

    def menu(self) -> list[dict]:
        st = self.status()
        state = st.get("state")
        if state in (None, "idle"):
            return []
        opts: list[dict] = []
        if not st.get("lan_open") and state != "finished":
            opts.append({"value": "open", "label": "Open to phones", "description": "Listen on the LAN for phones"})
        if state == "lobby":
            opts.append({"value": "start", "label": "Start", "description": "First question"})
        if state == "question":
            opts.append({"value": "skip", "label": "Skip", "description": "Close this question now"})
        if state in ("reveal", "leaderboard"):
            opts.append({"value": "next", "label": "Next question"})
        if state != "finished":
            opts.append({"value": "end", "label": "End the game"})
        else:
            opts.append({"value": "report", "label": "Show the report"})
        opts.append({"value": "status", "label": "Status"})
        return opts

    # ------------------------------------------------------------------ lifecycle

    def shutdown(self) -> None:
        srv, self.server = self.server, None
        if srv is not None:
            try:
                srv.stop()
            except Exception:  # noqa: BLE001
                log.debug("popquiz: stop failed", exc_info=True)
        for pool in (self._worker, self._llm_pool):
            if pool is not None:
                pool.shutdown(wait=False, cancel_futures=True)
        self._worker = self._llm_pool = None


def _public_status(st: dict) -> dict:
    st = dict(st or {})
    urls = dict(st.get("urls") or {})
    urls.pop("host", None)
    st["urls"] = urls
    st.pop("host_token", None)
    return st


def _status_line(st: dict) -> str:
    state = st.get("state", "idle")
    if state == "idle":
        return "Pop Quiz: no game running."
    parts = [f"Pop Quiz {st.get('room', '')}: {state}"]
    if state in ("question", "reveal", "leaderboard"):
        parts.append(f"question {st.get('question', 0)}/{st.get('total', 0)}")
    parts.append(f"{st.get('players', 0)} player(s)")
    parts.append(f"spent ${float(st.get('spent_usd') or 0):.4f}")
    parts.append("LAN open" if st.get("lan_open") else "loopback only")
    return ", ".join(parts)


def tool_json(fn: Callable[[], dict]) -> str:
    try:
        out = fn()
    except GameError as exc:
        out = {"error": exc.code, "detail": getattr(exc, "detail", "")}
    except Exception as exc:  # noqa: BLE001 - tools return errors, never raise
        log.exception("popquiz tool failed")
        out = {"error": "internal_error", "detail": f"{type(exc).__name__}: {exc}"[:300]}
    return json.dumps(out, ensure_ascii=False, default=str)
