"""Synthetic dataset generator — patents domain, seeded, per-subagent.

This is the entry point ``cli.py gen`` wires to::

    from amw.datasets.generator import generate
    result = generate(customer="demo_patents", n=70, mode="hybrid")

What it produces, per subagent: ``datasets/{subagent}.jsonl`` (the full set) and
``datasets/{subagent}.core.jsonl`` (the stratified judged split, sized from
``config/customers/<customer>.yaml: dataset.judged_core_set`` — not a literal).

Three properties are load-bearing.

**Determinism is real, not aspirational.** Same seed and same n give
byte-identical files. Every random draw comes from a :class:`random.Random`
seeded on ``(seed, subagent, index)``; there is no clock, no global RNG, no
``PYTHONHASHSEED`` dependence, and no timestamp in the output. Re-running the
generator on freeze day must not silently reshuffle the corpus underneath the
recorded traces.

**Provenance is on every item.** ``provenance: synthetic`` plus the seed plus
the template id plus a generator version, on every line (CLAUDE.md ground rule
2). A customer can point at any row of any report and get an answer about where
it came from.

**Zero credentials still works.** The Gemini realism pass
(``amw/datasets/surface.py``) is the only part that touches a model, and it
degrades to the template prose on a replay miss. ``--mode replay`` on a laptop
with no ADC produces a complete, schema-valid dataset; the run report says how
much prose was naturalised rather than pretending it all was.

Nothing here fabricates a measurement. Synthetic *inputs* are the product and
are labelled as such; there is not a metric value, score or latency anywhere in
the output.
"""

from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from amw.agents.schemas import SUBAGENTS
from amw.config import AppConfig, ConfigError, load_all
from amw.datasets.mix import allocate, difficulty_sequence, mix_of, stratified_sample
from amw.datasets.schema import DatasetItem, Difficulty, write_items
from amw.datasets.surface import RewriteStats, SurfaceRewriter
from amw.datasets.patents_bank import TECHS
from amw.datasets.templates import Draft, check_registry, shuffled_buckets
from amw.datasets.templates.common import tech_rotation

__all__ = [
    "GENERATOR_VERSION",
    "ITEM_PREFIXES",
    "SubagentResult",
    "GenerationResult",
    "generate",
    "generate_items",
    "default_output_dir",
]

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: Bump when template *semantics* change, so a stale corpus is detectable by
#: diffing this field rather than by noticing odd numbers three days later.
GENERATOR_VERSION = "t06.1"

#: Short, stable item-id prefixes. They show up in trace ids and failure
#: clusters, so they are worth being readable.
ITEM_PREFIXES: dict[str, str] = {
    "query_rewriter": "qr",
    "chunk_summarizer": "cs",
    "feature_extractor": "fe",
}

#: Preferred model role for the realism pass, then the fallback. A dedicated
#: ``dataset_generator`` role keeps the generator off the model under test; the
#: fallback means this works today, before that role exists in models.yaml.
GENERATOR_ROLES: tuple[str, ...] = ("dataset_generator", "gemini_candidate_alt")


def default_output_dir() -> Path:
    """``<repo>/datasets`` — the path TASKS.md T06 specifies."""
    return REPO_ROOT / "datasets"


def _rng(*parts: object) -> random.Random:
    """A stream seeded on a stable string.

    ``random.Random`` hashes a string seed reproducibly across processes and
    interpreters (unlike ``hash()``), which is exactly what byte-identical
    regeneration needs.
    """
    return random.Random("|".join(str(p) for p in parts))


# --------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------


@dataclass
class SubagentResult:
    subagent: str
    items: list[DatasetItem]
    core: list[DatasetItem]
    path: Path | None = None
    core_path: Path | None = None

    @property
    def mix(self) -> dict[Difficulty, float]:
        return mix_of(item.difficulty for item in self.items)

    @property
    def templates_used(self) -> dict[str, int]:
        return dict(Counter(item.template_id for item in self.items))

    def describe(self) -> str:
        mix = ", ".join(f"{d} {p:.0%}" for d, p in self.mix.items())
        return (
            f"{self.subagent}: {len(self.items)} items "
            f"({len(self.core)} core) — {mix}"
        )


@dataclass
class GenerationResult:
    customer: str
    seed: int
    n: int
    core_size: int
    subagents: dict[str, SubagentResult] = field(default_factory=dict)
    rewrite: RewriteStats = field(default_factory=RewriteStats)
    output_dir: Path | None = None

    def all_items(self) -> list[DatasetItem]:
        return [item for r in self.subagents.values() for item in r.items]

    def describe(self) -> str:
        lines = [
            f"customer={self.customer} seed={self.seed} n={self.n} "
            f"core={self.core_size} generator={GENERATOR_VERSION}",
            *(r.describe() for r in self.subagents.values()),
            f"realism pass: {self.rewrite.summary()}",
        ]
        if self.output_dir is not None:
            lines.append(f"written to {self.output_dir}")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# generation
