"""Golden tests for amw/eval/stats.py.

The bootstrap goldens here are **analytic**, not captured. Each one uses a
sample small enough that the exact resampling distribution can be written down,
so the expected interval is derived on paper and then checked to be
seed-invariant — a bound that only holds for one seed would be a snapshot, not
a golden.

The gate goldens are the ones that matter most to the project's promise: they
pin down that a ``min`` gate reads the **CI lower bound** and a ``max`` gate the
**upper**, and include cases where the point estimate passes and the bound does
not (ground rule 7 / CLAUDE.md).
"""

from __future__ import annotations

import math

import pytest

from amw.config import ConfigError, load_all
from amw.eval import stats as S
from amw.eval.metrics import MetricSample

GATES = load_all().gates


def estimate(**kwargs) -> S.Estimate:
    base = dict(metric="m", point=0.0, lo=0.0, hi=0.0, n=10)
    base.update(kwargs)
    return S.Estimate(**base)


# ==========================================================================
# means
# ==========================================================================


def test_mean_golden():
    """8 ones and 2 zeros -> 8/10."""
    assert S.mean([1, 1, 1, 0, 1, 0, 1, 1, 1, 1]) == 0.8


def test_mean_of_nothing_raises_rather_than_returning_zero():
    with pytest.raises(S.InsufficientDataError, match="fabricated"):
        S.mean([])


# ==========================================================================
# bootstrap CI — analytic goldens
# ==========================================================================


def test_bootstrap_ci_golden_two_point_sample():
    """GOLDEN, derived analytically.

    values = [0, 1], n = 2. Resampling 2 items with replacement gives
    mean 0.0 with p=1/4, 0.5 with p=1/2, 1.0 with p=1/4.

    The 2.5th percentile falls inside the 25% mass at 0.0  -> lo = 0.0
    The 97.5th percentile falls inside the 25% mass at 1.0 -> hi = 1.0
    """
    e = S.bootstrap_ci([0.0, 1.0], metric="v")
    assert (e.point, e.lo, e.hi) == (0.5, 0.0, 1.0)
    assert e.n == 2


def test_bootstrap_ci_golden_three_of_four():
    """GOLDEN, derived analytically, and seed-invariant.

    values = [0, 1, 1, 1]. A resample draws a 1 with probability 3/4, so the
    resample mean is k/4 with k ~ Binomial(4, 3/4):

        P(k=0) =  1/256 = 0.39%     cumulative  0.39%
        P(k=1) = 12/256 = 4.69%     cumulative  5.08%
        ...
        P(k=4) = 81/256 = 31.6%     cumulative   100%

    2.5%  falls above the 0.39% mass at 0.00 and inside the band ending at
          5.08%, so lo = 0.25.
    97.5% falls inside the top 31.6% mass at 1.00, so hi = 1.0.

    Both boundaries are many standard errors from a 10,000-resample tie, which
    is why every seed below agrees.
    """
    for seed in (1, 2, 3, 7, S.DEFAULT_BOOTSTRAP_SEED):
        e = S.bootstrap_ci([0.0, 1.0, 1.0, 1.0], metric="v", seed=seed)
        assert (e.point, e.lo, e.hi) == (0.75, 0.25, 1.0), f"seed {seed}"


def test_bootstrap_ci_of_a_constant_sample_has_zero_width():
    """GOLDEN. Every resample of 20 identical values has the same mean."""
    e = S.bootstrap_ci([1.0] * 20, metric="json_schema_validity")
    assert (e.point, e.lo, e.hi) == (1.0, 1.0, 1.0)
    assert e.width == 0.0


def test_bootstrap_ci_is_deterministic_for_a_given_seed():
    values = [i / 60 for i in range(60)]
    a = S.bootstrap_ci(values, metric="v", seed=4242)
    b = S.bootstrap_ci(values, metric="v", seed=4242)
    assert (a.lo, a.hi) == (b.lo, b.hi)


