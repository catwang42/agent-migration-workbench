"""The ablation ladder as the scorecard renders it.

These tests are about what the table is *not allowed* to say. The ladder is
the part of the report most likely to be skimmed as a leaderboard, and the
three failure modes worth a test are all silent: a superseded row that still
shows, a re-measurement at a different n collapsed into the one before it, and
a contaminated score printed as though it were clean.
"""

from __future__ import annotations

from amw.eval.runner import ArmResult, JudgeReport, MetricReport
from amw.eval.stats import Estimate
from amw.reporting.cells import NOT_MEASURED
from amw.reporting.ladder import (
    CONTAMINATION_MARKER,
    build_ladder,
    ladder_rows,
    render_ladder,
)
from amw.tuning.ablate import AblationResult, RungProvenance, RungRecord


def _provenance(**over) -> RungProvenance:
    fields = dict(
        customer="demo_patents",
        mode="replay",
        region="us-central1",
        dataset_provenance="synthetic",
        dataset_seed=7,
        generator_version="1",
        items=28,
        split="core",
        bootstrap_seed=20260812,
        judge_repeats=2,
        written_at="2026-08-11T00:00:00+00:00",
    )
    fields.update(over)
    return RungProvenance(**fields)


def _estimate(point: float, lo: float, hi: float, n: int) -> Estimate:
    return Estimate(
        metric="judge_score",
        point=point,
        lo=lo,
        hi=hi,
        n=n,
        unit="fraction",
        method="percentile_bootstrap",
    )


def _arm(*, variant: str, score: float, n: int, split: str, accuracy: float = 1.0):
    return ArmResult(
        subagent="feature_extractor",
        variant=variant,
        model="gemini-flash",
        output_mode="tool",
        prompt_sha="deadbeef",
        items=n,
        calls_ok=n,
        calls_error=0,
        metrics={
            "extraction_accuracy": MetricReport(
                metric="extraction_accuracy",
                point=accuracy,
                estimate=_estimate(accuracy, accuracy, accuracy, n),
                n=n,
                n_excluded=0,
            )
        },
        judge=JudgeReport(
            point=score,
            estimate=_estimate(score, score - 0.04, score + 0.03, n),
            split=split,
            items_scored=n,
            expected_repeats=2 * n,
            failed_repeats=0,
        ),
    )


def _rung(
    rung: str,
    *,
    variant: str = "gemini_novelty_v1_tool",
    status: str = "measured",
    score: float = 0.9,
    n: int = 28,
    split: str = "core",
    leaked: list[str] | None = None,
    reason: str | None = None,
    accuracy: float = 1.0,
) -> RungRecord:
    return RungRecord(
        rung=rung,
        label=rung,
        variant=variant,
        output_mode="tool",
        model="gemini-flash",
        prompt_sha="deadbeef",
        status=status,
        unmeasured_reason=reason,
        judged_n=n if status == "measured" else None,
        judged_split=split if status == "measured" else None,
        leaked_example_items=list(leaked or []),
        provenance=_provenance(items=n, split=split),
        arm=_arm(variant=variant, score=score, n=n, split=split, accuracy=accuracy)
        if status == "measured"
        else None,
    )


def _ladder(*rungs: RungRecord, notes: list[str] | None = None) -> AblationResult:
    return AblationResult(
        subagent="feature_extractor", rungs=list(rungs), notes=list(notes or [])
    )


def test_a_measured_rerun_supersedes_the_unmeasured_record_it_replaces():
    """no_recordings is the absence of a measurement, not a second data point.

    The FE ladder holds both: the rungs were registered and reported unmeasured
    on 2026-08-10, then run live on 2026-08-11. Rendering both leaves a row
    saying the rung has no numbers directly above the row carrying its numbers.
    """
    result = _ladder(
        _rung("A4-novelty-tool", status="no_recordings", reason="nothing recorded"),
        _rung("A4-novelty-tool", score=0.901),
    )
    rows = build_ladder(result).rows
    assert [r.rung for r in rows] == ["A4-novelty-tool"]
    assert rows[0].status == "measured"
    assert "0.901" in rows[0].judge_text()


def test_an_unmeasured_rung_with_no_measurement_anywhere_still_shows():
    """The other direction: a rung nobody ran is a row, not an omission."""
    result = _ladder(
        _rung("A0", variant="gemini_naive", score=0.837),
        _rung("A4-prime", status="no_recordings", reason="never run live"),
    )
    rows = build_ladder(result).rows
    assert [r.rung for r in rows] == ["A0", "A4-prime"]
    assert rows[1].judge is None
    assert rows[1].judge_text() == NOT_MEASURED


def test_the_same_rung_on_two_splits_stays_two_rows_and_says_so():
    """Widening core-28 to the full 70 adds a measurement; it does not replace one.

    Both survive, each carries its own n and split, and the mixed-split note
    fires — a 0.949 at n=28 and a 0.949 at n=70 are not two points on a curve.
    """
    result = _ladder(
        _rung("A4-optimizer", variant="gemini_optimizer_v1", score=0.949, n=28),
        _rung(
            "A4-optimizer",
            variant="gemini_optimizer_v1",
            score=0.940,
            n=70,
            split="all",
        ),
    )
    ladder = build_ladder(result)
    assert [r.split for r in ladder.rows] == ["core", "all"]
    assert ladder.splits == ["core", "all"]

    text = "\n".join(render_ladder(ladder))
    assert "judged n=28, split=core" in text
    assert "judged n=70, split=all" in text
    assert "different splits" in text


