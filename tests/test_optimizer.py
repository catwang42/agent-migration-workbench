"""Tests for the VAIPO rung scaffold — offline, credential-free, and unrun.

Two things this file is really guarding:

* **The scaffold cannot run itself.** No test here reaches a service, and the
  ones that exercise the submission path inject a stub client. If a change
  makes ``import amw.tuning.optimizer`` or any of these tests need ADC, the SDK
  or a network, that is the regression.
* **The scaffold cannot fabricate.** An optimizer that has not run must report
  that, not a zero. Several tests below exist only to assert the absence of a
  plausible-looking number.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from amw.agents import prompt_packs as pp
from amw.agents.schemas import SUBAGENTS
from amw.config import ConfigError, load_all
from amw.eval.crosscheck import INSUFFICIENT, UNRELIABLE, VALIDATED
from amw.tuning import ablate, optimizer
from amw.tuning.ablate import P1_RUNGS, ladder_for
from amw.tuning.optimizer import (
    CUTOFF_SECONDS,
    LEGACY_RUNG_ID,
    MIN_TRAINING_EXAMPLES,
    OPTIMIZER_RUNG_ID,
    OPTIMIZER_VARIANT,
    TARGETS,
    VAIPO_SURFACE,
    Deadline,
    OptimizationRun,
    TrainingExample,
    TrainingSet,
    build_training_set,
    candidate_rung,
    compose_prompt_file,
    crosscheck_verdict,
    installed_rung,
    render_lines,
    run_optimizer_rung,
    select_target,
    submit_optimization,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def no_credentials(monkeypatch):
    """Ground rule 4, asserted rather than assumed."""
    for var in (
        "PROJECT_ID",
        "REGION",
        "GOOGLE_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CLOUD_PROJECT",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def fe_target():
    return TARGETS["fe_novelty_judged"]


@pytest.fixture
def qr_target():
    """The retarget — and the only target still on the staging path.

    Feature Extractor's optimizer rung was promoted into the prompt pack when
    it was selected to ship, so it no longer stages anything. Every assertion
    about staging has to be made against a target that still stages, or it
    stops testing the code it names.
    """
    return TARGETS["qr_intent_deterministic"]


def training(size: int, *, overlaps=()) -> TrainingSet:
    return TrainingSet(
        subagent="feature_extractor",
        base_variant="gemini_naive",
        seed=1,
        requested=size,
        examples=[
            TrainingExample(
                item_id=f"fe-{n:04d}",
                prompt="p",
                model_response="m",
                target_response="t",
            )
            for n in range(size)
        ],
        overlaps_scored_split=list(overlaps),
    )


class StubPrompts:
    """Stands in for ``client.prompts``; records the call, returns a canned reply."""

    def __init__(self, instruction=None, error=None, guidelines=(), delay=0.0):
        self.instruction = instruction
        self.error = error
        self.guidelines = list(guidelines)
        self.delay = delay
        self.calls: list[dict] = []

    def optimize(self, *, prompt, config):
        self.calls.append({"prompt": prompt, "config": config})
        if self.delay:
            import time

            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        parsed = SimpleNamespace(
            suggested_prompt=self.instruction,
            applicable_guidelines=[
                SimpleNamespace(guideline=g) for g in self.guidelines
            ],
            optimization_type="INSTRUCTION",
        )
        return SimpleNamespace(parsed_response=parsed)


class StubClient:
    def __init__(self, **kwargs):
        self.prompts = StubPrompts(**kwargs)


# --------------------------------------------------------------------------
# the rung name, and the promise not to build it yet
# --------------------------------------------------------------------------


def test_the_rung_uses_the_owners_name_and_remembers_the_old_one():
    assert OPTIMIZER_RUNG_ID == "A4-optimizer"
    assert LEGACY_RUNG_ID == ablate.VAIPO_RUNG_ID == "A4-prime"
    # The old name is not dead — a reader who greps SPIKES.md or the T10 card
    # for A4-prime must be able to find their way here.
    assert LEGACY_RUNG_ID in optimizer.__doc__


def test_importing_the_scaffold_does_not_install_the_rung():
    """``tests/test_ablate.py`` asserts ``P1_RUNGS == {}``; import order must
    not be able to break that. Registration happens inside
    :func:`installed_rung` and nowhere else."""
    assert P1_RUNGS == {}
    for subagent in SUBAGENTS:
        rungs = {spec.rung for spec in ladder_for(subagent)}
        assert OPTIMIZER_RUNG_ID not in rungs
        assert LEGACY_RUNG_ID not in rungs


def test_the_optimizer_variant_is_committed_only_where_it_ships():
    """Exactly one subagent has a committed optimizer prompt: the one that ships it.

    The rule used to be "no prompt file exists for the generated variant, and
    none should" — committing one would mean shipping an "optimizer output" no
    optimizer produced. That still holds everywhere the rung is a run artifact.
    It stops holding the moment a rung is *selected to ship*: Feature Extractor
    quotes ``A4-optimizer``'s core-28 score on the scorecard, and a number
    quoted from a file that is deleted at the end of the block is a number no
    customer can check. So the file is promoted by hand, byte-identically, and
    the declaration in ``VARIANT_SPECS`` is what
    :func:`~amw.tuning.optimizer.installed_rung` reads to know it must not
    stage over it.

    Both halves are asserted, because either one failing is a different bug:
    an undeclared file is the stray-file guard's problem, and a declared
    variant with no file means the shipping arm cannot be loaded at all.
    """
    declared = pp.VARIANT_SPECS[OPTIMIZER_VARIANT].subagents
    assert declared == ("feature_extractor",)
    for subagent in SUBAGENTS:
        path = pp.prompts_dir() / subagent / f"{OPTIMIZER_VARIANT}.txt"
        assert path.exists() is (subagent in declared), path


def test_every_declared_target_names_things_that_exist():
    for key, target in TARGETS.items():
        assert target.key == key
        assert target.subagent in SUBAGENTS
        assert target.base_variant in pp.variants_for(target.subagent)
        ladder = {spec.rung for spec in ladder_for(target.subagent)}
        assert target.hand_tuned_rung in ladder, (
            f"{key} falls back to rung {target.hand_tuned_rung!r}, which is not "
            f"in {target.subagent}'s ladder {sorted(ladder)}"
        )
        assert target.branches_from in ladder


# --------------------------------------------------------------------------
# the owner's gate
# --------------------------------------------------------------------------


def test_validated_targets_the_judged_fe_problem():
    selection = select_target(VALIDATED)
    assert selection.target_key == "fe_novelty_judged"
    assert selection.target.objective == "judged"
    assert not selection.requires_owner_signoff


def test_unreliable_retargets_to_deterministic_qr_and_asks_first():
    selection = select_target(UNRELIABLE)
    assert selection.target_key == "qr_intent_deterministic"
    assert selection.target.objective == "deterministic"
    assert selection.requires_owner_signoff
    assert selection.reframe  # the owner is shown the reframe, not just a flag


@pytest.mark.parametrize("verdict", [INSUFFICIENT, None, "MAYBE"])
def test_no_ruling_means_no_target(verdict):
    selection = select_target(verdict)
    assert selection.target_key is None
    assert selection.target is None


def test_the_missing_and_the_undecided_rulings_read_differently():
    """"the cross-check could not tell" and "the cross-check has not run" are
    different things to put in front of an owner."""
    assert select_target(None).reason != select_target(INSUFFICIENT).reason


def test_the_verdict_is_read_from_the_artifact_and_is_absent_today(tmp_path):
    assert crosscheck_verdict("feature_extractor", tmp_path / "nope.json") is None
    # The real one. As of 2026-08-11 the cross-check HAS run and returned
    # VALIDATED for feature_extractor, so this reads a verdict rather than
    # None. Guarded both ways on purpose: the assertion is about the reader
    # agreeing with the artifact on disk, not about which way the gate fell.
    live = optimizer.default_crosscheck_path()
    if live.is_file():
        assert crosscheck_verdict("feature_extractor") in {
            VALIDATED,
            UNRELIABLE,
            INSUFFICIENT,
        }
    else:
        assert crosscheck_verdict("feature_extractor") is None


def test_the_retarget_refuses_until_the_owner_has_seen_it(tmp_path):
    result = run_optimizer_rung(
        mode="live",
        verdict=UNRELIABLE,
        out_path=tmp_path / "optimizer.json",
        run_ladder_after=False,
    )
    assert result.run.status == "refused"
    assert result.ablation is None
    assert any("retargeted" in note for note in result.notes)
    assert (tmp_path / "optimizer.json").is_file()


def test_an_undecided_ruling_writes_an_artifact_saying_so(tmp_path):
    # crosscheck_path must point somewhere empty. `verdict=None` means "read the
    # ruling off disk", and the default path is the repo's real artifact — which
    # exists as of 2026-08-11 and says VALIDATED. Without this the test stops
    # exercising the no-ruling branch the moment a cross-check has been run, and
    # worse, walks into the live optimize path.
    result = run_optimizer_rung(
        mode="live",
        verdict=None,
        crosscheck_path=tmp_path / "no_crosscheck.json",
        out_path=tmp_path / "o.json",
        run_ladder_after=False,
    )
    assert result.run.status == "refused"
    assert result.run.instruction is None
    assert result.run.elapsed_seconds is None
    assert result.subagent is None


# --------------------------------------------------------------------------
# real run only
# --------------------------------------------------------------------------


def test_replay_mode_is_refused_at_both_entry_points(fe_target):
    with pytest.raises(ConfigError, match="real-run-only|real run only"):
        run_optimizer_rung(mode="replay", verdict=VALIDATED, write=False)
    with pytest.raises(ConfigError, match="real-run-only|real run only"):
        submit_optimization(fe_target, training(8), mode="replay")


def test_the_unspiked_vapo_surface_is_refused_by_name(fe_target):
    with pytest.raises(ConfigError, match="CustomJob"):
        submit_optimization(fe_target, training(8), mode="live", surface="VAPO")


def test_only_the_spiked_surface_is_callable(fe_target):
    assert VAIPO_SURFACE == "prompts.optimize"
    with pytest.raises(ConfigError, match="unknown VAIPO surface"):
        submit_optimization(fe_target, training(8), mode="live", surface="magic")


# --------------------------------------------------------------------------
# nothing that did not happen gets a number
# --------------------------------------------------------------------------


def test_a_run_that_never_happened_reports_not_run_and_no_figures():
    run = OptimizationRun(run_id="x")
    assert run.status == "not_run"
    assert not run.delivered
    for field in ("instruction", "elapsed_seconds", "optimization_type"):
        assert getattr(run, field) is None, f"{field} must be absent, not defaulted"
    assert run.guidelines == []
    assert "not been run" in run.status_line()


def test_the_console_rendering_of_an_unrun_rung_carries_no_numbers(tmp_path):
    result = run_optimizer_rung(
        mode="live",
        verdict=None,
        crosscheck_path=tmp_path / "no_crosscheck.json",
        out_path=tmp_path / "o.json",
        run_ladder_after=False,
    )
    text = "\n".join(render_lines(result))
    assert "not run" in text
    assert "ladder      : not run" in text


def test_a_thin_training_set_is_refused_rather_than_optimized(fe_target):
    run = submit_optimization(
        fe_target, training(MIN_TRAINING_EXAMPLES - 1), mode="live"
    )
    assert run.status == "refused"
    assert run.instruction is None
    assert "SPIKE-S3" in run.detail


def test_missing_project_or_region_refuses_before_any_call(fe_target, monkeypatch):
    client = StubClient(instruction="never reached")
    run = submit_optimization(fe_target, training(8), mode="live", client=client)
    assert run.status == "refused"
    assert "PROJECT_ID" in run.detail
    assert client.prompts.calls == []


# --------------------------------------------------------------------------
# the cutoff, as code
# --------------------------------------------------------------------------


def test_the_default_cutoff_is_the_owners_two_hours():
    assert CUTOFF_SECONDS == 2 * 60 * 60


def test_a_deadline_measures_from_a_clock_it_is_given():
    ticks = iter([0.0, 10.0, 120.0])
    budget = Deadline(100.0, clock=lambda: next(ticks))
    assert budget.remaining() == pytest.approx(90.0)
    assert budget.expired


def test_an_expired_deadline_refuses_to_start_the_call():
    ticks = iter([0.0, 5.0, 5.0])
    budget = Deadline(1.0, clock=lambda: next(ticks))
    with pytest.raises(TimeoutError):
        budget.run(lambda: "should not run")


def test_a_slow_optimizer_times_out_and_hands_back_the_hand_tuned_rung(
    fe_target, monkeypatch
):
    monkeypatch.setenv("PROJECT_ID", "p")
    monkeypatch.setenv("REGION", "global")
    client = StubClient(instruction="too late", delay=0.5)
    run = submit_optimization(
        fe_target,
        training(8),
        mode="live",
        client=client,
        deadline=Deadline(0.05),
    )
    assert run.status == "timed_out"
    assert run.instruction is None
    assert run.elapsed_seconds is not None and run.elapsed_seconds > 0
    assert fe_target.hand_tuned_rung in candidate_rung(fe_target, run)


def test_a_service_failure_is_a_status_not_an_exception(fe_target, monkeypatch):
    monkeypatch.setenv("PROJECT_ID", "p")
    monkeypatch.setenv("REGION", "global")
    client = StubClient(error=RuntimeError("503 backend unavailable"))
    run = submit_optimization(fe_target, training(8), mode="live", client=client)
    assert run.status == "service_error"
    assert "503" in run.detail
    assert fe_target.hand_tuned_rung in candidate_rung(fe_target, run)


def test_no_suggestion_is_not_a_success(fe_target, monkeypatch):
    monkeypatch.setenv("PROJECT_ID", "p")
    monkeypatch.setenv("REGION", "global")
    run = submit_optimization(
        fe_target, training(8), mode="live", client=StubClient(instruction="   ")
    )
    assert run.status == "no_candidate"
    assert run.instruction is None


def test_a_delivered_candidate_does_not_declare_itself_the_winner(
    fe_target, monkeypatch
):
    """Two rungs, no verdict. Choosing between them is the ladder's job."""
    monkeypatch.setenv("PROJECT_ID", "p")
    monkeypatch.setenv("REGION", "global")
    run = submit_optimization(
        fe_target,
        training(8),
        mode="live",
        client=StubClient(instruction="Be specific about novelty.", guidelines=["g1"]),
    )
    assert run.status == "optimized"
    assert run.delivered
    assert run.guidelines == ["g1"]
    sentence = candidate_rung(fe_target, run)
    assert fe_target.hand_tuned_rung in sentence and OPTIMIZER_RUNG_ID in sentence
    assert "both" in sentence


