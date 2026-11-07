"""Pricing tests: catalog parse (extract of the real public catalog, 2026-11-07), missing price, fallback,
fetch with an injected opener (no network)."""

from __future__ import annotations

import io
import json

import pytest

from popquiz_plugin.bots import LlmReply
from popquiz_plugin.pricing import Pricing, fetch_catalog, load_pricing

# Extract of https://inference-api.nousresearch.com/v1/models fetched 2026-11-07 00:30 UTC.
CATALOG = json.loads(r"""{"data": [{"id": "inception/mercury-3", "canonical_slug": "inception/mercury-3-20261019", "pricing": {"prompt": "0.0000001000", "completion": "0.0000003500", "input_cache_read": "0.0000000100", "original": {"prompt": "0.0000002000", "completion": "0.0000007000", "input_cache_read": "0.0000000200"}}, "aliases": ["inception/mercury-3-20261019"]}, {"id": "x-ai/grok-4.3", "canonical_slug": "x-ai/grok-4.3-20260430", "pricing": {"prompt": "0.0000012500", "completion": "0.0000025000", "web_search": "0.0050000000", "input_cache_read": "0.0000002000", "overrides": [{"min_prompt_tokens": 200000, "prompt": "0.0000025", "completion": "0.000005", "input_cache_read": "0.0000004"}]}, "aliases": ["x-ai/grok-4.3-20260430"]}, {"id": "deepseek/deepseek-v4-flash", "canonical_slug": "deepseek/deepseek-v4-flash-20260423", "pricing": {"prompt": "0.0000000910", "completion": "0.0000001820", "input_cache_read": "0.0000000182"}, "aliases": ["deepseek/deepseek-v4-flash-20260423"]}, {"id": "openai/gpt-oss-120b", "canonical_slug": "openai/gpt-oss-120b", "pricing": {"prompt": "0.0000000160", "completion": "0.0000000570", "input_cache_read": "0.0000000140"}, "aliases": ["openai/gpt-oss-120b"]}, {"id": "anthropic/claude-haiku-5.5", "canonical_slug": "anthropic/claude-haiku-5.5", "pricing": {"prompt": "0.0000001000", "completion": "0.0000005000", "input_cache_read": "0.0000000100", "input_cache_write": "0.0000001250"}, "aliases": ["anthropic/claude-haiku-5.5"]}, {"id": "anthropic/claude-opus-5.5", "canonical_slug": "anthropic/claude-opus-5.5", "pricing": {"prompt": "0.0000040000", "completion": "0.0000200000", "input_cache_read": "0.0000002000", "input_cache_write": "0.0000050000"}, "aliases": ["anthropic/claude-opus-5.5"]}]}""")

DEFAULTS = ["anthropic/claude-opus-5.5", "deepseek/deepseek-v4-flash", "inception/mercury-3"]


def test_catalog_parse_defaults():
    p = Pricing.from_catalog(CATALOG)
    opus = p.price_for("anthropic/claude-opus-5.5")
    assert opus.prompt == pytest.approx(4e-6) and opus.completion == pytest.approx(20e-6)
    assert opus.input_cache_read == pytest.approx(0.2e-6) and opus.input_cache_write == pytest.approx(5e-6)
    ds = p.price_for("deepseek/deepseek-v4-flash")
    assert (ds.prompt, ds.completion) == (pytest.approx(0.091e-6), pytest.approx(0.182e-6))
    assert ds.input_cache_write == 0.0
    assert p.source == "catalog"


def test_catalog_aliases_and_case():
    p = Pricing.from_catalog(CATALOG)
    assert p.price_for("inception/mercury-3-20261019").prompt == pytest.approx(0.1e-6)
    assert p.price_for("Inception/Mercury-3") is not None


def test_missing_price_returns_none_and_zero_cost():
    p = Pricing.from_catalog(CATALOG)
    assert p.price_for("nobody/model-x") is None
    assert p.price_for("") is None
    assert p.cost("nobody/model-x", LlmReply(None, "", "", 1000, 1000)) == 0.0


@pytest.mark.parametrize("bad", [
    {"id": "a/neg", "pricing": {"prompt": "-1", "completion": "0.1"}},
    {"id": "a/nan", "pricing": {"prompt": "abc", "completion": "0.1"}},
    {"id": "a/none", "pricing": {"prompt": "0.1"}},
    {"id": "a/nopricing"},
    {"pricing": {"prompt": "1", "completion": "1"}},
    "not a dict",
])
def test_catalog_skips_invalid_entries(bad):
    p = Pricing.from_catalog({"data": [bad]})
    assert len(p) == 0


def test_catalog_garbage_shapes():
    assert len(Pricing.from_catalog({})) == 0
    assert len(Pricing.from_catalog({"data": "x"})) == 0
    assert len(Pricing.from_catalog([])) == 0


def test_cost_formula_all_token_kinds():
    p = Pricing.from_catalog(CATALOG)
    reply = LlmReply(None, "", "anthropic/claude-opus-5.5", input_tokens=1000, output_tokens=500,
                     cache_read_tokens=7000, cache_write_tokens=2000)
    expected = 1000 * 4e-6 + 500 * 20e-6 + 7000 * 0.2e-6 + 2000 * 5e-6
    assert p.cost("anthropic/claude-opus-5.5", reply) == pytest.approx(expected)


def test_fallback_table_covers_required_models():
    fb = Pricing.fallback()
    for model in DEFAULTS + ["anthropic/claude-haiku-5.5", "openai/gpt-oss-120b", "x-ai/grok-4.3"]:
        assert fb.price_for(model) is not None, model
    assert fb.as_of == "2026-11-07" and fb.source == "fallback"


def test_fallback_matches_catalog():
    fb, cat = Pricing.fallback(), Pricing.from_catalog(CATALOG)
    for model in cat.prices:
        if fb.price_for(model) is None:
            continue
        a, b = fb.price_for(model), cat.price_for(model)
        assert (a.prompt, a.completion, a.input_cache_read, a.input_cache_write) == pytest.approx(
            (b.prompt, b.completion, b.input_cache_read, b.input_cache_write)), model


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_fetch_catalog_with_fake_opener():
    seen = {}

    def opener(request, timeout):
        seen["url"], seen["timeout"] = request.full_url, timeout
        return _Resp(json.dumps(CATALOG).encode())

    data = fetch_catalog(timeout=3, opener=opener)
    assert data == CATALOG
    assert seen == {"url": "https://inference-api.nousresearch.com/v1/models", "timeout": 3}


@pytest.mark.parametrize("behaviour", ["raise", "bad_json", "wrong_shape"])
def test_fetch_catalog_never_raises(behaviour):
    def opener(request, timeout):
        if behaviour == "raise":
            raise OSError("offline")
        if behaviour == "bad_json":
            return _Resp(b"<html>")
        return _Resp(b'{"models": []}')

    assert fetch_catalog(opener=opener) is None
    assert load_pricing(opener=opener).source == "fallback"


def test_load_pricing_uses_catalog():
    p = load_pricing(opener=lambda r, timeout: _Resp(json.dumps(CATALOG).encode()))
    assert p.source == "catalog" and p.price_for("x-ai/grok-4.3") is not None


def test_estimate_question_cost_orders_models():
    p = Pricing.fallback()
    costs = {m: p.estimate_question_cost(m, 8000) for m in DEFAULTS}
    assert costs["anthropic/claude-opus-5.5"] > 10 * costs["deepseek/deepseek-v4-flash"]
    assert p.estimate_question_cost("nobody/x", 8000) == 0.0
