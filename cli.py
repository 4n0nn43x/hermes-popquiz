"""`hermes popquiz demo|bench|serve` (SPEC section 9).

demo  : the whole product without Telegram, without a model call and without network: the sample
        runbook, a first draft with 2 wrong questions refused by the anchoring check, the corrected
        questions accepted, then a live game with cassette model players and robot phones, auto-advance,
        URLs printed (and the projector opened in a browser with --browser). With --live the model players
        are real paid calls through ctx.llm instead of the cassette (needs Hermes, capped by budget_usd).
bench : N robot phones against the real server thread, prints p50/p95 question-to-answer latency.
serve : host a quiz from a questions file by hand (host page), optional cassette or live models.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .hermes_glue import SAMPLE_DIR, PopQuizApp, card_text

LETTERS = "ABCD"


def setup_parser(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="popquiz_cmd", metavar="{demo,bench,serve}")
    demo = sub.add_parser("demo", help="Full game on the sample runbook: cassette models + robot players, no model call")
    demo.add_argument("--robots", type=int, default=24, help="simulated phone players (default 24)")
    demo.add_argument("--port", type=int, default=8765, help="listening port on 127.0.0.1 (default 8765, 0 = any)")
    demo.add_argument("--open-lan", action="store_true", help="also listen on the LAN address so real phones can join")
    demo.add_argument("--browser", action="store_true", help="open the projector screen in the default browser")
    demo.add_argument("--questions", type=int, default=8, help="number of questions, 3 to 8 (default 8)")
    demo.add_argument("--lobby", type=float, default=5.0, help="seconds to wait in the lobby before starting")
    demo.add_argument("--speed", type=float, default=1.0, help="cassette replay speed (1 = recorded latencies)")
    demo.add_argument("--seed", type=int, default=7, help="robot random seed")
    demo.add_argument("--exit", action="store_true", help="exit at the end instead of keeping the report page up")
    demo.add_argument("--live", action="store_true",
                      help="real model players through Hermes (paid Portal calls, capped by the budget_usd setting)")

    bench = sub.add_parser("bench", help="Load bench with robot phones (prints p50/p95)")
    bench.add_argument("--players", type=int, nargs="+", default=[60])
    bench.add_argument("--questions", type=int, default=5)

    serve = sub.add_parser("serve", help="Host a quiz from a questions JSON file (host page drives it)")
    serve.add_argument("--doc", default=str(SAMPLE_DIR / "runbook.md"), help="document path")
    serve.add_argument("--questions-file", default=str(SAMPLE_DIR / "questions.json"),
                       help="JSON list of questions, or an object with a 'questions' list")
    serve.add_argument("--port", type=int, default=None, help="override the port setting")
    serve.add_argument("--open-lan", action="store_true")
    serve.add_argument("--bots", choices=["none", "cassette", "live"], default="none",
                       help="model players: none, the sample cassette, or live calls through ctx.llm (paid)")
    serve.add_argument("--browser", action="store_true")


def run(args: argparse.Namespace, ctx: Any = None, out: Callable[[str], None] | None = None) -> int:
    out = out or _print
    cmd = getattr(args, "popquiz_cmd", None)
    if cmd == "demo":
        return demo(args, ctx, out)
    if cmd == "bench":
        for n in args.players:
            out(json.dumps(asyncio.run(bench(n, args.questions))))
        return 0
    if cmd == "serve":
        return serve(args, ctx, out)
    out("usage: hermes popquiz {demo,bench,serve} [-h]")
    return 2


def _print(text: str) -> None:
    print(text, flush=True)


class _NullCtx:
    """Stand-in when the CLI runs outside Hermes (tests, scripts): no settings, no session."""

    def get_config(self, key, default=None):
        return default

    def session_cwd(self):
        return ""

    def has_capability(self, _cap):
        return False

    def post_message(self, _text):
        return type("Refused", (), {"ok": False, "error": "no_conversation"})()

    def inject_message(self, *_a, **_kw):
        return False


# ---------------------------------------------------------------------------------------------- demo

def demo_drafts(n_questions: int) -> tuple[list[dict], list[dict], list[dict]]:
    """(first_draft, fixes, final_questions) from sample/questions.json: the first draft carries the 2
    deliberately wrong questions in the last 2 slots, the fixes are the matching valid questions."""
    data = json.loads((SAMPLE_DIR / "questions.json").read_text(encoding="utf-8"))
    valid, wrong = list(data["questions"]), list(data["wrong"])
    n = max(3, min(len(valid), int(n_questions)))
    fixed = [valid[w["replaces"]] for w in wrong]
    keep = [q for i, q in enumerate(valid) if i not in {w["replaces"] for w in wrong}][: n - len(wrong)]
    first = keep + [w["question"] for w in wrong]
    final = keep + fixed
    return first, fixed, final


def _print_rejections(res: dict, sent: list[dict], out: Callable[[str], None]) -> None:
    for rej in res.get("rejected", []):
        q = sent[rej["index"]] if 0 <= rej["index"] < len(sent) else {}
        score = rej.get("best_score")
        out(f"  REFUSED q{rej['index'] + 1} [{rej['code']}] {q.get('prompt', '')}")
        out(f"     quote sent : \"{q.get('source_quote', '')}\"")
        if rej.get("closest_passage"):
            out(f"     closest doc: \"{rej['closest_passage']}\"" + (f" (score {score:.3f})" if score is not None else ""))
        if rej.get("detail"):
            out(f"     why        : {rej['detail']}")


def demo(args: argparse.Namespace, ctx: Any = None, out: Callable[[str], None] = _print) -> int:
    from .cassette import CassetteLlm
    from .model import default_bots
    from .pricing import Pricing
    from .robots import run_robots

    live = bool(getattr(args, "live", False))
    if live and ctx is None:
        out("--live needs Hermes (model calls go through ctx.llm): run it as `hermes popquiz demo --live`.")
        return 2
    ctx = ctx if ctx is not None else _NullCtx()
    first, fixes, final = demo_drafts(args.questions)
    base_settings = {"host": "127.0.0.1", "port": int(args.port), "public_url": "", "auto_advance": True}
    if live:
        # Players, budget, time limit and mode come from the plugin settings; calls are real and paid.
        app = PopQuizApp(ctx, settings_override=base_settings, use_card=False, inject=False)
    else:
        app = PopQuizApp(ctx, llm_call=CassetteLlm(SAMPLE_DIR / "cassette.json", speed=args.speed),
                         bots=default_bots(),
                         settings_override={**base_settings, "budget_usd": 1.0, "time_limit_s": 20, "mode": "open_book"},
                         use_card=False, inject=False, pricing_loader=lambda _models: Pricing.fallback())
    doc_path = str(SAMPLE_DIR / "runbook.md")
    if live:
        out("Pop Quiz demo, LIVE: real model players through Hermes (paid Portal calls), robot phones")
    else:
        out("Pop Quiz demo: the room vs the models (cassette models, robot phones, no model call, no network)")
    out(f"Document: {doc_path}")
    out("")
    out(f"Step 1. The agent sends a first draft of {len(first)} questions to popquiz_create.")
    res = app.create({"doc_path": doc_path, "questions": first, "title": "Kestrel Incident Runbook"})
    if res.get("status") != "needs_fix":
        out(f"unexpected answer: {json.dumps(res)[:400]}")
        app.shutdown()
        return 1
    out(f"  accepted {len(res['accepted'])}, refused {len(res['rejected'])} (attempt {res['attempt']}/{res['max_attempts']})")
    _print_rejections(res, first, out)
    out("")
    out(f"Step 2. The agent copies the quotes word for word and resends only the {len(fixes)} fixed questions.")
    res = app.create({"draft_id": res["draft_id"], "questions": fixes})
    if res.get("status") != "ready":
        out(f"unexpected answer: {json.dumps(res)[:400]}")
        app.shutdown()
        return 1
    out(f"  ready: room {res['room']}, {res['questions']} questions, players: {', '.join(res['players'])}")
    est = float(res.get('estimated_cost_usd') or 0)
    if live:
        out(f"  estimated model cost: ${est:.4f}, budget cap ${float(res.get('budget_usd') or 0):.2f}"
            + (" (degraded: main model only)" if res.get("degraded") else ""))
    else:
        out(f"  estimated model cost if live: ${est:.4f} (this demo: $0, cassette)")
    urls = res["urls"]
    if args.open_lan:
        lan = app.control("open_lan")
        if "error" in lan:
            out(f"  open_lan failed: {lan['error']} {lan.get('detail', '')}")
        else:
            urls["join"] = lan["status"]["urls"].get("join", urls.get("join"))
    out("")
    out(f"Projector : {urls.get('screen')}")
    out(f"Phones    : {urls.get('join')}")
    out(f"Host page : {urls.get('host')}  (this machine only)")
    if args.browser:
        try:
            import webbrowser
            webbrowser.open(urls["screen"])
        except Exception:  # noqa: BLE001
            out("  (could not open a browser)")

    finished = threading.Event()

    def on_event(ev: dict) -> None:
        t = ev.get("t")
        if t == "question":
            out(f"  question {int(ev.get('q', 0)) + 1}/{ev.get('total')}")
        elif t == "reveal":
            out(f"  reveal   {int(ev.get('q', 0)) + 1}, model spend so far ${float(ev.get('spent_usd') or 0):.4f}"
                + ("" if live else " (simulated)"))
            # on_event runs on the server thread, so reading the engine here is safe.
            game = getattr(app.server, "_game", None)
            try:
                bots_info = game.reveal_event(int(ev.get("q", 0)))["bots"] if game is not None else []
            except Exception:  # noqa: BLE001 - display only
                bots_info = []
            for b in bots_info:
                out("    " + bot_line(b))
        elif t == "finished":
            finished.set()

    app.event_listeners.append(on_event)
    base = f"http://127.0.0.1:{app.server.bound_port}"
    key = [int(q["answer_index"]) for q in final]
    robot_stats: dict = {}

    def robots_main() -> None:
        try:
            robot_stats.update(asyncio.run(run_robots(base, res["room"], int(args.robots), accuracy=(0.45, 0.9),
                                                      think=(1.0, 8.0), seed=args.seed, answer_key=key,
                                                      connect_spread_s=1.5)))
        except Exception as exc:  # noqa: BLE001
            robot_stats["error"] = repr(exc)

    robots = threading.Thread(target=robots_main, name="popquiz-robots", daemon=True)
    if args.robots > 0:
        robots.start()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and app.status().get("players", 0) < args.robots:
        time.sleep(0.2)
    out(f"\nStep 3. {app.status().get('players', 0)} players in the lobby; starting in {args.lobby:.0f} s "
        "(open the projector URL now).")
    time.sleep(max(0.0, args.lobby))
    started = app.control("start")
    if "error" in started:
        out(f"start failed: {started}")
        app.shutdown()
        return 1
    try:
        while not finished.wait(0.5):
            if not app.status().get("state") or app.status().get("state") == "idle":
                break
        robots.join(10)
        out("")
        out("Step 4. Game over. The report that goes back into the conversation:")
        out("")
        out(app.report_markdown() or "(no report)")
        st = app.status()
        out("")
        out(card_text(st))
        out(f"Robots: {json.dumps({k: robot_stats.get(k) for k in ('joined', 'answers', 'errors')})}")
        if not args.exit and sys.stdin is not None and sys.stdin.isatty():
            out("\nThe pages stay up for the report. Press Ctrl+C to stop.")
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        app.shutdown()
    return 0


def bot_line(b: dict) -> str:
    """One model player's answer at the reveal: choice, right or wrong, latency, cost, error."""
    if b.get("pending"):
        return f"{b.get('label', '?'):<18} still thinking (shown when it lands, 0 points)"
    choice = b.get("choice")
    letter = LETTERS[choice] if isinstance(choice, int) and 0 <= choice < len(LETTERS) else "-"
    lat = b.get("latency_ms")
    parts = [f"{b.get('label', '?'):<18} {letter} {'right' if b.get('correct') else 'wrong'}",
             f"{lat / 1000:6.2f} s" if isinstance(lat, (int, float)) else "     - s",
             f"${float(b.get('cost_usd') or 0):.5f}"]
    if b.get("error"):
        parts.append(f"[{b['error']}]")
    if b.get("late"):
        parts.append("[late]")
    return "  ".join(parts)


