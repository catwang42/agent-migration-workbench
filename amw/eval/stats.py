"""Bootstrap statistics and the gate check — the load-bearing module.

The Migration Readiness Scorecard rests on exactly one sentence: *"quality
parity within measurement on this workload under pre-agreed gates."* That
sentence is only licensed if the gate was checked against an **interval**, not
against a point estimate, so this module is built so that the point-estimate
shortcut is not available:

* :func:`bootstrap_ci` and :func:`paired_bootstrap_delta` return an
  :class:`Estimate` carrying ``lo``/``hi`` alongside the point value.
* :func:`check_gate` takes an :class:`Estimate` — never a float — and compares
  the **CI lower bound** for a ``min`` gate and the **CI upper bound** for a
  ``max`` gate, exactly as ``config/gates.yaml`` documents.
* There is deliberately **no** ``check_mean_against_threshold`` helper. If you
  find yourself wanting one, the number you are about to publish is not a
  parity claim. That omission is the feature.

**Determinism.** A confidence interval that moves between runs is not
reportable — a customer who re-runs the notebook and sees a different lower
bound has been given a number, not a measurement. Every resampling entry point
takes an explicit ``seed`` and records it on the returned
:class:`Estimate`, so a report footer can print it and a reader can reproduce
the interval exactly.

**Pairing.** Deltas use a paired bootstrap. The same dataset items are scored
under both arms, so the item is the unit of resampling: draw item indices once
and read *both* arms at those indices. Resampling the two score vectors
independently throws away the pairing and inflates the interval, which would
make a real regression look like noise.

**Percentile method**, 10,000 resamples by default (``BOOTSTRAP_RESAMPLES``).
No BCa: the extra machinery is not worth the reviewability, and a percentile
interval is something a customer's own data scientist can re-derive in ten
lines.
"""

from __future__ import annotations

import math
from typing import Any, Literal, Mapping, Sequence

import numpy as np
from pydantic import BaseModel, ConfigDict, model_validator

from amw.config import ConfigError, Gate, GatesConfig
from amw.eval.metrics import MetricSample

__all__ = [
    "BOOTSTRAP_RESAMPLES",
    "DEFAULT_BOOTSTRAP_SEED",
    "DEFAULT_CONFIDENCE",
    "MIN_OBSERVATIONS",
    "InsufficientDataError",
    "Estimate",
    "PairedSample",
    "RepeatAggregate",
    "GateCheck",
    "mean",
    "bootstrap_ci",
    "pair_samples",
    "paired_bootstrap_delta",
    "aggregate_repeats",
    "check_gate",
    "check_gates",
    "missing_gates",
    "resolve_bound",
]

#: master_plan §5.4 / the T08 card. Not a tunable: changing it changes every
#: published interval, so it is a module constant a reader can find.
BOOTSTRAP_RESAMPLES = 10_000

#: Default resampling seed. Always recorded on the Estimate; always overridable
#: per call. A default that is *stated* beats a default that is implicit.
DEFAULT_BOOTSTRAP_SEED = 20260812

DEFAULT_CONFIDENCE = 0.95

#: A bootstrap over one observation resamples the same number 10,000 times and
#: reports a zero-width interval — a fake precision. Refuse instead.
MIN_OBSERVATIONS = 2

Unit = Literal["fraction", "percent", "percentage_points", "ms", "usd", "count"]

#: Unit implied by a gate's name suffix in ``config/gates.yaml``. This encodes a
#: naming convention that already exists in that file; it is not a threshold.
#: Its whole job is to catch the one catastrophic mix-up — handing a fraction
#: (0.98) to a gate written in percentage points (-2.0) and passing it.
_UNIT_BY_GATE_SUFFIX: dict[str, Unit] = {
    "_pp": "percentage_points",
    "_pct": "percent",
}


class InsufficientDataError(ValueError):
    """Too few observations to produce an interval anyone should publish."""


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# the estimate type
# --------------------------------------------------------------------------


