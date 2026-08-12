"""Gates -> verdicts -> the Migration Readiness Scorecard.

The scorecard is the artefact the customer keeps, so this module is written
around what it is *not* allowed to do.

**It does not re-implement the gate check.** ``config/gates.yaml`` holds the
thresholds, :func:`amw.eval.stats.check_gate` compares them against a CI bound
(lower for ``min``, upper for ``max``), and this module only decides which
estimates are eligible to be checked and how the result is worded. There is no
threshold literal here.

**It does not name a verdict rule in code.** ``gates.yaml`` says which verdict
follows from which failure pattern (``all_pass``, ``only_quality_gates_fail``,
``any_blocking_gate_fails``) and which gates are "quality" and which are
"blocking". :class:`VerdictRules` reads that mapping; renaming MIGRATE in the
YAML renames it in the report.

**It never fills a gap with a pass.** :func:`amw.eval.stats.missing_gates`
names every pre-agreed gate nothing measured, and a subagent with any missing
gate cannot come out MIGRATE — it comes out :data:`INCOMPLETE`, with the
verdict it *would* have received shown as provisional. A verdict computed over
four of six gates is not the verdict the customer agreed to. The one exception
is a blocking gate that actually failed: that is a finding regardless of what
else went unmeasured, so it stands as HOLD.

**It never prints an unqualified number.** The four cells whose caveat is part
of the measurement are built by :mod:`amw.reporting.cells`, which welds the
qualification in by construction.

Parity language (ground rule 7): the report says "quality parity within
measurement under pre-agreed gates". It never says "zero quality drop", and
every gate line states the bound that was tested.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Sequence

from pydantic import BaseModel, ConfigDict, Field

from amw.config import AppConfig, ConfigError, GatesConfig
from amw.economics.cache_breakeven import CacheBreakeven, breakeven_curve
from amw.economics.cost_model import CostModelResult, VolumeSet, confirm_volumes, cost_model
from amw.eval.runner import Phase2Result
from amw.eval.stats import Estimate, GateCheck, check_gates, missing_gates
from amw.reporting.cells import (
    EM_DASH,
    FAIL_IMPRECISE,
    FAIL_REGRESSION,
    IMPRECISION_NOTE,
    cost_cell,
    delta_failure_kind,
    estimate_text,
    latency_cell,
    paired_delta_text,
)
from amw.reporting.evidence import (
    GATE_COST,
    GATE_LATENCY,
    GATE_SHADOW,
    EvidenceMismatchError,
    Regions,
    SubagentEvidence,
    build_evidence,
    collect_samples,
)
from amw.reporting.ladder import Ladder, build_ladder, render_ladder
from amw.shadow.triage import MALFORMED_CAVEAT, TriageSummary

__all__ = [
    "INCOMPLETE",
    "ALT_EVALUATORS",
    "apply_alt_clause",
    "UNDETERMINED",
    "TAXONOMY_LINE",
    "PARITY_SENTENCE",
    "VerdictRules",
    "SubagentVerdict",
    "Scorecard",
    "ConfigurationCost",
    "CostPanel",
    "ProjectionRow",
    "Projection",
    "decide_verdict",
    "build_scorecard",
    "render_markdown",
    "load_adjudications",
    "shadow_candidate_arm",
    "load_ladders",
    "cmd_scorecard",
]

#: Not a verdict in gates.yaml, and deliberately so: it is the *absence* of one.
#: A subagent lands here when a pre-agreed gate went unmeasured, so the verdict
#: the customer agreed to cannot be computed at all.
INCOMPLETE = "INCOMPLETE"

#: A failure pattern no rule in gates.yaml describes — e.g. only
#: ``cost_savings_pct`` fails, which is neither a quality gate nor a blocking
#: one. Surfaced rather than rounded to the nearest verdict.
UNDETERMINED = "UNDETERMINED"

#: Verbatim, owner-specified. Every scorecard footer carries it.
TAXONOMY_LINE = (
    "Verdicts apply to each subagent's measured behavior class (Level 1 "
    "single-call transforms, measured in full here); tool-selection and "
    "multi-step trajectory behaviors are evaluated with their own instruments "
    "in the follow-on and receive no verdict today."
)

#: Where a reader finds out which models these verdicts are about.
#:
#: The path is relative to the published page (``results/scorecard.md``), which
#: is where this report is read; in the raw ``artifacts/results/`` copy the link
#: does not resolve, so the sentence names what is on the other end rather than
#: relying on the reader following it.
MODELS_PAGE_LINK = (
    "[Models in this study](../models-in-this-study.md) — every arm's exact "
    "model ID, access path, part in the study, and recording window"
)

#: Ground rule 7. The only parity claim this repo makes.
PARITY_SENTENCE = (
    "Gates are checked against 95% confidence-range bounds, so a passing gate licenses "
    '"quality parity within measurement under pre-agreed gates" — never "zero '
    'quality drop".'
)

_TITLE_CASE = {
    "query_rewriter": "Query Rewriter",
    "chunk_summarizer": "Chunk Summarizer",
    "feature_extractor": "Feature Extractor",
}


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _pretty(subagent: str) -> str:
    return _TITLE_CASE.get(subagent, subagent.replace("_", " ").title())


def _model_name(card: "Scorecard", key: str) -> str:
    """A model registry key as its human display name, or the key if unknown.

    Falls back to the key rather than to a blank or a guess: an arm whose model
    is not in the registry is a fact worth seeing in the table.
    """
    if not key:
        return "—"
    return (card.footer.get("model_display") or {}).get(key, f"`{key}`")


def _arm_label(card: "Scorecard", model_key: str, variant: str) -> str:
    """``Model name<br>prompt variant`` — the two halves of an arm's identity.

    Both are needed and neither substitutes for the other. The variant says
    which prompt ran; the model says what ran it. ``gemini_tuned_v1`` alone is
    ambiguous across this study, because the same prompt was deliberately run
    on three different models to show that it ports.
    """
    name = _model_name(card, model_key)
    if not variant:
        return name
    return f"{name}<br><small>`{variant}`</small>"


# --------------------------------------------------------------------------
# verdicts, read out of gates.yaml
# --------------------------------------------------------------------------


class VerdictRules(_Base):
    """The verdict names and gate groups, extracted from ``gates.yaml``.

    Indexed by ``rule`` rather than by verdict name, so the code says "the
    verdict whose rule is all_pass" and the YAML says what that verdict is
    called.
    """

    all_pass: str
    only_quality_gates_fail: str
    any_blocking_gate_fails: str
    quality_gates: list[str] = Field(default_factory=list)
    blocking_gates: list[str] = Field(default_factory=list)
    descriptions: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def of(cls, gates: GatesConfig) -> "VerdictRules":
        by_rule: dict[str, str] = {}
        quality: list[str] = []
        blocking: list[str] = []
        descriptions: dict[str, str] = {}
        for name, rule in gates.verdicts.items():
            if rule.rule in by_rule:
                raise ValueError(
                    f"gates.yaml declares two verdicts with rule {rule.rule!r}: "
                    f"{by_rule[rule.rule]} and {name}. The mapping has to be "
                    "one-to-one or the scorecard cannot name a verdict."
                )
            by_rule[rule.rule] = name
            descriptions[name] = rule.description
            quality.extend(rule.quality)
            blocking.extend(rule.blocking)
        absent = {
            "all_pass",
            "only_quality_gates_fail",
            "any_blocking_gate_fails",
        } - set(by_rule)
        if absent:
            raise ValueError(f"gates.yaml has no verdict for rule(s) {sorted(absent)}")
        return cls(
            all_pass=by_rule["all_pass"],
            only_quality_gates_fail=by_rule["only_quality_gates_fail"],
            any_blocking_gate_fails=by_rule["any_blocking_gate_fails"],
            quality_gates=sorted(set(quality)),
            blocking_gates=sorted(set(blocking)),
            descriptions=descriptions,
        )


class SubagentVerdict(_Base):
    """One subagent's gate results and the verdict they support."""

    subagent: str
    verdict: str
    #: What the verdict would be if every unmeasured gate passed. Set only when
    #: ``verdict`` is INCOMPLETE, and never presented as the answer.
    provisional: str | None = None
    checks: dict[str, GateCheck] = Field(default_factory=dict)
    failed: list[str] = Field(default_factory=list)
    #: Gates that missed their CI bound and cleared on the ``alt`` clause
    #: gates.yaml wrote for them in advance. Not in ``failed`` — they passed —
    #: but named separately, because "passed" and "passed by the alternative
    #: route" are different claims and the card has to be able to say which.
    passed_by_alt: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    rationale: str = ""

    @property
    def evaluated(self) -> int:
        return len(self.checks)

    @property
    def total_gates(self) -> int:
        return len(self.checks) + len(self.missing)


