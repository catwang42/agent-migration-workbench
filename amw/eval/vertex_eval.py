"""Vertex Gen AI Evaluation Service — a managed instrument, run beside ours.

Everything judged on the scorecard is judged by *our* harness: our judge
prompt, our rubrics, our bootstrap. That is defensible, and it is also the
first thing a customer will push on — "you graded your own homework with your
own grader". The honest answer is the same one :mod:`amw.eval.crosscheck`
gives for the judge model: not an assurance, a second instrument. Here the
second instrument is not another model we drive, it is **Google's managed
evaluation service**, scoring the outputs we already recorded:

* ``general_quality_v1`` — the service's rubric-based quality metric, pointed
  at **our dataset's own rubrics** (via a rubric group), so it answers the same
  question the internal judge answered, criterion for criterion.
* ``tool_use_quality_v1`` — the service's tool-use quality metric, over the
  recorded ``tool_calls``. This is the Level 2 tool-calling instrument on real
  data: the internal harness only measures ``json_schema_validity``, which
  says the payload parsed, not that the call was a good call.
* ``generate_loss_clusters`` — the service's loss analysis, which groups the
  rubric failures it found into named loss patterns.

The separation rule, which this module enforces structurally
-------------------------------------------------------------

**The internal harness remains the gated instrument.** ``config/gates.yaml``
is checked against ``phase2.json`` and nothing in this file may change that.
The managed numbers land *beside* the internal ones, each labelled with its
source, and the two are never combined:

* no averaging, no blending, no "consensus" score;
* no substitution of one for the other, on any arm, for any reason;
* no writing back into ``phase2.json``.

That is enforced by the types rather than by good intentions.
:class:`ManagedFigure` and :class:`InternalFigure` are different classes with
a ``frozen`` ``source`` discriminator, so a figure cannot be relabelled after
the fact; :class:`ArmEvaluation` holds one list of each and owns no numeric
field of its own, so there is nowhere for a merged number to live. A test in
``tests/test_vertex_eval.py`` asserts both properties, because the cheapest
way to keep two instruments apart is to leave no place to put their average.

Why the managed metric is given our rubrics
-------------------------------------------

``general_quality_v1`` will happily generate its own rubrics. Letting it do so
would produce a number that is not comparable with anything: two instruments
disagreeing about a candidate is interesting, two instruments disagreeing about
*what the question was* is noise. So the dataset's own ``rubric`` list is
uploaded as a rubric group and the managed metric scores exactly those
criteria — the same text the internal judge scored, plus the same rerouted
Feature Extractor label criteria (:func:`amw.eval.runner.rubric_of`).

What this module will not do
----------------------------

It never invents a number, and it never lets an outage look like a
measurement. If the SDK is absent, credentials are absent, or the service
cannot be reached, the run **skips cleanly**: :attr:`VertexEvalResult.status`
becomes ``"unavailable"``, :attr:`VertexEvalResult.skip_reason` says why in
one sentence, and the artifact contains zero figures. That is why
``cli.py e2e --mode replay`` stays green offline — not because a placeholder
was substituted, but because nothing was claimed.

The generations are always replayed. This instrument re-scores outputs that
are already recorded; producing new ones here would mean the managed service
and the scorecard were talking about different answers.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field

from amw.adapters import AdapterRouter
from amw.agents.prompt_packs import VARIANTS, build_request
from amw.agents.schemas import SUBAGENTS
from amw.config import AppConfig, ConfigError, load_all
from amw.datasets.schema import DatasetItem, read_items
from amw.eval.crosscheck import stratified_sample
from amw.eval.runner import Phase2Result, prompt_view, rubric_of
from amw.traces.schema import Trace

__all__ = [
    "VERTEX_EVAL_VERSION",
    "MANAGED_SOURCE",
    "INTERNAL_SOURCE",
    "SEPARATION_RULE",
    "QUALITY_METRIC",
    "TOOL_USE_METRIC",
    "RUBRIC_GROUP",
    "RUBRIC_TYPE",
    "TOOL_CALLING_ARMS",
    "LOSS_ANALYSIS_REGION",
    "DEFAULT_SAMPLE_SIZE",
    "SAMPLE_SEED",
    "DEFAULT_QPS",
    "COMPLETED",
    "UNAVAILABLE",
    "CLUSTERED",
    "NO_CLUSTERS",
    "LOSS_UNAVAILABLE",
    "LOSS_NOT_REQUESTED",
    "ArmEvaluation",
    "InternalFigure",
    "LossCluster",
    "LossClusterReport",
    "ManagedFigure",
    "VertexEvalResult",
    "build_arm_rows",
    "default_vertex_eval_path",
    "internal_figures_for",
    "load_result",
    "loss_cluster_frame",
    "managed_banner",
    "render_markdown",
    "run_vertex_eval",
    "side_by_side_frame",
]

#: Bumped when the artifact shape changes, so a stale vertex_eval.json is
#: detectable the same way a stale phase2.json is.
VERTEX_EVAL_VERSION = "1"

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The two source labels. Every figure in the artifact carries exactly one of
#: them, and they are the discriminators that keep the instruments apart.
MANAGED_SOURCE = "vertex_gen_ai_evaluation_service"
INTERNAL_SOURCE = "internal_harness"

SEPARATION_RULE = (
    "The internal harness is the gated instrument: config/gates.yaml is checked "
    "against phase2.json and nothing here changes it. The Vertex Gen AI "
    "Evaluation Service is a managed second instrument reported beside it. The "
    "two are never averaged, never blended, and one is never substituted for "
    "the other — every figure below is labelled with the instrument that "
    "produced it."
)

#: The managed rubric-based quality metric, pointed at our own rubrics.
QUALITY_METRIC = "general_quality_v1"
#: The managed tool-use quality metric — the Level 2 tool-calling instrument.
TOOL_USE_METRIC = "tool_use_quality_v1"

#: Rubric group name the dataset's criteria are uploaded under, and the
#: ``type`` stamped on each rubric so the service's own generated rubrics could
#: never be mistaken for ours in a returned verdict.
RUBRIC_GROUP = "amw_dataset_rubric"
RUBRIC_TYPE = "amw_dataset_rubric"

#: Arms that emit through an ``emit_*`` tool call, and are therefore the only
#: arms the tool-use metric can say anything about. ``gemini_tuned_v1`` uses
#: ``response_schema`` and issues no tool call; it is excluded with a stated
#: reason rather than scored zero, because "did not use a tool" and "used a
#: tool badly" are different findings.
TOOL_CALLING_ARMS: tuple[str, ...] = ("claude_baseline", "gemini_naive")

#: The loss-analysis API is served only from the ``global`` endpoint (see the
#: SDK's ``generate_loss_clusters`` docstring), regardless of where the
#: evaluation itself ran. Recorded on the artifact so the region disclosure on
#: the report is accurate rather than assumed.
LOSS_ANALYSIS_REGION = "global"

#: Items per subagent, drawn deterministically. Deliberately a sample, not the
#: corpus: this is a showcase of a second instrument, it is not a gated
#: measurement, and it shares autorater quota with the runs that are. Nine arms
#: at this size is ~120 managed autorater calls; the gated numbers on the
#: scorecard still come from the full n=70 internal run.
DEFAULT_SAMPLE_SIZE = 8
SAMPLE_SEED = 20260812

#: Requests per second asked of the evaluation service. Low on purpose — other
#: jobs share this project's autorater quota, and a managed showcase that
#: 429s the gated run is a bad trade.
DEFAULT_QPS = 4.0

COMPLETED = "completed"
#: The managed service could not be reached. Never a pass, never a number.
UNAVAILABLE = "unavailable"

CLUSTERED = "clustered"
#: The loss-analysis LRO finished and returned no clusters. Reported as its own
#: state: it is not the same claim as "there are no defects".
NO_CLUSTERS = "no_clusters_returned"
LOSS_UNAVAILABLE = "unavailable"
LOSS_NOT_REQUESTED = "not_requested"


def default_vertex_eval_path() -> Path:
    return REPO_ROOT / "artifacts" / "results" / "vertex_eval.json"


def default_dataset_dir() -> Path:
    return REPO_ROOT / "datasets"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# the two kinds of figure — deliberately two types, never one
# --------------------------------------------------------------------------


class ManagedFigure(_Strict):
    """One metric, as the **managed service** measured it.

    ``source`` is frozen. A managed figure cannot be relabelled as an internal
    one after the fact, which is the only way the two could end up in the same
    column by accident.
    """

    source: Literal["vertex_gen_ai_evaluation_service"] = Field(
        default=MANAGED_SOURCE, frozen=True
    )
    #: The service's metric name, verbatim, e.g. ``general_quality_v1``.
    metric: str
    #: What kind of instrument it is, for a reader who does not know the
    #: service's metric catalogue.
    instrument: str
    #: Whose rubrics were scored. ``None`` for metrics that take no rubrics.
    rubric_source: str | None = None
    mean_score: float | None = None
    stdev_score: float | None = None
    pass_rate: float | None = None
    cases_total: int = 0
    cases_valid: int = 0
    cases_error: int = 0
    #: Items kept out of the managed dataset entirely, and why. An errored
    #: generation is not a bad answer, so it is excluded rather than scored.
    excluded: dict[str, int] = Field(default_factory=dict)


class InternalFigure(_Strict):
    """One metric, as the **internal harness** measured it — read, not recomputed.

    Copied verbatim out of ``phase2.json`` so the side-by-side view cannot
    drift from the artifact the gates were checked against. Nothing here is
    recalculated; if it disagrees with the scorecard, the scorecard is right
    and this is a bug.
    """

    source: Literal["internal_harness"] = Field(default=INTERNAL_SOURCE, frozen=True)
    metric: str
    instrument: str
    point: float | None = None
    ci_lo: float | None = None
    ci_hi: float | None = None
    n: int = 0
    #: "core" or "all" for judged metrics; None for deterministic ones.
    split: str | None = None
    #: The artifact the figure was read out of.
    read_from: str | None = None


# --------------------------------------------------------------------------
# loss clusters
# --------------------------------------------------------------------------


class LossCluster(_Strict):
    """One named loss pattern the managed loss analysis grouped failures into."""

    cluster_id: str | None = None
    l1_category: str | None = None
    l2_category: str | None = None
    description: str | None = None
    item_count: int | None = None
    #: Rubric ids from our own rubric group that the examples in this cluster
    #: failed. This is what makes a cluster actionable rather than a label.
    failed_rubric_ids: list[str] = Field(default_factory=list)


class LossClusterReport(_Strict):
    """The managed loss analysis for one arm, or the reason there is none."""

    source: Literal["vertex_gen_ai_evaluation_service"] = Field(
        default=MANAGED_SOURCE, frozen=True
    )
    metric: str
    candidate: str
    region: str = LOSS_ANALYSIS_REGION
    status: str = LOSS_NOT_REQUESTED
    reason: str | None = None
    analysis_time: str | None = None
    #: What the clustering was given to work with, counted off the managed
    #: service's own rubric verdicts. Without these, "no clusters returned" is
    #: unreadable: it could mean the arm was clean or it could mean the
    #: analysis found nothing in a pile of failures, and those are opposite
    #: findings.
    cases_analysed: int | None = None
    failing_verdicts: int | None = None
    clusters: list[LossCluster] = Field(default_factory=list)


# --------------------------------------------------------------------------
# one arm: both instruments, side by side, never merged
# --------------------------------------------------------------------------


class ArmEvaluation(_Strict):
    """One (subagent, arm), measured twice by two instruments.

    Note what is absent: this class owns no score of its own. There is no
    ``combined``, no ``consensus``, no ``mean_of_both``. The only numbers here
    live inside a :class:`ManagedFigure` or an :class:`InternalFigure`, and
    each of those says which instrument produced it.
    """

    subagent: str
    arm: str
    #: The model that produced the outputs being scored, from phase2.json.
    model: str | None = None
    #: How the arm emits: "tool" or "response_schema", from phase2.json.
    output_mode: str | None = None
    items_submitted: int = 0
    item_ids: list[str] = Field(default_factory=list)
    managed: list[ManagedFigure] = Field(default_factory=list)
    internal: list[InternalFigure] = Field(default_factory=list)
    loss_clusters: LossClusterReport | None = None
    notes: list[str] = Field(default_factory=list)


class VertexEvalResult(_Strict):
    """``artifacts/results/vertex_eval.json``.

    A separate file from ``phase2.json`` on purpose, for the same reason
    ``crosscheck.json`` is: the gated numbers live there, nothing in here is
    allowed to touch them, and two files is the cheapest way to make that true
    rather than merely intended.
    """

    vertex_eval_version: str = VERTEX_EVAL_VERSION
    separation_rule: str = SEPARATION_RULE
    customer: str
    #: ``completed`` or ``unavailable``. Never anything in between.
    status: str = UNAVAILABLE
    #: Set iff status is ``unavailable``. One sentence, on screen, no numbers.
    skip_reason: str | None = None
    provenance: str = "unknown"
    #: GCP project the managed service was called in. None when it was not.
    project: str | None = None
    #: Where the metrics ran, and where loss analysis ran. They differ: loss
    #: analysis is global-only.
    eval_region: str | None = None
    loss_analysis_region: str = LOSS_ANALYSIS_REGION
    sdk_version: str | None = None
    quality_metric: str = QUALITY_METRIC
    tool_use_metric: str = TOOL_USE_METRIC
    rubric_group: str = RUBRIC_GROUP
    sample_size: int = DEFAULT_SAMPLE_SIZE
    sample_seed: int = SAMPLE_SEED
    #: Per subagent: which items were submitted and out of what pool.
    scope: dict[str, str] = Field(default_factory=dict)
    #: The internal artifact the internal-harness column was read from.
    internal_artifact: str | None = None
    gates_version_hash: str | None = None
    #: Wall clock of the managed run. None when nothing ran.
    run_started: str | None = None
    #: Ground rule 1: the outputs scored here are recordings, and the artifact
    #: says when those calls were actually made.
    recorded_from: str | None = None
    recorded_to: str | None = None
    arms: list[ArmEvaluation] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def for_arm(self, subagent: str, arm: str) -> ArmEvaluation | None:
        for entry in self.arms:
            if entry.subagent == subagent and entry.arm == arm:
                return entry
        return None


# --------------------------------------------------------------------------
# turning recorded traces into the managed service's dataset shape
# --------------------------------------------------------------------------


def _candidate_text(trace: Trace) -> str | None:
    """The arm's answer, rendered for the service. None when there was none.

    Mirrors :func:`amw.eval.runner.judge_candidate`: prefer the structured
    payload, fall back to text, and return ``None`` only when the call
    genuinely produced nothing. A ``None`` here removes the item from the
    managed dataset with a reason — it is never sent as an empty answer, which
    the service would grade as a bad one.
    """
    if trace.status != "ok":
        return None
    if trace.output.json_ is not None:
        return json.dumps(trace.output.json_, sort_keys=True)
    text = trace.output.text
    return text if text and text.strip() else None


def _rubric_rows(item: DatasetItem) -> list[dict[str, Any]]:
    """The item's rubric in the service's ``Rubric`` shape.

    :func:`amw.eval.runner.rubric_of` is the single source, so the managed
    metric scores exactly the criteria the internal judge scored — including
    the Feature Extractor label criteria that were rerouted out of exact match.
    """
    return [
        {
            "rubric_id": criterion.id,
            "content": {"property": {"description": criterion.text}},
            "type": RUBRIC_TYPE,
        }
        for criterion in rubric_of(item).criteria
    ]


def _tool_events(item_id: str, trace: Trace) -> list[dict[str, Any]]:
    """The recorded tool calls as the service's ``Event`` shape."""
    return [
        {
            "event_id": f"{item_id}-tool-{index}",
            "author": "model",
            "content": {
                "role": "model",
                "parts": [{"function_call": {"name": call.name, "args": call.args}}],
            },
        }
        for index, call in enumerate(trace.tool_calls)
    ]


