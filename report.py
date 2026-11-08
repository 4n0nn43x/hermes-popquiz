"""End of game report (SPEC sections 1 and 10): Markdown for the chat (< 4000 chars), CSV, data dict.

The Markdown says what the humans misunderstood and which model understood the document best, for
what cost; it is injected back into the conversation so the agent can propose remediation.

Choices made where the SPEC is silent:
- A question is "misunderstood" when fewer than half of the humans who answered got it right, or when
  The Room (majority) was wrong; at most 5 are listed, worst first.
- Best model: most correct answers, then highest score, then lowest cost; none when no model answered
  correctly. "Best value": the cheapest model with that same number of correct answers.
- Money: a cost booked at its estimate (call launched, real cost unknown) reads "~$x (est.)"; with
  ``simulated=True`` (cassette replay, no model call) the spend is labelled simulated.
- CSV cells starting with ``= + - @`` (or tab/CR) are prefixed with ``'`` (spreadsheet formula
  injection: nicknames and questions come from untrusted input).
- The Markdown is built compact first, then sections are dropped (players, then misunderstood details)
  and finally hard cut so it always fits ``max_chars`` (default 3900).
"""

from __future__ import annotations

import csv
import io
import statistics
from dataclasses import dataclass, field

from .engine import ROOM_ID, ROOM_LABEL, Game

LETTERS = "ABCD"


@dataclass
class Report:
    markdown: str
    csv: str
    data: dict = field(default_factory=dict)


def _short(text: str, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 3].rstrip() + "..."


def _cell(value) -> object:
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def _money(x: float, estimated: bool = False) -> str:
    text = f"${x:.4f}" if x < 1 else f"${x:.2f}"
    return f"~{text} (est.)" if estimated else text


def _contenders(game: Game) -> list[dict]:
    rows = []
    room_answers = [a for q in range(len(game.questions)) for a in game.answers.get(q, []) if a.actor_id == ROOM_ID]
    rows.append({
        "id": ROOM_ID, "label": ROOM_LABEL, "kind": "room", "model": "", "score": game.room_score,
        "correct": game.room_correct,
        "avg_latency_ms": int(statistics.mean(a.latency_ms for a in room_answers if a.choice is not None))
        if any(a.choice is not None for a in room_answers) else None,
        "cost_usd": 0.0, "cost_estimated": False, "errors": 0, "answered": 0,
    })
    for b in game.bots:
        answers = [a for q in range(len(game.questions)) for a in game.answers.get(q, []) if a.actor_id == b.id]
        answered = [a for a in answers if a.choice is not None]
        rows.append({
            "id": b.id, "label": b.label, "kind": "bot", "model": b.model, "score": game.bot_scores.get(b.id, 0),
            "correct": game.bot_correct.get(b.id, 0),
            "avg_latency_ms": int(statistics.mean(a.latency_ms for a in answered)) if answered else None,
            "cost_usd": round(sum(float(a.meta.get("cost_usd") or 0.0) for a in answers), 6),
            "cost_estimated": any(a.meta.get("cost_estimated") for a in answers),
            "answered": len(answered),
            "errors": sum(1 for a in answers if a.meta.get("error") not in (None, "fallback")),
            "fallbacks": sum(1 for a in answers if a.meta.get("error") == "fallback"),
        })
    return rows


def _questions(game: Game) -> list[dict]:
    out = []
    closed = game.closed_questions
    for i, question in enumerate(game.questions):
        answers = game.answers.get(i, [])
        humans = [a for a in answers if a.actor_kind == "human"]
        dist = [0] * len(question.choices)
        for a in humans:
            if a.choice is not None:
                dist[a.choice] += 1
        wrong = [(n, c) for c, n in enumerate(dist) if c != question.answer_index and n]
        top_wrong = max(wrong) if wrong else None
        room = next((a for a in answers if a.actor_id == ROOM_ID), None)
        out.append({
            "q": i, "prompt": question.prompt, "choices": list(question.choices),
            "answer_index": question.answer_index, "source_quote": question.source_quote,
            "explanation": question.explanation, "played": i in closed,
            "humans_answered": len(humans), "humans_correct": sum(1 for a in humans if a.correct),
            "pct_correct": round(100 * sum(1 for a in humans if a.correct) / len(humans)) if humans else None,
            "distribution": dist,
            "most_common_wrong": {"choice": top_wrong[1], "count": top_wrong[0]} if top_wrong else None,
            "room": {"choice": room.choice if room else None, "correct": bool(room and room.correct)},
            "bots": [{"id": a.actor_id, "choice": a.choice, "correct": a.correct, "latency_ms": a.latency_ms,
                      "cost_usd": float(a.meta.get("cost_usd") or 0.0), "error": a.meta.get("error"),
                      "late": bool(a.meta.get("late"))} for a in answers if a.actor_kind == "bot"],
        })
    return out


