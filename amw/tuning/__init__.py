"""amw.tuning — the ablation ladder and the Claude-XML → Gemini translator.

Two things live here, and they are two halves of the same workshop segment:

* :mod:`amw.tuning.translator` answers "what would you change?" with a
  mechanical, model-free rewrite plus a side-by-side page to show it.
* :mod:`amw.tuning.ablate` answers "did it help?" by running each rung through
  the *same* scoring path ``cli.py phase2`` uses, so the answer is comparable
  to the baseline it is read against.

:func:`cmd_ablate` is the CLI seam. It takes the same ``(args, cfg)`` shape as
``cli.py::cmd_phase2`` and reads its optional arguments defensively, so wiring
the extra argparse flags is additive rather than a prerequisite.

Imports are deferred into the function bodies for the reason ``cli.py`` defers
its own: importing a CLI seam must not drag the eval stack (and its config
load) into a process that only wanted to check a flag.
"""

from __future__ import annotations

import sys
from typing import Any

__all__ = ["cmd_ablate"]


def cmd_ablate(args: Any, cfg: Any) -> int:
    """``python cli.py ablate --subagent feature_extractor --mode replay``.

    Runs the ladder for one subagent (or every evaluated subagent when
    ``--subagent`` is omitted) and appends the rung records to
    ``artifacts/results/ablation_{subagent}.json``.

    Exit codes follow ``cmd_e2e``: 0 on success, 5 when a rung that *did* run
    hit call errors. A rung with no recordings is **not** an error — it is a
    rung nobody has measured yet, it is printed as such, and it is written to
    the artifact with no numbers on it.
    """
    from amw.agents.schemas import SUBAGENTS
    from amw.tuning.ablate import error_kinds, format_rung, run_ladder

    subagents = [args.subagent] if getattr(args, "subagent", None) else list(SUBAGENTS)
    exit_code = 0

    out_path = getattr(args, "out", None)
    if out_path and len(subagents) > 1:
        # One artifact per subagent is the shape; a single --out across three
        # ladders would either clobber or fail halfway through.
        print(
            "--out names one artifact, so it needs --subagent too.",
            file=sys.stderr,
        )
        return 2

    for subagent in subagents:
        if subagent not in SUBAGENTS:
            print(
                f"unknown subagent {subagent!r}; expected one of {list(SUBAGENTS)}",
                file=sys.stderr,
            )
            return 2

        result = run_ladder(
            subagent,
            mode=args.mode,
            config=cfg,
            n=getattr(args, "n", None),
            rungs=getattr(args, "rung", None),
            dataset_dir=getattr(args, "dataset_dir", None),
            out_path=out_path,
            append=not getattr(args, "no_append", False),
            run_judge=not getattr(args, "no_judge", False),
        )

        print(f"\n=== ablation ladder: {subagent} ===")
        for record in result.rungs:
            for line in format_rung(record):
                print(line)
        for note in result.notes:
            print(f"note: {note}", file=sys.stderr)

        # Ground rule 1: a replayed number says on screen when it was recorded.
        windows = [
            (record.provenance.recorded_from, record.provenance.recorded_to)
            for record in result.rungs
            if record.provenance.recorded_from
        ]
        if windows:
            print(
                f"REPLAY — measured rungs above come from calls recorded "
                f"{min(w[0] for w in windows)} to {max(w[1] for w in windows)}, "
                f"not from a run just now."
            )

        errors = error_kinds(result.rungs)
        if errors:
            for kind, count in sorted(errors.items()):
                print(f"  {subagent}: {kind} x{count}", file=sys.stderr)
            exit_code = 5

    return exit_code
