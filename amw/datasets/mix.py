"""Difficulty mix and the stratified core split.

The dataset is not a uniform pile of questions. It is deliberately weighted
40/25/20/15 across simple / multi-hop / extraction / edge (TASKS.md T06),
because the interesting migration signal is concentrated in the last two
buckets: a model that keeps up on the easy 40% and falls apart on edge cases
would look fine under an unweighted average and be a bad migration.

Two properties matter enough to live in their own module and be tested
directly:

* **The mix holds at any n.** Largest-remainder allocation, so 70 items split
  28/18/14/10 and 10 items split 4/3/2/1 — never 4/2/2/1 losing an item to
  rounding, and never a bucket silently emptied at small n.
* **The core split preserves the mix.** The judged core set is the expensive
  part of every run (judge calls x k repeats), so it is a stratified sample,
  not the first N items. If the core were the head of the file it would be all
  ``simple`` and the judged score would be measured on the easy half.
"""

from __future__ import annotations

import random
from typing import Iterable, Sequence, TypeVar

from amw.datasets.schema import DIFFICULTIES, Difficulty

__all__ = ["MIX", "allocate", "difficulty_sequence", "stratified_sample", "mix_of"]

T = TypeVar("T")

#: Target proportions, in DIFFICULTIES order. Sums to 1.0.
MIX: dict[Difficulty, float] = {
    "simple": 0.40,
    "multi_hop": 0.25,
    "extraction": 0.20,
    "edge": 0.15,
}


def allocate(n: int, weights: dict[Difficulty, float] | None = None) -> dict[Difficulty, int]:
    """Split ``n`` items across the difficulty buckets, largest remainder.

    Deterministic and total: the returned counts always sum to exactly ``n``.
    Ties in the fractional part are broken by :data:`DIFFICULTIES` order, which
    favours the commoner buckets — at very small n it is better to lose an edge
    case than to lose the bucket that carries the mix.
    """
    if n < 0:
        raise ValueError(f"n must be non-negative, got {n}")
    weights = weights or MIX
    if n == 0:
        return {d: 0 for d in DIFFICULTIES}

    exact = {d: n * weights[d] for d in DIFFICULTIES}
    counts = {d: int(exact[d]) for d in DIFFICULTIES}
    short = n - sum(counts.values())
    # Hand the leftovers to the largest fractional parts.
    order = sorted(
        DIFFICULTIES,
        key=lambda d: (-(exact[d] - counts[d]), DIFFICULTIES.index(d)),
    )
    for d in order[:short]:
        counts[d] += 1
    return counts


def difficulty_sequence(n: int, rng: random.Random) -> list[Difficulty]:
    """The difficulty of item 0..n-1, shuffled but exactly on-mix.

    Shuffled so that a partial run (``-n 10`` against a 70-item plan, a truncated
    file, a head-of-file eyeball) still sees a spread rather than 28 consecutive
    ``simple`` items. Exactness comes from :func:`allocate`; the shuffle only
    reorders.
    """
    counts = allocate(n)
    sequence: list[Difficulty] = []
    for difficulty in DIFFICULTIES:
        sequence.extend([difficulty] * counts[difficulty])
    rng.shuffle(sequence)
    return sequence


def mix_of(difficulties: Iterable[Difficulty]) -> dict[Difficulty, float]:
    """Observed proportions, for the ±10% assertion in the tests."""
    seen = list(difficulties)
    total = len(seen)
    if not total:
        return {d: 0.0 for d in DIFFICULTIES}
    return {d: seen.count(d) / total for d in DIFFICULTIES}


def stratified_sample(
    items: Sequence[T],
    k: int,
    *,
    key,
    rng: random.Random,
) -> list[T]:
    """Pick ``k`` of ``items`` keeping the ``key`` distribution, deterministically.

    Returns them in original order, so the core split reads as a subset of the
    file rather than a reshuffle of it. Raises if ``k`` exceeds the population —
    a core set larger than the dataset is a config error, and silently
    returning fewer would understate the judged sample size in the report.
    """
    if k < 0:
        raise ValueError(f"k must be non-negative, got {k}")
    if k > len(items):
        raise ValueError(
            f"cannot draw a core split of {k} from {len(items)} items; "
            "lower dataset.judged_core_set or raise cases_per_subagent"
        )

    buckets: dict[object, list[int]] = {}
    for index, item in enumerate(items):
        buckets.setdefault(key(item), []).append(index)

    # Proportional quota per bucket, largest remainder again so the total is
    # exactly k and no bucket is rounded out of existence.
    exact = {b: k * len(idx) / len(items) for b, idx in buckets.items()}
    quota = {b: int(v) for b, v in exact.items()}
    short = k - sum(quota.values())
    order = sorted(buckets, key=lambda b: (-(exact[b] - quota[b]), str(b)))
    for b in order[:short]:
        quota[b] += 1

    chosen: list[int] = []
    for bucket in sorted(buckets, key=str):
        indices = buckets[bucket]
        take = min(quota[bucket], len(indices))
        chosen.extend(rng.sample(indices, take))
    return [items[i] for i in sorted(chosen)]
