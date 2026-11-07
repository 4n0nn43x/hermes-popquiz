"""Model prices for the cost shown on screen (SPEC section 7).

Source: the public Nous Portal catalog ``https://inference-api.nousresearch.com/v1/models`` (no key).
Each entry has ``pricing`` with string USD amounts per token: ``prompt``, ``completion`` and optionally
``input_cache_read`` and ``input_cache_write``. Tiered ``overrides`` (long prompt pricing) are ignored:
quiz prompts stay far below their thresholds.

Choices made where the SPEC is silent:
- ``cost()`` returns 0.0 for an unknown model (callers check ``price_for`` to show "price unknown").
- ``Pricing.from_catalog`` skips entries with missing, non numeric or negative ``prompt``/``completion``
  and also indexes ``aliases`` (a list, or one string) and ``canonical_slug``; any other shape is ignored;
  lookups are case-insensitive.
- ``fetch_catalog`` accepts an injectable ``opener`` (``urllib.request.urlopen``-like) for tests and
  returns None on any failure; ``load_pricing`` = catalog if available, else the embedded fallback.
- ``estimate_question_cost`` gives a rough per-question estimate (4 chars per token) used for the
  budget plan and the "estimated cost" shown before a game.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

CATALOG_URL = "https://inference-api.nousresearch.com/v1/models"
FALLBACK_AS_OF = "2026-11-07"
M = 1_000_000


@dataclass(frozen=True)
class ModelPrice:
    model: str
    prompt: float  # USD per input token
    completion: float  # USD per output token
    input_cache_read: float = 0.0
    input_cache_write: float = 0.0


# USD per million tokens, verified on the public catalog 2026-11-07 00:30 UTC.
_FALLBACK_PER_M: dict[str, tuple[float, float, float, float]] = {
    "anthropic/claude-opus-5.5": (4.00, 20.00, 0.20, 5.00),
    "deepseek/deepseek-v4-flash": (0.091, 0.182, 0.0182, 0.0),
    "inception/mercury-3": (0.10, 0.35, 0.01, 0.0),
    "anthropic/claude-haiku-5.5": (0.10, 0.50, 0.01, 0.125),
    "openai/gpt-oss-120b": (0.016, 0.057, 0.014, 0.0),
    "x-ai/grok-4.3": (1.25, 2.50, 0.20, 0.0),
}


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out < 0 or out == float("inf"):
        return None
    return out


class Pricing:
    def __init__(self, prices: dict[str, ModelPrice], source: str, as_of: str = "") -> None:
        self.prices = prices
        self.source = source
        self.as_of = as_of
        self._index = {k.casefold(): v for k, v in prices.items()}

    def __len__(self) -> int:
        return len(self.prices)

    @classmethod
    def from_catalog(cls, data: dict | list) -> "Pricing":
        items = data.get("data", []) if isinstance(data, dict) else data
        prices: dict[str, ModelPrice] = {}
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            p = item.get("pricing")
            if not isinstance(p, dict):
                continue
            prompt, completion = _num(p.get("prompt")), _num(p.get("completion"))
            if prompt is None or completion is None:
                continue
            price = ModelPrice(
                model=item["id"], prompt=prompt, completion=completion,
                input_cache_read=_num(p.get("input_cache_read")) or 0.0,
                input_cache_write=_num(p.get("input_cache_write")) or 0.0,
            )
            prices[item["id"]] = price
            aliases = item.get("aliases")
            aliases = [aliases] if isinstance(aliases, str) else aliases if isinstance(aliases, list) else []
            names = [item.get("canonical_slug")] + aliases
            for alias in names:
                if isinstance(alias, str) and alias and alias not in prices:
                    prices[alias] = price
        return cls(prices, source="catalog")

    @classmethod
    def fallback(cls) -> "Pricing":
        prices = {
            model: ModelPrice(model, i / M, o / M, cr / M, cw / M)
            for model, (i, o, cr, cw) in _FALLBACK_PER_M.items()
        }
        return cls(prices, source="fallback", as_of=FALLBACK_AS_OF)

    def price_for(self, model: str) -> ModelPrice | None:
        if not model:
            return None
        return self.prices.get(model) or self._index.get(model.casefold())

    def cost(self, model: str, reply: Any) -> float:
        """USD cost of one reply: in*prompt + out*completion + cache_read*read + cache_write*write."""
        price = self.price_for(model)
        if price is None:
            return 0.0

        def tokens(name: str) -> int:
            try:
                return max(0, int(getattr(reply, name, 0) or 0))
            except (TypeError, ValueError):
                return 0

        return (tokens("input_tokens") * price.prompt + tokens("output_tokens") * price.completion
                + tokens("cache_read_tokens") * price.input_cache_read
                + tokens("cache_write_tokens") * price.input_cache_write)

    def estimate_question_cost(self, model: str, input_chars: int, output_tokens: int = 300) -> float:
        price = self.price_for(model)
        if price is None:
            return 0.0
        return (max(0, input_chars) / 4) * price.prompt + max(0, output_tokens) * price.completion


def fetch_catalog(timeout: float = 5, opener: Callable[..., Any] | None = None,
                  url: str = CATALOG_URL) -> dict | None:
    """GET the public catalog. Never raises: returns None on any network or format failure."""
    open_fn = opener or urllib.request.urlopen
    try:
        request = urllib.request.Request(url, headers={"Accept": "application/json",
                                                       "User-Agent": "hermes-popquiz"})
        with open_fn(request, timeout=timeout) as resp:
            raw = resp.read(20_000_000)
        data = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except Exception:  # noqa: BLE001 - offline, DNS, HTTP error, bad JSON: all fall back
        return None
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        return None
    return data


def load_pricing(timeout: float = 5, opener: Callable[..., Any] | None = None) -> Pricing:
    """Catalog prices if available, else the embedded fallback. Never raises (SPEC section 7)."""
    data = fetch_catalog(timeout=timeout, opener=opener)
    if data is not None:
        try:
            pricing = Pricing.from_catalog(data)
        except Exception:  # noqa: BLE001 - an unexpected catalog shape must not break a game
            pricing = None
        if pricing is not None and len(pricing):
            return pricing
    return Pricing.fallback()