def _rule_verdict(failed: Sequence[str], rules: VerdictRules) -> str:
    if not failed:
        return rules.all_pass
    if set(failed) <= set(rules.quality_gates):
        return rules.only_quality_gates_fail
    return UNDETERMINED


#: Gates whose ``alt`` clause this module knows how to evaluate, and what it
#: reads to do it. A clause nobody can evaluate is not silently ignored —
#: :func:`apply_alt_clause` raises — because an unevaluated alternative route
#: is exactly the "gate that quietly disappears" failure that
#: :func:`amw.eval.stats.missing_gates` exists to prevent, wearing a different
#: hat.
ALT_EVALUATORS: dict[str, str] = {
    GATE_SHADOW: "the judge-adjudicated disagreement triage (SubagentEvidence.adjudication)",
}

#: Said when a gate has an alt clause and the run produced nothing to evaluate
#: it against. It is not a failure of the clause; it is an absence of evidence,
#: and the gate stands failed on its CI bound.
ALT_UNEVALUATED = (
    "the pre-registered alt clause was not evaluated: no {source} in this "
    "artifact set. The gate stands on its confidence-range bound."
)


def apply_alt_clause(check: GateCheck, evidence: SubagentEvidence) -> GateCheck:
    """Give a gate that missed its CI bound its pre-registered second route.

    Only a gate that (a) failed and (b) was written with an ``alt`` clause in
    ``gates.yaml`` *before* any of this was measured can be rescued here. The
    clause is evaluated on exactly what it says — ``shadow_agreement``'s reads
    "on disagreements, judge-adjudicated wins >= losses", with no exclusion in
    it, so it is decided on the overall tally. The quality-only tally travels
    beside it in :attr:`GateCheck.alt_evidence` as the honesty check, never as
    the thing being tested.

    :raises ~amw.config.ConfigError: gates.yaml declares an ``alt`` clause on a
        gate with no evaluator wired here.
    """
    if check.alt is None or check.passed:
        return check
    if check.gate not in ALT_EVALUATORS:
        raise ConfigError(
            f"gate {check.gate!r} has an alt clause in gates.yaml "
            f"({check.alt!r}) but nothing here knows how to evaluate it, so a "
            f"pre-agreed route to passing would be silently unavailable. Add an "
            f"evaluator to amw.reporting.scorecard.ALT_EVALUATORS."
        )

    summary = evidence.adjudication
    outcome = summary.wins_ge_losses if summary is not None else None
    if outcome is None:
        return check.model_copy(
            update={
                "alt_evidence": ALT_UNEVALUATED.format(
                    source=ALT_EVALUATORS[check.gate]
                )
            }
        )
    return check.model_copy(
        update={
            "alt_passed": bool(outcome),
            "alt_summary": f"{summary.wins}W/{summary.losses}L",
            "alt_evidence": summary.adjudication_text(
                baseline_label=f"`{evidence.baseline_variant}`"
            ),
        }
    )


