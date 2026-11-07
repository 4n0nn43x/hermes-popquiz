"""Conductor tests with the real engine and a fake hub (no network)."""

from __future__ import annotations

import asyncio
import json

import pytest

from popquiz_plugin.bots import LlmReply
from popquiz_plugin.conductor import Conductor
from popquiz_plugin.engine import Game
from popquiz_plugin.model import BotSpec, GameState, Mode, Question

QS = [Question(index=i, prompt=f"Question number {i}?", choices=["Red", "Blue", "Green", "Gold"], answer_index=i % 4,
               source_quote="some quote that is long", explanation="because", time_limit_s=2) for i in range(3)]
BOTS = [BotSpec("bot1", "Opus", "nous", "anthropic/claude-opus-5.5"),
        BotSpec("bot2", "Flash", "nous", "deepseek/deepseek-v4-flash")]


class FakeHub:
    def __init__(self):
        self.sent: list[tuple[dict, object]] = []
        self.events: list[dict] = []
        self.dirty: list[str] = []
        self.finished = 0

    async def broadcast(self, msg, personal=None):
        self.sent.append((json.loads(json.dumps(msg)), personal))

    def mark_dirty(self, kind):
        self.dirty.append(kind)

    def _notify(self, event):
        self.events.append(event)

    def on_finished(self):
        self.finished += 1

    def kinds(self):
        return [m["t"] for m, _ in self.sent]

    def of(self, t):
        return [m for m, _ in self.sent if m["t"] == t]


def llm_factory(delays=None, served=None, fail=None):
    delays = delays or {}

    async def llm(spec, instructions, input_text, schema, timeout_s):
        await asyncio.sleep(delays.get(spec.id, 0.01))
        if fail and spec.id in fail:
            raise fail[spec.id]
        q = next(q for q in QS if q.prompt in input_text)
        letter = "ABCD"[q.answer_index]
        return LlmReply(parsed={"choice": letter}, text="", model=(served or {}).get(spec.id, spec.model),
                        input_tokens=1000, output_tokens=10)

    return llm


class Price:
    def __init__(self, per_question):
        self.per_question = per_question

    def price_for(self, model):
        return object()

    def cost(self, model, reply):
        return self.per_question.get(model, 0.0)

    def estimate_question_cost(self, model, input_chars, output_tokens=300):
        return self.per_question.get(model, 0.0)


def make(llm=None, pricing=None, budget=1.0, auto=True, questions=QS, bots=BOTS):
    hub = FakeHub()
    game = Game(game_id="g", room_code="ABCDEF", questions=questions, bots=bots, clock=asyncio.get_running_loop().time,
                mode=Mode.OPEN_BOOK, budget_usd=budget)
    c = Conductor(game=game, questions=questions, bots=bots, doc_text="doc", mode=Mode.OPEN_BOOK, llm_call=llm,
                  pricing=pricing, hub=hub, auto_advance=auto, reveal_s=0.05, clock=asyncio.get_running_loop().time,
                  room="ABCDEF", budget_usd=budget, auto_advance_s=0.05, bot_grace_s=0.3)
    return c, game, hub


async def run_to_end(c, timeout=15):
    c.start()
    await asyncio.wait_for(c.task, timeout)


async def test_full_auto_game_closes_early_when_everyone_answered():
    c, game, hub = make(llm_factory())
    t0 = asyncio.get_running_loop().time()
    await run_to_end(c)
    assert game.state == GameState.FINISHED
    assert asyncio.get_running_loop().time() - t0 < 3  # 3 questions of 2 s each would take 6 s
    assert hub.kinds().count("question") == 3 and hub.kinds().count("reveal") == 3
    assert hub.kinds()[-1] == "finished" and hub.finished == 1
    assert game.bot_correct == {"bot1": 3, "bot2": 3}


async def test_question_message_never_contains_answer():
    c, game, hub = make(llm_factory())
    await run_to_end(c)
    for q in hub.of("question"):
        assert set(q) >= {"q", "total", "prompt", "choices", "ends_at_ms", "server_now_ms"}
        assert "answer_index" not in q and "source_quote" not in q and "explanation" not in q


async def test_bot_answers_not_broadcast_before_reveal():
    c, game, hub = make(llm_factory({"bot1": 0.2, "bot2": 0.01}))
    await run_to_end(c)
    first_reveal = hub.kinds().index("reveal")
    for m, _ in hub.sent[:first_reveal]:
        assert "bots" not in m


async def test_timeout_bot_marked_too_slow_and_game_continues():
    c, game, hub = make(llm_factory({"bot1": 5.0}))
    await run_to_end(c, timeout=20)
    reveal = hub.of("reveal")[0]
    bot1 = next(b for b in reveal["bots"] if b["id"] == "bot1")
    assert bot1["choice"] is None and bot1["error"] == "timeout" and bot1["correct"] is False
    assert game.state == GameState.FINISHED


async def test_fallback_model_flagged_but_scored():
    c, game, hub = make(llm_factory(served={"bot2": "other/cheap-model"}))
    await run_to_end(c)
    bot2 = next(b for b in hub.of("reveal")[0]["bots"] if b["id"] == "bot2")
    assert bot2["error"] == "fallback" and bot2["correct"] is True
    assert game.bot_scores["bot2"] > 0


