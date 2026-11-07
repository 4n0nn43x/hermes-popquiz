"""Server tests: real aiohttp server in its own thread on 127.0.0.1, real WebSocket clients."""

from __future__ import annotations

import asyncio
import json
import re
import socket
import sys
import threading
import time
from pathlib import Path

import aiohttp
import pytest

from popquiz_plugin import server as server_mod
from popquiz_plugin.bots import LlmReply
from popquiz_plugin.model import BotSpec, Document, GameError, Mode, Question
from popquiz_plugin.server import CSP, QuizServer

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"

QUESTIONS = [
    Question(index=0, prompt="How long are audit logs kept?", choices=["30 days", "35 days", "400 days", "2 years"],
             answer_index=2, source_quote="Audit logs are kept for 400 days.", explanation="Audit logs: 400 days.",
             time_limit_s=3),
    Question(index=1, prompt="Who approves a multi-service rollback?", choices=["Nobody", "The incident commander"],
             answer_index=1, source_quote="needs the commander's explicit approval", time_limit_s=3),
    Question(index=2, prompt="Within how many minutes must a page be acknowledged?",
             choices=["5 minutes", "10 minutes", "15 minutes"], answer_index=0,
             source_quote="acknowledge a page within 5 minutes", time_limit_s=3),
]
BOTS = [BotSpec("bot1", "Claude Opus 5.5", "nous", "anthropic/claude-opus-5.5"),
        BotSpec("bot2", "DeepSeek V4 Flash", "nous", "deepseek/deepseek-v4-flash"),
        BotSpec("bot3", "Mercury 3", "nous", "inception/mercury-3")]
DOC = Document(title="Runbook <b>", text="Audit logs are kept for 400 days.", path="runbook.md", sha256="0" * 64, kind="md")


def make_llm(delays=None, wrong=(), served=None):
    delays = delays or {"bot1": 0.15, "bot2": 0.08, "bot3": 0.02}

    async def llm(spec, instructions, input_text, schema, timeout_s):
        await asyncio.sleep(delays.get(spec.id, 0.02))
        q = next(q for q in QUESTIONS if q.prompt in input_text)
        idx = q.answer_index if spec.id not in wrong else (q.answer_index + 1) % len(q.choices)
        letter = "ABCD"[idx]
        return LlmReply(parsed={"choice": letter}, text=json.dumps({"choice": letter}),
                        model=(served or {}).get(spec.id, spec.model), input_tokens=500, output_tokens=5)

    return llm


def new_server(**kw) -> QuizServer:
    kw.setdefault("llm_call", make_llm())
    srv = QuizServer(port=0, **kw)
    srv.start()
    return srv


@pytest.fixture
def srv():
    s = new_server()
    s.create_game(questions=QUESTIONS, bots=BOTS, doc=DOC, mode=Mode.OPEN_BOOK, budget_usd=1.0, reveal_s=0.2)
    yield s
    s.stop()


def base(s: QuizServer) -> str:
    return f"http://127.0.0.1:{s.bound_port}"


def host_token(s: QuizServer) -> str:
    return s.urls()["host"].split("#", 1)[1]


async def recv_until(ws, t: str, timeout: float = 5.0, log: list | None = None) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError(t)
        msg = await ws.receive(timeout=left)
        if msg.type != aiohttp.WSMsgType.TEXT:
            raise ConnectionError(f"closed while waiting for {t}: {msg.type} {msg.data}")
        if log is not None:
            log.append(msg.data)
        data = json.loads(msg.data)
        if data.get("t") == t:
            return data


async def join(session, s, nickname="Ann", origin=None):
    headers = {"Origin": origin} if origin else {}
    ws = await session.ws_connect(base(s) + "/ws", headers=headers)
    await ws.send_str(json.dumps({"t": "join", "room": s.status()["room"], "nickname": nickname}))
    return ws


# ---------------------------------------------------------------------------------------------- lifecycle

