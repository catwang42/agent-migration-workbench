"""Assembling the evidence a gate is allowed to be checked against.

``config/gates.yaml`` names six gates. This module decides, per subagent,
which of them the run actually *measured* — and, just as importantly, records
a reason for each one it did not. That asymmetry is the whole design: a gate
with no estimate must never reach :func:`amw.eval.stats.check_gates`, because
a gate that quietly disappears is indistinguishable from a gate that passed.

Where each gate's estimate comes from
-------------------------------------

============================  =========================================
gate                          source
============================  =========================================
``json_schema_validity``      the candidate arm's ``MetricReport.estimate``
                              straight out of ``phase2.json``
``quality_delta_pp``          paired bootstrap of per-item judge scores
``groundedness_delta_pp``     paired bootstrap of per-item citation coverage
``shadow_agreement``          supplied by ``amw.shadow.agreement`` (T11)
``cost_savings_pct``          the economics model — blocked while
                              ``pricing.yaml`` is unverified
``latency_p95``               a **same-region probe** only; see below
============================  =========================================

The two ``*_delta_pp`` gates need per-item vectors, and ``phase2.json`` stores
only per-arm aggregates. :func:`collect_samples` therefore re-executes the
relevant arms in **replay** — no credentials, no new calls, the same recorded
responses the artifact was built from — and then asserts that every
recomputed arm mean equals the artifact's to within
:data:`RECOMPUTE_TOLERANCE`. If they disagree the delta is refused
(:class:`EvidenceMismatchError`) rather than published: a paired delta
computed over a *different* set of calls than the artifact reports is a
fabricated result even though every individual number in it is real.

That recomputation is a stopgap. The clean fix is for
``amw.eval.runner.JudgeReport`` to carry ``item_means`` (it already computes
them, in ``aggregate_repeats``) — see this lane's report.

Latency
-------

Claude ran in ``global`` because ``us-central1`` Model Garden quota was
exhausted; Gemini and the judge ran in ``us-central1``. p95 across those two
is not a comparison, so ``latency_p95`` is **not evaluated** unless the caller
supplies a :class:`SameRegionLatencyProbe`. The probe type refuses to be
constructed with two different regions, which is what makes it structurally
impossible for a cross-region input to produce a passing latency gate.
"""

from __future__ import annotations

import os
from typing import Any, Iterable, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from amw.config import AppConfig
from amw.eval.metrics import MetricSample
from amw.eval.runner import ArmResult, Phase2Result
from amw.eval.stats import Estimate, paired_bootstrap_delta
from amw.shadow.triage import TriageSummary
from amw.reporting.cells import (
    ClaudeSchemaValidityCell,
    JudgeScoreCell,
    REGION_SPLIT_DISCLOSURE,
)

__all__ = [
    "RECOMPUTE_TOLERANCE",
    "BASELINE_VARIANT",
    "CANDIDATE_VARIANT",
    "GATE_QUALITY",
    "GATE_GROUNDEDNESS",
    "GATE_SCHEMA",
    "GATE_SHADOW",
    "GATE_COST",
    "GATE_LATENCY",
    "SHADOW_METRIC_NOTES",
    "UNLABELLED_SHADOW_NOTE",
    "EvidenceMismatchError",
    "ArmSamples",
    "SameRegionLatencyProbe",
    "Regions",
    "SubagentEvidence",
    "collect_samples",
    "build_evidence",
]

#: A recomputed arm mean has to match the artifact bit-for-bit in practice;
#: this only absorbs float summation order.
RECOMPUTE_TOLERANCE = 1e-9

BASELINE_VARIANT = "claude_baseline"
CANDIDATE_VARIANT = "gemini_tuned_v1"

# Gate names as they appear in config/gates.yaml. Named constants rather than
# string literals scattered through the renderer, so a gate rename is a load
# error here instead of a silently-skipped gate downstream.
GATE_QUALITY = "quality_delta_pp"
GATE_GROUNDEDNESS = "groundedness_delta_pp"
GATE_SCHEMA = "json_schema_validity"
GATE_SHADOW = "shadow_agreement"
GATE_COST = "cost_savings_pct"
GATE_LATENCY = "latency_p95"