class Estimate(_Base):
    """A point estimate with the interval that makes it reportable.

    Everything needed to reproduce the interval travels with it: ``n``,
    ``resamples``, ``seed``, ``confidence`` and ``method``. Report footers print
    those; without them "95% CI [-1.2, 0.4]" is an assertion, not evidence.
    """

    metric: str
    point: float
    lo: float
    hi: float
    n: int
    unit: Unit = "fraction"
    confidence: float = DEFAULT_CONFIDENCE
    resamples: int = BOOTSTRAP_RESAMPLES
    seed: int = DEFAULT_BOOTSTRAP_SEED
    method: Literal["percentile_bootstrap", "paired_percentile_bootstrap"] = (
        "percentile_bootstrap"
    )
    #: Set on a paired delta: how many items were common to both arms and how
    #: many were dropped for having no counterpart.
    paired_n: int | None = None
    dropped_unpaired: int = 0

    @model_validator(mode="after")
    def _ordered(self) -> "Estimate":
        if self.lo > self.hi:
            raise ValueError(f"{self.metric}: CI lower bound {self.lo} exceeds {self.hi}")
        return self

    @property
    def width(self) -> float:
        return self.hi - self.lo

    def rescaled(self, factor: float, unit: Unit) -> "Estimate":
        """Same interval in different units (e.g. fraction -> percentage points)."""
        return self.model_copy(
            update={
                "point": self.point * factor,
                "lo": self.lo * factor,
                "hi": self.hi * factor,
                "unit": unit,
            }
        )

    def to_percentage_points(self) -> "Estimate":
        """A delta of proportions, expressed in percentage points.

        The ``*_delta_pp`` gates are written in percentage points, so a delta
        computed over 0..1 scores has to be rescaled *before* it meets a gate.
        :func:`check_gate` enforces that; this is how you satisfy it.
        """
        if self.unit == "percentage_points":
            return self
        if self.unit != "fraction":
            raise ValueError(
                f"cannot convert {self.unit!r} to percentage points; only a "
                "fraction-scaled estimate has that meaning"
            )
        return self.rescaled(100.0, "percentage_points")

    def to_percent(self) -> "Estimate":
        if self.unit == "percent":
            return self
        if self.unit != "fraction":
            raise ValueError(f"cannot convert {self.unit!r} to percent")
        return self.rescaled(100.0, "percent")

    def describe(self) -> str:
        """One line for a table cell or a log."""
        return (
            f"{self.point:.4g} [{self.lo:.4g}, {self.hi:.4g}] "
            f"({self.confidence:.0%} CI, n={self.n}, "
            f"{self.resamples} resamples, seed={self.seed})"
        )


# --------------------------------------------------------------------------
# point estimates
# --------------------------------------------------------------------------


def mean(values: Sequence[float]) -> float:
    """Arithmetic mean. Raises on an empty sample rather than returning 0.0.

    An empty arm is not an arm that scored zero — it is an arm that was not
    measured, and the two must never render as the same number (ground rule 1).
    """
    values = list(values)
    if not values:
        raise InsufficientDataError(
            "mean() of an empty sample: there is no measurement here, and 0.0 "
            "would be a fabricated one"
        )
    return float(np.mean(np.asarray(values, dtype=float)))


def _as_array(values: Sequence[float] | MetricSample, what: str) -> tuple[np.ndarray, str]:
    if isinstance(values, MetricSample):
        return np.asarray(values.values, dtype=float), values.metric
    return np.asarray(list(values), dtype=float), what


def _percentile_bounds(confidence: float) -> tuple[float, float]:
    if not 0 < confidence < 1:
        raise ValueError(f"confidence must be in (0, 1), got {confidence}")
    tail = (1.0 - confidence) / 2.0
    return 100.0 * tail, 100.0 * (1.0 - tail)