def build_arm_rows(
    triples: Sequence[tuple[DatasetItem, Any, Trace]],
) -> tuple[list[dict[str, Any]], list[str], dict[str, int]]:
    """Recorded (item, request, trace) triples -> managed evaluation rows.

    Returns ``(rows, item_ids, excluded)``. ``excluded`` counts what did not go
    to the service and why; an excluded item has no managed score at all,
    rather than a zero — an errored generation is a gap in the recording, not a
    bad answer, and scoring it zero would move a managed mean for a reason that
    has nothing to do with quality.

    The prompt sent to the autorater is the one the model was actually given:
    the recorded ``messages`` and ``context_chunks``, with the arm's own system
    prompt as ``instruction``. Grading the answer against a paraphrase of the
    question would make the managed column measure a different task than the
    internal one.

    ``agent_data`` restates the same call as a one-turn conversation, because
    the loss-analysis API rejects any result whose cases carry no conversation
    turns. Both views are the *recorded* call; nothing is synthesised.
    """
    rows: list[dict[str, Any]] = []
    item_ids: list[str] = []
    excluded: dict[str, int] = {}

    def _drop(reason: str) -> None:
        excluded[reason] = excluded.get(reason, 0) + 1

    for item, request, trace in triples:
        answer = _candidate_text(trace)
        if answer is None:
            _drop(
                "the recorded call errored or returned nothing"
                if trace.status != "ok"
                else "the recorded call returned an empty answer"
            )
            continue

        events = _tool_events(item.item_id, trace)
        prompt_text = "\n\n".join(
            [*trace.input.messages, *trace.input.context_chunks]
        )
        turn_events: list[dict[str, Any]] = [
            {
                "author": "user",
                "content": {"role": "user", "parts": [{"text": prompt_text}]},
            }
        ]
        turn_events.extend({k: v for k, v in e.items() if k != "event_id"} for e in events)
        turn_events.append(
            {"author": "model", "content": {"role": "model", "parts": [{"text": answer}]}}
        )

        rows.append(
            {
                "prompt": prompt_text,
                "instruction": getattr(request, "system_prompt", "") or "",
                "response": answer,
                "reference": json.dumps(item.gold, sort_keys=True),
                "intermediate_events": events,
                "agent_data": {
                    "turns": [
                        {"turn_index": 0, "turn_id": "turn_0", "events": turn_events}
                    ]
                },
                "rubric_groups": {RUBRIC_GROUP: _rubric_rows(item)},
            }
        )
        item_ids.append(item.item_id)

    return rows, item_ids, excluded