def test_bootstrap_ci_seed_actually_drives_the_resampling():
    """A seed that changed nothing would make `seed=` a decorative argument."""
    values = [i / 60 for i in range(60)]
    a = S.bootstrap_ci(values, metric="v", seed=1)
    b = S.bootstrap_ci(values, metric="v", seed=2)
    assert (a.lo, a.hi) != (b.lo, b.hi)
    assert a.point == b.point  # the point estimate is not resampled


def test_bootstrap_defaults_are_the_documented_ones():
    e = S.bootstrap_ci([0.0, 1.0], metric="v")
    assert e.resamples == 10_000 == S.BOOTSTRAP_RESAMPLES
    assert e.confidence == 0.95
    assert e.seed == S.DEFAULT_BOOTSTRAP_SEED
    assert e.method == "percentile_bootstrap"


def test_bootstrap_refuses_a_single_observation():
    with pytest.raises(S.InsufficientDataError, match="at least 2"):
        S.bootstrap_ci([1.0], metric="v")


def test_bootstrap_reads_a_metric_sample_directly():
    sample = MetricSample(
        metric="citation_coverage", values=[1.0, 1.0, 0.0, 1.0], item_ids=list("abcd")
    )
    e = S.bootstrap_ci(sample)
    assert e.metric == "citation_coverage"
    assert e.point == 0.75
    assert e.n == 4


def test_estimate_bounds_must_be_ordered():
    with pytest.raises(ValueError, match="exceeds"):
        estimate(lo=1.0, hi=0.0)


# ==========================================================================
# paired bootstrap
# ==========================================================================


def test_paired_delta_of_identical_arms_is_exactly_zero():
    """GOLDEN, and the proof that the bootstrap is paired.

    The same item vector in both arms: at every resampled index the two
    values cancel, so every resample delta is exactly 0 and the interval has
    zero width. Resampling the arms independently could not produce this.
    """
    arm = [0.0, 0.0, 1.0, 1.0, 0.0, 1.0, 0.0, 1.0, 1.0, 0.0]
    e = S.paired_bootstrap_delta(arm, list(arm), metric="quality")
    assert (e.point, e.lo, e.hi) == (0.0, 0.0, 0.0)

    # …whereas two independent bootstraps of the same data do have width.
    solo = S.bootstrap_ci(arm, metric="quality")
    assert solo.width > 0


def test_paired_delta_of_a_constant_shift_is_exactly_the_shift():
    """GOLDEN. candidate = baseline + 1 elementwise -> every delta is 1.0."""
    e = S.paired_bootstrap_delta([0, 0, 1, 1], [1, 1, 2, 2], metric="quality")
    assert (e.point, e.lo, e.hi) == (1.0, 1.0, 1.0)
    assert e.method == "paired_percentile_bootstrap"


def test_paired_delta_sign_is_candidate_minus_baseline():
    """Gemini (candidate) below Claude (baseline) must read negative."""
    e = S.paired_bootstrap_delta([1.0, 1.0, 1.0, 1.0], [0.0, 0.0, 0.0, 0.0])
    assert e.point == -1.0


def test_paired_delta_needs_aligned_arms():
    with pytest.raises(ValueError, match="unpaired arms"):
        S.paired_bootstrap_delta([0.0, 1.0], [0.0, 1.0, 1.0])


def test_pair_samples_aligns_on_item_id_and_names_the_drops():
    base = MetricSample(metric="q", values=[1.0, 0.0, 1.0], item_ids=["i1", "i2", "i3"])
    cand = MetricSample(metric="q", values=[0.0, 1.0, 1.0], item_ids=["i3", "i1", "i9"])
    pair = S.pair_samples(base, cand)

    # baseline order is preserved, i2 has no counterpart, i9 has no baseline
    assert pair.item_ids == ["i1", "i3"]
    assert pair.baseline == [1.0, 1.0]
    assert pair.candidate == [1.0, 0.0]
    assert pair.dropped_baseline_only == ["i2"]
    assert pair.dropped_candidate_only == ["i9"]
    assert pair.n_dropped == 2


