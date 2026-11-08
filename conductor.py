"""Async game loop (runs on the server loop): timers, model players, broadcasts.

Per question: engine transition, broadcast `question`, launch every model player at once (they run
while the humans answer), close at the deadline or as soon as everyone answered (or on `skip`),
wait a short grace for slow bots, broadcast `reveal` (bot results included even if late), after
`reveal_s` broadcast `leaderboard`, then wait for the host's `next` (or auto-advance after 6 s).
Bot answers stay server-side until the reveal: clients only learn which bots are done.

Budget: before each question the estimated cost of every call is checked against the budget left
(`game.plan_bots`). A launched call is billed even if we stop waiting, so when its real cost is unknown
(timeout, cancelled at the deadline or at `end`, network or other error, reply without token counts) it
is booked at its estimate with `meta["cost_estimated"] = True`; a reply with token counts is booked at
its real cost. A call refused before any generation (`trust_denied`, or a provider 4xx such as
`insufficient_credits_for_paid_model`: error "refused") costs nothing and is booked at $0.

The conductor talks to the transport through a small hub interface (implemented by `QuizServer`):
`await hub.broadcast(msg, personal=None)`, `hub.mark_dirty(kind)`, `hub._notify(event)`,
`hub.on_finished()`. Tests pass a fake hub.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from . import bots as bots_mod
from .model import GameState

log = logging.getLogger(__name__)

AUTO_ADVANCE_S = 6.0
BOT_GRACE_S = 1.5
EST_OUTPUT_TOKENS = 300


class Conductor:
    def __init__(self, *, game, questions, bots, doc_text: str, mode, llm_call, pricing, hub,
                 auto_advance: bool = False, reveal_s: float = 6.0, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time, room: str = "", budget_usd: float = 1.0, title: str = "",
                 auto_advance_s: float = AUTO_ADVANCE_S, bot_grace_s: float = BOT_GRACE_S):
        self.game = game
        self.questions = list(questions)
        self.bots = list(bots)
        self.doc_text = doc_text
        self.mode = mode
        self.llm_call = llm_call
        self.pricing = pricing
        self.hub = hub
        self.auto_advance = auto_advance
        self.reveal_s = reveal_s
        self.auto_advance_s = auto_advance_s
        self.bot_grace_s = bot_grace_s
        self.clock = clock
        self.wall = wall
        self.room = room
        self.budget_usd = budget_usd
        self.title = title

        self.task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._next = asyncio.Event()
        self._skip = False
        self._bot_tasks: dict[str, asyncio.Task] = {}
        self._stopped_bots: set[str] = set()
        self._last_cost: dict[str, float] = {}
        self._reserved: dict[str, float] = {}  # estimate of each call launched for the current question
        self.phase = "lobby"
        self.report = None
        self._report_data: dict | None = None

    # ------------------------------------------------------------------ commands (server loop)

    def start(self) -> None:
        if self.task is not None and not self.task.done():
            return
        if self.game.state != GameState.LOBBY:
            from .model import GameError
            raise GameError("wrong_state")
        self.task = asyncio.get_running_loop().create_task(self._run())

    def next(self) -> None:
        if self.game.state == GameState.LOBBY:
            self.start()
            return
        if self.game.state == GameState.QUESTION:
            self.skip()
            return
        self._next.set()

    def skip(self) -> None:
        self._skip = True
        self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    async def end(self) -> None:
        if self.game.state == GameState.FINISHED:
            return
        await self._cancel_task()
        await self._cancel_bots()
        if self.game.current >= 0:
            self._book_unanswered(self.game.current, 0)
        self.game.end()
        await self._finish()

    async def shutdown(self) -> None:
        await self._cancel_task()
        await self._cancel_bots()

    async def _cancel_task(self) -> None:
        t = self.task
        if t is not None and not t.done() and t is not asyncio.current_task():
            t.cancel()
            await asyncio.gather(t, return_exceptions=True)

    async def _cancel_bots(self) -> None:
        pending = [t for t in self._bot_tasks.values() if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    # ------------------------------------------------------------------ main loop

    async def _run(self) -> None:
        try:
            self.game.start()
            while True:
                await self._play_question()
                await self._sleep_or_next(self.reveal_s)
                lb = self.game.show_leaderboard()
                self.phase = "leaderboard"
                last = self.game.current + 1 >= self.game.total
                await self.hub.broadcast({**lb, "q": self.game.current, "total": self.game.total, "final": False,
                                          "last": last}, personal=self._lb_personal)
                self.hub._notify({"t": "leaderboard", "q": self.game.current})
                if self.auto_advance:
                    await self._sleep_or_next(self.auto_advance_s)
                else:
                    self._next.clear()
                    await self._next.wait()
                self._next.clear()
                self.game.next_question()
                if self.game.state == GameState.FINISHED:
                    break
            await self._finish()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("popquiz conductor crashed; ending the game")
            try:
                self.game.end()
                await self._finish()
            except Exception:
                log.exception("popquiz conductor could not finish cleanly")

    async def _sleep_or_next(self, seconds: float) -> None:
        self._next.clear()
        try:
            await asyncio.wait_for(self._next.wait(), max(0.0, seconds))
        except (asyncio.TimeoutError, TimeoutError):
            pass
        self._next.clear()

    async def _play_question(self) -> None:
        g = self.game
        qi = g.current
        question = g.questions[qi]
        self.phase = "question"
        self._skip = False
        await self.hub.broadcast(self.question_msg())
        self.hub._notify({"t": "question", "q": qi, "total": g.total})
        self._launch_bots(qi, question)
        self.hub.mark_dirty("answered")
        while not self._skip:
            remaining = (g.question_deadline or 0.0) - self.clock()
            if remaining <= 0 or g.everyone_answered():
                break
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), remaining)
            except (asyncio.TimeoutError, TimeoutError):
                pass
        self._skip = False
        g.close_question()
        pending = [t for t in self._bot_tasks.values() if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=self.bot_grace_s)
            await self._cancel_bots()
            self._book_unanswered(qi, int(question.time_limit_s * 1000) + 1000)
        self.phase = "reveal"
        reveal = self.reveal_msg(qi)
        await self.hub.broadcast(reveal, personal=lambda pid: self._reveal_personal(pid, qi))
        self.hub._notify({"t": "reveal", "q": qi, "spent_usd": round(g.spent_usd, 6)})

    async def _finish(self) -> None:
        self.phase = "finished"
        g = self.game
        lb = g.leaderboard_event()
        await self.hub.broadcast({**lb, "q": g.current, "total": g.total, "final": True, "last": True},
                                 personal=self._lb_personal)
        fin = g.finished_event()
        await self.hub.broadcast(fin)
        self._build_report()
        self.hub._notify({"t": "finished", "report_url": fin.get("report_url"), "spent_usd": round(g.spent_usd, 6)})
        try:
            self.hub.on_finished()
        except Exception:
            log.exception("popquiz on_finished failed")

    # ------------------------------------------------------------------ bots

    def estimate_bot_cost(self, spec) -> float:
        last = self._last_cost.get(spec.id)
        if last:
            return last
        p = self.pricing
        if p is None:
            return 0.0
        chars = len(self.doc_text) + 600 if str(getattr(self.mode, "value", self.mode)) == "open_book" else 600
        try:
            est = getattr(p, "estimate_question_cost", None)
            if est is not None:
                return float(est(spec.model, chars, EST_OUTPUT_TOKENS))
            reply = bots_mod.LlmReply(parsed=None, text="", model=spec.model, input_tokens=chars // 4,
                                      output_tokens=EST_OUTPUT_TOKENS)
            return float(p.cost(spec.model, reply))
        except Exception:
            return 0.0

    def estimated_total_usd(self) -> float:
        n = len(self.questions)
        return round(sum(self.estimate_bot_cost(b) for b in self.bots) * n, 4)

    def _launch_bots(self, qi: int, question) -> None:
        g = self.game
        self._bot_tasks = {}
        active = [b for b in self.bots if b.id not in self._stopped_bots]
        estimates = {b.id: self.estimate_bot_cost(b) for b in active}
        to_call, skipped = active, []
        planner = getattr(g, "plan_bots", None)
        if planner is not None:
            planned, _ = planner(estimates)
            ids = {b.id for b in planned}
            to_call = [b for b in active if b.id in ids]
            skipped = [b for b in active if b.id not in ids]
        self._stopped_bots.update(b.id for b in skipped)
        if self.llm_call is None:
            skipped, to_call = skipped + to_call, []
        for spec in self.bots:
            if spec.id in self._stopped_bots or spec in skipped:
                err = "budget" if spec.id in self._stopped_bots else "error"
                try:
                    g.submit_bot_answer(spec.id, qi, None, 0, {"error": err, "cost_usd": 0.0})
                except Exception:
                    pass
        timeout = max(1.0, (g.question_deadline or self.clock()) - self.clock())
        loop = asyncio.get_running_loop()
        self._reserved = {spec.id: estimates.get(spec.id, 0.0) for spec in to_call}
        for spec in to_call:
            self._bot_tasks[spec.id] = loop.create_task(self._run_bot(spec, qi, question, timeout))

    async def _run_bot(self, spec, qi: int, question, timeout: float) -> None:
        results = await bots_mod.ask_bots([spec], question, self.doc_text, self.mode, self.llm_call, self.pricing,
                                          timeout_s=timeout)
        r = results[0]
        meta = r.meta() if hasattr(r, "meta") else {"served_model": r.served_model, "cost_usd": r.cost_usd,
                                                     "error": r.error}
        if r.cost_usd:
            self._last_cost[spec.id] = float(r.cost_usd)
        if not (r.input_tokens or r.output_tokens) and r.error not in ("trust_denied", "refused"):
            meta = self._estimated(meta, spec.id)  # called but no usage known: book the estimate
        try:
            self.game.submit_bot_answer(spec.id, qi, r.choice, r.latency_ms, meta)
        except Exception as exc:
            log.debug("bot answer refused: %s", exc)
        self.hub.mark_dirty("answered")
        self._wake.set()

    def _estimated(self, meta: dict, bot_id: str) -> dict:
        est = self._reserved.get(bot_id, 0.0)
        return {**meta, "cost_usd": est, "cost_estimated": est > 0}

    def _book_unanswered(self, qi: int, latency_ms: int) -> None:
        """Calls launched for question qi that never answered (cancelled) are booked at their estimate."""
        for bot_id in self._bot_tasks:
            if not self._has_answer(qi, bot_id):
                try:
                    self.game.submit_bot_answer(bot_id, qi, None, latency_ms,
                                                self._estimated({"error": "timeout"}, bot_id))
                except Exception:
                    pass

    def _has_answer(self, qi: int, actor_id: str) -> bool:
        return any(a.actor_id == actor_id for a in self.game.answers.get(qi, []))

    def bots_status(self) -> list[dict]:
        rows = self.game.bot_status() if hasattr(self.game, "bot_status") else []
        for row in rows:
            row["stopped"] = row["id"] in self._stopped_bots
            row["estimate_usd"] = round(self.estimate_bot_cost(next(b for b in self.bots if b.id == row["id"])), 6)
        return rows

    # ------------------------------------------------------------------ messages

    def question_msg(self) -> dict:
        g = self.game
        ev = dict(g.question_event())
        q = g.questions[g.current]
        ev["time_limit_s"] = q.time_limit_s
        return ev

    def reveal_msg(self, qi: int) -> dict:
        g = self.game
        ev = dict(g.reveal_event(qi))
        ev["total"] = g.total
        ev["prompt"] = g.questions[qi].prompt
        ev["choices"] = list(g.questions[qi].choices)
        ev["answered"] = sum(ev.get("distribution") or [])
        ev["spent_usd"] = round(g.spent_usd, 6)
        return ev

    def answered_msg(self) -> dict | None:
        g = self.game
        if g.state != GameState.QUESTION:
            return None
        ev = dict(g.answered_event())
        ev["players"] = sum(1 for p in g.players.values() if p.connected)
        return ev

    def answered_count(self) -> int:
        msg = self.answered_msg()
        return int(msg["humans"]) if msg else 0

    def _player_answer(self, pid: str, qi: int):
        for a in self.game.answers.get(qi, []):
            if a.actor_id == pid:
                return a
        return None

    def _reveal_personal(self, pid: str, qi: int) -> dict | None:
        p = self.game.players.get(pid)
        if p is None:
            return None
        a = self._player_answer(pid, qi)
        return {"choice": a.choice if a else None, "correct": bool(a and a.correct), "points": a.points if a else 0,
                "score": p.score, "rank": self.game.rank_of(pid), "streak": p.streak}

    def _lb_personal(self, pid: str) -> dict | None:
        p = self.game.players.get(pid)
        if p is None:
            return None
        return {"rank": self.game.rank_of(pid), "score": p.score, "of": len(self.game.players)}

    def snapshot(self, pid: str | None) -> list[dict]:
        """Messages that bring a (re)connecting client up to date. Never contains hidden data."""
        g = self.game
        out: list[dict] = []
        state = g.state
        if state == GameState.QUESTION:
            out.append(self.question_msg())
            ans = self.answered_msg()
            if ans:
                out.append(ans)
            if pid:
                a = self._player_answer(pid, g.current)
                if a is not None:
                    out.append({"t": "locked", "q": g.current, "choice": a.choice})
        elif state in (GameState.REVEAL, GameState.LEADERBOARD) and g.current >= 0:
            reveal = self.reveal_msg(g.current)
            if pid:
                reveal["you"] = self._reveal_personal(pid, g.current)
            out.append(reveal)
            if state == GameState.LEADERBOARD:
                lb = {**g.leaderboard_event(), "q": g.current, "total": g.total, "final": False,
                      "last": g.current + 1 >= g.total}
                if pid:
                    lb["you"] = self._lb_personal(pid)
                out.append(lb)
        elif state == GameState.FINISHED:
            lb = {**g.leaderboard_event(), "q": g.current, "total": g.total, "final": True, "last": True}
            if pid:
                lb["you"] = self._lb_personal(pid)
            out.append(lb)
            out.append(g.finished_event())
        return out

    # ------------------------------------------------------------------ report

    def _build_report(self) -> None:
        self._report_data = self._compute_report_data()
        try:
            from . import report as report_mod
        except Exception:
            return
        builder = getattr(report_mod, "build_report", None)
        if builder is None:
            return
        try:
            self.report = builder(self.game, simulated=bool(getattr(self.llm_call, "simulated", False)))
        except TypeError:
            try:
                self.report = builder(game=self.game, questions=self.questions, bots=self.bots)
            except Exception:
                log.exception("popquiz report failed")
        except Exception:
            log.exception("popquiz report failed")

    def report_data(self) -> dict:
        if self._report_data is None:
            self._report_data = self._compute_report_data()
        return self._report_data

    def _compute_report_data(self) -> dict:
        """Public, post-game data for /report (no token, no player id)."""
        g = self.game
        questions = []
        for qi, q in enumerate(g.questions):
            answers = g.answers.get(qi, [])
            humans = [a for a in answers if a.actor_kind == "human"]
            dist = [0] * len(q.choices)
            for a in humans:
                if a.choice is not None and 0 <= a.choice < len(dist):
                    dist[a.choice] += 1
            n = len(humans)
            right = sum(1 for a in humans if a.correct)
            wrong = [(c, k) for c, k in enumerate(dist) if c != q.answer_index and k > 0]
            trap = max(wrong, key=lambda x: x[1]) if wrong else None
            room = next((a for a in answers if a.actor_kind == "room"), None)
            bots_rows = []
            for b in self.bots:
                a = next((x for x in answers if x.actor_id == b.id), None)
                bots_rows.append({"id": b.id, "label": b.label, "choice": a.choice if a else None,
                                  "correct": bool(a and a.correct), "latency_ms": a.latency_ms if a else None,
                                  "cost_usd": float(a.meta.get("cost_usd") or 0.0) if a else 0.0,
                                  "cost_estimated": bool(a and a.meta.get("cost_estimated")),
                                  "error": a.meta.get("error") if a else None})
            questions.append({
                "q": qi, "prompt": q.prompt, "choices": list(q.choices), "answer_index": q.answer_index,
                "source_quote": q.source_quote, "explanation": q.explanation, "played": qi in getattr(g, "_closed", set()),
                "distribution": dist, "answered": n, "correct_pct": round(100.0 * right / n, 1) if n else None,
                "trap": {"choice": trap[0], "count": trap[1]} if trap else None,
                "room": {"choice": room.choice if room else None, "correct": bool(room and room.correct)},
                "bots": bots_rows,
            })
        models = []
        for b in self.bots:
            rows = [a for qi in g.answers for a in g.answers[qi] if a.actor_id == b.id]
            lat = sorted(a.latency_ms for a in rows if a.choice is not None)
            models.append({"id": b.id, "label": b.label, "model": b.model, "score": g.bot_scores.get(b.id, 0),
                           "correct": g.bot_correct.get(b.id, 0),
                           "median_latency_ms": lat[len(lat) // 2] if lat else None,
                           "cost_usd": round(sum(float(a.meta.get("cost_usd") or 0.0) for a in rows), 6),
                           "cost_estimated": any(a.meta.get("cost_estimated") for a in rows),
                           "errors": sorted({a.meta.get("error") for a in rows if a.meta.get("error")})})
        played = sum(1 for x in questions if x["played"])
        return {
            "room": self.room, "title": self.title, "total": g.total, "played": played,
            "players": len(g.players), "spent_usd": round(g.spent_usd, 6),
            "spent_estimated": any(m["cost_estimated"] for m in models),
            "simulated": bool(getattr(self.llm_call, "simulated", False)),
            "top": [{"nickname": p.nickname, "score": p.score} for p in g.ranking()[:10]],
            "room_vs_models": g.room_vs_models(), "models": models, "questions": questions,
        }