# --------------------------------------------------------------------------
# reading the internal harness's own numbers, verbatim
# --------------------------------------------------------------------------


def internal_figures_for(
    phase2: Phase2Result, subagent: str, arm: str, *, read_from: str | None = None
) -> list[InternalFigure]:
    """The internal-harness figures for one arm, copied out of ``phase2.json``.

    Two of them, chosen to face the two managed metrics:

    * ``judge_score`` — the internal rubric-judged quality, facing
      ``general_quality_v1``;
    * ``json_schema_validity`` — the internal harness's whole tool-calling
      instrument, facing ``tool_use_quality_v1``. Putting them side by side is
      the point of the exercise: one says the payload parsed, the other says
      whether the call was a good call, and the gap between those two claims is
      what "Level 2" means.
    """
    match = next(
        (a for a in phase2.arms if a.subagent == subagent and a.variant == arm), None
    )
    if match is None:
        return []

    figures: list[InternalFigure] = []
    if match.judge is not None:
        estimate = match.judge.estimate
        figures.append(
            InternalFigure(
                metric="judge_score",
                instrument="rubric_judged_quality",
                point=match.judge.point,
                ci_lo=estimate.lo if estimate else None,
                ci_hi=estimate.hi if estimate else None,
                n=match.judge.items_scored,
                split=match.judge.split,
                read_from=read_from,
            )
        )
    validity = match.metrics.get("json_schema_validity")
    if validity is not None:
        figures.append(
            InternalFigure(
                metric="json_schema_validity",
                instrument="deterministic_output_contract",
                point=validity.point,
                ci_lo=validity.estimate.lo if validity.estimate else None,
                ci_hi=validity.estimate.hi if validity.estimate else None,
                n=validity.n,
                read_from=read_from,
            )
        )
    return figures


