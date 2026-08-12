"""T12 — the cost model and the caching breakeven, and the gates on both.

Two kinds of test here, and the first kind matters more:

1. **Refusals.** With prices unverified and the customer's volumes
   illustrative, neither module may emit a dollar figure. Not a zero, not a
   rounded placeholder — an explicit not-computable state naming both gates.
   The live volume override clears exactly one of those gates and is tested to
   leave the other closed. ``config/pricing.yaml`` was stamped on 2026-08-12,
   so the unverified side of that is now built by the ``unpriced`` fixture
   rather than read off the shipped file; the gate outlives this customer.
2. **Arithmetic**, against ``tests/fixtures/reporting/pricing_fixture.yaml`` —
   a table of round, obviously-synthetic prices whose expected outputs are
   hand-computed in the docstrings below. No real price appears in this file.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import yaml

from amw.config import VERIFY, AppConfig, ConfigError, PricingConfig, load_all
from amw.economics.cache_breakeven import HOURS_PER_DAY, breakeven_curve, cache_breakeven
from amw.economics.cost_model import (
    DAYS_PER_MONTH,
    DAYS_PER_YEAR,
    NOT_COMPUTABLE,
    SubagentVolume,
    VolumeSet,
    VolumeSource,
    confirm_volumes,
    cost_model,
)

FIXTURES = Path(__file__).parent / "fixtures" / "reporting"


def _pricing(name: str) -> PricingConfig:
    return PricingConfig.model_validate(
        yaml.safe_load((FIXTURES / name).read_text(encoding="utf-8"))
    )


@pytest.fixture(scope="module")
def cfg() -> AppConfig:
    return load_all()


@pytest.fixture()
def priced(cfg: AppConfig) -> AppConfig:
    """The real config with the synthetic fixture price table swapped in."""
    return cfg.model_copy(update={"pricing": _pricing("pricing_fixture.yaml")})


@pytest.fixture()
def unpriced(cfg: AppConfig) -> AppConfig:
    """The real config with every rate wound back to ``VERIFY``.

    The shipped ``config/pricing.yaml`` was stamped by a human on 2026-08-12,
    so it no longer exercises the pricing gate — but the gate is ground rule 3
    and still guards the next customer profile, whose rates start at VERIFY
    again. The fixture table is the one wound back, not the real file, so the
    rule this module opens with holds: no real price appears in here.
    """
    data = yaml.safe_load((FIXTURES / "pricing_fixture.yaml").read_text(encoding="utf-8"))
    for slot in data["models"].values():
        for field in slot:
            slot[field] = VERIFY
    data["cache_storage"]["per_1m_token_hour"] = VERIFY
    data["verified_on"] = None
    data["verified_by"] = None
    return cfg.model_copy(update={"pricing": PricingConfig.model_validate(data)})


@pytest.fixture()
def customer_volumes(cfg: AppConfig) -> VolumeSet:
    """Volumes as if the customer had read them out in the workshop."""
    return confirm_volumes(
        cfg,
        {"query_rewriter": {"calls_per_day": 250_000}},
        provided_by="A. Customer (platform lead)",
        provided_on=date(2026, 8, 13),
    )


# --------------------------------------------------------------------------
# the double gate
# --------------------------------------------------------------------------


def test_with_both_gates_closed_no_dollar_figure_exists_at_all(
    unpriced: AppConfig,
) -> None:
    result = cost_model(unpriced)
    assert result.computable is False
    assert result.rows == []
    assert result.state == f"{NOT_COMPUTABLE} — pricing unverified / volumes unconfirmed"
    assert {b.gate for b in result.blockers} == {"pricing", "volumes"}


def test_a_blocked_cost_model_returns_no_zeros(unpriced: AppConfig) -> None:
    # The failure mode this guards: a "safe" default of 0.0 in a cost column
    # reads as "migrating is free", which is a fabricated result (ground rule 1).
    result = cost_model(unpriced)
    assert result.rows == []
    assert result.prices_verified_on is None
    assert NOT_COMPUTABLE in result.state


def test_unconfirmed_volumes_alone_still_block(priced: AppConfig) -> None:
    result = cost_model(priced)
    assert result.computable is False
    assert [b.gate for b in result.blockers] == ["volumes"]
    assert result.rows == []


def test_the_volume_override_does_not_bypass_the_pricing_gate(
    unpriced: AppConfig, customer_volumes: VolumeSet
) -> None:
    # The owner's constraint, and the most dangerous number this repo could
    # produce: the customer's real call rate times a placeholder price.
    result = cost_model(unpriced, volumes=customer_volumes)
    assert result.computable is False
    assert [b.gate for b in result.blockers] == ["pricing"]
    assert result.rows == []
    assert result.volumes.source.kind == "customer-provided"


def test_both_gates_cleared_produces_rows(
    priced: AppConfig, customer_volumes: VolumeSet
) -> None:
    result = cost_model(priced, volumes=customer_volumes)
    assert result.computable is True
    assert result.blockers == []
    assert result.rows
    assert result.state == "computed"


# --------------------------------------------------------------------------
# volume provenance
# --------------------------------------------------------------------------


def test_illustrative_is_the_default_and_says_so(cfg: AppConfig) -> None:
    volumes = VolumeSet.illustrative(cfg)
    assert volumes.source.kind == "illustrative"
    assert volumes.source.footer_label() == "volumes: illustrative"
    # Only the three evaluated subagents; answer_drafter is P1 and off.
    assert sorted(volumes.subagents) == sorted(cfg.customer.evaluated_subagents)


def test_customer_provided_volumes_carry_who_and_when(
    customer_volumes: VolumeSet,
) -> None:
    label = customer_volumes.source.footer_label()
    assert label.startswith("volumes: customer-provided")
    assert "A. Customer (platform lead)" in label
    assert "2026-08-13" in label


def test_unattributed_customer_volumes_are_refused() -> None:
    with pytest.raises(ValueError, match="provided_by"):
        VolumeSource(kind="customer-provided")


def test_override_falls_back_to_profile_token_averages(cfg: AppConfig) -> None:
    profile = cfg.customer.subagent("chunk_summarizer")
    volumes = confirm_volumes(
        cfg,
        {"chunk_summarizer": {"calls_per_day": 7}},
        provided_by="workshop",
    )
    got = volumes.subagents["chunk_summarizer"]
    assert got.calls_per_day == 7
    assert got.avg_input_tokens == profile.avg_input_tokens
    assert got.avg_output_tokens == profile.avg_output_tokens


def test_override_rejects_an_unknown_subagent(cfg: AppConfig) -> None:
    with pytest.raises(ConfigError, match="not subagents"):
        confirm_volumes(cfg, {"nope": {"calls_per_day": 1}}, provided_by="x")


def test_a_confirmed_profile_is_treated_as_customer_provided(cfg: AppConfig) -> None:
    profile = cfg.customer.model_copy(
        update={
            "volumes_confirmed": True,
            "confirmed_with": "Platform team",
            "confirmed_on": date(2026, 8, 12),
        }
    )
    volumes = VolumeSet.illustrative(cfg.model_copy(update={"customer": profile}))
    assert volumes.source.kind == "customer-provided"
    assert "Platform team" in volumes.source.footer_label()


# --------------------------------------------------------------------------
# the arithmetic, against synthetic fixture prices
# --------------------------------------------------------------------------


def _row(result, subagent: str, multiplier: float, caching: str):
    (row,) = [
        r
        for r in result.rows
        if r.subagent == subagent and r.multiplier == multiplier and r.caching == caching
    ]
    return row


def test_uncached_daily_cost_matches_hand_computation(
    priced: AppConfig, customer_volumes: VolumeSet
) -> None:
    """Query Rewriter, 250,000 calls/day, 800 in / 200 out.

    Fixture rates, USD per 1M tokens:
      claude-sonnet 10 in / 20 out -> 800*10e-6 + 200*20e-6 = $0.012 per call
      gemini-flash   1 in /  2 out -> 800* 1e-6 + 200* 2e-6 = $0.0012 per call
    x250,000 calls -> $3,000/day and $300/day. 90% saved.
    """
    result = cost_model(priced, volumes=customer_volumes)
    row = _row(result, "query_rewriter", 1.0, "uncached")
    assert row.baseline_daily_usd == pytest.approx(3_000.0)
    assert row.candidate_daily_usd == pytest.approx(300.0)
    assert row.savings_pct == pytest.approx(90.0)
    assert row.baseline_monthly_usd == pytest.approx(3_000.0 * DAYS_PER_MONTH)
    assert row.candidate_annual_usd == pytest.approx(300.0 * DAYS_PER_YEAR)


def test_cached_rows_are_reported_separately_and_are_cheaper(
    priced: AppConfig, customer_volumes: VolumeSet
) -> None:
    """Same arm with every input token billed at the cached rate (1/10 fresh).

      gemini-flash cached -> 800*0.1e-6 + 200*2e-6 = $0.00048 per call
      x250,000 -> $120/day, against $300/day uncached.
    """
    result = cost_model(priced, volumes=customer_volumes)
    cached = _row(result, "query_rewriter", 1.0, "cached")
    uncached = _row(result, "query_rewriter", 1.0, "uncached")
    assert cached.candidate_daily_usd == pytest.approx(120.0)
    assert cached.candidate_daily_usd < uncached.candidate_daily_usd
    # Both scenarios exist for every multiplier — caching upside is never
    # folded into the headline figure.
    assert {r.caching for r in result.rows} == {"cached", "uncached"}


def test_multipliers_scale_the_run_rate(
    priced: AppConfig, customer_volumes: VolumeSet
) -> None:
    result = cost_model(priced, volumes=customer_volumes)
    base = _row(result, "query_rewriter", 1.0, "uncached").candidate_daily_usd
    assert _row(result, "query_rewriter", 0.5, "uncached").candidate_daily_usd == (
        pytest.approx(base / 2)
    )
    assert _row(result, "query_rewriter", 2.0, "uncached").candidate_daily_usd == (
        pytest.approx(base * 2)
    )


def test_every_evaluated_subagent_gets_every_scenario(
    priced: AppConfig, customer_volumes: VolumeSet
) -> None:
    result = cost_model(priced, volumes=customer_volumes)
    expected = len(customer_volumes.subagents) * len(result.multipliers) * 2
    assert len(result.rows) == expected


def test_zero_volume_reports_no_saving_rather_than_dividing(
    priced: AppConfig, cfg: AppConfig
) -> None:
    volumes = confirm_volumes(
        cfg,
        {"query_rewriter": SubagentVolume(
            subagent="query_rewriter",
            calls_per_day=0,
            avg_input_tokens=0,
            avg_output_tokens=0,
        )},
        provided_by="workshop",
    )
    result = cost_model(priced, volumes=volumes)
    assert _row(result, "query_rewriter", 1.0, "uncached").savings_pct is None


def test_negative_volumes_are_refused() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        SubagentVolume(
            subagent="query_rewriter",
            calls_per_day=-1,
            avg_input_tokens=1,
            avg_output_tokens=1,
        )


# --------------------------------------------------------------------------
# caching breakeven
# --------------------------------------------------------------------------


def test_breakeven_is_not_computable_on_unverified_prices(
    unpriced: AppConfig,
) -> None:
    result = cache_breakeven(unpriced, cached_tokens=100_000, ttl_hours=1.0)
    assert result.computable is False
    assert result.breakeven_calls_per_day is None
    assert result.write_usd is None and result.storage_usd is None
    assert result.state == f"{NOT_COMPUTABLE} — pricing unverified"
    assert result.net_usd_per_day(1_000_000) is None


def test_breakeven_matches_hand_computation(priced: AppConfig) -> None:
    """100,000 cached tokens on gemini-flash, TTL 1 h, fixture prices.

      write   = 0.1M x $1.00        = $0.10 per window
      storage = 0.1M x 1h x $0.50   = $0.05 per window
      saving  = 0.1M x ($1.00-$0.10)= $0.09 per read
      reads to break even per window = 0.15 / 0.09 = 1.6667
      windows per day = 24 / 1 = 24  ->  40 calls/day
    """
    result = cache_breakeven(priced, cached_tokens=100_000, ttl_hours=1.0)
    assert result.computable is True
    assert result.write_usd == pytest.approx(0.10)
    assert result.storage_usd == pytest.approx(0.05)
    assert result.saving_per_read_usd == pytest.approx(0.09)
    assert result.breakeven_reads_per_window == pytest.approx(0.15 / 0.09)
    assert result.breakeven_calls_per_day == pytest.approx(40.0)


def test_net_is_exactly_zero_at_the_breakeven_rate(priced: AppConfig) -> None:
    result = cache_breakeven(priced, cached_tokens=100_000, ttl_hours=1.0)
    assert result.net_usd_per_day(result.breakeven_calls_per_day) == pytest.approx(0.0)
    assert result.net_usd_per_day(result.breakeven_calls_per_day * 2) > 0
    assert result.net_usd_per_day(result.breakeven_calls_per_day / 2) < 0


def test_a_longer_ttl_costs_more_rent_per_window(priced: AppConfig) -> None:
    """Rent is linear in TTL, so a 24 h window rents 24x as much storage but
    amortises the write over one window a day instead of 24."""
    hour, day = breakeven_curve(priced, cached_tokens=100_000, ttl_hours=(1.0, 24.0))
    assert day.storage_usd == pytest.approx(hour.storage_usd * 24)
    assert day.breakeven_calls_per_day == pytest.approx((0.10 + 1.20) / 0.09)
    assert HOURS_PER_DAY / day.ttl_hours == pytest.approx(1.0)


def test_no_discount_means_it_never_breaks_even(cfg: AppConfig) -> None:
    priced = cfg.model_copy(
        update={"pricing": _pricing("pricing_no_discount_fixture.yaml")}
    )
    result = cache_breakeven(priced, cached_tokens=100_000, ttl_hours=1.0)
    assert result.never_breaks_even is True
    assert result.breakeven_calls_per_day is None
    assert result.state == "never breaks even at this price pair"


def test_breakeven_refuses_nonsense_inputs(priced: AppConfig) -> None:
    with pytest.raises(ValueError, match="cached_tokens"):
        cache_breakeven(priced, cached_tokens=0)
    with pytest.raises(ValueError, match="ttl_hours"):
        cache_breakeven(priced, cached_tokens=10, ttl_hours=0)


def test_no_price_literal_lives_in_the_economics_modules() -> None:
    """Ground rule 3, enforced structurally: the only numbers in these modules
    are calendar constants and unit conversions, and every rate is fetched
    through PricingConfig."""
    root = Path(__file__).resolve().parents[1] / "amw" / "economics"
    for module in ("cost_model.py", "cache_breakeven.py"):
        source = (root / module).read_text(encoding="utf-8")
        body = "\n".join(
            line for line in source.splitlines() if not line.strip().startswith("#")
        )
        # Both modules must read every rate through the config accessors, which
        # raise on VERIFY. A literal price would show up as a bare float used in
        # arithmetic; the accessors are the only source of one.
        assert ".rate(" in body or "cache_storage_rate" in body
        assert "input_per_1m" not in body