def build_data(game: Game, simulated: bool = False) -> dict:
    contenders = _contenders(game)
    questions = _questions(game)
    played = [q for q in questions if q["played"]]
    bots = [c for c in contenders if c["kind"] == "bot"]
    right = [c for c in bots if c["correct"]]
    best = max(right, key=lambda c: (c["correct"], c["score"], -c["cost_usd"])) if right else None
    value = min((c for c in bots if best and c["correct"] == best["correct"]), key=lambda c: c["cost_usd"],
                default=None)
    misunderstood = [q for q in played if q["humans_answered"] and (
        (q["pct_correct"] or 0) < 50 or not q["room"]["correct"])]
    misunderstood.sort(key=lambda q: (q["pct_correct"] if q["pct_correct"] is not None else 101, q["q"]))
    players = [{"nickname": p.nickname, "score": p.score,
                "correct": sum(1 for q in range(len(game.questions)) for a in game.answers.get(q, [])
                               if a.actor_id == p.id and a.correct)} for p in game.ranking()]
    return {
        "game_id": game.game_id, "room": game.room_code, "title": game.title, "mode": game.mode.value,
        "doc_sha256": game.doc_sha256, "state": game.state.value,
        "questions_total": len(game.questions), "questions_played": len(played),
        "players_count": len(game.players), "spent_usd": round(game.spent_usd, 6), "budget_usd": game.budget_usd,
        "spent_estimated": any(c["cost_estimated"] for c in bots), "simulated": bool(simulated),
        "contenders": contenders, "best_model": best["id"] if best else None,
        "best_value_model": value["id"] if value else None,
        "misunderstood": [q["q"] for q in misunderstood], "players": players, "questions": questions,
    }


def _latency(ms: int | None) -> str:
    return "-" if ms is None else f"{ms / 1000:.1f} s"


