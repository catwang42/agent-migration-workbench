"""T12 — gates to verdicts.

The verdict is the one line a customer will quote back, so these tests are
mostly about the ways it could be *wrong in the safe-looking direction*:

- a gate nothing measured must never round to a pass, and a verdict computed
  over a subset of the pre-agreed gates must not be issued as though it were
  the agreed verdict;
- the comparison must be against the CI bound, not the point estimate, so a
  metric whose mean clears the bound but whose interval does not still fails;
- the cross-region latency measurement must be structurally incapable of
  producing a pass.

No threshold appears as a literal here. Every fixture estimate is positioned
*relative to the bound read from* ``config/gates.yaml``, so editing that file
moves the fixtures with it and these tests keep testing the shipped gates.
Likewise no verdict name is written down: the tests assert against
``VerdictRules``, which reads the names out of the YAML.
"""

from __future__ import annotations

import copy

import pytest

from amw.config import AppConfig, ConfigError, GatesConfig, load_all
from amw.eval.stats import Estimate, check_gate
from amw.reporting.evidence import (
    GATE_COST,
    GATE_GROUNDEDNESS,
    GATE_LATENCY,
    GATE_QUALITY,
    GATE_SCHEMA,
    GATE_SHADOW,
    Regions,
    SameRegionLatencyProbe,
    SubagentEvidence,
)
from amw.reporting.scorecard import (
    INCOMPLETE,
    UNDETERMINED,
    SubagentVerdict,
    VerdictRules,
    decide_verdict,
)

#: How far a fixture estimate sits from whatever bound gates.yaml declares.
#: A step, not a threshold — the bound itself is always read from the config.
_STEP = {"fraction": 0.005, "percentage_points": 0.5, "percent": 5.0, "ms": 50.0}

_UNIT = {
    GATE_QUALITY: "percentage_points",
    GATE_GROUNDEDNESS: "percentage_points",
    GATE_SCHEMA: "fraction",
    GATE_SHADOW: "fraction",
    GATE_COST: "percent",
    GATE_LATENCY: "ms",
}

#: The measured Claude p95 that resolves the `claude_baseline_p95` sentinel.
#: Only a same-region probe may supply it; the value is arbitrary here.
SENTINEL_P95_MS = 900.0


@pytest.fixture(scope="module")
def cfg() -> AppConfig:
    return load_all()


@pytest.fixture(scope="module")
def gates(cfg: AppConfig) -> GatesConfig:
    return cfg.gates


@pytest.fixture(scope="module")
def rules(gates: GatesConfig) -> VerdictRules:
    return VerdictRules.of(gates)


def _bound(gates: GatesConfig, name: str) -> float:
    gate = gates.gate(name)
    return SENTINEL_P95_MS if gate.is_sentinel else float(gate.bound)


def _estimate(gates: GatesConfig, name: str, *, passes: bool) -> Estimate:
    """An interval placed on the passing or failing side of the real bound."""
    gate = gates.gate(name)
    unit = _UNIT[name]
    step = _STEP[unit]
    bound = _bound(gates, name)
    if gate.direction == "min":
        lo = bound + step if passes else bound - step
        hi = lo + step
    else:
        hi = bound - step if passes else bound + step
        lo = hi - step
    if unit == "fraction":
        lo, hi = min(max(lo, 0.0), 1.0), min(max(hi, 0.0), 1.0)
    return Estimate(
        metric=name, point=(lo + hi) / 2, lo=lo, hi=hi, n=28, unit=unit  # type: ignore[arg-type]
    )


def _evidence(
    gates: GatesConfig,
    *,
    failing: tuple[str, ...] = (),
    omit: tuple[str, ...] = (),
    subagent: str = "query_rewriter",
) -> SubagentEvidence:
    """A subagent with every pre-agreed gate measured except ``omit``."""
    estimates = {
        name: _estimate(gates, name, passes=name not in failing)
        for name in gates.subagent_gates
        if name not in omit
    }
    return SubagentEvidence(
        subagent=subagent,
        baseline_variant="claude_baseline",
        candidate_variant="gemini_tuned_v1",
        estimates=estimates,
        unmeasured={name: "omitted by this fixture" for name in omit},
        sentinel_values={"claude_baseline_p95": SENTINEL_P95_MS},
        regions=Regions(baseline="global", candidate="us-central1", source="fixture"),
    )


