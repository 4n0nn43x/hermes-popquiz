"""Glue tests with a fake ctx (SPEC section 10, "Glue"): needs_fix then ready, /popquiz with ask_choice,
degraded mode without the model-override capabilities, throttled card edits, report delivery, register().
No Hermes import, no network, no model call (the real QuizServer runs on an ephemeral loopback port)."""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

import popquiz_plugin
from popquiz_plugin import cli, hermes_glue
from popquiz_plugin.hermes_glue import ChatCard, PopQuizApp
from popquiz_plugin.model import GameError
from popquiz_plugin.pricing import Pricing

RUNBOOK = str(hermes_glue.SAMPLE_DIR / "runbook.md")


class Handle:
    ok = True

    def __init__(self):
        self.edits: list[str] = []

    def edit(self, text):
        self.edits.append(text)


class FakeLlm:
    def __init__(self):
        self.calls: list[dict] = []

    def complete_structured(self, **kw):
        self.calls.append(kw)
        usage = SimpleNamespace(input_tokens=100, output_tokens=5, cache_read_tokens=0, cache_write_tokens=0)
        return SimpleNamespace(parsed={"choice": "B"}, text='{"choice": "B"}', model=kw.get("model", "main-x"),
                               usage=usage)


class FakeCtx:
    """The ctx surface the plugin uses, recording every call."""

    def __init__(self, *, caps=True, post_ok=True, choice=None, inject_ok=True):
        self.caps, self.choice, self.inject_ok = caps, choice, inject_ok
        self.handle = Handle() if post_ok else None
        self.config: dict = {}
        self.llm = FakeLlm()
        self.posted, self.injected, self.asked = [], [], []
        self.tools, self.commands, self.cli, self.skills, self.unload = {}, {}, {}, {}, []

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def session_cwd(self):
        return ""

    def has_capability(self, _cap):
        return self.caps

    def post_message(self, text):
        self.posted.append(text)
        return self.handle or SimpleNamespace(ok=False, error="gateway_unavailable")

    def inject_message(self, text, role="user"):
        self.injected.append((text, role))
        return self.inject_ok

    def ask_choice(self, question, choices):
        self.asked.append((question, choices))
        return self.choice

    def register_tool(self, name, toolset, schema, handler, **_kw):
        self.tools[name] = (schema, handler)

    def register_command(self, name, handler, **_kw):
        self.commands[name] = handler

    def register_cli_command(self, name, help, setup_fn, handler_fn, description=""):  # noqa: A002
        self.cli[name] = (setup_fn, handler_fn)

    def register_skill(self, name, path, description=""):
        self.skills[name] = (path, description)

    def on_unload(self, fn):
        self.unload.append(fn)


def make_app(ctx, **kw) -> PopQuizApp:
    kw.setdefault("settings_override", {"port": 0})
    kw.setdefault("pricing_loader", lambda _models: Pricing.fallback())
    return PopQuizApp(ctx, **kw)


@pytest.fixture
def app_ctx():
    made: list[PopQuizApp] = []

    def build(ctx=None, **kw):
        ctx = ctx or FakeCtx()
        app = make_app(ctx, **kw)
        made.append(app)
        return app, ctx

    yield build
    for app in made:
        app.shutdown()


def test_register_wires_tools_command_cli_skill_and_unload():
    ctx = FakeCtx()
    popquiz_plugin.register(ctx)
    assert sorted(ctx.tools) == sorted(popquiz_plugin.PROVIDED_TOOLS)
    for name, (schema, _handler) in ctx.tools.items():
        assert schema["name"] == name
    assert set(ctx.commands) == {"popquiz"} and set(ctx.cli) == {"popquiz"}
    path, desc = ctx.skills["host-a-quiz"]
    assert path.is_file() and path.read_text(encoding="utf-8").startswith("---\nname: host-a-quiz\n")
    assert 0 < len(desc) <= 60
    assert len(ctx.unload) == 1
    ctx.unload[0]()  # nothing running: must not raise