def test_start_stop_leaves_no_thread_and_closes_port():
    before = {t.name for t in threading.enumerate()}
    s = new_server()
    port = s.bound_port
    assert any(t.name == "popquiz-server" for t in threading.enumerate())
    with socket.create_connection(("127.0.0.1", port), timeout=2):
        pass
    s.stop()
    assert s._thread is None
    assert not any(t.name == "popquiz-server" for t in threading.enumerate() if t.name not in before)
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
    assert s.status() == {"running": False}


def test_port_in_use_raises_game_error():
    s = new_server()
    try:
        other = QuizServer(port=s.bound_port)
        with pytest.raises(GameError) as exc:
            other.start()
        assert exc.value.code == "port_in_use"
        assert other._thread is None
    finally:
        s.stop()


def test_binds_loopback_only(srv):
    assert srv.host == "127.0.0.1"
    addrs = srv._runner.addresses
    assert addrs and all(a[0] == "127.0.0.1" for a in addrs)


def test_stop_is_idempotent_and_restartable():
    s = new_server()
    s.stop()
    s.stop()
    s.start()
    assert s.running
    s.stop()
    assert not s.running


def test_status_thread_safe_from_many_threads(srv):
    out = []
    threads = [threading.Thread(target=lambda: out.append(srv.status()["state"])) for _ in range(16)]
    [t.start() for t in threads]
    [t.join(5) for t in threads]
    assert out == ["lobby"] * 16


def test_create_game_returns_host_info(srv):
    info = srv.create_game(questions=QUESTIONS, bots=BOTS, doc=DOC, mode=Mode.OPEN_BOOK, budget_usd=0.5)
    assert re.fullmatch(r"[A-Z2-9]{6}", info["room"])
    assert info["host_token"] and info["urls"]["host"].endswith("#" + info["host_token"])
    assert info["players"] == [b.label for b in BOTS]
    assert "token" not in info["urls"]["screen"] and info["host_token"] not in info["urls"]["join"]


async def test_idle_shutdown_after_finish():
    s = new_server(idle_shutdown_s=0.3)
    s.create_game(questions=QUESTIONS, bots=BOTS, doc=DOC, mode=Mode.OPEN_BOOK, budget_usd=1.0)
    s.host_command("end")
    for _ in range(50):
        if s._thread is None or not s._thread.is_alive():
            break
        await asyncio.sleep(0.1)
    assert not s._thread.is_alive()
    s.stop()


# ---------------------------------------------------------------------------------------------- HTTP

ROUTES = ["/", "/play", "/screen", "/report", "/health", "/static/style.css", "/static/common.js", "/static/screen.js",
          "/static/play.js", "/static/host.js", "/static/join.js", "/static/report.js", "/static/icon.svg", "/nope"]


async def test_security_headers_on_every_response(srv):
    room = srv.status()["room"]
    async with aiohttp.ClientSession() as session:
        for path in ROUTES + [f"/qr.svg?room={room}", f"/api/report?room={room}", "/host"]:
            async with session.get(base(srv) + path) as r:
                assert r.headers["Content-Security-Policy"] == CSP, path
                assert r.headers["X-Content-Type-Options"] == "nosniff", path
                assert r.headers["Referrer-Policy"] == "no-referrer", path
        async with session.post(base(srv) + "/api/host/start") as r:
            assert r.status == 403
            assert r.headers["Content-Security-Policy"] == CSP


async def test_health(srv):
    async with aiohttp.ClientSession() as session:
        async with session.get(base(srv) + "/health") as r:
            assert r.status == 200 and await r.json() == {"ok": True}


async def test_no_public_route_leaks_tokens(srv):
    async with aiohttp.ClientSession() as session:
        ws = await join(session, srv, "Ann")
        welcome = await recv_until(ws, "welcome")
        player_token = welcome["token"]
        room = srv.status()["room"]
        secrets_ = [host_token(srv), player_token]
        paths = ROUTES + ["/host", f"/qr.svg?room={room}", f"/api/report?room={room}", f"/screen?room={room}",
                          f"/report?room={room}", f"/?room={room}"]
        paths += ["/static/" + p.name for p in STATIC.iterdir()]
        for path in paths:
            async with session.get(base(srv) + path) as r:
                body = await r.read()
                for sec in secrets_:
                    assert sec.encode() not in body, path
                for v in r.headers.values():
                    assert all(sec not in v for sec in secrets_), path
        await ws.close()


