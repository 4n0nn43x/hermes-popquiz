# Pop Quiz: the room vs the models

A Hermes Agent plugin that turns any document into a live multiplayer quiz for the whole room in about a
minute. Hermes reads the document and writes the questions. The plugin refuses every question whose
`source_quote` is not found word for word in the document, so the agent fixes it (you see the "refused,
then corrected" loop). People join from their phones with a QR code: no app, no account. AI models from the
Nous Portal answer the same questions on the same clock, with their latency and cost on the projector, and
"The Room" plays as a team (majority vote). At the end, a report says what the humans misunderstood and which
model understood the document best, for what cost, and it goes back into the conversation so the agent can
propose fixes.

First users: a team testing itself on its own runbooks and product docs. Then a teacher and a class.

## Try it (no Telegram, no model call)

```bash
hermes plugins install 4n0nn43x/hermes-popquiz --enable
hermes popquiz demo
```

`install` shows two consent screens: the Python dependencies (`aiohttp`, `segno`) and the two model
capabilities (`llm.model_override`, `llm.provider_override`, used only for real model players). The demo
needs neither the capabilities nor a messaging app nor any credit.

The demo runs the whole product on a sample runbook: the agent's first draft with two wrong quotes (refused
with the reason and the closest passage), the corrected questions, then a game with 24 simulated phones and
three model players replayed from a cassette. Open the `Projector` URL it prints in a browser. A full run
(8 questions) took 188 s here; `--questions 3` is shorter. Options: `--robots N`, `--port`, `--open-lan`
(let real phones on your Wi-Fi join), `--browser`, `--speed`.

The model latencies and costs shown by the demo come from `sample/cassette.json`, a hand-written cassette
with realistic values; they are not measurements. `hermes popquiz demo --live` plays the same game with real
model calls (paid, see Disclosures).

### With your agent

Send a document (Telegram, Discord, Slack, the CLI or Desktop) and ask: "make a pop quiz of 8 questions on
this for my team". The bundled skill `popquiz:host-a-quiz` tells the agent how to work:

1. The agent reads the document and calls `popquiz_create` with questions that each carry a `source_quote`.
2. Refused questions come back with a code (`quote_not_found`, `number_mismatch`, ...) and the closest
   passage; the agent resends only the fixed ones with the same `draft_id`.
3. The quiz is ready: open the screen URL on the projector, then `/popquiz` (buttons) or ask the agent:
   "open it to phones", "start", "next", "end".
4. At the end the report comes back into the conversation and the agent proposes remediation.

Tested with a real agent turn (`hermes chat -q`, Hermes v0.23.0, a free Portal model): the agent read the
runbook, sent 4 questions, got `number_mismatch` on the one whose quote said "90 days" (the prompt asked it to
quote that one from memory, to show the check), resent only that question with the exact passage ("35 days"),
and the quiz was ready in 52 s.

## Verify in 5 minutes

From a clone of this repository:

| Check | Command | Result here (Hermes v0.23.0) |
|---|---|---|
| Tests (no network, no model) | `uv run --no-project --with pytest --with pytest-asyncio --with aiohttp --with segno pytest -q` | `318 passed, 1 skipped` (the skip is a real PDF that needs `anydoc`) |
| Catalog validator | `hermes plugins validate .` | `Validation passed`, `security scan - safe`, 0 warnings |
| Load check | `hermes plugins doctor . --ci` | `registrations: 4 tool(s), 0 hook(s)`, exit 0 |
| Whole product, no cost | `hermes popquiz demo --questions 3 --exit` | refused then corrected, game, report |
| Load bench | `hermes popquiz bench --players 60 200` | 60 phones: p50 15 ms, p95 16 ms; 200 phones: p50 25 ms, p95 37 ms (question to answer, server side, 1000/1000 answers) |
| Real models (paid) | `hermes popquiz demo --live --questions 3` | estimate printed before the game ($0.0493 for 3 questions on the sample) |

The anchoring check alone, on the sample's two wrong quotes, is the first step of any demo:

```
REFUSED q7 [number_mismatch] For how long are database backups kept?
   quote sent : "Backups are encrypted with AES-256 and kept for 90 days."
   closest doc: "backups are encrypted with aes-256 and kept for 35 days." (score 0.964)
REFUSED q8 [quote_not_found] Within how many business days is the written post-incident review due?
```

## Hermes surfaces used