#: What the ``shadow_agreement`` number in the gate row actually counted. The
#: two figures are not two views of one quantity: `structured` compares the
#: fields that have a defined right answer, `item` additionally requires prose
#: fields to match and scores those with a token-overlap proxy, so it inherits
#: that proxy's threshold. On the n=70 corpus they differ by up to 8x. A gate
#: row showing one of them without saying which is an unattributed number.
SHADOW_METRIC_NOTES: dict[str, str] = {
    "structured": (
        "shadow_agreement counts **structured fields only** — the fields with a "
        "defined right answer. Prose fields are excluded here and adjudicated "
        "separately in the disagreement triage; this figure is not a claim that "
        "the prose matched."
    ),
    "item": (
        "shadow_agreement counts **whole items** — an item agrees only if every "
        "field matches, with prose fields scored by a token-overlap proxy at a "
        "fixed threshold. The figure moves substantially with that threshold, so "
        "it is partly a measurement of the instrument."
    ),
}

#: Used when a caller supplies shadow estimates without saying which figure
#: they are. Better a visible "we did not record this" than a silent one.
UNLABELLED_SHADOW_NOTE = (
    "shadow_agreement was supplied without recording which agreement figure it "
    "counts (structured-fields-only or whole-item). Re-render with "
    "--shadow-metric so the number is attributable."
)

#: gate -> the per-item metric whose paired delta answers it.
#: ``groundedness`` is only defined where a citation instrument exists, i.e.
#: Chunk Summarizer. QR and FE have no groundedness measurement at all, and
#: that shows up as a *missing* gate rather than as a pass.
_DELTA_METRIC: dict[str, str] = {
    GATE_QUALITY: "judge_score",
    GATE_GROUNDEDNESS: "citation_coverage",
}


class EvidenceMismatchError(RuntimeError):
    """A recomputed arm statistic disagrees with the published artifact."""


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ArmSamples(_Base):
    """Per-item vectors for one arm — what a paired bootstrap needs."""

    subagent: str
    variant: str
    #: metric name -> per-item sample, including the judge under "judge_score"
    metrics: dict[str, MetricSample] = Field(default_factory=dict)
    judge_split: str | None = None


class SameRegionLatencyProbe(_Base):
    """The only thing that unlocks the ``latency_p95`` gate on this build.

    Both arms must have been measured in one region. The validator is the
    enforcement point named in the T12 card: a cross-region input cannot be
    turned into a passing latency gate, because it cannot be turned into a
    probe.
    """

    region: str
    #: Candidate p95 with its interval, in milliseconds.
    candidate_p95: Estimate
    #: Measured Claude p95 for the same subagent, same region, same load
    #: profile. Resolves the ``claude_baseline_p95`` sentinel.
    baseline_p95_ms: float
    baseline_region: str
    candidate_region: str
    probed_on: str
    note: str | None = None

    @model_validator(mode="after")
    def _one_region(self) -> "SameRegionLatencyProbe":
        if not (self.region == self.baseline_region == self.candidate_region):
            raise ValueError(
                "a same-region latency probe needs one region: got baseline "
                f"{self.baseline_region!r}, candidate {self.candidate_region!r}, "
                f"declared {self.region!r}. A cross-region p95 is two "
                f"measurements, not a comparison — the scorecard renders "
                f"{REGION_SPLIT_DISCLOSURE!r} for it and leaves the gate "
                "unevaluated."
            )
        if self.candidate_p95.unit != "ms":
            raise ValueError(
                f"latency probe estimate is in {self.candidate_p95.unit!r}; the "
                "latency_p95 gate is in milliseconds"
            )
        return self


class Regions(_Base):
    """Where each arm ran. Not derivable from the artifact — traces carry no
    region field — so it is an explicit input, defaulted from the environment
    the run was configured with rather than guessed."""

    baseline: str
    candidate: str
    source: str = "environment"

    @property
    def split(self) -> bool:
        return self.baseline != self.candidate

    @classmethod
    def from_env(cls, cfg: AppConfig) -> "Regions":
        candidate = os.environ.get("REGION") or cfg.customer.region
        baseline = os.environ.get("CLAUDE_REGION") or candidate
        return cls(
            baseline=baseline,
            candidate=candidate,
            source="$CLAUDE_REGION / $REGION, falling back to the customer profile",
        )