async def test_static_traversal_refused(srv):
    async with aiohttp.ClientSession() as session:
        for path in ["/static/../server.py", "/static/%2e%2e/server.py", "/static/..%2fserver.py"]:
            async with session.get(base(srv) + path) as r:
                assert r.status in (403, 404), path
                assert b"QuizServer" not in await r.read()


async def test_qr_svg_and_wrong_room(srv):
    room = srv.status()["room"]
    async with aiohttp.ClientSession() as session:
        async with session.get(base(srv) + f"/qr.svg?room={room}") as r:
            assert r.status == 200 and r.content_type == "image/svg+xml"
            assert b"<svg" in await r.read()
        async with session.get(base(srv) + "/qr.svg?room=ZZZZZZ") as r:
            assert r.status == 404


async def test_qr_404_without_segno(srv, monkeypatch):
    monkeypatch.setitem(sys.modules, "segno", None)
    room = srv.status()["room"]
    async with aiohttp.ClientSession() as session:
        async with session.get(base(srv) + f"/qr.svg?room={room}") as r:
            assert r.status == 404
        ws = await session.ws_connect(base(srv) + "/ws")
        await ws.send_str(json.dumps({"t": "screen", "room": room}))
        msg = await recv_until(ws, "room")
        assert msg["qr"] is False and room in msg["join_url"]
        await ws.close()


async def test_report_api_only_after_finish(srv):
    room = srv.status()["room"]
    async with aiohttp.ClientSession() as session:
        async with session.get(base(srv) + f"/api/report?room={room}") as r:
            assert r.status == 409
        srv.host_command("end")
        async with session.get(base(srv) + f"/api/report?room={room}") as r:
            data = await r.json()
            assert r.status == 200 and data["ready"] and data["room"] == room
        async with session.get(base(srv) + "/api/report?room=ZZZZZZ") as r:
            assert r.status == 404


# ---------------------------------------------------------------------------------------------- host auth

async def test_host_api_refused_without_token(srv):
    async with aiohttp.ClientSession() as session:
        for cmd in ("start", "next", "skip", "end", "open_lan"):
            async with session.post(base(srv) + f"/api/host/{cmd}") as r:
                assert r.status == 403, cmd
    assert srv.status()["state"] == "lobby"


async def test_host_api_refused_with_wrong_token(srv):
    async with aiohttp.ClientSession() as session:
        async with session.post(base(srv) + "/api/host/start", headers={"Authorization": "Bearer nope"}) as r:
            assert r.status == 403
        async with session.post(base(srv) + "/api/host/start", headers={"Authorization": host_token(srv)}) as r:
            assert r.status == 403  # scheme required
    assert srv.status()["state"] == "lobby"


async def test_host_api_refused_from_non_loopback_peer(srv, monkeypatch):
    monkeypatch.setattr(srv, "_peer_ip", lambda request: "192.168.1.77")
    auth = {"Authorization": "Bearer " + host_token(srv), "X-Forwarded-For": "127.0.0.1"}
    async with aiohttp.ClientSession() as session:
        async with session.post(base(srv) + "/api/host/start", headers=auth) as r:
            assert r.status == 403
        async with session.get(base(srv) + "/host") as r:
            assert r.status == 403
    assert srv.status()["state"] == "lobby"


async def test_host_api_ok_with_token_from_loopback(srv):
    async with aiohttp.ClientSession() as session:
        async with session.post(base(srv) + "/api/host/start", headers={"Authorization": "Bearer " + host_token(srv)}) as r:
            assert r.status == 200
            data = await r.json()
            assert data["state"] == "question"
            assert host_token(srv) not in json.dumps(data)
        async with session.post(base(srv) + "/api/host/bogus", headers={"Authorization": "Bearer " + host_token(srv)}) as r:
            assert r.status == 404


async def test_host_api_foreign_origin_refused(srv):
    headers = {"Authorization": "Bearer " + host_token(srv), "Origin": "http://evil.example"}
    async with aiohttp.ClientSession() as session:
        async with session.post(base(srv) + "/api/host/start", headers=headers) as r:
            assert r.status == 403
    assert srv.status()["state"] == "lobby"


