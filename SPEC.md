# Pop Quiz: the room vs the models (SPEC)

Authority order for this repo: SPEC > tests > code. Research behind every decision:
`../_research/R2-popquiz.md` (architecture, APIs with file:line), `../_research/R1-manuel-plugin.md`
(plugin packaging, verified skeleton), `../_research/analyse-2026-11-06/1-faisabilite-technique.md`
and `5-soumission-revue.md` (scanner rules, review reasons). Target: Hermes Agent v0.23.0
(`requires_hermes: ">=0.23.0"`), Python >= 3.11 (Hermes runs 3.14).

## 1. Product in one paragraph

Any document becomes, in about a minute, a live multiplayer quiz for a whole room. The Hermes agent
reads the document and writes the questions; the plugin refuses every question whose `source_quote`
is not found verbatim in the document, so the agent fixes them (visible "refused, then corrected"
loop). Humans join from their phones with a QR code (no app, no account). Three AI models from the
Nous Portal play the same questions on the same clock, with latency and cost on the projector.
"The Room" plays as a team (majority vote of the humans). At the end the report says what the humans
misunderstood and which model understood the document best, for what cost; it goes back into the
conversation so the agent can propose remediation. First user: a team testing itself on its own docs
(runbook, product docs). Second: a teacher.

## 2. Repository layout (repo root IS the plugin directory)

```
plugin.yaml          manifest (see section 9)
pyproject.toml       [project].dependencies = plugin runtime deps; dev deps for tests
__init__.py          register(ctx): tools, /popquiz, CLI, skill, on_unload  (PQ-C)
model.py             dataclasses and enums (PQ-A)
anchor.py            normalization + quote anchoring + question validation (PQ-A)
ingest.py            path -> Document (PQ-A)
engine.py            pure game state machine, injectable clock, scoring, The Room (PQ-A)
bots.py              model players over an injected async LlmCall (PQ-A)
cassette.py          record/replay LlmCall for CI and the no-cost demo (PQ-A)
pricing.py           Portal public price list + embedded dated fallback (PQ-A)
report.py            Markdown + CSV + data report (PQ-A)
sample/              sample document, sample questions, sample cassette (PQ-A)
net.py               LAN IPv4 detection via psutil (PQ-B)
server.py            aiohttp app in a dedicated thread with its own loop (PQ-B)
conductor.py         async game loop: timers, bots, broadcasts (PQ-B)
robots.py            simulated phone players over WebSocket (demo, load bench) (PQ-B)
static/              join.html play.html screen.html host.html report.html *.js style.css (PQ-B)
hermes_glue.py       ctx.llm adapter, chat card, inject, settings (PQ-C)
cli.py               `hermes popquiz demo|bench|serve` (PQ-C)
skills/host-a-quiz/SKILL.md  usage skill for the agent (PQ-C)
desktop/plugin.js    bonus: Desktop pane wrapping /screen in SandboxedFrame (PQ-C)
tests/               pytest; core tests never import Hermes
scripts/             loadtest, record-cassette (outside runtime path)
README.md            catalog page: Try it, Verify in 5 minutes, disclosures, limits
```

Rules for every module except `__init__.py`, `hermes_glue.py`, `cli.py`: **no import of any Hermes
module** (keeps them testable with plain pytest and avoids private-import lint). Relative imports
only (`from .model import Question`). The plugin is imported by Hermes as package
`hermes_plugins.popquiz`; tests load it as package `popquiz_plugin` (see `tests/conftest.py`).

## 3. Data model (`model.py`)