def _markdown(data: dict, *, players_n: int, mis_n: int, detail: bool) -> str:
    played = data["questions_played"]
    lines = [f"# Pop Quiz report: {_short(data['title'] or 'Untitled document', 80)}", ""]
    mode = "open book" if data["mode"] == "open_book" else "closed book"
    sim = data.get("simulated")
    spend = _money(data["spent_usd"], data.get("spent_estimated", False))
    spend += " simulated (recorded cassette, no model call)" if sim else ""
    lines.append(f"Room {data['room']}, {played}/{data['questions_total']} questions played, "
                 f"{data['players_count']} players, {mode}. Model spend: {spend} "
                 f"(budget {_money(data['budget_usd'])}).")
    lines += ["", "## The Room vs the models", "",
              f"| Player | Correct | Score | Avg latency | {'Simulated cost' if sim else 'Cost'} |",
              "|---|---|---|---|---|"]
    for c in data["contenders"]:
        extra = ""
        if c.get("errors"):
            extra = f" ({c['errors']} no answer)"
        elif c.get("fallbacks"):
            extra = f" ({c['fallbacks']} fallback)"
        cost = "-" if c["kind"] == "room" else _money(c["cost_usd"], c.get("cost_estimated", False))
        lines.append(f"| {_short(c['label'], 30)}{extra} | {c['correct']}/{played} | {c['score']} | "
                     f"{_latency(c['avg_latency_ms'])} | {cost} |")
    by_id = {c["id"]: c for c in data["contenders"]}
    if data["best_model"]:
        best = by_id[data["best_model"]]
        lines += ["", f"Best model on this document: **{best['label']}** ({best['correct']}/{played} correct, "
                      f"{_money(best['cost_usd'], best.get('cost_estimated', False))})."]
        value = by_id.get(data["best_value_model"] or "")
        if value and value["id"] != best["id"] and value["cost_usd"] < best["cost_usd"]:
            lines.append(f"Same accuracy for less: **{value['label']}** at "
                         f"{_money(value['cost_usd'], value.get('cost_estimated', False))}.")
        room = by_id[ROOM_ID]
        verdict = ("beat" if room["score"] > best["score"]
                   else "tied with" if room["score"] == best["score"] else "lost to")
        lines.append(f"The Room {verdict} the best model on points ({room['score']} vs {best['score']}).")
    elif any(c["kind"] == "bot" for c in data["contenders"]):
        if any(c.get("answered") for c in data["contenders"] if c["kind"] == "bot"):
            lines += ["", "No model answered correctly, so there is no best model on this document."]
        else:
            codes = sorted({b["error"] for q in data["questions"] for b in q["bots"] if b.get("error")})
            lines += ["", "No model answered" + (f" (errors: {', '.join(codes)})" if codes else "")
                      + ", so there is no best model on this document."]
    questions = {q["q"]: q for q in data["questions"]}
    lines += ["", "## What the room misunderstood", ""]
    if not data["misunderstood"]:
        lines.append("Nothing major: the majority of the room got every played question right.")
    for n, qi in enumerate(data["misunderstood"][:mis_n], 1):
        q = questions[qi]
        correct = q["choices"][q["answer_index"]]
        line = (f"{n}. Q{qi + 1} \"{_short(q['prompt'], 90)}\": {q['pct_correct']}% correct. "
                f"Answer: \"{_short(correct, 50)}\".")
        if q["most_common_wrong"]:
            wrong = q["choices"][q["most_common_wrong"]["choice"]]
            line += f" Most common wrong answer: \"{_short(wrong, 50)}\" ({q['most_common_wrong']['count']})."
        lines.append(line)
        if detail:
            lines.append(f"   Source: \"{_short(q['source_quote'], 140)}\"")
            missed = [by_id[b["id"]]["label"] for b in q["bots"] if not b["correct"] and b["id"] in by_id]
            if missed:
                lines.append(f"   Models wrong too: {', '.join(missed)} (the passage may be ambiguous).")
    if len(data["misunderstood"]) > mis_n:
        lines.append(f"... and {len(data['misunderstood']) - mis_n} more (see CSV).")
    if players_n and data["players"]:
        lines += ["", "## Top players", ""]
        for i, p in enumerate(data["players"][:players_n], 1):
            lines.append(f"{i}. {_short(p['nickname'], 24)}: {p['score']} points, {p['correct']} correct")
    lines += ["", "## Suggested next step", "",
              "Explain the misunderstood points above with the source passages, propose a short fix to the "
              "document where even the models were wrong, and offer a follow-up quiz on those topics."]
    return "\n".join(lines) + "\n"


def to_markdown(data: dict, max_chars: int = 3900) -> str:
    for players_n, mis_n, detail in ((5, 5, True), (3, 5, True), (3, 5, False), (0, 3, False), (0, 1, False)):
        text = _markdown(data, players_n=players_n, mis_n=mis_n, detail=detail)
        if len(text) <= max_chars:
            return text
    return text[: max_chars - 16].rstrip() + "\n...(truncated)\n"


def to_csv(game: Game) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["q", "prompt", "actor_kind", "actor_id", "name", "choice", "choice_text", "correct",
                     "points", "latency_ms", "cost_usd", "error", "late"])
    names = {p.id: p.nickname for p in game.players.values()}
    names.update({b.id: b.label for b in game.bots})
    names[ROOM_ID] = ROOM_LABEL
    for i, question in enumerate(game.questions):
        for a in game.answers.get(i, []):
            letter = LETTERS[a.choice] if a.choice is not None else ""
            text = question.choices[a.choice] if a.choice is not None else ""
            writer.writerow([_cell(v) for v in (
                i + 1, question.prompt, a.actor_kind, a.actor_id, names.get(a.actor_id, a.actor_id), letter, text,
                int(a.correct), a.points, a.latency_ms, f"{float(a.meta.get('cost_usd') or 0.0):.6f}",
                a.meta.get("error") or "", int(bool(a.meta.get("late"))))])
    return buf.getvalue()


def build_report(game: Game, max_chars: int = 3900, simulated: bool = False) -> Report:
    data = build_data(game, simulated=simulated)
    return Report(markdown=to_markdown(data, max_chars=max_chars), csv=to_csv(game), data=data)
