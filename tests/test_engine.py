"""Engine tests: transitions, errors, scoring, The Room, late joiners, budget, views never leak."""

from __future__ import annotations

import json

import pytest

from popquiz_plugin.engine import Game, clean_nickname, hash_token, score_for
from popquiz_plugin.model import BotSpec, GameError, GameState, Mode, Question


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


def make_questions(n: int = 3) -> list[Question]:
    return [
        Question(index=99, prompt=f"Question {i}?", choices=["alpha", "beta", "gamma", "delta"], answer_index=i % 4,
                 source_quote=f"quote number {i} long enough", explanation=f"because {i}", time_limit_s=20)
        for i in range(n)
    ]


BOTS = [BotSpec("bot1", "Opus", "nous", "anthropic/claude-opus-5.5"),
        BotSpec("bot2", "DeepSeek", "nous", "deepseek/deepseek-v4-flash")]


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def game(clock: Clock) -> Game:
    return Game(game_id="g1", room_code="K7Q2PA", questions=make_questions(), bots=list(BOTS), clock=clock)


def err(code: str):
    return pytest.raises(GameError, match=rf"^{code}")


# ---------------------------------------------------------------- scoring formula
@pytest.mark.parametrize("t,expected", [(0, 1000), (10, 750), (20, 500), (30, 500), (5, 875), (-1, 1000)])
def test_score_formula(t, expected):
    assert score_for(True, t, 20) == expected


def test_score_wrong_is_zero():
    assert score_for(False, 1, 20) == 0


# ---------------------------------------------------------------- nicknames, tokens
def test_add_player_returns_token_and_stores_hash_only(game):
    p, token = game.add_player("  Ada  ")
    assert p.nickname == "Ada"
    assert p.token_hash == hash_token(token)
    assert token not in json.dumps(game.host_view())
    assert len(token) >= 24


def test_nickname_control_chars_removed():
    assert clean_nickname("A\x00d\x1ba\u200b") == "Ada"


@pytest.mark.parametrize("bad", ["", "   ", "\x00\x01", "x" * 25, None])
def test_bad_nickname(game, bad):
    with err("bad_nickname"):
        game.add_player(bad)


def test_nickname_unique_case_insensitive(game):
    a, _ = game.add_player("Bob")
    b, _ = game.add_player("bob")
    c, _ = game.add_player("BOB")
    assert [a.nickname, b.nickname, c.nickname] == ["Bob", "bob 2", "BOB 3"]


def test_nickname_suffix_keeps_max_length(game):
    game.add_player("x" * 24)
    p, _ = game.add_player("X" * 24)
    assert len(p.nickname) <= 24 and p.nickname.endswith(" 2")


def test_game_full(clock):
    g = Game(game_id="g", room_code="R", questions=make_questions(), bots=[], clock=clock, max_players=2)
    g.add_player("a")
    g.add_player("b")
    with err("game_full"):
        g.add_player("c")


def test_resume_with_token(game):
    p, token = game.add_player("Ada")
    game.set_connected(p.id, False)
    assert game.resume(token) is p
    assert p.connected is True


@pytest.mark.parametrize("token", ["", "nope", None])
def test_resume_bad_token(game, token):
    game.add_player("Ada")
    with err("bad_token"):
        game.resume(token)


def test_set_connected_unknown(game):
    with err("unknown_player"):
        game.set_connected("zzz", False)


def test_late_joiner_allowed_and_finished_refuses(game, clock):
    game.start()
    p, _ = game.add_player("Late")
    a = game.submit_answer(p.id, 0, 0)
    assert a.correct
    game.end()
    with err("game_finished"):
        game.add_player("Too late")


