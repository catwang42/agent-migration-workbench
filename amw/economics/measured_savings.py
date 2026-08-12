"""``cost_savings_pct`` computed from tokens that were actually recorded.

:mod:`amw.economics.cost_model` answers "what will this cost per year?", and it
is blocked until the customer confirms their call volumes — a run-rate built on
invented volumes is a fabricated result no matter how real the unit prices are.

This module answers the narrower question the gate actually asks. A *savings
percentage* is a ratio of two bills, and the volume term cancels out of a
ratio: if both arms serve the same traffic, the share saved depends only on the
token mix and the unit prices. So a per-subagent savings % is computable today,
from measured token counts, without the customer having stated a single call
volume — and it is a better number than the profile's would be, because the
tokens are counted rather than assumed.

What it is NOT
--------------
``config/gates.yaml`` registers the basis as *"customer volumes from the
profile, uncached list prices"*. This is a **different instrument**: measured
per-call tokens over this corpus, uncached list prices. Both are savings
percentages and both use list prices, but a reader must be able to tell them
apart, so :func:`savings_from_traces` returns the basis string it actually used
and the scorecard prints it beside the number. The profile-volume version stays
uncomputed rather than being quietly swapped for this one.

Two consequences of using this corpus's tokens follow the number everywhere:

* it is the saving **on this synthetic corpus's token mix**. A customer whose
  documents are twice as long shifts the input/output balance and moves it.
* it is **per subagent**. Rolling the three into one headline saving would need
  the relative call volumes, which is exactly the input that is missing.

Cached rates are deliberately not applied. The cache upside is reported
separately by :mod:`amw.economics.cache_breakeven`; folding a best-case cache
assumption into the gate number would inflate the one figure a customer
remembers.
"""

from __future__ import annotations

from typing import Mapping, Sequence

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from amw.config import AppConfig, PricingConfig
from amw.eval.stats import (
    BOOTSTRAP_RESAMPLES,
    DEFAULT_BOOTSTRAP_SEED,
    DEFAULT_CONFIDENCE,
    Estimate,
)
from amw.traces.schema import Trace

__all__ = [
    "MEASURED_TOKENS_BASIS",
    "PairedCost",
    "SubagentSavings",
    "call_cost_usd",
    "savings_from_traces",
]

#: Printed beside every number this module produces. See the module docstring:
#: the gate's registered basis is the profile-volume one, and these two must
#: never be mistaken for each other.
MEASURED_TOKENS_BASIS = (
    "measured per-call tokens over this corpus, uncached list prices from "
    "config/pricing.yaml — NOT the registered profile-volume basis, which "
    "stays uncomputed while customer volumes are unconfirmed"
)


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PairedCost(_Base):
    """One item, costed on both arms."""

    item_id: str
    baseline_usd: float
    candidate_usd: float
    baseline_input_tokens: int
    baseline_output_tokens: int
    candidate_input_tokens: int
    candidate_output_tokens: int


class SubagentSavings(_Base):
    """What one subagent's recorded tokens say about the cost gate."""

    subagent: str
    baseline_model: str
    candidate_model: str
    baseline_variant: str
    candidate_variant: str
    basis: str = MEASURED_TOKENS_BASIS
    paired: list[PairedCost] = Field(default_factory=list)
    #: Items dropped because one arm had no usable recorded call.
    dropped_unpaired: int = 0
    estimate: Estimate | None = None
    no_estimate_reason: str | None = None

    @property
    def baseline_total_usd(self) -> float:
        return sum(p.baseline_usd for p in self.paired)

    @property
    def candidate_total_usd(self) -> float:
        return sum(p.candidate_usd for p in self.paired)


def call_cost_usd(pricing: PricingConfig, model_key: str, trace: Trace) -> float:
    """USD for one recorded call at uncached list prices.

    ``pricing.rate`` raises on a ``VERIFY`` slot, so an unverified price cannot
    produce a cost here — it produces an exception (ground rule 3).

    ``usage.cached_tokens`` is reported by the providers but is *not* discounted
    here. Whether this corpus's prefix reuse would survive a real deployment is
    the question :mod:`amw.economics.cache_breakeven` exists to ask; assuming it
    would, inside the gate number, is how a savings % gets quietly inflated.
    """
    per_million = 1_000_000.0
    return (
        trace.usage.input_tokens / per_million * pricing.rate(model_key, "input")
        + trace.usage.output_tokens / per_million * pricing.rate(model_key, "output")
    )


