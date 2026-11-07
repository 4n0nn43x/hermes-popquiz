"""Anchor tests: normalization, exact and fuzzy anchoring, number guard, validation codes, speed."""

from __future__ import annotations

import random
import time

import pytest

from popquiz_plugin.anchor import anchor, normalize, validate_questions

DOC = (
    "# Incident Runbook\n\n"
    "Les sauvegardes sont chiff-\nrees avec AES-256 et conservees 30 jours.\n"
    "En cas d\u2019incident, l\u2019astreinte appelle le **responsable** sous 15 minutes.\n"
    "The \ufb01rst responder opens a ticket in the \u201cSEV\u201d queue\u00a0within 5 minutes.\n"
    "Audit logs are kept for 400 days; error budgets reset every 28 days.\n"
    "Escalate to the database team when replication lag exceeds 120 seconds.\n"
    "Use `max_retries` set to 3 for the payment worker.\n"
)
DOC_N = normalize(DOC)


# ---------------------------------------------------------------- normalize
def test_normalize_basic():
    assert normalize("  Hello\n\tWORLD  ") == "hello world"


def test_normalize_hyphenation_joined():
    assert "chiffrees" in DOC_N


def test_normalize_ligatures_and_quotes():
    assert normalize("\ufb01rst \ufb02ow \u201cok\u201d d\u2019accord") == "first flow \"ok\" d'accord"


def test_normalize_dashes_and_spaces():
    assert normalize("pages 3\u20135\u00a0and 7\u20148") == "pages 3-5 and 7-8"


def test_normalize_soft_hyphen_and_zero_width():
    assert normalize("re\u00adcovery time\u200b") == "recovery time"


def test_normalize_markdown_stripped():
    assert normalize("## Title\n**bold** _it_ `code` [link](x)") == "title bold it code link x"


@pytest.mark.parametrize("text", [DOC, "\u00c9t\u00e9 STRASSE stra\u00dfe", "a-\n b", "x * y", "", "\ufb03 \u2163"])
def test_normalize_idempotent(text):
    once = normalize(text)
    assert normalize(once) == once


# ---------------------------------------------------------------- anchor
def test_exact_with_hyphenation():
    r = anchor("Les sauvegardes sont chiffrees avec AES-256 et conservees 30 jours.", DOC_N)
    assert r.ok and r.score == 1.0 and r.code is None and r.offset >= 0


def test_exact_with_typographic_quotes_and_markdown():
    r = anchor("l'astreinte appelle le responsable sous 15 minutes", DOC_N)
    assert r.ok and r.score == 1.0


def test_exact_with_ligature_and_nbsp():
    assert anchor("The first responder opens a ticket in the \"SEV\" queue within 5 minutes", DOC_N).ok


def test_inline_code_quote():
    assert anchor("Use max_retries set to 3 for the payment worker", DOC_N).score == 1.0


def test_fuzzy_small_rewording_accepted():
    r = anchor("En cas d'incident, l'astreinte appelle le responsable sous 15 min.", DOC_N)
    assert r.ok and 0.9 <= r.score < 1.0


def test_number_guard_regression_30_vs_90():
    r = anchor("Les sauvegardes sont chiffrees avec AES-256 et conservees 90 jours.", DOC_N)
    assert not r.ok and r.code == "number_mismatch" and r.score >= 0.9
    assert "30" in r.closest


def test_number_guard_digit_bounded():
    r = anchor("Audit logs are kept for 40 days; error budgets reset every 28 days.", DOC_N)
    assert not r.ok and r.code == "number_mismatch"


def test_rewritten_sentence_rejected():
    r = anchor("Les sauvegardes sont conservees 90 jours sur bande magnetique.", DOC_N)
    assert not r.ok and r.code in {"quote_not_found", "number_mismatch"} and r.score < 0.9


def test_not_found_has_closest_and_score():
    r = anchor("Escalate to the network team when packet loss exceeds two percent.", DOC_N)
    assert not r.ok and r.code == "quote_not_found"
    assert 0 < r.score < 0.9 and 0 < len(r.closest) <= 160


def test_nothing_in_common():
    r = anchor("zzzzzz qqqqqqq wwwwwwww", DOC_N)
    assert (r.ok, r.code, r.score, r.closest) == (False, "quote_not_found", 0.0, "")


@pytest.mark.parametrize("quote", ["30 jours", "  **short** ", ""])
def test_quote_too_short(quote):
    assert anchor(quote, DOC_N).code == "quote_too_short"


def test_threshold_boundary():
    base = "escalate to the database team when replication lag exceeds 120 seconds"
    assert anchor(base, DOC_N).score == 1.0
    damaged = base.replace("database", "dxtxbxsx").replace("replication", "rxplxcxtxon")
    r = anchor(damaged, DOC_N)
    assert not r.ok and r.score < 0.9


def _big_doc(chars: int = 50_000) -> str:
    rng = random.Random(7)
    words = ("service deploy rollback alert pager queue latency budget replica shard cache token "
             "incident review owner window threshold metric dashboard runbook").split()
    out, n = [], 0
    while n < chars:
        sent = " ".join(rng.choice(words) for _ in range(12)) + f" {rng.randint(1, 999)}."
        out.append(sent)
        n += len(sent) + 1
    return " ".join(out)