# --------------------------------------------------------------------------
# the managed client — acquired in one place, and allowed to be absent
# --------------------------------------------------------------------------


class _Unavailable(Exception):
    """The managed service cannot be used. Carries the one-sentence reason."""


def _sdk_version() -> str | None:
    try:
        from importlib.metadata import version

        return version("google-cloud-aiplatform")
    except Exception:  # noqa: BLE001 - a missing version is not a failure
        return None


def _make_client(project: str, location: str):
    """A ``vertexai.Client``, or :class:`_Unavailable` with the reason.

    Imported here rather than at module scope so that importing this module
    needs neither the evaluation extra nor credentials — ground rule 4, and the
    reason ``cli.py e2e --mode replay`` can still import it offline.
    """
    try:
        import vertexai
    except Exception as exc:  # noqa: BLE001
        raise _Unavailable(
            "the Vertex Gen AI Evaluation SDK is not installed "
            f"(pip install 'google-cloud-aiplatform[evaluation]'): {exc}"
        ) from exc
    try:
        return vertexai.Client(project=project, location=location)
    except Exception as exc:  # noqa: BLE001
        raise _Unavailable(
            f"could not open a Vertex client for project {project!r} in "
            f"{location!r}: {exc}"
        ) from exc


def _resolve_project(cfg: AppConfig, project: str | None) -> str:
    resolved = project or os.environ.get("PROJECT_ID")
    if not resolved:
        raise _Unavailable(
            "no GCP project is configured ($PROJECT_ID is unset), so the "
            "managed evaluation service was not called."
        )
    return resolved


def _resolve_region(cfg: AppConfig, region: str | None) -> str:
    # Same precedence the Gemini adapter uses, then the customer profile, so
    # the managed autorater runs where the rest of the Gemini traffic runs.
    return region or os.environ.get("REGION") or cfg.customer.region


# --------------------------------------------------------------------------
# calling the service
# --------------------------------------------------------------------------


def _summary_to_managed(
    result: Any, metric: str, *, instrument: str, rubric_source: str | None,
    excluded: dict[str, int],
) -> ManagedFigure:
    """One ``AggregatedMetricResult`` -> one :class:`ManagedFigure`.

    Everything is read off the service's own aggregate. Nothing is recomputed
    here: a mean this file calculated itself would be our number wearing the
    managed service's label, which is the exact confusion the whole module is
    built to prevent.
    """
    summary = next(
        (
            s
            for s in (result.summary_metrics or [])
            if getattr(s, "metric_name", None) == metric
        ),
        None,
    )
    if summary is None:
        return ManagedFigure(
            metric=metric,
            instrument=instrument,
            rubric_source=rubric_source,
            excluded=dict(excluded),
        )
    return ManagedFigure(
        metric=metric,
        instrument=instrument,
        rubric_source=rubric_source,
        mean_score=summary.mean_score,
        stdev_score=summary.stdev_score,
        pass_rate=summary.pass_rate,
        cases_total=summary.num_cases_total or 0,
        cases_valid=summary.num_cases_valid or 0,
        cases_error=summary.num_cases_error or 0,
        excluded=dict(excluded),
    )


