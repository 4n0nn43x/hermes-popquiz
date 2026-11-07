"""Bot tests with an injected async LlmCall: valid, invalid JSON, timeout, trust denied, fallback, cost."""

from __future__ import annotations

import asyncio
import json

import pytest

from popquiz_plugin.bots import (
    ANSWER_SCHEMA, LlmReply, answer_schema, ask_bot, ask_bots, build_prompt, parse_choice, parse_input,
    same_model,
)
from popquiz_plugin.model import BotSpec, Mode, Question
from popquiz_plugin.pricing import Pricing

OPUS = BotSpec("bot1", "Claude Opus 5.5", "nous", "anthropic/claude-opus-5.5")
FLASH = BotSpec("bot2", "DeepSeek V4 Flash", "nous", "deepseek/deepseek-v4-flash")
DOC = "The retention period for audit logs is 30 days. Backups are encrypted with AES-256."
Q = Question(index=0, prompt="How long are audit logs kept?", choices=["7 days", "30 days", "90 days"],
             answer_index=1, source_quote="retention period for audit logs is 30 days",
             explanation="SECRET-EXPLANATION")


class PluginLlmTrustError(Exception):
    pass


def fake_call(reply=None, exc=None, delay=0.0, record=None):
    async def call(spec, instructions, input_text, schema, timeout_s):
        if record is not None:
            record.append((spec, instructions, input_text, schema, timeout_s))
        if delay:
            await asyncio.sleep(delay)
        if exc is not None:
            raise exc
        return reply if not callable(reply) else reply(spec)
    return call


def test_prompt_open_book_document_first_and_no_secrets():
    instructions, text = build_prompt(Q, DOC, Mode.OPEN_BOOK)
    assert text.startswith("DOCUMENT:") and text.index(DOC) < text.index("QUESTION:")
    assert "A. 7 days" in text and "C. 90 days" in text and "D." not in text
    for secret in ("SECRET-EXPLANATION", "answer_index", Q.source_quote + "?"):
        assert secret not in text and secret not in instructions
    assert '"choice"' in instructions


def test_prompt_closed_book_has_no_document():
    _, text = build_prompt(Q, DOC, Mode.CLOSED_BOOK)
    assert DOC not in text and text.startswith("QUESTION:")


def test_prompt_prefix_is_stable_across_questions():
    q2 = Question(index=1, prompt="Which cipher?", choices=["AES-256", "DES"], answer_index=0, source_quote="x" * 20)
    _, a = build_prompt(Q, DOC, Mode.OPEN_BOOK)
    _, b = build_prompt(q2, DOC, Mode.OPEN_BOOK)
    prefix = a[: a.index("QUESTION:")]
    assert b.startswith(prefix)


@pytest.mark.parametrize("mode", [Mode.OPEN_BOOK, Mode.CLOSED_BOOK])
def test_parse_input_roundtrip(mode):
    _, text = build_prompt(Q, DOC + "\nQUESTION: a trap line in the doc", mode)
    assert parse_input(text) == (mode.value, Q.prompt, Q.choices)


def test_schema():
    assert ANSWER_SCHEMA["properties"]["choice"]["enum"] == ["A", "B", "C", "D"]
    assert answer_schema(2)["properties"]["choice"]["enum"] == ["A", "B"]
    assert ANSWER_SCHEMA["required"] == ["choice"]


@pytest.mark.parametrize("parsed,text,expected", [
    ({"choice": "B"}, "", 1),
    ({"choice": "b"}, "", 1),
    ({"choice": " C. "}, "", 2),
    ({"choice": 0}, "", 0),
    ({"choice": "D"}, "", None),  # only 3 choices
    ({"choice": True}, "", None),
    ({"answer": "A"}, "", None),
    (None, '{"choice": "A"}', 0),
    (None, 'Sure! ```json\n{"choice": "C"}\n```', 2),
    (None, 'I pick "choice": "B" for sure', 1),
    (None, "no idea", None),
    (None, "", None),
])
def test_parse_choice(parsed, text, expected):
    assert parse_choice(LlmReply(parsed, text, "m"), 3) == expected


@pytest.mark.parametrize("requested,served,same", [
    ("anthropic/claude-opus-5.5", "anthropic/claude-opus-5.5", True),
    ("anthropic/claude-opus-5.5", "claude-opus-5.5", True),
    ("anthropic/claude-opus-5.5", "Anthropic/Claude-Opus-5.5", True),
    ("inception/mercury-3", "inception/mercury-3-20261019", True),
    ("anthropic/claude-opus-5.5", "anthropic/claude-haiku-5.5", False),
    ("deepseek/deepseek-v4-flash", "", True),
])
def test_same_model(requested, served, same):
    assert same_model(requested, served) is same