def test_the_submitted_prompt_is_the_base_variants_instruction(
    fe_target, monkeypatch
):
    monkeypatch.setenv("PROJECT_ID", "p")
    monkeypatch.setenv("REGION", "global")
    client = StubClient(instruction="better")
    submit_optimization(fe_target, training(8), mode="live", client=client)
    base = pp.load_pack(fe_target.subagent, fe_target.base_variant)
    assert client.prompts.calls[0]["prompt"] == base.system


# --------------------------------------------------------------------------
# training data comes from recorded calls, never from imagination
# --------------------------------------------------------------------------


def test_an_empty_store_yields_no_examples_rather_than_invented_ones(
    fe_target, tmp_path
):
    """The failure mode this guards is the tempting one: if no recorded call
    exists, write a plausible ``model_response`` and optimize against it. That
    would be a fabricated baseline (ground rule 1). Instead: nothing, with a
    reason per dropped item."""
    from amw.traces.store import ReplayStore

    built = build_training_set(fe_target, store=ReplayStore(tmp_path / "replay"))
    assert built.examples == []
    assert built.skipped, "a dropped training item must say why it was dropped"
    assert all("artifacts/replay" in reason for reason in built.skipped.values())


def test_the_training_draw_is_deterministic_and_reads_real_traces(fe_target):
    """Against the committed replay corpus this must actually produce examples
    — the scaffold is only "ready to run" if its input path works today."""
    first = build_training_set(fe_target)
    second = build_training_set(fe_target)
    assert [e.item_id for e in first.examples] == [e.item_id for e in second.examples]
    assert first.examples, (
        "no A0 traces for feature_extractor in artifacts/replay/; the optimizer "
        "cannot be given a real baseline to improve on"
    )
    for example in first.examples:
        assert example.model_response.strip()
        assert example.target_response.strip()


