"""T06 — the synthetic dataset generator.

Four properties are worth a test each, because each one is a way the dataset
could be quietly wrong in front of a customer:

* **Schema validity.** Every item's gold answer is an instance of the frozen
  contract in ``amw/agents/schemas.py``, every rubric has 3-5 criteria, and no
  gold cites a chunk that was never supplied. The last one matters most: a gold
  answer with an invented citation would teach the citation-coverage metric to
  accept invented citations.
* **The difficulty mix.** 40/25/20/15 within ±10 percentage points, at the real
  n and at small n, and preserved by the stratified core split.
* **Seed determinism.** Same seed and same n give byte-identical files —
  checked in a *separate process*, because the interesting failure mode is a
  dependence on ``PYTHONHASHSEED`` or dict ordering that a same-process
  comparison cannot see.
* **Zero credentials.** ``--mode replay`` with the environment stripped bare
  still produces a complete, valid dataset (CLAUDE.md ground rule 4). The
  realism pass degrades to template prose and says so.

Nothing here asserts anything about model quality, and no test needs the
network.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from amw.agents.schemas import SUBAGENTS, schema_model
from amw.config import AppConfig, ConfigError, load_all
from amw.datasets.generator import (
    GENERATOR_VERSION,
    ITEM_PREFIXES,
    generate,
    generate_items,
)
from amw.datasets.mix import MIX, allocate, mix_of, stratified_sample
from amw.datasets.schema import DIFFICULTIES, DatasetItem, read_items
from amw.datasets.templates import REGISTRY, check_registry
from amw.datasets.templates.common import SurfaceTarget, check_surface, unit_glyphs
from amw.traces.store import ReplayStore

#: The card's tolerance: proportions within ten percentage points of target.
MIX_TOLERANCE = 0.10

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def cfg() -> AppConfig:
    return load_all()


@pytest.fixture(scope="module")
def small(cfg: AppConfig):
    """A fast, model-free run. Enough items for every template to appear."""
    return generate(config=cfg, n=24, write=False, naturalise=False)


# --------------------------------------------------------------------------
# schema
# --------------------------------------------------------------------------


def test_every_item_validates_against_the_frozen_contract(small) -> None:
    for subagent, result in small.subagents.items():
        model = schema_model(subagent)
        for item in result.items:
            # Constructing DatasetItem already validates, but the assertion
            # that matters is that gold is a *contract instance*, not a dict
            # that happens to parse.
            assert isinstance(item.gold_model(), model)


def test_provenance_seed_and_version_on_every_item(small, cfg: AppConfig) -> None:
    for item in small.all_items():
        assert item.provenance == "synthetic"
        assert item.seed == cfg.customer.seed
        assert item.generator_version == GENERATOR_VERSION
        assert item.customer == cfg.customer.customer
        assert item.domain == cfg.customer.domain


def test_rubrics_are_three_to_five_unique_pass_fail_criteria(small) -> None:
    for item in small.all_items():
        assert 3 <= len(item.rubric) <= 5, item.item_id
        ids = [c.id for c in item.rubric]
        assert len(set(ids)) == len(ids), item.item_id
        for criterion in item.rubric:
            # Criteria are a yes/no question, optionally followed by a sentence
            # of rationale for the judge. The question mark is the part that
            # makes it answerable pass/fail.
            assert "?" in criterion.criterion, (
                f"{item.item_id}/{criterion.id} is not phrased as a question, so "
                "it cannot be answered pass/fail"
            )


def test_item_ids_are_unique_and_prefixed(small) -> None:
    for subagent, result in small.subagents.items():
        prefix = ITEM_PREFIXES[subagent]
        ids = [item.item_id for item in result.items]
        assert len(set(ids)) == len(ids)
        assert all(i.startswith(f"{prefix}-") for i in ids)


def test_gold_never_cites_an_unsupplied_chunk(small) -> None:
    for item in small.all_items():
        supplied = set(item.input.chunk_ids)
        for point in item.gold.get("key_points") or []:
            assert set(point["chunk_ids"]) <= supplied, item.item_id
            assert point["chunk_ids"], f"{item.item_id}: uncited key point"


def test_chunk_summarizer_items_actually_carry_chunks(small) -> None:
    for item in small.subagents["chunk_summarizer"].items:
        assert len(item.input.chunks) >= 2, item.item_id
        assert item.input.context_chunks()[0].startswith(
            f"[{item.input.chunks[0].chunk_id}] "
        )


def test_gold_is_rejected_when_it_cites_an_invented_chunk(small) -> None:
    """The schema guard is load-bearing; prove it actually fires."""
    item = next(i for i in small.subagents["chunk_summarizer"].items if i.input.chunks)
    gold = json.loads(json.dumps(item.gold))
    gold["key_points"][0]["chunk_ids"] = ["US0000000B2::desc::p0001"]
    with pytest.raises(ValueError, match="not supplied"):
        DatasetItem(**{**item.model_dump(), "gold": gold})


# --------------------------------------------------------------------------
# the mix
# --------------------------------------------------------------------------


@pytest.mark.parametrize("n", [10, 24, 70])
def test_difficulty_mix_within_ten_points(cfg: AppConfig, n: int) -> None:
    result = generate(config=cfg, n=n, write=False, naturalise=False)
    for subagent, sub in result.subagents.items():
        observed = mix_of(item.difficulty for item in sub.items)
        for difficulty, target in MIX.items():
            assert abs(observed[difficulty] - target) <= MIX_TOLERANCE, (
                f"{subagent} n={n}: {difficulty} is {observed[difficulty]:.0%}, "
                f"target {target:.0%}"
            )


def test_allocation_is_exact_and_loses_no_item() -> None:
    for n in range(1, 120):
        counts = allocate(n)
        assert sum(counts.values()) == n
    assert allocate(70) == {
        "simple": 28,
        "multi_hop": 18,
        "extraction": 14,
        "edge": 10,
    }


def test_every_registered_template_is_used_at_the_real_n(cfg: AppConfig) -> None:
    result = generate(config=cfg, n=70, write=False, naturalise=False)
    for subagent, sub in result.subagents.items():
        used = set(sub.templates_used)
        registered = {t.template_id for t in REGISTRY[subagent]}
        assert used == registered, f"{subagent}: unused templates {registered - used}"


def test_registry_covers_every_difficulty_for_every_subagent() -> None:
    check_registry()  # raises if a bucket is empty
    for subagent in SUBAGENTS:
        buckets = {t.difficulty for t in REGISTRY[subagent]}
        assert buckets == set(DIFFICULTIES), subagent


# --------------------------------------------------------------------------
# the core split
# --------------------------------------------------------------------------


def test_core_split_is_sized_from_config_not_a_literal(cfg: AppConfig) -> None:
    result = generate(config=cfg, n=70, write=False, naturalise=False)
    assert result.core_size == cfg.customer.dataset.judged_core_set
    for sub in result.subagents.values():
        assert len(sub.core) == cfg.customer.dataset.judged_core_set
        assert all(item.core for item in sub.core)
        assert sum(1 for item in sub.items if item.core) == len(sub.core)


def test_core_split_is_clamped_to_a_short_run(cfg: AppConfig) -> None:
    """`-n 10` with judged_core_set=28 must not ask for 28 of 10 items."""
    result = generate(config=cfg, n=10, write=False, naturalise=False)
    assert result.core_size == 10
    for sub in result.subagents.values():
        assert len(sub.core) == 10


def test_core_split_preserves_the_difficulty_mix(cfg: AppConfig) -> None:
    result = generate(config=cfg, n=70, write=False, naturalise=False)
    for subagent, sub in result.subagents.items():
        observed = mix_of(item.difficulty for item in sub.core)
        for difficulty, target in MIX.items():
            assert abs(observed[difficulty] - target) <= MIX_TOLERANCE, (
                f"{subagent} core: {difficulty} is {observed[difficulty]:.0%}"
            )


def test_stratified_sample_refuses_to_overdraw() -> None:
    import random

    with pytest.raises(ValueError, match="cannot draw a core split"):
        stratified_sample([1, 2, 3], 4, key=lambda x: "a", rng=random.Random(0))


# --------------------------------------------------------------------------
# determinism
# --------------------------------------------------------------------------


_SUBPROCESS = """
import json, sys
from amw.datasets.generator import generate
result = generate(n=5, mode="replay", write=False, naturalise=False)
sys.stdout.write("".join(i.to_jsonl_line() for i in result.all_items()))
"""


def _generate_in_subprocess(hash_seed: str) -> str:
    proc = subprocess.run(
        [sys.executable, "-c", _SUBPROCESS],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHONHASHSEED": hash_seed,
            "PYTHONPATH": str(REPO_ROOT),
        },
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_same_seed_same_n_is_byte_identical_across_processes() -> None:
    """The claim in the module docstring, tested the only way that proves it.

    Two runs in one process share dict ordering and a warm import cache. A
    dependence on hash randomisation only shows up across processes with
    different PYTHONHASHSEED values, and that is exactly the bug that would
    reshuffle the corpus underneath a recorded trace store on freeze day.
    """
    first = _generate_in_subprocess("0")
    second = _generate_in_subprocess("12345")
    assert first == second
    assert first.count("\n") == 15  # 5 items x 3 subagents


def test_regeneration_in_process_is_byte_identical(cfg: AppConfig) -> None:
    a = generate(config=cfg, n=5, write=False, naturalise=False)
    b = generate(config=cfg, n=5, write=False, naturalise=False)
    dump = lambda r: "".join(i.to_jsonl_line() for i in r.all_items())  # noqa: E731
    assert dump(a) == dump(b)


def test_a_different_seed_produces_a_different_corpus(cfg: AppConfig) -> None:
    other = cfg.model_copy(
        update={"customer": cfg.customer.model_copy(update={"seed": cfg.customer.seed + 1})}
    )
    a = generate(config=cfg, n=10, write=False, naturalise=False)
    b = generate(config=other, n=10, write=False, naturalise=False)
    dump = lambda r: "".join(i.to_jsonl_line() for i in r.all_items())  # noqa: E731
    assert dump(a) != dump(b)


def test_no_timestamp_leaks_into_an_item(small) -> None:
    """A generation timestamp would break byte-identical regeneration."""
    for item in small.all_items():
        assert "generated_at" not in item.model_dump()


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------


def test_writes_dataset_and_core_files_that_round_trip(cfg: AppConfig, tmp_path: Path):
    result = generate(config=cfg, n=12, out_dir=tmp_path, naturalise=False)
    assert result.output_dir == tmp_path
    for subagent, sub in result.subagents.items():
        path = tmp_path / f"{subagent}.jsonl"
        core_path = tmp_path / f"{subagent}.core.jsonl"
        assert path.exists() and core_path.exists()
        reloaded = read_items(path)
        assert [i.item_id for i in reloaded] == [i.item_id for i in sub.items]
        assert reloaded == sub.items
        assert all(i.core for i in read_items(core_path))
    # Rewriting the same run reproduces the same bytes.
    before = (tmp_path / "query_rewriter.jsonl").read_bytes()
    generate(config=cfg, n=12, out_dir=tmp_path, naturalise=False)
    assert (tmp_path / "query_rewriter.jsonl").read_bytes() == before


def test_generate_rejects_a_nonsense_n(cfg: AppConfig) -> None:
    with pytest.raises(ConfigError, match="n must be positive"):
        generate(config=cfg, n=0, write=False, naturalise=False)


def test_generate_rejects_an_unknown_subagent(cfg: AppConfig) -> None:
    with pytest.raises(ConfigError):
        generate_items(
            "summariser", 2, seed=1, customer="demo_patents", domain="patents"
        )


# --------------------------------------------------------------------------
# zero credentials
# --------------------------------------------------------------------------


def test_replay_mode_needs_no_credentials(cfg: AppConfig, monkeypatch, tmp_path: Path):
    """Ground rule 4, for this lane: a complete dataset on a bare machine.

    The realism pass runs, finds nothing recorded for whatever it asks (the
    store is redirected at an empty directory), and falls back to template
    prose rather than raising. Items are still schema-valid and complete.
    """
    for var in ("PROJECT_ID", "REGION", "GOOGLE_APPLICATION_CREDENTIALS"):
        monkeypatch.delenv(var, raising=False)

    result = generate(
        config=cfg,
        n=5,
        mode="replay",
        write=False,
        store=ReplayStore(tmp_path / "empty"),
    )
    assert len(result.all_items()) == 5 * len(cfg.customer.evaluated_subagents)
    assert all(i.surface_source == "template" for i in result.all_items())
    assert result.rewrite.accepted == 0
    assert result.rewrite.attempted > 0, "no item offered any prose to naturalise"


def test_naturalise_false_makes_no_model_call(cfg: AppConfig) -> None:
    result = generate(config=cfg, n=5, write=False, naturalise=False)
    assert result.rewrite.attempted == 0
    assert all(i.surface_source == "template" for i in result.all_items())


# --------------------------------------------------------------------------
# the realism-pass guard
# --------------------------------------------------------------------------


def test_surface_guard_rejects_a_dropped_fact() -> None:
    target = SurfaceTarget(kind="message", index=0, style="x", must_keep=("H01M4/386",))
    assert check_surface("anything in H01M4/386 please", target) is None
    assert "dropped required literal" in check_surface("anything please", target)


def test_surface_guard_rejects_an_invented_date() -> None:
    """The edge cases exist *because* something is missing. Guard the absence."""
    target = SurfaceTarget(
        kind="message",
        index=0,
        style="x",
        forbid=(r"\b(?:19|20)\d{2}\b",),
    )
    assert check_surface("anything from the last couple of years", target) is None
    assert "forbidden content" in check_surface("anything since 2023", target)


def test_surface_guard_rejects_a_changed_number() -> None:
    """Numbers are what the gold is pinned to. Neither direction is allowed."""
    target = SurfaceTarget(kind="message", index=0, style="x")
    before = "Example 1 gave a discharge capacity of 158 mAh/g."
    assert check_surface("A capacity of 158 mAh/g is had by Example 1.", target, before) is None
    assert "dropped number" in check_surface("Example 1 gave 160 mAh/g.", target, before)
    assert "introduced number" in check_surface(
        "Example 1 gave 158 mAh/g in 2019.", target, before
    )


def test_surface_guard_rejects_a_respelled_unit() -> None:
    """The rewrite that motivated this turned "30 um" into "30 µm".

    Cosmetically better, factually identical, and still a defect: the gold key
    point says "30 um", so accepting it leaves the answer key and the passage
    disagreeing about how to spell a unit the rubric wants reproduced verbatim.
    """
    target = SurfaceTarget(kind="message", index=0, style="x")
    before = "dies of less than 30 um, measured at 25 degrees C"
    assert check_surface("30 um dies, measured at 25 degrees C", target, before) is None
    assert "re-spelled a unit" in check_surface(
        "dies of less than 30 µm, measured at 25 degrees C", target, before
    )
    assert "re-spelled a unit" in check_surface(
        "dies of less than 30 um, measured at 25° C", target, before
    )


def test_surface_guard_ignores_wording_changes() -> None:
    """The guard must stay blind to the rewriting it exists to permit."""
    target = SurfaceTarget(kind="message", index=0, style="x")
    before = "A significant challenge is the defect rate at commercial throughputs."
    assert (
        check_surface(
            "Defect rates at commercially viable throughputs remain a real problem.",
            target,
            before,
        )
        is None
    )


def test_no_sample_item_disagrees_with_its_gold_about_a_unit() -> None:
    """End-to-end version of the guard, over the corpus the reviewer reads."""
    result = generate(n=10, mode="replay", naturalise=False, write=False)
    for item in result.all_items():
        rendered = " ".join([*item.input.messages, *item.input.context_chunks()])
        gold = json.dumps(item.gold, ensure_ascii=False)
        assert unit_glyphs(rendered) == unit_glyphs(gold), (
            f"{item.item_id}: input and gold spell a unit differently"
        )


def test_every_edge_template_guards_its_rewritable_prose() -> None:
    """An edge item is defined by what its input does *not* say.

    If such an item exposes prose to the rewriter without pinning the facts the
    gold depends on or forbidding the thing that must stay absent, a helpful
    rewrite silently turns it into an ordinary item and nothing raises. So every
    surface target on an edge template must carry at least one guard.
    """
    import random

    for subagent, templates in REGISTRY.items():
        for template in templates:
            if template.difficulty != "edge":
                continue
            draft = template.fn(random.Random(0))
            for target in draft.surface:
                assert target.must_keep or target.forbid, (
                    f"{subagent}/{template.template_id}: surface target "
                    f"{target.kind}[{target.index}] is unguarded"
                )


def test_every_surface_target_points_at_prose_that_exists() -> None:
    """An out-of-range target only fails when the realism pass is switched on."""
    import random

    for subagent, templates in REGISTRY.items():
        for template in templates:
            for seed in range(5):
                draft = template.fn(random.Random(seed))
                for target in draft.surface:
                    pool = draft.messages if target.kind == "message" else draft.chunks
                    assert 0 <= target.index < len(pool), (
                        f"{subagent}/{template.template_id}: {target.kind} target "
                        f"{target.index} is out of range"
                    )
