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

from amw.config import AppConfig, GatesConfig
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
    EvidenceMismatchError,
    Regions,
    SubagentEvidence,
    build_evidence,
    collect_samples,
)

__all__ = [
    "INCOMPLETE",
    "UNDETERMINED",
    "TAXONOMY_LINE",
    "PARITY_SENTENCE",
    "VerdictRules",
    "SubagentVerdict",
    "Scorecard",
    "decide_verdict",
    "build_scorecard",
    "render_markdown",
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

#: Ground rule 7. The only parity claim this repo makes.
PARITY_SENTENCE = (
    "Gates are checked against 95% CI bounds, so a passing gate licenses "
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


def decide_verdict(
    evidence: SubagentEvidence, gates: GatesConfig, *, rules: VerdictRules | None = None
) -> SubagentVerdict:
    """Evaluate every measurable gate and apply ``gates.yaml``'s verdict rules."""
    rules = rules or VerdictRules.of(gates)
    checks = check_gates(
        evidence.estimates, gates, sentinel_values=evidence.sentinel_values
    )
    missing = missing_gates(checks, gates)
    failed = sorted(name for name, check in checks.items() if not check.passed)
    blocking_failed = [name for name in failed if name in rules.blocking_gates]

    if blocking_failed:
        # A blocking gate that actually failed is a finding on its own terms.
        # Unlike every other pattern it does not need the full gate set to be
        # meaningful, so it is the one verdict issued over an incomplete run.
        return SubagentVerdict(
            subagent=evidence.subagent,
            verdict=rules.any_blocking_gate_fails,
            checks=checks,
            failed=failed,
            missing=missing,
            rationale=(
                f"blocking gate(s) {', '.join(blocking_failed)} failed on the CI "
                f"bound. {rules.descriptions.get(rules.any_blocking_gate_fails, '')}"
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
            missing=missing,
            rationale=(
                f"{len(checks)} of {len(gates.subagent_gates)} pre-agreed gates were "
                f"measured; {', '.join(missing)} were not. A verdict over a subset of "
                f"the gates is not the verdict that was agreed, so none is issued. "
                f"Were every unmeasured gate to pass, it would be {provisional}."
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
        missing=missing,
        rationale=rationale,
    )


# --------------------------------------------------------------------------
# the scorecard
# --------------------------------------------------------------------------


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


def _gate_table(
    evidence: SubagentEvidence,
    verdict: SubagentVerdict,
    gates: GatesConfig,
    *,
    prices_verified: bool,
) -> list[str]:
    lines = [
        "| Gate | Bound (gates.yaml) | Measured (95% CI) | Bound tested | Result |",
        "| --- | --- | --- | --- | --- |",
    ]
    imprecise: list[str] = []
    for gate_name in gates.subagent_gates:
        check = verdict.checks.get(gate_name)
        measured = _measured_cell(
            gate_name, evidence, check, prices_verified=prices_verified
        )
        if check is None:
            tested, result = "not evaluated", "not evaluated"
        else:
            tested = f"{check.compared_bound} = {check.compared_value:.4g}"
            result = "PASS" if check.passed else "**FAIL**"
            if not check.passed:
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


def _evidence_table(evidence: SubagentEvidence, *, prices_verified: bool) -> list[str]:
    claude_schema = evidence.claude_schema_validity
    cost = cost_cell(prices_verified=prices_verified)
    rows = [
        (
            f"Claude `json_schema_validity` (`{evidence.baseline_variant}`)",
            claude_schema.render() if claude_schema else "not measured",
        ),
        (
            f"Gemini `json_schema_validity` (`{evidence.candidate_variant}`)",
            estimate_text(evidence.candidate_schema_validity),
        ),
        (
            "Judge score — Claude",
            evidence.judge_baseline.render() if evidence.judge_baseline else "not measured",
        ),
        (
            "Judge score — Gemini",
            evidence.judge_candidate.render()
            if evidence.judge_candidate
            else "not measured",
        ),
        ("Latency p95", _latency_text(evidence)),
        ("Cost per call", cost),
        ("Monthly run rate", cost),
        ("Annual run rate", cost),
        ("Cost savings vs Claude", cost),
    ]
    return ["| Evidence | Value |", "| --- | --- |"] + [
        f"| {label} | {value} |" for label, value in rows
    ]


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
        f"| Bootstrap | 95% CI, seed `{footer['bootstrap_seed']}` |",
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
        "| Subagent | Baseline | Candidate | Gates evaluated | Verdict | Why |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for evidence in card.evidence:
        verdict = card.verdicts[evidence.subagent]
        label = f"**{verdict.verdict}**"
        if verdict.provisional:
            label += f" (provisional: {verdict.provisional})"
        lines.append(
            f"| {_pretty(evidence.subagent)} | `{evidence.baseline_variant}` | "
            f"`{evidence.candidate_variant}` | "
            f"{verdict.evaluated} of {verdict.total_gates} | {label} | "
            f"{verdict.rationale} |"
        )

    for evidence in card.evidence:
        verdict = card.verdicts[evidence.subagent]
        lines += ["", f"## {_pretty(evidence.subagent)}", ""]
        lines += _gate_table(
            evidence, verdict, card.gates, prices_verified=prices_verified
        )
        lines += ["", "### Evidence", ""]
        lines += _evidence_table(evidence, prices_verified=prices_verified)
        if evidence.unmeasured:
            lines += ["", "**Not evaluated, and why**", ""]
            lines += [
                f"- `{name}` — {evidence.unmeasured[name]}"
                for name in sorted(evidence.unmeasured)
            ]
        if evidence.notes:
            lines += [""] + [f"- {note}" for note in evidence.notes]

    lines += [""] + _economics_section(card)
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

    card = build_scorecard(
        cfg,
        phase2,
        volumes=volumes,
        cache_preamble_tokens=getattr(args, "cache_preamble_tokens", None),
        samples=samples,
        shadow=(
            load_shadow(Path(shadow_path), shadow_metric) if shadow_path else None
        ),
        shadow_metric=shadow_metric if shadow_path else None,
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