# --------------------------------------------------------------------------
# the rules come out of the YAML, not out of this file
# --------------------------------------------------------------------------


def test_verdict_names_are_read_from_gates_yaml(
    gates: GatesConfig, rules: VerdictRules
) -> None:
    by_rule = {rule.rule: name for name, rule in gates.verdicts.items()}
    assert rules.all_pass == by_rule["all_pass"]
    assert rules.only_quality_gates_fail == by_rule["only_quality_gates_fail"]
    assert rules.any_blocking_gate_fails == by_rule["any_blocking_gate_fails"]
    assert rules.quality_gates and rules.blocking_gates
    assert set(rules.quality_gates) | set(rules.blocking_gates) <= set(
        gates.subagent_gates
    )


def test_two_verdicts_sharing_a_rule_is_refused(gates: GatesConfig) -> None:
    verdicts = copy.deepcopy(gates.verdicts)
    (first, second) = list(verdicts)[:2]
    verdicts[second] = verdicts[second].model_copy(
        update={"rule": verdicts[first].rule}
    )
    with pytest.raises(ValueError, match="two verdicts with rule"):
        VerdictRules.of(gates.model_copy(update={"verdicts": verdicts}))


def test_a_missing_rule_is_refused(gates: GatesConfig) -> None:
    verdicts = {
        name: rule
        for name, rule in gates.verdicts.items()
        if rule.rule != "all_pass"
    }
    with pytest.raises(ValueError, match="no verdict for rule"):
        VerdictRules.of(gates.model_copy(update={"verdicts": verdicts}))


# --------------------------------------------------------------------------
# the four verdict fixtures
# --------------------------------------------------------------------------


def test_all_gates_pass_gives_the_all_pass_verdict(
    gates: GatesConfig, rules: VerdictRules
) -> None:
    verdict = decide_verdict(_evidence(gates), gates, rules=rules)
    assert verdict.verdict == rules.all_pass
    assert verdict.failed == [] and verdict.missing == []
    assert verdict.evaluated == verdict.total_gates == len(gates.subagent_gates)


def test_forced_tune_first_fixture(gates: GatesConfig, rules: VerdictRules) -> None:
    """Card requirement: a fixture that must come out TUNE_FIRST.

    Every gate measured, every blocking gate held, and only gates in the
    verdict rule's own ``quality`` list are short.
    """
    evidence = _evidence(gates, failing=tuple(rules.quality_gates))
    verdict = decide_verdict(evidence, gates, rules=rules)
    assert verdict.verdict == rules.only_quality_gates_fail
    assert sorted(verdict.failed) == sorted(rules.quality_gates)
    assert verdict.missing == []
    assert verdict.provisional is None
    assert "quality gate" in verdict.rationale


def test_one_quality_gate_short_is_also_tune_first(
    gates: GatesConfig, rules: VerdictRules
) -> None:
    verdict = decide_verdict(
        _evidence(gates, failing=(GATE_QUALITY,)), gates, rules=rules
    )
    assert verdict.verdict == rules.only_quality_gates_fail


def test_forced_hold_fixture(gates: GatesConfig, rules: VerdictRules) -> None:
    """A blocking gate that actually failed."""
    blocking = rules.blocking_gates[0]
    verdict = decide_verdict(_evidence(gates, failing=(blocking,)), gates, rules=rules)
    assert verdict.verdict == rules.any_blocking_gate_fails
    assert blocking in verdict.rationale


def test_hold_stands_even_when_other_gates_went_unmeasured(
    gates: GatesConfig, rules: VerdictRules
) -> None:
    """A structural failure is a finding on its own terms — it does not need
    the full gate set to mean something, so it is the one verdict issued over
    an incomplete run."""
    blocking = rules.blocking_gates[0]
    others = tuple(
        n for n in gates.subagent_gates if n not in rules.blocking_gates
    )[:2]
    verdict = decide_verdict(
        _evidence(gates, failing=(blocking,), omit=others), gates, rules=rules
    )
    assert verdict.verdict == rules.any_blocking_gate_fails
    assert verdict.missing == sorted(others)