def decide_verdict(
    evidence: SubagentEvidence, gates: GatesConfig, *, rules: VerdictRules | None = None
) -> SubagentVerdict:
    """Evaluate every measurable gate and apply ``gates.yaml``'s verdict rules."""
    rules = rules or VerdictRules.of(gates)
    checks = check_gates(
        evidence.estimates, gates, sentinel_values=evidence.sentinel_values
    )
    checks = {
        name: apply_alt_clause(check, evidence) for name, check in checks.items()
    }
    missing = missing_gates(checks, gates)
    # `effective_passed`, not `passed`: a gate carried by the alt clause
    # gates.yaml pre-registered for it has cleared, and the verdict rules are
    # written over cleared/not-cleared. `passed_by_alt` keeps the route on the
    # record so nothing downstream can print a bare PASS for it.
    failed = sorted(name for name, check in checks.items() if not check.effective_passed)
    by_alt = sorted(name for name, check in checks.items() if check.by_alt)
    blocking_failed = [name for name in failed if name in rules.blocking_gates]

    # Appended to whichever rationale is produced below. A verdict that rests
    # on an alt clause has to say so in its own sentence — the route is part of
    # the finding, not a footnote to it.
    alt_note = ""
    if by_alt:
        routes = "; ".join(
            f"{name} missed its confidence-range bound "
            f"({checks[name].compared_bound} = {checks[name].compared_value:.4g} "
            f"vs {checks[name].bound:g}) and cleared on the alt clause "
            f'pre-registered in gates.yaml ("{checks[name].alt}"), '
            f"measured at {checks[name].alt_evidence}"
            for name in by_alt
        )
        alt_note = f" {routes}."

    if blocking_failed:
        # A blocking gate that actually failed is a finding on its own terms.
        # Unlike every other pattern it does not need the full gate set to be
        # meaningful, so it is the one verdict issued over an incomplete run.
        return SubagentVerdict(
            subagent=evidence.subagent,
            verdict=rules.any_blocking_gate_fails,
            checks=checks,
            failed=failed,
            passed_by_alt=by_alt,
            missing=missing,
            rationale=(
                f"blocking gate(s) {', '.join(blocking_failed)} failed on the confidence-range "
                f"bound. {rules.descriptions.get(rules.any_blocking_gate_fails, '')}"
                f"{alt_note}"
            ).strip(),
        )

    if missing:
        provisional = _rule_verdict(failed, rules)
        return SubagentVerdict(
            subagent=evidence.subagent,
            verdict=INCOMPLETE,
            provisional=provisional,
            checks=checks,
            failed=failed,
            passed_by_alt=by_alt,
            missing=missing,
            rationale=(
                f"{len(checks)} of {len(gates.subagent_gates)} pre-agreed gates were "
                f"measured; {', '.join(missing)} were not. A verdict over a subset of "
                f"the gates is not the verdict that was agreed, so none is issued. "
                f"Were every unmeasured gate to pass, it would be {provisional}."
                f"{alt_note}"
            ),
        )

    verdict = _rule_verdict(failed, rules)
    if verdict == UNDETERMINED:
        rationale = (
            f"gate(s) {', '.join(failed)} failed. That pattern matches no verdict "
            f"rule in gates.yaml — they are neither quality gates nor blocking "
            f"gates — so no verdict is issued and the gates file needs a rule for it."
        )
    elif verdict == rules.all_pass:
        rationale = rules.descriptions.get(verdict, "")
    else:
        rationale = (
            f"quality gate(s) {', '.join(failed)} failed while every blocking gate "
            f"held. {rules.descriptions.get(verdict, '')}"
        ).strip()

    return SubagentVerdict(
        subagent=evidence.subagent,
        verdict=verdict,
        checks=checks,
        failed=failed,
        passed_by_alt=by_alt,
        missing=missing,
        rationale=(rationale + alt_note).strip(),
    )


# --------------------------------------------------------------------------
# the scorecard
# --------------------------------------------------------------------------


class ConfigurationCost(_Base):
    """One model *configuration's* measured economics for one subagent.

    Not a candidate row — a configuration row. The capped and default arms are
    the same provider model ID on the same prompt bytes, one setting apart, so
    putting them in the candidate table would invite a reader to compare them
    as if they were different models. They are the same model answering the
    question "what does the reasoning budget cost".
    """

    subagent: str
    #: Reader-facing configuration name, e.g. "reasoning budget minimised".
    configuration: str
    savings_text: str
    baseline_usd: float
    candidate_usd: float
    output_tokens: int
    baseline_output_tokens: int
    #: True for the configuration the scorecard recommends deploying.
    recommended: bool = False


class CostPanel(_Base):
    """The configurations compared, plus the findings that explain the gap."""

    rows: list[ConfigurationCost] = Field(default_factory=list)
    #: Prose that must appear under the table — the thinking-tax finding and
    #: the market-context line. Carried as text because both are conclusions a
    #: human drew from the audit, not values this module can recompute.
    notes: list[str] = Field(default_factory=list)


class ProjectionRow(_Base):
    """One arithmetic projection onto a model that was never measured."""

    subagent: str
    projected_usd: float
    measured_usd: float
    delta_pct: float


class Projection(_Base):
    """A cost projection onto a priced-but-unmeasured model.

    Exists so the panel cannot be mistaken for a measurement: it carries no
    quality field, no gate result and no verdict, and :func:`_projection_section`
    prints its ``basis`` and ``disclaimer`` above the numbers rather than
    below them.
    """

    model_display: str
    basis: str
    disclaimer: str
    rows: list[ProjectionRow] = Field(default_factory=list)


class Scorecard(_Base):
    """Everything the Markdown render needs, and nothing it computes itself.

    ``gates`` is embedded rather than re-read at render time so the rendered
    report and the version hash printed in its footer are guaranteed to come
    from the same file contents.
    """

    customer: str
    display_name: str
    gates: GatesConfig
    phase2: Phase2Result
    evidence: list[SubagentEvidence]
    verdicts: dict[str, SubagentVerdict]
    rules: VerdictRules
    footer: dict[str, Any]
    costs: CostModelResult
    cache: list[CacheBreakeven] = Field(default_factory=list)
    #: Per-subagent ablation ladders, keyed by subagent. Empty when no
    #: ``ablation_{subagent}.json`` sits beside the phase-2 artifact being
    #: scored — a scorecard with no ladder section is a scorecard that was not
    #: given one, not a subagent whose prompt was never tuned.
    ladders: dict[str, Ladder] = Field(default_factory=dict)
    #: Measured economics per model configuration. None when only one
    #: configuration was run, which is the case for every scorecard rendered
    #: before 2026-08-12.
    cost_panel: CostPanel | None = None
    #: Cost projections onto priced-but-unmeasured models. Never a verdict.
    projections: list[Projection] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    #: One line from the second-judge cross-check
    #: (:func:`amw.eval.crosscheck.crosscheck_footer_line`), or None when no
    #: cross-check artifact was supplied. Carried as rendered text rather than
    #: as numbers on purpose: the scorecard must be unable to do arithmetic
    #: with the validating judge's scores.
    crosscheck_line: str | None = None

    @property
    def prices_verified(self) -> bool:
        return self.footer["prices_verified_on"] != "UNVERIFIED"


