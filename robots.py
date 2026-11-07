"""Simulated phone players over WebSocket, for the demo and the load bench.

`run_robots` connects n players exactly like phones do (join, answer, receive every broadcast).
The answers are hidden until the reveal, so to aim at an accuracy band the caller passes
`answer_key` (the demo knows its own questions); without it the robots pick uniformly at random.
Each robot draws a skill in `accuracy` and a think time in `think` (seconds, clipped to the time left).
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass, field

NAMES = [
    "Ada", "Grace", "Linus", "Alan", "Hedy", "Katherine", "Margaret", "Dennis", "Barbara", "Ken", "Radia",
    "Edsger", "Frances", "Guido", "Joan", "Tim", "Sophie", "Donald", "Annie", "Claude", "Mary", "John", "Lovelace",
    "Shafi", "Yukihiro", "Anita", "Bjarne", "Jean", "Niklaus", "Leslie", "Evelyn", "Vint", "Karen", "Seymour",
]


@dataclass
class RobotStats:
    joined: int = 0
    answers: int = 0
    correct_sent: int = 0
    errors: dict = field(default_factory=dict)
    finished: int = 0

    def as_dict(self) -> dict:
        return {"joined": self.joined, "answers": self.answers, "correct_sent": self.correct_sent,
                "errors": dict(self.errors), "finished": self.finished}


async def _robot(session, ws_url: str, origin: str, room: str, nickname: str, skill: float, think: tuple[float, float],
                 rng: random.Random, answer_key: list[int] | None, stats: RobotStats, stop: asyncio.Event,
                 on_welcome=None) -> None:
    async with session.ws_connect(ws_url, headers={"Origin": origin}, heartbeat=30) as ws:
        await ws.send_str(json.dumps({"t": "join", "room": room, "nickname": nickname}))
        pending: asyncio.Task | None = None

        async def answer_later(q: int, n: int, left_s: float) -> None:
            lo, hi = think
            delay = rng.uniform(lo, hi) if hi > lo else lo
            delay = max(0.0, min(delay, max(0.0, left_s - 0.4)))
            if delay:
                await asyncio.sleep(delay)
            if answer_key is not None and q < len(answer_key):
                right = answer_key[q]
                if rng.random() < skill:
                    choice = right
                else:
                    others = [i for i in range(n) if i != right] or [right]
                    choice = rng.choice(others)
            else:
                choice = rng.randrange(n)
            await ws.send_str(json.dumps({"t": "answer", "q": q, "choice": choice}))
            stats.answers += 1
            if answer_key is not None and q < len(answer_key) and choice == answer_key[q]:
                stats.correct_sent += 1

        stop_wait = asyncio.ensure_future(stop.wait())
        try:
            while True:
                recv = asyncio.ensure_future(ws.receive())
                done, _ = await asyncio.wait({recv, stop_wait}, return_when=asyncio.FIRST_COMPLETED)
                if stop_wait in done:
                    recv.cancel()
                    break
                msg = recv.result()
                if msg.type.name != "TEXT":
                    break
                data = json.loads(msg.data)
                t = data.get("t")
                if t == "welcome":
                    stats.joined += 1
                    if on_welcome is not None:
                        on_welcome()
                elif t == "question":
                    left = (data.get("ends_at_ms", 0) - data.get("server_now_ms", 0)) / 1000.0
                    if pending is not None and not pending.done():
                        pending.cancel()
                    pending = asyncio.ensure_future(answer_later(int(data["q"]), len(data.get("choices") or [1]), left))
                elif t == "finished":
                    stats.finished += 1
                    break
                elif t == "error":
                    code = str(data.get("code"))
                    stats.errors[code] = stats.errors.get(code, 0) + 1
                    if code in ("bad_room", "game_full", "game_finished", "bad_nickname", "game_over"):
                        break
        finally:
            stop_wait.cancel()
            if pending is not None and not pending.done():
                pending.cancel()


async def run_robots(base_url: str, room: str, n: int, accuracy=(0.4, 0.9), think=(1.0, 8.0), seed=None, *,
                     answer_key: list[int] | None = None, stop: asyncio.Event | None = None,
                     joined: asyncio.Event | None = None, connect_spread_s: float = 0.0) -> dict:
    """Run n robot players until the game finishes (or `stop` is set). Returns aggregate stats.

    `joined` (optional) is set once every robot got its `welcome`.
    """
    from aiohttp import ClientSession, ClientTimeout, TCPConnector

    rng = random.Random(seed)
    base = base_url.rstrip("/")
    ws_url = base.replace("http://", "ws://", 1).replace("https://", "wss://", 1) + "/ws"
    stop = stop or asyncio.Event()
    stats = RobotStats()
    lo, hi = accuracy
    names = list(NAMES)
    rng.shuffle(names)
    started = time.perf_counter()
    count_ready = 0

    def on_welcome() -> None:
        nonlocal count_ready
        count_ready += 1
        if count_ready >= n and joined is not None:
            joined.set()

    async def one(i: int, session) -> None:
        nick = names[i % len(names)] + (f" {i // len(names) + 1}" if i >= len(names) else "")
        skill = rng.uniform(lo, hi)
        r = random.Random(rng.random())
        if connect_spread_s:
            await asyncio.sleep(r.uniform(0, connect_spread_s))
        try:
            await _robot(session, ws_url, base, room, nick, skill, think, r, answer_key, stats, stop, on_welcome)
        except Exception as exc:  # network errors are counted, never raised
            key = type(exc).__name__
            stats.errors[key] = stats.errors.get(key, 0) + 1

    # limit=0: every robot keeps one WebSocket open (the default pool caps at 100 connections).
    async with ClientSession(connector=TCPConnector(limit=0), timeout=ClientTimeout(total=None)) as session:
        await asyncio.gather(*(one(i, session) for i in range(n)))
    out = stats.as_dict()
    out["n"] = n
    out["elapsed_s"] = round(time.perf_counter() - started, 2)
    return out
