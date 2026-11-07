"""Cassette tests: record then replay, missing key, recorded errors, sample cassette coverage."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from popquiz_plugin.anchor import validate_questions
from popquiz_plugin.bots import LlmReply, ask_bot, ask_bots, build_prompt
from popquiz_plugin.cassette import CassetteLlm, cassette_key
from popquiz_plugin.model import BotSpec, Mode, Question, default_bots
from popquiz_plugin.pricing import Pricing

SPEC = BotSpec("bot3", "Mercury 3", "nous", "inception/mercury-3")
DOC = "Audit logs are kept for 400 days. Backups are kept for 35 days."
Q = Question(index=0, prompt="How long are audit logs kept?", choices=["35 days", "400 days"], answer_index=1,
             source_quote="Audit logs are kept for 400 days")


def test_key_is_stable_and_sensitive():
    k = cassette_key("m", "open_book", "p", ["a", "b"])
    assert k == cassette_key("m", "open_book", "p", ["a", "b"]) and len(k) == 64
    assert k != cassette_key("m", "closed_book", "p", ["a", "b"])
    assert k != cassette_key("m", "open_book", "p", ["b", "a"])
    assert k != cassette_key("other", "open_book", "p", ["a", "b"])


def test_constructor_errors(tmp_path):
    with pytest.raises(FileNotFoundError):
        CassetteLlm(tmp_path / "missing.json")
    with pytest.raises(ValueError):
        CassetteLlm(tmp_path / "x.json", mode="record")
    with pytest.raises(ValueError):
        CassetteLlm(tmp_path / "x.json", mode="bogus")


async def test_record_then_replay(tmp_path):
    path = tmp_path / "c.json"
    calls = []

    async def inner(spec, instructions, input_text, schema, timeout_s):
        calls.append(spec.id)
        await asyncio.sleep(0.02)
        return LlmReply({"choice": "B"}, '{"choice": "B"}', spec.model, 120, 9, 3, 1)

    rec = CassetteLlm(path, mode="record", inner=inner)
    r1 = await ask_bot(SPEC, Q, DOC, Mode.OPEN_BOOK, rec, Pricing.fallback())
    assert r1.choice == 1 and calls == ["bot3"]
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == 1 and len(data["entries"]) == 1
    entry = next(iter(data["entries"].values()))
    assert entry["prompt"] == Q.prompt and entry["choices"] == Q.choices and entry["mode"] == "open_book"
    assert entry["latency_ms"] >= 15 and entry["reply"]["input_tokens"] == 120

    play = CassetteLlm(path, speed=0)
    r2 = await ask_bot(SPEC, Q, "a different document text", Mode.OPEN_BOOK, play, Pricing.fallback())
    assert (r2.choice, r2.input_tokens, r2.output_tokens, r2.cache_read_tokens) == (1, 120, 9, 3)
    assert r2.cost_usd == pytest.approx(r1.cost_usd)
    assert play.misses == []


async def test_replay_sleeps_latency_over_speed(tmp_path):
    path = tmp_path / "c.json"
    _, text = build_prompt(Q, DOC, Mode.OPEN_BOOK)
    key = cassette_key(SPEC.model, "open_book", Q.prompt, Q.choices)
    path.write_text(json.dumps({"version": 1, "entries": {key: {
        "latency_ms": 400, "error": None, "reply": {"parsed": {"choice": "A"}, "text": "", "model": SPEC.model}}}}))
    play = CassetteLlm(path, speed=4)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    reply = await play(SPEC, "i", text, {}, 20)
    assert 0.08 <= loop.time() - t0 < 0.5 and reply.parsed == {"choice": "A"}


async def test_missing_key_counts_invalid(tmp_path, caplog):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"version": 1, "entries": {}}))
    play = CassetteLlm(path, speed=0)
    r = await ask_bot(SPEC, Q, DOC, Mode.OPEN_BOOK, play, None)
    assert r.choice is None and r.error == "invalid_json"
    assert len(play.misses) == 1 and "cassette miss" in caplog.text


@pytest.mark.parametrize("error,expected", [("timeout", "timeout"), ("trust_denied", "trust_denied"),
                                            ("error", "error")])
async def test_recorded_errors_replay(tmp_path, error, expected):
    path = tmp_path / "c.json"
    key = cassette_key(SPEC.model, "closed_book", Q.prompt, Q.choices)
    path.write_text(json.dumps({"version": 1, "entries": {key: {"latency_ms": 10, "error": error, "reply": None}}}))
    r = await ask_bot(SPEC, Q, DOC, Mode.CLOSED_BOOK, CassetteLlm(path, speed=0), None)
    assert r.error == expected and r.choice is None


async def test_record_exception_is_saved_and_reraised(tmp_path):
    path = tmp_path / "c.json"

    async def inner(spec, instructions, input_text, schema, timeout_s):
        raise TimeoutError()

    rec = CassetteLlm(path, mode="record", inner=inner)
    r = await ask_bot(SPEC, Q, DOC, Mode.OPEN_BOOK, rec, None)
    assert r.error == "timeout"
    entry = next(iter(json.loads(path.read_text())["entries"].values()))
    assert entry["error"] == "timeout"


# ---------------------------------------------------------------- shipped sample cassette
async def test_sample_cassette_covers_sample_questions(plugin_root: Path):
    doc = (plugin_root / "sample" / "runbook.md").read_text(encoding="utf-8")
    data = json.loads((plugin_root / "sample" / "questions.json").read_text(encoding="utf-8"))
    questions = validate_questions(data["questions"], doc).accepted
    play = CassetteLlm(plugin_root / "sample" / "cassette.json", speed=0)
    assert len(play) == 24
    wrong = {b.id: 0 for b in default_bots()}
    latencies: dict[str, list[int]] = {b.id: [] for b in default_bots()}
    for q in questions:
        results = await ask_bots(default_bots(), q, doc, Mode.OPEN_BOOK, play, Pricing.fallback())
        for r in results:
            assert r.error is None and r.choice is not None
            wrong[r.bot_id] += int(r.choice != q.answer_index)
            latencies[r.bot_id].append(play.entries[play.key_for(
                next(b for b in default_bots() if b.id == r.bot_id), build_prompt(q, doc, Mode.OPEN_BOOK)[1])]["latency_ms"])
    assert play.misses == []
    assert wrong["bot1"] == 1 and wrong["bot2"] == 2
    assert max(latencies["bot3"]) < 1500
    assert all(2000 <= x <= 5000 for x in latencies["bot2"])
    assert all(6000 <= x <= 16000 for x in latencies["bot1"])