def test_the_optimizer_trains_off_the_split_the_ladder_scores(fe_target):
    """``run_ladder`` judges the core split, so the non-core items are a
    genuine held-out pool and the optimizer must draw from it."""
    built = build_training_set(fe_target)
    assert built.held_out
    assert built.overlaps_scored_split == []


# --------------------------------------------------------------------------
# prompt-file plumbing
# --------------------------------------------------------------------------


def test_composing_a_pack_changes_the_instruction_and_nothing_else(fe_target):
    base = pp.load_pack(fe_target.subagent, fe_target.base_variant)
    text = compose_prompt_file(base, "  New instruction.  ")
    assert "=== system ===\nNew instruction." in text
    assert base.user_template in text
    if base.tool_description is not None:
        assert base.tool_description in text
    assert base.system not in text


def test_an_empty_instruction_is_not_composable(fe_target):
    base = pp.load_pack(fe_target.subagent, fe_target.base_variant)
    with pytest.raises(pp.PromptPackError):
        compose_prompt_file(base, "   ")


def test_installing_the_rung_registers_it_and_cleans_up_after(
    qr_target, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        optimizer, "default_instruction_path", lambda s, r: tmp_path / f"{s}-{r}.txt"
    )
    run = OptimizationRun(
        run_id="testrun",
        target_key=qr_target.key,
        status="optimized",
        instruction="Focus on what is new relative to the cited art.",
        training=training(6, overlaps=("fe-0001", "fe-0002")),
    )
    staged = pp.prompts_dir() / qr_target.subagent / f"{OPTIMIZER_VARIANT}.txt"

    with installed_rung(qr_target, run) as rung:
        assert rung.rung == OPTIMIZER_RUNG_ID
        assert rung.branches_from == qr_target.branches_from
        # EVERY training item rides in on the contamination field, not the
        # pre-computed `overlaps` subset. run_ladder intersects this with the
        # split it actually scores, so handing it the whole training set is
        # what keeps the disclosure correct on any split. Passing the
        # pre-computed intersection instead was a real bug: the overlap was
        # measured against the core 28, so re-running the rung at n=70 — where
        # all 6 of these items ARE scored — would have disclosed nothing.
        assert rung.few_shot_item_ids == tuple(
            example.item_id for example in run.training.examples
        )
        assert set(("fe-0001", "fe-0002")) <= set(rung.few_shot_item_ids)
        assert staged.is_file()
        assert OPTIMIZER_VARIANT in pp.variants_for(qr_target.subagent)
        assert rung in ladder_for(qr_target.subagent)
        assert pp.load_pack(qr_target.subagent, OPTIMIZER_VARIANT).system.startswith(
            "Focus on what is new"
        )

    assert not staged.exists(), "the staged prompt must not survive the run"
    assert P1_RUNGS == {}
    assert (tmp_path / f"{qr_target.subagent}-testrun.txt").is_file()
    # Not `not in`: the variant name is owned permanently by feature_extractor,
    # which ships it. Borrowing the name for the query_rewriter retarget must
    # put the shipping declaration back, not delete it.
    assert pp.VARIANT_SPECS[OPTIMIZER_VARIANT].subagents == ("feature_extractor",)