def _collect_clusters(response: Any) -> tuple[list[LossCluster], str | None]:
    clusters: list[LossCluster] = []
    for analysis in getattr(response, "results", None) or []:
        for cluster in getattr(analysis, "clusters", None) or []:
            taxonomy = getattr(cluster, "taxonomy_entry", None)
            failed: list[str] = []
            for example in getattr(cluster, "examples", None) or []:
                for rubric in getattr(example, "failed_rubrics", None) or []:
                    rubric_id = getattr(rubric, "rubric_id", None)
                    if rubric_id and rubric_id not in failed:
                        failed.append(rubric_id)
            clusters.append(
                LossCluster(
                    cluster_id=getattr(cluster, "cluster_id", None),
                    l1_category=getattr(taxonomy, "l1_category", None),
                    l2_category=getattr(taxonomy, "l2_category", None),
                    description=getattr(taxonomy, "description", None),
                    item_count=getattr(cluster, "item_count", None),
                    failed_rubric_ids=failed,
                )
            )
    return clusters, getattr(response, "analysis_time", None)


def _verdict_counts(eval_result: Any, metric: str) -> tuple[int, int]:
    """``(cases, failing rubric verdicts)``, counted off the service's output.

    Nothing is scored here — these are tallies of the managed autorater's own
    boolean verdicts, and they exist only so that "no clusters returned" can be
    read correctly.

    **A failing verdict arrives as ``None``, not ``False``.** The wire format is
    proto3, which omits fields at their default, so a failed rubric comes back
    with no ``verdict`` key at all and the SDK parses it to ``None``. Counting
    ``is False`` therefore reports zero failures on an arm the service actually
    marked down — verified against the service's own per-case ``score``, which
    equals ``true_verdicts / total_verdicts`` exactly when ``None`` is counted
    as a failure. This is the single most dangerous line in the module: getting
    it wrong makes "no clusters over 0 failures" look like a clean bill of
    health.
    """
    cases = 0
    failing = 0
    for case in getattr(eval_result, "eval_case_results", None) or []:
        cases += 1
        for candidate in getattr(case, "response_candidate_results", None) or []:
            result = (getattr(candidate, "metric_results", None) or {}).get(metric)
            for verdict in getattr(result, "rubric_verdicts", None) or []:
                if verdict.verdict is not True:
                    failing += 1
    return cases, failing


def _run_loss_clusters(
    loss_client: Any, eval_result: Any, *, metric: str, candidate: str
) -> LossClusterReport:
    """Loss analysis for one arm, with "found nothing" kept distinct from "failed".

    The SDK raises a bare ``RuntimeError`` when the long-running operation
    completes with an empty response. Observed on this corpus: the operation
    returns ``done: true`` with no error and no response even when the managed
    autorater recorded dozens of failing rubric verdicts, so an empty result
    here is *not* evidence that the arm was clean. That is why the report
    carries :attr:`LossClusterReport.failing_verdicts` beside the status —
    "returned no clusters over 26 failing verdicts" is a statement about the
    tool, and "returned no clusters over 0 failing verdicts" is a statement
    about the arm, and only the counts tell them apart.
    """
    cases, failing = _verdict_counts(eval_result, metric)
    try:
        response = loss_client.evals.generate_loss_clusters(
            eval_result=eval_result, metric=metric, candidate=candidate
        )
    except RuntimeError as exc:
        message = str(exc)
        if "no response" in message:
            return LossClusterReport(
                metric=metric,
                candidate=candidate,
                status=NO_CLUSTERS,
                cases_analysed=cases,
                failing_verdicts=failing,
                reason=(
                    f"the loss-analysis operation completed with no error and no "
                    f"response over {cases} case(s) carrying {failing} failing "
                    f"rubric verdict(s). This is not a claim that nothing failed: "
                    f"the failures are listed in the rubric verdicts above, the "
                    f"service simply returned no clusters for them."
                ),
            )
        return LossClusterReport(
            metric=metric,
            candidate=candidate,
            status=LOSS_UNAVAILABLE,
            cases_analysed=cases,
            failing_verdicts=failing,
            reason=message,
        )
    except Exception as exc:  # noqa: BLE001 - any failure is "no clusters measured"
        return LossClusterReport(
            metric=metric,
            candidate=candidate,
            status=LOSS_UNAVAILABLE,
            cases_analysed=cases,
            failing_verdicts=failing,
            reason=f"{type(exc).__name__}: {exc}",
        )

    clusters, analysis_time = _collect_clusters(response)
    return LossClusterReport(
        metric=metric,
        candidate=candidate,
        status=CLUSTERED if clusters else NO_CLUSTERS,
        cases_analysed=cases,
        failing_verdicts=failing,
        reason=(
            None
            if clusters
            else (
                f"the loss-analysis operation returned a response containing no "
                f"clusters, over {cases} case(s) carrying {failing} failing "
                f"rubric verdict(s)."
            )
        ),
        analysis_time=analysis_time,
        clusters=clusters,
    )


def _eligible_items(
    items: Sequence[DatasetItem], phase2: Phase2Result, subagent: str
) -> tuple[list[DatasetItem], str]:
    """The pool to sample from: exactly the split the internal judge scored.

    Scoring an item the internal judge never saw would put a managed number
    beside an empty cell, which is the one thing a side-by-side table must not
    do. Read off the artifact rather than assumed, because the split differs by
    subagent.
    """
    splits = {
        arm.judge.split
        for arm in phase2.arms
        if arm.subagent == subagent and arm.judge is not None
    }
    if not splits:
        return [], "unjudged"
    if len(splits) > 1:
        raise ConfigError(
            f"{subagent}: the artifact's arms were judged on different splits "
            f"({sorted(splits)}); there is no single pool to sample from."
        )
    split = splits.pop()
    if split == "all":
        return list(items), split
    return [item for item in items if item.core], split


