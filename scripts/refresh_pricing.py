#!/usr/bin/env python3
"""Interactively fill in config/pricing.yaml.

Run this on freeze day, before any cost figure is shown to a customer:

    python scripts/refresh_pricing.py

It prompts once per price, shows the source URLs it expects you to be reading,
rewrites the file in place (comments preserved), and stamps ``verified_on`` +
``verified_by``.

Every prompt line carries the three things needed to know it is the right
number: the **concrete provider model ID** (resolved out of config/models.yaml,
not the logical key), the **section of the pricing page** the rate is printed
in (from ``page_sections`` in pricing.yaml), and the **unit** the slot expects.
Logical keys like ``gemini-flash-current`` do not appear on any vendor page, so
a walkthrough that only named them would be asking an operator to do the
mapping from memory, once, under time pressure.

It deliberately will NOT stamp the file while any price still reads VERIFY —
a half-verified table that claims to be verified is exactly the failure mode
the VERIFY sentinel exists to prevent.

    --dry-run   print the rewritten file to stdout instead of saving
    --file P    operate on a different pricing.yaml (used by the tests)
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from amw.config import (  # noqa: E402
    CACHE_STORAGE_SECTION_KEY,
    VERIFY,
    ModelsConfig,
    PricingConfig,
)

PRICE_FIELDS = ("input_per_1m", "output_per_1m", "cached_input_per_1m")


def format_price(value: float) -> str:
    """Plain decimal, no exponent, no trailing zeros (0.30 -> '0.3')."""
    text = f"{float(value):.10f}".rstrip("0").rstrip(".")
    return text or "0"


def apply_updates(
    text: str,
    updates: dict[str, float],
    verified_on: date | None = None,
    verified_by: str | None = None,
) -> str:
    """Rewrite pricing.yaml text, preserving comments and layout.

    ``updates`` keys are dotted paths: ``models.<key>.<field>`` and
    ``cache_storage.per_1m_token_hour``.
    """
    remaining = dict(updates)
    section: str | None = None
    out: list[str] = []

    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        indent = len(line) - len(stripped)

        if stripped and not stripped.startswith("#") and indent == 0:
            section = stripped.split(":", 1)[0].strip()

            if section == "verified_on" and verified_on is not None:
                out.append(f"verified_on: {verified_on.isoformat()}\n")
                continue
            if section == "verified_by" and verified_by is not None:
                out.append(f'verified_by: "{verified_by}"\n')
                continue

        if section == "models" and indent == 2 and ":" in stripped:
            model_key = stripped.split(":", 1)[0].strip()
            for field in PRICE_FIELDS:
                path = f"models.{model_key}.{field}"
                if path in remaining:
                    line, n = re.subn(
                        rf"(\b{re.escape(field)}:\s*){VERIFY}\b",
                        lambda m: m.group(1) + format_price(remaining[path]),
                        line,
                        count=1,
                    )
                    if n:
                        remaining.pop(path)

        elif section == "cache_storage" and indent == 2:
            path = "cache_storage.per_1m_token_hour"
            if path in remaining:
                line, n = re.subn(
                    rf"(\bper_1m_token_hour:\s*){VERIFY}\b",
                    lambda m: m.group(1) + format_price(remaining[path]),
                    line,
                    count=1,
                )
                if n:
                    remaining.pop(path)

        out.append(line)

    if remaining:
        raise RuntimeError(
            "could not place these prices in the file (was the VERIFY value "
            f"already replaced?): {sorted(remaining)}"
        )
    return "".join(out)


#: Units, per field. ``cache_storage.per_1m_token_hour`` is rent — USD per 1M
#: cached tokens *per hour* — not a per-token price, and it is the only input to
#: the caching breakeven curve. Prompting for it under the same "USD / 1M
#: tokens" label as the four token prices invites an hourly rate being entered
#: as a flat one, which the file cannot detect and the breakeven table would
#: silently report.
TOKEN_UNIT = "USD / 1M tokens"
CACHE_STORAGE_UNIT = "USD / 1M cached tokens / hour"

#: Printed when a slot has no ``page_sections`` entry or no resolvable ID.
#: Loud on purpose: an uncited rate is one nobody can re-check next quarter.
UNCITED = "!! not cited in pricing.yaml page_sections — add it before typing a rate"
UNRESOLVED_ID = "!! no models.yaml beside this file — ID unresolved"


def access_path(provider: str) -> str:
    """Which vendor's ID (and therefore which price list) applies.

    Claude is dual-path: the Vertex partner offering and the direct Anthropic
    API are different SKUs on different pages. Printing the ID for the path
    this platform does not call would point the operator at the wrong table.
    """
    if provider == "anthropic":
        return os.environ.get("CLAUDE_PATH") or "vertex"
    return "vertex"


def load_models(pricing_path: Path) -> ModelsConfig | None:
    """``models.yaml`` beside ``pricing.yaml``, or None if there is none.

    None rather than an error, so ``--file`` aimed at a bare fixture still
    runs; the walkthrough then says the ID is unresolved rather than printing
    a logical key as though it were a provider ID.
    """
    path = pricing_path.parent / "models.yaml"
    if not path.exists():
        return None
    return ModelsConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def describe_model(model_key: str, models: ModelsConfig | None) -> str:
    """The concrete provider model ID line for a pricing slot."""
    if models is None:
        return UNRESOLVED_ID
    try:
        spec = models.spec(model_key)
    except Exception:
        return (
            f"!! {model_key} is priced here but absent from models.yaml — "
            f"nothing runs on it"
        )
    path = access_path(spec.provider)
    try:
        model_id = spec.id_for(path)
    except Exception as exc:
        return f"!! {exc}"
    if path == "anthropic":
        where = "direct Anthropic API"
    else:
        where = f"vertex, region {_region_for(spec)}"
    return f"{model_id}   ({spec.display_name}, {where})"


def _region_for(spec) -> str:
    """The region this model is actually called in.

    Mirrors the adapters' own resolution order rather than guessing: Claude
    reads ``$CLAUDE_REGION`` first (it runs in `global` while Gemini runs in
    us-central1), and a per-model pin in models.yaml beats both.
    """
    candidates = [spec.region]
    if spec.provider == "anthropic":
        candidates.append(os.environ.get("CLAUDE_REGION"))
    candidates.append(os.environ.get("REGION"))
    return next((c for c in candidates if c), "unset")


def prompt_float(
    label: str,
    unit: str = TOKEN_UNIT,
    *,
    identity: str = UNRESOLVED_ID,
    section: str | None = None,
) -> float | None:
    """Ask for one price. Blank keeps VERIFY. Loops until valid.

    The whole block below is the prompt string, so the three facts that decide
    whether a typed number is the right number — concrete model ID, page
    section, unit — are on screen at the moment of typing rather than scrolled
    off above.
    """
    prompt = (
        f"\n  {label}\n"
        f"      model ID : {identity}\n"
        f"      section  : {section or UNCITED}\n"
        f"      unit     : {unit}\n"
        f"    > "
    )
    while True:
        raw = input(prompt).strip()
        if not raw:
            return None
        try:
            value = float(raw)
        except ValueError:
            print("    not a number, try again")
            continue
        if value < 0:
            print("    must be >= 0")
            continue
        return value


def _load_env() -> None:
    """PROJECT_ID / REGION / CLAUDE_PATH from .env, for the ID lines.

    Called from ``main`` rather than at import: ``tests/test_refresh_pricing``
    exec's this module to reach ``apply_updates``, and loading .env at import
    time put CLAUDE_REGION into the whole pytest process, which quietly changed
    what the Claude adapter's env tests were testing.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - dotenv ships in requirements.txt
        return
    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)