# ---------------------------------------------------------------------------------------------- bench

def _pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    k = max(0, min(len(xs) - 1, int(round(p / 100 * len(xs) + 0.5)) - 1))
    return xs[k]


async def bench(n: int, n_questions: int) -> dict:
    from .cassette import CassetteLlm
    from .model import Document, Mode, Question, default_bots
    from .robots import run_robots
    from .server import QuizServer

    data = json.loads((SAMPLE_DIR / "questions.json").read_text(encoding="utf-8"))
    raw = data["questions"][: max(1, n_questions)]
    qs = [Question(index=i, prompt=q["prompt"], choices=q["choices"], answer_index=q["answer_index"],
                   source_quote=q["source_quote"], time_limit_s=20) for i, q in enumerate(raw)]
    text = (SAMPLE_DIR / "runbook.md").read_text(encoding="utf-8")
    doc = Document(title="Bench", text=text, path="bench", sha256="0" * 64, kind="md")
    srv = QuizServer(port=0, llm_call=CassetteLlm(SAMPLE_DIR / "cassette.json", speed=1.0))
    srv.start()
    try:
        info = srv.create_game(questions=qs, bots=default_bots(), doc=doc, mode=Mode.OPEN_BOOK, budget_usd=5.0,
                               auto_advance=True, reveal_s=0.3)
        srv._conductor.auto_advance_s = 0.3
        joined = asyncio.Event()
        t0 = time.perf_counter()
        task = asyncio.create_task(run_robots(f"http://127.0.0.1:{srv.bound_port}", info["room"], n,
                                              accuracy=(0.5, 0.9), think=(0.0, 0.0), seed=42,
                                              answer_key=[q.answer_index for q in qs], joined=joined))
        await asyncio.wait_for(joined.wait(), 60)
        connect_ms = (time.perf_counter() - t0) * 1000
        await asyncio.to_thread(srv.host_command, "start")
        stats = await asyncio.wait_for(task, 60 + 25 * len(qs))
        game = srv._game
        lat = [a.latency_ms for q in game.answers.values() for a in q if a.actor_kind == "human"]
        return {"players": n, "questions": len(qs), "connect_all_ms": round(connect_ms, 1), "answers": len(lat),
                "expected": n * len(qs), "robot_errors": stats["errors"],
                "question_to_answer_ms": {"p50": statistics.median(lat), "p95": _pct(lat, 95), "max": max(lat)}
                if lat else None}
    finally:
        srv.stop()


