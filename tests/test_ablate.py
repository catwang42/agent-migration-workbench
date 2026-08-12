"""T10 verify: the A0–A4 ablation ladder and the Feature Extractor 2×2.

Three things are worth pinning here, and they are all integrity properties
rather than behaviour properties:

1. **The ladder scores through phase-2's code.** A rung measured by a parallel
   eval path is not comparable to the phase-2 baseline it is printed next to.
   The tests assert the ladder's numbers *are* an ``ArmResult``.
2. **An unmeasured rung carries no numbers.** The FE novelty rungs need live
   calls nobody has recorded. In replay they must come back with a stated
   reason and nothing that could be read as a measurement (CLAUDE.md ground
   rule 1).
3. **The 2×2 varies two things and only two things.** If the "prompt only" cell
   also changed the output mode, the mode-confound diagnostic it exists to run
   would be confounded itself.

Everything here runs in ``--mode replay`` with zero credentials, against the
committed fixture corpus.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
from pathlib import Path

import pytest

import cli
from amw.agents import prompt_packs as pp
from amw.agents.schemas import SUBAGENTS
from amw.config import load_all
from amw.eval.runner import ArmResult
from amw.tuning import cmd_ablate
from amw.tuning.ablate import (
    ABLATION_VERSION,
    COMMON_RUNGS,
    CURRENT_GEN_MODEL,
    FEW_SHOT_ITEM_IDS,
    P1_RUNGS,
    SHIPPING_VARIANT,
    SUBAGENT_RUNGS,
    VAIPO_RUNG_ID,
    AblationResult,
    RungSpec,
    _current_gen_rungs,
    default_results_path,
    error_kinds,
    format_rung,
    ladder_for,
    register_p1_rung,
    run_ladder,
)

PROMPT_DIR = Path(pp.__file__).resolve().parent / "prompts" / "feature_extractor"

#: The four cells: (rung, prompt axis, mode axis).
GRID = [
    ("A0", "naive", "tool"),
    ("A0-schema", "naive", "response_schema"),
    ("A4-novelty-tool", "novelty", "tool"),
    ("A4-novelty-schema", "novelty", "response_schema"),
]


@pytest.fixture(scope="module")
def cfg():
    return load_all(customer="demo_patents")


def _lines(variant: str) -> list[str]:
    return (PROMPT_DIR / f"{variant}.txt").read_text(encoding="utf-8").splitlines()


def _changed_lines(before: str, after: str) -> list[str]:
    """The +/- lines of a unified diff, with the file headers dropped."""
    return [
        line
        for line in difflib.unified_diff(_lines(before), _lines(after), lineterm="", n=0)
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]


# --------------------------------------------------------------------------
# the ladder's shape
# --------------------------------------------------------------------------


@pytest.mark.parametrize("subagent", SUBAGENTS)
def test_every_subagent_has_the_three_common_rungs_first(subagent):
    rungs = ladder_for(subagent)
    assert [spec.rung for spec in rungs[:3]] == ["baseline", "A0", "A1-A3"]
    assert [spec.variant for spec in rungs[:3]] == list(pp.VARIANTS)


@pytest.mark.parametrize("subagent", SUBAGENTS)
def test_every_rung_names_a_variant_the_subagent_actually_has(subagent):
    available = set(pp.variants_for(subagent))
    for spec in ladder_for(subagent):
        assert spec.variant in available
        assert pp.load_pack(subagent, spec.variant).sha256


def test_rung_ids_are_unique_within_a_ladder():
    for subagent in SUBAGENTS:
        ids = [spec.rung for spec in ladder_for(subagent)]
        assert len(ids) == len(set(ids))


def test_extra_rungs_exist_only_where_a_measurement_asked_for_them():
    """Two subagents carry *hand-written* extra rungs, for two different
    reasons, and the third carries none. Chunk Summarizer is the control: no
    prompt work was added to it, so its ladder must be exactly the common three
    plus the current-generation pair every subagent gets."""
    assert set(SUBAGENT_RUNGS) == {"feature_extractor", "query_rewriter"}
    assert ladder_for("chunk_summarizer") == COMMON_RUNGS + _current_gen_rungs(
        "chunk_summarizer"
    )
    # Every extra rung is an extension of the common three, never a
    # replacement: a subagent-specific ladder still has to start with the
    # incumbent, A0 and A1-A3 or its rungs are not comparable to the others'.
    for subagent in SUBAGENTS:
        assert ladder_for(subagent)[: len(COMMON_RUNGS)] == COMMON_RUNGS


def test_every_subagent_gets_the_same_two_current_generation_rungs():
    """The current-generation pair is not subagent-specific work — it is the
    same question asked of all three (does the same prompt, unchanged, hold up
    on the model the workshop actually recommends?). So unlike SUBAGENT_RUNGS
    it is unconditional, it changes only the model, and it always sits at the
    end of the hand-tuned rungs so the prompt-work story reads in order."""
    for subagent in SUBAGENTS:
        rungs = ladder_for(subagent)
        current = [spec for spec in rungs if spec.model == CURRENT_GEN_MODEL]
        assert [spec.rung for spec in current] == ["A0-current", "ship-current"]
        # Same prompt bytes as rungs already measured on the old generation:
        # A0's variant, and the arm this subagent actually ships.
        assert current[0].variant == "gemini_naive"
        assert current[1].variant == SHIPPING_VARIANT[subagent]
        # Nothing else on the ladder names a model — every other rung takes the
        # model its variant's role resolves to.
        assert [spec for spec in rungs if spec.model is not None] == current
        assert rungs[-len(current) :] == tuple(current)


def test_the_novelty_rungs_branch_from_naive_not_from_the_tuned_bundle():
    """A1–A3 regressed on FE; a rung built on it would inherit the regression."""
    branches = {spec.rung: spec.branches_from for spec in ladder_for("feature_extractor")}
    assert branches["A0-schema"] == "A0"
    assert branches["A4-novelty-tool"] == "A0"
    # The "both" cell is the novelty prompt under the other mode.
    assert branches["A4-novelty-schema"] == "A4-novelty-tool"
    assert "A1-A3" not in {branches["A0-schema"], branches["A4-novelty-tool"]}


def test_a_rung_naming_a_variant_the_subagent_lacks_is_rejected(monkeypatch):
    monkeypatch.setitem(
        SUBAGENT_RUNGS,
        "query_rewriter",
        (RungSpec(rung="X", label="fixture", variant="gemini_novelty_v1_tool"),),
    )
    with pytest.raises(ValueError, match="no prompt pack for"):
        ladder_for("query_rewriter")


# --------------------------------------------------------------------------
# A4' (VAIPO) is P1 and must not be in this build
# --------------------------------------------------------------------------


def test_the_vaipo_rung_is_not_built():
    """P1, SPIKE-S3 gated (CLAUDE.md ground rule 6)."""
    assert P1_RUNGS == {}
    for subagent in SUBAGENTS:
        assert VAIPO_RUNG_ID not in {spec.rung for spec in ladder_for(subagent)}


def test_the_extension_point_exists_and_is_documented():
    from amw.tuning import ablate

    assert VAIPO_RUNG_ID in ablate.__doc__
    assert "SPIKE-S3" in ablate.__doc__
    assert "register_p1_rung" in ablate.__doc__


def test_register_p1_rung_appends_to_the_ladder():
    spec = RungSpec(
        rung="fixture-p1",
        label="fixture rung, registered by a test",
        variant="gemini_tuned_v1",
        branches_from="A0",
    )
    try:
        register_p1_rung("query_rewriter", spec)
        assert ladder_for("query_rewriter")[-1] is spec
    finally:
        P1_RUNGS.pop("query_rewriter", None)
    assert P1_RUNGS == {}


def test_register_p1_rung_rejects_an_unknown_subagent():
    with pytest.raises(ValueError, match="unknown subagent"):
        register_p1_rung("not_a_subagent", COMMON_RUNGS[0])


# --------------------------------------------------------------------------
# the Feature Extractor 2x2 — the prompt files themselves
# --------------------------------------------------------------------------


def test_the_grid_covers_all_four_cells():
    specs = {spec.rung: spec for spec in ladder_for("feature_extractor")}
    for rung, _prompt, mode in GRID:
        assert specs[rung].output_mode == mode
    assert len({spec.variant for rung, *_ in GRID for spec in [specs[rung]]}) == 4


@pytest.mark.parametrize("rung,prompt_axis,mode", GRID)
def test_each_cell_declares_exactly_one_output_mechanism(rung, prompt_axis, mode):
    """Tool XOR response_schema — the Gemini adapter refuses a request with both."""
    specs = {spec.rung: spec for spec in ladder_for("feature_extractor")}
    pack = pp.load_pack("feature_extractor", specs[rung].variant)
    assert pack.output_mode == mode
    assert (pack.tool_description is not None) == (mode == "tool")


def test_the_naive_arm_is_the_incumbent_prompt_byte_for_byte():
    """A0 is an endpoint swap. If the bytes differ it is not an endpoint swap."""
    baseline = pp.load_pack("feature_extractor", "claude_baseline")
    naive = pp.load_pack("feature_extractor", "gemini_naive")
    assert naive.text == baseline.text
    assert naive.sha256 == baseline.sha256


def test_the_novelty_prompt_is_the_naive_prompt_plus_one_inserted_block():
    """The prompt axis adds a rule. It must not also edit anything else."""
    naive = (PROMPT_DIR / "gemini_naive.txt").read_text(encoding="utf-8")
    novelty = (PROMPT_DIR / "gemini_novelty_v1_tool.txt").read_text(encoding="utf-8")

    block = re.search(
        r"<novelty_statement_rule>.*?</novelty_statement_rule>\n*", novelty, re.S
    )
    assert block is not None, "the added rule is not a single delimited block"
    assert novelty.replace(block.group(0), "", 1) == naive


def test_the_added_rule_says_what_the_owner_asked_it_to_say():
    novelty = (PROMPT_DIR / "gemini_novelty_v1_tool.txt").read_text(encoding="utf-8")
    block = re.search(
        r"<novelty_statement_rule>.*?</novelty_statement_rule>", novelty, re.S
    ).group(0)
    lowered = block.lower()

    # Scoped to one field, not a licence to stop abstaining everywhere.
    assert "novelty_statement" in block
    # The patent convention Gemini was missing (notes/day1_failures.md).
    assert "claim 1" in lowered
    # Numeric limits carried through unchanged, not paraphrased.
    assert "verbatim" in lowered
    # Null is still reachable — the rule narrows abstention, it does not ban it.
    assert "null" in lowered


def test_the_added_rule_carries_exactly_one_worked_example():
    """"Include exactly one worked example" was the instruction, and more
    examples would make this rung a few-shot change as well as a rule change."""
    naive = (PROMPT_DIR / "gemini_naive.txt").read_text(encoding="utf-8")
    novelty = (PROMPT_DIR / "gemini_novelty_v1_tool.txt").read_text(encoding="utf-8")
    block = re.search(
        r"<novelty_statement_rule>.*?</novelty_statement_rule>", novelty, re.S
    ).group(0)

    assert block.count("<example>") == 1
    assert novelty.count("<example>") == naive.count("<example>") + 1


def test_the_mode_axis_is_the_same_edit_on_both_prompts():
    """The 2×2 only separates prompt from mode if the mode edit is identical
    down each column. Comparing the two diffs is the direct way to say so."""
    naive_swap = _changed_lines("gemini_naive", "gemini_naive_schema")
    novelty_swap = _changed_lines("gemini_novelty_v1_tool", "gemini_novelty_v1_schema")
    assert naive_swap == novelty_swap
    assert naive_swap, "the schema twins are identical to their tool twins"


def test_the_mode_edit_only_touches_the_output_mechanism():
    """The mechanism is replaced; the instruction it was carrying is not."""
    changed = _changed_lines("gemini_naive", "gemini_naive_schema")
    removed = " ".join(l[1:].strip() for l in changed if l.startswith("-")).lower()
    added = " ".join(l[1:].strip() for l in changed if l.startswith("+")).lower()

    # the tool disappears entirely and the enforced schema takes its place
    assert "emit_features" in removed
    assert "emit_features" not in added
    assert "response schema" in added

    # ...but everything the mechanism sentence was also saying survives it
    for surviving in (
        "do not write any prose",
        "include every field",
        "null for anything the document does not state",
    ):
        assert surviving in removed
        assert surviving in added


def test_the_schema_twins_drop_the_tool_description_section():
    for variant in ("gemini_naive_schema", "gemini_novelty_v1_schema"):
        pack = pp.load_pack("feature_extractor", variant)
        assert pack.tool_description is None
        assert "emit_features" not in pack.text


# --------------------------------------------------------------------------
# few-shot contamination
# --------------------------------------------------------------------------


def test_the_novelty_rungs_quote_no_scored_corpus_item():
    """Both rungs quoted fe-0003 — a scored item — until 2026-08-11.

    The declaration made the overlap visible; the swap removed it
    (``notes/fe_worked_example_swap.md``). Both variants are still *declared*,
    with an empty tuple, so the rungs report a measured zero rather than
    having no check at all.
    """
    specs = {spec.rung: spec for spec in ladder_for("feature_extractor")}
    assert specs["A4-novelty-tool"].few_shot_item_ids == ()
    assert specs["A4-novelty-schema"].few_shot_item_ids == ()
    # Every hand-written rung declares its quoting, including the ones that
    # quote nothing. A rung missing from this mapping has no check at all,
    # which is indistinguishable in the artifact from a rung that passed one.
    assert set(FEW_SHOT_ITEM_IDS) == {
        "gemini_novelty_v1_tool",
        "gemini_novelty_v1_schema",
        "gemini_targeted_v1",
    }
    assert all(ids == () for ids in FEW_SHOT_ITEM_IDS.values())


def test_the_replacement_worked_example_is_not_drawn_from_the_corpus():
    """The real guard. ``few_shot_item_ids`` records what we *say* the prompt
    quotes; this checks what it actually contains — an id list can be wrong,
    the prompt text cannot."""
    items = [
        json.loads(line)
        for line in Path("datasets/feature_extractor.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert len(items) == 70, "corpus changed; re-check this guard"

    for variant in ("gemini_novelty_v1_tool", "gemini_novelty_v1_schema"):
        text = pp.load_pack("feature_extractor", variant).system
        for item in items:
            gold = item["gold"] or {}
            # The answer keys. A rung quoting one of these is being shown the
            # answer to an item it is scored on.
            for field in ("novelty_statement", "title", "assignee"):
                value = gold.get(field)
                if value:
                    assert value not in text, (
                        f"{variant} quotes {item['item_id']}'s gold {field} — the "
                        f"rung is being shown one of its own answer keys"
                    )
            # And the document itself. Short lines ("Claims:") are shared
            # boilerplate across every patent and say nothing about provenance,
            # so only distinctive ones count.
            for message in item["input"]["messages"]:
                for line in message.splitlines():
                    line = line.strip()
                    if len(line) >= 45:
                        assert line not in text, (
                            f"{variant} quotes a line of {item['item_id']}'s document"
                        )


def test_rungs_without_corpus_examples_declare_none():
    for spec in COMMON_RUNGS:
        assert spec.few_shot_item_ids == ()


def test_no_rung_reports_an_overlap_now(cfg):
    result = run_ladder(
        "feature_extractor",
        mode="replay",
        config=cfg,
        dataset_dir=cli.E2E_DATASET_DIR,
        write=False,
    )
    assert all(record.leaked_example_items == [] for record in result.rungs)
    assert not any("fe-0003" in note for note in result.notes)


def test_the_overlap_machinery_still_fires_if_an_overlap_returns(cfg, monkeypatch):
    """The swap removed today's contamination. This proves it would be caught
    again — a guard that only passes because there is nothing to catch is not a
    guard."""
    import amw.tuning.ablate as ablate_mod

    leaky = tuple(
        spec.model_copy(update={"few_shot_item_ids": ("fe-0003",)})
        if spec.rung == "A4-novelty-tool"
        else spec
        for spec in ablate_mod.ladder_for("feature_extractor")
    )
    monkeypatch.setattr(ablate_mod, "ladder_for", lambda _subagent: leaky)

    result = run_ladder(
        "feature_extractor",
        mode="replay",
        config=cfg,
        dataset_dir=cli.E2E_DATASET_DIR,
        write=False,
    )
    by_rung = {record.rung: record for record in result.rungs}
    assert by_rung["A4-novelty-tool"].leaked_example_items == ["fe-0003"]
    assert by_rung["A0"].leaked_example_items == []
    assert any("fe-0003" in note for note in result.notes)


def test_the_overlap_is_disclosed_not_silently_excluded(cfg):
    """Dropping the leaked item would give these two rungs a different
    denominator from every other rung — a worse problem than a disclosed one."""
    result = run_ladder(
        "feature_extractor",
        mode="replay",
        config=cfg,
        dataset_dir=cli.E2E_DATASET_DIR,
        write=False,
    )
    item_counts = {record.provenance.items for record in result.rungs}
    assert len(item_counts) == 1


# --------------------------------------------------------------------------
# running the ladder offline
# --------------------------------------------------------------------------


@pytest.mark.parametrize("subagent", SUBAGENTS)
def test_the_ladder_completes_in_replay_mode_with_no_credentials(cfg, subagent):
    result = run_ladder(
        subagent,
        mode="replay",
        config=cfg,
        dataset_dir=cli.E2E_DATASET_DIR,
        write=False,
    )
    assert result.subagent == subagent
    assert [r.rung for r in result.rungs] == [s.rung for s in ladder_for(subagent)]
    assert all(r.status in {"measured", "no_recordings"} for r in result.rungs)


def test_measured_rungs_are_scored_by_the_phase2_arm_runner(cfg):
    """Not "similar numbers computed here" — the same ArmResult phase2 writes."""
    result = run_ladder(
        "feature_extractor",
        mode="replay",
        config=cfg,
        dataset_dir=cli.E2E_DATASET_DIR,
        write=False,
    )
    measured = [r for r in result.rungs if r.status == "measured"]
    assert measured, "the fixture corpus should have recordings for the common rungs"
    for record in measured:
        assert isinstance(record.arm, ArmResult)
        assert record.arm.subagent == "feature_extractor"
        assert record.arm.variant == record.variant
        assert record.arm.items == record.provenance.items
        assert record.judged_split == "core"


def test_an_unmeasured_rung_carries_no_numbers_at_all(cfg):
    """Ground rule 1. A zero here would be read as a measured zero."""
    result = run_ladder(
        "feature_extractor",
        mode="replay",
        config=cfg,
        dataset_dir=cli.E2E_DATASET_DIR,
        write=False,
    )
    unmeasured = [r for r in result.rungs if r.status == "no_recordings"]
    assert {r.rung for r in unmeasured} == {
        "A0-schema",
        "A4-novelty-tool",
        "A4-novelty-schema",
        # The current-generation pair: the e2e fixture corpus was recorded on
        # the 2.5-class models, so these two miss the replay store by design.
        "A0-current",
        "ship-current",
    }
    for record in unmeasured:
        assert record.arm is None
        assert record.judged_n is None
        assert record.judged_split is None
        assert "hypothesis" in record.unmeasured_reason
        assert record.provenance.recorded_from is None


def test_an_unmeasured_rung_still_says_what_it_would_have_run(cfg):
    result = run_ladder(
        "feature_extractor",
        mode="replay",
        config=cfg,
        dataset_dir=cli.E2E_DATASET_DIR,
        write=False,
    )
    record = next(r for r in result.rungs if r.rung == "A4-novelty-schema")
    assert record.output_mode == "response_schema"
    assert record.model in set(cfg.models.models)
    assert record.prompt_sha == pp.load_pack(
        "feature_extractor", "gemini_novelty_v1_schema"
    ).sha256


def test_the_run_says_out_loud_that_rungs_are_unmeasured(cfg):
    result = run_ladder(
        "feature_extractor",
        mode="replay",
        config=cfg,
        dataset_dir=cli.E2E_DATASET_DIR,
        write=False,
    )
    assert any("no recorded calls" in note for note in result.notes)


def test_every_record_carries_its_own_provenance(cfg):
    """Records accumulate across runs, so row four must be datable on its own."""
    result = run_ladder(
        "query_rewriter",
        mode="replay",
        config=cfg,
        dataset_dir=cli.E2E_DATASET_DIR,
        write=False,
    )
    for record in result.rungs:
        prov = record.provenance
        assert prov.customer == cfg.customer.customer
        assert prov.mode == "replay"
        assert prov.region == cfg.customer.region
        assert prov.dataset_provenance == "synthetic"
        assert prov.dataset_seed
        assert prov.split == "core"
        assert prov.judge_repeats == cfg.customer.dataset.judge_repeats
        assert prov.written_at
        # replay did not run anything just now, so there is no run clock
        assert prov.run_started is None


def test_a_replayed_record_is_dated_by_the_calls_it_served(cfg):
    result = run_ladder(
        "feature_extractor",
        mode="replay",
        config=cfg,
        dataset_dir=cli.E2E_DATASET_DIR,
        write=False,
    )
    measured = [r for r in result.rungs if r.status == "measured"]
    assert all(r.provenance.recorded_from for r in measured)
    assert all(
        r.provenance.recorded_from <= r.provenance.recorded_to for r in measured
    )


def test_a_subset_of_rungs_can_be_run(cfg):
    result = run_ladder(
        "feature_extractor",
        mode="replay",
        config=cfg,
        dataset_dir=cli.E2E_DATASET_DIR,
        rungs=["A0", "A4-novelty-tool"],
        write=False,
    )
    assert [r.rung for r in result.rungs] == ["A0", "A4-novelty-tool"]


def test_an_unknown_rung_name_is_rejected(cfg):
    with pytest.raises(ValueError, match="unknown rung"):
        run_ladder(
            "feature_extractor",
            mode="replay",
            config=cfg,
            dataset_dir=cli.E2E_DATASET_DIR,
            rungs=["A9"],
            write=False,
        )


def test_n_limits_the_split(cfg):
    result = run_ladder(
        "query_rewriter",
        mode="replay",
        config=cfg,
        dataset_dir=cli.E2E_DATASET_DIR,
        n=2,
        write=False,
    )
    assert all(record.provenance.items == 2 for record in result.rungs)


def test_a_missing_corpus_says_how_to_make_one(cfg, tmp_path):
    with pytest.raises(FileNotFoundError, match="cli.py gen"):
        run_ladder(
            "query_rewriter",
            mode="replay",
            config=cfg,
            dataset_dir=tmp_path,
            write=False,
        )


# --------------------------------------------------------------------------
# the artifact
# --------------------------------------------------------------------------


def test_default_artifact_path_is_per_subagent():
    path = default_results_path("feature_extractor")
    assert path.name == "ablation_feature_extractor.json"
    assert path.parent.name == "results"


def _run_to(cfg, tmp_path: Path, **kwargs) -> Path:
    out = tmp_path / "ablation.json"
    run_ladder(
        "query_rewriter",
        mode="replay",
        config=cfg,
        dataset_dir=cli.E2E_DATASET_DIR,
        out_path=out,
        **kwargs,
    )
    return out


#: How many rungs ``_run_to`` writes. Derived, not a literal: these tests are
#: about append-vs-replace, and hardcoding the count makes every one of them
#: fail the day a rung is added — which says nothing about appending.
_QR_RUNGS = len(ladder_for("query_rewriter"))


def test_the_artifact_round_trips_through_its_model(cfg, tmp_path):
    out = _run_to(cfg, tmp_path)
    parsed = AblationResult.model_validate_json(out.read_text(encoding="utf-8"))
    assert parsed.ablation_version == ABLATION_VERSION
    assert parsed.subagent == "query_rewriter"
    assert len(parsed.rungs) == _QR_RUNGS


def test_records_append_across_runs_rather_than_replacing(cfg, tmp_path):
    """The ladder is re-run as rungs are edited; keeping only the last run
    would erase the history the ladder exists to show."""
    out = _run_to(cfg, tmp_path)
    _run_to(cfg, tmp_path)
    parsed = AblationResult.model_validate_json(out.read_text(encoding="utf-8"))
    assert len(parsed.rungs) == 2 * _QR_RUNGS


def test_append_can_be_turned_off(cfg, tmp_path):
    out = _run_to(cfg, tmp_path)
    _run_to(cfg, tmp_path, append=False)
    parsed = AblationResult.model_validate_json(out.read_text(encoding="utf-8"))
    assert len(parsed.rungs) == _QR_RUNGS


def test_appending_to_another_subagents_artifact_is_refused(cfg, tmp_path):
    out = _run_to(cfg, tmp_path)
    with pytest.raises(ValueError, match="holds rungs for"):
        run_ladder(
            "feature_extractor",
            mode="replay",
            config=cfg,
            dataset_dir=cli.E2E_DATASET_DIR,
            out_path=out,
        )


def test_appending_across_artifact_versions_is_refused(cfg, tmp_path):
    out = _run_to(cfg, tmp_path)
    stale = json.loads(out.read_text(encoding="utf-8"))
    stale["ablation_version"] = "0"
    out.write_text(json.dumps(stale), encoding="utf-8")
    with pytest.raises(ValueError, match="ablation_version"):
        _run_to(cfg, tmp_path)


def test_the_artifact_holds_no_hypothesis_and_no_prediction(cfg, tmp_path):
    """The hypothesis lives in the module docstring. An artifact is a record of
    what happened, and mixing a prediction into one invites it being read as a
    result."""
    out = tmp_path / "fe.json"
    run_ladder(
        "feature_extractor",
        mode="replay",
        config=cfg,
        dataset_dir=cli.E2E_DATASET_DIR,
        out_path=out,
    )
    text = out.read_text(encoding="utf-8").lower()
    for word in ("falsif", "predict", "should improve", "we expect", "is likely to"):
        assert word not in text, f"the artifact contains a prediction: {word!r}"

    # "hypothesis" appears exactly once per unmeasured rung, and only as the
    # stated reason that rung has no numbers — never as a claim about one.
    parsed = AblationResult.model_validate_json(out.read_text(encoding="utf-8"))
    reasons = " ".join(r.unmeasured_reason or "" for r in parsed.rungs).lower()
    assert reasons.count("hypothesis") == text.count("hypothesis") > 0


# --------------------------------------------------------------------------
# console rendering
# --------------------------------------------------------------------------


def test_format_rung_never_prints_a_number_for_an_unmeasured_rung(cfg):
    result = run_ladder(
        "feature_extractor",
        mode="replay",
        config=cfg,
        dataset_dir=cli.E2E_DATASET_DIR,
        write=False,
    )
    record = next(r for r in result.rungs if r.status == "no_recordings")
    text = "\n".join(format_rung(record))
    assert "NOT MEASURED" in text
    assert not re.search(r"\d+\.\d+", text), text


def test_format_rung_shows_the_judged_score_and_the_guard_metrics(cfg):
    result = run_ladder(
        "feature_extractor",
        mode="replay",
        config=cfg,
        dataset_dir=cli.E2E_DATASET_DIR,
        write=False,
    )
    record = next(r for r in result.rungs if r.status == "measured")
    text = "\n".join(format_rung(record))
    assert record.label in text
    assert "judge_score" in text
    assert "hallucination_rate" in text
    assert "answered_precision" in text


def test_error_kinds_aggregates_over_measured_rungs_only(cfg):
    result = run_ladder(
        "feature_extractor",
        mode="replay",
        config=cfg,
        dataset_dir=cli.E2E_DATASET_DIR,
        write=False,
    )
    counts = error_kinds(result.rungs)
    assert isinstance(counts, dict)
    assert all(isinstance(value, int) for value in counts.values())


# --------------------------------------------------------------------------
# the CLI seam
# --------------------------------------------------------------------------


def _args(**kwargs) -> argparse.Namespace:
    base = {"subagent": None, "mode": "replay"}
    base.update(kwargs)
    return argparse.Namespace(**base)


def test_cmd_ablate_runs_one_subagent_offline(cfg, tmp_path, capsys):
    out = tmp_path / "fe.json"
    code = cmd_ablate(
        _args(
            subagent="feature_extractor",
            dataset_dir=str(cli.E2E_DATASET_DIR),
            out=str(out),
        ),
        cfg,
    )
    captured = capsys.readouterr()
    assert code == 0
    assert "=== ablation ladder: feature_extractor ===" in captured.out
    assert "NOT MEASURED" in captured.out
    # ground rule 1: a replayed number says on screen when it was recorded
    assert "REPLAY — measured rungs above come from calls recorded" in captured.out
    assert out.exists()


def test_cmd_ablate_works_with_only_the_flags_cli_already_has(cfg, tmp_path, monkeypatch):
    """`--subagent` and `--mode` are the flags on the parser today; the seam
    must not require the new ones to have been wired first."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "amw.tuning.ablate.default_dataset_dir", lambda: cli.E2E_DATASET_DIR
    )
    monkeypatch.setattr(
        "amw.tuning.ablate.default_results_path",
        lambda subagent: tmp_path / f"ablation_{subagent}.json",
    )
    assert cmd_ablate(_args(subagent="query_rewriter"), cfg) == 0
    assert (tmp_path / "ablation_query_rewriter.json").exists()