async def test_host_page_served_on_loopback(srv):
    async with aiohttp.ClientSession() as session:
        async with session.get(base(srv) + "/host") as r:
            body = await r.text()
            assert r.status == 200 and "host.js" in body and host_token(srv) not in body


async def test_host_ws_auth(srv, monkeypatch):
    async with aiohttp.ClientSession() as session:
        ws = await session.ws_connect(base(srv) + "/ws")
        await ws.send_str(json.dumps({"t": "host", "cmd": "start"}))
        assert (await recv_until(ws, "error"))["code"] == "forbidden"
        await ws.send_str(json.dumps({"t": "host_auth", "token": "wrong"}))
        assert (await recv_until(ws, "error"))["code"] == "forbidden"
        await ws.send_str(json.dumps({"t": "host_auth", "token": host_token(srv)}))
        state = await recv_until(ws, "host_state")
        assert state["state"] == "lobby" and "host_token" not in state
        await ws.send_str(json.dumps({"t": "host", "cmd": "start"}))
        state = await recv_until(ws, "host_state")
        assert state["state"] == "question"
        await ws.close()
        monkeypatch.setattr(srv, "_peer_ip", lambda request: "10.0.0.9")
        ws = await session.ws_connect(base(srv) + "/ws")
        await ws.send_str(json.dumps({"t": "host_auth", "token": host_token(srv)}))
        assert (await recv_until(ws, "error"))["code"] == "forbidden"
        await ws.close()


# ---------------------------------------------------------------------------------------------- WS limits

async def test_ws_message_over_2kb_closes_connection(srv):
    async with aiohttp.ClientSession() as session:
        ws = await session.ws_connect(base(srv) + "/ws")
        await ws.send_str(json.dumps({"t": "join", "room": "X", "nickname": "a" * 3000}))
        msg = await ws.receive(timeout=5)
        assert msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING)
        assert ws.close_code == 1009 or msg.data == 1009
    assert srv.status()["players"] == 0


async def test_ws_rate_limit(srv):
    async with aiohttp.ClientSession() as session:
        ws = await session.ws_connect(base(srv) + "/ws")
        for _ in range(12):
            await ws.send_str('{"t":"ping"}')
        got = [json.loads((await ws.receive(timeout=5)).data) for _ in range(6)]
        kinds = [m["t"] for m in got]
        assert kinds.count("pong") == 5 and {"t": "error", "code": "rate_limited"} in got
        await ws.close()


async def test_join_rate_limit_per_ip():
    s = new_server(trust_loopback_joins=False)
    try:
        s.create_game(questions=QUESTIONS, bots=BOTS, doc=DOC, mode=Mode.OPEN_BOOK, budget_usd=1.0)
        async with aiohttp.ClientSession() as session:
            socks = []
            for i in range(10):
                ws = await join(session, s, f"P{i}")
                await recv_until(ws, "welcome")
                socks.append(ws)
            ws = await join(session, s, "Eleven")
            assert (await recv_until(ws, "error"))["code"] == "rate_limited"
            for w in socks + [ws]:
                await w.close()
        assert s.status()["players"] == 10
    finally:
        s.stop()


async def test_bad_messages(srv):
    async with aiohttp.ClientSession() as session:
        ws = await session.ws_connect(base(srv) + "/ws")
        await ws.send_str("not json")
        assert (await recv_until(ws, "error"))["code"] == "bad_message"
        await ws.send_str("[1,2]")
        assert (await recv_until(ws, "error"))["code"] == "bad_message"
        await ws.send_str('{"t":"launch_missiles"}')
        assert (await recv_until(ws, "error"))["code"] == "unknown_type"
        await asyncio.sleep(0.7)
        await ws.send_bytes(b"\x00\x01")
        assert (await recv_until(ws, "error"))["code"] == "bad_message"
        await ws.close()


