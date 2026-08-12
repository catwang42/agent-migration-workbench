"""Typed loaders for everything under ``config/``.

This module is the single enforcement point for three of the project's ground
rules (CLAUDE.md):

* **Prices only from pricing.yaml.** Unverified prices are the literal string
  ``VERIFY``; :meth:`PricingConfig.rate` raises :class:`UnverifiedPriceError`
  rather than returning a plausible-looking number. Nothing downstream can
  quietly produce a dollar figure from an unverified table.
* **Thresholds only from gates.yaml, model IDs only from models.yaml.** Code
  refers to logical keys (``claude-sonnet``, ``quality_delta_pp``); the mapping
  to provider IDs and numbers lives in YAML.
* **Fail loudly.** Every model forbids unknown keys, so a typo in a config file
  is an error at load time, not a silently-ignored setting that surfaces as a
  wrong number in a customer report.

Usage::

    from amw.config import load_all
    cfg = load_all()                      # real config/, default customer
    cfg = load_all(customer="demo_patents")
"""

from __future__ import annotations

import hashlib
import os
from datetime import date
from pathlib import Path
from typing import Any, Literal, Union

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

__all__ = [
    "ConfigError",
    "UnverifiedPriceError",
    "VERIFY",
    "ModelSpec",
    "ModelsConfig",
    "ModelPrices",
    "PricingConfig",
    "Gate",
    "VerdictRule",
    "GatesConfig",
    "SubagentProfile",
    "CustomerProfile",
    "AppConfig",
    "load_all",
    "default_config_dir",
]

# Sentinel meaning "a human has not yet checked this price against the vendor
# page". See scripts/refresh_pricing.py.
VERIFY = "VERIFY"

PriceValue = Union[Literal["VERIFY"], float]

REPO_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(Exception):
    """Raised for any unreadable, malformed, or invalid configuration."""


class UnverifiedPriceError(ConfigError):
    """Raised when code asks for a price that still reads ``VERIFY``."""


class _Base(BaseModel):
    """Strict base: unknown keys are errors, not shrugs."""

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# models.yaml
# --------------------------------------------------------------------------


class ModelSpec(_Base):
    provider: Literal["anthropic", "google"]
    display_name: str
    #: access path -> provider model ID, e.g. {"vertex": "claude-sonnet-5"}
    ids: dict[str, str]
    context_window: int
    supports_json_schema: bool
    supports_tools: bool
    #: Pin this model to one region, overriding ``$REGION``. Set only when the
    #: model is not served where the rest of the workbench runs — the
    #: current-generation Gemini SKUs serve in ``global`` and 404 in
    #: ``us-central1``. Leaving it unset is what keeps the gated judge running
    #: exactly where it ran when it was registered; an instrument that moves
    #: region on freeze day is not the instrument the comparison was agreed on.
    region: str | None = None
    #: Cap on the model's internal reasoning tokens. ``None`` means "the
    #: model's default", which is what every arm recorded before 2026-08-12
    #: ran on and therefore what must stay the default here.
    #:
    #: It lives on the model spec rather than on a rung or a callsite for the
    #: same reason ``region`` does: the replay store is keyed on
    #: ``(subagent, model, input_sha)`` and ``input_sha`` folds in the prompt
    #: and the tools, not the sampling configuration. Two arms with the same
    #: prompt and different budgets under one model key would collide in the
    #: corpus and the later recording would silently supersede the earlier.
    #: A capped configuration is therefore its own model key.
    thinking_budget: int | None = None
    notes: str | None = None
    #: What this model *is* in the study, in reader-facing words. Drives the
    #: published "Models in this study" page, so a model cannot appear in a
    #: result without the page being able to say what part it played. Free
    #: text rather than an enum: a model can hold two roles at once (the
    #: incumbent is also the cross-check judge) and the honest description of
    #: a role is a phrase, not a token.
    study_roles: list[str] = []

    def id_for(self, path: str) -> str:
        """Provider model ID for an access path (``vertex``/``anthropic``)."""
        try:
            return self.ids[path]
        except KeyError:
            raise ConfigError(
                f"{self.display_name} has no model ID for access path {path!r}; "
                f"known paths: {sorted(self.ids)}"
            ) from None