# ---------------------------------------------------------------------------------------------- serve

def serve(args: argparse.Namespace, ctx: Any = None, out: Callable[[str], None] = _print) -> int:
    from .cassette import CassetteLlm
    from .model import default_bots

    ctx = ctx if ctx is not None else _NullCtx()
    data = json.loads(Path(args.questions_file).read_text(encoding="utf-8"))
    questions = data.get("questions") if isinstance(data, dict) else data
    override: dict = {}
    if args.port is not None:
        override["port"] = int(args.port)
    kwargs: dict = {"use_card": False, "inject": False, "settings_override": override}
    if args.bots == "none":
        kwargs.update(bots=[], llm_call=None)
        kwargs["llm_call"] = _no_llm
    elif args.bots == "cassette":
        kwargs.update(bots=default_bots(), llm_call=CassetteLlm(SAMPLE_DIR / "cassette.json"))
    app = PopQuizApp(ctx, **kwargs)
    res = app.create({"doc_path": str(Path(args.doc).expanduser()), "questions": questions})
    if res.get("status") != "ready":
        out(json.dumps(res, indent=2)[:3000])
        app.shutdown()
        return 1
    if args.open_lan:
        lan = app.control("open_lan")
        if "error" not in lan:
            res["urls"]["join"] = lan["status"]["urls"].get("join")
    out(f"Room {res['room']}: {res['questions']} questions, models: {', '.join(res['players']) or 'none'}")
    for k in ("screen", "join", "host"):
        out(f"{k:7}: {res['urls'].get(k)}")
    if args.browser:
        import webbrowser
        webbrowser.open(res["urls"]["screen"])
    out("Drive the game from the host page. Ctrl+C to stop.")
    try:
        while app.status().get("state") not in (None, "idle"):
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        app.shutdown()
    return 0


async def _no_llm(*_a, **_kw):  # pragma: no cover - never called: no bots
    raise RuntimeError("no model players")