def test_paired_delta_from_metric_samples_reports_the_drops():
    base = MetricSample(metric="q", values=[1.0, 1.0, 1.0], item_ids=["i1", "i2", "i3"])
    cand = MetricSample(metric="q", values=[0.0, 0.0], item_ids=["i1", "i2"])
    e = S.paired_bootstrap_delta(base, cand)
    assert e.point == -1.0
    assert e.paired_n == 2
    assert e.dropped_unpaired == 1


def test_pairing_refuses_ambiguous_duplicate_item_ids():
    base = MetricSample(metric="q", values=[1.0, 0.0], item_ids=["i1", "i1"])
    cand = MetricSample(metric="q", values=[1.0, 0.0], item_ids=["i1", "i2"])
    with pytest.raises(ValueError, match="duplicate item_ids"):
        S.pair_samples(base, cand)


# ==========================================================================
# units
# ==========================================================================


def test_percentage_point_conversion_scales_every_bound():
    e = estimate(point=-0.012, lo=-0.031, hi=0.007, unit="fraction")
    pp = e.to_percentage_points()
    assert pp.unit == "percentage_points"
    assert pp.point == pytest.approx(-1.2)
    assert pp.lo == pytest.approx(-3.1)
    assert pp.hi == pytest.approx(0.7)
    # idempotent: converting twice must not multiply by 100 twice
    assert pp.to_percentage_points() == pp


def test_percentage_point_conversion_refuses_a_nonsense_source_unit():
    with pytest.raises(ValueError, match="percentage points"):
        estimate(unit="ms").to_percentage_points()


# ==========================================================================
# the gate check — CI bounds, never point estimates
# ==========================================================================


def test_min_gate_reads_the_ci_lower_bound():
    """GOLDEN. quality_delta_pp min is -2.0; lower bound -1.9 clears it."""
    e = estimate(metric="quality_delta_pp", point=-0.5, lo=-1.9, hi=0.9,
                 unit="percentage_points")
    check = S.check_gate("quality_delta_pp", e, GATES)
    assert check.passed is True
    assert check.compared_bound == "ci_lower"
    assert check.compared_value == -1.9
    assert check.bound == -2.0
    assert check.bound_source == "gates.yaml:quality_delta_pp.min"


def test_a_passing_point_estimate_does_not_pass_the_gate():
    """THE test for ground rule 7.

    Point estimate -0.5pp sits comfortably inside the -2.0pp gate. The CI
    lower bound is -2.1pp and does not. The gate must FAIL, because
    "quality parity within measurement" is a claim about the interval.
    """
    e = estimate(metric="quality_delta_pp", point=-0.5, lo=-2.1, hi=1.1,
                 unit="percentage_points")
    check = S.check_gate("quality_delta_pp", e, GATES)
    assert check.passed is False
    assert check.compared_value == -2.1
    assert "FAIL" in check.describe()


def test_schema_validity_gate_also_reads_the_lower_bound():
    """point 0.995 clears min 0.99; lower bound 0.985 does not."""
    e = estimate(metric="json_schema_validity", point=0.995, lo=0.985, hi=1.0)
    assert S.check_gate("json_schema_validity", e, GATES).passed is False

    tight = estimate(metric="json_schema_validity", point=1.0, lo=1.0, hi=1.0)
    assert S.check_gate("json_schema_validity", tight, GATES).passed is True


def test_max_gate_reads_the_ci_upper_bound_and_resolves_its_sentinel():
    """latency_p95's bound is the sentinel claude_baseline_p95, supplied by the
    caller from a measured baseline. The gate is tested on the CI upper bound."""
    e = estimate(metric="latency_p95", point=800, lo=690, hi=910, unit="ms")
    check = S.check_gate(
        "latency_p95", e, GATES, sentinel_values={"claude_baseline_p95": 900.0}
    )
    assert check.direction == "max"
    assert check.compared_bound == "ci_upper"
    assert check.compared_value == 910
    assert check.bound == 900.0
    assert check.bound_source == "sentinel:claude_baseline_p95"
    assert check.passed is False

    faster = estimate(metric="latency_p95", point=780, lo=670, hi=880, unit="ms")
    assert S.check_gate(
        "latency_p95", faster, GATES, sentinel_values={"claude_baseline_p95": 900.0}
    ).passed is True


