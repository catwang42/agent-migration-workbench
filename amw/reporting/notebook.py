"""The display layer the two notebooks import.

Ground rule 8 says notebooks hold no logic: they load an artifact, call one of
these functions, and display what comes back. Everything a cell would otherwise
have to do inline — reaching into nested pydantic models, deciding what an
unmeasured cell looks like, keeping rung order stable, drawing an error bar —
lives here, where it is importable, testable, and reviewable in a diff.

Three rules this module holds to, because it is the last thing between a
recorded call and a customer's eyes:

**Unmeasured is never zero.** Every frame that can contain a gap carries a
``status`` column and puts ``NaN`` (which pandas renders as ``NaN``, and
matplotlib refuses to plot) in the value column. A bar of height zero and a bar
that was never measured must not look alike.

**Intervals travel with points.** The frames carry ``lo``/``hi`` beside every
``point``, and :func:`interval_chart` draws them. A point estimate on its own is
the thing ground rule 7 exists to prevent.

**Provenance is printable.** :func:`replay_banner` turns any of the three
artifacts into the on-screen recording-date label ground rule 1 requires, so a
notebook can open with it rather than hoping the reader remembers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

import pandas as pd

from amw.eval.runner import Phase2Result
from amw.eval.stats import Estimate

__all__ = [
    "ARTIFACTS",
    "LADDER_COLUMNS",
    "LATENCY_COLUMNS",
    "METRIC_COLUMNS",
    "NOT_MEASURED",
    "TRIAGE_COLUMNS",
    "agreement_frame",
    "ablation_frame",
    "interval_chart",
    "judge_frame",
    "latency_frame",
    "load_ablation",
    "load_phase2",
    "load_shadow_result",
    "metrics_frame",
    "replay_banner",
    "triage_frame",
]

#: Where `cli.py` writes. Notebooks take no arguments and read from here.
ARTIFACTS = Path(__file__).resolve().parents[2] / "artifacts" / "results"

#: The string every frame uses for "there is no number here". Spelled out
#: rather than left blank so a reader scanning a table cannot mistake a gap for
#: a value they missed.
NOT_MEASURED = "not measured"

# Column subsets the notebooks display. They live here rather than inline in a
# cell because they are the editorial decision — which columns a customer sees
# — and that decision belongs somewhere reviewable, not in notebook JSON. Every
# one of them keeps the qualifier next to the number: judged_n beside a judge
# score, rung_status beside a ladder value, disclosure beside a latency.
METRIC_COLUMNS = ["subagent", "variant", "model", "metric", "point", "lo", "hi", "n"]
LADDER_COLUMNS = [
    "rung",
    "variant",
    "output_mode",
    "branches_from",
    "metric",
    "point",
    "lo",
    "hi",
    "judged_n",
    "rung_status",
]
TRIAGE_COLUMNS = [
    "item_id",
    "verdict",
    "fields",
    "baseline_score",
    "candidate_score",
    "rationale",
    "reason",
]
LATENCY_COLUMNS = [
    "subagent",
    "side",
    "model",
    "region",
    "n_traces",
    "total_p50_ms",
    "total_p95_ms",
]


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------


def load_phase2(path: str | Path | None = None) -> Phase2Result:
    """The phase-2 artifact, preferring the full run over the dev subset."""
    from amw.reporting.scorecard import PHASE2_ARTIFACTS

    if path is None:
        for name in PHASE2_ARTIFACTS:
            if (ARTIFACTS / name).is_file():
                path = ARTIFACTS / name
                break
        else:
            raise FileNotFoundError(
                f"no phase-2 artifact in {ARTIFACTS}. Run `python cli.py phase2` "
                f"— the notebook displays a run, it does not perform one."
            )
    return Phase2Result.model_validate_json(Path(path).read_text(encoding="utf-8"))


def load_ablation(subagent: str, path: str | Path | None = None):
    """The ladder artifact for one subagent."""
    from amw.tuning.ablate import AblationResult

    path = Path(path) if path else ARTIFACTS / f"ablation_{subagent}.json"
    if not path.is_file():
        raise FileNotFoundError(
            f"no ablation artifact at {path}. Run "
            f"`python cli.py ablate --subagent {subagent} --mode replay`."
        )
    return AblationResult.model_validate_json(path.read_text(encoding="utf-8"))


def load_shadow_result(path: str | Path | None = None):
    """The shadow artifact, agreement and triage included."""
    from amw.shadow import ShadowResult

    path = Path(path) if path else ARTIFACTS / "shadow.json"
    if not path.is_file():
        raise FileNotFoundError(
            f"no shadow artifact at {path}. Run `python cli.py shadow --mode replay`."
        )
    return ShadowResult.model_validate_json(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# provenance
# --------------------------------------------------------------------------


def replay_banner(artifact: Any) -> str:
    """The recording-date label ground rule 1 requires, for any artifact.

    Reads ``mode``/``recorded_from``/``recorded_to`` off whatever it is handed,
    so it works for phase-2 and shadow results alike. An artifact that carries
    no window says so rather than implying a fresh run.
    """
    mode = getattr(artifact, "mode", None) or "unknown"
    start = getattr(artifact, "recorded_from", None)
    end = getattr(artifact, "recorded_to", None)
    if mode == "live":
        started = getattr(artifact, "run_started", None)
        return f"LIVE — calls made {started}." if started else "LIVE run."
    if not start or not end:
        return (
            f"{mode.upper()} — this artifact records no call window, so the age "
            f"of the numbers below cannot be stated."
        )
    return (
        f"{mode.upper()} — every number below comes from model calls recorded "
        f"{start} to {end}, not from a run just now."
    )


# --------------------------------------------------------------------------
# frames
# --------------------------------------------------------------------------


def _row(estimate: Estimate | None) -> dict[str, Any]:
    """point / lo / hi / n from an estimate, or a labelled hole."""
    if estimate is None:
        return {
            "point": float("nan"),
            "lo": float("nan"),
            "hi": float("nan"),
            "n": None,
            "status": NOT_MEASURED,
        }
    return {
        "point": estimate.point,
        "lo": estimate.lo,
        "hi": estimate.hi,
        "n": estimate.n,
        "status": "measured",
    }


def metrics_frame(
    phase2: Phase2Result, *, metrics: Sequence[str] | None = None
) -> pd.DataFrame:
    """One row per (subagent, variant, metric), with the interval attached.

    ``metrics=`` filters and, more usefully, *orders*: pass the metrics you are
    about to chart and the frame comes back in that order rather than in the
    order the runner happened to compute them.
    """
    rows: list[dict[str, Any]] = []
    for arm in phase2.arms:
        for name, report in arm.metrics.items():
            if metrics is not None and name not in metrics:
                continue
            rows.append(
                {
                    "subagent": arm.subagent,
                    "variant": arm.variant,
                    "model": arm.model,
                    "metric": name,
                    **_row(report.estimate),
                    "n_excluded": report.n_excluded,
                }
            )
    frame = pd.DataFrame(rows)
    if metrics is not None and not frame.empty:
        order = {name: i for i, name in enumerate(metrics)}
        frame = frame.sort_values(
            ["subagent", "metric", "variant"],
            key=lambda col: col.map(order) if col.name == "metric" else col,
        ).reset_index(drop=True)
    return frame


def judge_frame(phase2: Phase2Result) -> pd.DataFrame:
    """Judge scores with the split and judged-n they were measured on.

    ``judged_n`` and ``split`` are columns, not a footnote: Feature Extractor
    was judged on 70 items and the other two on the 28-item core, so a judge
    column read without them compares two different measurements.
    """
    rows: list[dict[str, Any]] = []
    for arm in phase2.arms:
        judge = arm.judge
        row = {
            "subagent": arm.subagent,
            "variant": arm.variant,
            "model": arm.model,
            **_row(judge.estimate if judge else None),
        }
        row["judged_n"] = judge.items_scored if judge else None
        row["split"] = judge.split if judge else NOT_MEASURED
        row["repeats"] = judge.expected_repeats if judge else None
        row["full_agreement_rate"] = judge.full_agreement_rate if judge else None
        rows.append(row)
    return pd.DataFrame(rows)


def ablation_frame(result: Any, *, metric: str | None = None) -> pd.DataFrame:
    """The ladder, in rung order, unmeasured rungs included.

    A rung nobody has run is a row with ``status='no_recordings'``, its reason,
    and ``NaN`` in the value columns — not a missing row. Dropping it would turn
    "we have not tested this yet" into "this rung does not exist", which is the
    difference between an honest ladder and a flattering one.
    """
    rows: list[dict[str, Any]] = []
    for record in result.rungs:
        arm = record.arm
        judge = arm.judge if arm else None
        chosen = metric
        if chosen is None and arm is not None:
            # Default to the judge score where there is one, since that is the
            # ladder's headline; fall back to the arm's first metric.
            chosen = "judge_score" if judge else next(iter(arm.metrics), None)
        if chosen == "judge_score":
            estimate = judge.estimate if judge else None
        elif chosen and arm is not None and chosen in arm.metrics:
            estimate = arm.metrics[chosen].estimate
        else:
            estimate = None
        rows.append(
            {
                "rung": record.rung,
                "label": record.label,
                "variant": record.variant,
                "model": record.model,
                "output_mode": record.output_mode,
                "branches_from": record.branches_from,
                "metric": chosen or NOT_MEASURED,
                **_row(estimate),
                "judged_n": record.judged_n,
                "judged_split": record.judged_split,
                "rung_status": record.status,
                "unmeasured_reason": record.unmeasured_reason,
                "leaked_example_items": ", ".join(record.leaked_example_items) or "",
            }
        )
    frame = pd.DataFrame(rows)
    # A rung that did not run has no interval either way; make the two status
    # columns agree so a filter on one is a filter on both.
    if not frame.empty:
        frame.loc[frame["rung_status"] != "measured", "status"] = NOT_MEASURED
    return frame


def agreement_frame(shadow: Any, *, metric: str | None = None) -> pd.DataFrame:
    """Per-subagent shadow agreement, both figures side by side.

    Both are returned because they are not interchangeable — the item-level
    figure requires prose fields to match under a token-overlap proxy and moves
    with that proxy's threshold; the structured figure counts only fields with a
    defined right answer. ``metric=`` marks one as ``gate_figure`` so a chart can
    show which one a gate was actually checked against.
    """
    rows: list[dict[str, Any]] = []
    for record in shadow.subagents:
        block = record.agreement
        item = block.agreement if block else None
        structured = block.structured_agreement if block else None
        rows.append(
            {
                "subagent": record.subagent,
                "items": record.items,
                "n_compared": block.n_compared if block else None,
                "item_point": item.point if item else float("nan"),
                "item_lo": item.lo if item else float("nan"),
                "item_hi": item.hi if item else float("nan"),
                "structured_point": structured.point if structured else float("nan"),
                "structured_lo": structured.lo if structured else float("nan"),
                "structured_hi": structured.hi if structured else float("nan"),
                "prose_threshold": block.prose_threshold if block else None,
                "similarity_metric": block.similarity_metric if block else None,
                "gate_figure": metric or "",
            }
        )
    return pd.DataFrame(rows)


def triage_frame(shadow: Any, *, subagent: str | None = None) -> pd.DataFrame:
    """Every adjudicated disagreement, one row each — the triage browser.

    ``not_adjudicated`` rows keep their reason and are never folded into
    ``tie``. An item outside the judged split has no recorded verdict; calling
    that a tie would manufacture an outcome nobody measured.
    """
    rows: list[dict[str, Any]] = []
    for record in shadow.subagents:
        if subagent and record.subagent != subagent:
            continue
        for row in record.triage:
            rows.append(
                {
                    "subagent": row.subagent,
                    "item_id": row.item_id,
                    "verdict": row.verdict,
                    "fields": ", ".join(row.fields),
                    "rationale": row.rationale,
                    "baseline_score": row.baseline_score,
                    "candidate_score": row.candidate_score,
                    "judged_split": row.judged_split,
                    "reason": row.reason or "",
                }
            )
    return pd.DataFrame(rows)


def latency_frame(shadow: Any) -> pd.DataFrame:
    """Per-arm latency percentiles, each carrying its region and disclosure.

    The disclosure column is not decoration. Claude ran in ``global`` and
    Gemini in ``us-central1``, so these two rows are not comparable, and the
    frame says so in the row rather than in a caption someone can crop out.
    """
    rows: list[dict[str, Any]] = []
    for record in shadow.subagents:
        for side, arm in (("baseline", record.baseline), ("candidate", record.candidate)):
            latency = arm.latency
            rows.append(
                {
                    "subagent": record.subagent,
                    "side": side,
                    "arm": arm.arm,
                    "model": arm.model,
                    "region": latency.region if latency else None,
                    "n_traces": latency.n_traces if latency else 0,
                    "total_p50_ms": latency.total_p50_ms if latency else None,
                    "total_p95_ms": latency.total_p95_ms if latency else None,
                    "ttft_p95_ms": latency.ttft_p95_ms if latency else None,
                    "disclosure": (latency.disclosure if latency else NOT_MEASURED),
                }
            )
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# charts
# --------------------------------------------------------------------------


def interval_chart(
    frame: pd.DataFrame,
    *,
    label: str | Sequence[str] = "variant",
    point: str = "point",
    lo: str = "lo",
    hi: str = "hi",
    title: str = "",
    xlabel: str = "",
    bound: float | None = None,
    bound_label: str = "gate bound",
    figsize: tuple[float, float] | None = None,
):
    """Horizontal point-and-interval chart — the only chart shape used.

    Bars imply a measurement from zero, which none of these are; a point with
    its 95% interval is what the gates are actually checked on, so that is what
    gets drawn. Rows whose ``point`` is ``NaN`` are kept on the axis and labelled
    ``not measured`` instead of being silently dropped, so the reader can see
    that a rung exists and has no number.

    ``bound=`` draws the gate threshold as a vertical line, which is what makes
    a chart answer "did it pass" rather than "which is bigger".
    """
    import matplotlib.pyplot as plt

    if isinstance(label, str):
        labels = frame[label].astype(str).tolist()
    else:
        labels = [" / ".join(str(row[c]) for c in label) for _, row in frame.iterrows()]

    points = frame[point].tolist()
    los = frame[lo].tolist()
    his = frame[hi].tolist()
    positions = list(range(len(labels)))[::-1]  # first row at the top

    figsize = figsize or (8.0, max(2.0, 0.45 * len(labels) + 1.2))
    fig, ax = plt.subplots(figsize=figsize)

    for y, value, low, high in zip(positions, points, los, his):
        if value != value:  # NaN
            ax.text(0.02, y, NOT_MEASURED, va="center", fontsize=9, style="italic",
                    transform=ax.get_yaxis_transform())
            continue
        low = value if low != low else low
        high = value if high != high else high
        ax.plot([low, high], [y, y], linewidth=2, solid_capstyle="butt", color="#4a6fa5")
        ax.plot([value], [y], marker="o", markersize=6, color="#1f3b63")

    if bound is not None:
        ax.axvline(bound, linestyle="--", linewidth=1.2, color="#b3413b")
        ax.text(bound, len(labels) - 0.4, f" {bound_label}: {bound:g}",
                color="#b3413b", fontsize=8, va="bottom")

    ax.set_yticks(positions)
    ax.set_yticklabels(labels, fontsize=9)
    ax.set_xlabel(xlabel or "point estimate with 95% CI")
    if title:
        ax.set_title(title, fontsize=11)
    ax.grid(axis="x", alpha=0.25)
    ax.set_axisbelow(True)
    fig.tight_layout()
    # Closed before returning so the inline backend does not also flush it at
    # the end of the cell: the caller does `display(fig)`, and without this the
    # same chart is emitted twice. Closing removes it from pyplot's registry;
    # the Figure object itself still renders.
    plt.close(fig)
    return fig