async def test_foreign_origin_refused_for_join(srv):
    async with aiohttp.ClientSession() as session:
        ws = await join(session, srv, "Mallory", origin="http://evil.example")
        assert (await recv_until(ws, "error"))["code"] == "bad_origin"
        await ws.close()
        ws = await join(session, srv, "Nully", origin="null")
        assert (await recv_until(ws, "error"))["code"] == "bad_origin"
        await ws.close()
        ws = await join(session, srv, "Good", origin=base(srv))
        assert (await recv_until(ws, "welcome"))["nickname"] == "Good"
        await ws.close()
    assert srv.status()["players"] == 1


async def test_screen_allowed_with_null_origin(srv):
    async with aiohttp.ClientSession() as session:
        ws = await session.ws_connect(base(srv) + "/ws", headers={"Origin": "null"})
        await ws.send_str(json.dumps({"t": "screen", "room": ""}))
        room = await recv_until(ws, "room")
        assert room["room"] == srv.status()["room"]
        await recv_until(ws, "lobby")
        await ws.close()


async def test_bad_room_and_game_full(srv):
    async with aiohttp.ClientSession() as session:
        ws = await session.ws_connect(base(srv) + "/ws")
        await ws.send_str(json.dumps({"t": "join", "room": "ZZZZZZ", "nickname": "A"}))
        assert (await recv_until(ws, "error"))["code"] == "bad_room"
        srv._game.max_players = 1
        await ws.send_str(json.dumps({"t": "join", "room": srv.status()["room"], "nickname": "A"}))
        await recv_until(ws, "welcome")
        ws2 = await join(session, srv, "B")
        assert (await recv_until(ws2, "error"))["code"] == "game_full"
        await ws.close()
        await ws2.close()


async def test_xss_nickname_is_data_and_pages_never_use_html_sinks(srv):
    evil = "<script>alert(1)"  # 24 chars max per SPEC
    async with aiohttp.ClientSession() as session:
        screen = await session.ws_connect(base(srv) + "/ws")
        await screen.send_str(json.dumps({"t": "screen", "room": srv.status()["room"]}))
        ws = await join(session, srv, evil)
        welcome = await recv_until(ws, "welcome")
        assert welcome["nickname"] == evil
        lobby = await recv_until(screen, "lobby")
        while evil not in lobby["names"]:
            lobby = await recv_until(screen, "lobby")
        await ws.close()
        await screen.close()
    for js in STATIC.glob("*.js"):
        src = js.read_text()
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            assert sink not in src, (js.name, sink)


def test_message_page_escapes_html():
    resp = QuizServer._message_page(403, "<b>x</b>", "<script>alert(1)</script>")
    assert b"<script>alert" not in resp.body and b"&lt;script&gt;" in resp.body


def test_static_has_no_inline_script_style_or_cdn():
    for page in STATIC.glob("*.html"):
        src = page.read_text()
        for m in re.finditer(r"<script\b[^>]*>", src):
            assert 'src="/static/' in m.group(0) and 'type="module"' in m.group(0), page.name
        assert not re.search(r"<script\b[^>]*>(?!</script>)", src), page.name
        assert not re.search(r"\son[a-z]+\s*=", src, re.I), page.name
        assert not re.search(r"\sstyle\s*=", src, re.I), page.name
        assert "<style" not in src, page.name
    for f in STATIC.iterdir():
        text = f.read_text()
        urls = re.findall(r"https?://[^\s\"')]+", text)
        assert all(u.startswith("http://www.w3.org/") for u in urls), (f.name, urls)
        assert "setAttribute(\"style\"" not in text


def test_scanner_forbidden_literals_absent():
    bad = [re.compile(r"0\.0\.0\.0:\d"), re.compile("INADDR" + "_ANY"),
           re.compile("|".join(["ng" + "rok", "local" + "tunnel", "ser" + "veo", "cloud" + "flared"]), re.I)]
    files = [ROOT / n for n in ("server.py", "net.py", "conductor.py", "robots.py")] + list(STATIC.iterdir())
    for f in files:
        text = f.read_text()
        for rx in bad:
            assert not rx.search(text), (f.name, rx.pattern)
        assert "\u2014" not in text, f.name


# ---------------------------------------------------------------------------------------------- game flow

