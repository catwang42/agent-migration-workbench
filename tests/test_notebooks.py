"""T13 — the notebooks stay thin, and the display layer never invents a number.

Two halves:

* Structural checks on the ``.ipynb`` files themselves. Ground rule 8 is easy to
  state and easy to erode one convenient cell at a time, so it is asserted here
  rather than trusted.
* Behavioural checks on :mod:`amw.reporting.notebook`, concentrated on the one
  failure mode that matters: a gap rendering as a value. An unmeasured rung must
  come back as ``NaN`` with a status, never as ``0.0``, because a zero-height bar
  and an absent bar look nothing alike to a reader but identical to a chart.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

nbformat = pytest.importorskip("nbformat")
pd = pytest.importorskip("pandas")

from amw.eval.stats import Estimate
from amw.reporting import notebook as nb

REPO = Path(__file__).resolve().parents[1]
NOTEBOOK_DIR = REPO / "notebooks"
NOTEBOOKS = sorted(NOTEBOOK_DIR.glob("*.ipynb"))

#: Ground rule 8's "~15 lines of logic". Blank lines, comments, imports and
#: `display(...)` calls are presentation, not logic, so they do not count — the
#: rule is about analysis leaking into notebook JSON, not about cell length.
MAX_LOGIC_LINES = 15


def _logic_lines(source: str) -> list[str]:
    lines = []
    for raw in source.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("%"):
            continue
        if line.startswith(("import ", "from ")):
            continue
        if line.startswith(("display(", "print(")):
            continue
        lines.append(line)
    return lines


def test_there_are_two_notebooks():
    assert [p.name for p in NOTEBOOKS] == [
        "01_baseline_and_tuning.ipynb",
        "02_shadow_scorecard.ipynb",
    ]


@pytest.mark.parametrize("path", NOTEBOOKS, ids=lambda p: p.name)
def test_notebook_is_valid_and_holds_no_committed_output(path: Path):
    """Committed notebooks carry source only.

    A committed output is a number with no provenance: it says nothing about
    when it was recorded, and it survives long after the artifact it came from
    has been regenerated. The executed copies go to ``artifacts/notebooks/``.
    """
    notebook = nbformat.read(path, as_version=4)
    nbformat.validate(notebook)
    for index, cell in enumerate(notebook.cells):
        if cell.cell_type == "code":
            assert not cell.get("outputs"), f"{path.name} cell {index} has saved output"
            assert cell.get("execution_count") is None


@pytest.mark.parametrize("path", NOTEBOOKS, ids=lambda p: p.name)
def test_notebook_has_a_papermill_parameters_cell(path: Path):
    notebook = nbformat.read(path, as_version=4)
    tagged = [c for c in notebook.cells if "parameters" in c.metadata.get("tags", [])]
    assert len(tagged) == 1, f"{path.name} needs exactly one parameters-tagged cell"
    assert "MODE" in tagged[0].source


@pytest.mark.parametrize("path", NOTEBOOKS, ids=lambda p: p.name)
def test_notebook_cells_stay_thin(path: Path):
    """Ground rule 8: logic lives in ``amw/``, notebooks import and display."""
    notebook = nbformat.read(path, as_version=4)
    fat = [
        (index, len(_logic_lines(cell.source)))
        for index, cell in enumerate(notebook.cells)
        if cell.cell_type == "code" and len(_logic_lines(cell.source)) > MAX_LOGIC_LINES
    ]
    assert not fat, (
        f"{path.name}: cells {fat} exceed {MAX_LOGIC_LINES} lines of logic. "
        "Move the logic into amw/reporting/notebook.py."
    )


@pytest.mark.parametrize("path", NOTEBOOKS, ids=lambda p: p.name)
def test_notebook_defaults_to_replay(path: Path):
    """A notebook that opens in live mode bills someone by being opened."""
    notebook = nbformat.read(path, as_version=4)
    (params,) = [c for c in notebook.cells if "parameters" in c.metadata.get("tags", [])]
    assert 'MODE = "replay"' in params.source


# --------------------------------------------------------------------------
# the display layer
# --------------------------------------------------------------------------


def test_a_missing_estimate_is_nan_and_labelled_never_zero():
    row = nb._row(None)
    assert row["point"] != row["point"]  # NaN
    assert row["status"] == nb.NOT_MEASURED
    assert row["n"] is None


def test_a_present_estimate_keeps_its_interval():
    estimate = Estimate(
        metric="m", point=0.5, lo=0.4, hi=0.6, n=10, unit="fraction",
        confidence=0.95, resamples=10000, seed=1, method="percentile_bootstrap",
    )
    row = nb._row(estimate)
    assert (row["point"], row["lo"], row["hi"], row["n"]) == (0.5, 0.4, 0.6, 10)
    assert row["status"] == "measured"


def test_replay_banner_states_the_recording_window():
    class Artifact:
        mode = "replay"
        recorded_from = "2026-08-09T16:07:15+00:00"
        recorded_to = "2026-08-10T02:45:59+00:00"

    banner = nb.replay_banner(Artifact())
    assert banner.startswith("REPLAY —")
    assert Artifact.recorded_from in banner and Artifact.recorded_to in banner
    assert "not from a run just now" in banner


def test_replay_banner_refuses_to_imply_freshness_without_a_window():
    class Artifact:
        mode = "replay"
        recorded_from = None
        recorded_to = None

    assert "cannot be stated" in nb.replay_banner(Artifact())


def test_live_banner_does_not_claim_a_recording_window():
    class Artifact:
        mode = "live"
        run_started = "2026-08-12T09:00:00+00:00"
        recorded_from = None
        recorded_to = None

    banner = nb.replay_banner(Artifact())
    assert banner.startswith("LIVE") and "recorded" not in banner


# --------------------------------------------------------------------------
# frames over the real artifacts, where they exist
# --------------------------------------------------------------------------

HAVE_PHASE2 = any((nb.ARTIFACTS / n).is_file() for n in ("phase2_n70.json", "phase2.json"))
HAVE_SHADOW = (nb.ARTIFACTS / "shadow.json").is_file()
HAVE_LADDER = (nb.ARTIFACTS / "ablation_feature_extractor.json").is_file()


@pytest.mark.skipif(not HAVE_PHASE2, reason="no phase-2 artifact committed")
def test_judge_frame_carries_the_split_every_score_was_measured_on():
    frame = nb.judge_frame(nb.load_phase2())
    assert {"judged_n", "split"} <= set(frame.columns)
    # The whole reason these columns exist: the splits are not all the same.
    assert frame["split"].nunique() > 1 or frame["judged_n"].nunique() > 1


@pytest.mark.skipif(not HAVE_LADDER, reason="no ablation artifact committed")
def test_unrun_rungs_survive_into_the_frame_with_no_number_on_them():
    frame = nb.ablation_frame(nb.load_ablation("feature_extractor"))
    unmeasured = frame[frame["rung_status"] != "measured"]
    assert not unmeasured.empty, "this fixture is meant to contain unrun rungs"
    assert unmeasured["point"].isna().all()
    assert (unmeasured["status"] == nb.NOT_MEASURED).all()
    assert unmeasured["unmeasured_reason"].notna().all()


@pytest.mark.skipif(not HAVE_SHADOW, reason="no shadow artifact committed")
def test_agreement_frame_returns_both_figures_not_one():
    """The two agreement figures are not interchangeable, so neither is hidden."""
    frame = nb.agreement_frame(nb.load_shadow_result(), metric="structured")
    assert {"item_point", "structured_point"} <= set(frame.columns)
    assert (frame["gate_figure"] == "structured").all()
    # If these ever coincide the caveat is over-stated; on this corpus they do not.
    assert not frame["item_point"].equals(frame["structured_point"])


@pytest.mark.skipif(not HAVE_SHADOW, reason="no shadow artifact committed")
def test_triage_frame_never_folds_unadjudicated_rows_into_ties():
    frame = nb.triage_frame(nb.load_shadow_result())
    unadjudicated = frame[frame["verdict"] == "not_adjudicated"]
    if not unadjudicated.empty:
        assert (unadjudicated["reason"].str.len() > 0).all()
    assert "tie" not in set(unadjudicated["verdict"])


@pytest.mark.skipif(not HAVE_SHADOW, reason="no shadow artifact committed")
def test_latency_rows_carry_their_region_disclosure():
    frame = nb.latency_frame(nb.load_shadow_result())
    assert frame["disclosure"].notna().all()
    assert (frame["disclosure"].str.len() > 0).all()


@pytest.mark.skipif(not HAVE_LADDER, reason="no ablation artifact committed")
def test_interval_chart_draws_unmeasured_rows_without_inventing_a_point():
    matplotlib = pytest.importorskip("matplotlib")
    matplotlib.use("Agg")
    frame = nb.ablation_frame(nb.load_ablation("feature_extractor"))
    fig = nb.interval_chart(frame, label="rung", bound=0.9)
    (ax,) = fig.axes
    # Every rung keeps its row on the axis, measured or not.
    assert len(ax.get_yticklabels()) == len(frame)
    labels = [t.get_text() for t in ax.texts]
    assert labels.count(nb.NOT_MEASURED) == int((frame["point"].isna()).sum())