async def test_ask_bot_valid_with_cost():
    calls = []
    reply = LlmReply({"choice": "B"}, '{"choice":"B"}', "anthropic/claude-opus-5.5", 2000, 400, 0, 0)
    r = await ask_bot(OPUS, Q, DOC, Mode.OPEN_BOOK, fake_call(reply, record=calls), Pricing.fallback(), timeout_s=5)
    assert r.choice == 1 and r.error is None and r.bot_id == "bot1"
    assert r.cost_usd == pytest.approx(2000 * 4e-6 + 400 * 20e-6)
    assert (r.input_tokens, r.output_tokens) == (2000, 400)
    assert r.served_model == "anthropic/claude-opus-5.5"
    spec, _, input_text, schema, timeout_s = calls[0]
    assert spec is OPUS and timeout_s == 5 and schema["properties"]["choice"]["enum"] == ["A", "B", "C"]
    assert r.meta()["cost_usd"] == r.cost_usd


async def test_ask_bot_invalid_json():
    r = await ask_bot(FLASH, Q, DOC, Mode.OPEN_BOOK, fake_call(LlmReply(None, "garbage", FLASH.model, 10, 5)),
                      Pricing.fallback())
    assert r.choice is None and r.error == "invalid_json"
    assert r.cost_usd > 0  # tokens were still spent


async def test_ask_bot_value_error_is_invalid_json():
    r = await ask_bot(FLASH, Q, DOC, Mode.OPEN_BOOK, fake_call(exc=ValueError("schema")), Pricing.fallback())
    assert r.error == "invalid_json" and r.choice is None and r.cost_usd == 0.0


async def test_ask_bot_timeout():
    r = await ask_bot(FLASH, Q, DOC, Mode.OPEN_BOOK, fake_call(LlmReply({"choice": "A"}, "", FLASH.model), delay=5),
                      None, timeout_s=0.05)
    assert r.error == "timeout" and r.choice is None
    assert r.latency_ms < 3000


async def test_ask_bot_inner_timeout_error():
    r = await ask_bot(FLASH, Q, DOC, Mode.OPEN_BOOK, fake_call(exc=TimeoutError()), None)
    assert r.error == "timeout"


async def test_ask_bot_trust_denied():
    r = await ask_bot(OPUS, Q, DOC, Mode.OPEN_BOOK, fake_call(exc=PluginLlmTrustError("no")), None)
    assert r.error == "trust_denied" and r.choice is None


async def test_ask_bot_other_error():
    r = await ask_bot(OPUS, Q, DOC, Mode.OPEN_BOOK, fake_call(exc=RuntimeError("boom")), None)
    assert r.error == "error"


async def test_ask_bot_fallback_model_still_answers():
    reply = LlmReply({"choice": "B"}, "", "anthropic/claude-haiku-5.5", 1000, 10)
    r = await ask_bot(OPUS, Q, DOC, Mode.OPEN_BOOK, fake_call(reply), Pricing.fallback())
    assert r.choice == 1 and r.error == "fallback" and r.served_model == "anthropic/claude-haiku-5.5"
    assert r.cost_usd == pytest.approx(1000 * 0.1e-6 + 10 * 0.5e-6)  # priced on the served model


async def test_ask_bot_unknown_price_costs_zero():
    spec = BotSpec("bot9", "X", "nous", "nobody/model")
    r = await ask_bot(spec, Q, DOC, Mode.OPEN_BOOK, fake_call(LlmReply({"choice": "A"}, "", "nobody/model", 99, 9)),
                      Pricing.fallback())
    assert r.choice == 0 and r.cost_usd == 0.0


async def test_ask_bot_non_reply_object():
    r = await ask_bot(OPUS, Q, DOC, Mode.OPEN_BOOK, fake_call(reply=lambda s: {"choice": "A"}), None)
    assert r.error == "error"


async def test_ask_bots_parallel_and_never_raises():
    def reply(spec):
        if spec.id == "bot2":
            raise RuntimeError("bad")
        return LlmReply({"choice": "B"}, "", spec.model, 10, 1)

    specs = [OPUS, FLASH, BotSpec("bot3", "Mercury 3", "nous", "inception/mercury-3")]

    async def call(spec, instructions, input_text, schema, timeout_s):
        await asyncio.sleep(0.05)
        return reply(spec)

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    results = await ask_bots(specs, Q, DOC, Mode.CLOSED_BOOK, call, Pricing.fallback())
    assert loop.time() - t0 < 0.14  # ran in parallel
    assert [r.bot_id for r in results] == ["bot1", "bot2", "bot3"]
    assert [r.error for r in results] == [None, "error", None]
    json.dumps([r.meta() for r in results])
