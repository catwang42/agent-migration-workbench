"""T01 — the config system must load the real config and reject broken ones.

The bias of every test here is toward *loud failure*. A config bug that throws
at load time costs a minute; one that silently defaults costs a wrong number in
front of a customer.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import yaml

from amw.config import (
    VERIFY,
    AppConfig,
    ConfigError,
    Gate,
    GatesConfig,
    ModelsConfig,
    PricingConfig,
    UnverifiedPriceError,
    default_config_dir,
    load_all,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def cfg() -> AppConfig:
    return load_all()


# --------------------------------------------------------------------------
# the real config loads
# --------------------------------------------------------------------------


def test_load_all_on_real_config(cfg: AppConfig) -> None:
    assert isinstance(cfg, AppConfig)
    assert cfg.config_dir == default_config_dir()
    assert cfg.customer.customer == "demo_patents"


def test_three_evaluated_subagents(cfg: AppConfig) -> None:
    # act1_build_plan.md §2: Query Rewriter, Chunk Summarizer, Feature
    # Extractor. Answer Drafter is P1; root orchestrator is a stub.
    assert cfg.customer.evaluated_subagents == [
        "query_rewriter",
        "chunk_summarizer",
        "feature_extractor",
    ]
    assert not cfg.customer.subagents["answer_drafter"].evaluated
    assert not cfg.customer.subagents["root_orchestrator"].evaluated


def test_every_model_role_resolves(cfg: AppConfig) -> None:
    for role in cfg.models.roles:
        key, spec = cfg.models.for_role(role)
        assert key in cfg.models.models
        assert spec.ids, f"{key} has no provider IDs"


def test_claude_vertex_ids_are_unprefixed(cfg: AppConfig) -> None:
    # On Vertex, current-generation Claude models use the bare first-party ID.
    # A vendor prefix ("anthropic.") is Bedrock's convention and 404s here.
    for key, spec in cfg.models.models.items():
        if spec.provider != "anthropic":
            continue
        vertex_id = spec.id_for("vertex")
        assert not vertex_id.startswith("anthropic.")
        assert vertex_id == spec.id_for("anthropic")


def test_unknown_role_and_model_raise(cfg: AppConfig) -> None:
    with pytest.raises(ConfigError, match="unknown model role"):
        cfg.models.for_role("no_such_role")
    with pytest.raises(ConfigError, match="unknown model key"):
        cfg.models.spec("no-such-model")


# --------------------------------------------------------------------------
# malformed / invalid config fails loudly
# --------------------------------------------------------------------------


def test_malformed_yaml_raises_naming_the_file() -> None:
    with pytest.raises(ConfigError) as exc:
        load_all(config_dir=FIXTURES / "config_malformed")
    assert "malformed YAML" in str(exc.value)
    assert "models.yaml" in str(exc.value)


def test_valid_yaml_but_invalid_config_raises() -> None:
    with pytest.raises(ConfigError) as exc:
        load_all(config_dir=FIXTURES / "config_invalid")
    assert "gemini-ultra-does-not-exist" in str(exc.value)


def test_missing_config_dir_raises() -> None:
    with pytest.raises(ConfigError, match="config directory not found"):
        load_all(config_dir=FIXTURES / "nope")


def test_unknown_customer_raises() -> None:
    with pytest.raises(ConfigError, match="missing config file"):
        load_all(customer="no_such_customer")


def test_unknown_key_is_rejected() -> None:
    # extra="forbid" everywhere: a typo must not become a silently ignored key.
    data = yaml.safe_load((default_config_dir() / "models.yaml").read_text())
    data["rolez"] = {}
    with pytest.raises(Exception) as exc:
        ModelsConfig.model_validate(data)
    assert "rolez" in str(exc.value)


# --------------------------------------------------------------------------
# ground rule 3: prices only from pricing.yaml, and only when verified
# --------------------------------------------------------------------------


def unverified_pricing() -> PricingConfig:
    """The shipped pricing file with every rate wound back to ``VERIFY``.

    The shipped file was stamped by a human on 2026-08-12, so it can no longer
    stand in for the unverified case. The refusal behaviour it used to exercise
    is ground rule 3 and does not stop mattering once prices land — it is what
    protects the *next* customer profile, whose rates start at VERIFY again —
    so the state is reconstructed here rather than the tests being deleted.
    """
    data = yaml.safe_load((default_config_dir() / "pricing.yaml").read_text())
    for slot in data["models"].values():
        for field in slot:
            slot[field] = VERIFY
    data["cache_storage"]["per_1m_token_hour"] = VERIFY
    data["verified_on"] = None
    data["verified_by"] = None
    return PricingConfig.model_validate(data)


def test_the_shipped_prices_are_verified_and_complete(cfg: AppConfig) -> None:
    """Freeze-day state: the walkthrough was run and nothing was left behind.

    Before 2026-08-12 this asserted the opposite — every slot still VERIFY.
    A half-walked file is the dangerous state, because `is_verified` would be
    False while some cells already carried real digits, so the count check
    stays: verified *and* zero unverified slots, not just the stamp.
    """
    assert cfg.pricing.verified_on == date(2026, 8, 12)
    assert cfg.pricing.verified_by
    assert cfg.pricing.is_verified
    assert cfg.pricing.unverified_keys() == []
    # 8 models x 3 fields + cache storage. Two generations are priced: the
    # 2.5-class development generation the tuning ladder was built on, and the
    # deployment candidates the workshop recommends migrating to (Gemini 3.6
    # Flash and Gemini 3.5 Flash), plus the preview Pro rung that is priced but
    # never measured. The eighth slot is the capped deployment configuration,
    # which is the SAME SKU as gemini-flash-current at the same rates — it has
    # its own row only because it has its own model key. The literal is
    # deliberate — a human walks refresh_pricing.py rate by rate, so adding a
    # model has to force a re-count here rather than silently lengthening that
    # walkthrough.
    assert len(unverified_pricing().unverified_keys()) == 25
    assert 25 == 3 * len(cfg.pricing.models) + 1


def test_reading_an_unverified_price_raises() -> None:
    pricing = unverified_pricing()
    with pytest.raises(UnverifiedPriceError, match="refresh_pricing"):
        pricing.rate("gemini-flash", "input")
    with pytest.raises(UnverifiedPriceError, match="refresh_pricing"):
        pricing.cache_storage_rate()


def test_verified_price_is_returned() -> None:
    data = unverified_pricing().model_dump()
    data["models"]["gemini-flash"]["input_per_1m"] = 0.3
    pricing = PricingConfig.model_validate(data)
    assert pricing.rate("gemini-flash", "input") == pytest.approx(0.3)
    # still not "verified" overall: other prices remain VERIFY
    assert not pricing.is_verified
    with pytest.raises(UnverifiedPriceError):
        pricing.rate("gemini-flash", "output")


def test_price_lookup_errors_are_specific(cfg: AppConfig) -> None:
    with pytest.raises(ConfigError, match="no pricing entry"):
        cfg.pricing.rate("not-a-model", "input")
    with pytest.raises(ConfigError, match="unknown price kind"):
        cfg.pricing.rate("gemini-flash", "sideways")


def test_every_model_is_priceable(cfg: AppConfig) -> None:
    assert set(cfg.models.models) <= set(cfg.pricing.models)


def test_every_shipped_rate_says_where_it_is_read_from(cfg: AppConfig) -> None:
    """A price nobody can re-check is a price nobody should quote.

    ``page_sections`` is optional in the schema so a two-model test fixture
    stays writable, but the shipped file has to cite every slot: those are the
    citations refresh_pricing.py reads aloud, once, on freeze morning.
    """
    assert cfg.pricing.uncited_keys() == []


def test_no_shipped_citation_points_at_a_slot_that_does_not_exist(
    cfg: AppConfig,
) -> None:
    """A misspelled key would silently leave the real slot uncited."""
    assert cfg.pricing.stale_sections() == []
    data = yaml.safe_load((default_config_dir() / "pricing.yaml").read_text())
    data["page_sections"]["gemini-flsah"] = "typo"
    assert PricingConfig.model_validate(data).stale_sections() == ["gemini-flsah"]


def test_missing_price_entry_fails_cross_validation(tmp_path: Path) -> None:
    src = default_config_dir()
    dst = tmp_path / "config"
    (dst / "customers").mkdir(parents=True)
    for name in ("models.yaml", "gates.yaml"):
        (dst / name).write_text((src / name).read_text())
    (dst / "customers" / "demo_patents.yaml").write_text(
        (src / "customers" / "demo_patents.yaml").read_text()
    )
    pricing = yaml.safe_load((src / "pricing.yaml").read_text())
    del pricing["models"]["gemini-flash"]
    (dst / "pricing.yaml").write_text(yaml.safe_dump(pricing))

    with pytest.raises(ConfigError, match="no entry in pricing.yaml"):
        load_all(config_dir=dst)


# --------------------------------------------------------------------------
# gates
# --------------------------------------------------------------------------


def test_gates_load_with_expected_bounds(cfg: AppConfig) -> None:
    gates = cfg.gates
    assert gates.gate("quality_delta_pp").bound == pytest.approx(-2.0)
    assert gates.gate("json_schema_validity").bound == pytest.approx(0.99)
    assert gates.gate("groundedness_delta_pp").bound == pytest.approx(-1.0)
    assert gates.gate("shadow_agreement").bound == pytest.approx(0.90)
    assert gates.gate("cost_savings_pct").bound == pytest.approx(30)
    assert set(gates.verdicts) == {"MIGRATE", "TUNE_FIRST", "HOLD"}


def test_latency_gate_is_a_resolvable_sentinel(cfg: AppConfig) -> None:
    gate = cfg.gates.gate("latency_p95")
    assert gate.direction == "max"
    assert gate.is_sentinel
    assert gate.bound in cfg.gates.sentinels


def test_gate_needs_exactly_one_bound() -> None:
    with pytest.raises(Exception, match="exactly one"):
        Gate.model_validate({"basis": "x"})
    with pytest.raises(Exception, match="exactly one"):
        Gate.model_validate({"min": 1.0, "max": 2.0, "basis": "x"})


def test_undeclared_sentinel_is_rejected() -> None:
    data = yaml.safe_load((default_config_dir() / "gates.yaml").read_text())
    data["subagent_gates"]["latency_p95"]["max"] = "made_up_sentinel"
    with pytest.raises(Exception, match="unknown sentinel"):
        GatesConfig.model_validate(data)


def test_verdict_referencing_unknown_gate_is_rejected() -> None:
    data = yaml.safe_load((default_config_dir() / "gates.yaml").read_text())
    data["verdicts"]["HOLD"]["blocking"] = ["not_a_gate"]
    with pytest.raises(Exception, match="unknown gate"):
        GatesConfig.model_validate(data)


def test_gates_version_hash_tracks_file_contents(cfg: AppConfig, tmp_path: Path) -> None:
    src = default_config_dir()
    dst = tmp_path / "config"
    (dst / "customers").mkdir(parents=True)
    for name in ("models.yaml", "pricing.yaml", "gates.yaml"):
        (dst / name).write_text((src / name).read_text())
    (dst / "customers" / "demo_patents.yaml").write_text(
        (src / "customers" / "demo_patents.yaml").read_text()
    )

    assert load_all(config_dir=dst).gates_version_hash == cfg.gates_version_hash

    # Move a threshold -> the hash on the report footer must change.
    (dst / "gates.yaml").write_text(
        (dst / "gates.yaml").read_text().replace("min: -2.0", "min: -5.0")
    )
    assert load_all(config_dir=dst).gates_version_hash != cfg.gates_version_hash


# --------------------------------------------------------------------------
# customer profile / provenance
# --------------------------------------------------------------------------


def test_dataset_sizing_matches_the_build_plan(cfg: AppConfig) -> None:
    ds = cfg.customer.dataset
    assert ds.cases_per_subagent == 70
    assert 25 <= ds.judged_core_set <= 30
    assert ds.judge_repeats == 2


def test_core_set_cannot_exceed_full_set() -> None:
    data = yaml.safe_load(
        (default_config_dir() / "customers" / "demo_patents.yaml").read_text()
    )
    data["dataset"]["judged_core_set"] = 999
    with pytest.raises(Exception, match="exceeds"):
        from amw.config import CustomerProfile

        CustomerProfile.model_validate(data)


def test_demo_profile_declares_synthetic_provenance_and_a_seed(cfg: AppConfig) -> None:
    assert cfg.customer.provenance == "synthetic"
    assert isinstance(cfg.customer.seed, int)


def test_demo_volumes_are_flagged_unconfirmed(cfg: AppConfig) -> None:
    # Illustrative volumes must never read as customer-confirmed.
    assert cfg.customer.volumes_confirmed is False
    assert cfg.customer.confirmed_with is None


def test_provenance_footer_carries_everything_a_report_must_print(
    cfg: AppConfig,
) -> None:
    footer = cfg.provenance_footer()
    assert footer["provenance"] == "synthetic"
    assert footer["region"] == cfg.customer.region
    assert footer["gates_version_hash"] == cfg.gates_version_hash
    assert footer["prices_verified_on"] == "2026-08-12"
    assert footer["volumes_confirmed"] is False


def test_customer_name_must_match_filename(tmp_path: Path) -> None:
    src = default_config_dir()
    dst = tmp_path / "config"
    (dst / "customers").mkdir(parents=True)
    for name in ("models.yaml", "pricing.yaml", "gates.yaml"):
        (dst / name).write_text((src / name).read_text())
    profile = yaml.safe_load((src / "customers" / "demo_patents.yaml").read_text())
    profile["customer"] = "someone_else"
    (dst / "customers" / "demo_patents.yaml").write_text(yaml.safe_dump(profile))

    with pytest.raises(ConfigError, match="but the file is named"):
        load_all(config_dir=dst)


def test_verify_sentinel_value() -> None:
    assert VERIFY == "VERIFY"