```python
class GameState(str, Enum): LOBBY="lobby"; QUESTION="question"; REVEAL="reveal"; LEADERBOARD="leaderboard"; FINISHED="finished"
class Mode(str, Enum): OPEN_BOOK="open_book"; CLOSED_BOOK="closed_book"

@dataclass
class Question:
    index: int
    prompt: str
    choices: list[str]          # 2..4, distinct after normalization
    answer_index: int
    source_quote: str
    explanation: str = ""
    anchor_score: float = 1.0
    quote_offset: int = -1      # char offset in normalized doc, -1 unknown
    time_limit_s: int = 20

@dataclass
class Player:
    id: str                     # short random id
    nickname: str
    token_hash: str             # sha256 hex of the player token; token itself never stored
    joined_at: float
    connected: bool = True
    score: int = 0
    streak: int = 0

@dataclass
class BotSpec:
    id: str                     # "bot1".. stable
    label: str                  # display name, e.g. "Claude Opus 5.5"
    provider: str               # "nous"
    model: str                  # Portal id, e.g. "anthropic/claude-opus-5.5"

@dataclass
class Answer:
    actor_id: str               # player id, bot id, or "room"
    actor_kind: str             # "human" | "bot" | "room"
    q: int
    choice: int | None          # None = no answer / timeout / invalid
    received_at: float
    latency_ms: int
    correct: bool
    points: int
    meta: dict = field(default_factory=dict)   # bots: served_model, tokens, cost_usd, error

@dataclass
class Document:
    title: str
    text: str                   # extracted plain text / markdown
    path: str
    sha256: str
    kind: str                   # "txt" | "md" | "html" | "pdf" | "docx" | "epub"
```

`GameError(Exception)` carries `.code` (snake_case string). Codes used below are part of the contract.

## 4. Anchoring and validation (`anchor.py`)

- `normalize(text: str) -> str`: NFKC; typographic quotes, dashes, non-breaking spaces, ligatures to
  ASCII; join hyphenated line breaks `(\w)-\s*\n\s*(\w)`; drop U+00AD; strip Markdown emphasis/heading
  markers; casefold; collapse whitespace. Pure and idempotent.
- `anchor(quote: str, doc_norm: str) -> AnchorResult(ok: bool, score: float, code: str|None, offset: int, closest: str)`:
  normalized quote shorter than 12 chars: `quote_too_short`. Exact `find`: score 1.0. Else sliding
  window seeded by the 3 rarest words, `difflib.SequenceMatcher(autojunk=False)`, score = matched chars /
  len(quote); accept at >= 0.90. **Number guard**: every number in the quote must appear in the chosen
  window, else `number_mismatch` (regression case: doc "30 days", quote "90 days" scores 0.985 without
  the guard). Not found: `quote_not_found` with `closest` (<= 160 chars) and `score`.
- `validate_questions(raw: list[dict], doc_text: str) -> Validation(accepted: list[Question], rejected: list[dict], warnings: list[dict])`.
  Per question checks in order: schema (prompt <= 240 chars, 2..4 choices <= 90 chars each,
  answer_index in range) `bad_schema`; duplicate choices after normalization `duplicate_choice`;
  correct choice text contained in the prompt `prompt_leaks_answer`; anchoring codes above.
  Warning only (not rejection): `answer_not_in_quote` when no significant word (>= 4 letters) of the
  correct choice appears in the quote. Rejected entry shape:
  `{"index": i, "code": str, "detail": str, "best_score": float|None, "closest_passage": str|None}`.
- Must handle 10 pages (~50k chars) in well under 1 s total for 20 questions.

## 5. Ingestion (`ingest.py`)

`load_document(path: str, allowed_roots: list[Path], max_bytes: int = 20_000_000, max_chars: int = 400_000) -> Document`.
- Resolve the real path; refuse if not inside one of `allowed_roots` (`path_not_allowed`), symlink
  escaping a root (`path_not_allowed`), missing (`not_found`), too big (`too_large`).
- `.txt`/`.md`/no extension: UTF-8 with replacement. `.html`/`.htm`: stdlib `html.parser` text
  extraction (drop script/style). `.pdf`/`.docx`/`.epub`: `anydoc` (a Hermes core dependency) if
  importable, else `GameError("unsupported_format")` with a hint; empty extraction (scanned PDF):
  `empty_document`. Title: first Markdown heading, else file stem. Truncate to `max_chars` and flag it.
- Allowed roots are decided by the glue (Hermes document cache dir, session cwd, plugin sample dir).

## 6. Engine (`engine.py`): pure, synchronous, no I/O, no asyncio