def test_cmd_ablate_runs_every_subagent_when_none_is_named(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "amw.tuning.ablate.default_results_path",
        lambda subagent: tmp_path / f"ablation_{subagent}.json",
    )
    code = cmd_ablate(_args(dataset_dir=str(cli.E2E_DATASET_DIR)), cfg)
    assert code == 0
    for subagent in SUBAGENTS:
        assert (tmp_path / f"ablation_{subagent}.json").exists()


def test_cmd_ablate_refuses_one_out_path_for_three_ladders(cfg, tmp_path, capsys):
    code = cmd_ablate(
        _args(dataset_dir=str(cli.E2E_DATASET_DIR), out=str(tmp_path / "x.json")), cfg
    )
    assert code == 2
    assert "needs --subagent too" in capsys.readouterr().err


def test_cmd_ablate_rejects_an_unknown_subagent(cfg, capsys):
    assert cmd_ablate(_args(subagent="nope"), cfg) == 2
    assert "unknown subagent" in capsys.readouterr().err


def test_cmd_ablate_honours_the_optional_flags(cfg, tmp_path):
    out = tmp_path / "fe.json"
    code = cmd_ablate(
        _args(
            subagent="feature_extractor",
            dataset_dir=str(cli.E2E_DATASET_DIR),
            out=str(out),
            n=2,
            rung=["A0"],
            no_judge=True,
            no_append=True,
        ),
        cfg,
    )
    assert code == 0
    parsed = AblationResult.model_validate_json(out.read_text(encoding="utf-8"))
    assert [r.rung for r in parsed.rungs] == ["A0"]
    assert parsed.rungs[0].provenance.items == 2
    assert parsed.rungs[0].arm is not None
    assert parsed.rungs[0].arm.judge is None