def build_scorecard(
    cfg: AppConfig,
    phase2: Phase2Result,
    *,
    evidence: Sequence[SubagentEvidence] | None = None,
    volumes: VolumeSet | None = None,
    cache_preamble_tokens: int | None = None,
    crosscheck_line: str | None = None,
    ladders: Sequence[Any] | None = None,
    **evidence_kwargs: Any,
) -> Scorecard:
    """Assemble a scorecard from a phase-2 artifact plus whatever else exists.

    ``evidence`` can be supplied directly (fixtures, notebooks); otherwise it is
    built by :func:`amw.reporting.evidence.build_evidence`, which decides gate
    by gate what was measured and records a reason for everything that was not.
    """
    costs = cost_model(cfg, volumes=volumes)
    if evidence is None:
        evidence = build_evidence(
            cfg,
            phase2,
            cost_reason=None if costs.computable else costs.state,
            **evidence_kwargs,
        )
    elif evidence_kwargs:
        raise TypeError(
            f"evidence= was supplied, so {sorted(evidence_kwargs)} cannot be used; "
            "they are arguments to build_evidence()"
        )
    rules = VerdictRules.of(cfg.gates)
    verdicts = {e.subagent: decide_verdict(e, cfg.gates, rules=rules) for e in evidence}

    cache: list[CacheBreakeven] = []
    if cache_preamble_tokens:
        cache = breakeven_curve(cfg, cached_tokens=cache_preamble_tokens)

    footer = dict(cfg.provenance_footer())
    footer.update(
        {
            "mode": phase2.mode,
            "run_started": phase2.run_started,
            "recorded_from": phase2.recorded_from,
            "recorded_to": phase2.recorded_to,
            "judge_model": phase2.judge_model,
            "judge_prompt_version": phase2.judge_prompt_version,
            "bootstrap_seed": phase2.bootstrap_seed,
            "judge_repeats": phase2.judge_repeats,
            "generator_version": phase2.generator_version,
            "pricing_sources": list(cfg.pricing.sources),
            "unverified_prices": len(cfg.pricing.unverified_keys()),
            "volumes": costs.volumes.source.footer_label(),
            # Registry key -> display name, so every arm in the report can be
            # named by the model it ran on and not only by its prompt variant.
            # Resolved from config/models.yaml here, once, rather than at each
            # render site: a display name typed into a table is a display name
            # that can disagree with the registry.
            "model_display": {
                key: spec.display_name for key, spec in cfg.models.models.items()
            },
        }
    )
    return Scorecard(
        customer=cfg.customer.customer,
        display_name=cfg.customer.display_name,
        gates=cfg.gates,
        phase2=phase2,
        evidence=list(evidence),
        verdicts=verdicts,
        rules=rules,
        footer=footer,
        costs=costs,
        cache=cache,
        ladders={r.subagent: build_ladder(r) for r in (ladders or [])},
        notes=list(phase2.notes),
        crosscheck_line=crosscheck_line,
    )


# --------------------------------------------------------------------------
# markdown
# --------------------------------------------------------------------------


def _bound_text(gates: GatesConfig, gate_name: str) -> str:
    gate = gates.gate(gate_name)
    arrow = ">=" if gate.direction == "min" else "<="
    return f"`{arrow} {gate.bound}`"


def _latency_text(evidence: SubagentEvidence) -> str:
    probe = evidence.latency_probe
    return latency_cell(
        probe.candidate_p95 if probe else None,
        same_region_probe=probe is not None,
        candidate_region=evidence.regions.candidate,
    )


def _measured_cell(
    gate_name: str,
    evidence: SubagentEvidence,
    check: GateCheck | None,
    *,
    prices_verified: bool,
) -> str:
    if gate_name == GATE_LATENCY:
        return _latency_text(evidence)
    if gate_name == GATE_COST and check is None:
        return cost_cell(prices_verified=prices_verified)
    if check is None:
        return f"not evaluated — {evidence.unmeasured.get(gate_name, 'no estimate')}"
    return paired_delta_text(check.estimate)


def _prior_arm_sentence(evidence: SubagentEvidence) -> str:
    """The control tally: the same clause, evaluated on the arm this replaced.

    "The clause passes" on its own is a fact about the subagent — it is
    consistent with the clause having passed all along, before any tuning. Put
    the replaced arm's tally on the same corpus, same gate, same baseline
    beside it and the sentence becomes a fact about the *rung*, which is the
    claim the report is actually making. Rendered only when a prior shadow run
    was supplied; there is no default and nothing is inferred.
    """
    prior = evidence.adjudication_prior
    if prior is None:
        return ""
    arm = f"`{evidence.adjudication_prior_arm}`" if evidence.adjudication_prior_arm else "the arm it replaced"
    return (
        f" The clause is not passing on its own momentum: on the same corpus, "
        f"the same gate and the same baseline, {arm} adjudicated "
        f"{prior.adjudication_text(baseline_label='Claude baseline')} — so the "
        f"pass is attributable to this rung, not to the subagent."
    )


def _alt_clause_note(check: GateCheck, evidence: SubagentEvidence) -> str:
    """The paragraph under the table for a gate that passed by its alt clause.

    The Result cell has room for the tally the clause was decided on and
    nothing else. Everything a reader needs to audit the pass goes here: the
    bound that was missed, the clause quoted from ``gates.yaml``, both
    adjudication figures, the mechanism behind the exclusion in the second one
    — which is the same org-policy tool-emission artifact disclosed beside the
    baseline everywhere else in this report — and, when it exists, the
    replaced arm's tally as the control.
    """
    return (
        f"`{check.gate}` did not clear its confidence-range bound "
        f"({check.compared_bound} = {check.compared_value:.4g}, bound "
        f"{'≥' if check.direction == 'min' else '≤'} {check.bound:g}). It passes on "
        f'the alternative route pre-registered in gates.yaml — "{check.alt}" — '
        f"measured at {check.alt_evidence}. {MALFORMED_CAVEAT}"
        f"{_prior_arm_sentence(evidence)} The clause was "
        f"written before any of this was measured; it is the pre-agreed second "
        f"route, not a threshold chosen after seeing the result."
    )