async def test_answer_requires_join_and_single_answer(srv):
    async with aiohttp.ClientSession() as session:
        anon = await session.ws_connect(base(srv) + "/ws")
        ws = await join(session, srv, "Ann")
        await recv_until(ws, "welcome")
        srv.host_command("start")
        q = await recv_until(ws, "question")
        assert "answer_index" not in q and "source_quote" not in q
        await anon.send_str(json.dumps({"t": "answer", "q": 0, "choice": 2}))
        assert (await recv_until(anon, "error"))["code"] == "not_joined"
        await ws.send_str(json.dumps({"t": "answer", "q": 0, "choice": True}))
        assert (await recv_until(ws, "error"))["code"] == "bad_choice"
        await ws.send_str(json.dumps({"t": "answer", "q": 0, "choice": 2}))
        assert (await recv_until(ws, "locked")) == {"t": "locked", "q": 0, "choice": 2}
        await ws.send_str(json.dumps({"t": "answer", "q": 0, "choice": 1}))
        assert (await recv_until(ws, "error"))["code"] == "already_answered"
        await ws.send_str(json.dumps({"t": "answer", "q": 1, "choice": 1}))
        assert (await recv_until(ws, "error"))["code"] == "wrong_question"
        reveal = await recv_until(ws, "reveal")
        assert reveal["you"]["correct"] is True and reveal["you"]["points"] > 0
        await ws.close()
        await anon.close()


async def test_bot_answers_hidden_until_reveal(srv):
    async with aiohttp.ClientSession() as session:
        screen = await session.ws_connect(base(srv) + "/ws")
        await screen.send_str(json.dumps({"t": "screen", "room": srv.status()["room"]}))
        ws = await join(session, srv, "Slow")  # never answers: the question runs to the deadline
        await recv_until(ws, "welcome")
        srv.host_command("start")
        log: list[str] = []
        reveal = await recv_until(screen, "reveal", timeout=8, log=log)
        before = [json.loads(x) for x in log[:-1]]
        answered = [m for m in before if m["t"] == "answered"]
        assert answered and set(answered[-1]["bots_done"]) == {"bot1", "bot2", "bot3"}
        for m in before:
            assert m["t"] != "reveal"
            assert "choice" not in json.dumps(m.get("bots_done", [])), m
            assert "answer_index" not in m and "bots" not in m or m["t"] == "room"
        assert {b["id"]: b["correct"] for b in reveal["bots"]} == {"bot1": True, "bot2": True, "bot3": True}
        assert all(b["latency_ms"] is not None for b in reveal["bots"])
        await ws.close()
        await screen.close()


async def test_late_joiner_can_play(srv):
    async with aiohttp.ClientSession() as session:
        srv.host_command("start")
        ws = await join(session, srv, "Late")
        await recv_until(ws, "welcome")
        q = await recv_until(ws, "question")
        await ws.send_str(json.dumps({"t": "answer", "q": q["q"], "choice": 2}))
        await recv_until(ws, "locked")
        await ws.close()


async def test_resume_bad_token(srv):
    async with aiohttp.ClientSession() as session:
        ws = await session.ws_connect(base(srv) + "/ws")
        await ws.send_str(json.dumps({"t": "resume", "room": srv.status()["room"], "token": "x" * 24}))
        assert (await recv_until(ws, "error"))["code"] == "bad_token"
        await ws.close()


async def test_reconnect_with_token_keeps_score(srv):
    room = srv.status()["room"]
    async with aiohttp.ClientSession() as session:
        ws = await join(session, srv, "Rae")
        welcome = await recv_until(ws, "welcome")
        srv.host_command("start")
        await recv_until(ws, "question")
        await ws.send_str(json.dumps({"t": "answer", "q": 0, "choice": 2}))
        reveal = await recv_until(ws, "reveal")
        score = reveal["you"]["score"]
        assert score > 0
        await ws.close()
        await asyncio.sleep(0.3)
        assert srv.status()["connected"] == 0
        ws2 = await session.ws_connect(base(srv) + "/ws")
        await ws2.send_str(json.dumps({"t": "resume", "room": room, "token": welcome["token"]}))
        again = await recv_until(ws2, "welcome")
        assert again["player_id"] == welcome["player_id"] and again["nickname"] == "Rae"
        lb = await recv_until(ws2, "leaderboard", timeout=6)
        assert lb["you"]["score"] == score and lb["you"]["rank"] == 1
        assert srv.status()["players"] == 1 and srv.status()["connected"] == 1
        await ws2.close()


