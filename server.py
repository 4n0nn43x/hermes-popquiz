"""Real-time server: aiohttp app in a dedicated daemon thread with its own asyncio loop.

Thread model: `QuizServer` owns one thread running one event loop and one aiohttp `AppRunner`.
Every public method is thread-safe: it hands work to the server loop with
`asyncio.run_coroutine_threadsafe` and waits for the result. All game state (the pure `engine.Game`)
is mutated only on the server loop, so the engine needs no locks.

Security (SPEC section 8): loopback bind by default, one extra site on the detected private LAN
IPv4 only after `open_lan`, host access needs the host token AND a loopback peer, player tokens for
every player action, CSP and friends on every response, 2 KB WebSocket messages, 200 players,
5 messages/s per connection, 10 joins per minute per IP, Origin checked on state-changing messages,
automatic stop 10 minutes after the game ends. aiohttp and segno are imported lazily.

Hardening after review: every response checks the Host header (loopback names, the LAN IP served, the
`public_url` host) against DNS rebinding; loopback joins skip the join limit only without `public_url`
(a tunnel delivers every player from loopback). Each connection has a bounded outbox drained by its
own writer task, so no broadcast ever waits for one client; a client too slow to keep up is dropped
and can resume with its token. A socket must pick a role within ROLE_TIMEOUT_S; non loopback IPs have
MAX_CONNS_PER_IP sockets; HOST_SLOTS loopback slots above MAX_CONNECTIONS only accept `host_auth`, and
a newcomer on a full server evicts the oldest socket without a role.
"""

from __future__ import annotations

import asyncio
import errno
import hmac
import html
import io
import json
import logging
import secrets
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from . import net

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().with_name("static")
CSP = "default-src 'self'; img-src 'self' data:; connect-src 'self'; style-src 'self'; frame-ancestors *"
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
ROOM_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L
MAX_MSG_BYTES = 2048
MAX_PLAYERS = 200
MAX_CONNECTIONS = 400
MAX_CONNS_PER_IP = 20  # non loopback peers; a tunnel (public_url) arrives from loopback
HOST_SLOTS = 4  # loopback slots above MAX_CONNECTIONS, usable only by the host console
ROLE_TIMEOUT_S = 10.0  # a socket must send join, resume, screen or host_auth within this delay
OUTBOX_MAX = 64  # queued messages per connection before a slow client is dropped
CLOSE_TIMEOUT_S = 1.0
MSG_RATE_PER_S = 5.0
MSG_BURST = 5.0
JOINS_PER_MIN = 10
BATCH_S = 0.25
HOST_CMDS = ("open_lan", "start", "next", "skip", "end")
PAGES = {"/": "join.html", "/play": "play.html", "/screen": "screen.html", "/report": "report.html"}


def _game_error(code: str) -> Exception:
    try:
        from .model import GameError
    except Exception:  # pragma: no cover - model.py always ships with the plugin
        err = RuntimeError(code)
        err.code = code  # type: ignore[attr-defined]
        return err
    return GameError(code)


def _state_name(state: Any) -> str:
    return str(getattr(state, "value", state) or "")


class _Conn:
    """One WebSocket connection and its limits."""

    __slots__ = ("ws", "ip", "origin", "host_hdr", "role", "player_id", "allowance", "last", "drops", "auth_fail",
                 "transport", "spare", "opened", "outbox", "writer")

    def __init__(self, ws, ip: str, origin: str | None, host_hdr: str, now: float, transport=None,
                 spare: bool = False):
        self.ws = ws
        self.ip = ip
        self.origin = origin
        self.host_hdr = host_hdr
        self.role: str | None = None  # "player" | "screen" | "host"
        self.player_id: str | None = None
        self.allowance = MSG_BURST
        self.last = now
        self.drops = 0
        self.auth_fail = 0
        self.transport = transport
        self.spare = spare  # admitted on a host slot: may only authenticate as host
        self.opened = now
        self.outbox: asyncio.Queue = asyncio.Queue(OUTBOX_MAX)
        self.writer: asyncio.Task | None = None

    def drop(self) -> None:
        """Abort the TCP connection without waiting for the client (its reader loop then ends)."""
        if self.transport is not None:
            self.transport.abort()  # also after a close() stuck flushing to a client that never reads

    def allow(self, now: float) -> bool:
        self.allowance = min(MSG_BURST, self.allowance + (now - self.last) * MSG_RATE_PER_S)
        self.last = now
        if self.allowance < 1.0:
            return False
        self.allowance -= 1.0
        return True