def _load_dataset(subagent: str, *, dataset_dir: Path) -> list[DatasetItem]:
    path = dataset_dir / f"{subagent}.jsonl"
    if not path.exists():
        raise FileNotFoundError(
            f"no dataset at {path}. The managed evaluation re-scores a corpus "
            f"that has already been run; it does not create one."
        )
    return list(read_items(path))


def run_vertex_eval(
    *,
    customer: str | None = None,
    config: AppConfig | None = None,
    phase2: Phase2Result | None = None,
    results_path: str | Path | None = None,
    subagents: Sequence[str] | None = None,
    variants: Sequence[str] | None = None,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    sample_seed: int = SAMPLE_SEED,
    dataset_dir: str | Path | None = None,
    out_path: str | Path | None = None,
    write: bool = True,
    loss_clusters: bool = True,
    project: str | None = None,
    region: str | None = None,
    qps: float = DEFAULT_QPS,
    router: AdapterRouter | None = None,
    client: Any = None,
    loss_client: Any = None,
    progress: bool = True,
) -> VertexEvalResult:
    """Score the recorded outputs with the managed service; write the artifact.

    This is the whole entry point — a CLI handler needs nothing but this call
    and the returned :class:`VertexEvalResult`.

    Generations are always replayed, never re-run: the managed service scores
    the same answers the scorecard reports on. Only the *scoring* is live, and
    even that is allowed to be absent — if the SDK, the credentials or the
    service are missing, this returns a result with
    ``status == "unavailable"``, a stated reason and no figures, rather than
    raising or inventing anything.
    """
    cfg = config or load_all(customer=customer)
    subagents = tuple(subagents or SUBAGENTS)
    variants = tuple(variants or VARIANTS)
    dataset_dir = Path(dataset_dir) if dataset_dir else default_dataset_dir()

    if phase2 is None:
        path = Path(results_path) if results_path else None
        if path is None:
            raise ValueError(
                "run_vertex_eval needs the internal artifact it reports beside: "
                "pass phase2= or results_path=. The eligible item pool and the "
                "internal-harness column both come from there."
            )
        phase2 = Phase2Result.model_validate_json(path.read_text())

    result = VertexEvalResult(
        customer=cfg.customer.customer,
        provenance=phase2.provenance,
        sample_size=sample_size,
        sample_seed=sample_seed,
        sdk_version=_sdk_version(),
        internal_artifact=str(results_path) if results_path else None,
        gates_version_hash=cfg.gates_version_hash,
        notes=[SEPARATION_RULE],
    )

    try:
        resolved_project = _resolve_project(cfg, project)
        resolved_region = _resolve_region(cfg, region)
        client = client or _make_client(resolved_project, resolved_region)
        loss_client = (
            loss_client
            if loss_client is not None
            else (
                client
                if resolved_region == LOSS_ANALYSIS_REGION
                else _make_client(resolved_project, LOSS_ANALYSIS_REGION)
            )
        )
    except _Unavailable as exc:
        result.skip_reason = str(exc)
        result.notes.append(
            "The managed evaluation service was not called, so this artifact "
            "carries no managed figures. Nothing was substituted for them."
        )
        if write:
            _write(result, out_path)
        return result

    result.status = COMPLETED
    result.project = resolved_project
    result.eval_region = resolved_region
    result.run_started = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Generations: replay only. This instrument re-scores what was recorded.
    router = router or AdapterRouter(mode="replay", models=cfg.models)

    from vertexai._genai import types as vertex_types  # local: see _make_client

    metric_config = vertex_types.EvaluateMethodConfig(evaluation_service_qps=qps)

    for subagent in subagents:
        items = _load_dataset(subagent, dataset_dir=dataset_dir)
        eligible, split = _eligible_items(items, phase2, subagent)
        if not eligible:
            result.notes.append(
                f"{subagent}: the internal judge scored nothing in the supplied "
                f"artifact, so there is nothing to place a managed number beside."
            )
            continue
        selected = stratified_sample(eligible, size=sample_size, seed=sample_seed)
        result.scope[subagent] = (
            f"{len(selected)} items, stratified by difficulty (seed {sample_seed}), "
            f"drawn from the {len(eligible)} items in the internal judge's "
            f"{split} split of a {len(items)}-item corpus"
        )

        for arm in variants:
            entry = _evaluate_arm(
                subagent,
                arm,
                selected,
                cfg=cfg,
                phase2=phase2,
                router=router,
                client=client,
                loss_client=loss_client,
                metric_config=metric_config,
                vertex_types=vertex_types,
                loss_clusters=loss_clusters,
                internal_read_from=result.internal_artifact,
                progress=progress,
            )
            result.arms.append(entry)

    window = router.served_window()
    if window is not None:
        result.recorded_from = window[0].isoformat(timespec="seconds")
        result.recorded_to = window[1].isoformat(timespec="seconds")
    else:
        result.notes.append(
            "no recordings were served for these arms, so the managed service "
            "scored nothing and no recording window can be stated."
        )

    if write:
        _write(result, out_path)
    return result