class SubagentEvidence(_Base):
    """Everything the scorecard is allowed to say about one subagent."""

    subagent: str
    baseline_variant: str
    candidate_variant: str
    #: gate name -> estimate, for gates this run measured. Only these are
    #: handed to check_gates(); the rest are surfaced by missing_gates().
    estimates: dict[str, Estimate] = Field(default_factory=dict)
    #: gate name -> why nothing was measured. Every gate not in `estimates`
    #: appears here, so "not evaluated" always comes with a reason.
    unmeasured: dict[str, str] = Field(default_factory=dict)
    sentinel_values: dict[str, float] = Field(default_factory=dict)

    claude_schema_validity: ClaudeSchemaValidityCell | None = None
    candidate_schema_validity: Estimate | None = None
    judge_baseline: JudgeScoreCell | None = None
    judge_candidate: JudgeScoreCell | None = None
    latency_probe: SameRegionLatencyProbe | None = None
    #: The judge-adjudicated disagreement tally, when a shadow run produced
    #: one. This is the evidence for ``shadow_agreement``'s pre-registered
    #: ``alt`` clause and is the *only* thing that can turn that gate's missed
    #: CI bound into a pass; absent it, the gate fails as measured.
    adjudication: TriageSummary | None = None
    #: The same adjudication for the arm this one replaced, when a prior shadow
    #: run was supplied. It is the control: same gate, same corpus, same
    #: baseline, different candidate prompt. Without it "the clause passes" is
    #: a fact about the subagent; with it, it is a fact about the *rung*, which
    #: is the claim the report actually wants to make.
    adjudication_prior: TriageSummary | None = None
    #: Which arm that prior adjudication belongs to. Empty when there is none.
    adjudication_prior_arm: str = ""
    regions: Regions
    notes: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# re-deriving per-item vectors from the recorded corpus
# --------------------------------------------------------------------------


def _arm(phase2: Phase2Result, subagent: str, variant: str) -> ArmResult | None:
    for arm in phase2.arms:
        if arm.subagent == subagent and arm.variant == variant:
            return arm
    return None


def collect_samples(
    cfg: AppConfig,
    phase2: Phase2Result,
    *,
    mode: str = "replay",
    dataset_dir: Any = None,
    arms: Sequence[tuple[str, str]] | None = None,
    verify_against_artifact: bool = True,
) -> dict[tuple[str, str], ArmSamples]:
    """Re-execute arms in ``mode`` to recover per-item vectors.

    Imports of the runner/adapter stack are deferred to call time so that
    merely importing the reporting package costs nothing — the scorecard is
    also rendered from pre-built samples in tests and from notebooks.

    :param verify_against_artifact: cross-check every recomputed arm mean
        against ``phase2.json``. On by default; the only reason to switch it
        off is a fixture that has no artifact to check against.
    """
    from amw.adapters import AdapterRouter
    from amw.agents.prompt_packs import build_request
    from amw.datasets.schema import read_items
    from amw.eval.judge import Judge, JudgeRequest, verdicts_to_repeat_scores
    from amw.eval.metrics import aggregate, deterministic_metrics
    from amw.eval.runner import default_dataset_dir, judge_candidate, prompt_view, rubric_of
    from amw.eval.stats import aggregate_repeats

    from pathlib import Path

    dataset_dir = Path(dataset_dir) if dataset_dir else default_dataset_dir()
    if arms is None:
        arms = [(a.subagent, a.variant) for a in phase2.arms]

    router = AdapterRouter(mode=mode, models=cfg.models)
    judge = Judge(mode=mode, models=cfg.models)
    repeats = cfg.customer.dataset.judge_repeats

    items_by_subagent: dict[str, list] = {}
    out: dict[tuple[str, str], ArmSamples] = {}

    for subagent, variant in arms:
        if subagent not in items_by_subagent:
            items_by_subagent[subagent] = list(read_items(dataset_dir / f"{subagent}.jsonl"))
        items = items_by_subagent[subagent]
        published = _arm(phase2, subagent, variant)

        requests = [
            build_request(subagent, variant, prompt_view(item), item_id=item.item_id)
            for item in items
        ]
        traces = router.complete_many(requests)

        outcomes: list[Any] = []
        for item, trace in zip(items, traces):
            outcomes.extend(
                deterministic_metrics(
                    subagent,
                    gold=item.gold,
                    source=trace,
                    provided_chunk_ids=item.input.chunk_ids,
                    item_id=item.item_id,
                ).values()
            )
        by_metric: dict[str, list[Any]] = {}
        for outcome in outcomes:
            by_metric.setdefault(outcome.metric, []).append(outcome)
        metrics = {
            name: aggregate(group, metric=name) for name, group in sorted(by_metric.items())
        }

        split = published.judge.split if (published and published.judge) else "core"
        judged = [
            (item, trace)
            for item, trace in zip(items, traces)
            if split == "all" or item.core
        ]
        if judged:
            verdicts = judge.score_many(
                [
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
                        arm=variant,
                    )
                    for item, trace in judged
                    for repeat in range(1, repeats + 1)
                ]
            )
            metrics["judge_score"] = aggregate_repeats(
                verdicts_to_repeat_scores(verdicts),
                metric="judge_score",
                expected_k=repeats,
            ).to_sample()

        samples = ArmSamples(
            subagent=subagent, variant=variant, metrics=metrics, judge_split=split
        )
        if verify_against_artifact and published is not None:
            _verify(samples, published)
        out[(subagent, variant)] = samples

    return out