# ---------------------------------------------------------------- transitions
def test_full_flow_transitions(game, clock):
    assert game.state == GameState.LOBBY and game.current == -1
    ev = game.start()
    assert ev["t"] == "question" and ev["q"] == 0 and ev["total"] == 3
    assert ev["ends_at_ms"] - ev["server_now_ms"] == 20_000
    assert game.state == GameState.QUESTION
    rev = game.close_question()
    assert rev["t"] == "reveal" and game.state == GameState.REVEAL
    lb = game.show_leaderboard()
    assert lb["t"] == "leaderboard" and game.state == GameState.LEADERBOARD
    ev = game.next_question()
    assert ev["q"] == 1
    game.close_question()
    ev = game.next_question()  # REVEAL -> QUESTION allowed
    assert ev["q"] == 2
    game.close_question()
    game.show_leaderboard()
    fin = game.next_question()
    assert fin == {"t": "finished", "report_url": "/report?room=K7Q2PA"}
    assert game.state == GameState.FINISHED


def test_questions_renumbered(game):
    assert [q.index for q in game.questions] == [0, 1, 2]


def test_start_twice_wrong_state(game):
    game.start()
    with err("wrong_state"):
        game.start()


def test_start_without_questions(clock):
    g = Game(game_id="g", room_code="R", questions=[], bots=[], clock=clock)
    with err("no_questions"):
        g.start()


@pytest.mark.parametrize("method", ["close_question", "show_leaderboard", "next_question"])
def test_wrong_state_in_lobby(game, method):
    with err("wrong_state"):
        getattr(game, method)()


def test_show_leaderboard_needs_reveal(game):
    game.start()
    with err("wrong_state"):
        game.show_leaderboard()


def test_end_from_any_state_is_idempotent(game):
    game.start()
    p, _ = game.add_player("Ada")
    game.submit_answer(p.id, 0, 0)
    fin = game.end()
    assert fin["t"] == "finished" and game.state == GameState.FINISHED
    assert game.players[p.id].score > 0  # question closed on end, score applied
    assert game.end() == fin


def test_end_from_lobby(game):
    assert game.end()["t"] == "finished"


# ---------------------------------------------------------------- answers
def test_answer_errors(game, clock):
    p, _ = game.add_player("Ada")
    with err("wrong_state"):
        game.submit_answer(p.id, 0, 0)
    game.start()
    with err("unknown_player"):
        game.submit_answer("ghost", 0, 0)
    with err("wrong_question"):
        game.submit_answer(p.id, 1, 0)
    for bad in (-1, 4, "1", True, None):
        with err("bad_choice"):
            game.submit_answer(p.id, 0, bad)
    game.submit_answer(p.id, 0, 0)
    with err("already_answered"):
        game.submit_answer(p.id, 0, 1)


def test_too_late_after_grace(game, clock):
    p, _ = game.add_player("Ada")
    q, _ = game.add_player("Bob")
    game.start()
    clock.advance(20.4)
    a = game.submit_answer(p.id, 0, 0)  # inside the 0.5 s grace
    assert a.points == 500
    clock.advance(0.2)
    with err("too_late"):
        game.submit_answer(q.id, 0, 0)


def test_scoring_uses_server_clock(game, clock):
    p, _ = game.add_player("Ada")
    w, _ = game.add_player("Wrong")
    game.start()
    clock.advance(10)
    a = game.submit_answer(p.id, 0, 0)
    b = game.submit_answer(w.id, 0, 1)
    assert (a.points, a.latency_ms, a.correct) == (750, 10_000, True)
    assert (b.points, b.correct) == (0, False)
    # scores are applied at close, not before
    assert game.players[p.id].score == 0
    game.close_question()
    assert game.players[p.id].score == 750 and game.players[p.id].streak == 1
    assert game.players[w.id].streak == 0


def test_streak_reset_on_wrong_and_no_answer(game, clock):
    p, _ = game.add_player("Ada")
    game.start()
    game.submit_answer(p.id, 0, 0)
    game.close_question()
    game.next_question()
    game.submit_answer(p.id, 1, 1)  # answer_index 1 -> correct
    game.close_question()
    assert game.players[p.id].streak == 2
    game.next_question()
    game.close_question()  # no answer
    assert game.players[p.id].streak == 0


# ---------------------------------------------------------------- bots
def test_bot_answer_scored_on_latency(game, clock):
    game.start()
    a = game.submit_bot_answer("bot1", 0, 0, 4000, {"cost_usd": 0.01, "served_model": "x"})
    assert a.points == 900 and a.correct and a.actor_kind == "bot"
    assert game.spent_usd == pytest.approx(0.01)
    game.close_question()
    assert game.bot_scores["bot1"] == 900 and game.bot_correct["bot1"] == 1


