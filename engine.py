"""Pure game state machine (SPEC section 6). Synchronous, no I/O, no asyncio, injectable clock.

Choices made where the SPEC is silent (also in the PQ-A build log):
- Scores (``Player.score``, bot and Room scores) and streaks are applied in ``close_question``, not at
  submit time, so no public view can leak who answered correctly before the reveal. ``Answer.points``
  is computed at submit time.
- Bot timing: ``t = latency_ms / 1000`` (measured by the bot runner from question start), not the
  server clock at submission, because ``ask_bots`` gathers all bots and submits them together.
  A bot answer slower than ``T + 0.5 s`` or arriving after the question was closed gets
  ``meta["late"] = True`` and 0 points.
- An out-of-range bot choice is recorded as ``None`` with ``meta["error"] = "invalid_json"``
  (no exception, the conductor never has to catch it). Unknown bot: ``unknown_bot``.
- ``start()`` outside LOBBY raises ``wrong_state``; with no question ``no_questions``.
  ``game_started`` is never raised: late joiners are allowed (SPEC).
- ``end()`` while a question is open closes it first (Room answer recorded), then FINISHED. Idempotent.
- ``everyone_answered()`` is vacuously true when there is no connected human and no bot.
- Questions are copied and renumbered 0..n-1 (``Question.index`` = position in the game).
- Extra read-only helpers for the server: ``question_event``, ``reveal_event``, ``leaderboard_event``,
  ``lobby_event``, ``answered_event``, ``finished_event``, ``plan_bots`` (budget), ``bot_status``.
- Clients must compute the countdown as ``ends_at_ms - server_now_ms`` (the clock may be monotonic).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import statistics
import unicodedata
from dataclasses import replace
from typing import Callable

from .model import Answer, BotSpec, GameError, GameState, Mode, Player, Question

GRACE_S = 0.5
MAX_NICKNAME = 24
LOBBY_NAMES = 30
TOP_N = 10
ROOM_ID = "room"
ROOM_LABEL = "The Room"


def score_for(correct: bool, t: float, limit_s: float) -> int:
    """Kahoot-like score: ``round(1000 * (1 - 0.5 * min(t, T) / T))`` when correct, else 0."""
    if not correct:
        return 0
    limit = max(float(limit_s), 0.001)
    t = max(0.0, float(t))
    return round(1000 * (1 - 0.5 * min(t, limit) / limit))


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def clean_nickname(raw: str) -> str:
    """Strip, remove control/format characters, collapse whitespace. Raises ``bad_nickname``."""
    if not isinstance(raw, str):
        raise GameError("bad_nickname", "nickname must be a string")
    text = "".join(ch for ch in raw if not unicodedata.category(ch).startswith("C"))
    text = " ".join(text.split())
    if not 1 <= len(text) <= MAX_NICKNAME:
        raise GameError("bad_nickname", f"nickname must be 1 to {MAX_NICKNAME} characters")
    return text


class Game:
    def __init__(
        self,
        *,
        game_id: str,
        room_code: str,
        questions: list[Question],
        bots: list[BotSpec],
        clock: Callable[[], float],
        mode: Mode = Mode.OPEN_BOOK,
        title: str = "",
        doc_sha256: str = "",
        budget_usd: float = 1.0,
        max_players: int = 200,
        reveal_s: float = 6.0,
    ) -> None:
        self.game_id = game_id
        self.room_code = room_code
        self.questions: list[Question] = [replace(q, choices=list(q.choices), index=i) for i, q in enumerate(questions)]
        self.bots: list[BotSpec] = list(bots)
        self.clock = clock
        self.mode = Mode(mode)
        self.title = title
        self.doc_sha256 = doc_sha256
        self.budget_usd = float(budget_usd)
        self.max_players = int(max_players)
        self.reveal_s = float(reveal_s)

        self.state: GameState = GameState.LOBBY
        self.current: int = -1
        self.question_started_at: float | None = None
        self.question_deadline: float | None = None
        self.players: dict[str, Player] = {}
        self.answers: dict[int, list[Answer]] = {}
        self.spent_usd: float = 0.0
        self.created_at: float = clock()
        self.finished_at: float | None = None

        self.bot_scores: dict[str, int] = {b.id: 0 for b in self.bots}
        self.bot_correct: dict[str, int] = {b.id: 0 for b in self.bots}
        self.room_score: int = 0
        self.room_correct: int = 0
        self._closed: set[int] = set()
        self._bot_ids = {b.id for b in self.bots}

    # ------------------------------------------------------------------ helpers
    @property
    def total(self) -> int:
        return len(self.questions)

    @property
    def current_question(self) -> Question | None:
        if 0 <= self.current < len(self.questions):
            return self.questions[self.current]
        return None

    @property
    def closed_questions(self) -> set[int]:
        """Indexes of the questions already closed (revealed)."""
        return set(self._closed)

    def _answers_of(self, q: int) -> list[Answer]:
        return self.answers.setdefault(q, [])

    def _find(self, q: int, actor_id: str) -> Answer | None:
        for a in self.answers.get(q, []):
            if a.actor_id == actor_id:
                return a
        return None

    def bot(self, bot_id: str) -> BotSpec | None:
        for b in self.bots:
            if b.id == bot_id:
                return b
        return None

    # ------------------------------------------------------------------ players
    def add_player(self, nickname: str) -> tuple[Player, str]:
        if self.state == GameState.FINISHED:
            raise GameError("game_finished")
        name = clean_nickname(nickname)
        if len(self.players) >= self.max_players:
            raise GameError("game_full")
        name = self._unique_name(name)
        pid = secrets.token_hex(4)
        while pid in self.players or pid in self._bot_ids or pid == ROOM_ID:
            pid = secrets.token_hex(4)
        token = secrets.token_urlsafe(18)
        player = Player(id=pid, nickname=name, token_hash=hash_token(token), joined_at=self.clock())
        self.players[pid] = player
        return player, token

    def _unique_name(self, name: str) -> str:
        taken = {p.nickname.casefold() for p in self.players.values()}
        if name.casefold() not in taken:
            return name
        n = 2
        while True:
            suffix = f" {n}"
            candidate = name[: MAX_NICKNAME - len(suffix)].rstrip() + suffix
            if candidate.casefold() not in taken:
                return candidate
            n += 1

    def resume(self, token: str) -> Player:
        if not isinstance(token, str) or not token:
            raise GameError("bad_token")
        digest = hash_token(token)
        found: Player | None = None
        for p in self.players.values():
            if hmac.compare_digest(p.token_hash, digest):
                found = p
        if found is None:
            raise GameError("bad_token")
        found.connected = True
        return found

    def set_connected(self, player_id: str, connected: bool) -> None:
        player = self.players.get(player_id)
        if player is None:
            raise GameError("unknown_player")
        player.connected = bool(connected)

    # ------------------------------------------------------------------ transitions
    def start(self) -> dict:
        if self.state != GameState.LOBBY:
            raise GameError("wrong_state", f"cannot start from {self.state.value}")
        if not self.questions:
            raise GameError("no_questions")
        return self._begin(0)

    def _begin(self, i: int) -> dict:
        self.current = i
        self.state = GameState.QUESTION
        now = self.clock()
        self.question_started_at = now
        self.question_deadline = now + self.questions[i].time_limit_s
        self._answers_of(i)
        return self.question_event()

    def submit_answer(self, player_id: str, q: int, choice: int) -> Answer:
        player = self.players.get(player_id)
        if player is None:
            raise GameError("unknown_player")
        if self.state != GameState.QUESTION:
            raise GameError("wrong_state")
        if q != self.current:
            raise GameError("wrong_question")
        question = self.questions[q]
        if isinstance(choice, bool) or not isinstance(choice, int) or not 0 <= choice < len(question.choices):
            raise GameError("bad_choice")
        if self._find(q, player_id) is not None:
            raise GameError("already_answered")
        now = self.clock()
        assert self.question_deadline is not None and self.question_started_at is not None
        if now > self.question_deadline + GRACE_S:
            raise GameError("too_late")
        t = now - self.question_started_at
        correct = choice == question.answer_index
        answer = Answer(
            actor_id=player_id, actor_kind="human", q=q, choice=choice, received_at=now,
            latency_ms=int(round(t * 1000)), correct=correct, points=score_for(correct, t, question.time_limit_s),
        )
        self._answers_of(q).append(answer)
        return answer

    def submit_bot_answer(self, bot_id: str, q: int, choice: int | None, latency_ms: int, meta: dict) -> Answer:
        if bot_id not in self._bot_ids:
            raise GameError("unknown_bot")
        if self.state == GameState.LOBBY:
            raise GameError("wrong_state")
        if not isinstance(q, int) or not 0 <= q <= self.current or q >= len(self.questions):
            raise GameError("wrong_question")
        if self._find(q, bot_id) is not None:
            raise GameError("already_answered")
        question = self.questions[q]
        meta = dict(meta or {})
        if choice is not None and (isinstance(choice, bool) or not isinstance(choice, int)
                                   or not 0 <= choice < len(question.choices)):
            choice = None
            meta.setdefault("error", "invalid_json")
        latency_ms = max(0, int(latency_ms or 0))
        t = latency_ms / 1000.0
        late = q in self._closed or t > question.time_limit_s + GRACE_S
        if late:
            meta["late"] = True
        correct = choice is not None and choice == question.answer_index
        points = 0 if late else score_for(correct, t, question.time_limit_s)
        cost = meta.get("cost_usd") or 0.0
        try:
            self.spent_usd += max(0.0, float(cost))
        except (TypeError, ValueError):
            pass
        answer = Answer(
            actor_id=bot_id, actor_kind="bot", q=q, choice=choice, received_at=self.clock(),
            latency_ms=latency_ms, correct=correct, points=points, meta=meta,
        )
        self._answers_of(q).append(answer)
        return answer

    def everyone_answered(self) -> bool:
        if self.state != GameState.QUESTION:
            return False
        answered = {a.actor_id for a in self.answers.get(self.current, [])}
        humans = all(pid in answered for pid, p in self.players.items() if p.connected)
        bots = all(b.id in answered for b in self.bots)
        return humans and bots

    def close_question(self) -> dict:
        if self.state != GameState.QUESTION:
            raise GameError("wrong_state")
        q = self.current
        question = self.questions[q]
        room = self._room_answer(q, question)
        self._answers_of(q).append(room)
        self._closed.add(q)
        # Apply scores now (never before the reveal).
        for a in self.answers[q]:
            if a.actor_kind == "human":
                p = self.players.get(a.actor_id)
                if p is not None:
                    p.score += a.points
                    p.streak = p.streak + 1 if a.correct else 0
            elif a.actor_kind == "bot":
                self.bot_scores[a.actor_id] += a.points
                if a.correct and not a.meta.get("late"):
                    self.bot_correct[a.actor_id] += 1
        # Players who did not answer lose their streak.
        answered = {a.actor_id for a in self.answers[q]}
        for pid, p in self.players.items():
            if pid not in answered:
                p.streak = 0
        self.room_score += room.points
        self.room_correct += int(room.correct)
        self.state = GameState.REVEAL
        return self.reveal_event(q)

    def _room_answer(self, q: int, question: Question) -> Answer:
        humans = [a for a in self.answers.get(q, []) if a.actor_kind == "human" and a.choice is not None]
        now = self.clock()
        if not humans:
            return Answer(actor_id=ROOM_ID, actor_kind="room", q=q, choice=None, received_at=now,
                          latency_ms=0, correct=False, points=0, meta={"votes": 0})
        counts: dict[int, int] = {}
        first_at: dict[int, float] = {}
        for a in humans:
            c = int(a.choice or 0)
            counts[c] = counts.get(c, 0) + 1
            first_at[c] = min(first_at.get(c, a.received_at), a.received_at)
        top = max(counts.values())
        tied = [c for c, n in counts.items() if n == top]
        choice = min(tied, key=lambda c: first_at[c])
        latency_ms = int(statistics.median(a.latency_ms for a in humans if a.choice == choice))
        correct = choice == question.answer_index
        return Answer(
            actor_id=ROOM_ID, actor_kind="room", q=q, choice=choice, received_at=now, latency_ms=latency_ms,
            correct=correct, points=score_for(correct, latency_ms / 1000.0, question.time_limit_s),
            meta={"votes": len(humans), "majority": top},
        )

    def show_leaderboard(self) -> dict:
        if self.state != GameState.REVEAL:
            raise GameError("wrong_state")
        self.state = GameState.LEADERBOARD
        return self.leaderboard_event()

    def next_question(self) -> dict:
        if self.state not in (GameState.LEADERBOARD, GameState.REVEAL):
            raise GameError("wrong_state")
        if self.current + 1 >= len(self.questions):
            return self._finish()
        return self._begin(self.current + 1)

    def end(self) -> dict:
        if self.state == GameState.QUESTION:
            self.close_question()
        return self._finish()

    def _finish(self) -> dict:
        if self.state != GameState.FINISHED:
            self.state = GameState.FINISHED
            self.finished_at = self.clock()
            self.question_deadline = None
        return self.finished_event()

    # ------------------------------------------------------------------ budget
    def plan_bots(self, estimates: dict[str, float]) -> tuple[list[BotSpec], list[BotSpec]]:
        """Split bots into (to_call, skipped) so the estimated cost of this round fits the budget left.

        Drops the most expensive bot first until the remaining estimates fit. The conductor records
        each skipped bot with ``submit_bot_answer(bot_id, q, None, 0, {"error": "budget"})``.
        """
        left = self.budget_usd - self.spent_usd
        to_call = list(self.bots)
        skipped: list[BotSpec] = []
        while to_call and sum(max(0.0, estimates.get(b.id, 0.0)) for b in to_call) > left + 1e-12:
            worst = max(to_call, key=lambda b: estimates.get(b.id, 0.0))
            to_call.remove(worst)
            skipped.append(worst)
        return to_call, skipped

    # ------------------------------------------------------------------ events
    def question_event(self) -> dict:
        question = self.current_question
        if question is None or self.question_deadline is None:
            raise GameError("wrong_state")
        return {
            "t": "question",
            "q": self.current,
            "total": len(self.questions),
            "prompt": question.prompt,
            "choices": list(question.choices),
            "ends_at_ms": int(round(self.question_deadline * 1000)),
            "server_now_ms": int(round(self.clock() * 1000)),
        }

    def _bot_entry(self, bot: BotSpec, q: int) -> dict:
        a = self._find(q, bot.id)
        if a is None:
            return {"id": bot.id, "label": bot.label, "choice": None, "correct": False, "latency_ms": None,
                    "cost_usd": 0.0, "error": None, "pending": True, "late": False}
        return {
            "id": bot.id, "label": bot.label, "choice": a.choice, "correct": a.correct,
            "latency_ms": a.latency_ms, "cost_usd": float(a.meta.get("cost_usd") or 0.0),
            "error": a.meta.get("error"), "pending": False, "late": bool(a.meta.get("late")),
        }

    def reveal_event(self, q: int | None = None) -> dict:
        """Reveal for question ``q`` (default current). Refused while that question is still open."""
        q = self.current if q is None else q
        if q not in self._closed:
            raise GameError("wrong_state", "question not closed yet")
        question = self.questions[q]
        distribution = [0] * len(question.choices)
        for a in self.answers.get(q, []):
            if a.actor_kind == "human" and a.choice is not None:
                distribution[a.choice] += 1
        room = self._find(q, ROOM_ID)
        return {
            "t": "reveal",
            "q": q,
            "answer_index": question.answer_index,
            "explanation": question.explanation,
            "source_quote": question.source_quote,
            "distribution": distribution,
            "room": {"choice": room.choice if room else None, "correct": bool(room and room.correct)},
            "bots": [self._bot_entry(b, q) for b in self.bots],
        }

    def ranking(self) -> list[Player]:
        return sorted(self.players.values(), key=lambda p: (-p.score, p.joined_at))

    def rank_of(self, player_id: str) -> int | None:
        p = self.players.get(player_id)
        if p is None:
            return None
        return 1 + sum(1 for o in self.players.values() if o.score > p.score)

    def room_vs_models(self) -> list[dict]:
        rows = [{"id": ROOM_ID, "label": ROOM_LABEL, "score": self.room_score, "correct": self.room_correct}]
        for b in self.bots:
            rows.append({"id": b.id, "label": b.label, "score": self.bot_scores[b.id],
                         "correct": self.bot_correct[b.id]})
        return rows

    def leaderboard_event(self, player_id: str | None = None) -> dict:
        event = {
            "t": "leaderboard",
            "top": [{"nickname": p.nickname, "score": p.score} for p in self.ranking()[:TOP_N]],
            "room_vs_models": self.room_vs_models(),
            "you": None,
        }
        if player_id is not None and player_id in self.players:
            event["you"] = {"rank": self.rank_of(player_id), "score": self.players[player_id].score}
        return event

    def lobby_event(self) -> dict:
        names = [p.nickname for p in sorted(self.players.values(), key=lambda p: p.joined_at)]
        return {"t": "lobby", "count": len(self.players), "names": names[-LOBBY_NAMES:]}

    def answered_event(self) -> dict:
        q = self.current
        answers = self.answers.get(q, []) if self.state == GameState.QUESTION else []
        humans = sum(1 for a in answers if a.actor_kind == "human")
        bots_done = [a.actor_id for a in answers if a.actor_kind == "bot"]
        return {"t": "answered", "q": q, "count": humans + len(bots_done), "humans": humans, "bots_done": bots_done}

    def finished_event(self) -> dict:
        return {"t": "finished", "report_url": f"/report?room={self.room_code}"}

    # ------------------------------------------------------------------ views
    def public_view(self) -> dict:
        """Safe for the projector and the phones: no token, no answer before the reveal, no bot choice."""
        view: dict = {
            "t": "state",
            "game_id": self.game_id,
            "room": self.room_code,
            "title": self.title,
            "mode": self.mode.value,
            "state": self.state.value,
            "q": self.current,
            "total": len(self.questions),
            "players": len(self.players),
            "names": [p.nickname for p in sorted(self.players.values(), key=lambda p: p.joined_at)][-LOBBY_NAMES:],
            "bots": [{"id": b.id, "label": b.label} for b in self.bots],
            "question": None,
            "reveal": None,
            "leaderboard": None,
        }
        if self.state == GameState.QUESTION:
            view["question"] = self.question_event()
            view["answered"] = self.answered_event()
        elif self.state in (GameState.REVEAL, GameState.LEADERBOARD) or (
            self.state == GameState.FINISHED and self.current in self._closed
        ):
            question = self.current_question
            if question is not None:
                view["question"] = {"t": "question", "q": self.current, "total": len(self.questions),
                                    "prompt": question.prompt, "choices": list(question.choices)}
            if self.current in self._closed:
                view["reveal"] = self.reveal_event(self.current)
        if self.state in (GameState.LEADERBOARD, GameState.FINISHED, GameState.REVEAL):
            view["leaderboard"] = self.leaderboard_event()
        return view

    def bot_status(self) -> list[dict]:
        q = self.current
        rows = []
        for b in self.bots:
            a = self._find(q, b.id) if q >= 0 else None
            rows.append({"id": b.id, "label": b.label, "model": b.model, "provider": b.provider,
                         "answered": a is not None, "error": a.meta.get("error") if a else None,
                         "score": self.bot_scores[b.id]})
        return rows

    def host_view(self) -> dict:
        """Public view plus counts, spend and bot status. Still no token and no answer before reveal."""
        view = self.public_view()
        view["t"] = "host_state"
        answers = self.answers.get(self.current, []) if self.current >= 0 else []
        view.update({
            "connected": sum(1 for p in self.players.values() if p.connected),
            "answered_humans": sum(1 for a in answers if a.actor_kind == "human"),
            "spent_usd": round(self.spent_usd, 6),
            "budget_usd": self.budget_usd,
            "bots_status": self.bot_status(),
            "deadline_ms": int(round(self.question_deadline * 1000)) if self.question_deadline else None,
            "server_now_ms": int(round(self.clock() * 1000)),
        })
        return view