def test_the_rung_is_cleaned_up_even_when_the_ladder_explodes(
    qr_target, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        optimizer, "default_instruction_path", lambda s, r: tmp_path / "i.txt"
    )
    run = OptimizationRun(
        run_id="boom", status="optimized", instruction="x", training=training(6)
    )
    staged = pp.prompts_dir() / qr_target.subagent / f"{OPTIMIZER_VARIANT}.txt"
    with pytest.raises(RuntimeError):
        with installed_rung(qr_target, run):
            raise RuntimeError("the ladder fell over")
    assert not staged.exists()
    assert P1_RUNGS == {}
    assert pp.VARIANT_SPECS[OPTIMIZER_VARIANT].subagents == ("feature_extractor",)


def test_a_leftover_staged_prompt_stops_the_run(qr_target, tmp_path, monkeypatch):
    """A file left behind by a crashed run is a stale instruction; reusing it
    would score this rung on a prompt from a different optimization."""
    monkeypatch.setattr(
        optimizer, "default_instruction_path", lambda s, r: tmp_path / "i.txt"
    )
    staged = pp.prompts_dir() / qr_target.subagent / f"{OPTIMIZER_VARIANT}.txt"
    staged.write_text("=== system ===\nstale\n\n=== user ===\n{x}\n", encoding="utf-8")
    run = OptimizationRun(
        run_id="r", status="optimized", instruction="fresh", training=training(6)
    )
    try:
        with pytest.raises(pp.PromptPackError, match="already exists"):
            with installed_rung(qr_target, run):
                pass
    finally:
        staged.unlink(missing_ok=True)
        pp.load_pack.cache_clear()