```python
class Game:
    def __init__(self, *, game_id: str, room_code: str, questions: list[Question], bots: list[BotSpec],
                 clock: Callable[[], float], mode: Mode = Mode.OPEN_BOOK, title: str = "",
                 doc_sha256: str = "", budget_usd: float = 1.0, max_players: int = 200,
                 reveal_s: float = 6.0)
    state: GameState; current: int  # -1 before start
    question_started_at: float | None; question_deadline: float | None
    players: dict[str, Player]; answers: dict[int, list[Answer]]; spent_usd: float
    def add_player(self, nickname: str) -> tuple[Player, str]      # returns (player, token)
    def resume(self, token: str) -> Player                          # bad_token
    def set_connected(self, player_id: str, connected: bool) -> None
    def start(self) -> dict                                         # LOBBY->QUESTION(0); needs >=1 question
    def submit_answer(self, player_id: str, q: int, choice: int) -> Answer
    def submit_bot_answer(self, bot_id: str, q: int, choice: int | None, latency_ms: int, meta: dict) -> Answer
    def everyone_answered(self) -> bool    # all connected humans + all bots answered current q
    def close_question(self) -> dict       # QUESTION->REVEAL; adds The Room answer; returns reveal event
    def show_leaderboard(self) -> dict     # REVEAL->LEADERBOARD
    def next_question(self) -> dict        # LEADERBOARD (or REVEAL) -> QUESTION(i+1) or FINISHED
    def end(self) -> dict                  # any -> FINISHED
    def public_view(self) -> dict          # safe for screen/players: never tokens, never answer before reveal
    def host_view(self) -> dict            # adds counts, spent, bots status
```

- Nickname: strip, remove control chars, 1..24 chars, unique case-insensitively (suffix " 2", " 3").
  Errors: `bad_nickname`, `game_full`, `game_started` (joining after start is ALLOWED: late joiners
  play remaining questions; only FINISHED refuses with `game_finished`).
- Token: `secrets.token_urlsafe(18)`; store sha256 only; compare with `hmac.compare_digest`.
- Answer errors: `wrong_state`, `wrong_question`, `already_answered`, `too_late` (received after
  deadline + 0.5 s grace), `bad_choice`.
- Scoring (humans, bots and The Room alike): correct -> `round(1000 * (1 - 0.5 * min(t, T) / T))`
  where t = seconds since question start (server clock) and T = time limit; wrong/none -> 0.
  Streak +1 on correct, reset on wrong (display only, no bonus).
- The Room: on `close_question`, majority choice among human answers of this question; tie -> the
  choice chosen first in time among the tied; no human answers -> choice None. Room latency = median
  latency of humans who picked the room's choice. Stored as an Answer with actor_id "room".
- Bot answers arriving after close are recorded with `meta["late"]=True`, 0 points, still shown.
- `spent_usd` accumulates `meta["cost_usd"]` of bot answers; the conductor stops calling the most
  expensive bot when the budget would be exceeded (bot answer recorded with error `budget`).
- Events returned by transitions are plain dicts with key `t` (see section 8) so the server can
  broadcast them unchanged.

## 7. Model players (`bots.py`, `cassette.py`, `pricing.py`)

```python
@dataclass
class LlmReply:
    parsed: dict | None; text: str; model: str
    input_tokens: int = 0; output_tokens: int = 0; cache_read_tokens: int = 0; cache_write_tokens: int = 0

LlmCall = Callable[[BotSpec, str, str, dict, float], Awaitable[LlmReply]]
#        (spec, instructions, input_text, json_schema, timeout_s)

@dataclass
class BotResult:
    bot_id: str; choice: int | None; latency_ms: int; served_model: str
    input_tokens: int; output_tokens: int; cache_read_tokens: int; cache_write_tokens: int
    cost_usd: float; error: str | None   # None | "timeout" | "invalid_json" | "fallback" | "trust_denied" | "budget" | "error"

def build_prompt(question: Question, doc_text: str, mode: Mode) -> tuple[str, str]
ANSWER_SCHEMA = {"type": "object", "properties": {"choice": {"type": "string", "enum": ["A","B","C","D"][:n]}}, "required": ["choice"]}
async def ask_bot(spec, question, doc_text, mode, llm_call, pricing, timeout_s=20.0) -> BotResult
async def ask_bots(specs, question, doc_text, mode, llm_call, pricing, timeout_s=20.0) -> list[BotResult]  # gather, never raises
```