def test_a_non_quality_non_blocking_failure_is_undetermined(
    gates: GatesConfig, rules: VerdictRules
) -> None:
    unclassified = [
        n
        for n in gates.subagent_gates
        if n not in rules.quality_gates and n not in rules.blocking_gates
    ]
    verdict = decide_verdict(
        _evidence(gates, failing=(unclassified[0],)), gates, rules=rules
    )
    assert verdict.verdict == UNDETERMINED
    assert "matches no verdict rule" in verdict.rationale


# --------------------------------------------------------------------------
# a gap is never a pass
# --------------------------------------------------------------------------


@pytest.mark.parametrize("omitted", sorted(load_all().gates.subagent_gates))
def test_any_single_missing_gate_blocks_the_migrate_verdict(
    gates: GatesConfig, rules: VerdictRules, omitted: str
) -> None:
    verdict = decide_verdict(_evidence(gates, omit=(omitted,)), gates, rules=rules)
    assert verdict.verdict == INCOMPLETE
    assert verdict.verdict != rules.all_pass
    assert verdict.missing == [omitted]
    # The verdict it *would* have earned is shown, and labelled provisional.
    assert verdict.provisional == rules.all_pass
    assert "not the verdict that was agreed" in verdict.rationale


def test_incomplete_reports_how_much_of_the_agreed_set_was_measured(
    gates: GatesConfig, rules: VerdictRules
) -> None:
    omit = tuple(list(gates.subagent_gates)[:3])
    verdict = decide_verdict(_evidence(gates, omit=omit), gates, rules=rules)
    assert verdict.evaluated == len(gates.subagent_gates) - len(omit)
    assert verdict.total_gates == len(gates.subagent_gates)
    assert f"of {len(gates.subagent_gates)} pre-agreed gates" in verdict.rationale


def test_measuring_nothing_at_all_is_incomplete_not_migrate(
    gates: GatesConfig, rules: VerdictRules
) -> None:
    # The degenerate case the "all_pass" rule would otherwise wave through:
    # zero gates checked means zero gates failed.
    verdict = decide_verdict(
        _evidence(gates, omit=tuple(gates.subagent_gates)), gates, rules=rules
    )
    assert verdict.verdict == INCOMPLETE
    assert verdict.evaluated == 0


def test_incomplete_is_not_a_verdict_name_in_gates_yaml(gates: GatesConfig) -> None:
    assert INCOMPLETE not in gates.verdicts
    assert UNDETERMINED not in gates.verdicts


# --------------------------------------------------------------------------
# the bound that gets tested is the CI bound
# --------------------------------------------------------------------------


def test_a_min_gate_is_tested_on_the_ci_lower_bound(gates: GatesConfig) -> None:
    """Point estimate above the bound, interval straddling it: FAIL."""
    bound = _bound(gates, GATE_SCHEMA)
    step = _STEP["fraction"]
    estimate = Estimate(
        metric=GATE_SCHEMA,
        point=min(bound + step, 1.0),
        lo=bound - step,
        hi=1.0,
        n=70,
    )
    check = check_gate(GATE_SCHEMA, estimate, gates)
    assert check.compared_bound == "ci_lower"
    assert check.compared_value == pytest.approx(estimate.lo)
    assert check.passed is False
    assert estimate.point > check.bound  # the point alone would have passed


def test_a_max_gate_is_tested_on_the_ci_upper_bound(gates: GatesConfig) -> None:
    step = _STEP["ms"]
    estimate = Estimate(
        metric=GATE_LATENCY,
        point=SENTINEL_P95_MS - step,
        lo=SENTINEL_P95_MS - 2 * step,
        hi=SENTINEL_P95_MS + step,
        n=70,
        unit="ms",
    )
    check = check_gate(
        GATE_LATENCY,
        estimate,
        gates,
        sentinel_values={"claude_baseline_p95": SENTINEL_P95_MS},
    )
    assert check.compared_bound == "ci_upper"
    assert check.passed is False
    assert check.bound_source.startswith("sentinel:")