def main(argv: list[str] | None = None) -> int:
    _load_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--file",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "config" / "pricing.yaml",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    path: Path = args.file
    text = path.read_text(encoding="utf-8")
    pricing = PricingConfig.model_validate(yaml.safe_load(text))

    print(f"\nRefreshing {path}")
    print("Read the current prices from:")
    for url in pricing.sources:
        print(f"  - {url}")
    if pricing.verified_on:
        print(f"\nLast verified {pricing.verified_on} by {pricing.verified_by}.")
    print("\nEvery prompt names the concrete provider model ID the slot prices,")
    print("the section of the page above to read it from, and the unit. Token")
    print("prices are USD per 1,000,000 tokens; cache storage is rent, per 1M")
    print("cached tokens per hour. A rate quoted per 1k characters or per 1k")
    print("tokens must be converted before it goes in — leave VERIFY if unsure.")

    models = load_models(path)

    updates: dict[str, float] = {}
    for model_key, prices in pricing.models.items():
        identity = describe_model(model_key, models)
        section = pricing.page_section(model_key)
        printed_header = False
        for field in PRICE_FIELDS:
            if getattr(prices, field) != VERIFY:
                continue
            if not printed_header:
                print(f"\n{'-' * 72}\n{model_key}:")
                printed_header = True
            value = prompt_float(field, identity=identity, section=section)
            if value is not None:
                updates[f"models.{model_key}.{field}"] = value

    if pricing.cache_storage.per_1m_token_hour == VERIFY:
        print(f"\n{'-' * 72}\ncache_storage:")
        value = prompt_float(
            "per_1m_token_hour",
            CACHE_STORAGE_UNIT,
            # Not a model rate: one shared retention price covers every cached
            # model in the file, so there is no single ID to resolve.
            identity="— storage rent, shared by every cached model above",
            section=pricing.page_section(CACHE_STORAGE_SECTION_KEY),
        )
        if value is not None:
            updates["cache_storage.per_1m_token_hour"] = value

    if not updates:
        print("\nNothing to update.")
        return 0

    still_unverified = [k for k in pricing.unverified_keys() if k not in updates]
    if still_unverified:
        print(
            "\nWARNING: leaving these unverified, so the file will NOT be "
            "stamped as verified:"
        )
        for key in still_unverified:
            print(f"  - {key}")
        verified_on = verified_by = None
    else:
        operator = ""
        while not operator:
            operator = input("\nYour name (recorded as verified_by): ").strip()
        verified_on = date.today()
        verified_by = operator

    new_text = apply_updates(text, updates, verified_on, verified_by)

    # Never write something we cannot read back.
    PricingConfig.model_validate(yaml.safe_load(new_text))

    if args.dry_run:
        sys.stdout.write(new_text)
        return 0

    path.write_text(new_text, encoding="utf-8")
    print(f"\nWrote {path} ({len(updates)} price(s) updated).")
    if verified_on is None:
        print("Left verified_on: null — prices are still incomplete.")
    else:
        print(f"Stamped verified_on: {verified_on} by {verified_by}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
