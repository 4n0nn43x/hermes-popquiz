"""Pop Quiz: the room vs the models. Hermes Agent plugin entry point (SPEC section 9).

register(ctx) wires 4 tools, the /popquiz slash command, the `hermes popquiz` CLI, the bundled
host-a-quiz skill and an unload hook that stops the game server. Nothing is read from the settings
here: every setting is read when a tool or command runs.
"""

from __future__ import annotations

from pathlib import Path

from .hermes_glue import PopQuizApp, tool_json

TOOLSET = "popquiz"
SKILL = "host-a-quiz"
SKILL_DESCRIPTION = "Use when asked for a quiz on a document. Runs Pop Quiz."

_QUESTION_SCHEMA = {
    "type": "object",
    "required": ["prompt", "choices", "answer_index", "source_quote"],
    "properties": {
        "prompt": {"type": "string", "maxLength": 240,
                   "description": "The question, answerable from the document alone. Must not contain the answer."},
        "choices": {"type": "array", "minItems": 2, "maxItems": 4,
                    "items": {"type": "string", "maxLength": 90},
                    "description": "2 to 4 distinct, plausible options. Exactly one is correct."},
        "answer_index": {"type": "integer", "minimum": 0, "maximum": 3,
                         "description": "0-based index of the correct choice."},
        "source_quote": {"type": "string", "minLength": 12, "maxLength": 300,
                         "description": ("A passage copied WORD FOR WORD from the document that proves the answer "
                                         "(12 to 300 chars). Never paraphrase, never change a number.")},
        "explanation": {"type": "string", "maxLength": 300,
                        "description": "One sentence shown after the reveal."},
    },
}

CREATE_SCHEMA = {
    "name": "popquiz_create",
    "description": (
        "Create a live multiplayer quiz from a document: people join from their phones and AI models play the "
        "same questions. Read the document first, then write 5 to 12 multiple-choice questions. Every question "
        "MUST include source_quote copied word for word from the document (exact wording and numbers); the tool "
        "checks each quote against the document and refuses the ones it cannot find. On status needs_fix, fix "
        "ONLY the rejected questions and call again with the same draft_id and only the fixed questions. On "
        "status ready, give the user the screen and join URLs."),
    "parameters": {
        "type": "object",
        "required": ["questions"],
        "properties": {
            "doc_path": {"type": "string",
                         "description": ("Path of the document (txt, md, html, pdf, docx, epub): a file the user sent "
                                         "(the path given in the conversation) or a file under the current folder. "
                                         "Required on the first call.")},
            "questions": {"type": "array", "minItems": 1, "maxItems": 20, "items": _QUESTION_SCHEMA,
                          "description": "First call: all questions. Retry with draft_id: only the fixed ones."},
            "title": {"type": "string", "maxLength": 120, "description": "Optional quiz title."},
            "mode": {"type": "string", "enum": ["open_book", "closed_book"],
                     "description": ("open_book (default): the models see the document. closed_book: models see "
                                     "only the question.")},
            "draft_id": {"type": "string", "description": "Draft id returned by a needs_fix answer."},
        },
    },
}

CONTROL_SCHEMA = {
    "name": "popquiz_control",
    "description": ("Drive the running quiz like the host: open_lan (let phones on the local network join), start, "
                    "next (next question), skip (close the current question now), end."),
    "parameters": {
        "type": "object",
        "required": ["action"],
        "properties": {"action": {"type": "string", "enum": ["open_lan", "start", "next", "skip", "end"]}},
    },
}

STATUS_SCHEMA = {
    "name": "popquiz_status",
    "description": "State of the running quiz: room code, state, question, players, model spend, public URLs.",
    "parameters": {"type": "object", "properties": {}},
}

REPORT_SCHEMA = {
    "name": "popquiz_report",
    "description": ("Markdown report of the finished quiz: what the room misunderstood (with the document quotes) "
                    "and which model understood the document best, for what cost. Use it to propose remediation."),
    "parameters": {"type": "object", "properties": {}},
}

PROVIDED_TOOLS = ("popquiz_create", "popquiz_control", "popquiz_status", "popquiz_report")

# The live app of the last register() call (tests and the smoke script read it; Hermes never needs it).
_APP: dict = {}


def _check_runtime() -> bool:
    try:
        import aiohttp  # noqa: F401
        return True
    except ImportError:
        return False


def register(ctx) -> None:
    app = PopQuizApp(ctx)
    _APP["app"] = app

    ctx.register_tool(name="popquiz_create", toolset=TOOLSET, schema=CREATE_SCHEMA, check_fn=_check_runtime,
                      handler=lambda args, **_kw: tool_json(lambda: app.create(args)),
                      description=CREATE_SCHEMA["description"], emoji="🎯")
    ctx.register_tool(name="popquiz_control", toolset=TOOLSET, schema=CONTROL_SCHEMA, check_fn=_check_runtime,
                      handler=lambda args, **_kw: tool_json(lambda: app.control((args or {}).get("action", ""))),
                      description=CONTROL_SCHEMA["description"], emoji="🎯")
    ctx.register_tool(name="popquiz_status", toolset=TOOLSET, schema=STATUS_SCHEMA, check_fn=_check_runtime,
                      handler=lambda args, **_kw: tool_json(app.status),
                      description=STATUS_SCHEMA["description"], emoji="🎯")
    ctx.register_tool(name="popquiz_report", toolset=TOOLSET, schema=REPORT_SCHEMA, check_fn=_check_runtime,
                      handler=lambda args, **_kw: tool_json(app.report),
                      description=REPORT_SCHEMA["description"], emoji="🎯")

    def popquiz_command(raw_args: str = "") -> str:
        return app.command(raw_args)

    ctx.register_command("popquiz", popquiz_command, description="Host the Pop Quiz: open, start, next, end",
                         args_hint="[open|start|next|skip|end|status|report]")

    from . import cli
    ctx.register_cli_command(name="popquiz", help="Pop Quiz: demo, load bench, server",
                             setup_fn=cli.setup_parser, handler_fn=lambda args: cli.run(args, ctx),
                             description="Pop Quiz: the room vs the models (the demo makes no model call)")

    ctx.register_skill(name=SKILL, path=Path(__file__).resolve().parent / "skills" / SKILL / "SKILL.md",
                       description=SKILL_DESCRIPTION)
    ctx.on_unload(app.shutdown)


__all__ = ["register", "PROVIDED_TOOLS"]
