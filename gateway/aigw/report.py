"""Cost attribution from the audit log: spend, tokens, and tool calls per team (ADR-0003).

    python -m aigw.report audit.log [more.log ...]
    kubectl logs deploy/aigw | python -m aigw.report
    python -m aigw.report --json audit.log

Lines that are not audit JSON (server logs mixed into the same stream) are skipped.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from collections.abc import Iterable
from typing import Any, TextIO


def _blank() -> dict[str, Any]:
    return {"requests": 0, "denied": 0, "errors": 0, "prompt_tokens": 0,
            "completion_tokens": 0, "cost_usd": 0.0}


def summarize(lines: Iterable[str]) -> dict[str, Any]:
    llm: dict[tuple[str, str], dict[str, Any]] = defaultdict(_blank)
    tools: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: {"calls": 0, "denied": 0})
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        team = event.get("team", "?")
        if event.get("event") == "chat_completion":
            row = llm[(team, event.get("model", "?"))]
            row["requests"] += 1
            row["denied"] += event.get("status") == "denied"
            row["errors"] += event.get("status") == "error"
            row["prompt_tokens"] += event.get("prompt_tokens") or 0
            row["completion_tokens"] += event.get("completion_tokens") or 0
            row["cost_usd"] += event.get("cost_usd") or 0.0
        elif event.get("event") == "mcp" and event.get("method") == "tools/call":
            row = tools[(team, f"{event.get('server')}/{event.get('tool')}")]
            row["calls"] += 1
            row["denied"] += event.get("status") == "denied"

    teams: dict[str, dict[str, Any]] = defaultdict(lambda: {"cost_usd": 0.0, "models": {},
                                                            "tools": {}})
    for (team, model), row in sorted(llm.items()):
        row["cost_usd"] = round(row["cost_usd"], 6)
        teams[team]["models"][model] = row
        teams[team]["cost_usd"] = round(teams[team]["cost_usd"] + row["cost_usd"], 6)
    for (team, tool), row in sorted(tools.items()):
        teams[team]["tools"][tool] = row
    return {"total_cost_usd": round(sum(t["cost_usd"] for t in teams.values()), 6),
            "teams": dict(sorted(teams.items(), key=lambda kv: -kv[1]["cost_usd"]))}


def render(summary: dict[str, Any], out: TextIO) -> None:
    out.write(f"Total spend: ${summary['total_cost_usd']:.6f}\n")
    for team, data in summary["teams"].items():
        out.write(f"\n{team}  ${data['cost_usd']:.6f}\n")
        if data["models"]:
            out.write(f"  {'model':<22}{'requests':>9}{'denied':>8}{'errors':>8}"
                      f"{'in tokens':>12}{'out tokens':>12}{'cost USD':>13}\n")
        for model, r in data["models"].items():
            out.write(f"  {model:<22}{r['requests']:>9}{r['denied']:>8}{r['errors']:>8}"
                      f"{r['prompt_tokens']:>12}{r['completion_tokens']:>12}"
                      f"{r['cost_usd']:>13.6f}\n")
        if data["tools"]:
            out.write(f"  {'mcp tool':<22}{'calls':>9}{'denied':>8}\n")
        for tool, r in data["tools"].items():
            out.write(f"  {tool:<22}{r['calls']:>9}{r['denied']:>8}\n")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="*", help="audit log files (default: stdin)")
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = parser.parse_args(argv)

    def lines() -> Iterable[str]:
        if not args.files:
            yield from sys.stdin
        for path in args.files:
            with open(path) as fh:
                yield from fh

    summary = summarize(lines())
    if args.json:
        json.dump(summary, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        render(summary, sys.stdout)


if __name__ == "__main__":
    main()