def test_an_unresolved_sentinel_is_an_error_not_a_skipped_gate():
    e = estimate(metric="latency_p95", point=800, lo=690, hi=910, unit="ms")
    with pytest.raises(ConfigError, match="claude_baseline_p95"):
        S.check_gate("latency_p95", e, GATES)


def test_a_unit_mismatch_cannot_pass_a_gate_by_accident():
    """0.98 as a fraction would sail past a -2.0 percentage-point bound."""
    e = estimate(metric="quality_delta_pp", point=-0.005, lo=-0.019, hi=0.009)
    with pytest.raises(ValueError, match="percentage_points"):
        S.check_gate("quality_delta_pp", e, GATES)


def test_cost_savings_gate_expects_percent():
    ok = estimate(metric="cost_savings_pct", point=44.0, lo=31.0, hi=57.0,
                  unit="percent")
    assert S.check_gate("cost_savings_pct", ok, GATES).passed is True
    with pytest.raises(ValueError, match="percent"):
        S.check_gate("cost_savings_pct", ok.model_copy(update={"unit": "fraction"}), GATES)


def test_unknown_gate_names_fail_loudly():
    with pytest.raises(ConfigError, match="unknown gate"):
        S.check_gate("vibes", estimate(), GATES)


def test_gate_check_carries_the_gates_version_hash_for_the_footer():
    e = estimate(metric="json_schema_validity", point=1.0, lo=1.0, hi=1.0)
    check = S.check_gate("json_schema_validity", e, GATES)
    assert check.gates_version == GATES.version
    assert check.gates_version_hash == GATES.version_hash
    assert len(check.gates_version_hash) == 12
    assert check.basis.startswith("95% CI lower bound")


def test_gate_alt_clause_is_carried_not_silently_dropped():
    e = estimate(metric="shadow_agreement", point=0.93, lo=0.88, hi=0.97)
    check = S.check_gate("shadow_agreement", e, GATES)
    assert check.passed is False
    assert check.alt and "wins >= losses" in check.alt


def test_check_gates_and_missing_gates_expose_the_unmeasured_ones():
    estimates = {
        "json_schema_validity": estimate(
            metric="json_schema_validity", point=1.0, lo=1.0, hi=1.0
        ),
        "shadow_agreement": estimate(
            metric="shadow_agreement", point=0.95, lo=0.92, hi=0.98
        ),
    }
    checks = S.check_gates(estimates, GATES)
    assert set(checks) == set(estimates)
    assert all(c.passed for c in checks.values())
    assert S.missing_gates(checks, GATES) == [
        "cost_savings_pct",
        "groundedness_delta_pp",
        "latency_p95",
        "quality_delta_pp",
    ]


def test_no_point_estimate_shortcut_is_offered():
    """A helper comparing a mean to a threshold would quietly break the
    project's parity language, so the module must not expose one."""
    banned = [
        name
        for name in dir(S)
        if not name.startswith("_")
        and ("threshold" in name.lower() or "check_mean" in name.lower())
    ]
    assert banned == []


# ==========================================================================
# judge repeats
# ==========================================================================