def _evaluate_arm(
    subagent: str,
    arm: str,
    items: Sequence[DatasetItem],
    *,
    cfg: AppConfig,
    phase2: Phase2Result,
    router: AdapterRouter,
    client: Any,
    loss_client: Any,
    metric_config: Any,
    vertex_types: Any,
    loss_clusters: bool,
    internal_read_from: str | None,
    progress: bool,
) -> ArmEvaluation:
    """One arm through the managed service, beside its internal figures."""
    match = next(
        (a for a in phase2.arms if a.subagent == subagent and a.variant == arm), None
    )
    entry = ArmEvaluation(
        subagent=subagent,
        arm=arm,
        model=match.model if match else None,
        output_mode=match.output_mode if match else None,
        internal=internal_figures_for(
            phase2, subagent, arm, read_from=internal_read_from
        ),
    )

    requests = [
        build_request(
            subagent, arm, prompt_view(item), models=cfg.models, item_id=item.item_id
        )
        for item in items
    ]
    traces = router.complete_many(requests)

    tool_arm = arm in TOOL_CALLING_ARMS
    if not tool_arm:
        entry.notes.append(
            f"{TOOL_USE_METRIC} was not run on this arm: it emits via "
            f"response_schema and issues no tool call, so there is no tool-calling "
            f"behaviour to score. Not a zero — an absence."
        )

    rows, item_ids, excluded = build_arm_rows(list(zip(items, requests, traces)))
    entry.items_submitted = len(rows)
    entry.item_ids = item_ids
    if not rows:
        entry.notes.append(
            "no recorded output for this arm could be scored; the managed "
            "service was not called for it."
        )
        return entry

    metrics = [
        vertex_types.Metric(name=QUALITY_METRIC, rubric_group_name=RUBRIC_GROUP)
    ]
    if tool_arm:
        metrics.append(vertex_types.Metric(name=TOOL_USE_METRIC))

    if progress:
        print(
            f"  {subagent}/{arm}: {len(rows)} recorded outputs -> "
            f"{', '.join(m.name for m in metrics)}"
        )

    try:
        import pandas as pd

        eval_result = client.evals.evaluate(
            dataset=vertex_types.EvaluationDataset(
                eval_dataset_df=pd.DataFrame(rows), candidate_name=arm
            ),
            metrics=metrics,
            config=metric_config,
        )
    except Exception as exc:  # noqa: BLE001 - a failed arm is a stated gap
        entry.notes.append(
            f"the managed evaluation call failed for this arm and it therefore "
            f"has no managed figures: {type(exc).__name__}: {exc}"
        )
        return entry

    entry.managed.append(
        _summary_to_managed(
            eval_result,
            QUALITY_METRIC,
            instrument="rubric_based_quality",
            rubric_source=(
                "amw dataset rubric (amw.eval.runner.rubric_of) — the same "
                "criteria the internal judge scored"
            ),
            excluded=excluded,
        )
    )
    if tool_arm:
        entry.managed.append(
            _summary_to_managed(
                eval_result,
                TOOL_USE_METRIC,
                instrument="tool_use_quality",
                rubric_source=None,
                excluded=excluded,
            )
        )

    if loss_clusters:
        entry.loss_clusters = _run_loss_clusters(
            loss_client, eval_result, metric=QUALITY_METRIC, candidate=arm
        )
    return entry


def _write(result: VertexEvalResult, out_path: str | Path | None) -> Path:
    path = Path(out_path) if out_path else default_vertex_eval_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(result.model_dump_json(indent=2) + "\n")
    return path


# --------------------------------------------------------------------------
# reading it back — the notebook and the report layer
# --------------------------------------------------------------------------


def load_result(path: str | Path | None = None) -> VertexEvalResult | None:
    """The artifact, or ``None`` when it has not been produced.

    ``None`` rather than an empty result on purpose: "the managed showcase has
    not been run" and "the managed showcase measured nothing" are different
    statements, and the display layer says which one it is.
    """
    target = Path(path) if path else default_vertex_eval_path()
    if not target.is_file():
        return None
    return VertexEvalResult.model_validate_json(target.read_text())


def managed_banner(result: VertexEvalResult | None) -> str:
    """One line for the top of a notebook cell or a report section."""
    if result is None:
        return (
            "MANAGED EVALUATION NOT RUN — no artifact at "
            f"{default_vertex_eval_path()}. Nothing below is a measurement."
        )
    if result.status != COMPLETED:
        return (
            "MANAGED EVALUATION UNAVAILABLE — "
            f"{result.skip_reason or 'no reason recorded'} No managed figure is "
            "shown, and none was substituted."
        )
    window = (
        f"recorded {result.recorded_from} to {result.recorded_to}"
        if result.recorded_from
        else "recording window not stated"
    )
    return (
        f"VERTEX GEN AI EVALUATION SERVICE — {result.quality_metric} and "
        f"{result.tool_use_metric} over outputs {window}; metrics in "
        f"{result.eval_region}, loss analysis in {result.loss_analysis_region}; "
        f"run {result.run_started}. {SEPARATION_RULE}"
    )


def side_by_side_frame(result: VertexEvalResult | None):
    """One row per (arm, metric), with the instrument named in its own column.

    Long rather than wide on purpose. A wide frame would need one column per
    metric and would invite a reader — or a future edit — to subtract two of
    them; in this shape the two instruments are separate rows that happen to be
    adjacent, which is exactly what they are.
    """
    import pandas as pd

    columns = [
        "subagent",
        "arm",
        "source",
        "instrument",
        "metric",
        "point",
        "lo",
        "hi",
        "n",
        "detail",
    ]
    if result is None or result.status != COMPLETED:
        return pd.DataFrame(columns=columns)

    rows: list[dict[str, Any]] = []
    for entry in result.arms:
        for figure in entry.managed:
            rows.append(
                {
                    "subagent": entry.subagent,
                    "arm": entry.arm,
                    "source": figure.source,
                    "instrument": figure.instrument,
                    "metric": figure.metric,
                    "point": figure.mean_score,
                    "lo": None,
                    "hi": None,
                    "n": figure.cases_valid,
                    "detail": (
                        f"pass_rate={figure.pass_rate}, "
                        f"errors={figure.cases_error}"
                    ),
                }
            )
        for figure in entry.internal:
            rows.append(
                {
                    "subagent": entry.subagent,
                    "arm": entry.arm,
                    "source": figure.source,
                    "instrument": figure.instrument,
                    "metric": figure.metric,
                    "point": figure.point,
                    "lo": figure.ci_lo,
                    "hi": figure.ci_hi,
                    "n": figure.n,
                    "detail": f"split={figure.split}" if figure.split else "",
                }
            )
    return pd.DataFrame(rows, columns=columns)