def test_create_needs_fix_then_ready_keeps_accepted_questions(app_ctx):
    app, ctx = app_ctx()
    first, fixes, final = cli.demo_drafts(8)
    res = app.create({"doc_path": RUNBOOK, "questions": first, "title": "Kestrel"})
    assert res["status"] == "needs_fix" and res["attempt"] == 1 and res["max_attempts"] == 3
    assert res["accepted"] == [0, 1, 2, 3, 4, 5]
    assert [(r["index"], r["code"]) for r in res["rejected"]] == [(6, "number_mismatch"), (7, "quote_not_found")]
    assert all(r["closest_passage"] for r in res["rejected"])
    assert app.server is None and not ctx.posted  # nothing listens before the quiz is ready

    res2 = app.create({"draft_id": res["draft_id"], "questions": fixes})
    assert res2["status"] == "ready" and res2["attempt"] == 2
    assert res2["questions"] == len(final) == 8
    assert res2["players"] == ["Claude Opus 5.5", "DeepSeek V4 Flash", "Mercury 3"] and not res2["degraded"]
    assert set(res2["urls"]) == {"screen", "join", "host"} and res2["urls"]["screen"].startswith("http://127.0.0.1:")
    assert res2["card"] == "posted" and len(ctx.posted) == 1 and res2["room"] in ctx.posted[0]
    # the questions really kept are the 6 accepted first + the 2 fixes, in slot order
    assert [q.prompt for q in app.server._game.questions] == [q["prompt"] for q in final]
    # the draft is consumed; reusing it is an explicit error
    assert app.create({"draft_id": res["draft_id"], "questions": fixes})["error"] == "unknown_draft"
    # public status never carries the host token
    st = json.loads(popquiz_plugin.hermes_glue.tool_json(app.status))
    assert st["state"] == "lobby" and "host" not in st["urls"] and "host_token" not in st


def test_create_argument_errors(app_ctx):
    app, _ = app_ctx()
    assert app.create({"questions": []})["error"] == "bad_arguments"
    assert app.create({"questions": [{"prompt": "x"}]})["error"] == "bad_arguments"  # no doc_path
    assert app.create({"draft_id": "nope", "questions": [{"prompt": "x"}]})["error"] == "unknown_draft"
    assert app.create({"doc_path": "/etc/hostname", "questions": [{"prompt": "x"}]})["error"] == "path_not_allowed"
    out = json.loads(hermes_glue.tool_json(lambda: (_ for _ in ()).throw(RuntimeError("boom"))))
    assert out["error"] == "internal_error"  # a tool never raises into Hermes


def test_relative_doc_path_is_relative_to_the_session_folder(app_ctx, tmp_path, monkeypatch):
    """Found in a real agent turn: the gateway's process cwd is not the session folder."""
    (tmp_path / "runbook.md").write_text((hermes_glue.SAMPLE_DIR / "runbook.md").read_text(encoding="utf-8"))
    ctx = FakeCtx()
    ctx.session_cwd = lambda: str(tmp_path)
    monkeypatch.chdir(hermes_glue.PLUGIN_DIR)  # some other folder, like the gateway's
    app, _ = app_ctx(ctx)
    _, _, final = cli.demo_drafts(3)
    assert app.create({"doc_path": "runbook.md", "questions": final})["status"] == "ready"
    assert app.create({"doc_path": "../outside.md", "questions": final})["error"] == "path_not_allowed"


def test_hidden_files_are_refused(tmp_path):
    secret = tmp_path / ".hidden.md"
    secret.write_text("x" * 50)
    with pytest.raises(GameError) as exc:
        hermes_glue.check_document_path(str(secret), [tmp_path])
    assert exc.value.code == "path_not_allowed"


def test_command_without_argument_uses_ask_choice(app_ctx):
    app, ctx = app_ctx(FakeCtx(choice="start"))
    assert "No quiz yet" in app.command("")  # no game: no menu, no prompt
    assert not ctx.asked
    _, _, final = cli.demo_drafts(3)
    assert app.create({"doc_path": RUNBOOK, "questions": final})["status"] == "ready"
    out = app.command("")
    question, options = ctx.asked[-1]
    assert "lobby" in question
    values = [o["value"] for o in options]
    assert values[:2] == ["open", "start"] and "end" in values and "status" in values
    assert "question 1/3" in out  # the picked choice ran
    ctx.choice = None
    assert app.command("").startswith("No choice made")
    assert "question" in app.command("status")
    assert app.command("bogus").startswith("Pop Quiz: bad_action")
    assert app.control("end")["status"]["state"] == "finished"
    assert [o["value"] for o in app.menu()] == ["report", "status"]


def test_degraded_without_capabilities_plays_main_model_only(app_ctx):
    app, ctx = app_ctx(FakeCtx(caps=False))
    _, _, final = cli.demo_drafts(3)
    res = app.create({"doc_path": RUNBOOK, "questions": final})
    assert res["status"] == "ready" and res["degraded"] is True and "note" in res
    assert len(res["players"]) == 1
    bots, degraded = hermes_glue.resolve_bots(ctx, hermes_glue.read_settings(ctx))
    assert degraded and bots[0].provider == ""
    # the degraded bot sends no provider/model override; a granted bot sends both
    call = hermes_glue.make_llm_call(ctx).call_sync
    call(bots[0], "instr", "input", {"type": "object"}, 5)
    assert "provider" not in ctx.llm.calls[-1] and "model" not in ctx.llm.calls[-1]
    granted = hermes_glue.parse_players(None)[1]
    reply = call(granted, "instr", "input", {"type": "object"}, 5)
    assert ctx.llm.calls[-1]["provider"] == "nous" and ctx.llm.calls[-1]["model"] == granted.model
    assert ctx.llm.calls[-1]["timeout"] == 5.0
    assert reply.parsed == {"choice": "B"} and reply.input_tokens == 100 and reply.output_tokens == 5


