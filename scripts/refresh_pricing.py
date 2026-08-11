#!/usr/bin/env python3
"""Interactively fill in config/pricing.yaml.

Run this on freeze day, before any cost figure is shown to a customer:

    python scripts/refresh_pricing.py

It prompts once per price, shows the source URLs it expects you to be reading,
rewrites the file in place (comments preserved), and stamps ``verified_on`` +
``verified_by``.

It deliberately will NOT stamp the file while any price still reads VERIFY —
a half-verified table that claims to be verified is exactly the failure mode
the VERIFY sentinel exists to prevent.

    --dry-run   print the rewritten file to stdout instead of saving
    --file P    operate on a different pricing.yaml (used by the tests)
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from amw.config import VERIFY, PricingConfig  # noqa: E402

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


def prompt_float(label: str, unit: str = TOKEN_UNIT) -> float | None:
    """Ask for one price. Blank keeps VERIFY. Loops until valid."""
    while True:
        raw = input(f"  {label} ({unit}, blank = leave VERIFY): ").strip()
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


def main(argv: list[str] | None = None) -> int:
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
    print("\nEach prompt states its own unit. Token prices are USD per")
    print("1,000,000 tokens; cache storage is rent, per 1M cached tokens/hour.\n")

    updates: dict[str, float] = {}
    for model_key, prices in pricing.models.items():
        printed_header = False
        for field in PRICE_FIELDS:
            if getattr(prices, field) != VERIFY:
                continue
            if not printed_header:
                print(f"{model_key}:")
                printed_header = True
            value = prompt_float(field)
            if value is not None:
                updates[f"models.{model_key}.{field}"] = value

    if pricing.cache_storage.per_1m_token_hour == VERIFY:
        print("cache_storage:")
        value = prompt_float("per_1m_token_hour", CACHE_STORAGE_UNIT)
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
