"""Regression tests for the adversarial review findings (P1 to P4 server, A1 to A5 core).

Each test failed before its fix. Server tests use a real server on 127.0.0.1 port 0.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import socket
import sys
import time
from pathlib import Path

import aiohttp
import pytest
from popquiz_plugin import anchor, pricing
from popquiz_plugin import server as server_mod
from popquiz_plugin.model import Document, Mode, Question
from popquiz_plugin.server import QuizServer

sys.path.insert(0, str(Path(__file__).resolve().parent))  # reuse the helpers of the sibling test files
from test_conductor import BOTS as C_BOTS
from test_conductor import Price, make
from test_server import (
    BOTS,
    DOC,
    QUESTIONS,
    base,
    host_token,
    join,
    new_server,
    recv_until,
)

CLOSED = (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING)


@pytest.fixture
def srv():
    s = new_server()
    s.create_game(questions=QUESTIONS, bots=BOTS, doc=DOC, mode=Mode.OPEN_BOOK, budget_usd=1.0, reveal_s=0.2)
    yield s
    s.stop()


def _frame(payload: bytes) -> bytes:
    """One masked client text frame (RFC 6455)."""
    mask = os.urandom(4)
    body = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    n = len(payload)
    head = bytes([0x81, 0x80 | n]) if n < 126 else bytes([0x81, 0x80 | 126]) + n.to_bytes(2, "big")
    return head + mask + body


def _raw_ws(port: int) -> socket.socket:
    """A WebSocket client that completes the handshake and then never reads again (a phone gone to sleep)."""
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2048)
    s.settimeout(3)
    s.connect(("127.0.0.1", port))
    s.sendall((f"GET /ws HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    head = b""
    while b"\r\n\r\n" not in head:
        head += s.recv(1)
    assert b" 101 " in head
    return s


def _flood(sock: socket.socket, n: int = 6000) -> None:
    """Ask for n big snapshots and never read them. Stops when the server stops reading or drops us."""
    sock.settimeout(2.0)
    try:
        sock.sendall(_frame(b'{"t":"screen"}') * n)
    except OSError:
        pass


def _big_doc() -> Document:
    # A long title makes every `room` snapshot about 2 KB, so the flood fills the socket buffers fast.
    return Document(title="T" * 2000, text=DOC.text, path="runbook.md", sha256="0" * 64, kind="md")


# ---------------------------------------------------------------------------------------------- P4

# 50 KB prompts: aiohttp 3.14 only waits for the drain once 256 KB were written since its last wait, so
# big messages reach that point within this short game instead of a long real one.
BIG_QS = [Question(index=i, prompt=f"Q{i} " + "x" * 50_000, choices=["a", "b", "c"], answer_index=1,
                   source_quote="quote quote quote", time_limit_s=3) for i in range(4)]


async def _play_all(ws, n: int) -> list[str]:
    """Answer every question right; return the message types seen until the final leaderboard."""
    seen = []
    while True:
        m = json.loads((await ws.receive(timeout=4)).data)
        seen.append(m["t"])
        if m["t"] == "question":
            await ws.send_str(json.dumps({"t": "answer", "q": m["q"], "choice": 1}))
        if m["t"] == "leaderboard" and m.get("final"):
            return seen


async def test_p4_client_that_never_reads_does_not_stall_others(monkeypatch):
    monkeypatch.setattr(server_mod, "MSG_RATE_PER_S", 1e6)  # same mechanism, reached in a second
    monkeypatch.setattr(server_mod, "MSG_BURST", 1e6)
    monkeypatch.setattr(server_mod, "OUTBOX_MAX", 10**6, raising=False)  # never dropped: stays stalled
    s = new_server(llm_call=None)
    atk = None
    try:
        s.create_game(questions=BIG_QS, bots=[], doc=_big_doc(), mode=Mode.CLOSED_BOOK, budget_usd=0.0,
                      auto_advance=True, reveal_s=0.2)
        s._conductor.auto_advance_s = 0.1
        async with aiohttp.ClientSession() as session:
            honest = await join(session, s, "Honest")
            await recv_until(honest, "welcome")
            atk = _raw_ws(s.bound_port)
            _flood(atk)  # the server-side buffers of this socket are now full
            await asyncio.sleep(0.5)
            s.host_command("start")
            seen = await asyncio.wait_for(_play_all(honest, len(BIG_QS)), 20)
            assert seen.count("question") == 4 and seen.count("reveal") == 4
            await honest.close()
        t0 = time.monotonic()
        s.stop()  # closing the stalled socket must not block the shutdown either
        assert s._thread is None and time.monotonic() - t0 < 4
    finally:
        if atk is not None:
            atk.close()
        s.stop()


async def test_p4_slow_client_is_dropped_and_can_resume(monkeypatch):
    monkeypatch.setattr(server_mod, "MSG_RATE_PER_S", 1e6)
    monkeypatch.setattr(server_mod, "MSG_BURST", 1e6)
    s = new_server(llm_call=None)
    atk = None
    try:
        s.create_game(questions=QUESTIONS, bots=[], doc=_big_doc(), mode=Mode.CLOSED_BOOK, budget_usd=0.0)
        room = s.status()["room"]
        async with aiohttp.ClientSession() as session:
            ws = await join(session, s, "Sleepy")
            welcome = await recv_until(ws, "welcome")
            await ws.close()
            atk = _raw_ws(s.bound_port)
            atk.sendall(_frame(json.dumps({"t": "resume", "room": room, "token": welcome["token"]}).encode()))
            _flood(atk)
            await asyncio.sleep(0.5)
            st = s.status()
            assert st["players"] == 1 and st["connected"] == 0  # dropped, so marked offline
            again = await session.ws_connect(base(s) + "/ws")
            await again.send_str(json.dumps({"t": "resume", "room": room, "token": welcome["token"]}))
            assert (await recv_until(again, "welcome"))["player_id"] == welcome["player_id"]
            await again.close()
    finally:
        if atk is not None:
            atk.close()
        s.stop()


# ---------------------------------------------------------------------------------------------- P1

async def test_p1_screen_refused_from_foreign_origin(srv):
    room = srv.status()["room"]
    async with aiohttp.ClientSession() as session:
        ws = await session.ws_connect(base(srv) + "/ws", headers={"Origin": "https://evil.example"})
        await ws.send_str(json.dumps({"t": "screen", "room": ""}))
        log: list[str] = []
        assert (await recv_until(ws, "error", log=log))["code"] == "bad_origin"
        assert not any(room in raw for raw in log)
        await ws.close()
        for headers in ({}, {"Origin": base(srv)}, {"Origin": "null"}):  # no Origin: non-browser client
            ws = await session.ws_connect(base(srv) + "/ws", headers=headers)
            await ws.send_str(json.dumps({"t": "screen", "room": ""}))
            assert (await recv_until(ws, "room"))["room"] == room
            await ws.close()


# ---------------------------------------------------------------------------------------------- P2

async def test_p2_dns_rebinding_host_refused(srv):
    port = srv.bound_port
    evil = f"rebind.evil.example:{port}"
    async with aiohttp.ClientSession() as session:
        async with session.get(base(srv) + "/health", headers={"Host": evil}) as r:
            assert r.status == 421
            assert r.headers["X-Content-Type-Options"] == "nosniff"
        with pytest.raises(aiohttp.WSServerHandshakeError) as exc:
            await session.ws_connect(base(srv) + "/ws", headers={"Host": evil, "Origin": f"http://{evil}"})
        assert exc.value.status == 421
        for host in (f"127.0.0.1:{port}", f"localhost:{port}", "localhost"):
            async with session.get(base(srv) + "/health", headers={"Host": host}) as r:
                assert r.status == 200


async def test_p2_public_url_host_allowed_and_loopback_joins_rate_limited():
    s = new_server(public_url="https://quiz.example.org/")
    try:
        s.create_game(questions=QUESTIONS, bots=BOTS, doc=DOC, mode=Mode.OPEN_BOOK, budget_usd=1.0)
        async with aiohttp.ClientSession() as session:
            async with session.get(base(s) + "/health", headers={"Host": "quiz.example.org"}) as r:
                assert r.status == 200
            socks = []
            for i in range(server_mod.JOINS_PER_MIN):
                ws = await join(session, s, f"P{i}")
                await recv_until(ws, "welcome")
                socks.append(ws)
            ws = await join(session, s, "Tunnel flood")  # a tunnel delivers every player from loopback
            assert (await recv_until(ws, "error"))["code"] == "rate_limited"
            for w in socks + [ws]:
                await w.close()
        assert s.status()["players"] == server_mod.JOINS_PER_MIN
    finally:
        s.stop()


# ---------------------------------------------------------------------------------------------- P3

async def test_p3_socket_without_role_is_closed_after_deadline(srv, monkeypatch):
    monkeypatch.setattr(server_mod, "ROLE_TIMEOUT_S", 0.4, raising=False)
    async with aiohttp.ClientSession() as session:
        idle = await session.ws_connect(base(srv) + "/ws")
        await idle.send_str('{"t":"ping"}')  # traffic does not extend the deadline
        await recv_until(idle, "pong")
        msg = await idle.receive(timeout=3)
        assert msg.type in CLOSED
        screen = await session.ws_connect(base(srv) + "/ws")
        await screen.send_str(json.dumps({"t": "screen", "room": ""}))
        await recv_until(screen, "room")
        await asyncio.sleep(0.8)
        await screen.send_str('{"t":"ping"}')
        await recv_until(screen, "pong")  # a socket with a role is kept
        await screen.close()


async def test_p3_connection_cap_per_non_loopback_ip(srv, monkeypatch):
    monkeypatch.setattr(server_mod, "MAX_CONNS_PER_IP", 3, raising=False)
    monkeypatch.setattr(srv, "_peer_ip", lambda request: "192.168.1.50")
    async with aiohttp.ClientSession() as session:
        socks = [await session.ws_connect(base(srv) + "/ws") for _ in range(3)]
        with pytest.raises(aiohttp.WSServerHandshakeError) as exc:
            await session.ws_connect(base(srv) + "/ws")
        assert exc.value.status == 503
        for ws in socks:
            await ws.close()


async def test_p3_host_console_always_gets_a_slot(srv, monkeypatch):
    monkeypatch.setattr(server_mod, "MAX_CONNECTIONS", 4)
    monkeypatch.setattr(server_mod, "HOST_SLOTS", 1, raising=False)
    screen_msg = json.dumps({"t": "screen", "room": ""})
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=0)) as session:
        screens = []
        for _ in range(4):  # every regular slot holds a socket with a role
            ws = await session.ws_connect(base(srv) + "/ws")
            await ws.send_str(screen_msg)
            await recv_until(ws, "room")
            screens.append(ws)
        idle = await session.ws_connect(base(srv) + "/ws")  # admitted on the spare host slot
        await idle.send_str(screen_msg)
        assert (await recv_until(idle, "error"))["code"] == "server_full"
        host = await session.ws_connect(base(srv) + "/ws")  # evicts the idle socket
        await host.send_str(json.dumps({"t": "host_auth", "token": host_token(srv)}))
        assert (await recv_until(host, "host_state"))["state"] == "lobby"
        assert (await idle.receive(timeout=3)).type in CLOSED
        for ws in screens + [host]:
            await ws.close()


# ---------------------------------------------------------------------------------------------- A4, A5

async def test_a4_one_player_per_socket(srv):
    room = srv.status()["room"]
    async with aiohttp.ClientSession() as session:
        tokens = []
        for nick in ("Ann", "Bob"):
            ws = await join(session, srv, nick)
            tokens.append((await recv_until(ws, "welcome"))["token"])
            await ws.close()
        ws = await session.ws_connect(base(srv) + "/ws")
        await ws.send_str(json.dumps({"t": "resume", "room": room, "token": tokens[0]}))
        assert (await recv_until(ws, "welcome"))["nickname"] == "Ann"
        await ws.send_str(json.dumps({"t": "resume", "room": room, "token": tokens[1]}))
        assert (await recv_until(ws, "error"))["code"] == "already_joined"
        await ws.close()


def test_a5_host_command_from_server_thread_keeps_its_task():
    box: dict = {}

    def on_event(ev):
        if ev["t"] == "created":
            box["s"].host_command("start")
            box["held"] = [t for t in box["s"]._bg_tasks if not t.done()]

    s = QuizServer(port=0, llm_call=None, on_event=on_event)
    box["s"] = s
    s.start()
    try:
        s.create_game(questions=QUESTIONS, bots=[], doc=DOC, mode=Mode.CLOSED_BOOK, budget_usd=0.0)
        time.sleep(0.3)
        assert len(box["held"]) == 1
        assert s.status()["state"] != "lobby" and not s._bg_tasks
    finally:
        s.stop()


# ---------------------------------------------------------------------------------------------- A1

def _soup(n_chars: int, seed: int = 1) -> str:
    rng = random.Random(seed)
    vocab = ["".join(rng.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(rng.randint(3, 9))) for _ in range(3000)]
    return " ".join(rng.choice(vocab) for _ in range(n_chars // 5))[:n_chars]


def _shuffled_quote(doc: str, n_chars: int, seed: int = 2) -> str:
    words = doc[10_000:10_000 + n_chars].split()
    random.Random(seed).shuffle(words)
    return " ".join(words)


def test_a1_long_quote_rejected_fast_with_explicit_code():
    doc = _soup(100_000)
    item = {"prompt": "Q?", "choices": ["alpha one", "beta two"], "answer_index": 0,
            "source_quote": _shuffled_quote(doc, 3000)}
    t0 = time.perf_counter()
    v = anchor.validate_questions([item], doc)
    assert time.perf_counter() - t0 < 0.5
    assert v.rejected[0]["code"] == "quote_too_long" and str(anchor.MAX_QUOTE) in v.rejected[0]["detail"]
    exact = anchor.normalize(doc)[:anchor.MAX_QUOTE + 1]  # the cap applies even to a verbatim passage
    assert anchor.anchor(exact, anchor.normalize(doc)).code == "quote_too_long"
    assert anchor.anchor(exact[:anchor.MAX_QUOTE], anchor.normalize(doc)).ok


def test_a1_worst_case_at_the_cap_is_bounded():
    doc = _soup(50_000)  # SPEC size: 10 pages
    items = [{"prompt": "Q?", "choices": ["alpha one", "beta two"], "answer_index": 0,
              "source_quote": _shuffled_quote(doc, anchor.MAX_QUOTE, seed=s)[:anchor.MAX_QUOTE]} for s in range(20)]
    t0 = time.perf_counter()
    v = anchor.validate_questions(items, doc)
    assert time.perf_counter() - t0 < 10.0  # 20 adversarial (fuzzy path) quotes
    assert {r["code"] for r in v.rejected} == {"quote_not_found"}


# ---------------------------------------------------------------------------------------------- A2

class _Resp:
    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n=-1):
        return self.body


@pytest.mark.parametrize("aliases", [5, "gpt", {"a": 1}, [1, None, "alias/ok"], None])
def test_a2_load_pricing_never_raises_on_odd_catalog(aliases):
    entry = {"id": "m/x", "pricing": {"prompt": "0.000001", "completion": "0.000002"}, "aliases": aliases}
    body = json.dumps({"data": [entry]}).encode()
    p = pricing.load_pricing(opener=lambda req, timeout: _Resp(body))
    assert p.source == "catalog" and p.price_for("m/x") is not None
    assert not ({"g", "p", "t", "a"} & set(p.prices))  # a string alias is one name, never its letters
    if aliases == "gpt":
        assert p.price_for("gpt") is not None
    if isinstance(aliases, list):
        assert p.price_for("alias/ok") is not None


# ---------------------------------------------------------------------------------------------- A3

@pytest.mark.parametrize("grace", [0.3, 3.5])  # conductor cancels the call / ask_bot times out by itself
async def test_a3_timed_out_call_books_its_estimate_and_budget_holds(grace):
    calls = []

    async def slow_llm(spec, instructions, input_text, schema, timeout_s):
        calls.append(spec.id)  # the provider was called: the request is billed even if we stop waiting
        await asyncio.sleep(30)

    c, game, hub = make(slow_llm, pricing=Price({"anthropic/claude-opus-5.5": 0.4}), budget=0.5, bots=[C_BOTS[0]])
    c.bot_grace_s = grace
    c.start()
    await asyncio.wait_for(c.task, 20)
    assert calls == ["bot1"]
    assert game.spent_usd == pytest.approx(0.4) and game.spent_usd <= 0.5
    rows = [r["bots"][0] for r in hub.of("reveal")]
    assert [r["error"] for r in rows] == ["timeout", "budget", "budget"]
    assert rows[0]["cost_usd"] == pytest.approx(0.4) and rows[0]["cost_estimated"] is True
    assert rows[1]["cost_usd"] == 0.0 and rows[1]["cost_estimated"] is False
    assert c.report_data()["spent_usd"] == pytest.approx(0.4)


async def test_a3_call_in_flight_when_the_game_ends_is_booked():
    async def slow_llm(spec, instructions, input_text, schema, timeout_s):
        await asyncio.sleep(30)

    c, game, _ = make(slow_llm, pricing=Price({"anthropic/claude-opus-5.5": 0.4}), budget=1.0, bots=[C_BOTS[0]])
    c.start()
    await asyncio.sleep(0.2)
    await c.end()
    assert game.spent_usd == pytest.approx(0.4)
    assert game.answers[0][0].meta["cost_estimated"] is True


class _StatusError(Exception):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code


async def test_a3_refusal_before_generation_costs_nothing():
    """The Portal refused every call (no credit): $0 booked, nothing estimated, every question still tried."""
    calls = []

    async def no_credit(spec, instructions, input_text, schema, timeout_s):
        calls.append(spec.id)
        raise _StatusError(404, "Error code: 404 - {'code': 'insufficient_credits_for_paid_model'}")

    c, game, hub = make(no_credit, pricing=Price({"anthropic/claude-opus-5.5": 0.4}), budget=0.5, bots=[C_BOTS[0]])
    c.start()
    await asyncio.wait_for(c.task, 15)
    assert calls == ["bot1"] * 3 and game.spent_usd == 0.0
    rows = [r["bots"][0] for r in hub.of("reveal")]
    assert [r["error"] for r in rows] == ["refused"] * 3
    assert all(r["cost_usd"] == 0.0 and r["cost_estimated"] is False for r in rows)
    assert c.report_data()["spent_usd"] == 0.0


@pytest.mark.parametrize("exc", [ConnectionError("reset"), _StatusError(502, "bad gateway")])
async def test_a3_unknown_failure_after_launch_books_estimate_and_budget_holds(exc):
    calls = []

    async def failing(spec, instructions, input_text, schema, timeout_s):
        calls.append(spec.id)
        raise exc

    c, game, hub = make(failing, pricing=Price({"anthropic/claude-opus-5.5": 0.4}), budget=0.5, bots=[C_BOTS[0]])
    c.start()
    await asyncio.wait_for(c.task, 15)
    assert calls == ["bot1"] and game.spent_usd == pytest.approx(0.4) and game.spent_usd <= 0.5
    rows = [r["bots"][0] for r in hub.of("reveal")]
    assert [r["error"] for r in rows] == ["error", "budget", "budget"]
    assert rows[0]["cost_estimated"] is True
    data = c.report_data()
    assert data["spent_estimated"] is True and data["models"][0]["cost_estimated"] is True
    assert "Model spend: ~$0.4000 (est.)" in c.report.markdown


async def test_a3_real_cost_used_when_the_reply_reports_tokens():
    from popquiz_plugin.bots import LlmReply

    async def llm(spec, instructions, input_text, schema, timeout_s):
        q = next(q for q in c.questions if q.prompt in input_text)
        return LlmReply(parsed={"choice": "ABCD"[q.answer_index]}, text="", model=spec.model, input_tokens=10,
                        output_tokens=1)

    c, game, hub = make(llm, pricing=Price({"anthropic/claude-opus-5.5": 0.01}), budget=1.0, bots=[C_BOTS[0]])
    c.start()
    await asyncio.wait_for(c.task, 15)
    rows = [r["bots"][0] for r in hub.of("reveal")]
    assert all(r["error"] is None and r["cost_estimated"] is False for r in rows)
    assert game.spent_usd == pytest.approx(0.03)