def test_capability_check_fails_closed():
    class Broken(FakeCtx):
        def has_capability(self, _cap):
            raise RuntimeError("old Hermes")

    assert hermes_glue.capabilities_granted(Broken()) is False


def test_settings_are_read_at_call_time_and_clamped():
    ctx = FakeCtx()
    ctx.config = {"port": "99999", "budget_usd": "abc", "time_limit_s": 1, "mode": "weird", "auto_advance": "yes",
                  "players": [{"model": "x-ai/grok-4.3"}, 42, {"label": "no model"}]}
    s = hermes_glue.read_settings(ctx)
    assert s["port"] == 65535 and s["budget_usd"] == 1.0 and s["time_limit_s"] == 5
    assert s["mode"] == "open_book" and s["auto_advance"] is True
    assert [(b.label, b.provider, b.model) for b in s["players"]] == [("grok-4.3", "nous", "x-ai/grok-4.3")]


class FakeClock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def test_card_edits_are_throttled_to_one_every_two_seconds():
    ctx, clock, timers = FakeCtx(), FakeClock(), []
    card = ChatCard(ctx, clock=clock, timer=lambda delay, fn: timers.append((clock.t + delay, fn)))
    assert card.post("v0") and card.active
    sent_at: list[float] = []
    orig_edit = ctx.handle.edit
    ctx.handle.edit = lambda text: (sent_at.append(clock.t), orig_edit(text))

    def fire_due():
        while timers and timers[0][0] <= clock.t:
            _, fn = timers.pop(0)
            fn()

    # 20 updates over 6 s, every 0.3 s
    for i in range(1, 21):
        clock.t = 100.0 + 0.3 * i
        fire_due()
        card.update(f"v{i}")
    clock.t += 5
    fire_due()
    assert ctx.handle.edits, "no edit at all"
    assert ctx.handle.edits[-1] == "v20"  # the latest state always lands
    gaps = [b - a for a, b in zip([100.0] + sent_at, sent_at)]
    assert all(g >= 2.0 - 1e-9 for g in gaps), gaps
    assert len(ctx.handle.edits) <= 4  # 6 s of updates: at most one edit per 2 s
    # force (game finished) bypasses the throttle
    n = len(ctx.handle.edits)
    card.update("final", force=True)
    assert len(ctx.handle.edits) == n + 1 and ctx.handle.edits[-1] == "final"


def test_card_falls_back_to_text_when_posting_is_unavailable(app_ctx):
    app, ctx = app_ctx(FakeCtx(post_ok=False))
    _, _, final = cli.demo_drafts(3)
    res = app.create({"doc_path": RUNBOOK, "questions": final})
    assert res["status"] == "ready" and res["card"].startswith("unavailable (gateway_unavailable)")
    assert app.card is None


class FakeServer:
    running = True

    def __init__(self, **_kw):
        self.stopped = False

    def start(self):
        pass

    def stop(self):
        self.stopped = True

    def status(self):
        return {"state": "finished", "room": "ABCDEF", "players": 3, "total": 3, "spent_usd": 0.01,
                "budget_usd": 1.0, "urls": {"screen": "s", "report": "r"}}

    def urls(self):
        return {"report": "http://127.0.0.1:1/report?room=ABCDEF"}

    def report(self):
        return SimpleNamespace(markdown="# Report\nThe room missed Q2.")


def _wait(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end and not pred():
        time.sleep(0.02)
    return pred()


@pytest.mark.parametrize("inject_ok", [True, False])
def test_report_is_injected_or_left_for_popquiz_report(inject_ok):
    ctx = FakeCtx(inject_ok=inject_ok)
    app = PopQuizApp(ctx)
    app.server = FakeServer()
    try:
        app.on_event({"t": "finished"})
        assert _wait(lambda: app.report_delivered is not None)
        text, role = ctx.injected[-1]
        assert role == "user" and "The room missed Q2." in text
        assert app.report_delivered == ("injected" if inject_ok else "pending")
        rep = app.report()
        assert rep["report_markdown"].startswith("# Report") and rep["report_url"].endswith("room=ABCDEF")
    finally:
        app.shutdown()
    assert app.server is None


def test_shutdown_stops_the_real_server(app_ctx):
    before = {t.ident for t in threading.enumerate()}
    app, _ = app_ctx()
    _, _, final = cli.demo_drafts(3)
    assert app.create({"doc_path": RUNBOOK, "questions": final})["status"] == "ready"
    assert any(t.name == "popquiz-server" for t in threading.enumerate())
    app.shutdown()
    assert _wait(lambda: not any(t.name == "popquiz-server" and t.ident not in before
                                 for t in threading.enumerate()))
    assert app.status()["state"] == "idle"