# -- the promoted path: feature_extractor, which ships this rung -------------


def test_a_promoted_rung_runs_off_the_committed_pack(fe_target, tmp_path, monkeypatch):
    """No staging, no deletion — the committed file *is* the rung.

    Feature Extractor's scorecard row quotes ``A4-optimizer``'s core-28 score,
    and the honesty of that citation rests on the prompt behind it being one a
    customer can open. So re-installing the rung must leave the pack file
    exactly where it is: the old code wrote a scaffolding copy on the way in
    and unlinked it on the way out, which would now delete the shipping arm.
    """
    monkeypatch.setattr(
        optimizer, "default_instruction_path", lambda s, r: tmp_path / "i.txt"
    )
    pack_file = pp.prompts_dir() / fe_target.subagent / f"{OPTIMIZER_VARIANT}.txt"
    before = pack_file.read_bytes()
    committed = pp.load_pack(fe_target.subagent, OPTIMIZER_VARIANT)

    run = OptimizationRun(
        run_id="20260811T060312Z",
        target_key=fe_target.key,
        status="optimized",
        # The instruction that composes back to the committed file: its own
        # system section. Anything else is a different prompt and is refused
        # by the test below.
        instruction=committed.system,
        training=training(6),
    )
    with installed_rung(fe_target, run) as rung:
        assert rung.rung == OPTIMIZER_RUNG_ID
        assert rung.variant == OPTIMIZER_VARIANT
        assert rung in ladder_for(fe_target.subagent)
        assert pack_file.read_bytes() == before

    assert pack_file.read_bytes() == before, "the shipping prompt was modified"
    assert pp.VARIANT_SPECS[OPTIMIZER_VARIANT].subagents == ("feature_extractor",)
    assert P1_RUNGS == {}