def _verify(samples: ArmSamples, published: ArmResult) -> None:
    """Refuse to publish a delta computed over calls the artifact did not score."""
    for name, report in published.metrics.items():
        if report.point is None:
            continue
        sample = samples.metrics.get(name)
        if sample is None or not sample.n:
            raise EvidenceMismatchError(
                f"{published.subagent}/{published.variant}: the artifact reports "
                f"{name}={report.point:.6f} but the replayed corpus produced no "
                f"sample for it. The artifact and the replay store have diverged."
            )
        got = sum(sample.values) / sample.n
        if abs(got - report.point) > RECOMPUTE_TOLERANCE or sample.n != report.n:
            raise EvidenceMismatchError(
                f"{published.subagent}/{published.variant}: recomputed {name} = "
                f"{got:.6f} (n={sample.n}) but the artifact reports "
                f"{report.point:.6f} (n={report.n}). Refusing to derive a paired "
                f"delta from calls the artifact did not score."
            )
    if published.judge is not None and published.judge.point is not None:
        sample = samples.metrics.get("judge_score")
        if sample is None or not sample.n:
            raise EvidenceMismatchError(
                f"{published.subagent}/{published.variant}: no judge sample was "
                "recovered, but the artifact carries a judged score."
            )
        got = sum(sample.values) / sample.n
        if (
            abs(got - published.judge.point) > RECOMPUTE_TOLERANCE
            or sample.n != published.judge.items_scored
        ):
            raise EvidenceMismatchError(
                f"{published.subagent}/{published.variant}: recomputed judge score "
                f"{got:.6f} (n={sample.n}) != artifact {published.judge.point:.6f} "
                f"(n={published.judge.items_scored})."
            )


# --------------------------------------------------------------------------
# building the evidence
# --------------------------------------------------------------------------


def _judge_cell(arm: ArmResult | None) -> JudgeScoreCell | None:
    if arm is None or arm.judge is None:
        return None
    return JudgeScoreCell(
        split=arm.judge.split,
        items_scored=arm.judge.items_scored,
        estimate=arm.judge.estimate,
        point=arm.judge.point,
        arm=arm.variant,
    )


def _paired_delta(
    gate: str,
    baseline: ArmSamples,
    candidate: ArmSamples,
    *,
    seed: int,
) -> tuple[Estimate | None, str | None]:
    """``(estimate, reason it is absent)`` — exactly one of the two is set."""
    metric = _DELTA_METRIC[gate]
    base = baseline.metrics.get(metric)
    cand = candidate.metrics.get(metric)
    if base is None or cand is None:
        return None, (
            f"{baseline.subagent} has no {metric} instrument, so "
            f"{gate} was not measured for it"
        )
    try:
        delta = paired_bootstrap_delta(base, cand, seed=seed)
    except Exception as exc:  # InsufficientDataError, ragged pairing, ...
        return None, f"paired {metric} delta unavailable: {exc}"
    return delta.to_percentage_points(), None