def _resample_means(
    data: np.ndarray, *, resamples: int, seed: int, second: np.ndarray | None = None
) -> np.ndarray:
    """Bootstrap distribution of the mean (or of a paired difference of means).

    One ``rng`` seeded exactly once, one index draw per resample. When
    ``second`` is given, both arrays are read at the *same* indices — that is
    the pairing.
    """
    n = data.shape[0]
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(resamples, n))
    if second is None:
        return data[idx].mean(axis=1)
    return second[idx].mean(axis=1) - data[idx].mean(axis=1)


def bootstrap_ci(
    values: Sequence[float] | MetricSample,
    *,
    metric: str = "metric",
    unit: Unit = "fraction",
    resamples: int = BOOTSTRAP_RESAMPLES,
    confidence: float = DEFAULT_CONFIDENCE,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
) -> Estimate:
    """Percentile bootstrap CI for the mean of one arm.

    :param values: a plain sequence, or a :class:`~amw.eval.metrics.MetricSample`
        (whose ``metric`` name is then used automatically).
    :param seed: recorded on the result. Two calls with the same seed and the
        same data give byte-identical bounds, on any machine.
    :raises InsufficientDataError: fewer than :data:`MIN_OBSERVATIONS` values.
    """
    data, name = _as_array(values, metric)
    if data.size < MIN_OBSERVATIONS:
        raise InsufficientDataError(
            f"{name}: {data.size} observation(s); a bootstrap CI needs at least "
            f"{MIN_OBSERVATIONS}. Report the raw values, not an interval."
        )
    if resamples < 1:
        raise ValueError(f"resamples must be >= 1, got {resamples}")

    lo_pct, hi_pct = _percentile_bounds(confidence)
    dist = _resample_means(data, resamples=resamples, seed=seed)
    return Estimate(
        metric=name,
        point=float(data.mean()),
        lo=float(np.percentile(dist, lo_pct)),
        hi=float(np.percentile(dist, hi_pct)),
        n=int(data.size),
        unit=unit,
        confidence=confidence,
        resamples=resamples,
        seed=seed,
        method="percentile_bootstrap",
    )


# --------------------------------------------------------------------------
# paired deltas
# --------------------------------------------------------------------------


class PairedSample(_Base):
    """Two arms aligned item by item, plus what could not be aligned.

    ``dropped_baseline_only`` / ``dropped_candidate_only`` are not bookkeeping
    trivia: an item that only one arm could score is usually an item the other
    arm failed on, and dropping it silently is how a delta gets flattered.
    """

    metric: str
    item_ids: list[str]
    baseline: list[float]
    candidate: list[float]
    dropped_baseline_only: list[str] = []
    dropped_candidate_only: list[str] = []

    @model_validator(mode="after")
    def _aligned(self) -> "PairedSample":
        if not (len(self.item_ids) == len(self.baseline) == len(self.candidate)):
            raise ValueError(
                f"{self.metric}: paired arrays are ragged "
                f"({len(self.item_ids)}/{len(self.baseline)}/{len(self.candidate)})"
            )
        return self

    @property
    def n(self) -> int:
        return len(self.item_ids)

    @property
    def n_dropped(self) -> int:
        return len(self.dropped_baseline_only) + len(self.dropped_candidate_only)


def pair_samples(
    baseline: MetricSample, candidate: MetricSample, *, metric: str | None = None
) -> PairedSample:
    """Align two arms on ``item_id``, in the baseline's order.

    Items measured in only one arm are excluded from the delta — a difference
    needs both halves — but they are *named* in the result so the caller can
    report them. They are typically the interesting items.
    """
    name = metric or baseline.metric
    if baseline.metric != candidate.metric and metric is None:
        raise ValueError(
            f"pairing different metrics: {baseline.metric!r} vs "
            f"{candidate.metric!r}; pass metric= if that is intended"
        )
    base_map = dict(zip(baseline.item_ids, baseline.values))
    cand_map = dict(zip(candidate.item_ids, candidate.values))
    if len(base_map) != len(baseline.item_ids) or len(cand_map) != len(
        candidate.item_ids
    ):
        raise ValueError(
            f"{name}: duplicate item_ids in a sample; pairing would be ambiguous"
        )

    shared = [i for i in baseline.item_ids if i in cand_map]
    return PairedSample(
        metric=name,
        item_ids=shared,
        baseline=[base_map[i] for i in shared],
        candidate=[cand_map[i] for i in shared],
        dropped_baseline_only=[i for i in baseline.item_ids if i not in cand_map],
        dropped_candidate_only=[i for i in candidate.item_ids if i not in base_map],
    )