class ModelsConfig(_Base):
    ids_verified_on: date | None = None
    roles: dict[str, str]
    models: dict[str, ModelSpec]

    @model_validator(mode="after")
    def _roles_point_at_real_models(self) -> "ModelsConfig":
        for role, key in self.roles.items():
            if key not in self.models:
                raise ValueError(
                    f"roles.{role} = {key!r} is not a key under models: "
                    f"{sorted(self.models)}"
                )
        return self

    def for_role(self, role: str) -> tuple[str, ModelSpec]:
        """``(model_key, spec)`` for a logical role such as ``judge``."""
        try:
            key = self.roles[role]
        except KeyError:
            raise ConfigError(
                f"unknown model role {role!r}; known roles: {sorted(self.roles)}"
            ) from None
        return key, self.models[key]

    def spec(self, model_key: str) -> ModelSpec:
        try:
            return self.models[model_key]
        except KeyError:
            raise ConfigError(
                f"unknown model key {model_key!r}; known: {sorted(self.models)}"
            ) from None


# --------------------------------------------------------------------------
# pricing.yaml
# --------------------------------------------------------------------------


class ModelPrices(_Base):
    input_per_1m: PriceValue
    output_per_1m: PriceValue
    cached_input_per_1m: PriceValue


class CacheStoragePrice(_Base):
    per_1m_token_hour: PriceValue


#: Reserved key in ``page_sections`` for the storage-rent slot, which is not a
#: model.
CACHE_STORAGE_SECTION_KEY = "cache_storage"


class PricingConfig(_Base):
    verified_on: date | None = None
    verified_by: str | None = None
    sources: list[str]
    models: dict[str, ModelPrices]
    cache_storage: CacheStoragePrice
    #: Slot key -> where on the source page that rate is printed. Citations,
    #: not prices: nothing here reaches a calculation. ``refresh_pricing.py``
    #: reads them aloud so an operator can see they are on the right table
    #: before typing. Optional in the schema (test fixtures price a model or
    #: two without a page), required of the shipped file by a test.
    page_sections: dict[str, str] = {}

    def uncited_keys(self) -> list[str]:
        """Priced slots with no ``page_sections`` entry."""
        missing = [key for key in self.models if not self.page_section(key)]
        if not self.page_section(CACHE_STORAGE_SECTION_KEY):
            missing.append(CACHE_STORAGE_SECTION_KEY)
        return missing

    def stale_sections(self) -> list[str]:
        """Citations for slots this file does not price.

        Not a validation error: a stale citation misleads nobody and cannot
        reach a number, whereas raising here would mask the *missing price*
        error that a removed model should actually produce. Caught by a test
        over the shipped file instead, where a typo is worth failing on.
        """
        known = set(self.models) | {CACHE_STORAGE_SECTION_KEY}
        return sorted(set(self.page_sections) - known)

    def page_section(self, slot_key: str) -> str | None:
        """Where to read ``slot_key``'s rate, or None if uncited."""
        section = self.page_sections.get(slot_key)
        return " ".join(section.split()) if section else None

    @property
    def is_verified(self) -> bool:
        """True only when a human stamped the file *and* no VERIFY remains."""
        if self.verified_on is None:
            return False
        return not self.unverified_keys()

    def unverified_keys(self) -> list[str]:
        """Dotted paths of every price still reading ``VERIFY``."""
        missing: list[str] = []
        for model_key, prices in self.models.items():
            for field in ModelPrices.model_fields:
                if getattr(prices, field) == VERIFY:
                    missing.append(f"models.{model_key}.{field}")
        if self.cache_storage.per_1m_token_hour == VERIFY:
            missing.append("cache_storage.per_1m_token_hour")
        return missing

    def rate(self, model_key: str, kind: str) -> float:
        """USD per 1M tokens for ``kind`` in {input, output, cached_input}.

        Raises :class:`UnverifiedPriceError` on a ``VERIFY`` value. This is the
        only supported way to read a price: no caller may fall back to a
        default, because a defaulted price becomes a fabricated saving.
        """
        try:
            prices = self.models[model_key]
        except KeyError:
            raise ConfigError(
                f"no pricing entry for model {model_key!r}; known: {sorted(self.models)}"
            ) from None
        field = f"{kind}_per_1m"
        if field not in ModelPrices.model_fields:
            raise ConfigError(
                f"unknown price kind {kind!r}; expected one of "
                f"{[f.removesuffix('_per_1m') for f in ModelPrices.model_fields]}"
            )
        value = getattr(prices, field)
        if value == VERIFY:
            raise UnverifiedPriceError(
                f"pricing.yaml models.{model_key}.{field} is still VERIFY. "
                "Run `python scripts/refresh_pricing.py` before producing any "
                "cost figure."
            )
        return float(value)

    def cache_storage_rate(self) -> float:
        value = self.cache_storage.per_1m_token_hour
        if value == VERIFY:
            raise UnverifiedPriceError(
                "pricing.yaml cache_storage.per_1m_token_hour is still VERIFY. "
                "Run `python scripts/refresh_pricing.py` first."
            )
        return float(value)