def test_a_promoted_rung_refuses_an_instruction_that_is_not_the_committed_one(
    fe_target, tmp_path, monkeypatch
):
    """A new optimizer run does not get to silently shadow the shipping prompt.

    Staging over it would score the rung on a file that is deleted seconds
    later, while the scorecard goes on citing the committed one — two
    different prompts behind a single number. Promotion is a deliberate act;
    this is the speed bump that makes it one.
    """
    monkeypatch.setattr(
        optimizer, "default_instruction_path", lambda s, r: tmp_path / "i.txt"
    )
    pack_file = pp.prompts_dir() / fe_target.subagent / f"{OPTIMIZER_VARIANT}.txt"
    before = pack_file.read_bytes()
    run = OptimizationRun(
        run_id="later",
        status="optimized",
        instruction="A different instruction from a later run.",
        training=training(6),
    )
    with pytest.raises(pp.PromptPackError, match="differs from the committed"):
        with installed_rung(fe_target, run):
            pass
    assert pack_file.read_bytes() == before
    # Still recoverable: the composed text is on disk under its own run id.
    assert (tmp_path / "i.txt").is_file()


def test_an_undelivered_run_cannot_be_installed(fe_target):
    with pytest.raises(ValueError, match="no instruction"):
        with installed_rung(fe_target, OptimizationRun(run_id="r")):
            pass


# --------------------------------------------------------------------------
# housekeeping
# --------------------------------------------------------------------------


def test_no_model_id_is_hardcoded_in_the_optimizer_module():
    source = (REPO_ROOT / "amw" / "tuning" / "optimizer.py").read_text(encoding="utf-8")
    models = load_all().models
    for key in models.models:
        provider_id = models.spec(key).id_for("vertex")
        assert provider_id not in source, (
            f"provider model ID {provider_id!r} is a literal in optimizer.py; "
            f"model IDs come from config/models.yaml"
        )


def test_the_module_imports_no_provider_sdk_at_module_scope():
    source = (REPO_ROOT / "amw" / "tuning" / "optimizer.py").read_text(encoding="utf-8")
    for line in source.splitlines():
        if line.startswith(("import ", "from ")):
            assert "vertexai" not in line and "pandas" not in line, (
                f"module-scope provider import breaks the zero-credential rule: "
                f"{line!r}"
            )