def paired_bootstrap_delta(
    baseline: Sequence[float] | MetricSample | PairedSample,
    candidate: Sequence[float] | MetricSample | None = None,
    *,
    metric: str = "delta",
    unit: Unit = "fraction",
    resamples: int = BOOTSTRAP_RESAMPLES,
    confidence: float = DEFAULT_CONFIDENCE,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
) -> Estimate:
    """Paired percentile bootstrap of ``mean(candidate) - mean(baseline)``.

    Sign convention: **candidate minus baseline**, i.e. Gemini minus Claude.
    A negative lower bound that clears ``quality_delta_pp: min -2.0`` is the
    parity claim; a positive point estimate with a lower bound below the gate
    is *not*, which is precisely why the gate reads the bound.

    Call it either with two aligned :class:`~amw.eval.metrics.MetricSample`
    objects (they get paired on ``item_id`` first) or with a
    :class:`PairedSample` you built yourself. Two bare sequences are accepted
    only if they are already aligned index by index.

    :raises InsufficientDataError: fewer than :data:`MIN_OBSERVATIONS` pairs.
    """
    dropped = 0
    if isinstance(baseline, PairedSample):
        if candidate is not None:
            raise TypeError(
                "pass either a PairedSample or two samples, not both"
            )
        pair = baseline
        base_arr = np.asarray(pair.baseline, dtype=float)
        cand_arr = np.asarray(pair.candidate, dtype=float)
        name = pair.metric if metric == "delta" else metric
        dropped = pair.n_dropped
    elif isinstance(baseline, MetricSample) and isinstance(candidate, MetricSample):
        pair = pair_samples(baseline, candidate, metric=None if metric == "delta" else metric)
        base_arr = np.asarray(pair.baseline, dtype=float)
        cand_arr = np.asarray(pair.candidate, dtype=float)
        name = pair.metric
        dropped = pair.n_dropped
    else:
        if candidate is None:
            raise TypeError("paired_bootstrap_delta needs both arms")
        base_arr = np.asarray(list(baseline), dtype=float)  # type: ignore[arg-type]
        cand_arr = np.asarray(list(candidate), dtype=float)  # type: ignore[arg-type]
        name = metric
        if base_arr.shape != cand_arr.shape:
            raise ValueError(
                f"{name}: unpaired arms ({base_arr.size} vs {cand_arr.size}); "
                "pass MetricSamples so they can be aligned on item_id"
            )

    if base_arr.size < MIN_OBSERVATIONS:
        raise InsufficientDataError(
            f"{name}: {base_arr.size} paired observation(s); a paired bootstrap "
            f"needs at least {MIN_OBSERVATIONS}"
        )

    lo_pct, hi_pct = _percentile_bounds(confidence)
    dist = _resample_means(base_arr, resamples=resamples, seed=seed, second=cand_arr)
    return Estimate(
        metric=name,
        point=float(cand_arr.mean() - base_arr.mean()),
        lo=float(np.percentile(dist, lo_pct)),
        hi=float(np.percentile(dist, hi_pct)),
        n=int(base_arr.size),
        unit=unit,
        confidence=confidence,
        resamples=resamples,
        seed=seed,
        method="paired_percentile_bootstrap",
        paired_n=int(base_arr.size),
        dropped_unpaired=dropped,
    )


# --------------------------------------------------------------------------
# judge repeats
# --------------------------------------------------------------------------


