#!/usr/bin/env python3
"""Agent Migration Workbench CLI.

    python cli.py gen --customer demo_patents -n 70
    python cli.py phase2 --mode hybrid -n 10
    python cli.py ablate --subagent query_rewriter
    python cli.py shadow --mode hybrid
    python cli.py scorecard
    python cli.py e2e --mode replay
    python cli.py smoke --mode live -n 2

Subcommands are wired up as their tasks land (T09 owns the phase runners and
the e2e pipeline). Until then an unimplemented subcommand exits non-zero
saying which task will deliver it — it never pretends to have run.
"""

from __future__ import annotations

import argparse
import sys

from amw.config import ConfigError, load_all

MODES = ("live", "replay", "hybrid")

# subcommand -> (help text, task that implements it)
COMMANDS: dict[str, tuple[str, str]] = {
    "gen": ("generate the synthetic dataset + rubrics", "T06"),
    "phase2": ("baseline eval: Claude vs naive Gemini", "T09"),
    "ablate": ("run the A0-A4 prompt ablation ladder", "T10"),
    "shadow": ("shadow run + disagreement triage", "T11"),
    "scorecard": ("gates -> verdicts -> markdown report", "T12"),
    "e2e": ("full offline pipeline (CI gate)", "T09"),
    "smoke": ("pre-demo health check against live backends", "T16"),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cli.py", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    for name, (help_text, task) in COMMANDS.items():
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument(
            "--mode",
            choices=MODES,
            default="replay",
            help="execution mode (default: replay, the zero-credential path)",
        )
        sub.add_argument(
            "--customer",
            default=None,
            help="customer profile under config/customers/ (default: $AMW_CUSTOMER)",
        )
        sub.set_defaults(task=task)

    subparsers.choices["gen"].add_argument("-n", type=int, default=None)
    subparsers.choices["phase2"].add_argument("-n", type=int, default=None)
    subparsers.choices["smoke"].add_argument("-n", type=int, default=2)
    subparsers.choices["ablate"].add_argument("--subagent", default=None)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 1

    try:
        load_all(customer=args.customer)
    except ConfigError as exc:
        print(f"config error:\n{exc}", file=sys.stderr)
        return 2

    print(
        f"`{args.command}` is not implemented yet — it lands in {args.task}. "
        "Nothing was run.",
        file=sys.stderr,
    )
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
