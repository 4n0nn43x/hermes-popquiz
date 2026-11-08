"""Report tests (content, CSV, chat length) and sample tests (runbook, questions: 8 valid + 2 wrong)."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest

from popquiz_plugin.anchor import validate_questions
from popquiz_plugin.engine import Game
from popquiz_plugin.model import Question, default_bots
from popquiz_plugin.report import Report, build_report


class Clock:
    def __init__(self) -> None:
        self.t = 50.0

    def __call__(self) -> float:
        return self.t


def play_game(n_players: int = 4, n_questions: int = 3, long_text: bool = False) -> Game:
    clock = Clock()
    pad = " with a very long tail of words to stress the chat length limit" * 4 if long_text else ""
    questions = [Question(index=i, prompt=f"What is fact number {i}?{pad}", choices=["red", "green", "blue", "=cmd"],
                          answer_index=i % 3, source_quote=f"Fact {i} is described here{pad}") for i in range(n_questions)]
    game = Game(game_id="g", room_code="ROOM42", questions=questions, bots=default_bots(), clock=clock,
                title="Kestrel Runbook")
    ids = [game.add_player(n)[0].id for n in ["Ada", "=HYPERLINK(evil)", "Bob", "Cy", "Di", "Ed"][:n_players]]
    game.start()
    for qi in range(n_questions):
        q = game.questions[qi]
        for k, pid in enumerate(ids):
            clock.t += 1
            # most humans wrong on question 1
            choice = (q.answer_index + 1) % 3 if (qi == 1 and k > 0) else q.answer_index
            game.submit_answer(pid, qi, choice)
        game.submit_bot_answer("bot1", qi, q.answer_index, 9000, {"cost_usd": 0.02})
        game.submit_bot_answer("bot2", qi, (q.answer_index + 1) % 3 if qi == 1 else q.answer_index, 3000,
                               {"cost_usd": 0.0004})
        game.submit_bot_answer("bot3", qi, None, 21000, {"cost_usd": 0.0, "error": "timeout"})
        game.close_question()
        game.show_leaderboard()
        game.next_question()
    return game


def test_report_markdown_content():
    rep = build_report(play_game())
    assert isinstance(rep, Report)
    md = rep.markdown
    assert md.startswith("# Pop Quiz report: Kestrel Runbook")
    assert "## The Room vs the models" in md and "| The Room |" in md and "Claude Opus 5.5" in md
    assert "Best model on this document: **Claude Opus 5.5**" in md
    assert "## What the room misunderstood" in md and "What is fact number 1?" in md
    assert "25% correct" in md and "Most common wrong answer" in md
    assert "Models wrong too: DeepSeek V4 Flash" in md
    assert "Mercury 3 (3 no answer)" in md
    assert "## Suggested next step" in md
    assert len(md) < 4000


def test_report_data():
    data = build_report(play_game()).data
    assert data["questions_played"] == 3 and data["players_count"] == 4
    assert data["best_model"] == "bot1" and data["misunderstood"] == [1]
    room = data["contenders"][0]
    assert room["id"] == "room" and room["correct"] == 2
    assert data["spent_usd"] == pytest.approx(3 * 0.0204)
    json.dumps(data)


def test_report_csv_rows_and_injection_guard():
    game = play_game()
    rows = list(csv.DictReader(io.StringIO(build_report(game).csv)))
    assert len(rows) == 3 * (4 + 3 + 1)
    assert {r["actor_kind"] for r in rows} == {"human", "bot", "room"}
    names = {r["name"] for r in rows}
    assert "'=HYPERLINK(evil)" in names and "=HYPERLINK(evil)" not in names
    timeout_rows = [r for r in rows if r["actor_id"] == "bot3"]
    assert all(r["error"] == "timeout" and r["choice"] == "" for r in timeout_rows)


@pytest.mark.parametrize("players,questions", [(6, 20), (1, 1)])
def test_report_always_fits_chat(players, questions):
    md = build_report(play_game(players, questions, long_text=True)).markdown
    assert len(md) < 4000


def test_report_before_any_question():
    game = Game(game_id="g", room_code="R", questions=[], bots=[], clock=Clock())
    rep = build_report(game)
    assert "0/0 questions played" in rep.markdown and rep.data["best_model"] is None


def test_best_value_model():
    game = play_game()
    game.bot_correct["bot2"] = game.bot_correct["bot1"]
    data = build_report(game).data
    assert data["best_value_model"] == "bot2"


# ---------------------------------------------------------------- sample files
def _sample(plugin_root: Path):
    doc = (plugin_root / "sample" / "runbook.md").read_text(encoding="utf-8")
    data = json.loads((plugin_root / "sample" / "questions.json").read_text(encoding="utf-8"))
    return doc, data


def test_sample_runbook_size_and_style(plugin_root):
    doc, _ = _sample(plugin_root)
    assert 1300 <= len(doc.split()) <= 1800
    assert doc.startswith("# Kestrel Incident Runbook") and "\u2014" not in doc


def test_sample_questions_valid(plugin_root):
    doc, data = _sample(plugin_root)
    v = validate_questions(data["questions"], doc)
    assert len(v.accepted) == 8 and v.rejected == [] and v.warnings == []


def test_sample_wrong_questions_are_refused_then_corrected(plugin_root):
    doc, data = _sample(plugin_root)
    draft = list(data["questions"])
    for w in data["wrong"]:
        draft[w["replaces"]] = w["question"]
    first = validate_questions(draft, doc)
    assert len(first.accepted) == 6
    assert [(r["index"], r["code"]) for r in first.rejected] == [(w["replaces"], w["expected_code"]) for w in data["wrong"]]
    assert {w["expected_code"] for w in data["wrong"]} == {"number_mismatch", "quote_not_found"}
    fixed = validate_questions([data["questions"][w["replaces"]] for w in data["wrong"]], doc)
    assert len(fixed.accepted) == 2


def test_sample_files_have_no_em_dash(plugin_root):
    for name in ("runbook.md", "questions.json", "cassette.json"):
        assert "\u2014" not in (plugin_root / "sample" / name).read_text(encoding="utf-8"), name


def bots_only_game(answer) -> Game:
    """One human, 2 questions; answer(qi, q, bot_id) -> (choice, meta) for each default bot."""
    clock = Clock()
    questions = [Question(index=i, prompt=f"Question {i}?", choices=["a", "b", "c"], answer_index=0,
                          source_quote=f"Fact {i} is described here") for i in range(2)]
    game = Game(game_id="g", room_code="ROOM42", questions=questions, bots=default_bots(), clock=clock, title="Doc")
    pid = game.add_player("Ada")[0].id
    game.start()
    for qi, q in enumerate(questions):
        clock.t += 1
        game.submit_answer(pid, qi, 0)
        for b in game.bots:
            choice, meta = answer(qi, q, b.id)
            game.submit_bot_answer(b.id, qi, choice, 2000, meta)
        game.close_question()
        game.show_leaderboard()
        game.next_question()
    return game


def test_no_best_model_when_every_model_was_refused():
    game = bots_only_game(lambda qi, q, bid: (None, {"cost_usd": 0.0, "error": "refused"}))
    rep = build_report(game)
    assert rep.data["best_model"] is None and rep.data["best_value_model"] is None
    assert "Best model" not in rep.markdown and "The Room beat" not in rep.markdown
    assert "No model answered (errors: refused), so there is no best model" in rep.markdown
    assert "Model spend: $0.0000 (budget" in rep.markdown


def test_no_best_model_when_no_model_answered_correctly():
    game = bots_only_game(lambda qi, q, bid: (1, {"cost_usd": 0.001}))
    md = build_report(game).markdown
    assert "Best model" not in md and "No model answered correctly" in md


def test_estimated_costs_read_as_estimates():
    def answer(qi, q, bid):
        if bid == "bot1":
            return None, {"cost_usd": 0.02, "cost_estimated": True, "error": "timeout"}
        return 0, {"cost_usd": 0.001}

    rep = build_report(bots_only_game(answer))
    assert rep.data["spent_estimated"] is True
    assert "Model spend: ~$0.0440 (est.)" in rep.markdown
    assert "| ~$0.0400 (est.) |" in rep.markdown  # the timed-out model's row
    assert "| $0.0020 |" in rep.markdown  # real costs stay plain


def test_cassette_spend_is_labelled_simulated():
    game = bots_only_game(lambda qi, q, bid: (0, {"cost_usd": 0.01}))
    md = build_report(game, simulated=True).markdown
    assert "Model spend: $0.0600 simulated (recorded cassette, no model call)" in md
    assert "| Simulated cost |" in md
    assert "simulated" not in build_report(game).markdown


def test_no_cheaper_claim_when_models_cost_the_same():
    """Real free game, 2026-11-07: every model right at $0 printed "Same accuracy for less" (false)."""
    game = bots_only_game(lambda qi, q, bid: (0, {"cost_usd": 0.0}))
    md = build_report(game).markdown
    assert "Best model on this document" in md and "for less" not in md
    game = play_game()
    game.bot_correct["bot2"] = game.bot_correct["bot1"]  # same accuracy, bot2 really is cheaper
    assert "Same accuracy for less: **DeepSeek V4 Flash**" in build_report(game).markdown