- The model sees only the prompt and labelled choices A..D (never answer_index, quote, explanation).
  Open book: document first (stable prefix, cache friendly), then the question. Closed book: question only.
- `asyncio.wait_for(..., timeout_s + 1)`; timeout -> error "timeout". Exceptions whose class name is
  `PluginLlmTrustError` -> "trust_denied". Missing/invalid choice -> "invalid_json". If
  `reply.model` differs from `spec.model` (ignoring provider prefix and case) -> keep the answer but
  set error "fallback" (shown as "fallback model" on screen, still scored).
- `pricing.Pricing`: `Pricing.from_catalog(json_dict)`, `Pricing.fallback()` (embedded table dated
  2026-11-07 for at least the 3 default models and claude-haiku-5.5, gpt-oss-120b, grok-4.3),
  `price_for(model) -> ModelPrice|None`, `cost(model, LlmReply) -> float` =
  in*prompt + out*completion + cache_read*input_cache_read + cache_write*input_cache_write, per token.
  `fetch_catalog(timeout=5)` uses urllib on `https://inference-api.nousresearch.com/v1/models` (public,
  no key); the glue caches it 24 h. Never raise to the caller: fall back.
  Defaults verified 2026-11-07 ($ per million tokens, in/out): `anthropic/claude-opus-5.5` 4.00/20.00
  (cache read 0.20, write 5.00); `deepseek/deepseek-v4-flash` 0.091/0.182; `inception/mercury-3` 0.10/0.35.
- `cassette.CassetteLlm(path, mode="replay"|"record", inner: LlmCall|None, speed: float = 1.0)`:
  key = sha256 of (model, mode, prompt, choices). Replay sleeps the recorded latency / speed and
  returns the recorded reply; missing key -> reply with parsed None (counted invalid) and a logged
  warning. JSON file format documented in the module docstring.
- `sample/`: `runbook.md` (an original fictional incident runbook, ~1 500 words, numbers inside such
  as retention days and thresholds), `questions.json` (8 valid questions + 2 deliberately wrong ones
  used by tests and the demo's "refused then corrected" step), `cassette.json` (replies for the 3
  default models on the 8 questions, realistic latencies: mercury < 1.5 s, deepseek 2 to 5 s, opus 6
  to 16 s, with opus wrong on 1 and deepseek wrong on 2).

## 8. Server, protocol, frontend (`server.py`, `conductor.py`, `net.py`, `robots.py`, `static/`)

Threading: `QuizServer` owns a daemon thread running its own asyncio loop and an aiohttp
`AppRunner`. API (thread-safe, callable from any thread):
```python
class QuizServer:
    def __init__(self, *, host="127.0.0.1", port=8765, llm_call=None, pricing=None, on_event=None)
    def start(self) -> None            # binds loopback; raises GameError("port_in_use")
    def stop(self, timeout=5.0) -> None
    def create_game(self, *, questions, bots, doc: Document, mode, budget_usd, auto_advance=False) -> dict  # returns host info
    def host_command(self, cmd: str) -> dict    # open_lan | start | next | skip | end
    def status(self) -> dict
    def urls(self) -> dict             # host (loopback, token in #fragment), screen, join (LAN if open)
    def report(self) -> Report | None
```
`on_event(event: dict)` is called from the server thread for state changes (glue uses it to edit the
chat card and to deliver the report). One active game at a time in the MVP.

Routes:
- `GET /` join page (room code + nickname), `GET /play`, `GET /screen?room=CODE` (read-only
  projector), `GET /host` (token only in the URL fragment), `GET /report?room=CODE` (after FINISHED),
  `GET /qr.svg?room=CODE` (segno SVG of the join URL; if segno missing return 404 and the screen shows
  the URL and code in large type), `GET /health` -> `{"ok": true}`, `GET /static/*`.
