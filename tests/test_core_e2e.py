"""Core end to end: sample document and questions, cassette bots, engine, report. No server, no network."""

from __future__ import annotations

import json
from pathlib import Path

from popquiz_plugin.anchor import validate_questions
from popquiz_plugin.bots import ask_bots
from popquiz_plugin.cassette import CassetteLlm
from popquiz_plugin.engine import Game
from popquiz_plugin.model import GameState, Mode, default_bots
from popquiz_plugin.pricing import Pricing
from popquiz_plugin.report import build_report


async def test_sample_game_end_to_end(plugin_root: Path):
    doc = (plugin_root / "sample" / "runbook.md").read_text(encoding="utf-8")
    data = json.loads((plugin_root / "sample" / "questions.json").read_text(encoding="utf-8"))
    questions = validate_questions(data["questions"], doc).accepted
    t = [0.0]
    game = Game(game_id="demo", room_code="DEMO42", questions=questions, bots=default_bots(),
                clock=lambda: t[0], title=data["title"], budget_usd=1.0)
    players = [game.add_player(n)[0].id for n in ("Ada", "Bob", "Cy")]
    llm = CassetteLlm(plugin_root / "sample" / "cassette.json", speed=0)
    pricing = Pricing.fallback()
    game.start()
    while game.state != GameState.FINISHED:
        q = game.current
        question = game.questions[q]
        for k, pid in enumerate(players):
            t[0] += 2
            game.submit_answer(pid, q, question.answer_index if k != 2 or q % 2 else (question.answer_index + 1) % len(question.choices))
        to_call, skipped = game.plan_bots({b.id: pricing.estimate_question_cost(b.model, len(doc)) for b in game.bots})
        assert skipped == []
        for r in await ask_bots(to_call, question, doc, Mode.OPEN_BOOK, llm, pricing):
            game.submit_bot_answer(r.bot_id, q, r.choice, r.latency_ms, r.meta())
        assert game.everyone_answered()
        game.close_question()
        game.show_leaderboard()
        game.next_question()
    rep = build_report(game)
    assert rep.data["questions_played"] == 8 and len(rep.markdown) < 4000
    by_id = {c["id"]: c for c in rep.data["contenders"]}
    assert by_id["bot1"]["correct"] == 7 and by_id["bot2"]["correct"] == 6
    assert by_id["room"]["correct"] == 8
    assert 0 < game.spent_usd < 1.0
    assert by_id["bot1"]["cost_usd"] > 20 * by_id["bot2"]["cost_usd"]
    assert "Claude Opus 5.5" in rep.markdown
