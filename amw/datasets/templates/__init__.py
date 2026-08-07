"""Template registry: subagent -> the templates that fill each difficulty bucket.

Registration is explicit rather than discovered by import scanning, so adding a
template is a visible one-line diff and a bucket can never be silently emptied
by a rename. :func:`check_registry` is called by the tests: every subagent must
be able to fill every difficulty, otherwise the 40/25/20/15 mix cannot hold.
"""

from __future__ import annotations

import random
from typing import Sequence

from amw.agents.schemas import SUBAGENTS
from amw.datasets.schema import DIFFICULTIES, Difficulty
from amw.datasets.templates import chunk_summarizer, feature_extractor, query_rewriter
from amw.datasets.templates.common import Draft, SurfaceTarget, Template

__all__ = [
    "REGISTRY",
    "Draft",
    "SurfaceTarget",
    "Template",
    "templates_for",
    "shuffled_buckets",
    "check_registry",
]

REGISTRY: dict[str, tuple[Template, ...]] = {
    "query_rewriter": query_rewriter.TEMPLATES,
    "chunk_summarizer": chunk_summarizer.TEMPLATES,
    "feature_extractor": feature_extractor.TEMPLATES,
}


def templates_for(subagent: str, difficulty: Difficulty | None = None) -> list[Template]:
    try:
        templates = REGISTRY[subagent]
    except KeyError:
        raise KeyError(
            f"no templates registered for subagent {subagent!r}; "
            f"known: {sorted(REGISTRY)}"
        ) from None
    if difficulty is None:
        return list(templates)
    return [t for t in templates if t.difficulty == difficulty]


def shuffled_buckets(
    subagent: str, rng_for: "callable"
) -> dict[Difficulty, list[Template]]:
    """Per-difficulty template order for one generation run.

    Shuffled per (seed, subagent, difficulty) and then walked round-robin by the
    generator, which spreads the templates evenly instead of letting
    ``rng.choice`` pile six items onto one template and none onto another.
    """
    buckets: dict[Difficulty, list[Template]] = {}
    for difficulty in DIFFICULTIES:
        options = templates_for(subagent, difficulty)
        rng: random.Random = rng_for(difficulty)
        rng.shuffle(options)
        buckets[difficulty] = options
    return buckets


def check_registry(subagents: Sequence[str] = SUBAGENTS) -> None:
    """Raise if any subagent cannot fill any difficulty bucket, or ids collide."""
    seen: set[str] = set()
    for subagent in subagents:
        templates = templates_for(subagent)
        for template in templates:
            if template.template_id in seen:
                raise ValueError(f"duplicate template_id {template.template_id!r}")
            seen.add(template.template_id)
        missing = [d for d in DIFFICULTIES if not templates_for(subagent, d)]
        if missing:
            raise ValueError(
                f"subagent {subagent!r} has no templates for difficulty "
                f"{missing}; the 40/25/20/15 mix cannot be produced"
            )
