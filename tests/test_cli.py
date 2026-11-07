"""CLI smoke tests: `hermes popquiz demo` end to end (refused then corrected, cassette models, robot
phones, report) with the timings shrunk, and `bench`. Outside Hermes (ctx=None), no model call."""

from __future__ import annotations

import argparse
import json
import threading

from popquiz_plugin import cli, robots
from popquiz_plugin.server import QuizServer


def parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="hermes popquiz")
    cli.setup_parser(parser)
    return parser.parse_args(argv)


def test_demo_runs_a_whole_game_and_prints_the_report(monkeypatch):
    orig_create, orig_robots = QuizServer.create_game, robots.run_robots

    def fast_create(self, **kw):
        kw["reveal_s"] = 0.1
        info = orig_create(self, **kw)
        self._conductor.auto_advance_s = 0.1
        return info

    async def fast_robots(*a, **kw):
        kw["think"] = (0.05, 0.2)
        return await orig_robots(*a, **kw)

    monkeypatch.setattr(QuizServer, "create_game", fast_create)
    monkeypatch.setattr(robots, "run_robots", fast_robots)
    lines: list[str] = []
    before = {t.ident for t in threading.enumerate()}
    args = parse(["demo", "--robots", "4", "--port", "0", "--questions", "3", "--lobby", "0", "--speed", "0", "--exit"])
    assert cli.run(args, None, lines.append) == 0
    text = "\n".join(lines)
    assert "REFUSED q2 [number_mismatch]" in text and "REFUSED q3 [quote_not_found]" in text
    assert "accepted 1, refused 2 (attempt 1/3)" in text
    assert "ready: room" in text and "players: Claude Opus 5.5, DeepSeek V4 Flash, Mercury 3" in text
    assert "4 players in the lobby" in text
    assert text.count("  question ") == 3 and text.count("  reveal   ") == 3
    assert "Step 4. Game over." in text
    assert text.count("    Claude Opus 5.5 ") == 3  # one line per model at each reveal
    robots_line = next(line for line in lines if line.startswith("Robots: "))
    stats = json.loads(robots_line[len("Robots: "):])
    assert stats["joined"] == 4 and stats["answers"] == 12 and not stats["errors"]
    assert not any(t.name == "popquiz-server" and t.ident not in before for t in threading.enumerate())


def test_bench_prints_percentiles():
    lines: list[str] = []
    assert cli.run(parse(["bench", "--players", "5", "--questions", "1"]), None, lines.append) == 0
    out = json.loads(lines[0])
    assert out["answers"] == out["expected"] == 5
    assert set(out["question_to_answer_ms"]) == {"p50", "p95", "max"}


def test_live_demo_needs_hermes():
    lines: list[str] = []
    assert cli.run(parse(["demo", "--live"]), None, lines.append) == 2
    assert "needs Hermes" in lines[0]


def test_bot_line_shows_choice_latency_cost_and_errors():
    line = cli.bot_line({"label": "Mercury 3", "choice": 1, "correct": True, "latency_ms": 840, "cost_usd": 0.00012})
    assert "Mercury 3" in line and " B right" in line and "0.84 s" in line and "$0.00012" in line
    line = cli.bot_line({"label": "Opus", "choice": None, "correct": False, "latency_ms": 21000, "cost_usd": 0,
                         "error": "timeout"})
    assert " - wrong" in line and "[timeout]" in line
    assert "still thinking" in cli.bot_line({"label": "Opus", "pending": True})


def test_no_subcommand_prints_usage():
    lines: list[str] = []
    assert cli.run(parse([]), None, lines.append) == 2
    assert lines[0].startswith("usage: hermes popquiz")