def test_every_check_records_the_gates_version_it_used(
    gates: GatesConfig, rules: VerdictRules
) -> None:
    verdict = decide_verdict(_evidence(gates), gates, rules=rules)
    for check in verdict.checks.values():
        assert check.gates_version == gates.version
        assert check.gates_version_hash == gates.version_hash


# --------------------------------------------------------------------------
# latency: a cross-region input cannot become a pass
# --------------------------------------------------------------------------


def test_a_cross_region_probe_cannot_be_constructed() -> None:
    """Rule (b)'s enforcement point. Claude ran in `global`, Gemini in
    `us-central1`; the type refuses to hold both."""
    with pytest.raises(ValueError, match="one region"):
        SameRegionLatencyProbe(
            region="us-central1",
            candidate_p95=Estimate(
                metric=GATE_LATENCY, point=1.0, lo=1.0, hi=1.0, n=1, unit="ms"
            ),
            baseline_p95_ms=SENTINEL_P95_MS,
            baseline_region="global",
            candidate_region="us-central1",
            probed_on="2026-08-12",
        )


def test_a_probe_must_be_in_milliseconds() -> None:
    with pytest.raises(ValueError, match="milliseconds"):
        SameRegionLatencyProbe(
            region="us-central1",
            candidate_p95=Estimate(metric=GATE_LATENCY, point=1.0, lo=1.0, hi=1.0, n=1),
            baseline_p95_ms=SENTINEL_P95_MS,
            baseline_region="us-central1",
            candidate_region="us-central1",
            probed_on="2026-08-12",
        )


def test_without_a_probe_the_latency_gate_is_missing_never_passed(
    cfg: AppConfig, gates: GatesConfig, rules: VerdictRules
) -> None:
    evidence = _evidence(gates, omit=(GATE_LATENCY,))
    verdict = decide_verdict(evidence, gates, rules=rules)
    assert GATE_LATENCY in verdict.missing
    assert GATE_LATENCY not in verdict.checks
    assert verdict.verdict == INCOMPLETE


def test_a_same_region_probe_does_unlock_the_gate(gates: GatesConfig) -> None:
    """The override exists and works — it is just not available for this run."""
    probe = SameRegionLatencyProbe(
        region="us-central1",
        candidate_p95=_estimate(gates, GATE_LATENCY, passes=True),
        baseline_p95_ms=SENTINEL_P95_MS,
        baseline_region="us-central1",
        candidate_region="us-central1",
        probed_on="2026-08-12",
    )
    check = check_gate(
        GATE_LATENCY,
        probe.candidate_p95,
        gates,
        sentinel_values={"claude_baseline_p95": probe.baseline_p95_ms},
    )
    assert check.passed is True


def test_an_unresolved_sentinel_is_a_hard_error(gates: GatesConfig) -> None:
    """Never a silently skipped gate — gates.yaml says so explicitly."""
    evidence = _evidence(gates)
    evidence = evidence.model_copy(update={"sentinel_values": {}})
    with pytest.raises(ConfigError, match="sentinel"):
        decide_verdict(evidence, gates)


# --------------------------------------------------------------------------
# unit guard rail
# --------------------------------------------------------------------------


def test_a_fraction_cannot_be_compared_against_a_percentage_point_gate(
    gates: GatesConfig,
) -> None:
    raw = Estimate(metric=GATE_QUALITY, point=0.01, lo=-0.01, hi=0.03, n=28)
    with pytest.raises(ValueError, match="percentage_points"):
        check_gate(GATE_QUALITY, raw, gates)
    # Rescaled explicitly, the same numbers are admissible.
    assert check_gate(GATE_QUALITY, raw.to_percentage_points(), gates).passed is True


def test_verdict_is_serialisable(gates: GatesConfig) -> None:
    # The scorecard embeds these; a verdict that cannot round-trip cannot be
    # attached to an artifact.
    verdict = decide_verdict(_evidence(gates), gates)
    assert SubagentVerdict.model_validate_json(verdict.model_dump_json()) == verdict