def loss_cluster_frame(result: VertexEvalResult | None):
    """The managed loss clusters, one row per cluster, arms with none included.

    An arm whose analysis produced nothing keeps its row with a ``status`` and
    no counts, for the same reason an unmeasured rung keeps its row in the
    ablation chart: a missing row reads as "clean" and an empty one reads as
    "not measured", and only the second is true.
    """
    import pandas as pd

    columns = [
        "subagent",
        "arm",
        "source",
        "status",
        "failing_verdicts",
        "l1_category",
        "l2_category",
        "items",
        "failed_rubrics",
        "description",
    ]
    if result is None or result.status != COMPLETED:
        return pd.DataFrame(columns=columns)

    rows: list[dict[str, Any]] = []
    for entry in result.arms:
        report = entry.loss_clusters
        if report is None:
            rows.append(
                {
                    "subagent": entry.subagent,
                    "arm": entry.arm,
                    "source": MANAGED_SOURCE,
                    "status": LOSS_NOT_REQUESTED,
                    "failing_verdicts": None,
                    "l1_category": None,
                    "l2_category": None,
                    "items": None,
                    "failed_rubrics": None,
                    "description": None,
                }
            )
            continue
        if not report.clusters:
            rows.append(
                {
                    "subagent": entry.subagent,
                    "arm": entry.arm,
                    "source": report.source,
                    "status": report.status,
                    "failing_verdicts": report.failing_verdicts,
                    "l1_category": None,
                    "l2_category": None,
                    "items": None,
                    "failed_rubrics": None,
                    "description": report.reason,
                }
            )
            continue
        for cluster in report.clusters:
            rows.append(
                {
                    "subagent": entry.subagent,
                    "arm": entry.arm,
                    "source": report.source,
                    "status": report.status,
                    "failing_verdicts": report.failing_verdicts,
                    "l1_category": cluster.l1_category,
                    "l2_category": cluster.l2_category,
                    "items": cluster.item_count,
                    "failed_rubrics": ", ".join(cluster.failed_rubric_ids),
                    "description": cluster.description,
                }
            )
    return pd.DataFrame(rows, columns=columns)


def _fmt(value: float | None) -> str:
    return "not measured" if value is None else f"{value:.4f}"


def render_markdown(result: VertexEvalResult | None) -> str:
    """The managed showcase as a page a human can read, with the rule up top."""
    if result is None:
        return (
            "# Vertex Gen AI Evaluation Service\n\n"
            "Not run — no artifact on disk. No managed figure is available, and "
            "none has been substituted.\n"
        )

    lines = [
        "# Vertex Gen AI Evaluation Service — managed second instrument",
        "",
        SEPARATION_RULE,
        "",
    ]
    if result.status != COMPLETED:
        lines += [
            f"**Status: UNAVAILABLE.** {result.skip_reason}",
            "",
            "No managed number is shown below, because none was measured.",
            "",
        ]
        return "\n".join(lines)

    lines += [
        f"**Metrics:** `{result.quality_metric}` (rubric-based, scoring the "
        f"workbench's own rubric group `{result.rubric_group}`) and "
        f"`{result.tool_use_metric}` (tool-use quality, over recorded tool calls).",
        "",
        f"**Where:** project `{result.project}`, metrics in "
        f"`{result.eval_region}`, loss analysis in "
        f"`{result.loss_analysis_region}` (the loss-analysis API is served only "
        f"from the global endpoint).",
        "",
        "## Side by side — never merged",
        "",
        "| Subagent | Arm | Source | Instrument | Metric | Value | n |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for entry in result.arms:
        for figure in entry.managed:
            lines.append(
                f"| {entry.subagent} | {entry.arm} | `{figure.source}` | "
                f"{figure.instrument} | `{figure.metric}` | "
                f"{_fmt(figure.mean_score)} | {figure.cases_valid} |"
            )
        for figure in entry.internal:
            lines.append(
                f"| {entry.subagent} | {entry.arm} | `{figure.source}` | "
                f"{figure.instrument} | `{figure.metric}` | "
                f"{_fmt(figure.point)} | {figure.n} |"
            )

    lines += ["", "## Loss clusters", ""]
    for entry in result.arms:
        report = entry.loss_clusters
        if report is None:
            continue
        lines.append(f"### {entry.subagent} / {entry.arm} — {report.status}")
        lines.append("")
        if report.failing_verdicts is not None:
            lines += [
                f"- analysed {report.cases_analysed} case(s) carrying "
                f"{report.failing_verdicts} failing rubric verdict(s).",
            ]
        if not report.clusters:
            lines += [f"- {report.reason or 'no clusters returned.'}", ""]
            continue
        for cluster in report.clusters:
            rubrics = ", ".join(cluster.failed_rubric_ids) or "none reported"
            lines += [
                f"- **{cluster.l1_category or 'uncategorised'} / "
                f"{cluster.l2_category or '-'}** — {cluster.item_count} item(s); "
                f"failed rubrics: {rubrics}",
                f"  - {cluster.description or 'no description returned'}",
            ]
        lines.append("")

    notes = [note for note in result.notes if note != SEPARATION_RULE]
    if notes:
        lines += ["## Notes", ""] + [f"- {note}" for note in notes] + [""]
    for subagent, scope in sorted(result.scope.items()):
        lines.append(f"- **{subagent} scope:** {scope}")
    lines += [
        "",
        f"Outputs scored were recorded {result.recorded_from or 'n/a'} to "
        f"{result.recorded_to or 'n/a'}; the managed scoring ran "
        f"{result.run_started or 'n/a'}. Provenance: {result.provenance}. "
        f"Gates version hash: {result.gates_version_hash}.",
        "",
    ]
    return "\n".join(lines)