# --------------------------------------------------------------------------


def _draft_sequence(subagent: str, n: int, seed: int) -> list[tuple[Difficulty, object]]:
    """The (difficulty, template) plan for one subagent, before any prose exists.

    Templates are walked round-robin within their bucket rather than sampled, so
    a 70-item set uses each template a near-equal number of times. Sampling
    would leave one template with nine items and another with one, which reads
    as a lopsided dataset to anyone browsing it.
    """
    order = difficulty_sequence(n, _rng(seed, subagent, "order"))
    buckets = shuffled_buckets(
        subagent, lambda difficulty: _rng(seed, subagent, "bucket", difficulty)
    )
    position: Counter = Counter()
    plan: list[tuple[Difficulty, object]] = []
    for difficulty in order:
        options = buckets[difficulty]
        template = options[position[difficulty] % len(options)]
        position[difficulty] += 1
        plan.append((difficulty, template))
    return plan


def _tech_rotation(subagent: str, n: int, seed: int) -> list:
    """One technology area per item, shuffled, cycling if ``n`` exceeds the bank.

    Templates pick their own scenario, and independent draws over fourteen areas
    collide often enough that a ten-item sample showed the same patent subject
    three times under three different applicants. Dealing the areas out in a
    shuffled rotation spreads them without making the corpus look like a
    checklist: each cycle is reshuffled, so an area can still recur.
    """
    rotation: list = []
    cycle = 0
    while len(rotation) < n:
        deck = list(TECHS)
        _rng(seed, subagent, "techs", cycle).shuffle(deck)
        rotation.extend(deck)
        cycle += 1
    return rotation[:n]


def generate_items(
    subagent: str,
    n: int,
    *,
    seed: int,
    customer: str,
    domain: str,
    rewriter: SurfaceRewriter | None = None,
) -> list[DatasetItem]:
    """Generate ``n`` items for one subagent. Pure apart from ``rewriter``."""
    if subagent not in SUBAGENTS:
        raise ConfigError(
            f"unknown subagent {subagent!r}; expected one of {list(SUBAGENTS)}"
        )
    prefix = ITEM_PREFIXES[subagent]
    items: list[DatasetItem] = []

    rotation = _tech_rotation(subagent, n, seed)
    for index, (difficulty, template) in enumerate(_draft_sequence(subagent, n, seed)):
        rng = _rng(seed, subagent, index, template.template_id)
        # One area is dealt to this item; anything else a template needs (a
        # neighbouring field for a distractor, say) still comes from its rng.
        with tech_rotation([rotation[index]]):
            draft: Draft = template.fn(rng)
        if draft.difficulty != difficulty:  # pragma: no cover - registry guards this
            raise ValueError(
                f"template {template.template_id!r} is registered as "
                f"{difficulty!r} but produced {draft.difficulty!r}"
            )

        surface_source = "template"
        if rewriter is not None and draft.surface:
            if rewriter.apply(subagent, draft):
                surface_source = "gemini"

        items.append(
            DatasetItem(
                item_id=f"{prefix}-{index:04d}",
                subagent=subagent,
                customer=customer,
                domain=domain,
                provenance="synthetic",
                seed=seed,
                generator_version=GENERATOR_VERSION,
                difficulty=draft.difficulty,
                template_id=draft.template_id,
                input={"messages": draft.messages, "chunks": draft.chunks},
                gold=draft.gold.model_dump(),
                rubric=draft.rubric,
                surface_source=surface_source,
            )
        )
    return items


def _mark_core(items: list[DatasetItem], k: int, *, seed: int, subagent: str):
    """Stratified core split. Returns the chosen items; sets ``core`` on them.

    The core set is what the judge runs on (k repeats x every prompt variant),
    so it is the expensive half of every run. Stratifying it keeps the judged
    score measuring the same difficulty mix as the deterministic metrics — a
    core set taken off the top of the file would be judged almost entirely on
    ``simple`` items and would flatter both models equally but meaninglessly.
    """
    chosen = stratified_sample(
        list(items),
        k,
        key=lambda item: item.difficulty,
        rng=_rng(seed, subagent, "core"),
    )
    chosen_ids = {item.item_id for item in chosen}
    marked: list[DatasetItem] = []
    for index, item in enumerate(items):
        if item.item_id in chosen_ids:
            items[index] = item.model_copy(update={"core": True})
            marked.append(items[index])
    return marked