async def test_trust_denied_recorded():
    class PluginLlmTrustError(Exception):
        pass

    c, game, hub = make(llm_factory(fail={"bot1": PluginLlmTrustError("no")}))
    await run_to_end(c)
    assert next(b for b in hub.of("reveal")[0]["bots"] if b["id"] == "bot1")["error"] == "trust_denied"


async def test_budget_stops_most_expensive_bot():
    pricing = Price({"anthropic/claude-opus-5.5": 0.4, "deepseek/deepseek-v4-flash": 0.001})
    c, game, hub = make(llm_factory(), pricing=pricing, budget=0.5)
    await run_to_end(c)
    errors = [next(b for b in r["bots"] if b["id"] == "bot1")["error"] for r in hub.of("reveal")]
    assert errors[0] is None and errors[1] == "budget" and errors[2] == "budget"
    assert all(next(b for b in r["bots"] if b["id"] == "bot2")["error"] is None for r in hub.of("reveal"))
    assert game.spent_usd <= 0.5 + 1e-9
    assert c.bots_status()[0]["stopped"] is True


async def test_estimated_total_uses_pricing():
    pricing = Price({"anthropic/claude-opus-5.5": 0.01, "deepseek/deepseek-v4-flash": 0.002})
    c, game, hub = make(llm_factory(), pricing=pricing)
    assert c.estimated_total_usd() == pytest.approx(0.036)
    c2, _, _ = make(llm_factory(), pricing=None)
    assert c2.estimated_total_usd() == 0.0


async def test_without_llm_call_bots_record_error():
    c, game, hub = make(None)
    await run_to_end(c)
    assert all(b["error"] == "error" for b in hub.of("reveal")[0]["bots"])


async def test_manual_next_and_skip():
    c, game, hub = make(llm_factory({"bot1": 1.5}), auto=False)
    c.start()
    await asyncio.sleep(0.2)
    assert game.state == GameState.QUESTION
    c.skip()
    await asyncio.sleep(0.6)
    assert game.state in (GameState.REVEAL, GameState.LEADERBOARD)
    await asyncio.sleep(0.2)
    assert game.state == GameState.LEADERBOARD
    c.next()
    await asyncio.sleep(0.1)
    assert game.state == GameState.QUESTION and game.current == 1
    await c.end()
    assert game.state == GameState.FINISHED and hub.kinds()[-1] == "finished"
    await c.shutdown()


async def test_next_in_lobby_starts_and_double_start_refused():
    c, game, hub = make(llm_factory({"bot1": 1.0, "bot2": 1.0}), auto=False)
    c.next()
    await asyncio.sleep(0.05)
    assert game.state == GameState.QUESTION
    c.start()  # already running: no-op
    await c.end()
    with pytest.raises(Exception):
        c2, g2, _ = make(llm_factory())
        g2.state = GameState.FINISHED
        c2.start()


async def test_personal_reveal_and_leaderboard():
    c, game, hub = make(llm_factory({"bot1": 0.3, "bot2": 0.3}))
    p, _ = game.add_player("Zoe")
    c.start()
    await asyncio.sleep(0.05)
    game.submit_answer(p.id, 0, QS[0].answer_index)
    await asyncio.wait_for(c.task, 15)
    reveal, personal = next((m, f) for m, f in hub.sent if m["t"] == "reveal")
    you = personal(p.id)
    assert you["correct"] is True and you["points"] > 0 and you["rank"] == 1
    assert personal("nobody") is None
    lb, lbf = next((m, f) for m, f in hub.sent if m["t"] == "leaderboard")
    assert lbf(p.id)["rank"] == 1 and lb["room_vs_models"][0]["id"] == "room"


async def test_snapshot_per_state():
    c, game, hub = make(llm_factory({"bot1": 0.5, "bot2": 0.5}), auto=False)
    p, _ = game.add_player("Kai")
    c.start()
    await asyncio.sleep(0.05)
    game.submit_answer(p.id, 0, 1)
    snap = c.snapshot(p.id)
    assert [m["t"] for m in snap] == ["question", "answered", "locked"]
    assert snap[2]["choice"] == 1
    c.skip()
    await asyncio.sleep(0.5)
    snap = c.snapshot(p.id)
    assert snap[0]["t"] == "reveal" and "you" in snap[0]
    await c.end()
    assert [m["t"] for m in c.snapshot(None)] == ["leaderboard", "finished"]


async def test_report_data_contents():
    c, game, hub = make(llm_factory())
    p, _ = game.add_player("Lea")
    c.start()
    await asyncio.sleep(0.02)
    game.submit_answer(p.id, 0, (QS[0].answer_index + 1) % 4)
    await asyncio.wait_for(c.task, 15)
    data = c.report_data()
    q0 = data["questions"][0]
    assert q0["correct_pct"] == 0.0 and q0["trap"]["count"] == 1 and q0["played"]
    assert {m["id"] for m in data["models"]} == {"bot1", "bot2"}
    assert "token" not in json.dumps(data).lower()
    assert c.report is not None  # report.build_report from PQ-A
