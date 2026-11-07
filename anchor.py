"""Quote anchoring and question validation (SPEC section 4). Stdlib only.

``normalize`` makes document and quote comparable (NFKC, typographic quotes, dashes, spaces and
ligatures to ASCII, PDF hyphenation joined, soft hyphens dropped, Markdown markers stripped,
casefold, whitespace collapsed). ``anchor`` finds a quote in the normalized document: exact match,
else a fuzzy window search seeded by the rarest quote words, with a number guard.

Choices made where the SPEC is silent:
- Markdown markers ``* _ ` # > | [ ] ( ) ~`` and every other non word symbol except ``' " % . , : ; ! ? -``
  become spaces on both sides (symmetric, so exact matching still works).
- Seeds: the 3 rarest quote words (>= 3 chars, present in the document, word bounded); at most
  30 occurrences per seed are examined; ``quick_ratio`` prunes windows that cannot win.
- Number guard: numbers (``\\d+([.,]\\d+)*``) of the quote must appear digit bounded in the chosen
  window. Among windows scoring >= 0.90 the best one passing the guard wins; if none passes,
  ``number_mismatch``. ``closest`` is the normalized best window passage (<= 160 chars).
  A fuzzy score is capped at 0.999: 1.0 always means an exact match.
- ``prompt_leaks_answer``: the normalized correct choice appears word bounded in the prompt while at
  least one wrong choice does not (a prompt listing every choice leaks nothing).
- ``quote_too_long``: a normalized quote over ``MAX_QUOTE`` chars is refused before any matching, which
  bounds the fuzzy search (difflib is quadratic in the quote length).
- ``validate_questions`` keeps the raw position as ``Question.index``; the caller renumbers (the
  engine does). Optional ``explanation`` (cut to 300 chars) and ``time_limit_s`` (5..120) are read.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, field

from .model import Question

MIN_QUOTE = 12
MAX_QUOTE = 400  # the question skill asks for 12 to 300 chars; this is the hard cap
THRESHOLD = 0.90
CLOSEST_MAX = 160
MAX_PROMPT = 240
MAX_CHOICE = 90
MAX_EXPLANATION = 300
MAX_SEED_HITS = 30

_TRANSLATE = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u2032": "'", "\u00b4": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"', "\u2033": '"', "\u00ab": '"', "\u00bb": '"',
    "\u2039": "'", "\u203a": "'",
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2015": "-", "\u2212": "-",
    "\u00a0": " ", "\u202f": " ", "\u2007": " ", "\u2009": " ", "\u200a": " ", "\u3000": " ",
    "\u200b": "", "\u200c": "", "\u200d": "", "\ufeff": "", "\u00ad": "",
    "\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi", "\ufb04": "ffl", "\ufb05": "st", "\ufb06": "st",
})
_HYPHEN_BREAK = re.compile(r"(\w)-[ \t]*\r?\n\s*(\w)")
_MARKDOWN = re.compile(r"[*_`#>|\[\]()~]")
_SYMBOLS = re.compile(r"[^\w\s'\"%.,:;!?-]")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_WORD = re.compile(r"\w+")
_SIGNIFICANT = re.compile(r"[^\W\d_]{4,}")


def normalize(text: str) -> str:
    """Pure and idempotent normalization used on both document and quotes."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text).translate(_TRANSLATE)
    text = _HYPHEN_BREAK.sub(r"\1\2", text)
    text = _MARKDOWN.sub(" ", text)
    text = unicodedata.normalize("NFKC", text.casefold()).translate(_TRANSLATE)
    text = _SYMBOLS.sub(" ", text)
    return " ".join(text.split())


@dataclass
class AnchorResult:
    ok: bool
    score: float
    code: str | None
    offset: int
    closest: str


def _numbers(text: str) -> list[str]:
    return _NUMBER.findall(text)


def _has_numbers(numbers: list[str], window: str) -> bool:
    return all(re.search(rf"(?<!\d){re.escape(n)}(?!\d)", window) for n in numbers)


def _seed_positions(words: list[str], doc_norm: str) -> list[int]:
    # ponytail: one regex scan of the document per quote word (MAX_QUOTE bounds it: about 0.5 s per
    # adversarial quote on a 400k char document); count the document words once if that matters.
    counts: dict[str, int] = {}
    for w in set(words):
        if len(w) < 3:
            continue
        n = len(re.findall(rf"(?<!\w){re.escape(w)}(?!\w)", doc_norm))
        if n:
            counts[w] = n
    seeds = sorted(counts, key=lambda w: (counts[w], -len(w)))[:3]
    positions: list[int] = []
    for seed in seeds:
        for i, m in enumerate(re.finditer(rf"(?<!\w){re.escape(seed)}(?!\w)", doc_norm)):
            if i >= MAX_SEED_HITS:
                break
            positions.append(m.start())
    return positions