class QuizServer:
    def __init__(self, *, host: str = "127.0.0.1", port: int = 8765, llm_call=None, pricing=None, on_event=None,
                 public_url: str | None = None, idle_shutdown_s: float = 600.0, trust_loopback_joins: bool = True,
                 clock: Callable[[], float] = time.monotonic):
        self.host = host
        self.port = port
        self.llm_call = llm_call
        self.pricing = pricing
        self.on_event = on_event
        self.public_url = (public_url or "").strip().rstrip("/") or None
        pub = self.public_url or ""
        self._public_host = urlsplit(pub if "//" in pub else "//" + pub).hostname if pub else None
        self.idle_shutdown_s = idle_shutdown_s
        self.trust_loopback_joins = trust_loopback_joins
        self.clock = clock
        self.wall = time.time

        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._start_error: BaseException | None = None
        self._stop_evt: asyncio.Event | None = None
        self._runner = None
        self._bound_port: int | None = None
        self._lan_ip: str | None = None

        self._game = None
        self._conductor = None
        self._room: str = ""
        self._host_token: str = ""
        self._meta: dict = {}
        self._conns: set[_Conn] = set()
        self._join_log: dict[str, deque] = {}
        self._dirty: set[str] = set()
        self._idle_handle: asyncio.TimerHandle | None = None
        self._last_lobby: tuple | None = None
        self._bg_tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ lifecycle (any thread)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and self._ready.is_set() and self._start_error is None)

    def start(self) -> None:
        if self.running:
            return
        import aiohttp  # noqa: F401  (lazy import; fail fast in the caller's thread)

        self._ready.clear()
        self._start_error = None
        self._thread = threading.Thread(target=self._thread_main, name="popquiz-server", daemon=True)
        self._thread.start()
        if not self._ready.wait(15):
            raise RuntimeError("popquiz server did not start in time")
        if self._start_error is not None:
            self._thread.join(5)
            self._thread = None
            raise self._start_error

    def stop(self, timeout: float = 5.0) -> None:
        thread, loop, evt = self._thread, self._loop, self._stop_evt
        if thread is None:
            return
        if loop is not None and evt is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(evt.set)
            except RuntimeError:
                pass
        if threading.current_thread() is not thread:
            thread.join(timeout)
            if not thread.is_alive():
                self._thread = None

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main())
        except BaseException:  # pragma: no cover - defensive
            log.exception("popquiz server loop crashed")
        finally:
            try:
                pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                for t in pending:
                    t.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:  # pragma: no cover
                pass
            loop.close()
            self._loop = None
            self._ready.set()

    async def _main(self) -> None:
        self._stop_evt = asyncio.Event()
        try:
            await self._setup()
        except OSError as exc:
            await self._cleanup()
            if exc.errno in (errno.EADDRINUSE, errno.EACCES):
                self._start_error = _game_error("port_in_use")
            else:
                self._start_error = exc
            self._ready.set()
            return
        except BaseException as exc:
            await self._cleanup()
            self._start_error = exc
            self._ready.set()
            return
        self._ready.set()
        flusher = asyncio.create_task(self._flush_loop())
        try:
            await self._stop_evt.wait()
        finally:
            flusher.cancel()
            if self._conductor is not None:
                await self._conductor.shutdown()
            await asyncio.gather(*(self._close(c) for c in list(self._conns)), return_exceptions=True)
            await self._cleanup()
            self._notify({"t": "stopped"})

    async def _cleanup(self) -> None:
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception:  # pragma: no cover
                pass
        self._runner = None
        self._lan_ip = None

    async def _setup(self) -> None:
        from aiohttp import web

        app = web.Application(client_max_size=64 * 1024, middlewares=[self._error_middleware()])
        app.on_response_prepare.append(self._add_headers)
        r = app.router
        for path, page in PAGES.items():
            r.add_get(path, self._page(page))
        r.add_get("/host", self._host_page)
        r.add_get("/health", self._health)
        r.add_get("/qr.svg", self._qr)
        r.add_get("/ws", self._ws)
        r.add_get("/api/report", self._api_report)
        r.add_post("/api/host/{cmd}", self._api_host)
        r.add_static("/static/", STATIC_DIR, show_index=False, follow_symlinks=False)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        self._bound_port = self._runner.addresses[0][1] if self._runner.addresses else self.port

    # ------------------------------------------------------------------ thread bridge

    def _call(self, coro, timeout: float = 10.0):
        loop = self._loop
        if loop is None or not self.running:
            coro.close()
            raise _game_error("not_running")
        if threading.current_thread() is self._thread:
            coro.close()
            raise RuntimeError("QuizServer API called from the server thread; use the async internals")
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)

    # ------------------------------------------------------------------ public API

    @property
    def bound_port(self) -> int:
        return self._bound_port or self.port

    def create_game(self, *, questions, bots, doc, mode, budget_usd, auto_advance=False, reveal_s: float = 6.0) -> dict:
        return self._call(self._create_game(questions=list(questions), bots=list(bots), doc=doc, mode=mode,
                                            budget_usd=float(budget_usd), auto_advance=bool(auto_advance),
                                            reveal_s=float(reveal_s)))

    def host_command(self, cmd: str) -> dict:
        if self._thread is not None and threading.current_thread() is self._thread:
            # Called from on_event (server thread): schedule instead of blocking our own loop.
            task = asyncio.ensure_future(self._host_command(cmd))
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
            return {"scheduled": True, **self._status_sync()}
        return self._call(self._host_command(cmd), timeout=15.0)

    def status(self) -> dict:
        if not self.running:
            return {"running": False}
        if threading.current_thread() is self._thread:
            return self._status_sync()
        return self._call(self._status_async())

    def urls(self) -> dict:
        port = self.bound_port
        base = f"http://{self._display_host()}:{port}"
        out = {"base": base}
        if self._room:
            out.update({
                "host": f"{base}/host#{self._host_token}",
                "screen": f"{base}/screen?room={self._room}",
                "join": self._join_url(),
                "report": f"{base}/report?room={self._room}",
            })
        return out

    def report(self):
        c = self._conductor
        return c.report if c is not None else None

    # ------------------------------------------------------------------ helpers

    def _display_host(self) -> str:
        return "127.0.0.1" if self.host in ("", "localhost") else self.host

    def _join_url(self) -> str:
        if self.public_url:
            return f"{self.public_url}/?room={self._room}"
        host = self._lan_ip or self._display_host()
        return f"http://{host}:{self.bound_port}/?room={self._room}"

    def _peer_ip(self, request) -> str:
        """Socket peer address. Forwarding headers are never trusted."""
        transport = request.transport
        peer = transport.get_extra_info("peername") if transport is not None else None
        if isinstance(peer, (tuple, list)) and peer:
            return str(peer[0])
        return str(request.remote or "")

    def _token_ok(self, token: Any) -> bool:
        if not isinstance(token, str) or not token or not self._host_token or len(token) > 256:
            return False
        return hmac.compare_digest(token.encode(), self._host_token.encode())

    @staticmethod
    def _origin_ok(origin: str | None, host_hdr: str, allow_null: bool = False) -> bool:
        if origin is None:
            return True  # non-browser client: browsers always send Origin on WebSocket and POST
        if origin == "null":
            return allow_null
        parts = urlsplit(origin)
        return parts.scheme in ("http", "https") and bool(host_hdr) and parts.netloc.lower() == host_hdr.lower()

    def _host_ok(self, host_hdr: str | None) -> bool:
        """DNS rebinding guard: the Host header must name this server (no header: non-browser client)."""
        if host_hdr is None:
            return True
        try:
            name = urlsplit("//" + host_hdr).hostname or ""
        except ValueError:
            return False
        return bool(name) and name in {"localhost", "127.0.0.1", "::1", self.host, self._lan_ip, self._public_host}

    def _notify(self, event: dict) -> None:
        self._dirty.add("host")
        cb = self.on_event
        if cb is None:
            return
        try:
            cb(dict(event))
        except Exception:
            log.exception("popquiz on_event callback failed")

    def mark_dirty(self, kind: str) -> None:
        self._dirty.add(kind)

    # ------------------------------------------------------------------ middleware and headers

    async def _add_headers(self, request, response) -> None:
        for k, v in SECURITY_HEADERS.items():
            response.headers[k] = v
        if request.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-cache")
        else:
            response.headers["Cache-Control"] = "no-store"

    def _error_middleware(self):
        from aiohttp import web

        @web.middleware
        async def mw(request, handler):
            if not self._host_ok(request.headers.get("Host")):
                return web.Response(status=421, text="unknown host")
            try:
                return await handler(request)
            except web.HTTPException:
                raise
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("popquiz request failed: %s", request.path)
                return web.Response(status=500, text="internal error")

        return mw

    # ------------------------------------------------------------------ HTTP routes

    def _page(self, name: str):
        from aiohttp import web

        async def handler(request):
            return web.Response(body=(STATIC_DIR / name).read_bytes(), content_type="text/html", charset="utf-8")

        return handler

    @staticmethod
    def _message_page(status: int, title: str, message: str):
        from aiohttp import web

        body = (
            "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><title>" + html.escape(title)
            + "</title><link rel=\"stylesheet\" href=\"/static/style.css\"></head><body class=\"page-msg\"><main><h1>"
            + html.escape(title) + "</h1><p>" + html.escape(message) + "</p></main></body></html>"
        )
        return web.Response(status=status, text=body, content_type="text/html")

    async def _host_page(self, request):
        from aiohttp import web

        if not net.is_loopback(self._peer_ip(request)):
            return self._message_page(403, "Host only", "The host page only opens on the machine running Hermes.")
        return web.Response(body=(STATIC_DIR / "host.html").read_bytes(), content_type="text/html", charset="utf-8")

    async def _health(self, request):
        from aiohttp import web

        return web.json_response({"ok": True})

    async def _qr(self, request):
        from aiohttp import web

        room = request.query.get("room", "").strip().upper()
        if not self._room or room != self._room:
            raise web.HTTPNotFound()
        try:
            import segno
        except Exception:
            raise web.HTTPNotFound()
        buf = io.BytesIO()
        segno.make(self._join_url(), error="m").save(buf, kind="svg", scale=10, border=2, dark="#0b0d17",
                                                     light="#ffffff", xmldecl=False)
        return web.Response(body=buf.getvalue(), content_type="image/svg+xml")

    async def _api_report(self, request):
        from aiohttp import web

        room = request.query.get("room", "").strip().upper()
        g = self._game
        if g is None or room != self._room:
            raise web.HTTPNotFound()
        if _state_name(g.state) != "finished" or self._conductor is None:
            return web.json_response({"ready": False, "state": _state_name(g.state)}, status=409)
        return web.json_response({"ready": True, **self._conductor.report_data()})

    async def _api_host(self, request):
        from aiohttp import web

        auth = request.headers.get("Authorization", "")
        token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
        if not net.is_loopback(self._peer_ip(request)) or not self._token_ok(token):
            return web.json_response({"error": "forbidden"}, status=403)
        if not self._origin_ok(request.headers.get("Origin"), request.host):
            return web.json_response({"error": "bad_origin"}, status=403)
        cmd = request.match_info.get("cmd", "")
        if cmd not in HOST_CMDS:
            return web.json_response({"error": "bad_command"}, status=404)
        try:
            return web.json_response(await self._host_command(cmd))
        except Exception as exc:
            code = getattr(exc, "code", None)
            if not code:
                raise
            return web.json_response({"error": code}, status=409)

    # ------------------------------------------------------------------ WebSocket

    async def _ws(self, request):
        from aiohttp import WSMsgType, web

        ip = self._peer_ip(request)
        spare = self._admit(ip)
        if spare is None:
            raise web.HTTPServiceUnavailable()
        ws = web.WebSocketResponse(max_msg_size=MAX_MSG_BYTES, heartbeat=25.0)
        conn = _Conn(ws, ip, request.headers.get("Origin"), request.host, self.clock(), request.transport, spare)
        self._conns.add(conn)  # counted before the handshake awaits, so the limits stay exact
        loop = asyncio.get_running_loop()
        deadline: float | None = loop.time() + ROLE_TIMEOUT_S
        try:
            await ws.prepare(request)
            conn.writer = asyncio.create_task(self._write_loop(conn))
            while True:
                if conn.role is not None:
                    deadline = None  # once a role was chosen the socket may stay idle (phones, screens)
                try:
                    msg = await ws.receive(timeout=None if deadline is None else max(0.01, deadline - loop.time()))
                except TimeoutError:
                    break  # no role in time: free the slot
                if msg.type == WSMsgType.TEXT:
                    await self._on_text(conn, msg.data)
                elif msg.type == WSMsgType.BINARY:
                    await self._send(conn, {"t": "error", "code": "bad_message"})
                else:
                    break
        finally:
            if conn.writer is not None:
                conn.writer.cancel()
            self._conns.discard(conn)
            pid = conn.player_id
            g = self._game
            if pid and g is not None and not any(c.player_id == pid for c in self._conns):
                try:
                    g.set_connected(pid, False)
                except Exception:
                    pass
                self._dirty.update(("lobby", "answered"))
                if self._conductor is not None:
                    self._conductor.wake()
        return ws

    def _admit(self, ip: str) -> bool | None:
        """Connection limits. None: refuse; True: admitted on a host slot; False: regular slot."""
        local = net.is_loopback(ip)
        if not local and sum(1 for c in self._conns if c.ip == ip) >= MAX_CONNS_PER_IP:
            return None
        if len(self._conns) >= MAX_CONNECTIONS + (HOST_SLOTS if local else 0):
            idle = min((c for c in self._conns if c.role is None), key=lambda c: c.opened, default=None)
            if idle is None:
                return None
            self._conns.discard(idle)  # its handler finishes the cleanup
            idle.drop()
        return len(self._conns) >= MAX_CONNECTIONS

    async def _send(self, conn: _Conn, msg: dict | str) -> None:
        """Queue a message for this connection. Never waits for the client: a full outbox drops it."""
        data = msg if isinstance(msg, str) else json.dumps(msg, separators=(",", ":"))
        try:
            conn.outbox.put_nowait(data)
        except asyncio.QueueFull:
            log.info("popquiz dropping a client that does not read (%s)", conn.ip)
            conn.drop()

    async def _write_loop(self, conn: _Conn) -> None:
        try:
            while True:
                await conn.ws.send_str(await conn.outbox.get())
        except asyncio.CancelledError:
            raise
        except Exception:
            if not conn.ws.closed:  # a broken transport; a normal close needs nothing more
                conn.drop()

    async def _close(self, conn: _Conn) -> None:
        try:
            await asyncio.wait_for(conn.ws.close(code=1001, message=b"server stopping"), CLOSE_TIMEOUT_S)
        except Exception:
            conn.drop()

    async def broadcast(self, msg: dict, personal: Callable[[str], dict | None] | None = None,
                        roles: tuple[str, ...] = ("player", "screen", "host")) -> None:
        """Send `msg` to every connection with a role; players get `you` from `personal(player_id)`."""
        base = json.dumps(msg, separators=(",", ":"))
        for conn in list(self._conns):
            if conn.role not in roles:
                continue
            data = base
            if personal is not None and conn.role == "player" and conn.player_id:
                you = personal(conn.player_id)
                if you is not None:
                    data = json.dumps({**msg, "you": you}, separators=(",", ":"))
            await self._send(conn, data)  # only queues: never waits for this client

    async def _on_text(self, conn: _Conn, data: str) -> None:
        if not conn.allow(self.clock()):
            conn.drops += 1
            if conn.drops > 40:
                await conn.ws.close(code=1008, message=b"rate limited")
            elif conn.drops == 1 or conn.drops % 10 == 0:
                await self._send(conn, {"t": "error", "code": "rate_limited"})
            return
        try:
            msg = json.loads(data)
        except (ValueError, TypeError):
            await self._send(conn, {"t": "error", "code": "bad_message"})
            return
        if not isinstance(msg, dict) or not isinstance(msg.get("t"), str):
            await self._send(conn, {"t": "error", "code": "bad_message"})
            return
        handler = {
            "ping": self._m_ping, "join": self._m_join, "resume": self._m_resume, "answer": self._m_answer,
            "screen": self._m_screen, "host_auth": self._m_host_auth, "host": self._m_host,
        }.get(msg["t"])
        if handler is None:
            await self._send(conn, {"t": "error", "code": "unknown_type"})
            return
        try:
            await handler(conn, msg)
        except Exception as exc:
            code = getattr(exc, "code", None)
            if not isinstance(code, str):
                log.exception("popquiz ws handler failed")
                code = "internal"
            await self._send(conn, {"t": "error", "code": code})

    async def _m_ping(self, conn, msg):
        await self._send(conn, {"t": "pong", "server_now_ms": int(self.wall() * 1000)})

    def _check_room(self, msg: dict, required: bool = True):
        g = self._game
        if g is None:
            raise _game_error("no_game")
        room = msg.get("room")
        if room in (None, "") and not required:
            return g
        if not isinstance(room, str) or room.strip().upper() != self._room:
            raise _game_error("bad_room")
        return g

    def _join_allowed(self, ip: str) -> bool:
        if self.trust_loopback_joins and not self.public_url and net.is_loopback(ip):
            return True
        now = self.clock()
        dq = self._join_log.setdefault(ip, deque())
        while dq and now - dq[0] > 60.0:
            dq.popleft()
        if len(dq) >= JOINS_PER_MIN:
            return False
        dq.append(now)
        return True

    async def _m_join(self, conn, msg):
        if not self._origin_ok(conn.origin, conn.host_hdr):
            raise _game_error("bad_origin")
        g = self._check_room(msg)
        if conn.player_id:
            raise _game_error("already_joined")
        if conn.spare:
            raise _game_error("server_full")
        if not self._join_allowed(conn.ip):
            raise _game_error("rate_limited")
        nickname = msg.get("nickname")
        if not isinstance(nickname, str) or len(nickname) > 200:
            raise _game_error("bad_nickname")
        player, token = g.add_player(nickname)
        conn.role, conn.player_id = "player", player.id
        await self._send(conn, {"t": "welcome", "player_id": player.id, "token": token, "nickname": player.nickname})
        await self._snapshot(conn)
        self._dirty.update(("lobby", "answered"))
        self._notify({"t": "player_joined", "players": len(g.players)})

    async def _m_resume(self, conn, msg):
        if not self._origin_ok(conn.origin, conn.host_hdr):
            raise _game_error("bad_origin")
        g = self._check_room(msg)
        if conn.player_id:
            raise _game_error("already_joined")
        if conn.spare:
            raise _game_error("server_full")
        token = msg.get("token")
        if not isinstance(token, str) or not token or len(token) > 128:
            raise _game_error("bad_token")
        player = g.resume(token)
        conn.role, conn.player_id = "player", player.id
        g.set_connected(player.id, True)
        await self._send(conn, {"t": "welcome", "player_id": player.id, "token": token, "nickname": player.nickname})
        await self._snapshot(conn)
        self._dirty.update(("lobby", "answered"))

    async def _m_answer(self, conn, msg):
        if conn.role != "player" or not conn.player_id:
            raise _game_error("not_joined")
        if not self._origin_ok(conn.origin, conn.host_hdr):
            raise _game_error("bad_origin")
        q, choice = msg.get("q"), msg.get("choice")
        if type(q) is not int or type(choice) is not int:
            raise _game_error("bad_choice")
        g = self._game
        if g is None:
            raise _game_error("no_game")
        g.submit_answer(conn.player_id, q, choice)
        await self._send(conn, {"t": "locked", "q": q, "choice": choice})
        self._dirty.add("answered")
        if self._conductor is not None:
            self._conductor.wake()

    async def _m_screen(self, conn, msg):
        if not self._origin_ok(conn.origin, conn.host_hdr, allow_null=True):
            raise _game_error("bad_origin")
        self._check_room(msg, required=False)
        if conn.spare:
            raise _game_error("server_full")
        conn.role = "screen"
        await self._snapshot(conn)

    async def _m_host_auth(self, conn, msg):
        if conn.auth_fail >= 5:
            await conn.ws.close(code=1008, message=b"forbidden")
            return
        if not net.is_loopback(conn.ip) or not self._token_ok(msg.get("token")):
            conn.auth_fail += 1
            raise _game_error("forbidden")
        conn.role = "host"
        await self._send(conn, await self._host_state())
        if self._game is not None:
            await self._snapshot(conn)

    async def _m_host(self, conn, msg):
        if conn.role != "host":
            raise _game_error("forbidden")
        if not self._origin_ok(conn.origin, conn.host_hdr):
            raise _game_error("bad_origin")
        cmd = msg.get("cmd")
        if cmd not in HOST_CMDS:
            raise _game_error("bad_command")
        await self._host_command(cmd)
        await self._send(conn, await self._host_state())

    # ------------------------------------------------------------------ snapshots and batches

    def _room_msg(self) -> dict:
        g = self._game
        try:
            import segno  # noqa: F401
            qr = True
        except Exception:
            qr = False
        join = self._join_url()
        return {
            "t": "room", "room": self._room, "title": self._meta.get("title", ""), "total": self._meta.get("total", 0),
            "mode": self._meta.get("mode", ""), "state": _state_name(g.state) if g is not None else "",
            "bots": [{"id": b.id, "label": b.label, "model": b.model} for b in self._meta.get("bots", [])],
            "join_url": join, "join_display": join.split("://", 1)[-1].split("/?", 1)[0], "qr": qr,
            "lan": bool(self._lan_ip or self.public_url),
        }

    def _lobby_msg(self) -> dict:
        g = self._game
        players = sorted(g.players.values(), key=lambda p: p.joined_at)
        return {"t": "lobby", "count": len(players), "names": [p.nickname for p in players[-30:]]}

    async def _snapshot(self, conn: _Conn) -> None:
        g, c = self._game, self._conductor
        if g is None:
            return
        await self._send(conn, self._room_msg())
        state = _state_name(g.state)
        pid = conn.player_id if conn.role == "player" else None
        if state == "lobby" or c is None:
            await self._send(conn, self._lobby_msg())
            return
        for m in c.snapshot(pid):
            await self._send(conn, m)

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(BATCH_S)
            if not self._dirty or self._game is None:
                continue
            dirty, self._dirty = self._dirty, set()
            try:
                if "lobby" in dirty:
                    msg = self._lobby_msg()
                    key = (msg["count"], tuple(msg["names"]))
                    if key != self._last_lobby:
                        self._last_lobby = key
                        await self.broadcast(msg)
                if "answered" in dirty and self._conductor is not None:
                    msg = self._conductor.answered_msg()
                    if msg is not None:
                        await self.broadcast(msg)
                if "host" in dirty:
                    hosts = [c for c in self._conns if c.role == "host"]
                    if hosts:
                        hs = await self._host_state()
                        for c in hosts:
                            await self._send(c, hs)
            except Exception:  # pragma: no cover - never let the flusher die
                log.exception("popquiz flush failed")

    # ------------------------------------------------------------------ game management (server loop)

    async def _create_game(self, *, questions, bots, doc, mode, budget_usd, auto_advance, reveal_s) -> dict:
        from .conductor import Conductor
        from .engine import Game

        if self._conductor is not None:
            await self._conductor.shutdown()
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None
        room = "".join(secrets.choice(ROOM_ALPHABET) for _ in range(6))
        self._host_token = secrets.token_urlsafe(16)
        game_id = secrets.token_hex(6)
        title = getattr(doc, "title", "") or ""
        game = Game(game_id=game_id, room_code=room, questions=questions, bots=bots, clock=self.clock, mode=mode,
                    title=title, doc_sha256=getattr(doc, "sha256", "") or "", budget_usd=budget_usd,
                    max_players=MAX_PLAYERS, reveal_s=reveal_s)
        self._game, self._room = game, room
        self._meta = {"title": title, "total": len(questions), "mode": _state_name(mode), "bots": bots,
                      "budget_usd": budget_usd, "game_id": game_id}
        self._conductor = Conductor(game=game, questions=questions, bots=bots, doc_text=getattr(doc, "text", "") or "",
                                    mode=mode, llm_call=self.llm_call, pricing=self.pricing, hub=self,
                                    auto_advance=auto_advance, reveal_s=reveal_s, clock=self.clock, wall=self.wall,
                                    room=room, budget_usd=budget_usd, title=title)
        self._last_lobby = None
        for conn in list(self._conns):
            if conn.role == "player":
                conn.role, conn.player_id = None, None
                await self._send(conn, {"t": "error", "code": "game_over"})
            elif conn.role in ("screen", "host"):
                await self._snapshot(conn)
        self._notify({"t": "created", "room": room, "game_id": game_id})
        return {"game_id": game_id, "room": room, "host_token": self._host_token, "urls": self.urls(),
                "players": [b.label for b in bots], "estimated_cost_usd": self._conductor.estimated_total_usd()}

    async def _open_lan(self) -> None:
        from aiohttp import web

        if self._lan_ip:
            return
        ip = net.detect_lan_ipv4()
        if not ip:
            raise _game_error("no_lan")
        site = web.TCPSite(self._runner, ip, self.bound_port)
        try:
            await site.start()
        except OSError:
            raise _game_error("port_in_use")
        self._lan_ip = ip
        for conn in list(self._conns):
            if conn.role in ("screen", "host"):
                await self._send(conn, self._room_msg())
        self._notify({"t": "lan_open", "join": self._join_url()})

    async def _host_command(self, cmd: str) -> dict:
        if cmd not in HOST_CMDS:
            raise _game_error("bad_command")
        if cmd == "open_lan":
            await self._open_lan()
            return await self._status_async()
        g, c = self._game, self._conductor
        if g is None or c is None:
            raise _game_error("no_game")
        if cmd == "start":
            c.start()
        elif cmd == "next":
            c.next()
        elif cmd == "skip":
            c.skip()
        elif cmd == "end":
            await c.end()
        await asyncio.sleep(0)
        return await self._status_async()

    def on_finished(self) -> None:
        """Called by the conductor when the game is over: schedule the idle stop."""
        loop = self._loop
        if loop is None or self._stop_evt is None:
            return
        if self._idle_handle is not None:
            self._idle_handle.cancel()
        self._idle_handle = loop.call_later(self.idle_shutdown_s, self._stop_evt.set)

    async def _status_async(self) -> dict:
        return self._status_sync()

    def _status_sync(self) -> dict:
        g, c = self._game, self._conductor
        out: dict = {"running": True, "port": self.bound_port, "lan_ip": self._lan_ip, "lan_open": bool(self._lan_ip),
                     "public_url": self.public_url}
        if g is None:
            out["state"] = "idle"
            return out
        connected = sum(1 for p in g.players.values() if getattr(p, "connected", True))
        out.update({
            "room": self._room, "game_id": self._meta.get("game_id"), "title": self._meta.get("title", ""),
            "state": _state_name(g.state), "question": g.current + 1 if g.current >= 0 else 0,
            "total": self._meta.get("total", 0), "players": len(g.players), "connected": connected,
            "spent_usd": round(float(getattr(g, "spent_usd", 0.0) or 0.0), 6),
            "budget_usd": self._meta.get("budget_usd"),
            "estimated_cost_usd": c.estimated_total_usd() if c is not None else 0.0,
            "auto_advance": c.auto_advance if c is not None else False,
            "bots": c.bots_status() if c is not None else [],
            "urls": {k: v for k, v in self.urls().items() if k != "host"},
        })
        return out

    async def _host_state(self) -> dict:
        st = await self._status_async()
        c = self._conductor
        if c is not None:
            st["answered"] = c.answered_count()
        return {"t": "host_state", **st}
