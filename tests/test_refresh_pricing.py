"""The pricing refresh runs once, on freeze morning, under time pressure.

These tests exist so it cannot fail then: the rewrite must preserve comments,
produce a file that loads back cleanly, and never claim verification it does
not have.
"""

from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path

import pytest
import yaml

from amw.config import PricingConfig, default_config_dir

_spec = importlib.util.spec_from_file_location(
    "refresh_pricing",
    Path(__file__).resolve().parent.parent / "scripts" / "refresh_pricing.py",
)
refresh_pricing = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(refresh_pricing)

PRICING = default_config_dir() / "pricing.yaml"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0.3, "0.3"), (30, "30"), (1.25, "1.25"), (0.000125, "0.000125"), (0, "0")],
)
def test_format_price_never_uses_exponent(value: float, expected: str) -> None:
    assert refresh_pricing.format_price(value) == expected


def test_apply_updates_fills_prices_and_stamps() -> None:
    text = PRICING.read_text()
    # Every model in the shipped file, not a hand-listed subset: the assertion
    # below is that a full walkthrough leaves nothing unverified, which is only
    # a real assertion if "full" tracks the file.
    shipped = PricingConfig.model_validate(yaml.safe_load(text))
    updates = {
        f"models.{m}.{f}": 1.5
        for m in shipped.models
        for f in refresh_pricing.PRICE_FIELDS
    }
    updates["cache_storage.per_1m_token_hour"] = 0.25

    new_text = refresh_pricing.apply_updates(
        text, updates, verified_on=date(2026, 8, 11), verified_by="A. Operator"
    )

    pricing = PricingConfig.model_validate(yaml.safe_load(new_text))
    assert pricing.unverified_keys() == []
    assert pricing.is_verified
    assert pricing.verified_on == date(2026, 8, 11)
    assert pricing.verified_by == "A. Operator"
    assert pricing.rate("claude-sonnet", "cached_input") == pytest.approx(1.5)
    assert pricing.cache_storage_rate() == pytest.approx(0.25)
    assert pricing.sources == PricingConfig.model_validate(
        yaml.safe_load(text)
    ).sources


def test_apply_updates_preserves_comments() -> None:
    text = PRICING.read_text()
    new_text = refresh_pricing.apply_updates(
        text, {"models.gemini-flash.input_per_1m": 0.3}
    )
    assert "THE ONLY PLACE PRICES LIVE" in new_text
    assert "refresh_pricing.py" in new_text
    # untouched prices stay VERIFY
    assert new_text.count("VERIFY") == text.count("VERIFY") - 1


def test_partial_update_leaves_the_file_unstamped() -> None:
    new_text = refresh_pricing.apply_updates(
        PRICING.read_text(), {"models.gemini-flash.input_per_1m": 0.3}
    )
    pricing = PricingConfig.model_validate(yaml.safe_load(new_text))
    assert pricing.verified_on is None
    assert not pricing.is_verified


def test_unplaceable_update_raises_rather_than_silently_dropping() -> None:
    with pytest.raises(RuntimeError, match="could not place"):
        refresh_pricing.apply_updates(
            PRICING.read_text(), {"models.no-such-model.input_per_1m": 1.0}
        )