def anchor(quote: str, doc_norm: str) -> AnchorResult:
    q = normalize(quote)
    if len(q) < MIN_QUOTE:
        return AnchorResult(False, 0.0, "quote_too_short", -1, "")
    if len(q) > MAX_QUOTE:
        return AnchorResult(False, 0.0, "quote_too_long", -1, "")
    pos = doc_norm.find(q)
    if pos >= 0:
        return AnchorResult(True, 1.0, None, pos, "")
    numbers = _numbers(q)
    best = (0.0, -1, "")  # score, offset, passage (any window)
    best_ok = (0.0, -1, "")  # best window that passes the number guard
    seen: set[int] = set()
    for p in _seed_positions(q.split(), doc_norm):
        lo = max(0, p - len(q))
        if lo // 8 in seen:
            continue
        seen.add(lo // 8)
        window = doc_norm[lo:p + 2 * len(q)]
        sm = difflib.SequenceMatcher(None, q, window, autojunk=False)
        upper = sm.quick_ratio() * (len(q) + len(window)) / 2 / len(q)
        if upper <= best_ok[0] and upper <= best[0]:
            continue
        blocks = [b for b in sm.get_matching_blocks() if b.size]
        if not blocks:
            continue
        score = sum(b.size for b in blocks) / len(q)
        start, end = blocks[0].b, blocks[-1].b + blocks[-1].size
        passage = window[start:end]
        if score > best[0]:
            best = (score, lo + start, passage)
        if score > best_ok[0] and _has_numbers(numbers, passage):
            best_ok = (score, lo + start, passage)
    if best_ok[0] >= THRESHOLD:
        return AnchorResult(True, _fuzzy(best_ok[0]), None, best_ok[1], "")
    closest = best[2][:CLOSEST_MAX]
    if best[0] >= THRESHOLD:
        return AnchorResult(False, _fuzzy(best[0]), "number_mismatch", best[1], closest)
    return AnchorResult(False, _fuzzy(best[0]), "quote_not_found", best[1], closest)


def _fuzzy(score: float) -> float:
    """Fuzzy scores stay below 1.0 so 1.0 always means an exact match."""
    return min(round(score, 3), 0.999)


@dataclass
class Validation:
    accepted: list[Question] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)


def _schema_error(item: object) -> str | None:
    if not isinstance(item, dict):
        return "question must be an object"
    prompt = item.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return "prompt must be a non-empty string"
    if len(prompt) > MAX_PROMPT:
        return f"prompt longer than {MAX_PROMPT} chars"
    choices = item.get("choices")
    if not isinstance(choices, list) or not 2 <= len(choices) <= 4:
        return "choices must be a list of 2 to 4 strings"
    for c in choices:
        if not isinstance(c, str) or not c.strip():
            return "every choice must be a non-empty string"
        if len(c) > MAX_CHOICE:
            return f"choice longer than {MAX_CHOICE} chars"
    idx = item.get("answer_index")
    if isinstance(idx, bool) or not isinstance(idx, int) or not 0 <= idx < len(choices):
        return "answer_index must be an integer index into choices"
    if not isinstance(item.get("source_quote"), str):
        return "source_quote must be a string quoted verbatim from the document"
    return None


def _bounded_in(needle: str, hay: str) -> bool:
    return bool(needle) and re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", hay) is not None


def _reject(i: int, code: str, detail: str, score: float | None = None, closest: str | None = None) -> dict:
    return {"index": i, "code": code, "detail": detail, "best_score": score, "closest_passage": closest}


def validate_questions(raw: list[dict], doc_text: str) -> Validation:
    doc_norm = normalize(doc_text)
    out = Validation()
    for i, item in enumerate(raw if isinstance(raw, list) else []):
        problem = _schema_error(item)
        if problem:
            out.rejected.append(_reject(i, "bad_schema", problem))
            continue
        prompt, choices, idx = item["prompt"].strip(), [c.strip() for c in item["choices"]], item["answer_index"]
        norm_choices = [normalize(c) for c in choices]
        if len(set(norm_choices)) != len(norm_choices) or not all(norm_choices):
            out.rejected.append(_reject(i, "duplicate_choice", "two choices are identical after normalization"))
            continue
        norm_prompt = normalize(prompt)
        if _bounded_in(norm_choices[idx], norm_prompt) and not all(
                _bounded_in(c, norm_prompt) for j, c in enumerate(norm_choices) if j != idx):
            out.rejected.append(_reject(i, "prompt_leaks_answer", "the correct choice appears in the prompt"))
            continue
        res = anchor(item["source_quote"], doc_norm)
        if not res.ok:
            detail = {
                "quote_too_short": f"source_quote must be at least {MIN_QUOTE} characters after normalization",
                "quote_too_long": f"source_quote must be at most {MAX_QUOTE} characters after normalization",
                "number_mismatch": "a number in source_quote is not in the matching document passage",
                "quote_not_found": "source_quote was not found verbatim in the document",
            }[res.code or "quote_not_found"]
            if res.code == "number_mismatch":
                missing = [n for n in _numbers(normalize(item["source_quote"])) if not _has_numbers([n], res.closest)]
                detail += f" (missing: {', '.join(missing)})" if missing else ""
            score = res.score if res.code not in ("quote_too_short", "quote_too_long") else None
            out.rejected.append(_reject(i, res.code or "quote_not_found", detail, score, res.closest or None))
            continue
        quote_norm = normalize(item["source_quote"])
        words = _SIGNIFICANT.findall(norm_choices[idx])
        nums = _numbers(norm_choices[idx])
        if (words and not any(_bounded_in(w, quote_norm) for w in words)) or (
                not words and nums and not any(_has_numbers([n], quote_norm) for n in nums)):
            out.warnings.append({"index": i, "code": "answer_not_in_quote",
                                 "detail": "no significant word of the correct choice appears in source_quote"})
        explanation = item.get("explanation")
        limit = item.get("time_limit_s")
        out.accepted.append(Question(
            index=i, prompt=prompt, choices=choices, answer_index=idx, source_quote=item["source_quote"].strip(),
            explanation=explanation.strip()[:MAX_EXPLANATION] if isinstance(explanation, str) else "",
            anchor_score=res.score, quote_offset=res.offset,
            time_limit_s=limit if isinstance(limit, int) and not isinstance(limit, bool) and 5 <= limit <= 120 else 20,
        ))
    return out
