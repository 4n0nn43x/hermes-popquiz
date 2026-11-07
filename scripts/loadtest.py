"""Load bench: N robot phones against the real server thread, with cassette bots when available.

Usage (from the plugin directory):
    uv run --no-project --with aiohttp --with segno python scripts/loadtest.py [60 200 ...] [--questions 5]

Measures, per answer, the server-side delay between the question broadcast (question start on the
server clock) and the reception of the answer, with robots that answer as soon as they receive the
question (think time 0). Prints p50 / p95 / p99 / max in ms for each player count.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "popquiz_plugin"


def _load_plugin():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(PACKAGE, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = mod
    spec.loader.exec_module(mod)
    return mod


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    k = max(0, min(len(xs) - 1, int(round(p / 100 * len(xs) + 0.5)) - 1))
    return xs[k]


async def bench(n: int, n_questions: int) -> dict:
    _load_plugin()
    from popquiz_plugin.model import Document, Mode, Question, default_bots
    from popquiz_plugin.robots import run_robots
    from popquiz_plugin.server import QuizServer

    qfile, cfile = ROOT / "sample" / "questions.json", ROOT / "sample" / "cassette.json"
    llm, bots, cassette = None, [], False
    if qfile.exists():
        data = json.loads(qfile.read_text())
        raw = data["questions"]
        title = data.get("title", "Sample")
    else:
        raw = [{"prompt": f"Question {i}?", "choices": ["A", "B", "C", "D"], "answer_index": i % 4,
                "source_quote": "quote"} for i in range(n_questions)]
        title = "Bench"
    qs = [Question(index=i, prompt=q["prompt"], choices=q["choices"], answer_index=q["answer_index"],
                   source_quote=q["source_quote"], time_limit_s=20) for i, q in enumerate(raw[:n_questions])]
    if cfile.exists():
        from popquiz_plugin.cassette import CassetteLlm
        llm, bots, cassette = CassetteLlm(cfile, speed=1.0), default_bots(), True
    text = (ROOT / "sample" / "runbook.md").read_text() if (ROOT / "sample" / "runbook.md").exists() else "doc"
    doc = Document(title=title, text=text, path="bench", sha256="0" * 64, kind="md")

    srv = QuizServer(port=0, llm_call=llm)
    srv.start()
    try:
        info = srv.create_game(questions=qs, bots=bots, doc=doc, mode=Mode.OPEN_BOOK, budget_usd=5.0,
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
        stats = await asyncio.wait_for(task, 60 + 25 * n_questions)
        game = srv._game
        lat = [a.latency_ms for q in game.answers.values() for a in q if a.actor_kind == "human"]
        bot_lat = [a.latency_ms for q in game.answers.values() for a in q if a.actor_kind == "bot" and a.choice is not None]
        return {
            "players": n, "questions": len(qs), "cassette_bots": cassette, "connect_all_ms": round(connect_ms, 1),
            "answers": len(lat), "expected": n * len(qs), "robot_errors": stats["errors"],
            "question_to_answer_ms": {"p50": statistics.median(lat), "p95": pct(lat, 95), "p99": pct(lat, 99),
                                      "max": max(lat)} if lat else None,
            "bot_answers": len(bot_lat),
        }
    finally:
        srv.stop()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("players", nargs="*", type=int, default=[60, 200])
    ap.add_argument("--questions", type=int, default=5)
    args = ap.parse_args()
    for n in args.players:
        print(json.dumps(asyncio.run(bench(n, args.questions))), flush=True)


if __name__ == "__main__":
    main()
