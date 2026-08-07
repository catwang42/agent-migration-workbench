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
import os
import sys

# Importing the adapter package must not need credentials or provider SDKs —
# the lanes' tests assert that — so MODES/CLAUDE_PATHS are taken from the one
# place that defines them rather than restated here.
from amw.adapters import CLAUDE_PATHS, MODES
from amw.config import ConfigError, load_all

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
    subparsers.choices["ablate"].add_argument("--subagent", default=None)

    smoke = subparsers.choices["smoke"]
    smoke.add_argument("-n", type=int, default=2)
    smoke.add_argument(
        "--backend",
        choices=("gemini", "claude"),
        default=None,
        help="check one backend only (default: both)",
    )
    smoke.add_argument(
        "--claude-path",
        choices=CLAUDE_PATHS,
        default=None,
        help="override $CLAUDE_PATH for this check (default: $CLAUDE_PATH)",
    )

    return parser


def region_warnings(cfg, mode: str) -> list[str]:
    """Flag region settings that would make a report footer misstate the run.

    The footer prints ``cfg.customer.region`` (ground rule 2), but live calls go
    wherever ``$REGION`` / ``$CLAUDE_REGION`` point. If those disagree the
    report claims a region the numbers did not come from — and a cross-region
    Claude/Gemini split additionally makes the ``latency_p95`` gate a
    cross-region comparison, which has to be disclosed rather than silently
    compared. Warn loudly; do not "fix" it by overriding either value, since
    which one is wrong is the operator's call.
    """
    if mode == "replay":
        return []  # nothing is dialled anywhere; the corpus carries its own labels

    profile = cfg.customer.region
    env_region = os.environ.get("REGION")
    claude_region = os.environ.get("CLAUDE_REGION") or env_region

    warnings: list[str] = []
    if env_region and env_region != profile:
        warnings.append(
            f"REGION={env_region} but config/customers/{cfg.customer.customer}.yaml "
            f"says region: {profile}. Gemini calls will run in {env_region} while "
            f"report footers print {profile}. Align them before a customer run."
        )
    if claude_region and env_region and claude_region != env_region:
        warnings.append(
            f"Claude runs in {claude_region}, Gemini in {env_region}. Quality and "
            f"cost gates are unaffected, but latency_p95 becomes a cross-region "
            f"comparison — the scorecard must disclose this, not present it as a "
            f"same-region measurement."
        )
    return warnings


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 1

    try:
        cfg = load_all(customer=args.customer)
    except ConfigError as exc:
        print(f"config error:\n{exc}", file=sys.stderr)
        return 2

    for warning in region_warnings(cfg, args.mode):
        print(f"warning: {warning}", file=sys.stderr)

    print(
        f"`{args.command}` is not implemented yet — it lands in {args.task}. "
        "Nothing was run.",
        file=sys.stderr,
    )
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