def test_aggregate_repeats_golden():
    """GOLDEN over four items at k=2.

    i1 [1.0, 0.5] -> mean 0.75; sample sd (ddof=1) = sqrt(0.125) = 0.3535533905932738
                     range 0.5; the two repeats disagree
    i2 [1.0, 1.0] -> mean 1.0; sd 0.0; range 0.0; the repeats agree
    i3 [0.5, None] -> mean 0.5 over its one good repeat; short of k
    i4 [None, None] -> no score at all; dropped, never scored 0.0

    failed_repeats       = 1 (i3) + 2 (i4)          = 3
    mean_within_item_sd  = (0.3535533905932738 + 0)/2 = 0.1767766952966369
                           (only i1 and i2 have k>=2)
    max_within_item_range = 0.5
    full_agreement_rate  = 1 of 2 multi-repeat items = 0.5
    """
    agg = S.aggregate_repeats(
        {
            "i1": [1.0, 0.5],
            "i2": [1.0, 1.0],
            "i3": [0.5, None],
            "i4": [None, None],
        },
        metric="judge_score",
        expected_k=2,
    )
    assert agg.item_means == {"i1": 0.75, "i2": 1.0, "i3": 0.5}
    assert agg.repeats_per_item == {"i1": 2, "i2": 2, "i3": 1}
    assert agg.n_items == 3
    assert agg.failed_repeats == 3
    assert agg.items_short_of_k == ["i3"]
    assert agg.dropped_items and "i4" in agg.dropped_items

    assert agg.mean_within_item_sd == pytest.approx(math.sqrt(0.125) / 2)
    assert agg.mean_within_item_sd == pytest.approx(0.1767766952966369)
    assert agg.max_within_item_range == 0.5
    assert agg.full_agreement_rate == 0.5


def test_a_failed_judge_repeat_is_never_read_as_a_zero():
    """A judge outage is our failure, not the model's. Averaging in a 0 would
    charge the model under test for our infrastructure."""
    agg = S.aggregate_repeats({"i1": [1.0, None]}, expected_k=2)
    assert agg.item_means == {"i1": 1.0}
    assert agg.failed_repeats == 1


def test_repeat_aggregate_hands_item_means_to_the_bootstrap():
    agg = S.aggregate_repeats(
        {"i1": [1.0, 0.5], "i2": [1.0, 1.0], "i3": [0.0, 0.5], "i4": [None, None]},
        metric="judge_score",
        expected_k=2,
    )
    sample = agg.to_sample()
    assert sample.metric == "judge_score"
    assert sorted(zip(sample.item_ids, sample.values)) == [
        ("i1", 0.75),
        ("i2", 1.0),
        ("i3", 0.25),
    ]
    assert sample.excluded == {"error": 1}
    e = S.bootstrap_ci(sample)
    assert e.n == 3
    assert e.point == pytest.approx((0.75 + 1.0 + 0.25) / 3)


def test_repeat_noise_stays_visible_rather_than_being_averaged_away():
    """k=2 exists to expose judge instability; hiding it in a mean wastes the
    spend. A fully-disagreeing set must not look identical to a stable one."""
    stable = S.aggregate_repeats({"i1": [0.5, 0.5], "i2": [0.5, 0.5]}, expected_k=2)
    noisy = S.aggregate_repeats({"i1": [1.0, 0.0], "i2": [0.0, 1.0]}, expected_k=2)

    # Identical item means, so the score sample alone cannot tell them apart…
    assert stable.item_means == noisy.item_means == {"i1": 0.5, "i2": 0.5}
    assert stable.to_sample().values == noisy.to_sample().values

    # …and the repeat statistics are what make the instability visible.
    # sd of [1, 0] with ddof=1 is sqrt(0.5); of [0.5, 0.5] it is 0.
    assert stable.full_agreement_rate == 1.0
    assert noisy.full_agreement_rate == 0.0
    assert stable.mean_within_item_sd == 0.0
    assert noisy.mean_within_item_sd == pytest.approx(math.sqrt(0.5))
    assert stable.max_within_item_range == 0.0
    assert noisy.max_within_item_range == 1.0


def test_single_repeat_runs_report_no_repeat_statistics():
    agg = S.aggregate_repeats({"i1": [1.0], "i2": [0.0]}, expected_k=1)
    assert agg.mean_within_item_sd is None
    assert agg.full_agreement_rate is None
    assert agg.items_short_of_k == []