async def test_no_public_ws_message_leaks_tokens():
    """Every message type seen by a screen, an anonymous socket and another player during a full game."""
    s = new_server()
    try:
        s.create_game(questions=QUESTIONS, bots=BOTS, doc=DOC, mode=Mode.OPEN_BOOK, budget_usd=1.0,
                      auto_advance=True, reveal_s=0.1)
        s._conductor.auto_advance_s = 0.1
        room = s.status()["room"]
        htok = host_token(s)
        async with aiohttp.ClientSession() as session:
            screen = await session.ws_connect(base(s) + "/ws")
            await screen.send_str(json.dumps({"t": "screen", "room": room}))
            anon = await session.ws_connect(base(s) + "/ws")
            await anon.send_str('{"t":"ping"}')
            a = await join(session, s, "Alice")
            tok_a = (await recv_until(a, "welcome"))["token"]
            b = await join(session, s, "Bob")
            log_b: list[str] = []
            tok_b = (await recv_until(b, "welcome", log=log_b))["token"]

            async def play(ws, log):
                while True:
                    m = json.loads((await ws.receive(timeout=10)).data)
                    if log is not None:
                        log.append(json.dumps(m))
                    if m["t"] == "question":
                        await ws.send_str(json.dumps({"t": "answer", "q": m["q"], "choice": 0}))
                    if m["t"] == "finished":
                        return

            s.host_command("start")
            log_screen: list[str] = []
            await asyncio.gather(play(a, None), play(b, log_b), recv_until(screen, "finished", 30, log_screen))
            await anon.send_str(json.dumps({"t": "resume", "room": room, "token": "nope"}))
            log_anon: list[str] = []
            await recv_until(anon, "error", log=log_anon)
            kinds = {json.loads(x)["t"] for x in log_screen + log_b}
            assert {"room", "lobby", "question", "answered", "reveal", "leaderboard", "finished", "locked",
                    "welcome"} <= kinds
            for raw in log_screen + log_anon:
                assert htok not in raw and tok_a not in raw and tok_b not in raw
            for raw in log_b:
                assert htok not in raw and tok_a not in raw
            for ws in (screen, anon, a, b):
                await ws.close()
    finally:
        s.stop()


async def test_full_game_five_clients_with_cassette_bots():
    cassette_path = ROOT / "sample" / "cassette.json"
    qfile = ROOT / "sample" / "questions.json"
    if not cassette_path.exists() or not qfile.exists():
        pytest.skip("sample cassette not available")
    from popquiz_plugin.cassette import CassetteLlm
    from popquiz_plugin.model import default_bots

    data = json.loads(qfile.read_text())
    qs = [Question(index=i, prompt=q["prompt"], choices=q["choices"], answer_index=q["answer_index"],
                   source_quote=q["source_quote"], explanation=q.get("explanation", ""), time_limit_s=4)
          for i, q in enumerate(data["questions"])]
    doc = Document(title=data.get("title", "Runbook"), text=(ROOT / "sample" / "runbook.md").read_text(),
                   path="runbook.md", sha256="1" * 64, kind="md")
    events = []
    s = new_server(llm_call=CassetteLlm(cassette_path, speed=40.0), on_event=events.append)
    try:
        s.create_game(questions=qs, bots=default_bots(), doc=doc, mode=Mode.OPEN_BOOK, budget_usd=1.0,
                      auto_advance=True, reveal_s=0.1)
        s._conductor.auto_advance_s = 0.05
        key = [q.answer_index for q in qs]
        async with aiohttp.ClientSession() as session:
            async def client(i):
                ws = await join(session, s, f"P{i}")
                await recv_until(ws, "welcome")
                ranks = None
                while True:
                    m = json.loads((await ws.receive(timeout=15)).data)
                    if m["t"] == "question":
                        choice = key[m["q"]] if i < 3 else (key[m["q"]] + 1) % len(m["choices"])
                        await ws.send_str(json.dumps({"t": "answer", "q": m["q"], "choice": choice}))
                    elif m["t"] == "leaderboard" and m.get("final"):
                        ranks = m["you"]
                    elif m["t"] == "finished":
                        await ws.close()
                        return ranks

            tasks = [asyncio.create_task(client(i)) for i in range(5)]
            await asyncio.sleep(0.5)
            s.host_command("start")
            results = await asyncio.wait_for(asyncio.gather(*tasks), 60)
        st = s.status()
        assert st["state"] == "finished" and st["players"] == 5
        assert all(r["rank"] <= 3 for r in results[:3]) and all(r["score"] == 0 for r in results[3:])
        rvm = {r["id"]: r for r in s._game.room_vs_models()}
        assert rvm["room"]["correct"] == len(qs)
        assert [e["t"] for e in events].count("reveal") == len(qs)
        assert events[-1]["t"] == "finished"
        rep = s.report()
        assert rep is not None
        data = s._conductor.report_data()
        assert data["played"] == len(qs) and len(data["models"]) == 3
    finally:
        s.stop()