- `GET /ws` WebSocket for players, screen and host. `POST /api/host/<cmd>` same commands.
- Host access requires BOTH the host token (`Authorization: Bearer` or WS `host_auth`) AND a loopback
  peer address. Tokens compared with `hmac.compare_digest`.

WebSocket messages (JSON objects, key `t`):
- client -> server: `join{room, nickname}`, `resume{room, token}`, `answer{q, choice}`,
  `screen{room}`, `host_auth{token}`, `host{cmd}`, `ping`.
- server -> client: `welcome{player_id, token, nickname}`, `lobby{count, names[<=30]}`,
  `question{q, total, prompt, choices, ends_at_ms, server_now_ms}`,
  `answered{q, count, humans, bots_done[]}`,
  `reveal{q, answer_index, explanation, source_quote, distribution[], room{choice, correct},
  bots[{id, label, choice, correct, latency_ms, cost_usd, error}]}`,
  `leaderboard{top[{nickname, score}], room_vs_models[{id, label, score, correct}], you{rank, score}}`,
  `finished{report_url}`, `error{code}`, `pong`.
- Lobby counts and leaderboards are broadcast in batches (at most every 250 ms).

Conductor (async, in the server loop): on `start`/`next`: engine transition, broadcast `question`,
launch `ask_bots` immediately (bots run while humans answer), end the question at the deadline or
as soon as `everyone_answered()`, `close_question` -> broadcast `reveal` (bot results included even if
late), after `reveal_s` -> `leaderboard`; then wait for host `next` (or auto-advance after 6 s when
`auto_advance=True`, used by the demo). Bot answers are hidden from all clients until reveal.

Security (all are tested):
1. Bind 127.0.0.1 by default. `open_lan` adds a second site on the detected private LAN IPv4 (net.py,
   psutil, prefer the default-route interface; never all interfaces). Never write the all-interfaces
   address joined to a port, the C socket constant for "any address", or tunnel product names anywhere
   in code or docs (catalog scanner rule `bind_all_interfaces`, see `tools/skills_guard.py`). A
   user-provided `public_url` setting may replace the join URL in the QR.
2. No public route or public WS message ever contains the host token or another player's token
   (automated test over every route and message type).
3. Player tokens required for `answer`/`resume`; one answer per player per question; server clock only.
4. All text rendered with `textContent` in the browser; server-generated HTML escapes with
   `html.escape`. Headers on every response: `Content-Security-Policy: default-src 'self'; img-src 'self' data:; connect-src 'self'; style-src 'self'; frame-ancestors *`,
   `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`. No inline scripts, no CDN.
5. Limits: WS message <= 2 KB (`max_msg_size`), 200 players, 5 messages/s per connection, 10 joins
   per minute per IP, nickname rules from section 6, `Origin` must match the Host for state-changing
   WS messages (screen may send Origin null when embedded in the Desktop sandboxed frame).
6. The server stops at game end + 10 minutes idle, on `stop()`, and on plugin unload.

Frontend (`static/`, vanilla JS modules, no build step, English UI): polished, projector-first.
- screen: room code in huge type, QR, player names popping in, then question with a countdown ring,
  live "answered" counter, reveal with correct answer highlighted, the document quote, answer
  distribution bars, model cards (label, choice, check or cross, latency, cost), then leaderboard and a
  "The Room vs the models" bar race. Must look good at 1920x1080 and readable from 8 m.
- play (phone): nickname, then 2 to 4 big coloured buttons, lock after answering, result feedback,
  rank; reconnect transparently with the token kept in `localStorage` (fallback to memory).
- host: big buttons Open to phones / Start / Next / End, status, URLs, estimated and spent cost.

Robots (`robots.py`): `async def run_robots(base_url, room, n, accuracy=(0.4, 0.9), think=(1.0, 8.0), seed=None)`
connects n WebSocket players that answer with the given accuracy band; used by the demo and the bench.

