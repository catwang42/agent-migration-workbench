"""The managed evaluation showcase: a second instrument that cannot become the
gated one, and an outage that cannot become a measurement.

Three properties carry the whole module, and each has a plausible-looking wrong
answer that these tests exist to forbid:

* **The two instruments cannot be merged.** Not "are not merged today" — there
  must be nowhere to put a blended number. Enforced by asserting the ``source``
  discriminators are frozen and that :class:`~amw.eval.vertex_eval.ArmEvaluation`
  owns no score of its own.
* **A gap is never a number.** No SDK, no credentials, a failed arm, an errored
  recording, an empty loss analysis: every one of them has a tempting default
  (skip silently, score 0.0, report "no defects found") and every one of them
  must instead surface as a stated absence.
* **The managed metric scores our rubrics.** If the service generates its own,
  the managed column and the internal column stop being about the same
  question, and putting them side by side becomes misleading rather than
  informative.

Everything here runs offline against a fake client. The module's live path is
exercised by the committed ``artifacts/results/vertex_eval.json``, which the
last group of tests reads if it is present.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from amw.config import load_all
from amw.eval import vertex_eval as ve
from amw.eval.runner import Phase2Result, rubric_of
from amw.traces.schema import Trace, TraceInput, TraceOutput, ToolCall

REPO = Path(__file__).resolve().parents[1]
PHASE2 = REPO / "artifacts" / "results" / "phase2_n70.json"


# --------------------------------------------------------------------------
# fakes — a managed service that never leaves the process
# --------------------------------------------------------------------------


class _Summary:
    def __init__(self, name, mean=0.75):
        self.metric_name = name
        self.mean_score = mean
        self.stdev_score = 0.2
        self.pass_rate = 0.5
        self.num_cases_total = 4
        self.num_cases_valid = 3
        self.num_cases_error = 1


class _Verdict:
    def __init__(self, ok):
        self.verdict = ok


class _MetricResult:
    def __init__(self, ok):
        # A failed rubric comes back as verdict=None, not False — proto3 omits
        # the field at its default. The fake reproduces that faithfully; a fake
        # that used False would let the counting bug back in.
        self.rubric_verdicts = [_Verdict(True), _Verdict(True if ok else None)]


class _CandidateResult:
    def __init__(self, metrics, ok):
        self.metric_results = {m.name: _MetricResult(ok) for m in metrics}


class _CaseResult:
    def __init__(self, metrics, ok):
        self.response_candidate_results = [_CandidateResult(metrics, ok)]


class _EvalResult:
    """Two cases, one of which failed a rubric — so a "no clusters" report has
    something to be surprising about."""

    def __init__(self, metrics):
        self.summary_metrics = [_Summary(m.name) for m in metrics]
        self.eval_case_results = [
            _CaseResult(metrics, True),
            _CaseResult(metrics, False),
        ]


class _Evals:
    """Records what was asked of the service, and answers plausibly."""

    def __init__(self, *, loss=None):
        self.calls: list[dict] = []
        self._loss = loss

    def evaluate(self, *, dataset, metrics, config=None):
        self.calls.append(
            {
                "candidate": dataset.candidate_name,
                "df": dataset.eval_dataset_df,
                "metrics": [m.name for m in metrics],
                "rubric_groups": [getattr(m, "rubric_group_name", None) for m in metrics],
            }
        )
        return _EvalResult(metrics)

    def generate_loss_clusters(self, *, eval_result, metric, candidate):
        if isinstance(self._loss, Exception):
            raise self._loss
        return self._loss


class _Client:
    def __init__(self, *, loss=None):
        self.evals = _Evals(loss=loss)


def _phase2() -> Phase2Result:
    return Phase2Result.model_validate_json(PHASE2.read_text())


def _run(**kwargs):
    """A one-subagent managed run against a fake client, writing to tmp."""
    client = kwargs.pop("client", None) or _Client()
    out = kwargs.pop("out_path")
    result = ve.run_vertex_eval(
        config=kwargs.pop("config", None) or load_all(customer="demo_patents"),
        results_path=str(PHASE2),
        phase2=_phase2(),
        subagents=kwargs.pop("subagents", ["query_rewriter"]),
        sample_size=kwargs.pop("sample_size", 4),
        project="test-project",
        region="us-central1",
        client=client,
        loss_client=client,
        out_path=out,
        progress=False,
        **kwargs,
    )
    return result, client


pytestmark = pytest.mark.skipif(
    not PHASE2.is_file(), reason="no phase-2 artifact to report beside"
)


# --------------------------------------------------------------------------
# the separation rule, enforced by the types
# --------------------------------------------------------------------------


def test_a_figures_source_cannot_be_relabelled_after_the_fact():
    """The one edit that would silently merge the instruments is refused.

    Relabelling is how two instruments end up in one column: not by anyone
    deciding to average them, but by a figure being copied into the other
    list and its label following along.
    """
    managed = ve.ManagedFigure(metric=ve.QUALITY_METRIC, instrument="rubric_based_quality")
    internal = ve.InternalFigure(metric="judge_score", instrument="rubric_judged_quality")
    assert managed.source == ve.MANAGED_SOURCE
    assert internal.source == ve.INTERNAL_SOURCE
    for figure in (managed, internal):
        with pytest.raises(Exception):
            figure.source = "something_else"


def test_the_two_figure_types_are_not_interchangeable():
    with pytest.raises(Exception):
        ve.ManagedFigure(
            metric="judge_score",
            instrument="rubric_judged_quality",
            source=ve.INTERNAL_SOURCE,
        )
    with pytest.raises(Exception):
        ve.InternalFigure(
            metric=ve.QUALITY_METRIC,
            instrument="rubric_based_quality",
            source=ve.MANAGED_SOURCE,
        )


def test_an_arm_owns_no_number_of_its_own():
    """There is nowhere for a blended score to live, which is the point.

    ``ArmEvaluation`` holds one list per instrument and nothing numeric beside
    them, so "combined quality" cannot be added by accident — only by adding a
    field, which fails this test.
    """
    numeric = {
        name
        for name, field in ve.ArmEvaluation.model_fields.items()
        if field.annotation in (float, int, "float", "int")
    }
    assert numeric == {"items_submitted"}, (
        "ArmEvaluation grew a scalar metric. Managed and internal figures live "
        "in their own lists precisely so no field can hold their average."
    )


def test_every_schema_forbids_unknown_fields():
    for model in (
        ve.ManagedFigure,
        ve.InternalFigure,
        ve.LossCluster,
        ve.LossClusterReport,
        ve.ArmEvaluation,
        ve.VertexEvalResult,
    ):
        assert model.model_config.get("extra") == "forbid", model.__name__


def test_every_row_of_the_side_by_side_frame_names_its_instrument():
    result, _ = _run(out_path="/tmp/amw_ve_sbs.json")
    frame = ve.side_by_side_frame(result)
    assert not frame.empty
    assert set(frame["source"]) <= {ve.MANAGED_SOURCE, ve.INTERNAL_SOURCE}
    assert frame["source"].notna().all()
    # Both instruments present, and neither has absorbed the other.
    assert {ve.MANAGED_SOURCE, ve.INTERNAL_SOURCE} == set(frame["source"])


def test_the_artifact_states_the_separation_rule_where_a_reader_will_see_it():
    result, _ = _run(out_path="/tmp/amw_ve_rule.json")
    assert "never averaged" in result.separation_rule
    assert result.separation_rule in ve.render_markdown(result)


# --------------------------------------------------------------------------
# the managed metric is asked our question
# --------------------------------------------------------------------------


def test_the_managed_quality_metric_is_pointed_at_the_datasets_own_rubrics():
    """Otherwise the two columns are about different questions.

    ``general_quality_v1`` will generate its own rubrics if not given any. The
    rubric group must be both requested on the metric *and* supplied on every
    row, or the service silently falls back and the side-by-side comparison
    stops being a comparison.
    """
    result, client = _run(out_path="/tmp/amw_ve_rubrics.json")
    call = client.evals.calls[0]
    assert ve.RUBRIC_GROUP in call["rubric_groups"]
    frame = call["df"]
    assert (frame["rubric_groups"].map(lambda g: ve.RUBRIC_GROUP in g)).all()

    # ...and those rubrics are the harness's own, criterion for criterion.
    from amw.datasets.schema import read_items

    items = {i.item_id: i for i in read_items(REPO / "datasets" / "query_rewriter.jsonl")}
    arm = result.arms[0]
    expected = [c.id for c in rubric_of(items[arm.item_ids[0]]).criteria]
    sent = [r["rubric_id"] for r in frame["rubric_groups"].iloc[0][ve.RUBRIC_GROUP]]
    assert sent == expected
    assert all(
        r["type"] == ve.RUBRIC_TYPE
        for r in frame["rubric_groups"].iloc[0][ve.RUBRIC_GROUP]
    )


def test_the_tool_use_metric_runs_only_where_a_tool_was_actually_called():
    """A response_schema arm has no tool-calling behaviour to be bad at.

    Scoring ``gemini_tuned_v1`` on tool use would produce a low number that
    reads as "worse at tool calling" when the truth is "does not tool call by
    design". It is excluded with the reason on the record.
    """
    result, client = _run(out_path="/tmp/amw_ve_tools.json")
    by_candidate = {c["candidate"]: c["metrics"] for c in client.evals.calls}
    for arm in ve.TOOL_CALLING_ARMS:
        assert ve.TOOL_USE_METRIC in by_candidate[arm]
    assert ve.TOOL_USE_METRIC not in by_candidate["gemini_tuned_v1"]

    tuned = result.for_arm("query_rewriter", "gemini_tuned_v1")
    assert not any(f.metric == ve.TOOL_USE_METRIC for f in tuned.managed)
    assert any("response_schema" in note for note in tuned.notes)


def test_recorded_tool_calls_reach_the_service_as_function_calls():
    result, client = _run(out_path="/tmp/amw_ve_events.json")
    call = next(c for c in client.evals.calls if c["candidate"] == "claude_baseline")
    events = call["df"]["intermediate_events"].iloc[0]
    assert events, "the recorded emit_* call must be visible to the tool-use metric"
    assert events[0]["content"]["parts"][0]["function_call"]["name"].startswith("emit_")


def test_the_case_carries_conversation_turns_so_loss_analysis_can_run():
    """The loss-analysis API rejects results whose cases have no turns.

    Learned the hard way: without ``agent_data`` the evaluation succeeds and
    only the clustering step 400s, which would leave the showcase's headline
    deliverable silently missing.
    """
    _, client = _run(out_path="/tmp/amw_ve_turns.json")
    turns = client.evals.calls[0]["df"]["agent_data"].iloc[0]["turns"]
    assert len(turns) == 1
    roles = [e["content"]["role"] for e in turns[0]["events"]]
    assert roles[0] == "user" and roles[-1] == "model"


def test_the_prompt_sent_for_grading_is_the_one_the_model_was_given():
    """Grading against a paraphrase would measure a different task."""
    _, client = _run(out_path="/tmp/amw_ve_prompt.json")
    frame = client.evals.calls[0]["df"]
    assert (frame["prompt"].str.len() > 0).all()
    assert (frame["instruction"].str.len() > 0).all()


def test_only_items_the_internal_judge_scored_are_submitted():
    """A managed number beside an empty internal cell is not a comparison."""
    from amw.datasets.schema import read_items

    result, _ = _run(out_path="/tmp/amw_ve_pool.json")
    core = {
        i.item_id
        for i in read_items(REPO / "datasets" / "query_rewriter.jsonl")
        if i.core
    }
    assert set(result.arms[0].item_ids) <= core
    assert "core split" in result.scope["query_rewriter"]


def test_the_sample_is_deterministic():
    a, _ = _run(out_path="/tmp/amw_ve_seed_a.json")
    b, _ = _run(out_path="/tmp/amw_ve_seed_b.json")
    assert a.arms[0].item_ids == b.arms[0].item_ids


# --------------------------------------------------------------------------
# gaps stay gaps
# --------------------------------------------------------------------------


def test_no_sdk_and_no_credentials_is_a_stated_skip_not_a_zero(tmp_path, monkeypatch):
    """Ground rules 1 and 4 at once: e2e must stay green with nothing installed.

    The wrong behaviour here is not a crash — a crash would be noticed. It is a
    run that "completes" with every managed figure defaulting to 0.0.
    """
    monkeypatch.delenv("PROJECT_ID", raising=False)
    out = tmp_path / "vertex_eval.json"
    result = ve.run_vertex_eval(
        config=load_all(customer="demo_patents"),
        phase2=_phase2(),
        results_path=str(PHASE2),
        project=None,
        out_path=out,
        progress=False,
    )
    assert result.status == ve.UNAVAILABLE
    assert result.skip_reason and "not called" in result.skip_reason
    assert result.arms == []
    assert out.is_file()
    written = ve.VertexEvalResult.model_validate_json(out.read_text())
    assert written.status == ve.UNAVAILABLE
    banner = ve.managed_banner(written)
    assert "UNAVAILABLE" in banner and "none was substituted" in banner
    assert ve.side_by_side_frame(written).empty
    assert ve.loss_cluster_frame(written).empty


def test_an_arm_whose_managed_call_fails_reports_the_failure_not_a_score():
    class _Boom(_Client):
        def __init__(self):
            super().__init__()
            self.evals.evaluate = self._boom

        @staticmethod
        def _boom(**kwargs):
            raise RuntimeError("429 quota exhausted")

    result, _ = _run(out_path="/tmp/amw_ve_boom.json", client=_Boom())
    for arm in result.arms:
        assert arm.managed == []
        assert any("429" in note for note in arm.notes)
    # The internal column survives — one instrument failing does not blank both.
    assert all(arm.internal for arm in result.arms)


def test_an_errored_recording_is_excluded_with_a_reason_never_scored_zero():
    """A failed generation is a gap in the recording, not a bad answer."""
    trace = Trace(
        trace_id="t1",
        subagent="query_rewriter",
        provenance="synthetic",
        ts="2026-08-09T16:00:00Z",
        model="claude-sonnet",
        system_prompt_sha="abc",
        input=TraceInput(messages=["q"]),
        status="error",
        error="boom",
    )
    ok = trace.model_copy(
        update={
            "status": "ok",
            "tool_calls": [ToolCall(name="emit_query_plan", args={"a": 1})],
            "output": TraceOutput(json_={"a": 1}),
        }
    )

    class _Item:
        item_id = "x1"
        gold = {"a": 1}
        rubric = []

    from amw.datasets.schema import read_items

    item = next(iter(read_items(REPO / "datasets" / "query_rewriter.jsonl")))

    class _Req:
        system_prompt = "sys"

    rows, ids, excluded = ve.build_arm_rows(
        [(item, _Req(), trace), (item, _Req(), ok)]
    )
    assert len(rows) == 1 and ids == [item.item_id]
    assert sum(excluded.values()) == 1
    assert any("errored" in reason for reason in excluded)
    # The exclusion is carried onto the figure, so the n is explainable.
    figure = ve.ManagedFigure(
        metric=ve.QUALITY_METRIC, instrument="rubric_based_quality", excluded=excluded
    )
    assert figure.mean_score is None and figure.excluded == excluded


def test_an_empty_loss_analysis_is_not_a_claim_that_nothing_failed():
    """The SDK raises for an empty LRO response; that is a finding, not an error.

    "No clusters returned" and "no defects" are different statements, and only
    the first one was measured.
    """
    boom = RuntimeError("Loss analysis operation completed but returned no response.")
    result, _ = _run(out_path="/tmp/amw_ve_noclust.json", client=_Client(loss=boom))
    for arm in result.arms:
        report = arm.loss_clusters
        assert report.status == ve.NO_CLUSTERS
        assert report.clusters == []
        assert "not a claim that nothing failed" in report.reason
        # The counts are what make the status readable: an empty analysis over
        # known failures is a fact about the tool, not about the arm.
        assert report.cases_analysed == 2
        assert report.failing_verdicts == 1
        assert "1 failing rubric verdict" in report.reason
    text = ve.render_markdown(result)
    assert "no defects" not in text.lower()
    assert "failing rubric verdict(s)" in text


def test_a_failed_rubric_arrives_as_none_and_is_counted_as_a_failure():
    """proto3 omits ``verdict`` when it is false, so the SDK yields ``None``.

    Counting ``is False`` reports zero failures on an arm the service marked
    down, which turns "no clusters returned" into a clean bill of health. The
    counting rule is checked directly because the consequence is silent.
    """

    class _M:
        rubric_verdicts = [_Verdict(True), _Verdict(None), _Verdict(False)]

    class _RC:
        metric_results = {ve.QUALITY_METRIC: _M()}

    class _C:
        response_candidate_results = [_RC()]

    class _R:
        eval_case_results = [_C()]

    assert ve._verdict_counts(_R(), ve.QUALITY_METRIC) == (1, 2)


def test_a_broken_loss_analysis_is_unavailable_not_empty():
    result, _ = _run(
        out_path="/tmp/amw_ve_lossfail.json",
        client=_Client(loss=ValueError("permission denied")),
    )
    report = result.arms[0].loss_clusters
    assert report.status == ve.LOSS_UNAVAILABLE
    assert "permission denied" in report.reason
    assert report.status != ve.NO_CLUSTERS


def test_arms_with_no_clusters_keep_their_row_in_the_frame():
    """A missing row reads as "clean"; an empty row reads as "not measured"."""
    boom = RuntimeError("Loss analysis operation completed but returned no response.")
    result, _ = _run(out_path="/tmp/amw_ve_frame.json", client=_Client(loss=boom))
    frame = ve.loss_cluster_frame(result)
    assert len(frame) == len(result.arms)
    assert frame["items"].isna().all()
    assert (frame["status"] == ve.NO_CLUSTERS).all()
    assert frame["description"].notna().all()


def test_load_result_distinguishes_not_run_from_measured_nothing(tmp_path):
    assert ve.load_result(tmp_path / "absent.json") is None
    assert "NOT RUN" in ve.managed_banner(None)
    assert "Nothing below is a measurement" in ve.managed_banner(None)


# --------------------------------------------------------------------------
# provenance on the artifact
# --------------------------------------------------------------------------


def test_the_artifact_carries_provenance_region_and_the_recording_window():
    """Ground rule 2, plus the bit that is specific to a re-scoring instrument:
    the outputs are recordings, and the artifact says when they were made."""
    result, _ = _run(out_path="/tmp/amw_ve_prov.json")
    assert result.provenance in ("synthetic", "customer")
    assert result.eval_region and result.run_started
    assert result.loss_analysis_region == ve.LOSS_ANALYSIS_REGION
    assert result.recorded_from and result.recorded_to
    assert result.recorded_from <= result.recorded_to
    assert result.gates_version_hash
    footer = ve.render_markdown(result)
    assert result.recorded_from in footer and result.gates_version_hash in footer


def test_the_two_regions_are_reported_separately():
    """Metrics run in the customer's region; loss analysis is global-only.

    Collapsing them to one 'region' field would put a wrong region on a
    customer-facing report footer.
    """
    result, _ = _run(out_path="/tmp/amw_ve_region.json")
    assert result.eval_region == "us-central1"
    assert result.loss_analysis_region == "global"
    assert "global endpoint" in ve.render_markdown(result)


def test_internal_figures_are_copied_not_recomputed():
    """If this file ever disagrees with the scorecard, the scorecard is right."""
    phase2 = _phase2()
    arm = next(a for a in phase2.arms if a.judge is not None)
    figures = ve.internal_figures_for(phase2, arm.subagent, arm.variant)
    judge = next(f for f in figures if f.metric == "judge_score")
    assert judge.point == arm.judge.point
    assert judge.n == arm.judge.items_scored
    assert judge.split == arm.judge.split


# --------------------------------------------------------------------------
# the committed artifact, if the live run has been made
# --------------------------------------------------------------------------

LIVE = ve.default_vertex_eval_path()


@pytest.mark.skipif(not LIVE.is_file(), reason="managed run not committed")
def test_the_committed_artifact_loads_and_keeps_the_instruments_apart():
    result = ve.load_result(LIVE)
    assert result.vertex_eval_version == ve.VERTEX_EVAL_VERSION
    frame = ve.side_by_side_frame(result)
    if result.status == ve.COMPLETED:
        assert result.recorded_from and result.eval_region
        assert set(frame["source"]) <= {ve.MANAGED_SOURCE, ve.INTERNAL_SOURCE}
        for arm in result.arms:
            for figure in arm.managed:
                assert figure.source == ve.MANAGED_SOURCE
            for figure in arm.internal:
                assert figure.source == ve.INTERNAL_SOURCE
    else:
        assert result.skip_reason


@pytest.mark.skipif(not LIVE.is_file(), reason="managed run not committed")
def test_the_committed_artifact_never_reports_a_number_it_did_not_measure():
    result = ve.load_result(LIVE)
    for arm in result.arms:
        for figure in arm.managed:
            if figure.mean_score is None:
                assert figure.cases_valid == 0
            else:
                assert figure.cases_valid > 0
