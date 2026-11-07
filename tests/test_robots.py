"""Robots against a real server."""

from __future__ import annotations

import asyncio

from popquiz_plugin.model import BotSpec, Document, Mode, Question
from popquiz_plugin.robots import run_robots
from popquiz_plugin.server import QuizServer

QS = [Question(index=i, prompt=f"Robot question {i}?", choices=["A1", "B1", "C1", "D1"], answer_index=(i + 1) % 4,
               source_quote="robot quote long enough", time_limit_s=3) for i in range(3)]
DOC = Document(title="Robots", text="robot quote long enough", path="r.md", sha256="2" * 64, kind="md")


async def _game(n, accuracy, answer_key):
    s = QuizServer(port=0, llm_call=None)
    s.start()
    try:
        info = s.create_game(questions=QS, bots=[BotSpec("bot1", "B", "nous", "x/y")], doc=DOC, mode=Mode.OPEN_BOOK,
                             budget_usd=1.0, auto_advance=True, reveal_s=0.05)
        s._conductor.auto_advance_s = 0.05
        joined = asyncio.Event()
        task = asyncio.create_task(run_robots(f"http://127.0.0.1:{s.bound_port}", info["room"], n, accuracy=accuracy,
                                              think=(0.0, 0.2), seed=3, answer_key=answer_key, joined=joined))
        await asyncio.wait_for(joined.wait(), 10)
        s.host_command("start")
        stats = await asyncio.wait_for(task, 30)
        return s, stats
    finally:
        s.stop()


async def test_robots_play_a_full_game_with_perfect_accuracy():
    s, stats = await _game(8, (1.0, 1.0), [q.answer_index for q in QS])
    assert stats["joined"] == 8 and stats["finished"] == 8 and stats["answers"] == 24
    assert stats["correct_sent"] == 24 and not stats["errors"]
    humans = [a for q in s._game.answers.values() for a in q if a.actor_kind == "human"]
    assert len(humans) == 24 and all(a.correct for a in humans)
    assert len({p.nickname for p in s._game.players.values()}) == 8


async def test_robots_zero_accuracy_and_without_key():
    s, stats = await _game(4, (0.0, 0.0), [q.answer_index for q in QS])
    assert stats["answers"] == 12 and stats["correct_sent"] == 0
    s, stats = await _game(3, (0.5, 0.5), None)
    assert stats["answers"] == 9 and stats["finished"] == 3


async def test_robots_stop_event():
    s = QuizServer(port=0)
    s.start()
    try:
        info = s.create_game(questions=QS, bots=[], doc=DOC, mode=Mode.OPEN_BOOK, budget_usd=1.0)
        stop, joined = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(run_robots(f"http://127.0.0.1:{s.bound_port}", info["room"], 5, stop=stop,
                                              joined=joined, seed=1))
        await asyncio.wait_for(joined.wait(), 10)
        stop.set()
        stats = await asyncio.wait_for(task, 5)
        assert stats["joined"] == 5 and stats["finished"] == 0
    finally:
        s.stop()