def _gate_table(
    evidence: SubagentEvidence,
    verdict: SubagentVerdict,
    gates: GatesConfig,
    *,
    prices_verified: bool,
) -> list[str]:
    lines = [
        "| Gate | Bound (gates.yaml) | Measured (95% confidence range) | Bound tested "
        "| Result |",
        "| --- | --- | --- | --- | --- |",
    ]
    imprecise: list[str] = []
    by_alt: list[str] = []
    for gate_name in gates.subagent_gates:
        check = verdict.checks.get(gate_name)
        measured = _measured_cell(
            gate_name, evidence, check, prices_verified=prices_verified
        )
        if check is None:
            tested, result = "not evaluated", "not evaluated"
        else:
            tested = f"{check.compared_bound} = {check.compared_value:.4g}"
            # Never a bare "PASS" for a gate carried by its alt clause: the
            # cell itself names the route, because the CI bound was missed and
            # a reader scanning the Result column would otherwise not know it.
            result = check.result_text()
            if check.by_alt:
                by_alt.append(gate_name)
            elif not check.passed:
                # A failing paired delta says *which* kind of failure it is.
                # CS's -2.32 pp [-5.00, +0.36] and FE's -10.44 pp
                # [-13.78, -7.12] are both FAIL and are not the same finding;
                # one interval spans zero and the other does not.
                kind = delta_failure_kind(check.estimate)
                if kind is not None:
                    result = f"{result} — {kind}"
                if kind == FAIL_IMPRECISE:
                    imprecise.append(gate_name)
        lines.append(
            f"| `{gate_name}` | {_bound_text(gates, gate_name)} | {measured} "
            f"| {tested} | {result} |"
        )
    for gate_name in by_alt:
        lines.extend(["", _alt_clause_note(verdict.checks[gate_name], evidence)])
    for gate_name in imprecise:
        lines.extend(
            [
                "",
                IMPRECISION_NOTE.format(
                    gate=gate_name,
                    imprecise=FAIL_IMPRECISE,
                    regression=FAIL_REGRESSION,
                ),
            ]
        )
    return lines


def _savings_cell(evidence: SubagentEvidence, fallback: str) -> str:
    """The savings row, kept consistent with the gate row above it.

    The other three cost rows are run-rate figures and stay "not measured"
    while customer volumes are unconfirmed. A savings *ratio* does not need
    volumes, so when one has been measured from recorded tokens it appears
    here — printing "not measured" directly beneath a gate row carrying
    ``-16.5%`` reads as a rendering bug, and a reader resolves that
    contradiction by guessing. The basis is named because it is **not** the
    profile-volume basis ``gates.yaml`` registers.
    """
    estimate = evidence.estimates.get(GATE_COST)
    if estimate is None:
        return fallback
    return (
        f"{estimate_text(estimate)} — measured per-call tokens over this corpus "
        f"at list prices, not the registered profile-volume basis"
    )


def _evidence_table(
    evidence: SubagentEvidence,
    *,
    prices_verified: bool,
    card: "Scorecard | None" = None,
) -> list[str]:
    claude_schema = evidence.claude_schema_validity
    cost = cost_cell(prices_verified=prices_verified)
    # Row labels name the model, not the vendor. "Judge score — Gemini" was
    # ambiguous the moment a second Gemini generation entered the study, and
    # this table is the one a reader screenshots.
    base = _model_name(card, evidence.baseline_model) if card else "Claude"
    cand = _model_name(card, evidence.candidate_model) if card else "Gemini"
    rows = [
        (
            f"{base} `json_schema_validity` (`{evidence.baseline_variant}`)",
            claude_schema.render() if claude_schema else "not measured",
        ),
        (
            f"{cand} `json_schema_validity` (`{evidence.candidate_variant}`)",
            estimate_text(evidence.candidate_schema_validity),
        ),
        (
            f"Judge score — {base} (incumbent)",
            evidence.judge_baseline.render() if evidence.judge_baseline else "not measured",
        ),
        (
            f"Judge score — {cand} (candidate)",
            evidence.judge_candidate.render()
            if evidence.judge_candidate
            else "not measured",
        ),
        ("Latency p95", _latency_text(evidence)),
        ("Cost per call", cost),
        ("Monthly run rate", cost),
        ("Annual run rate", cost),
        (f"Cost savings vs {base}", _savings_cell(evidence, cost)),
    ]
    return ["| Evidence | Value |", "| --- | --- |"] + [
        f"| {label} | {value} |" for label, value in rows
    ]


