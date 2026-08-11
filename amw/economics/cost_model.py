"""Per-subagent run-rate cost, behind two gates that are both closed today.

The arithmetic here is trivial — tokens x rate x calls. Everything difficult
about this module is about *refusing to do it*.

Two independent things have to be true before a dollar figure may exist:

1. **Prices are verified.** ``config/pricing.yaml`` ships every rate as the
   literal ``VERIFY`` and :meth:`amw.config.PricingConfig.rate` raises rather
   than defaulting. Ground rule 3.
2. **Volumes are the customer's.** ``config/customers/demo_patents.yaml``
   carries an *illustrative* volume profile and says so
   (``volumes_confirmed: false``). A run-rate computed from invented call
   volumes is a fabricated result even when the unit prices are real, because
   the number a customer remembers is the annual total.

Today both are closed, so :func:`cost_model` returns
``not computable — pricing unverified / volumes unconfirmed`` and **no rows**.
Not zeros: a zero is a number, and a number in a cost column is what somebody
screenshots.

Clearing gate 2 in the room
---------------------------

Volumes are the half a customer can supply in the workshop, so
:func:`confirm_volumes` exists: a notebook cell or ``cli.py scorecard
--volume ... --volumes-confirmed-by ...`` records who gave the numbers and
when, and the footer flips from ``volumes: illustrative`` to
``volumes: customer-provided (name, date)``. It does **not** touch gate 1 —
unverified prices still block every figure, and a customer's real volumes
multiplied by a placeholder price is the most convincing wrong number this
repo could produce.

Rehearsals keep the illustrative profile, labelled as such.
"""

from __future__ import annotations

from datetime import date
from typing import Iterable, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from amw.config import AppConfig, ConfigError, PricingConfig, UnverifiedPriceError

__all__ = [
    "DAYS_PER_MONTH",
    "DAYS_PER_YEAR",
    "DEFAULT_MULTIPLIERS",
    "NOT_COMPUTABLE",
    "Blocker",
    "VolumeSource",
    "SubagentVolume",
    "VolumeSet",
    "CostRow",
    "CostModelResult",
    "confirm_volumes",
    "cost_model",
]

#: Calendar constants, not prices: 365-day year, twelve equal months. Stated
#: here rather than inlined so a reader can see that "monthly" is 30.4 days and
#: not 30, which moves an annual figure by more than a percent.
DAYS_PER_YEAR = 365.0
DAYS_PER_MONTH = DAYS_PER_YEAR / 12.0

#: The sensitivity band the T12 card asks for. A single point estimate of a
#: customer's future volume is the least believable number on the page.
DEFAULT_MULTIPLIERS: tuple[float, ...] = (0.5, 1.0, 2.0)

NOT_COMPUTABLE = "not computable"

#: Model roles from config/models.yaml. No model key appears literally here.
BASELINE_ROLE = "claude_baseline"
CANDIDATE_ROLE = "gemini_candidate"


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Blocker(_Base):
    """One reason no dollar figure exists, and what would clear it."""

    gate: Literal["pricing", "volumes"]
    label: str
    reason: str
    clears_when: str


class VolumeSource(_Base):
    """Where the call volumes came from — printed on every report footer."""

    kind: Literal["illustrative", "customer-provided"]
    provided_by: str | None = None
    provided_on: date | None = None

    @model_validator(mode="after")
    def _attribution_required(self) -> "VolumeSource":
        if self.kind == "customer-provided" and not self.provided_by:
            raise ValueError(
                "customer-provided volumes need provided_by: an unattributed "
                "'the customer told us' is not a source a footer can print"
            )
        return self

    def footer_label(self) -> str:
        if self.kind == "illustrative":
            return "volumes: illustrative"
        when = self.provided_on.isoformat() if self.provided_on else "date not recorded"
        return f"volumes: customer-provided ({self.provided_by}, {when})"

    @property
    def confirmed(self) -> bool:
        return self.kind == "customer-provided"


class SubagentVolume(_Base):
    subagent: str
    calls_per_day: int
    avg_input_tokens: int
    avg_output_tokens: int

    @model_validator(mode="after")
    def _non_negative(self) -> "SubagentVolume":
        for field in ("calls_per_day", "avg_input_tokens", "avg_output_tokens"):
            if getattr(self, field) < 0:
                raise ValueError(f"{self.subagent}.{field} must not be negative")
        return self