# --------------------------------------------------------------------------
# gates.yaml
# --------------------------------------------------------------------------


class Gate(_Base):
    #: A numeric bound, or one of the sentinel strings declared in `sentinels`
    #: (resolved against measured baseline stats at scorecard time).
    min: float | str | None = None
    max: float | str | None = None
    basis: str
    alt: str | None = None

    @model_validator(mode="after")
    def _exactly_one_bound(self) -> "Gate":
        if (self.min is None) == (self.max is None):
            raise ValueError("a gate needs exactly one of `min` or `max`")
        return self

    @property
    def bound(self) -> float | str:
        return self.min if self.min is not None else self.max

    @property
    def direction(self) -> Literal["min", "max"]:
        return "min" if self.min is not None else "max"

    @property
    def is_sentinel(self) -> bool:
        return isinstance(self.bound, str)


class VerdictRule(_Base):
    rule: Literal["all_pass", "only_quality_gates_fail", "any_blocking_gate_fails"]
    description: str
    quality: list[str] = []
    blocking: list[str] = []


class GatesConfig(_Base):
    version: int
    subagent_gates: dict[str, Gate]
    verdicts: dict[str, VerdictRule]
    sentinels: list[str]

    #: sha256 of the raw file bytes, truncated. Printed on every report footer
    #: so a reader can confirm thresholds were not moved after the run.
    version_hash: str = ""

    @model_validator(mode="after")
    def _validate_references(self) -> "GatesConfig":
        allowed = set(self.sentinels)
        for name, gate in self.subagent_gates.items():
            if gate.is_sentinel and gate.bound not in allowed:
                raise ValueError(
                    f"gate {name!r} uses unknown sentinel {gate.bound!r}; "
                    f"declared sentinels: {sorted(allowed)}"
                )
        known = set(self.subagent_gates)
        for verdict, rule in self.verdicts.items():
            for group in ("quality", "blocking"):
                for gate_name in getattr(rule, group):
                    if gate_name not in known:
                        raise ValueError(
                            f"verdicts.{verdict}.{group} references unknown gate "
                            f"{gate_name!r}; known gates: {sorted(known)}"
                        )
        return self

    def gate(self, name: str) -> Gate:
        try:
            return self.subagent_gates[name]
        except KeyError:
            raise ConfigError(
                f"unknown gate {name!r}; known: {sorted(self.subagent_gates)}"
            ) from None


# --------------------------------------------------------------------------
# customers/*.yaml
# --------------------------------------------------------------------------


class SubagentProfile(_Base):
    enabled: bool
    evaluated: bool
    tier: str = "P0"
    calls_per_day: int
    avg_input_tokens: int
    avg_output_tokens: int


class DatasetProfile(_Base):
    cases_per_subagent: int
    judged_core_set: int
    judge_repeats: int

    @model_validator(mode="after")
    def _core_fits_in_full_set(self) -> "DatasetProfile":
        if self.judged_core_set > self.cases_per_subagent:
            raise ValueError(
                f"judged_core_set ({self.judged_core_set}) exceeds "
                f"cases_per_subagent ({self.cases_per_subagent})"
            )
        return self