def build_evidence(
    cfg: AppConfig,
    phase2: Phase2Result,
    *,
    samples: Mapping[tuple[str, str], ArmSamples] | None = None,
    shadow: Mapping[str, Estimate] | None = None,
    shadow_metric: str | None = None,
    adjudications: Mapping[str, TriageSummary] | None = None,
    prior_adjudications: Mapping[str, TriageSummary] | None = None,
    prior_arm: str = "",
    latency_probes: Mapping[str, SameRegionLatencyProbe] | None = None,
    regions: Regions | None = None,
    baseline_variant: str = BASELINE_VARIANT,
    candidate_variant: str = CANDIDATE_VARIANT,
    subagents: Iterable[str] | None = None,
    cost_savings: Mapping[str, Estimate] | None = None,
    cost_reason: str | None = None,
) -> list[SubagentEvidence]:
    """One :class:`SubagentEvidence` per subagent in the artifact.

    Every one of the six gates ends up either in ``estimates`` (measured) or in
    ``unmeasured`` (with a reason). Nothing is left implicit, because
    :func:`amw.eval.stats.missing_gates` reads the difference and the verdict
    depends on it.
    """
    regions = regions or Regions.from_env(cfg)
    shadow = shadow or {}
    adjudications = adjudications or {}
    prior_adjudications = prior_adjudications or {}
    latency_probes = latency_probes or {}
    cost_savings = cost_savings or {}
    seed = phase2.bootstrap_seed
    names = list(subagents) if subagents is not None else sorted(
        {arm.subagent for arm in phase2.arms}
    )

    out: list[SubagentEvidence] = []
    for subagent in names:
        base_arm = _arm(phase2, subagent, baseline_variant)
        cand_arm = _arm(phase2, subagent, candidate_variant)
        estimates: dict[str, Estimate] = {}
        unmeasured: dict[str, str] = {}
        notes: list[str] = []

        # --- schema validity: the candidate's own rate, absolute gate ---
        cand_schema = (
            cand_arm.metrics.get(GATE_SCHEMA).estimate
            if cand_arm and GATE_SCHEMA in cand_arm.metrics
            else None
        )
        if cand_schema is not None:
            estimates[GATE_SCHEMA] = cand_schema
        else:
            unmeasured[GATE_SCHEMA] = (
                f"no {GATE_SCHEMA} estimate on arm {candidate_variant} in the artifact"
            )

        claude_schema = None
        if base_arm and GATE_SCHEMA in base_arm.metrics:
            report = base_arm.metrics[GATE_SCHEMA]
            if report.point is not None:
                claude_schema = ClaudeSchemaValidityCell(
                    estimate=report.estimate, point=report.point
                )

        # --- the two paired deltas ---
        base_samples = (samples or {}).get((subagent, baseline_variant))
        cand_samples = (samples or {}).get((subagent, candidate_variant))
        for gate in (GATE_QUALITY, GATE_GROUNDEDNESS):
            if base_samples is None or cand_samples is None:
                unmeasured[gate] = (
                    "per-item scores were not supplied; phase2.json stores only "
                    "per-arm aggregates, and a paired bootstrap needs the items"
                )
                continue
            estimate, reason = _paired_delta(
                gate, base_samples, cand_samples, seed=seed
            )
            if estimate is None:
                unmeasured[gate] = reason or "not measured"
            else:
                estimates[gate] = estimate

        # --- shadow agreement (T11) ---
        if subagent in shadow:
            estimates[GATE_SHADOW] = shadow[subagent]
            # "Agreement" is not one number. The same recordings yield 0.957 or
            # 0.129 for chunk_summarizer depending on which figure is read, so
            # a bare 0.957 in a gate row is unattributed. Say which one it is.
            notes.append(SHADOW_METRIC_NOTES.get(shadow_metric, UNLABELLED_SHADOW_NOTE))
        else:
            unmeasured[GATE_SHADOW] = (
                "no shadow run in this artifact set — shadow_agreement is "
                "produced by `cli.py shadow`, which has not been run for this "
                "corpus"
            )

        # --- cost savings: blocked upstream by pricing.yaml ---
        if subagent in cost_savings:
            estimates[GATE_COST] = cost_savings[subagent]
        else:
            unmeasured[GATE_COST] = cost_reason or (
                "no dollar figure is computable: config/pricing.yaml is "
                "unverified and customer volumes are unconfirmed"
            )

        # --- latency: probe or disclosure, never a cross-region pass ---
        probe = latency_probes.get(subagent)
        sentinels: dict[str, float] = {}
        if probe is not None:
            estimates[GATE_LATENCY] = probe.candidate_p95
            sentinels["claude_baseline_p95"] = probe.baseline_p95_ms
        else:
            unmeasured[GATE_LATENCY] = (
                f"{REGION_SPLIT_DISCLOSURE}: Claude ran in {regions.baseline}, "
                f"Gemini in {regions.candidate}. The claude_baseline_p95 sentinel "
                f"resolves only from a same-region probe, so this gate is not "
                f"evaluated — it is not passed."
            )

        if cand_arm is None:
            notes.append(
                f"no {candidate_variant} arm for {subagent} in this artifact"
            )
        if base_arm is None:
            notes.append(f"no {baseline_variant} arm for {subagent} in this artifact")

        out.append(
            SubagentEvidence(
                subagent=subagent,
                baseline_variant=baseline_variant,
                candidate_variant=candidate_variant,
                estimates=estimates,
                unmeasured=unmeasured,
                sentinel_values=sentinels,
                claude_schema_validity=claude_schema,
                candidate_schema_validity=cand_schema,
                judge_baseline=_judge_cell(base_arm),
                judge_candidate=_judge_cell(cand_arm),
                latency_probe=probe,
                adjudication=adjudications.get(subagent),
                adjudication_prior=prior_adjudications.get(subagent),
                adjudication_prior_arm=(
                    prior_arm if subagent in prior_adjudications else ""
                ),
                regions=regions,
                notes=notes,
            )
        )
    return out