class VolumeSet(_Base):
    source: VolumeSource
    subagents: dict[str, SubagentVolume] = Field(default_factory=dict)

    @classmethod
    def illustrative(cls, cfg: AppConfig) -> "VolumeSet":
        """The profile's own numbers, labelled for what they are.

        Honours ``volumes_confirmed`` in the profile: a customer file whose
        volumes *have* been confirmed by a human editing the YAML is treated as
        customer-provided, with the profile's own ``confirmed_with`` /
        ``confirmed_on`` as the attribution.
        """
        profile = cfg.customer
        if profile.volumes_confirmed:
            source = VolumeSource(
                kind="customer-provided",
                provided_by=profile.confirmed_with or f"{profile.customer}.yaml",
                provided_on=profile.confirmed_on,
            )
        else:
            source = VolumeSource(kind="illustrative")
        return cls(
            source=source,
            subagents={
                name: SubagentVolume(
                    subagent=name,
                    calls_per_day=sub.calls_per_day,
                    avg_input_tokens=sub.avg_input_tokens,
                    avg_output_tokens=sub.avg_output_tokens,
                )
                for name, sub in profile.subagents.items()
                if sub.enabled and sub.evaluated
            },
        )


def confirm_volumes(
    cfg: AppConfig,
    volumes: Mapping[str, Mapping[str, int] | SubagentVolume],
    *,
    provided_by: str,
    provided_on: date | None = None,
) -> VolumeSet:
    """Record volumes the customer gave us in-session.

    Callable straight from a notebook cell (ground rule 8 keeps the logic here,
    not there) and wired to ``cli.py scorecard --volume``. Unspecified fields
    fall back to the profile's token averages, because a customer in a workshop
    knows their call rate long before they know their mean prompt length —
    but the *source* becomes customer-provided for the whole set, which the
    footer states, so nobody has to guess which half was supplied.

    :raises ConfigError: a subagent name that is not in the customer profile.
    """
    profile = cfg.customer
    unknown = sorted(set(volumes) - set(profile.subagents))
    if unknown:
        raise ConfigError(
            f"volumes given for {unknown}, which are not subagents of "
            f"{profile.customer}; known: {sorted(profile.subagents)}"
        )

    base = VolumeSet.illustrative(cfg)
    merged = dict(base.subagents)
    for name, value in volumes.items():
        if isinstance(value, SubagentVolume):
            merged[name] = value
            continue
        fallback = profile.subagent(name)
        merged[name] = SubagentVolume(
            subagent=name,
            calls_per_day=int(value["calls_per_day"]),
            avg_input_tokens=int(
                value.get("avg_input_tokens", fallback.avg_input_tokens)
            ),
            avg_output_tokens=int(
                value.get("avg_output_tokens", fallback.avg_output_tokens)
            ),
        )
    return VolumeSet(
        source=VolumeSource(
            kind="customer-provided",
            provided_by=provided_by,
            provided_on=provided_on or date.today(),
        ),
        subagents=merged,
    )


class CostRow(_Base):
    """One subagent, one volume multiplier, one caching scenario."""

    subagent: str
    multiplier: float
    caching: Literal["uncached", "cached"]
    baseline_model: str
    candidate_model: str
    calls_per_day: float
    baseline_daily_usd: float
    candidate_daily_usd: float

    @property
    def baseline_monthly_usd(self) -> float:
        return self.baseline_daily_usd * DAYS_PER_MONTH

    @property
    def candidate_monthly_usd(self) -> float:
        return self.candidate_daily_usd * DAYS_PER_MONTH

    @property
    def baseline_annual_usd(self) -> float:
        return self.baseline_daily_usd * DAYS_PER_YEAR

    @property
    def candidate_annual_usd(self) -> float:
        return self.candidate_daily_usd * DAYS_PER_YEAR

    @property
    def savings_pct(self) -> float | None:
        """Percent saved against the baseline. ``None`` when the baseline is
        zero — a saving against nothing is a division, not a finding."""
        if self.baseline_daily_usd == 0:
            return None
        return 100.0 * (
            (self.baseline_daily_usd - self.candidate_daily_usd)
            / self.baseline_daily_usd
        )


class CostModelResult(_Base):
    """Rows, or an explicit and attributable refusal to produce rows."""

    computable: bool
    blockers: list[Blocker] = Field(default_factory=list)
    volumes: VolumeSet
    rows: list[CostRow] = Field(default_factory=list)
    multipliers: list[float] = Field(default_factory=list)
    #: Fraction of input tokens served from cache in the "cached" rows.
    cached_input_share: float = 1.0
    prices_verified_on: date | None = None
    unverified_prices: list[str] = Field(default_factory=list)

    @property
    def state(self) -> str:
        """``not computable — pricing unverified / volumes unconfirmed``."""
        if self.computable:
            return "computed"
        return f"{NOT_COMPUTABLE} — {' / '.join(b.label for b in self.blockers)}"

    def footer_lines(self) -> list[str]:
        lines = [self.volumes.source.footer_label()]
        if self.prices_verified_on:
            lines.append(f"prices verified: {self.prices_verified_on.isoformat()}")
        else:
            lines.append(
                f"prices verified: never — {len(self.unverified_prices)} rate(s) "
                "still VERIFY in config/pricing.yaml"
            )
        return lines

    def rows_for(self, subagent: str) -> list[CostRow]:
        return [row for row in self.rows if row.subagent == subagent]