class RepeatAggregate(_Base):
    """k repeats per item, folded to one score per item — with the noise shown.

    The customer profile asks for ``judge_repeats: 2``. Repeats exist to expose
    judge instability, so hiding it inside a mean would waste the spend: the
    item mean is what the bootstrap consumes, and ``mean_within_item_sd`` /
    ``full_agreement_rate`` are reported next to it so a reader can see how much
    of the score is the judge disagreeing with itself.

    Note what the CI from :meth:`to_sample` does and does not cover: it
    resamples **items**, so it carries item-level sampling uncertainty. Judge
    noise enters only through its effect on each item's mean. That is the
    honest, conventional treatment for k=2, and the residual is exactly what
    ``mean_within_item_sd`` makes visible.
    """

    metric: str
    #: item_id -> mean over that item's successful repeats
    item_means: dict[str, float] = {}
    #: item_id -> number of successful repeats
    repeats_per_item: dict[str, int] = {}
    expected_k: int | None = None
    #: item_id -> reason, for items with no successful repeat at all
    dropped_items: dict[str, str] = {}
    #: Repeats that returned no score (judge error). Counted, never scored 0.
    failed_repeats: int = 0
    #: Items whose successful-repeat count is below ``expected_k``.
    items_short_of_k: list[str] = []
    #: Mean of the per-item sample standard deviations (items with k>=2 only).
    mean_within_item_sd: float | None = None
    #: Largest spread seen on any single item.
    max_within_item_range: float | None = None
    #: Fraction of multi-repeat items where every repeat gave the same score.
    full_agreement_rate: float | None = None

    @property
    def n_items(self) -> int:
        return len(self.item_means)

    def to_sample(self) -> MetricSample:
        """Item means as a :class:`~amw.eval.metrics.MetricSample` for the bootstrap."""
        return MetricSample(
            metric=self.metric,
            values=[self.item_means[i] for i in self.item_means],
            item_ids=list(self.item_means),
            excluded={"error": len(self.dropped_items)} if self.dropped_items else {},
            excluded_items=dict(self.dropped_items),
        )


def aggregate_repeats(
    scores: Mapping[str, Sequence[float | None]],
    *,
    metric: str = "judge_score",
    expected_k: int | None = None,
) -> RepeatAggregate:
    """Fold k repeats per item into one score per item, keeping the noise visible.

    :param scores: ``item_id -> [score_repeat_1, score_repeat_2, ...]``. A
        ``None`` entry is a repeat that produced no score — a judge call that
        errored. It is counted in ``failed_repeats`` and **excluded**; it is
        never read as 0, because a judge failure is our infrastructure failing,
        not the model under test scoring badly.
    :param expected_k: the configured repeat count
        (``config/customers/*.yaml: dataset.judge_repeats``). Items that came
        back with fewer successes are listed in ``items_short_of_k``.
    """
    agg = RepeatAggregate(metric=metric, expected_k=expected_k)
    sds: list[float] = []
    ranges: list[float] = []
    agreements: list[bool] = []

    for item_id, raw in scores.items():
        good = [float(s) for s in raw if s is not None]
        agg.failed_repeats += sum(1 for s in raw if s is None)
        if not good:
            agg.dropped_items[item_id] = (
                f"all {len(raw)} judge repeat(s) failed; no score exists for "
                "this item"
            )
            continue
        agg.item_means[item_id] = float(np.mean(good))
        agg.repeats_per_item[item_id] = len(good)
        if expected_k is not None and len(good) < expected_k:
            agg.items_short_of_k.append(item_id)
        if len(good) >= 2:
            sds.append(float(np.std(good, ddof=1)))
            ranges.append(max(good) - min(good))
            agreements.append(max(good) == min(good))

    if sds:
        agg.mean_within_item_sd = float(np.mean(sds))
        agg.max_within_item_range = float(max(ranges))
        agg.full_agreement_rate = sum(agreements) / len(agreements)
    return agg