def _resolve_generator_model(config: AppConfig) -> str:
    for role in GENERATOR_ROLES:
        try:
            key, _ = config.models.for_role(role)
        except ConfigError:
            continue
        return key
    raise ConfigError(
        f"config/models.yaml declares none of the roles {list(GENERATOR_ROLES)}; "
        "the dataset generator has no model to call. Add a "
        "`dataset_generator` role, or run with naturalise=False."
    )


def generate(
    *,
    customer: str | None = None,
    n: int | None = None,
    mode: str = "replay",
    config: AppConfig | None = None,
    subagents: Sequence[str] | None = None,
    out_dir: str | Path | None = None,
    write: bool = True,
    naturalise: bool = True,
    model: str | None = None,
    on_miss: str = "fallback",
    store: object | None = None,
) -> GenerationResult:
    """Generate (and by default write) the synthetic dataset.

    :param customer: profile stem under ``config/customers/``. Defaults to
        ``$AMW_CUSTOMER`` then ``demo_patents``, via :func:`amw.config.load_all`.
    :param n: items per subagent. Defaults to the profile's
        ``dataset.cases_per_subagent``.
    :param mode: ``live`` | ``replay`` | ``hybrid``, passed straight to
        ``amw.adapters.resolve``. Only the realism pass uses it.
    :param config: pre-loaded config, for tests and for callers that already
        have one.
    :param subagents: restrict to a subset; defaults to the customer profile's
        evaluated subagents.
    :param out_dir: defaults to ``<repo>/datasets``.
    :param write: set False to generate without touching the filesystem.
    :param naturalise: set False to skip the Gemini pass entirely — pure
        templates, no model calls, no adapter import.
    :param model: logical model key for the realism pass; defaults to the first
        available role in :data:`GENERATOR_ROLES`.
    :param on_miss: ``"fallback"`` (default) or ``"raise"`` on a replay miss.
    :param store: replay store override, passed through to ``resolve``. Lets a
        test point the realism pass at an empty corpus and prove the
        zero-credential fallback, without touching ``artifacts/replay/``.
    """
    check_registry()
    cfg = config if config is not None else load_all(customer=customer)
    profile = cfg.customer

    count = n if n is not None else profile.dataset.cases_per_subagent
    if count <= 0:
        raise ConfigError(f"n must be positive, got {count}")

    names = list(subagents) if subagents is not None else profile.evaluated_subagents
    unknown = [s for s in names if s not in SUBAGENTS]
    if unknown:
        raise ConfigError(
            f"customer {profile.customer!r} enables subagent(s) {unknown} that "
            f"have no output schema in amw/agents/schemas.py "
            f"(known: {list(SUBAGENTS)})"
        )

    # The core split cannot exceed the run's n. The profile guarantees
    # judged_core_set <= cases_per_subagent, but `-n 10` is a smaller run.
    core_size = min(profile.dataset.judged_core_set, count)

    rewriter: SurfaceRewriter | None = None
    if naturalise:
        # Imported here so `naturalise=False` needs no adapter stack at all.
        from amw.adapters import resolve

        model_key = model or _resolve_generator_model(cfg)
        adapter = resolve(model_key, mode, models=cfg.models, store=store)  # type: ignore[arg-type]
        rewriter = SurfaceRewriter(adapter, model=model_key, on_miss=on_miss)  # type: ignore[arg-type]

    result = GenerationResult(
        customer=profile.customer,
        seed=profile.seed,
        n=count,
        core_size=core_size,
        rewrite=rewriter.stats if rewriter is not None else RewriteStats(),
    )

    target_dir = Path(out_dir) if out_dir is not None else default_output_dir()
    for subagent in names:
        items = generate_items(
            subagent,
            count,
            seed=profile.seed,
            customer=profile.customer,
            domain=profile.domain,
            rewriter=rewriter,
        )
        core = _mark_core(items, core_size, seed=profile.seed, subagent=subagent)
        entry = SubagentResult(subagent=subagent, items=items, core=core)
        if write:
            entry.path = write_items(target_dir / f"{subagent}.jsonl", items)
            entry.core_path = write_items(
                target_dir / f"{subagent}.core.jsonl", core
            )
        result.subagents[subagent] = entry

    if write:
        result.output_dir = target_dir
    return result


def planned_mix(n: int) -> dict[Difficulty, int]:
    """Difficulty counts a run of ``n`` will produce. Exposed for the CLI banner."""
    return allocate(n)