async def test_open_lan_adds_second_site(srv, monkeypatch):
    monkeypatch.setattr(server_mod.net, "detect_lan_ipv4", lambda *a, **k: "127.0.0.2")
    st = srv.host_command("open_lan")
    assert st["lan_open"] and st["lan_ip"] == "127.0.0.2"
    assert srv.urls()["join"].startswith(f"http://127.0.0.2:{srv.bound_port}/?room=")
    assert srv.urls()["host"].startswith("http://127.0.0.1:")
    async with aiohttp.ClientSession() as session:
        async with session.get(f"http://127.0.0.2:{srv.bound_port}/health") as r:
            assert r.status == 200
    srv.host_command("open_lan")  # idempotent


def test_open_lan_without_network(srv, monkeypatch):
    monkeypatch.setattr(server_mod.net, "detect_lan_ipv4", lambda *a, **k: None)
    with pytest.raises(GameError) as exc:
        srv.host_command("open_lan")
    assert exc.value.code == "no_lan"


def test_public_url_used_for_join():
    s = new_server(public_url="https://quiz.example.org/")
    try:
        info = s.create_game(questions=QUESTIONS, bots=BOTS, doc=DOC, mode=Mode.OPEN_BOOK, budget_usd=1.0)
        assert s.urls()["join"] == f"https://quiz.example.org/?room={info['room']}"
    finally:
        s.stop()


async def test_end_command_broadcasts_finished(srv):
    async with aiohttp.ClientSession() as session:
        screen = await session.ws_connect(base(srv) + "/ws")
        await screen.send_str(json.dumps({"t": "screen", "room": srv.status()["room"]}))
        await recv_until(screen, "lobby")
        srv.host_command("start")
        await recv_until(screen, "question")
        srv.host_command("end")
        fin = await recv_until(screen, "finished")
        assert fin["report_url"].startswith("/report?room=")
        await screen.close()
    with pytest.raises(GameError):
        srv.host_command("bogus")


def test_on_event_can_call_api_from_server_thread():
    out, box = [], {}

    def on_event(ev):
        if ev["t"] == "created":
            out.append(box["s"].status()["state"])
            out.append(box["s"].host_command("start").get("scheduled"))

    s = QuizServer(port=0, llm_call=make_llm(), on_event=on_event)
    box["s"] = s
    s.start()
    try:
        s.create_game(questions=QUESTIONS, bots=BOTS, doc=DOC, mode=Mode.OPEN_BOOK, budget_usd=1.0)
        time.sleep(0.3)
        assert out == ["lobby", True]
        assert s.status()["state"] in ("question", "reveal", "leaderboard")
    finally:
        s.stop()


def test_api_requires_running_server():
    s = QuizServer(port=0)
    with pytest.raises(GameError) as exc:
        s.host_command("start")
    assert exc.value.code == "not_running"
    assert s.report() is None and s.urls()["base"].startswith("http://127.0.0.1:")