class CustomerProfile(_Base):
    customer: str
    display_name: str
    domain: str
    provenance: Literal["synthetic", "customer"]
    seed: int
    region: str
    dataset: DatasetProfile
    volumes_confirmed: bool
    confirmed_with: str | None = None
    confirmed_on: date | None = None
    subagents: dict[str, SubagentProfile]

    @property
    def evaluated_subagents(self) -> list[str]:
        return [
            name
            for name, profile in self.subagents.items()
            if profile.enabled and profile.evaluated
        ]

    def subagent(self, name: str) -> SubagentProfile:
        try:
            return self.subagents[name]
        except KeyError:
            raise ConfigError(
                f"customer {self.customer!r} has no subagent {name!r}; "
                f"known: {sorted(self.subagents)}"
            ) from None


# --------------------------------------------------------------------------
# top level
# --------------------------------------------------------------------------


class AppConfig(_Base):
    models: ModelsConfig
    pricing: PricingConfig
    gates: GatesConfig
    customer: CustomerProfile
    config_dir: Path

    @property
    def gates_version_hash(self) -> str:
        return self.gates.version_hash

    def provenance_footer(self) -> dict[str, Any]:
        """The fields every report footer must print (ground rule 2).

        Deliberately omits run date: that belongs to the run, not the config.
        """
        return {
            "customer": self.customer.customer,
            "provenance": self.customer.provenance,
            "seed": self.customer.seed,
            "region": self.customer.region,
            "gates_version": self.gates.version,
            "gates_version_hash": self.gates.version_hash,
            "prices_verified_on": (
                self.pricing.verified_on.isoformat()
                if self.pricing.verified_on
                else "UNVERIFIED"
            ),
            "volumes_confirmed": self.customer.volumes_confirmed,
        }


def default_config_dir() -> Path:
    return REPO_ROOT / "config"


def _read_yaml(path: Path) -> Any:
    if not path.is_file():
        raise ConfigError(f"missing config file: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"malformed YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(
            f"{path} must contain a YAML mapping at the top level, got "
            f"{type(data).__name__}"
        )
    return data


def _parse(model: type[BaseModel], data: dict[str, Any], path: Path):
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"invalid config in {path}:\n{exc}") from exc


def load_all(
    config_dir: str | Path | None = None,
    customer: str | None = None,
) -> AppConfig:
    """Load and cross-validate every config file. Raises :class:`ConfigError`.

    :param config_dir: defaults to ``<repo>/config``.
    :param customer: profile stem under ``config/customers/``; defaults to
        ``$AMW_CUSTOMER`` and then ``demo_patents``.
    """
    cfg_dir = Path(config_dir) if config_dir is not None else default_config_dir()
    if not cfg_dir.is_dir():
        raise ConfigError(f"config directory not found: {cfg_dir}")

    customer_name = customer or os.environ.get("AMW_CUSTOMER") or "demo_patents"

    models_path = cfg_dir / "models.yaml"
    pricing_path = cfg_dir / "pricing.yaml"
    gates_path = cfg_dir / "gates.yaml"
    customer_path = cfg_dir / "customers" / f"{customer_name}.yaml"

    models = _parse(ModelsConfig, _read_yaml(models_path), models_path)
    pricing = _parse(PricingConfig, _read_yaml(pricing_path), pricing_path)

    gates_data = _read_yaml(gates_path)
    if "version_hash" in gates_data:
        raise ConfigError(
            f"{gates_path}: version_hash is computed from the file contents and "
            "must not be written into the file"
        )
    gates = _parse(GatesConfig, gates_data, gates_path)
    gates.version_hash = hashlib.sha256(gates_path.read_bytes()).hexdigest()[:12]

    profile = _parse(CustomerProfile, _read_yaml(customer_path), customer_path)
    if profile.customer != customer_name:
        raise ConfigError(
            f"{customer_path}: customer field is {profile.customer!r} but the "
            f"file is named {customer_name}.yaml"
        )

    # --- cross-file checks: catch a half-edited config at load time ---
    missing_prices = sorted(set(models.models) - set(pricing.models))
    if missing_prices:
        raise ConfigError(
            f"models.yaml declares {missing_prices} with no entry in "
            f"{pricing_path.name}. Every model must be priceable."
        )

    return AppConfig(
        models=models,
        pricing=pricing,
        gates=gates,
        customer=profile,
        config_dir=cfg_dir,
    )