def test_bot_answer_late_after_close(game, clock):
    game.start()
    game.close_question()
    a = game.submit_bot_answer("bot2", 0, 0, 25_000, {"cost_usd": 0.002})
    assert a.meta["late"] is True and a.points == 0
    rev = game.reveal_event(0)
    entry = next(b for b in rev["bots"] if b["id"] == "bot2")
    assert entry["late"] is True and entry["choice"] == 0 and entry["pending"] is False
    assert game.bot_scores["bot2"] == 0


def test_bot_answer_slower_than_limit_is_late(game):
    game.start()
    a = game.submit_bot_answer("bot1", 0, 0, 21_000, {})
    assert a.meta["late"] and a.points == 0


def test_bot_errors(game):
    with err("wrong_state"):
        game.submit_bot_answer("bot1", 0, 0, 10, {})
    game.start()
    with err("unknown_bot"):
        game.submit_bot_answer("bot9", 0, 0, 10, {})
    with err("wrong_question"):
        game.submit_bot_answer("bot1", 1, 0, 10, {})
    game.submit_bot_answer("bot1", 0, None, 10, {"error": "timeout"})
    with err("already_answered"):
        game.submit_bot_answer("bot1", 0, 0, 10, {})


def test_bot_bad_choice_becomes_invalid(game):
    game.start()
    a = game.submit_bot_answer("bot1", 0, 7, 10, {})
    assert a.choice is None and a.meta["error"] == "invalid_json" and a.points == 0


def test_fallback_bot_still_scored(game):
    game.start()
    a = game.submit_bot_answer("bot1", 0, 0, 0, {"error": "fallback"})
    assert a.points == 1000


def test_everyone_answered(game, clock):
    p, _ = game.add_player("Ada")
    q, _ = game.add_player("Bob")
    assert not game.everyone_answered()
    game.start()
    game.submit_answer(p.id, 0, 0)
    game.set_connected(q.id, False)  # disconnected players are not waited for
    assert not game.everyone_answered()
    game.submit_bot_answer("bot1", 0, 0, 100, {})
    game.submit_bot_answer("bot2", 0, 1, 100, {})
    assert game.everyone_answered()


# ---------------------------------------------------------------- The Room
def _room(game):
    return next(a for a in game.answers[game.current] if a.actor_id == "room")


def test_room_majority(game, clock):
    ids = [game.add_player(n)[0].id for n in "abcde"]
    game.start()
    choices = [0, 0, 1, 0, 2]
    for pid, c in zip(ids, choices, strict=True):
        clock.advance(1)
        game.submit_answer(pid, 0, c)
    rev = game.close_question()
    room = _room(game)
    assert room.choice == 0 and room.correct
    assert room.latency_ms == 2000  # median of 1 s, 2 s, 4 s
    assert room.points == score_for(True, 2.0, 20)
    assert rev["room"] == {"choice": 0, "correct": True}
    assert rev["distribution"] == [3, 1, 1, 0]
    assert game.room_score == room.points and game.room_correct == 1


def test_room_tie_first_in_time(game, clock):
    ids = [game.add_player(n)[0].id for n in "abcd"]
    game.start()
    for pid, c in zip(ids, [2, 1, 1, 2], strict=True):
        clock.advance(1)
        game.submit_answer(pid, 0, c)
    game.close_question()
    assert _room(game).choice == 2  # 2 was chosen first among the tied choices


def test_room_empty(game):
    game.start()
    rev = game.close_question()
    room = _room(game)
    assert room.choice is None and not room.correct and room.points == 0
    assert rev["room"] == {"choice": None, "correct": False}