def _cost_panel_section(panel: CostPanel) -> list[str]:
    """Both measured configurations, recommended one first.

    Order is the argument. The capped configuration is what the scorecard
    recommends deploying, so it is the row a reader meets first; the default
    configuration renders beneath it, labelled, because it is what every arm
    measured before 2026-08-12 ran on and removing it would delete a
    measurement to make a recommendation look tidier.
    """
    lines = [
        "## Cost — configurations compared",
        "",
        "Same model, same prompt bytes, same corpus. One setting apart.",
        "",
        "| Configuration | Subagent | Savings vs Claude | Corpus cost | "
        "Output tokens (vs Claude) |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in sorted(panel.rows, key=lambda r: (not r.recommended, r.subagent)):
        name = row.configuration
        if row.recommended:
            name = f"**{name}** — recommended"
        ratio = (
            f"{row.output_tokens:,} ({row.output_tokens / row.baseline_output_tokens:.2f}x)"
            if row.baseline_output_tokens
            else f"{row.output_tokens:,}"
        )
        lines.append(
            f"| {name} | {_pretty(row.subagent)} | {row.savings_text} | "
            f"${row.baseline_usd:.4f} → ${row.candidate_usd:.4f} | {ratio} |"
        )
    if panel.notes:
        lines += [""] + [f"- {note}" for note in panel.notes]
    return lines


def _projection_section(projection: Projection) -> list[str]:
    """A labelled projection. The label goes above the numbers, not below."""
    lines = [
        f"## Cost projection — {projection.model_display}",
        "",
        f"**Projection, not a measurement.** {projection.disclaimer}",
        "",
        f"Basis: {projection.basis}",
        "",
        "| Subagent | Measured (as run) | Projected | Difference |",
        "| --- | --- | --- | --- |",
    ]
    for row in projection.rows:
        lines.append(
            f"| {_pretty(row.subagent)} | ${row.measured_usd:.4f} | "
            f"${row.projected_usd:.4f} | {row.delta_pct:+.1f}% |"
        )
    return lines


def _economics_section(card: Scorecard) -> list[str]:
    costs = card.costs
    lines = ["## Economics", ""]
    if not costs.computable:
        lines += [
            f"**{costs.state}.** No dollar figure is produced — not a zero, not a "
            "placeholder. "
            + (
                "Both gates below are independent and each has to be cleared by a "
                "human."
                if len(costs.blockers) > 1
                else "The gate below has to be cleared by a human."
            ),
            "",
            "| Gate | Why it is closed | Clears when |",
            "| --- | --- | --- |",
        ]
        lines += [
            f"| {b.gate} | {b.reason} | {b.clears_when} |" for b in costs.blockers
        ]
        lines += [
            "",
            f"Volume basis: {costs.volumes.source.footer_label()} — "
            f"{len(costs.volumes.subagents)} evaluated subagent(s), "
            f"x{'/x'.join(f'{m:g}' for m in costs.multipliers)} sensitivity band and "
            "cached/uncached rows ready to run the moment both gates clear.",
        ]
    else:
        lines += [
            "| Subagent | Volume | Caching | Calls/day | Claude $/mo | Gemini $/mo | "
            "Saving |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for row in costs.rows:
            saving = f"{row.savings_pct:.1f}%" if row.savings_pct is not None else EM_DASH
            lines.append(
                f"| {row.subagent} | x{row.multiplier:g} | {row.caching} | "
                f"{row.calls_per_day:,.0f} | ${row.baseline_monthly_usd:,.0f} | "
                f"${row.candidate_monthly_usd:,.0f} | {saving} |"
            )
        lines += ["", f"Volume basis: {costs.volumes.source.footer_label()}."]

    if card.cache:
        lines += ["", "### Context-caching breakeven", ""]
        first = card.cache[0]
        if not first.computable:
            lines.append(
                f"**{first.state}.** The formula is implemented and tested; it "
                "produces figures as soon as `scripts/refresh_pricing.py` stamps "
                "`verified_on`."
            )
        else:
            lines += [
                "| TTL (h) | Breakeven calls/day | Write $/window | Storage $/window |",
                "| --- | --- | --- | --- |",
            ]
            for entry in card.cache:
                if entry.breakeven_calls_per_day is None:
                    lines.append(
                        f"| {entry.ttl_hours:g} | {entry.state} | "
                        f"${entry.write_usd:,.4f} | ${entry.storage_usd:,.4f} |"
                    )
                    continue
                lines.append(
                    f"| {entry.ttl_hours:g} | {entry.breakeven_calls_per_day:,.0f} | "
                    f"${entry.write_usd:,.4f} | ${entry.storage_usd:,.4f} |"
                )
            lines += ["", "Assumptions: " + "; ".join(first.assumptions) + "."]
    return lines


def _provenance_line(card: Scorecard) -> str:
    """Ground rule 1 on screen: say when the calls were actually made."""
    phase2 = card.phase2
    if phase2.recorded_from:
        return (
            f"**REPLAY — every number below comes from model calls recorded "
            f"{phase2.recorded_from} to {phase2.recorded_to}, not from a run just now.**"
        )
    if phase2.run_started:
        return f"**LIVE — run started {phase2.run_started}.**"
    return (
        "**This artifact carries no run date and no recording window — do not show "
        "it to a customer until it has been re-run.**"
    )


def _region_row(card: Scorecard) -> str:
    if not card.evidence:
        return f"| Region | {card.footer['region']} |"
    regions = card.evidence[0].regions
    return (
        f"| Region(s) | Claude `{regions.baseline}`, Gemini + judge "
        f"`{regions.candidate}` (source: {regions.source}) |"
    )


def _footer(card: Scorecard) -> list[str]:
    footer = card.footer
    pricing = (
        footer["prices_verified_on"]
        if card.prices_verified
        else f"UNVERIFIED — {footer['unverified_prices']} rates still read `VERIFY`"
    )
    lines = [
        "## Footer",
        "",
        TAXONOMY_LINE,
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Customer | {card.display_name} (`{card.customer}`) |",
        f"| Provenance | {footer['provenance']}, generator `{footer['generator_version']}`, "
        f"dataset seed `{footer['seed']}` |",
        f"| Bootstrap | 95% confidence range, seed `{footer['bootstrap_seed']}` |",
        f"| Judge | {footer['judge_model']}, prompt `{footer['judge_prompt_version']}`, "
        f"k={footer['judge_repeats']} repeats |",
        f"| Mode | `{footer['mode']}` |",
        f"| Run date | {footer['run_started'] or 'no live run — assembled from recordings'} |",
        f"| Recording window | {footer['recorded_from'] or 'n/a'} to "
        f"{footer['recorded_to'] or 'n/a'} |",
        _region_row(card),
        f"| Prices verified on | {pricing} |",
        f"| Pricing sources | {', '.join(footer['pricing_sources'])} |",
        f"| Volumes | {footer['volumes']} |",
        f"| Gates | version {card.gates.version}, hash `{card.gates.version_hash}` |",
        # Every arm's exact model ID, access path, part in the study and
        # recording window. A scorecard that names "Gemini" without a version
        # is not re-checkable, and the footer is where a reader goes to find
        # out what they are looking at.
        f"| Models | {MODELS_PAGE_LINK} |",
    ]
    if card.notes:
        lines += ["", "**Run notes**", ""] + [f"- {note}" for note in card.notes]
    if card.crosscheck_line:
        # Its own paragraph, not a table row: the combination rule is a
        # sentence a reader has to actually read, and it is the answer to the
        # first question anyone asks about a Gemini-judged comparison.
        lines += ["", f"**Judge cross-check.** {card.crosscheck_line}"]
    return lines


def render_markdown(card: Scorecard) -> str:
    """The whole report. Pure formatting — no arithmetic happens here."""
    prices_verified = card.prices_verified
    lines: list[str] = [
        f"# Migration Readiness Scorecard — {card.display_name}",
        "",
        _provenance_line(card),
        "",
        PARITY_SENTENCE,
        "",
        "## Verdicts",
        "",
        "| Subagent | Incumbent | Candidate | Gates evaluated | Verdict | Why |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for evidence in card.evidence:
        verdict = card.verdicts[evidence.subagent]
        label = f"**{verdict.verdict}**"
        if verdict.provisional:
            label += f" (provisional: {verdict.provisional})"
        lines.append(
            f"| {_pretty(evidence.subagent)} | "
            f"{_arm_label(card, evidence.baseline_model, evidence.baseline_variant)} | "
            f"{_arm_label(card, evidence.candidate_model, evidence.candidate_variant)} | "
            f"{verdict.evaluated} of {verdict.total_gates} | {label} | "
            f"{verdict.rationale} |"
        )

    for evidence in card.evidence:
        verdict = card.verdicts[evidence.subagent]
        lines += [
            "",
            f"## {_pretty(evidence.subagent)}",
            "",
            f"**{_model_name(card, evidence.candidate_model)}** "
            f"(`{evidence.candidate_variant}`) measured against "
            f"**{_model_name(card, evidence.baseline_model)}** "
            f"(`{evidence.baseline_variant}`).",
            "",
        ]
        lines += _gate_table(
            evidence, verdict, card.gates, prices_verified=prices_verified
        )
        lines += ["", "### Evidence", ""]
        lines += _evidence_table(
            evidence, prices_verified=prices_verified, card=card
        )
        if evidence.unmeasured:
            lines += ["", "**Not evaluated, and why**", ""]
            lines += [
                f"- `{name}` — {evidence.unmeasured[name]}"
                for name in sorted(evidence.unmeasured)
            ]
        if evidence.notes:
            lines += [""] + [f"- {note}" for note in evidence.notes]
        ladder = card.ladders.get(evidence.subagent)
        if ladder is not None and ladder.rows:
            lines += ["", "### Ablation ladder", ""]
            lines += render_ladder(ladder)

    if card.cost_panel is not None and card.cost_panel.rows:
        lines += [""] + _cost_panel_section(card.cost_panel)
    lines += [""] + _economics_section(card)
    for projection in card.projections:
        lines += [""] + _projection_section(projection)
    lines += [""] + _footer(card)
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

DEFAULT_BASELINE_VARIANT = "claude_baseline"
DEFAULT_CANDIDATE_VARIANT = "gemini_tuned_v1"


#: Phase-2 artifacts to score, in preference order. Same list and same order
#: as ``amw.shadow.runner.PHASE2_ARTIFACTS`` — the scorecard and the shadow run
#: must default to the *same* run, or the shadow_agreement column would be
#: computed over a different corpus than the quality columns beside it.
#: ``phase2.json`` is an n=10 subset left over from development; scoring it by
#: default silently downgrades the report, so the full run wins.
PHASE2_ARTIFACTS: tuple[str, ...] = ("phase2_n70.json", "phase2.json")


def _default_results_path() -> Path:
    results = Path(__file__).resolve().parents[2] / "artifacts" / "results"
    for name in PHASE2_ARTIFACTS:
        if (results / name).is_file():
            return results / name
    return results / PHASE2_ARTIFACTS[0]


def parse_volume(spec: str) -> tuple[str, dict[str, int]]:
    """``SUBAGENT:CALLS_PER_DAY[:AVG_IN:AVG_OUT]`` from ``--volume``."""
    parts = spec.split(":")
    if len(parts) not in (2, 4):
        raise ValueError(
            f"--volume {spec!r}: expected SUBAGENT:CALLS_PER_DAY or "
            "SUBAGENT:CALLS_PER_DAY:AVG_IN_TOKENS:AVG_OUT_TOKENS"
        )
    try:
        values = {"calls_per_day": int(parts[1])}
        if len(parts) == 4:
            values["avg_input_tokens"] = int(parts[2])
            values["avg_output_tokens"] = int(parts[3])
    except ValueError:
        raise ValueError(
            f"--volume {spec!r}: the numeric fields must be integers"
        ) from None
    return parts[0], values


#: Which agreement figure the ``shadow_agreement`` gate is checked against.
#:
#: ``item`` is the item-level rate: an item agrees only if *every* field
#: matches, and prose fields are matched with a lexical proxy
#: (``token_jaccard_lexical_proxy``) rather than an embedding cosine or a
#: semantic judgement. ``structured`` drops prose fields entirely.
#:
#: These are not two views of the same thing. On the n=70 corpus they differ by
#: up to 8x (chunk_summarizer 0.129 vs 0.957), and the item-level figure tracks
#: the proxy's arbitrary threshold: moving it from 0.6 to 0.4 takes query
#: rewriter from 0.143 to 0.357. A gate checked against the item-level figure
#: is substantially measuring the instrument.
SHADOW_METRICS: tuple[str, ...] = ("item", "structured")
DEFAULT_SHADOW_METRIC = "structured"


def load_shadow(path: Path, metric: str = DEFAULT_SHADOW_METRIC) -> dict[str, Estimate]:
    """``{subagent: Estimate}`` from the shadow lane's artifact.

    Reads the shape ``cli.py shadow`` writes — a top-level ``subagents`` list of
    per-subagent records, each carrying an ``agreement`` block with both the
    item-level ``agreement`` estimate and the prose-free
    ``structured_agreement``. ``metric`` picks which one the gate sees; see
    :data:`SHADOW_METRICS` for why that choice is not cosmetic.

    Also accepts a bare ``{subagent: estimate}`` mapping, for a hand-built file
    or a future artifact version. An unreadable or unrecognised file raises: a
    silently skipped shadow file is a missing gate the operator believes was
    measured.
    """
    if metric not in SHADOW_METRICS:
        raise ValueError(
            f"unknown shadow metric {metric!r}; expected one of {list(SHADOW_METRICS)}."
        )
    data = json.loads(path.read_text(encoding="utf-8"))

    if isinstance(data, dict) and isinstance(data.get("subagents"), list):
        key = "agreement" if metric == "item" else "structured_agreement"
        out: dict[str, Estimate] = {}
        for record in data["subagents"]:
            name = record["subagent"]
            block = record.get("agreement") or {}
            value = block.get(key)
            if value is None:
                # Absent is not zero and not "agreed". Leaving the subagent out
                # of the mapping is what makes the gate render "not evaluated"
                # rather than inventing a bound to test.
                continue
            out[name] = Estimate.model_validate(value)
        if not out:
            raise ValueError(
                f"{path}: no subagent carries a {key!r} estimate. The shadow run "
                f"may have been written by an incompatible version."
            )
        return out

    if isinstance(data, dict) and "agreement" in data:
        data = data["agreement"]
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping of subagent -> estimate")
    return {name: Estimate.model_validate(value) for name, value in data.items()}


def load_adjudications(path: Path) -> dict[str, TriageSummary]:
    """``{subagent: TriageSummary}`` from the same shadow artifact.

    This is the evidence for ``shadow_agreement``'s ``alt`` clause, and it
    comes off disk beside the agreement estimates rather than being recomputed
    — the clause has to be decided on the same run the gate was checked
    against, or the two halves of the row describe different corpora.

    A record with no ``triage_summary`` is skipped, not defaulted. An absent
    triage is "the clause was not evaluated", which
    :func:`apply_alt_clause` says out loud; a zeroed summary would be
    "adjudicated, and it tied", which is a measurement nobody made.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if not (isinstance(data, dict) and isinstance(data.get("subagents"), list)):
        return {}
    out: dict[str, TriageSummary] = {}
    for record in data["subagents"]:
        summary = record.get("triage_summary")
        if summary:
            out[record["subagent"]] = TriageSummary.model_validate(summary)
    return out


def shadow_candidate_arm(path: Path) -> str:
    """Which candidate arm a shadow artifact was run against.

    Needed so the prior-arm comparison can *name* the arm it is comparing
    against instead of saying "the previous run". An artifact with no
    ``candidate_arm`` yields ``""`` and the comparison renders without a name
    rather than with a guessed one.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return ""
    arm = data.get("candidate_arm")
    return arm if isinstance(arm, str) else ""


def load_ladders(results_dir: Path) -> list[Any]:
    """Every ``ablation_{subagent}.json`` sitting beside the scored artifact.

    Read from the *artifact's own* directory rather than from a fixed path, so
    scoring a fixture run cannot pick up the real corpus's ladders and scoring
    the real run cannot miss them. An unparseable ladder raises rather than
    being skipped: a ladder silently dropped is a set of rungs the operator
    believes are in the report.
    """
    from amw.tuning.ablate import AblationResult

    ladders = []
    for path in sorted(results_dir.glob("ablation_*.json")):
        ladders.append(
            AblationResult.model_validate_json(path.read_text(encoding="utf-8"))
        )
    return ladders


def cmd_scorecard(args, cfg) -> int:
    """``cli.py scorecard`` — gates to verdicts to Markdown.

    Runs entirely offline. The phase-2 artifact is read from disk; the per-item
    vectors the paired-delta gates need are re-derived by replaying the same
    recorded calls (``--no-recompute`` skips that, at the cost of two gates
    going unevaluated).
    """
    results_path = Path(getattr(args, "results", None) or _default_results_path())
    if not results_path.is_file():
        print(
            f"no phase-2 artifact at {results_path}. Run `python cli.py phase2` "
            "first — the scorecard scores a run, it does not perform one.",
            file=sys.stderr,
        )
        return 4
    phase2 = Phase2Result.model_validate_json(results_path.read_text(encoding="utf-8"))

    baseline = getattr(args, "baseline_variant", None) or DEFAULT_BASELINE_VARIANT
    candidate = getattr(args, "candidate_variant", None) or DEFAULT_CANDIDATE_VARIANT

    volumes: VolumeSet | None = None
    specs = getattr(args, "volume", None) or []
    if specs:
        provider = getattr(args, "volumes_confirmed_by", None)
        if not provider:
            print(
                "--volume needs --volumes-confirmed-by NAME: customer-provided "
                "volumes are recorded with who supplied them and when, or the "
                "footer cannot say where the numbers came from.",
                file=sys.stderr,
            )
            return 2
        volumes = confirm_volumes(
            cfg, dict(parse_volume(spec) for spec in specs), provided_by=provider
        )

    samples = None
    if not getattr(args, "no_recompute", False):
        try:
            samples = collect_samples(
                cfg,
                phase2,
                mode=getattr(args, "mode", "replay"),
                dataset_dir=getattr(args, "dataset_dir", None),
                arms=[
                    (arm.subagent, arm.variant)
                    for arm in phase2.arms
                    if arm.variant in (baseline, candidate)
                ],
            )
        except EvidenceMismatchError as exc:
            # The artifact and the replay store disagree about what was scored.
            # Refuse, loudly and with the reason — a traceback reads like a bug
            # in the tool, and the operator's actual next move is to re-run
            # phase 2 or point --results at the artifact that matches.
            print(f"evidence check failed: {exc}", file=sys.stderr)
            print(
                f"the artifact being scored is {results_path}. Either score the "
                "artifact that matches the current replay store, or re-run "
                "`python cli.py phase2`. `--no-recompute` renders the card "
                "without the two paired-delta gates rather than guessing at them.",
                file=sys.stderr,
            )
            return 5

    # Traces carry no region, so the footer's region row is an input. Default
    # to the environment the run was configured with; let an operator state it
    # explicitly when rendering an old artifact from a different shell.
    regions = None
    claude_region = getattr(args, "claude_region", None)
    gemini_region = getattr(args, "gemini_region", None)
    if claude_region or gemini_region:
        gemini_region = gemini_region or cfg.customer.region
        regions = Regions(
            baseline=claude_region or gemini_region,
            candidate=gemini_region,
            source="--claude-region / --gemini-region",
        )

    shadow_path = getattr(args, "shadow", None)
    shadow_metric = getattr(args, "shadow_metric", None) or DEFAULT_SHADOW_METRIC

    # The control for an alt-clause pass. Refused loudly if the path is wrong:
    # a missing prior silently degrades the note from "this rung earned it" to
    # "the clause passed", which is a weaker claim rendered as if it were the
    # only one available.
    prior_path = getattr(args, "shadow_prior", None)
    prior_adjudications = None
    prior_arm = ""
    if prior_path:
        prior = Path(prior_path)
        if not prior.is_file():
            print(f"no shadow artifact at {prior}", file=sys.stderr)
            return 4
        prior_adjudications = load_adjudications(prior)
        prior_arm = shadow_candidate_arm(prior)

    # Imported here rather than at module scope: the scorecard package is
    # imported on every cli.py invocation and must not drag the eval runner in
    # with it. Only text crosses this boundary — see Scorecard.crosscheck_line.
    crosscheck_line = None
    crosscheck_path = getattr(args, "crosscheck", None)
    if crosscheck_path:
        from amw.eval.crosscheck import CrosscheckResult, crosscheck_footer_line

        path = Path(crosscheck_path)
        if not path.is_file():
            print(f"no cross-check artifact at {path}", file=sys.stderr)
            return 4
        crosscheck_line = crosscheck_footer_line(
            CrosscheckResult.model_validate_json(path.read_text(encoding="utf-8"))
        )

    ladders = None
    if not getattr(args, "no_ladder", False):
        ladders = load_ladders(results_path.parent)

    card = build_scorecard(
        cfg,
        phase2,
        volumes=volumes,
        ladders=ladders,
        cache_preamble_tokens=getattr(args, "cache_preamble_tokens", None),
        samples=samples,
        shadow=(
            load_shadow(Path(shadow_path), shadow_metric) if shadow_path else None
        ),
        shadow_metric=shadow_metric if shadow_path else None,
        # Same file, same run: the alt clause is evaluated on the adjudication
        # of the very disagreements the agreement estimate was computed over.
        adjudications=(
            load_adjudications(Path(shadow_path)) if shadow_path else None
        ),
        prior_adjudications=prior_adjudications,
        prior_arm=prior_arm,
        regions=regions,
        baseline_variant=baseline,
        candidate_variant=candidate,
        crosscheck_line=crosscheck_line,
    )
    markdown = render_markdown(card)

    out = getattr(args, "out", None)
    if out:
        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")
        print(f"scorecard written to {path}")
    else:
        print(markdown)

    for note in card.notes:
        print(f"note: {note}", file=sys.stderr)
    return 0
