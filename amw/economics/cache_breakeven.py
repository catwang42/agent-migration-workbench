"""When does context caching start paying for itself?

Context caching is the cheapest-looking win in a RAG migration and the easiest
one to get backwards. A cache is not free storage: creating it bills the
preamble at the *full* input rate, keeping it bills rent per token-hour for as
long as the TTL runs, and only reads within that TTL earn the discount. Below
some call rate the rent exceeds the discount and caching costs money.

This module answers the one question worth putting on a slide — **how many
calls per day does this subagent need before caching is worth turning on** —
and it answers it from ``config/pricing.yaml`` alone. Every rate is fetched
through :meth:`amw.config.PricingConfig.rate` and
:meth:`~amw.config.PricingConfig.cache_storage_rate`, both of which raise on a
``VERIFY`` placeholder, so with today's unverified table the function returns
an explicit not-computable state rather than a plausible number.

The model
---------

For one TTL window of ``ttl_hours`` over a shared preamble of ``cached_tokens``:

===============  ==============================================================
write            ``cached_tokens/1e6 x input_rate`` — the creating call pays
                 full price for the preamble, once per window
rent             ``cached_tokens/1e6 x ttl_hours x storage_rate``
saving per read  ``cached_tokens/1e6 x (input_rate - cached_input_rate)``
===============  ==============================================================

Breakeven reads in one window is ``(write + rent) / saving_per_read``; the
daily figure scales that by the ``24 / ttl_hours`` windows a day contains,
i.e. it assumes the cache is refreshed each time it expires and reads arrive
evenly. Both assumptions are stated on the result
(:attr:`CacheBreakeven.assumptions`) because they are what a customer's
architect will want to argue with, and they should be arguing with the
assumption rather than with an unexplained number.

Only the preamble is modelled. Per-call suffix tokens and output tokens are
billed identically with and without a cache, so they cancel out of the
comparison and adding them would only make the breakeven look better than it
is.
"""

from __future__ import annotations

from typing import Iterable

from pydantic import BaseModel, ConfigDict, Field

from amw.config import AppConfig, PricingConfig
from amw.economics.cost_model import Blocker, CANDIDATE_ROLE, NOT_COMPUTABLE

__all__ = [
    "HOURS_PER_DAY",
    "DEFAULT_TTL_HOURS",
    "CacheBreakeven",
    "cache_breakeven",
    "breakeven_curve",
]

HOURS_PER_DAY = 24.0

#: TTL ladder for the workshop's sensitivity table. Hours, not a price.
DEFAULT_TTL_HOURS: tuple[float, ...] = (1.0, 6.0, 24.0)

_PER_MILLION = 1_000_000.0


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CacheBreakeven(_Base):
    """Breakeven for one (model, preamble size, TTL), or a refusal."""

    computable: bool
    blockers: list[Blocker] = Field(default_factory=list)
    model: str
    cached_tokens: int
    ttl_hours: float
    #: All USD, for one TTL window. None whenever ``computable`` is False —
    #: never 0.0, which would read as "caching is free".
    write_usd: float | None = None
    storage_usd: float | None = None
    saving_per_read_usd: float | None = None
    breakeven_reads_per_window: float | None = None
    breakeven_calls_per_day: float | None = None
    #: Set when the cached rate is not below the fresh rate: there is no
    #: discount to earn, so no call rate makes caching pay.
    never_breaks_even: bool = False
    assumptions: list[str] = Field(default_factory=list)

    @property
    def state(self) -> str:
        if not self.computable:
            return f"{NOT_COMPUTABLE} — {' / '.join(b.label for b in self.blockers)}"
        if self.never_breaks_even:
            return "never breaks even at this price pair"
        return "computed"

    def net_usd_per_day(self, calls_per_day: float) -> float | None:
        """Saving (positive) or extra cost (negative) at a given call rate.

        ``None`` while the prices are unverified — the sign of this number is
        the recommendation, and guessing it is worse than withholding it.
        """
        if not self.computable or self.saving_per_read_usd is None:
            return None
        windows = HOURS_PER_DAY / self.ttl_hours
        overhead = (self.write_usd or 0.0) + (self.storage_usd or 0.0)
        return calls_per_day * self.saving_per_read_usd - windows * overhead


def _pricing_blockers(pricing: PricingConfig) -> list[Blocker]:
    if pricing.is_verified:
        return []
    return [
        Blocker(
            gate="pricing",
            label="pricing unverified",
            reason=(
                f"config/pricing.yaml has {len(pricing.unverified_keys())} rate(s) "
                f"still reading VERIFY and verified_on is {pricing.verified_on or 'null'}"
            ),
            clears_when="a human runs scripts/refresh_pricing.py",
        )
    ]


def cache_breakeven(
    cfg: AppConfig,
    *,
    cached_tokens: int,
    ttl_hours: float = 1.0,
    model_role: str = CANDIDATE_ROLE,
    model_key: str | None = None,
) -> CacheBreakeven:
    """Breakeven calls/day for caching a ``cached_tokens``-token preamble.

    :param model_key: overrides ``model_role``; both resolve through
        ``config/models.yaml``, so no model ID appears in this module.
    """
    if cached_tokens <= 0:
        raise ValueError("cached_tokens must be positive; there is nothing to cache")
    if ttl_hours <= 0:
        raise ValueError("ttl_hours must be positive")

    key = model_key or cfg.models.for_role(model_role)[0]
    pricing = cfg.pricing
    blockers = _pricing_blockers(pricing)
    assumptions = [
        "the cache is recreated every time the TTL expires",
        "reads are spread evenly across the day",
        "only the shared preamble is cached; suffix and output tokens are "
        "billed the same either way and cancel out",
    ]
    if blockers:
        return CacheBreakeven(
            computable=False,
            blockers=blockers,
            model=key,
            cached_tokens=cached_tokens,
            ttl_hours=ttl_hours,
            assumptions=assumptions,
        )

    millions = cached_tokens / _PER_MILLION
    fresh_rate = pricing.rate(key, "input")
    cached_rate = pricing.rate(key, "cached_input")
    write_usd = millions * fresh_rate
    storage_usd = millions * ttl_hours * pricing.cache_storage_rate()
    saving = millions * (fresh_rate - cached_rate)

    if saving <= 0:
        return CacheBreakeven(
            computable=True,
            model=key,
            cached_tokens=cached_tokens,
            ttl_hours=ttl_hours,
            write_usd=write_usd,
            storage_usd=storage_usd,
            saving_per_read_usd=saving,
            never_breaks_even=True,
            assumptions=assumptions,
        )

    reads = (write_usd + storage_usd) / saving
    return CacheBreakeven(
        computable=True,
        model=key,
        cached_tokens=cached_tokens,
        ttl_hours=ttl_hours,
        write_usd=write_usd,
        storage_usd=storage_usd,
        saving_per_read_usd=saving,
        breakeven_reads_per_window=reads,
        breakeven_calls_per_day=reads * (HOURS_PER_DAY / ttl_hours),
        assumptions=assumptions,
    )


def breakeven_curve(
    cfg: AppConfig,
    *,
    cached_tokens: int,
    ttl_hours: Iterable[float] = DEFAULT_TTL_HOURS,
    model_role: str = CANDIDATE_ROLE,
    model_key: str | None = None,
) -> list[CacheBreakeven]:
    """The same calculation across a TTL ladder — the sensitivity table."""
    return [
        cache_breakeven(
            cfg,
            cached_tokens=cached_tokens,
            ttl_hours=ttl,
            model_role=model_role,
            model_key=model_key,
        )
        for ttl in ttl_hours
    ]