def test_speed_20_questions_on_50k_chars():
    doc = _big_doc()
    sentences = [s.strip() + "." for s in doc.split(".") if len(s.strip()) > 40][:20]
    raw = []
    for i, s in enumerate(sentences):
        quote = s if i % 2 else s.replace("e", "a", 3)  # half exact, half fuzzy
        raw.append({"prompt": f"Question {i}?", "choices": ["one", "two"], "answer_index": 0, "source_quote": quote})
    t0 = time.perf_counter()
    validate_questions(raw, doc)
    assert time.perf_counter() - t0 < 1.0


# ---------------------------------------------------------------- validation
def q(**kw):
    base = {"prompt": "How long are audit logs kept?", "choices": ["400 days", "28 days", "30 days"],
            "answer_index": 0, "source_quote": "Audit logs are kept for 400 days", "explanation": "See the policy."}
    base.update(kw)
    return base


def test_validate_accepts_valid():
    v = validate_questions([q(), q(prompt="Lag threshold before escalating to the DB team?",
                                  choices=["60 seconds", "120 seconds"], answer_index=1,
                                  source_quote="replication lag exceeds 120 seconds")], DOC)
    assert len(v.accepted) == 2 and v.rejected == [] and v.warnings == []
    first = v.accepted[0]
    assert first.anchor_score == 1.0 and first.quote_offset >= 0 and first.explanation == "See the policy."
    assert first.index == 0 and first.time_limit_s == 20


@pytest.mark.parametrize("bad", [
    "not a dict",
    {"choices": ["a", "b"], "answer_index": 0, "source_quote": "x" * 20},
    q(prompt="x" * 241),
    q(prompt="  "),
    q(choices=["only one"], answer_index=0),
    q(choices=["a", "b", "c", "d", "e"]),
    q(choices=["a", "x" * 91]),
    q(choices=["a", ""]),
    q(choices=["a", 3]),
    q(answer_index=3),
    q(answer_index=-1),
    q(answer_index=True),
    q(answer_index="0"),
    q(source_quote=None),
])
def test_bad_schema(bad):
    v = validate_questions([bad], DOC)
    assert v.rejected[0]["code"] == "bad_schema" and v.accepted == []
    assert set(v.rejected[0]) == {"index", "code", "detail", "best_score", "closest_passage"}


def test_duplicate_choice():
    v = validate_questions([q(choices=["400 days", "**400 Days**", "30 days"])], DOC)
    assert v.rejected[0]["code"] == "duplicate_choice"


def test_prompt_leaks_answer():
    v = validate_questions([q(prompt="Audit logs are kept 400 days: true for how long?")], DOC)
    assert v.rejected[0]["code"] == "prompt_leaks_answer"


def test_prompt_listing_all_choices_is_not_a_leak():
    v = validate_questions([q(prompt="Audit logs: 400 days, 28 days or 30 days?")], DOC)
    assert v.accepted and not v.rejected


def test_short_number_choice_not_a_false_leak():
    v = validate_questions([q(prompt="In section 3, how many days are audit logs kept?",
                              choices=["400", "3", "30"], answer_index=0)], DOC)
    assert v.accepted


def test_validation_anchor_codes_and_shape():
    v = validate_questions([
        q(source_quote="Audit logs are kept for 90 days; error budgets reset every 28 days."),
        q(source_quote="Audit trails are archived to cold storage yearly."),
        q(source_quote="400 days"),
    ], DOC)
    codes = [r["code"] for r in v.rejected]
    assert codes == ["number_mismatch", "quote_not_found", "quote_too_short"]
    nm = v.rejected[0]
    assert nm["best_score"] >= 0.9 and "400" in nm["closest_passage"] and "90" in nm["detail"]
    assert v.rejected[1]["best_score"] < 0.9 and v.rejected[2]["best_score"] is None
    assert [r["index"] for r in v.rejected] == [0, 1, 2]


def test_answer_not_in_quote_is_warning_only():
    v = validate_questions([q(prompt="Which cipher protects backups?", choices=["Blowfish", "Serpent"],
                              answer_index=0, source_quote="Les sauvegardes sont chiffrees avec AES-256")], DOC)
    assert len(v.accepted) == 1 and v.warnings[0]["code"] == "answer_not_in_quote"


def test_number_answer_checked_against_quote():
    v = validate_questions([q(choices=["365", "28"], answer_index=0)], DOC)
    assert v.accepted and v.warnings and v.warnings[0]["code"] == "answer_not_in_quote"
    v = validate_questions([q(choices=["400", "28"], answer_index=0)], DOC)
    assert v.warnings == []


def test_optional_fields():
    v = validate_questions([q(explanation="e" * 400, time_limit_s=30), q(time_limit_s=1), q(explanation=5)], DOC)
    assert len(v.accepted[0].explanation) == 300 and v.accepted[0].time_limit_s == 30
    assert v.accepted[1].time_limit_s == 20 and v.accepted[2].explanation == ""


def test_validate_non_list():
    v = validate_questions(None, DOC)  # type: ignore[arg-type]
    assert v.accepted == [] and v.rejected == []