| Surface | Where | Why |
|---|---|---|
| `ctx.llm.complete_structured(provider=, model=, json_schema=, timeout=)` | `hermes_glue.py:173` | Each model player answers with JSON, timed, in a worker thread |
| Capabilities `llm.model_override` + `llm.provider_override`, `ctx.has_capability` | `plugin.yaml`, `hermes_glue.py:130` | Consent screen; without both, only your main model plays |
| `ctx.register_tool` (4 tools) | `__init__.py:107-118` | `popquiz_create` (refuse and fix loop), `popquiz_control`, `popquiz_status`, `popquiz_report` |
| `ctx.register_command` + `ctx.ask_choice` | `__init__.py:123`, `hermes_glue.py:710` | `/popquiz` with buttons that fit the game state |
| `ctx.post_message` + `handle.edit` | `hermes_glue.py:327`, `:369` | One progress card in the chat, edited at most every 2 s |
| `ctx.inject_message(role="user")` | `hermes_glue.py:688` | The final report goes back into the conversation |
| `ctx.register_skill` | `__init__.py:131` | `skills/host-a-quiz/SKILL.md` |
| `ctx.register_cli_command` | `__init__.py:127` | `hermes popquiz demo / bench / serve` |
| `ctx.get_config`, `ctx.state`, `ctx.session_cwd`, `ctx.on_unload` | `hermes_glue.py:89`, `:198-225`, `:258`, `__init__.py:133` | Settings read at call time, 24 h price cache, allowed folder, clean stop |
| `hermes_constants.get_hermes_home` | `hermes_glue.py:243` | Profile-aware location of the document cache |
| Desktop SDK `SandboxedFrame` (opt-in) | `desktop/plugin.js` | Projector pane in Hermes Desktop |

## Disclosures: what it does on your computer

- **Listening port.** A web server (aiohttp, in its own thread) starts when a quiz is created, on
  `127.0.0.1:8765` (settings `host`, `port`). It stops at game end plus 10 minutes, on `popquiz_control end`
  followed by idle, and when the plugin unloads.
- **LAN, only on request.** "Open to phones" (`/popquiz open`, `popquiz_control open_lan`, or `--open-lan`)
  adds a second listener on the machine's private LAN address (the default-route interface), same port.
  It never listens on all interfaces. Anyone on that network who has the room code can join as a player with
  a nickname. Traffic is plain HTTP on the LAN. The host page and host commands require the host token and a
  loopback client, so phones cannot drive the game. The optional `public_url` setting only changes the URL
  in the QR code; the plugin opens no tunnel.
- **Paid model calls.** With the two capabilities granted, each question triggers one call per model player
  (default: `anthropic/claude-opus-5.5`, `deepseek/deepseek-v4-flash`, `inception/mercury-3`) through Hermes,
  on your Nous Portal account. `budget_usd` (default $1.00 per game) is checked before each question against
  the estimated cost; players that no longer fit are skipped, most expensive first. Costs are computed by the
  plugin from the token counts Hermes returns and the public price list, so they are estimates of what the
  provider bills. If the Portal refuses a call, Hermes may retry it on its fallback chain and then on your
  main model and provider; such answers are labelled "fallback model" and still count.
- **Document text sent to model providers.** In `open_book` mode (default) the full extracted text of the
  document goes to every model player with every question. In `closed_book` mode only the question and its
  choices are sent. The agent's own model reads the document as usual.
- **Public price list.** The plugin fetches `https://inference-api.nousresearch.com/v1/models` (public, no
  key) at most once a day and caches the prices in its plugin state; an embedded table dated 2026-11-07 is the
  fallback.
- **Files.** It reads only the document you name, and only under the session folder, the Hermes document cache
  or its own `sample/` folder; hidden files and the rest of the Hermes home are refused. It writes only its
  plugin state (the price cache). No telemetry, no account, no third-party service besides the model calls.
- **Conversation.** It posts one progress message and edits it; at game end it injects the report as a user
  message when Hermes allows it (otherwise the agent fetches it with `popquiz_report`).

## Settings

`plugins.entries.popquiz.settings.*`: `host` (127.0.0.1), `port` (8765), `public_url` (empty), `players`
(up to 4 `{label, model, provider}`), `budget_usd` (1.0), `time_limit_s` (20), `mode` (`open_book` or
`closed_book`), `auto_advance` (false). They are read each time a quiz is created.

## Limits

- One game at a time per Hermes process; up to 200 players.
- The anchoring check proves that the quote exists in the document, not that the right answer follows from
  it (a warning only, `answer_not_in_quote`).
- Phones must reach the machine on the local network; guest Wi-Fi with client isolation, or a firewall on
  the port, blocks them.
- Documents: txt, md, html directly; pdf, docx, epub through `anydoc` (a Hermes dependency); scanned PDFs are
  refused; text beyond 400 000 characters is cut.
- `open_book` cost grows with the document size (the whole text is sent with each question).
- In-process only: the chat card handle is bound to the gateway loop, so the plugin does not run in the
  isolated host.
- Model players need Portal credit for paid models; without it every model answer is an error and the game
  still runs for the humans.
- The Desktop pane (`desktop/plugin.js`, opt-in) frames the projector page in the SDK's sandboxed frame. In that
  frame the page's scripts load only if the server sends `Access-Control-Allow-Origin` on `/static/`, which it
  does not yet: until then the pane stays on "Connecting". Not tried inside the Desktop app.

## Team

- TODO(user): name, role, background, country.
- TODO(user): GitHub, X and LinkedIn links.
- TODO(user): past projects and hackathons.

## License

MIT, see `LICENSE`.