def _blockers(pricing: PricingConfig, volumes: VolumeSet) -> list[Blocker]:
    out: list[Blocker] = []
    if not pricing.is_verified:
        out.append(
            Blocker(
                gate="pricing",
                label="pricing unverified",
                reason=(
                    "config/pricing.yaml has "
                    f"{len(pricing.unverified_keys())} rate(s) still reading "
                    "VERIFY and verified_on is "
                    f"{pricing.verified_on or 'null'}"
                ),
                clears_when="a human runs scripts/refresh_pricing.py",
            )
        )
    if not volumes.source.confirmed:
        out.append(
            Blocker(
                gate="volumes",
                label="volumes unconfirmed",
                reason=(
                    "the customer profile's volume block is illustrative "
                    "(volumes_confirmed: false)"
                ),
                clears_when=(
                    "the customer states their call volumes — "
                    "economics.confirm_volumes() or `cli.py scorecard --volume`"
                ),
            )
        )
    return out


def _call_cost(
    pricing: PricingConfig,
    model_key: str,
    volume: SubagentVolume,
    *,
    cached_input_share: float,
) -> float:
    """USD for one call. Every rate comes from ``pricing.rate``, which raises
    on ``VERIFY`` — there is no local default to fall back to."""
    per_million = 1_000_000.0
    fresh_in = volume.avg_input_tokens * (1.0 - cached_input_share)
    cached_in = volume.avg_input_tokens * cached_input_share
    return (
        fresh_in / per_million * pricing.rate(model_key, "input")
        + cached_in / per_million * pricing.rate(model_key, "cached_input")
        + volume.avg_output_tokens / per_million * pricing.rate(model_key, "output")
    )


def cost_model(
    cfg: AppConfig,
    *,
    volumes: VolumeSet | None = None,
    multipliers: Iterable[float] = DEFAULT_MULTIPLIERS,
    cached_input_share: float = 1.0,
    baseline_role: str = BASELINE_ROLE,
    candidate_role: str = CANDIDATE_ROLE,
) -> CostModelResult:
    """Daily/monthly/annual run rate per subagent, cached and uncached.

    :param volumes: defaults to the customer profile's illustrative block. Pass
        the result of :func:`confirm_volumes` to use numbers the customer gave.
    :param cached_input_share: fraction of input tokens billed at the cached
        rate in the ``cached`` rows. Default 1.0 — the *upper bound* of caching
        upside, which is why it is reported as a separate row and never folded
        into the headline saving. :mod:`amw.economics.cache_breakeven` answers
        whether reaching it is worth the write and storage cost.

    Returns a result whose ``computable`` is ``False`` and whose ``rows`` are
    empty whenever either gate is closed.
    """
    volumes = volumes or VolumeSet.illustrative(cfg)
    multipliers = list(multipliers)
    if not 0.0 <= cached_input_share <= 1.0:
        raise ValueError(
            f"cached_input_share must be a fraction, got {cached_input_share}"
        )

    pricing = cfg.pricing
    blockers = _blockers(pricing, volumes)
    result = CostModelResult(
        computable=not blockers,
        blockers=blockers,
        volumes=volumes,
        multipliers=multipliers,
        cached_input_share=cached_input_share,
        prices_verified_on=pricing.verified_on if pricing.is_verified else None,
        unverified_prices=pricing.unverified_keys(),
    )
    if blockers:
        return result

    baseline_key, _ = cfg.models.for_role(baseline_role)
    candidate_key, _ = cfg.models.for_role(candidate_role)

    for name, volume in sorted(volumes.subagents.items()):
        for multiplier in multipliers:
            calls = volume.calls_per_day * multiplier
            for caching, share in (("uncached", 0.0), ("cached", cached_input_share)):
                try:
                    base = _call_cost(pricing, baseline_key, volume, cached_input_share=share)
                    cand = _call_cost(pricing, candidate_key, volume, cached_input_share=share)
                except UnverifiedPriceError:  # pragma: no cover - is_verified covers it
                    raise
                result.rows.append(
                    CostRow(
                        subagent=name,
                        multiplier=multiplier,
                        caching=caching,  # type: ignore[arg-type]
                        baseline_model=baseline_key,
                        candidate_model=candidate_key,
                        calls_per_day=calls,
                        baseline_daily_usd=base * calls,
                        candidate_daily_usd=cand * calls,
                    )
                )
    return result