# ---------------------------------------------------------------- budget
def test_plan_bots_drops_most_expensive(clock):
    bots = BOTS + [BotSpec("bot3", "Mercury", "nous", "inception/mercury-3")]
    g = Game(game_id="g", room_code="R", questions=make_questions(), bots=bots, clock=clock, budget_usd=0.05)
    call, skipped = g.plan_bots({"bot1": 0.04, "bot2": 0.001, "bot3": 0.001})
    assert [b.id for b in call] == ["bot1", "bot2", "bot3"]
    g.spent_usd = 0.02
    call, skipped = g.plan_bots({"bot1": 0.04, "bot2": 0.001, "bot3": 0.001})
    assert [b.id for b in skipped] == ["bot1"] and [b.id for b in call] == ["bot2", "bot3"]


def test_budget_error_recorded(game):
    game.start()
    a = game.submit_bot_answer("bot1", 0, None, 0, {"error": "budget"})
    assert a.choice is None and a.meta["error"] == "budget"
    rev = game.close_question()
    assert next(b for b in rev["bots"] if b["id"] == "bot1")["error"] == "budget"


# ---------------------------------------------------------------- views never leak
def _dump(view) -> str:
    return json.dumps(view, default=str)


def test_public_view_never_leaks_answer_or_token(game, clock):
    p, token = game.add_player("Ada")
    game.start()
    game.submit_answer(p.id, 0, 0)
    game.submit_bot_answer("bot1", 0, 0, 500, {"cost_usd": 0.01})
    for view in (game.public_view(), game.host_view()):
        text = _dump(view)
        assert token not in text and game.players[p.id].token_hash not in text
        assert "answer_index" not in text and "source_quote" not in text and "explanation" not in text
        assert view["leaderboard"] is None
        assert "because 0" not in text
    assert game.public_view()["question"]["prompt"] == "Question 0?"
    game.close_question()
    view = game.public_view()
    assert view["reveal"]["answer_index"] == 0
    assert token not in _dump(view)


def test_public_view_hides_scores_until_reveal(game, clock):
    p, _ = game.add_player("Ada")
    game.start()
    game.submit_answer(p.id, 0, 0)
    assert game.leaderboard_event()["top"][0]["score"] == 0
    game.close_question()
    assert game.leaderboard_event(p.id)["you"] == {"rank": 1, "score": 1000}


def test_reveal_event_refused_before_close(game):
    game.start()
    with err("wrong_state"):
        game.reveal_event(0)


def test_reveal_marks_pending_bots(game):
    game.start()
    rev = game.close_question()
    assert all(b["pending"] for b in rev["bots"])


def test_host_view_counts(game, clock):
    p, _ = game.add_player("Ada")
    game.start()
    game.submit_answer(p.id, 0, 1)
    game.submit_bot_answer("bot2", 0, 1, 100, {"cost_usd": 0.5})
    hv = game.host_view()
    assert hv["answered_humans"] == 1 and hv["spent_usd"] == 0.5 and hv["budget_usd"] == 1.0
    assert [b["answered"] for b in hv["bots_status"]] == [False, True]


def test_lobby_and_answered_events(game):
    for i in range(35):
        game.add_player(f"p{i}")
    ev = game.lobby_event()
    assert ev["count"] == 35 and len(ev["names"]) == 30
    game.start()
    pid = next(iter(game.players))
    game.submit_answer(pid, 0, 0)
    game.submit_bot_answer("bot1", 0, 0, 1, {})
    ans = game.answered_event()
    assert ans == {"t": "answered", "q": 0, "count": 2, "humans": 1, "bots_done": ["bot1"]}


def test_leaderboard_ranking(game, clock):
    a, _ = game.add_player("A")
    b, _ = game.add_player("B")
    c, _ = game.add_player("C")
    game.start()
    game.submit_answer(b.id, 0, 0)
    clock.advance(10)
    game.submit_answer(a.id, 0, 0)
    game.close_question()
    lb = game.show_leaderboard()
    assert [r["nickname"] for r in lb["top"]] == ["B", "A", "C"]
    assert lb["room_vs_models"][0]["id"] == "room"
    assert game.rank_of(c.id) == 3


def test_mode_and_title_in_view(clock):
    g = Game(game_id="g", room_code="R", questions=make_questions(1), bots=[], clock=clock,
             mode=Mode.CLOSED_BOOK, title="Runbook")
    v = g.public_view()
    assert v["mode"] == "closed_book" and v["title"] == "Runbook" and v["state"] == "lobby"