## 9. Hermes integration (`__init__.py`, `hermes_glue.py`, `cli.py`)

Tools (declared exactly in `provides_tools`):
- `popquiz_create(doc_path, questions[], title?, mode?, draft_id?)` (schema in R2 section b). Returns
  `{"status": "needs_fix", "draft_id", "accepted": [...], "rejected": [...], "attempt", "max_attempts": 3}`
  or `{"status": "ready", "room", "urls": {...}, "players": [bot labels], "estimated_cost_usd", "next": "..."}`.
  Accepted questions are kept per `draft_id`, so the agent resends only the fixed ones.
- `popquiz_control(action)` with action in open_lan|start|next|skip|end; returns status.
- `popquiz_status()`; `popquiz_report()` returns the Markdown report (fallback when injection is off).
Command `/popquiz [open|start|next|end]`: SYNC handler. Without argument it calls `ctx.ask_choice`
with the actions that make sense in the current state; returns a short text. CLI:
`hermes popquiz demo [--robots 24] [--port 8765] [--open-lan]` (sample doc, sample questions incl. the
refused-then-corrected step printed to the terminal, cassette bots, robots, auto-advance; zero model
calls, zero gateway), `hermes popquiz bench [--players 60]` (prints p50/p95), `hermes popquiz serve`.
Chat card: in `popquiz_create` (agent turn, session bound) post one progress card with
`ctx.post_message`, keep the handle, and edit it from `on_event` at most every 2 s; if posting is
unavailable (CLI), fall back to returning text. Report delivery: `ctx.inject_message(report, role="user")`
when allowed, else tell the agent to call `popquiz_report`. Model calls: `hermes_glue.make_llm_call(ctx)`
runs the SYNC `ctx.llm.complete_structured(provider=spec.provider, model=spec.model, ...)` in a
thread executor (works from the server loop whatever loop ctx.llm binds to). Capabilities declared:
`llm.model_override`, `llm.provider_override`; if not granted, degrade to one bot = the main model.
Settings (`config_schema`): `host`, `port` (8765), `public_url`, `players` (list of `{label, model,
provider}`, default the 3 models of section 7), `budget_usd` (1.0), `time_limit_s` (20), `mode`
(open_book), `auto_advance` (false). Read settings at call time, never in `register()`.

## 10. Tests (target >= 120, all green, no network, no model calls)

Core: anchor (exact, hyphenation, ligatures, quotes, Markdown, number guard, short quote, threshold,
closest passage), validation codes, ingest (txt, md, html, pdf via anydoc when available else skip,
too large, outside roots, symlink escape), engine (every transition, wrong state, double answer,
late answer, scoring formula, The Room majority/tie/empty, late joiner, budget stop, public_view
never leaks answer before reveal nor tokens), bots (valid, invalid JSON, timeout, trust denied,
fallback model, cost), pricing (catalog parse, missing price, fallback), cassette (record/replay),
report (content, CSV, length < 4000 chars for chat).
Server: route security sweep, host routes refused from non-loopback and without token, XSS nickname,
oversized WS message, rate limit, Origin check, full game with 5 simulated clients + cassette bots,
reconnect with token keeps score, server start/stop leaves no thread and no open port.
Glue (with fake ctx): tool flow needs_fix then ready, command with ask_choice, degrade without
capabilities, card edits throttled. Bench script measures p95 with 60 robots.

## 11. Definition of done

`uv run --no-project --with pytest --with pytest-asyncio --with aiohttp --with segno pytest -q` green;
`hermes plugins validate .` passes with 0 warnings and verdict safe; `hermes plugins doctor . --ci`
exit 0; installed in the isolated home (`~/.cache/nous-build/H-popquiz`), a real game runs end to end;
README has Try it (demo in 2 minutes without Telegram), Verify in 5 minutes, Disclosures (listening
port, LAN, paid model calls with budget cap, document text sent to the chosen model providers,
public price-list fetch), Limits.

Style: no em dash character (U+2014) anywhere in this repo (code, comments, UI strings, docs).