# --------------------------------------------------------------------------
# the gate check
# --------------------------------------------------------------------------


class GateCheck(_Base):
    """One gate, evaluated against a CI bound. The scorecard's atom.

    ``compared_value`` is the bound that was actually tested (``lo`` for a
    ``min`` gate, ``hi`` for a ``max`` gate) and ``bound_source`` says whether
    the threshold came straight from ``gates.yaml`` or from resolving a
    sentinel. Both are printed, so "this gate passed" is always accompanied by
    "…on this number, against this threshold, from this file".
    """

    gate: str
    direction: Literal["min", "max"]
    bound: float
    bound_source: str
    basis: str
    estimate: Estimate
    compared_value: float
    compared_bound: Literal["ci_lower", "ci_upper"]
    passed: bool
    gates_version: int
    gates_version_hash: str = ""
    #: The gate's `alt` clause from gates.yaml, when it has one. Not evaluated
    #: here — it needs triage data — but carried so the scorecard cannot forget
    #: that a failed gate had a documented alternative route.
    alt: str | None = None
    #: Whether that alt clause was met. ``None`` means it was never evaluated:
    #: the gate has no clause, or nothing supplied the evidence one needs.
    #: ``None`` is emphatically not ``False`` — an unevaluated route is not a
    #: rejected one, and a report that conflates them is claiming a finding it
    #: does not have. Set by the scorecard (:func:`apply_alt_clause`), which is
    #: the layer that has the triage data.
    alt_passed: bool | None = None
    #: The measurement the clause was decided on, in full, quoted into the
    #: report's footnote. Never a bare verdict — the number has to travel with
    #: it or "passed by the alt clause" is unauditable.
    alt_evidence: str = ""
    #: The same measurement compressed to a table cell (e.g. ``15W/3L``).
    alt_summary: str = ""

    @property
    def by_alt(self) -> bool:
        """The gate missed its CI bound and its alternative route carried it."""
        return not self.passed and self.alt_passed is True

    @property
    def effective_passed(self) -> bool:
        """Did this gate clear, by *either* pre-registered route?

        This — not :attr:`passed` — is what a verdict is computed from. They
        differ only where gates.yaml wrote an ``alt`` clause in advance, which
        is the only circumstance under which a missed CI bound is allowed to
        become a pass.
        """
        return self.passed or self.alt_passed is True

    def result_text(self) -> str:
        """The result cell: PASS, FAIL, or PASS with the route named.

        A gate carried by its alt clause never renders as a bare ``PASS``. The
        CI bound was missed; a reader who cannot see that from the cell is
        being told something untrue by omission.
        """
        if self.passed:
            return "PASS"
        if self.alt_passed is True:
            detail = f": adjudication {self.alt_summary}" if self.alt_summary else ""
            return f"PASS (by pre-registered alt clause{detail})"
        return "**FAIL**"

    def describe(self) -> str:
        verb = ">=" if self.direction == "min" else "<="
        status = "PASS" if self.effective_passed else "FAIL"
        if self.by_alt:
            status = "PASS(alt)"
        return (
            f"{status} {self.gate}: {self.compared_bound}={self.compared_value:.4g} "
            f"{verb} {self.bound:g} ({self.estimate.unit})"
        )


def resolve_bound(
    gate: Gate, gate_name: str, sentinel_values: Mapping[str, float] | None
) -> tuple[float, str]:
    """``(numeric bound, human-readable source)``.

    A sentinel (``claude_baseline_p95``) must be supplied by the caller from a
    measured baseline statistic. An unresolved sentinel raises: gates.yaml is
    explicit that this is "a hard error, never a skipped gate", because a gate
    that quietly disappears is indistinguishable from a gate that passed.
    """
    bound = gate.bound
    if not gate.is_sentinel:
        return float(bound), f"gates.yaml:{gate_name}.{gate.direction}"
    sentinel = str(bound)
    values = sentinel_values or {}
    if sentinel not in values:
        raise ConfigError(
            f"gate {gate_name!r} is bounded by the sentinel {sentinel!r}, which "
            f"was not supplied. Pass sentinel_values={{{sentinel!r}: <measured "
            "value>}}. An unresolved sentinel is a hard error, never a skipped "
            "gate (config/gates.yaml)."
        )
    return float(values[sentinel]), f"sentinel:{sentinel}"