def _bootstrap_savings(
    paired: Sequence[PairedCost],
    *,
    seed: int,
    resamples: int,
    confidence: float,
) -> Estimate:
    """Percentile bootstrap of ``(base - cand) / base``, resampling *items*.

    The statistic is the ratio of the two totals, not the mean of the per-item
    ratios. They are different numbers and only the first one is the bill: a
    mean of ratios gives a one-cent item the same weight as a one-dollar one,
    which is how a savings % ends up describing a workload nobody runs.
    """
    base = np.array([p.baseline_usd for p in paired], dtype=float)
    cand = np.array([p.candidate_usd for p in paired], dtype=float)
    rng = np.random.default_rng(seed)
    index = rng.integers(0, base.size, size=(resamples, base.size))
    base_sums = base[index].sum(axis=1)
    cand_sums = cand[index].sum(axis=1)
    # A resample whose baseline bill is exactly zero has no defined saving.
    # Dropping it silently would bias the interval, so it is kept out of the
    # percentile and counted — in practice a priced call is never free.
    usable = base_sums > 0
    dist = (base_sums[usable] - cand_sums[usable]) / base_sums[usable] * 100.0
    tail = (1.0 - confidence) / 2.0 * 100.0
    total_base, total_cand = float(base.sum()), float(cand.sum())
    return Estimate(
        metric="cost_savings_pct",
        point=(total_base - total_cand) / total_base * 100.0,
        lo=float(np.percentile(dist, tail)),
        hi=float(np.percentile(dist, 100.0 - tail)),
        n=len(paired),
        unit="percent",
        confidence=confidence,
        resamples=resamples,
        seed=seed,
        method="percentile_bootstrap",
        paired_n=len(paired),
    )


def savings_from_traces(
    cfg: AppConfig,
    *,
    subagent: str,
    baseline_variant: str,
    candidate_variant: str,
    baseline_model: str,
    candidate_model: str,
    baseline_traces: Mapping[str, Trace],
    candidate_traces: Mapping[str, Trace],
    seed: int = DEFAULT_BOOTSTRAP_SEED,
    resamples: int = BOOTSTRAP_RESAMPLES,
    confidence: float = DEFAULT_CONFIDENCE,
) -> SubagentSavings:
    """Pair two arms' recorded calls item by item and cost both sides.

    ``*_traces`` map ``item_id -> Trace``. Only items present on *both* arms
    with ``status == "ok"`` are costed: an errored call billed nothing like a
    successful one, and pricing it as if it had answered would credit a failure
    with a saving. The count of what that dropped travels on the result.
    """
    out = SubagentSavings(
        subagent=subagent,
        baseline_model=baseline_model,
        candidate_model=candidate_model,
        baseline_variant=baseline_variant,
        candidate_variant=candidate_variant,
    )
    for item_id in sorted(set(baseline_traces) & set(candidate_traces)):
        base, cand = baseline_traces[item_id], candidate_traces[item_id]
        if base.status != "ok" or cand.status != "ok":
            out.dropped_unpaired += 1
            continue
        out.paired.append(
            PairedCost(
                item_id=item_id,
                baseline_usd=call_cost_usd(cfg.pricing, baseline_model, base),
                candidate_usd=call_cost_usd(cfg.pricing, candidate_model, cand),
                baseline_input_tokens=base.usage.input_tokens,
                baseline_output_tokens=base.usage.output_tokens,
                candidate_input_tokens=cand.usage.input_tokens,
                candidate_output_tokens=cand.usage.output_tokens,
            )
        )
    out.dropped_unpaired += len(set(baseline_traces) ^ set(candidate_traces))

    if len(out.paired) < 2:
        out.no_estimate_reason = (
            f"{len(out.paired)} item(s) had a successful recorded call on both "
            f"arms; a paired bootstrap needs at least 2"
        )
        return out
    if out.baseline_total_usd <= 0:
        out.no_estimate_reason = (
            "the baseline arm's recorded tokens price to $0, so a savings "
            "percentage has no denominator"
        )
        return out
    out.estimate = _bootstrap_savings(
        out.paired, seed=seed, resamples=resamples, confidence=confidence
    )
    return out