def test_one_split_throughout_does_not_raise_a_comparability_warning():
    result = _ladder(
        _rung("A0", variant="gemini_naive", score=0.837),
        _rung("A1-A3", variant="gemini_tuned_v1", score=0.807),
    )
    text = "\n".join(render_ladder(build_ladder(result)))
    assert "different splits" not in text


def test_a_contaminated_rung_is_marked_in_the_row_and_explained_below_it():
    result = _ladder(_rung("A4-novelty-tool", score=0.93, leaked=["fe-0003"]))
    ladder = build_ladder(result)
    row = ladder.rows[0]
    assert row.contaminated
    assert row.judge_text().endswith(CONTAMINATION_MARKER)

    text = "\n".join(render_ladder(ladder))
    assert f"{CONTAMINATION_MARKER} `A4-novelty-tool` quotes `fe-0003`" in text
    assert "1 of 28" in text
    # Disclosed, never silently dropped: excluding the item would change this
    # rung's denominator alone.
    assert "not excluded" in text


def test_a_clean_rung_carries_no_marker_and_no_note():
    result = _ladder(_rung("A4-novelty-tool", score=0.901))
    text = "\n".join(render_ladder(build_ladder(result)))
    assert CONTAMINATION_MARKER not in text


def test_the_unmeasured_reason_goes_under_the_table_not_inside_a_cell():
    """A replay-miss diagnostic is several lines long and would eat the table."""
    reason = (
        "replay mode has no recorded calls for variant 'gemini_naive_schema' "
        "(replay miss for input_sha='f2b73b67'). Re-record with --mode live."
    )
    result = _ladder(_rung("A0-schema", status="no_recordings", reason=reason))
    lines = render_ladder(build_ladder(result))
    row = next(line for line in lines if line.startswith("| `A0-schema`"))
    assert reason not in row
    assert NOT_MEASURED in row
    assert any(line == f"- `A0-schema` — {reason}" for line in lines)


def test_the_ladder_names_no_winner():
    """Rungs render in ladder order, with no ranking column and no top-row bold.

    Rider 2 of the owner's B1 ruling: the hand-tuned rung and the optimizer
    rung both stay in the ladder permanently and neither is labelled the
    winner. The selection is made against the gated rows.
    """
    result = _ladder(
        _rung("A0", variant="gemini_naive", score=0.837),
        _rung("A4-novelty-schema", variant="gemini_novelty_v1_schema", score=0.920),
        _rung("A4-optimizer", variant="gemini_optimizer_v1", score=0.949),
    )
    ladder = build_ladder(result)
    assert [r.rung for r in ladder.rows] == ["A0", "A4-novelty-schema", "A4-optimizer"]

    lines = render_ladder(ladder)
    header = lines[0].lower()
    for banned in ("best", "winner", "rank", "selected", "recommend"):
        assert banned not in header

    text = "\n".join(lines)
    assert "ranks nothing and selects nothing" in text
    # The top-scoring rung gets no typographic promotion over the others.
    assert "**0.949" not in text


def test_a_measured_rung_with_no_judge_pass_reports_no_judged_score():
    """Deterministic metrics without a judge run is a real state.

    It must not render as a zero, and the arm's extraction accuracy must not
    slide into the judged-score column to fill the gap.
    """
    record = _rung("A0", variant="gemini_naive", score=0.8, accuracy=0.93)
    record.arm.judge = None
    ladder = build_ladder(_ladder(record))
    row = ladder.rows[0]
    assert row.judge is None
    assert row.judge_text() == NOT_MEASURED
    assert row.metrics["extraction_accuracy"].startswith("0.930")


def test_metric_columns_are_the_union_across_rungs_in_first_seen_order():
    a = _rung("A0", variant="gemini_naive", score=0.8)
    b = _rung("A1-A3", variant="gemini_tuned_v1", score=0.9)
    b.arm.metrics["json_schema_validity"] = MetricReport(
        metric="json_schema_validity",
        point=1.0,
        estimate=_estimate(1.0, 1.0, 1.0, 28),
        n=28,
        n_excluded=0,
    )
    ladder = build_ladder(_ladder(a, b))
    assert ladder.metric_columns == ["extraction_accuracy", "json_schema_validity"]
    # The rung that never reported the metric gets an em dash, not a zero.
    assert ladder.rows[0].metrics["json_schema_validity"] == "—"


def test_an_empty_ladder_renders_nothing_rather_than_an_empty_table():
    assert render_ladder(build_ladder(_ladder())) == []


def test_ladder_rows_preserves_first_seen_rung_order():
    records = [
        _rung("A1-A3", variant="gemini_tuned_v1"),
        _rung("A0", variant="gemini_naive"),
        _rung("A1-A3", variant="gemini_tuned_v1", score=0.81),
    ]
    assert [r.rung for r in ladder_rows(records)] == ["A1-A3", "A0"]