def _expected_unit(gate_name: str) -> Unit | None:
    for suffix, unit in _UNIT_BY_GATE_SUFFIX.items():
        if gate_name.endswith(suffix):
            return unit
    return None


def check_gate(
    gate_name: str,
    estimate: Estimate,
    gates: GatesConfig,
    *,
    sentinel_values: Mapping[str, float] | None = None,
    expect_unit: Unit | None = None,
) -> GateCheck:
    """Evaluate one gate from ``gates.yaml`` against a CI bound.

    ``min`` gates are tested on ``estimate.lo``; ``max`` gates on
    ``estimate.hi``. Never on ``estimate.point`` — that is the whole contract of
    this function and of ground rule 7. A gate that passes here has passed
    *within measurement*, which is the only claim the scorecard is allowed to
    make.

    The unit check is a guard rail, not a threshold: gates named ``*_pp`` and
    ``*_pct`` in ``gates.yaml`` demand estimates in those units, so a 0.98
    fraction can never be silently compared against a ``-2.0`` percentage-point
    bound. Use :meth:`Estimate.to_percentage_points` first.

    :raises ~amw.config.ConfigError: unknown gate, or an unresolved sentinel.
    :raises ValueError: the estimate is in the wrong unit for this gate.
    """
    gate = gates.gate(gate_name)  # ConfigError on an unknown name
    want_unit = expect_unit or _expected_unit(gate_name)
    if want_unit is not None and estimate.unit != want_unit:
        raise ValueError(
            f"gate {gate_name!r} is expressed in {want_unit}, but the estimate "
            f"is in {estimate.unit}. Rescale it explicitly (see "
            "Estimate.to_percentage_points) — comparing mismatched units is how "
            "a failing gate reads as a pass."
        )

    bound, source = resolve_bound(gate, gate_name, sentinel_values)
    if gate.direction == "min":
        compared, which = estimate.lo, "ci_lower"
        passed = compared >= bound
    else:
        compared, which = estimate.hi, "ci_upper"
        passed = compared <= bound

    if math.isnan(compared):  # pragma: no cover - defensive
        raise ValueError(f"gate {gate_name!r}: CI bound is NaN; refusing to judge it")

    return GateCheck(
        gate=gate_name,
        direction=gate.direction,
        bound=bound,
        bound_source=source,
        basis=gate.basis,
        estimate=estimate,
        compared_value=float(compared),
        compared_bound=which,  # type: ignore[arg-type]
        passed=bool(passed),
        gates_version=gates.version,
        gates_version_hash=gates.version_hash,
        alt=gate.alt,
    )


def check_gates(
    estimates: Mapping[str, Estimate],
    gates: GatesConfig,
    *,
    sentinel_values: Mapping[str, float] | None = None,
) -> dict[str, GateCheck]:
    """Evaluate every gate for which an estimate was supplied.

    Gates with no estimate are **not** returned, and are not "passed" — the
    caller must notice the gap. :func:`missing_gates` names them.
    """
    return {
        name: check_gate(
            name, estimate, gates, sentinel_values=sentinel_values
        )
        for name, estimate in estimates.items()
    }


def missing_gates(
    checks: Mapping[str, Any], gates: GatesConfig
) -> list[str]:
    """Gates declared in ``gates.yaml`` that nothing measured.

    A verdict computed over a subset of the gates is not the verdict the
    customer pre-agreed to, so the scorecard has to be able to see the gap.
    """
    return sorted(set(gates.subagent_gates) - set(checks))
