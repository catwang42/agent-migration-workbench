"""The tuned_v2 selection table — every rung, per subagent, at its actual n.

Tuesday plan item 7. The owner asked for one table per subagent carrying every
rung with its judged score, CI and deterministic metrics, the optimizer rung
sitting beside the hand-tuned novelty rung, and an explicit answer to one
question: *does A4-optimizer still clear the incumbent at n=70?*

Two comparators, and conflating them is the trap
------------------------------------------------

The ruling quotes the incumbent as **0.903**. That is the Feature Extractor
Claude baseline on the **28-item core split** — the ladder's own instrument. The
full-70 Claude baseline, from ``phase2_n70_widened.json``, is **0.8996**. They
are two measurements of two different samples and the difference is not noise
to be rounded away: a rung measured at n=70 that is compared against 0.903 is
being scored against a number from a different corpus.

So this script never compares across splits. Every row is compared to the
incumbent **at its own split**, the incumbent's split is printed beside it, and
a rung whose split has no incumbent gets no comparison at all rather than a
borrowed one.

What the comparison is, and what it is not
------------------------------------------

It is unpaired: rung CI lower bound against the incumbent's point estimate on
the same split. That is a weaker instrument than the ``quality_delta_pp`` gate,
which is a **paired** bootstrap over per-item differences and is the thing the
verdict is actually decided on. This table informs the selection; it does not
make it, and it does not restate the gate.

Three verdict words, and they are deliberately not "win"/"lose":

``clears``
    The rung's entire interval sits above the incumbent's point estimate.
``recovery to parity``
    The interval contains the incumbent's point estimate. The rung closed the
    gap; the data does not establish that it went past it. The owner's
    instruction, verbatim: "if the CI lower bound crosses 0.903, say so plainly
    and we report it as recovery-to-parity rather than a win."
``below incumbent``
    The entire interval sits under the incumbent's point estimate.

    .venv/bin/python scripts/selection_table.py [--out PATH]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from amw.eval.runner import Phase2Result  # noqa: E402
from amw.reporting.cells import EM_DASH, estimate_text  # noqa: E402
from amw.reporting.ladder import CONTAMINATION_MARKER, build_ladder  # noqa: E402
from amw.tuning.ablate import AblationResult  # noqa: E402

RESULTS = REPO_ROOT / "artifacts" / "results"

#: The gated run the scorecard rows come from. Its arms enter the table as
#: rows in their own right, labelled, because the incumbent at ``split=all``
#: exists only here — the ladder never re-ran ``baseline`` at n=70.
GATED_ARTIFACT = "phase2_n70_widened.json"

SUBAGENT_ORDER = ("query_rewriter", "chunk_summarizer", "feature_extractor")

BASELINE_VARIANT = "claude_baseline"

CLEARS = "clears"
PARITY = "recovery to parity"
BELOW = "below incumbent"

UNPAIRED_CAVEAT = (
    "Comparison column: the rung's 95% CI lower bound against the **incumbent's "
    "point estimate on the same split**. It is *unpaired* and it is not the "
    "`quality_delta_pp` gate, which is a paired bootstrap over per-item "
    "differences and is what the verdict is decided on. This table informs the "
    "selection; it does not make it."
)

SPLIT_CAVEAT = (
    "Rows are grouped by split because the two splits have different "
    "incumbents: Feature Extractor's Claude baseline is **0.903** on the core "
    "28 and **0.900** on the full 70. A rung measured at n=70 compared against "
    "0.903 is being scored against a different corpus."
)


def _judge(arm) -> object | None:
    return getattr(arm, "judge", None) if arm is not None else None


def _rows_from_ladder(subagent: str) -> list[dict]:
    path = RESULTS / f"ablation_{subagent}.json"
    if not path.is_file():
        return []
    result = AblationResult.model_validate_json(path.read_text(encoding="utf-8"))
    rows = []
    for row in build_ladder(result).rows:
        rows.append(
            {
                "name": row.rung,
                "variant": row.variant,
                "output_mode": row.output_mode,
                "source": "ladder",
                "split": row.split,
                "judge": row.judge,
                "metrics": row.metrics,
                "leaked": row.leaked_example_items,
                "status": row.status,
                "unmeasured_reason": row.unmeasured_reason,
            }
        )
    return rows


def _rows_from_gated(subagent: str) -> list[dict]:
    path = RESULTS / GATED_ARTIFACT
    if not path.is_file():
        return []
    phase2 = Phase2Result.model_validate_json(path.read_text(encoding="utf-8"))
    rows = []
    for arm in phase2.arms:
        if arm.subagent != subagent:
            continue
        judge = _judge(arm)
        rows.append(
            {
                "name": f"gated:{arm.variant}",
                "variant": arm.variant,
                "output_mode": arm.output_mode,
                "source": "gated run",
                "split": getattr(judge, "split", None),
                "judge": judge,
                "metrics": {
                    name: estimate_text(report.estimate, point=report.point)
                    for name, report in arm.metrics.items()
                },
                "leaked": [],
                "status": "measured" if judge else "no_judge",
                "unmeasured_reason": None,
            }
        )
    return rows


def _score_text(row: dict) -> str:
    judge = row["judge"]
    if judge is None:
        return f"not measured — {row.get('unmeasured_reason') or row['status']}"
    # Ladder rows carry a JudgeScoreCell; gated rows carry a JudgeReport.
    if hasattr(judge, "render"):
        text = judge.render()
    else:
        text = (
            f"{estimate_text(judge.estimate, point=judge.point)} "
            f"(judged n={judge.items_scored}, split={judge.split})"
        )
    return f"{text} {CONTAMINATION_MARKER}" if row["leaked"] else text


def _interval(row: dict) -> tuple[float, float] | None:
    judge = row["judge"]
    estimate = getattr(judge, "estimate", None)
    if estimate is None:
        return None
    return estimate.lo, estimate.hi


def _compare(row: dict, incumbent_point: float | None) -> str:
    """The rung against the incumbent on its own split — or an honest blank."""
    if incumbent_point is None:
        return f"{EM_DASH} (no incumbent measured on this split)"
    if row["variant"] == BASELINE_VARIANT:
        return "incumbent"
    interval = _interval(row)
    if interval is None:
        return EM_DASH
    lo, hi = interval
    if lo > incumbent_point:
        return f"**{CLEARS}** (lo {lo:.3f} > {incumbent_point:.3f})"
    if hi < incumbent_point:
        return f"{BELOW} (hi {hi:.3f} < {incumbent_point:.3f})"
    return f"**{PARITY}** (lo {lo:.3f} <= {incumbent_point:.3f} <= hi {hi:.3f})"


def _incumbent_point(rows: list[dict]) -> float | None:
    for row in rows:
        if row["variant"] == BASELINE_VARIANT and row["judge"] is not None:
            return getattr(row["judge"], "point", None)
    return None


def _split_label(split: str | None) -> str:
    return {"core": "core 28", "all": "full 70"}.get(split or "", split or "unmeasured")


def render(subagents: tuple[str, ...] = SUBAGENT_ORDER) -> list[str]:
    lines = [
        "# tuned_v2 selection table",
        "",
        "Every rung that was run, per subagent, at the n it was actually run on.",
        "",
        SPLIT_CAVEAT,
        "",
        UNPAIRED_CAVEAT,
        "",
    ]
    for subagent in subagents:
        rows = _rows_from_ladder(subagent) + _rows_from_gated(subagent)
        if not rows:
            continue
        lines += [f"## {subagent}", ""]

        splits: list[str | None] = []
        for row in rows:
            if row["split"] not in splits:
                splits.append(row["split"])
        # Unmeasured rows last, under their own heading.
        for split in sorted(splits, key=lambda s: (s is None, s or "")):
            group = [r for r in rows if r["split"] == split]
            incumbent = _incumbent_point(group)
            heading = (
                f"### split = {_split_label(split)}"
                if split
                else "### no measurement"
            )
            lines += [heading, ""]

            columns: list[str] = []
            for row in group:
                for name in row["metrics"]:
                    if name not in columns:
                        columns.append(name)
            header = ["Rung / arm", "Source", "Mode", "Judged score (95% CI)"]
            header += [f"`{c}`" for c in columns]
            header += ["vs incumbent"]
            lines += [
                "| " + " | ".join(header) + " |",
                "| " + " | ".join("---" for _ in header) + " |",
            ]
            for row in group:
                cells = [
                    f"`{row['name']}`",
                    row["source"],
                    f"`{row['output_mode']}`",
                    _score_text(row),
                ]
                cells += [row["metrics"].get(c, EM_DASH) for c in columns]
                cells.append(_compare(row, incumbent))
                lines.append("| " + " | ".join(cells) + " |")
            lines.append("")

        contaminated = [r for r in rows if r["leaked"]]
        for row in contaminated:
            items = ", ".join(f"`{i}`" for i in row["leaked"])
            lines += [
                f"{CONTAMINATION_MARKER} `{row['name']}` quotes {items} as a "
                f"worked example and those items are inside the split it was "
                f"scored on. Its judged score is optimistic there by "
                f"construction; the items are not excluded, because that would "
                f"give this rung a different denominator from every other rung.",
                "",
            ]
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=None, help="write markdown here")
    args = parser.parse_args()

    text = "\n".join(render()) + "\n"
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"selection table written to {path}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
