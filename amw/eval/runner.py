"""Phase-2 runner: dataset items through every arm, into scored results.

This module is the join between the three Fan-out 2 lanes. Each built against
the same frozen output contract (``amw/agents/schemas.py``) but nothing forced
their *container* shapes to agree, so two small adapters live here rather than
being pushed into either lane:

* :func:`prompt_view` turns a :class:`~amw.datasets.schema.DatasetItem` into the
  mapping the prompt packs render from.
* :func:`rubric_of` turns the item's rubric list into the judge's
  :class:`~amw.eval.judge.Rubric`.

Both are deliberately thin and deliberately here. A dataset item is a record of
a *question and its answer key*; how that question is worded to a model is the
prompt pack's business, and what the judge is handed is the judge's. Making
either lane import the other's shape would have coupled them for no gain.

What an arm is
--------------

An arm is a (subagent, variant) pair. The three variants are the first three
rungs of the ablation ladder:

* ``claude_baseline`` — the customer's prompt, on Claude, emitting via an
  ``emit_*`` tool.
* ``gemini_naive`` — rung A0. The *same bytes* on Gemini, same tool. The
  Claude-vs-A0 delta is therefore a model difference, not a prompt-format or
  mechanism difference.
* ``gemini_tuned_v1`` — rungs A1-A3, with ``response_schema``.

The model each arm runs on comes from ``config/models.yaml`` roles, resolved by
the prompt pack — no model IDs appear here.

What this module will not do
----------------------------

It never invents a number. A call that fails produces a ``status:"error"``
trace and the item drops out of that metric's sample with a recorded reason
(see ``MetricSample.excluded``); it is never scored zero, because a backend that
errored did not answer badly, it did not answer. Likewise a judge repeat that
fails is a missing repeat, not a zero. The counts of what was excluded travel
all the way into ``phase2.json`` so a reader can see the denominator.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from amw.adapters import AdapterRouter, merge_windows
from amw.agents.prompt_packs import VARIANTS, build_request, load_pack
from amw.agents.schemas import SUBAGENTS
from amw.config import AppConfig, load_all
from amw.datasets.schema import DatasetItem, read_items
from amw.eval.judge import Judge, JudgeRequest, JudgeVerdict, Rubric, RubricCriterion
from amw.eval.judge import cluster_failures, verdicts_to_repeat_scores
from amw.eval.metrics import (
    FE_JUDGED_FIELDS,
    MetricOutcome,
    MetricSample,
    aggregate,
    deterministic_metrics,
)
from amw.eval.stats import Estimate, aggregate_repeats, bootstrap_ci
from amw.traces.schema import Trace

__all__ = [
    "PHASE2_VERSION",
    "ArmResult",
    "Phase2Result",
    "prompt_view",
    "rubric_of",
    "run_arm",
    "run_phase2",
    "default_results_path",
]

#: Bumped when the result shape changes, so a stale phase2.json is detectable.
PHASE2_VERSION = "1"

REPO_ROOT = Path(__file__).resolve().parents[2]


def default_results_path() -> Path:
    return REPO_ROOT / "artifacts" / "results" / "phase2.json"


def default_dataset_dir() -> Path:
    return REPO_ROOT / "datasets"


# --------------------------------------------------------------------------
# lane adapters
# --------------------------------------------------------------------------


def prompt_view(item: DatasetItem) -> dict[str, Any]:
    """The subset of an item a prompt pack may see.

    Only the model-facing content: never the gold output, the rubric, the
    difficulty tag or the item id. Leaking any of those would let a model score
    well by reading the answer key rather than by doing the task.

    ``input.messages`` holds raw content turns, so they are joined rather than
    reformatted — the packs own all wording, and the adapters add no glue text
    of their own (``tests/test_adapter_parity.py``). Chunks stay structured so
    each variant can mark them up its own way, which is part of what the tuned
    rung changes.
    """
    pack_key = "document" if item.subagent == "feature_extractor" else "question"
    return {
        pack_key: "\n\n".join(item.input.messages),
        "chunks": [
            {"chunk_id": chunk.chunk_id, "text": chunk.text} for chunk in item.input.chunks
        ],
    }


#: Field -> how to ask a judge whether an open-text label is right.
#:
#: These are the fields :data:`~amw.eval.metrics.FE_JUDGED_FIELDS` took out of
#: exact match. The wording carries the whole point of the change: a different
#: form of words for the same thing is *correct*. Both questions also keep the
#: fabrication check the deterministic metric used to provide — an answer the
#: source does not support scores 0.
_FE_LABEL_CRITERIA: dict[str, str] = {
    "technical_field": (
        "Does technical_field name the subject matter of this document "
        "correctly? The reference label is {gold!r}. A different wording for "
        "the same field is CORRECT — judge the meaning, not the phrasing. "
        "Answer no if it names a different field, is too broad or too narrow "
        "to identify the subject matter, or is not supported by the source."
    ),
    "novelty_statement": (
        "Does novelty_statement state what this document presents as new, "
        "accurately and in one sentence? The reference statement is {gold!r}. "
        "A paraphrase that preserves the substance — including any numeric "
        "limits — is CORRECT. Answer no if it changes or invents a value, "
        "describes something other than the disclosed advance, or asserts "
        "anything the source does not state."
    ),
}


def rubric_of(item: DatasetItem) -> Rubric:
    """The item's rubric in the judge's shape, plus the rerouted FE fields.

    The dataset calls the assertion ``criterion`` and the judge calls it
    ``text``; that is the whole difference for the generator's own criteria.
    ``tag`` is left unset on those because the generator's ``id`` values are
    already semantic slugs that recur across items (``filing_not_priority``
    appears on every item whose source states a priority date), and the judge
    clusters on ``tag`` or falls back to ``id``. Setting ``tag = id`` would add
    a column that says nothing.

    Feature Extractor items additionally get one 0/1 criterion per field in
    :data:`~amw.eval.metrics.FE_JUDGED_FIELDS` that the gold actually states.
    Those fields left the exact-match metric on 2026-08-07 because open text
    cannot be exact-matched; the scoring has to land *somewhere* or a real
    fabrication would go uncaught, so it lands here. They are tagged
    ``fe_field_label`` so triage can separate "labelled it differently" from
    the dataset's own substantive criteria.
    """
    criteria = [
        RubricCriterion(id=criterion.id, text=criterion.criterion)
        for criterion in item.rubric
    ]
    if item.subagent == "feature_extractor":
        for field in FE_JUDGED_FIELDS:
            gold = item.gold.get(field)
            # A null gold means "the source does not state this". There is no
            # label to be right about, and the deterministic metric still scores
            # that case: the field is absent from FE_FIELDS, but asserting a
            # value the source never gave is what the item's own rubric and the
            # other fields' hallucination check cover. Nothing to ask here.
            if gold is None:
                continue
            criteria.append(
                RubricCriterion(
                    id=f"{field}_correct",
                    text=_FE_LABEL_CRITERIA[field].format(gold=gold),
                    tag="fe_field_label",
                )
            )
    return Rubric(item_id=item.item_id, subagent=item.subagent, criteria=criteria)


def judge_candidate(trace: Trace) -> Any:
    """What the judge is shown as the arm's answer for one item.

    A :class:`~amw.traces.schema.TraceOutput` is a container, not an answer:
    it holds a structured payload, a text body, or neither. The judge renders
    whatever it is handed, so it has to be handed the answer itself.

    Prefer the structured payload, and fall back to the text. The fallback is
    the point: a model that ignored the tool and replied in prose *did* answer,
    and showing the judge "produced no output" for it would grade a
    wrong-format answer as a non-answer. That distinction belongs to
    ``json_schema_validity``, which already measures it separately.

    Returns ``None`` only when there is genuinely nothing — an errored call, or
    an empty response — which :func:`~amw.eval.judge._render_payload` turns
    into an explicit absence marker rather than a blank.

    Note this is deliberately laxer than
    :func:`~amw.eval.metrics.extract_payload`, which returns ``None`` for a
    JSON array because it has no fields to score. The judge has a rubric, not
    a field list, and can read an array fine.
    """
    if trace.output.json_ is not None:
        return trace.output.json_
    text = trace.output.text
    return text if text and text.strip() else None


# --------------------------------------------------------------------------
# result shapes
# --------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MetricReport(_Strict):
    """One metric on one arm: the estimate, plus what it was computed over."""

    metric: str
    #: The observed mean. Present whenever anything was measured at all.
    point: float | None = None
    #: The bootstrap interval. Needs n >= 2; ``point`` can be set without it.
    estimate: Estimate | None = None
    n: int
    n_excluded: int
    excluded: dict[str, int] = Field(default_factory=dict)

    @classmethod
    def of(cls, sample: MetricSample, *, seed: int) -> "MetricReport":
        # An empty sample gets no number rather than a zero. "Nothing was
        # measurable" and "everything measured zero" are different findings and
        # a report that conflates them is a fabricated result (ground rule 1).
        if not sample.n:
            return cls(
                metric=sample.metric,
                n=0,
                n_excluded=sample.n_excluded,
                excluded=dict(sample.excluded),
            )

        point = sum(sample.values) / sample.n
        # A single observation has a real mean but no interval — resampling one
        # value 10k times yields [x, x], which reads as a precise measurement
        # and is not one. stats.bootstrap_ci refuses n < 2 for exactly this
        # reason; honour that by reporting the bare value instead of inventing
        # a width. Small-n arms are normal on `-n 2` smoke runs and on metrics
        # that only apply to a few items.
        estimate = (
            bootstrap_ci(sample, metric=sample.metric, seed=seed) if sample.n >= 2 else None
        )
        return cls(
            metric=sample.metric,
            point=point,
            estimate=estimate,
            n=sample.n,
            n_excluded=sample.n_excluded,
            excluded=dict(sample.excluded),
        )


class JudgeReport(_Strict):
    """Rubric-judged quality for one arm, over some split at k repeats."""

    point: float | None = None
    estimate: Estimate | None = None
    #: Which items the judge saw: "core" (the registered default) or "all".
    #: Recorded per arm because judged n differs by split, and a reader
    #: comparing two subagents' judge scores has to be able to see that the
    #: samples are not the same size without going back to the run log.
    split: str = "core"
    items_scored: int
    expected_repeats: int
    failed_repeats: int
    items_short_of_k: list[str] = Field(default_factory=list)
    dropped_items: list[str] = Field(default_factory=list)
    mean_within_item_sd: float | None = None
    full_agreement_rate: float | None = None
    #: rubric criterion id -> item_ids that failed it. The seed for triage.
    failure_clusters: dict[str, list[str]] = Field(default_factory=dict)


class ArmResult(_Strict):
    """One (subagent, variant) arm."""

    subagent: str
    variant: str
    model: str
    output_mode: str
    prompt_sha: str
    items: int
    calls_ok: int
    calls_error: int
    #: Distinct error strings and how often each occurred. Kept because "12
    #: items failed" is unactionable without knowing they were all one quota
    #: error rather than twelve different bugs.
    error_kinds: dict[str, int] = Field(default_factory=dict)
    metrics: dict[str, MetricReport] = Field(default_factory=dict)
    judge: JudgeReport | None = None


class Phase2Result(_Strict):
    """Everything ``cli.py phase2`` produced, including how it was produced.

    The provenance block is not decoration: CLAUDE.md ground rule 2 requires
    every report footer to state provenance, run date, region and gates
    version, and the footer can only do that if the run recorded it here.
    """

    phase2_version: str = PHASE2_VERSION
    customer: str
    mode: str
    region: str
    provenance: str
    dataset_seed: int
    generator_version: str
    bootstrap_seed: int
    judge_repeats: int
    #: None in replay: no wall-clock run happened, the corpus carries its dates.
    run_started: str | None = None
    #: Earliest and latest timestamp of the recordings this run replayed, as
    #: ISO-8601. Ground rule 1: a replayed number has to carry the date its
    #: call was actually made, or a reader has no way to tell it from a fresh
    #: one. None in live mode, where `run_started` is the date that matters.
    recorded_from: str | None = None
    recorded_to: str | None = None
    adapters: dict[str, str] = Field(default_factory=dict)
    judge_model: str | None = None
    judge_prompt_version: str | None = None
    arms: list[ArmResult] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# running one arm
# --------------------------------------------------------------------------


def _error_kind(trace: Trace) -> str:
    """Collapse an error to its type, so counts group usefully."""
    text = (trace.error or "unknown").strip()
    return text.split(":", 1)[0][:80] if ":" in text else text[:80]


def run_arm(
    subagent: str,
    variant: str,
    items: Sequence[DatasetItem],
    *,
    router: AdapterRouter,
    judge: Judge | None = None,
    repeats: int = 2,
    bootstrap_seed: int = 20260812,
    judge_split: str = "core",
    model: str | None = None,
) -> tuple[ArmResult, list[Trace]]:
    """Execute one arm over ``items`` and score it.

    Returns the scored result and the raw traces, so a caller can record or
    inspect them without re-running anything.

    ``judge_split`` is "core" (T08's registered sizing) or "all". Widening it
    multiplies judge calls by roughly 70/28, so it is opt-in per subagent and
    the choice is written into the arm's ``JudgeReport.split``.
    """
    if judge_split not in ("core", "all"):
        raise ValueError(f"judge_split must be 'core' or 'all', not {judge_split!r}")
    pack = load_pack(subagent, variant)
    # ``model`` overrides the variant's role lookup. It is how the same prompt
    # bytes get run against a different model — which is the only way to
    # measure a model swap as a model swap rather than as a prompt change.
    requests = [
        build_request(
            subagent, variant, prompt_view(item), item_id=item.item_id, model=model
        )
        for item in items
    ]
    traces = router.complete_many(requests)

    outcomes: list[MetricOutcome] = []
    errors: Counter[str] = Counter()
    for item, trace in zip(items, traces):
        if trace.status != "ok":
            errors[_error_kind(trace)] += 1
        outcomes.extend(
            deterministic_metrics(
                subagent,
                gold=item.gold,
                # A failed call passes the trace through as-is; the metrics
                # layer decides per metric whether that is a measured miss or
                # an exclusion. It is never turned into a zero here.
                source=trace,
                provided_chunk_ids=item.input.chunk_ids,
                item_id=item.item_id,
            ).values()
        )

    by_metric: dict[str, list[MetricOutcome]] = defaultdict(list)
    for outcome in outcomes:
        by_metric[outcome.metric].append(outcome)

    result = ArmResult(
        subagent=subagent,
        variant=variant,
        model=requests[0].model if requests else pack.model_role,
        output_mode=pack.output_mode,
        prompt_sha=pack.sha256,
        items=len(items),
        calls_ok=sum(1 for t in traces if t.status == "ok"),
        calls_error=sum(1 for t in traces if t.status != "ok"),
        error_kinds=dict(errors),
        metrics={
            name: MetricReport.of(aggregate(group, metric=name), seed=bootstrap_seed)
            for name, group in sorted(by_metric.items())
        },
    )

    if judge is not None:
        judged = [
            (item, trace)
            for item, trace in zip(items, traces)
            if judge_split == "all" or item.core
        ]
        if judged:
            result.judge = _judge_arm(
                subagent,
                judged,
                judge=judge,
                repeats=repeats,
                bootstrap_seed=bootstrap_seed,
                arm=variant,
                split=judge_split,
            )
    return result, list(traces)


def _judge_arm(
    subagent: str,
    judged: Sequence[tuple[DatasetItem, Trace]],
    *,
    judge: Judge,
    repeats: int,
    bootstrap_seed: int,
    arm: str,
    split: str = "core",
) -> JudgeReport:
    requests = [
        JudgeRequest(
            item_id=item.item_id,
            subagent=subagent,
            rubric=rubric_of(item),
            candidate=judge_candidate(trace),
            task_input=list(item.input.messages),
            context_chunks=item.input.context_chunks(),
            reference=item.gold,
            repeat=repeat,
            repeats=repeats,
            arm=arm,
        )
        for item, trace in judged
        for repeat in range(1, repeats + 1)
    ]
    verdicts = judge.score_many(requests)
    aggregated = aggregate_repeats(
        verdicts_to_repeat_scores(verdicts),
        metric="judge_score",
        expected_k=repeats,
    )
    sample = aggregated.to_sample()
    # Same n < 2 rule as MetricReport.of: a mean is reportable from one item,
    # an interval is not.
    return JudgeReport(
        split=split,
        point=(sum(sample.values) / sample.n) if sample.n else None,
        estimate=(
            bootstrap_ci(sample, metric="judge_score", seed=bootstrap_seed)
            if sample.n >= 2
            else None
        ),
        items_scored=sample.n,
        expected_repeats=repeats,
        failed_repeats=aggregated.failed_repeats,
        items_short_of_k=list(aggregated.items_short_of_k),
        dropped_items=list(aggregated.dropped_items),
        mean_within_item_sd=aggregated.mean_within_item_sd,
        full_agreement_rate=aggregated.full_agreement_rate,
        failure_clusters=cluster_failures(verdicts),
    )


# --------------------------------------------------------------------------
# the phase-2 run
# --------------------------------------------------------------------------


def _load_dataset(
    subagent: str, *, dataset_dir: Path, limit: int | None
) -> list[DatasetItem]:
    path = dataset_dir / f"{subagent}.jsonl"
    if not path.exists():
        raise FileNotFoundError(
            f"no dataset at {path}. Run `python cli.py gen --customer <name> -n 70` "
            f"first — phase2 scores a corpus, it does not create one."
        )
    items = list(read_items(path))
    if limit is None:
        return items
    # Take the core split first so a small -n still has something judgeable;
    # otherwise `phase2 -n 10` can silently produce no judged score at all.
    core = [item for item in items if item.core]
    rest = [item for item in items if not item.core]
    return (core + rest)[:limit]


def run_phase2(
    *,
    customer: str | None = None,
    mode: str = "replay",
    n: int | None = None,
    config: AppConfig | None = None,
    subagents: Sequence[str] | None = None,
    variants: Sequence[str] | None = None,
    dataset_dir: str | Path | None = None,
    out_path: str | Path | None = None,
    write: bool = True,
    judge: Judge | None = None,
    run_judge: bool = True,
    router: AdapterRouter | None = None,
    bootstrap_seed: int | None = None,
    judge_all: Sequence[str] = (),
) -> Phase2Result:
    """Run every arm and write ``artifacts/results/phase2.json``.

    ``judge_all`` names subagents to judge on the full corpus instead of the
    core split. T08 registered core-split judging as the sizing for this
    build, so any widening is a deviation and is written into ``notes`` as
    one — an artifact that quietly judged different subagents on different
    sample sizes would invite exactly the wrong comparison.
    """
    cfg = config or load_all(customer=customer)
    subagents = tuple(subagents or SUBAGENTS)
    variants = tuple(variants or VARIANTS)
    dataset_dir = Path(dataset_dir) if dataset_dir else default_dataset_dir()
    repeats = cfg.customer.dataset.judge_repeats
    seed = bootstrap_seed if bootstrap_seed is not None else cfg.customer.seed

    router = router or AdapterRouter(mode=mode, models=cfg.models)
    if run_judge and judge is None:
        judge = Judge(mode=mode, models=cfg.models)

    judge_all = tuple(judge_all)
    unknown = set(judge_all) - set(subagents)
    if unknown:
        raise ValueError(f"judge_all names subagents that are not being run: {sorted(unknown)}")

    datasets = {
        subagent: _load_dataset(subagent, dataset_dir=dataset_dir, limit=n)
        for subagent in subagents
    }
    seeds = {item.seed for items in datasets.values() for item in items}
    versions = {item.generator_version for items in datasets.values() for item in items}
    provenances = {item.provenance for items in datasets.values() for item in items}

    notes: list[str] = []
    if len(seeds) > 1:
        notes.append(
            f"dataset seeds disagree across subagents ({sorted(seeds)}) — the corpus "
            f"was generated in more than one run and is not reproducible as a whole."
        )
    if len(versions) > 1:
        notes.append(
            f"generator versions disagree ({sorted(versions)}) — regenerate before "
            f"showing these numbers to a customer."
        )
    if judge_all:
        notes.append(
            f"judged on the FULL corpus, not the registered core split: "
            f"{', '.join(sorted(judge_all))}. Every other subagent is judged on "
            f"core only, so judged n differs across subagents — see each arm's "
            f"judge.split and judge.items_scored before comparing judge scores."
        )

    result = Phase2Result(
        customer=cfg.customer.customer,
        mode=mode,
        region=cfg.customer.region,
        provenance="/".join(sorted(provenances)) if provenances else "unknown",
        dataset_seed=min(seeds) if seeds else cfg.customer.seed,
        generator_version="/".join(sorted(versions)) if versions else "unknown",
        bootstrap_seed=seed,
        judge_repeats=repeats,
        adapters=router.describe(),
        # Judge.describe() prefixes every key with "judge_" so the scorecard
        # footer can splat it without collisions. Reading "model" here silently
        # produced None on the first real run — the footer would have printed a
        # blank where ground rule 2 requires the judge's identity.
        judge_model=judge.describe().get("judge_model") if judge else None,
        judge_prompt_version=(
            judge.describe().get("judge_prompt_version") if judge else None
        ),
        # Ground rule 2: the footer prints the run date. Replay leaves this None
        # on purpose — no wall-clock run happened, and the recordings carry
        # their own dates.
        run_started=(
            datetime.now(timezone.utc).isoformat(timespec="seconds")
            if mode != "replay"
            else None
        ),
        notes=notes,
    )
    # Populated after the arms run: the window has to describe the calls this
    # run actually replayed, not everything sitting in the store. The store
    # holds spike traces from 08-07 and superseded recordings from earlier
    # runs; dating an artifact by those would make fresh numbers look stale.
    def _stamp_replay_window() -> None:
        if mode == "live":
            return
        window = merge_windows(
            [
                router.served_window(),
                getattr(
                    getattr(judge, "adapter", None), "served_window", lambda: None
                )(),
            ]
        )
        if window is not None:
            result.recorded_from = window[0].isoformat(timespec="seconds")
            result.recorded_to = window[1].isoformat(timespec="seconds")
        elif mode == "replay":
            notes.append(
                "replay mode served no recordings — no number here is a "
                "measurement."
            )

    for subagent in subagents:
        items = datasets[subagent]
        if not items:
            notes.append(f"{subagent}: dataset is empty, no arm was run")
            continue
        for variant in variants:
            arm, _ = run_arm(
                subagent,
                variant,
                items,
                router=router,
                judge=judge,
                repeats=repeats,
                bootstrap_seed=seed,
                judge_split="all" if subagent in judge_all else "core",
            )
            result.arms.append(arm)

    _stamp_replay_window()

    if write:
        path = Path(out_path) if out_path else default_results_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(result.model_dump_json(indent=2) + "\n")
    return result
