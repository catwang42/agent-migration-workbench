"""Rung ``A4-optimizer``: the Vertex AI Prompt Optimizer, wired but not fired.

This module is the whole distance between "SPIKE-S3 is GREEN" and "one command
runs the optimizer rung". Nothing in it has been executed against the service.
Nothing in it produces a number. It exists so that the moment the owner rules
on item 1, ``python cli.py optimize --subagent … --mode live`` is the only
action left.

Why the rung is called ``A4-optimizer``
--------------------------------------

``amw.tuning.ablate`` reserves the extension point under
:data:`~amw.tuning.ablate.VAIPO_RUNG_ID` = ``"A4-prime"`` — the name the T10
card used for a rung nobody had scoped yet. The owner named it
**``A4-optimizer``** on 2026-08-11 when the rung was actually specified, and
that is the name a customer sees beside the hand-tuned rung, so that is the
name this module registers. :data:`LEGACY_RUNG_ID` records the old one rather
than deleting the history: ``ablate.py``'s docstring, ``SPIKES.md`` and
``act1_build_plan.md`` all still say A4′/``A4-prime``, and a reader who
searches for either should land here. No edit was made to ``ablate.py`` —
:func:`~amw.tuning.ablate.register_p1_rung` is a public seam and needed none.

Which VAIPO surface this uses, and which it refuses
---------------------------------------------------

``SPIKES.md`` S3 is precise about this and so is the code:

* ``vertexai.Client().prompts.optimize(...)`` — the **synchronous instruction
  optimizer**. Prompt plus an examples dataframe in, a suggested instruction
  out, in ~124 s on the spike. This is the surface that went GREEN, and it is
  the only one :func:`submit_optimization` will call.
* ``prompt_optimizer.optimize(method="VAPO", …)`` — the data-driven optimizer
  that runs as a long-lived Vertex CustomJob against a GCS bucket. **Never
  exercised.** Asking for it raises: it is a separate go/no-go and S3's GREEN
  does not cover it (:data:`VAIPO_SURFACE`, :func:`submit_optimization`).

That distinction has a consequence worth stating plainly, because the owner's
instruction says "optimize against the JUDGED metric": the synchronous surface
does **not** accept a custom metric. It optimizes toward *target responses*.
So "targeting the judged metric" is implemented the only honest way the GREEN
surface allows — the targets are the gold outputs, and the rung is then
**scored** by the judged metric through the ordinary ladder, which is the
measurement that decides anything. :attr:`OptimizerTarget.objective_note`
carries that sentence into the artifact so nobody reads the rung as "VAIPO
optimised a judge score".

The target is chosen by item 1's ruling, not by this module
------------------------------------------------------------

Owner's rule, encoded in :func:`select_target`:

===========================  ====================================================
cross-check verdict          target
===========================  ====================================================
``VALIDATED``                FE novelty, read against the **judged** FE score
``UNRELIABLE``               QR intent matching, **deterministic**
``INSUFFICIENT`` / no file   nothing runs — the ruling has not happened
===========================  ====================================================

The middle row additionally sets
:attr:`TargetSelection.requires_owner_signoff`: the owner asked to see the FE
reframe *before* a retargeted run proceeds, so the retarget is surfaced and
refused until ``--i-have-the-ruling`` says otherwise. The bottom row is the
state today — ``artifacts/results/crosscheck.json`` does not exist yet.

Real run only. No replay fallback.
----------------------------------

``ablate.py``'s docstring pins this rung as "real run only" per the T10 card,
and this module honours it structurally rather than by comment:
:func:`submit_optimization` and :func:`run_optimizer_rung` both raise on
``mode="replay"``. A replayed optimizer rung would be an instruction nobody
optimized, scored on calls nobody made, printed next to a rung that was — the
exact shape of a fabricated result (ground rule 1).

Importing this module needs no credentials, no ``vertexai``, and no
``pandas``: every provider import is inside a function body, the same way
``amw/adapters/gemini.py`` defers ``google.genai`` (ground rule 4).

The cutoff, and what "attempted, with status" means
----------------------------------------------------

Owner's rule: two hours or a service failure, and the hand-tuned rung is the
candidate while the optimizer is *reported as attempted, with status*. That is
:class:`Deadline` and :class:`OptimizationRun.status` — a real budget enforced
on a worker thread, and a status enum with no success-shaped default. An
optimizer that never ran reports ``not_run``; one that timed out reports
``timed_out`` with the seconds it burned; neither ever yields a rung with
numbers on it. :func:`candidate_rung` names the hand-tuned rung as the
candidate in every non-``optimized`` state, and says why in the same string.

Both rungs stay in the ladder regardless of which one wins, so nothing here
deletes or hides a rung: :class:`OptimizerResult` reports beside the ladder,
never instead of it.
"""

from __future__ import annotations

import contextlib
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal

from pydantic import BaseModel, ConfigDict, Field

from amw.adapters import AdapterRouter
from amw.agents.prompt_packs import (
    VARIANT_SPECS,
    PromptPack,
    PromptPackError,
    VariantSpec,
    build_request,
    load_pack,
    prompts_dir,
)
from amw.agents.schemas import SUBAGENTS
from amw.config import AppConfig, ConfigError, load_all
from amw.datasets.schema import DatasetItem, read_items
from amw.eval.crosscheck import INSUFFICIENT, UNRELIABLE, VALIDATED, CrosscheckResult
from amw.traces.store import ReplayMissError, ReplayStore
from amw.tuning.ablate import (
    P1_RUNGS,
    VAIPO_RUNG_ID,
    AblationResult,
    RungSpec,
    register_p1_rung,
    run_ladder,
)

__all__ = [
    "OPTIMIZER_VERSION",
    "OPTIMIZER_RUNG_ID",
    "LEGACY_RUNG_ID",
    "OPTIMIZER_VARIANT",
    "VAIPO_SURFACE",
    "REFUSED_SURFACES",
    "CUTOFF_SECONDS",
    "TRAINING_SEED",
    "TRAINING_EXAMPLES",
    "MIN_TRAINING_EXAMPLES",
    "TARGETS",
    "OptimizerTarget",
    "TargetSelection",
    "TrainingExample",
    "TrainingSet",
    "Deadline",
    "OptimizationRun",
    "OptimizerResult",
    "candidate_rung",
    "cmd_optimize",
    "compose_prompt_file",
    "crosscheck_verdict",
    "default_crosscheck_path",
    "default_instruction_path",
    "default_result_path",
    "build_training_set",
    "installed_rung",
    "render_lines",
    "run_optimizer_rung",
    "select_target",
    "submit_optimization",
]

#: Bumped when the artifact shape changes, same contract as ABLATION_VERSION.
OPTIMIZER_VERSION = "1"

#: The rung id the owner named on 2026-08-11. See the module docstring.
OPTIMIZER_RUNG_ID = "A4-optimizer"

#: What ``ablate.py``'s extension point still calls it. Kept so a reader who
#: greps for the T10-card name finds this module.
LEGACY_RUNG_ID = VAIPO_RUNG_ID

#: Variant key the generated instruction is registered under. One per subagent
#: is unnecessary — the ladder is run for one subagent at a time — but the name
#: says which mechanism produced the prompt, which matters when it is shown on
#: screen beside a hand-written one.
OPTIMIZER_VARIANT = "gemini_optimizer_v1"

#: The one VAIPO surface SPIKE-S3 proved. Anything else is a separate go/no-go.
VAIPO_SURFACE = "prompts.optimize"

#: Surfaces this module deliberately will not call, and why, in one place so
#: the refusal message can name the reason instead of just saying no.
REFUSED_SURFACES: dict[str, str] = {
    "VAPO": (
        "the data-driven optimizer runs as a long-lived Vertex CustomJob "
        "against a GCS bucket and was NOT exercised by SPIKE-S3. Running it "
        "here would put an unspiked service surface on the demo path."
    ),
}

#: Owner's cutoff: two hours, then the hand-tuned rung is the candidate.
#: Wall-clock seconds, not a threshold on a measurement, so it lives here
#: rather than in ``config/gates.yaml`` (which holds gate bounds only).
CUTOFF_SECONDS = 2 * 60 * 60

#: Deterministic draw of the optimizer's training items, so the set behind a
#: candidate instruction can be re-derived by a reader from the artifact alone.
TRAINING_SEED = 20260811

#: How many worked examples go to the optimizer. SPIKE-S3 used five and the
#: caveat it came back with was that five was too few for the optimizer to
#: infer a boundary rule correctly — so the default here is larger, and the
#: number actually used is recorded on :class:`TrainingSet`.
TRAINING_EXAMPLES = 12

#: Below this, refuse rather than optimize. An instruction inferred from three
#: examples is the S3 failure mode with fewer chances to average out.
MIN_TRAINING_EXAMPLES = 5

REPO_ROOT = Path(__file__).resolve().parents[2]


def default_result_path(subagent: str) -> Path:
    return REPO_ROOT / "artifacts" / "results" / f"optimizer_{subagent}.json"


def default_instruction_path(subagent: str, run_id: str) -> Path:
    """Where the candidate instruction is kept for good.

    Deliberately **not** ``amw/agents/prompts/``. That directory holds prompts
    a human wrote and a human reviewed; a machine-generated instruction filed
    beside them would be indistinguishable from one, and
    ``tests/test_prompts.py`` would start policing a file whose existence
    depends on whether anyone has run the optimizer. The generated file is a
    run artifact — dated, tied to a run id, and staged into the pack directory
    only for the seconds the ladder needs it (:func:`installed_rung`).
    """
    return REPO_ROOT / "artifacts" / "optimizer" / subagent / f"{run_id}.txt"


def default_crosscheck_path() -> Path:
    from amw.eval.crosscheck import default_crosscheck_path as _path

    return _path()


def default_dataset_dir() -> Path:
    return REPO_ROOT / "datasets"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# what the owner's ruling selects
# --------------------------------------------------------------------------


class OptimizerTarget(BaseModel):
    """One thing the optimizer could be pointed at, fully specified up front.

    Both targets are declared before the ruling, deliberately: choosing what to
    optimize after seeing which way the cross-check went is how a retarget
    turns into a search for the flattering result.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    subagent: str
    #: Variant whose instruction is handed to the optimizer as the starting
    #: point, and whose user turn and tool description the generated pack
    #: reuses verbatim. Branching from A0 for the same reason the hand-tuned
    #: novelty rung does — see ``ablate.py``.
    base_variant: str
    branches_from: str
    #: The hand-tuned rung this one is printed beside, and the standing
    #: candidate whenever the optimizer does not deliver.
    hand_tuned_rung: str
    #: Which instrument reads the rung: the rubric judge, or a deterministic
    #: metric from ``amw.eval.metrics``.
    objective: Literal["judged", "deterministic"]
    #: Metric of record. For ``judged`` this is the ladder's judge score; for
    #: ``deterministic`` it is a key in ``deterministic_metrics``.
    metric: str
    rationale: str
    objective_note: str


#: The two rungs item 1's gate chooses between. Nothing else may be optimized
#: without a new entry here, which is a deliberate speed bump.
TARGETS: dict[str, OptimizerTarget] = {
    "fe_novelty_judged": OptimizerTarget(
        key="fe_novelty_judged",
        subagent="feature_extractor",
        base_variant="gemini_naive",
        branches_from="A0",
        hand_tuned_rung="A4-novelty-tool",
        objective="judged",
        metric="judge_score",
        rationale=(
            "FE deterministic accuracy is 1.000 on both Gemini arms "
            "(notes/day1_failures.md): an optimizer pointed at it would report "
            "success and change nothing. The defect is in technical_field and "
            "novelty_statement, which are judge-only."
        ),
        objective_note=(
            "The synchronous VAIPO surface optimizes toward target responses, "
            "not toward a custom metric — it is not given the judge. The "
            "targets are the gold outputs, and the judged FE score is the "
            "instrument the resulting rung is READ against on the ladder. "
            "Nothing here optimizes a judge score directly."
        ),
    ),
    "qr_intent_deterministic": OptimizerTarget(
        key="qr_intent_deterministic",
        subagent="query_rewriter",
        base_variant="gemini_naive",
        branches_from="A0",
        hand_tuned_rung="A1-A3",
        objective="deterministic",
        metric="exact_match_intent",
        rationale=(
            "The retarget the owner named for a non-validated cross-check: QR "
            "intent matching is deterministic, so the rung can be read without "
            "leaning on the instrument the cross-check just failed to confirm."
        ),
        objective_note=(
            "Targets are the gold QueryPlan outputs; the rung is read against "
            "exact_match_intent, a deterministic metric, so no judged number "
            "enters this rung's verdict."
        ),
    ),
}


class TargetSelection(_Strict):
    """Which target the ruling selects, and whether it may proceed unattended."""

    verdict: str | None
    target_key: str | None
    #: True when the owner asked to see this branch before it runs. The retarget
    #: is one of those: "surface the FE reframe to me before proceeding".
    requires_owner_signoff: bool = False
    reason: str
    #: What to put in front of the owner when signoff is required.
    reframe: str | None = None

    @property
    def target(self) -> OptimizerTarget | None:
        return TARGETS[self.target_key] if self.target_key else None


def crosscheck_verdict(
    subagent: str = "feature_extractor", path: str | Path | None = None
) -> str | None:
    """The cross-check's verdict for ``subagent``, or ``None`` if it has not run.

    ``None`` is the state today and it is not a failure: item 1's ruling rests
    on an artifact nobody has produced. Returning ``None`` rather than
    defaulting to :data:`~amw.eval.crosscheck.INSUFFICIENT` keeps "the
    instrument said it could not tell" distinct from "the instrument has not
    been run", which are different things to tell an owner.
    """
    artifact = Path(path) if path else default_crosscheck_path()
    if not artifact.is_file():
        return None
    result = CrosscheckResult.model_validate_json(artifact.read_text(encoding="utf-8"))
    entry = result.for_subagent(subagent)
    return entry.verdict if entry else None


def select_target(verdict: str | None) -> TargetSelection:
    """Apply the owner's gate. Pure function of the verdict — deliberately.

    No file reads, no service calls, no clock: the mapping from ruling to
    target is the part that must be arguable in a review, so it is one table
    lookup a reader can check against the instruction in ten seconds.
    """
    if verdict == VALIDATED:
        return TargetSelection(
            verdict=verdict,
            target_key="fe_novelty_judged",
            reason=(
                f"the cross-check returned {VALIDATED} on feature_extractor, so "
                f"the judged FE gap is real enough to optimize against."
            ),
        )
    if verdict == UNRELIABLE:
        target = TARGETS["qr_intent_deterministic"]
        return TargetSelection(
            verdict=verdict,
            target_key=target.key,
            requires_owner_signoff=True,
            reason=(
                f"the cross-check returned {UNRELIABLE} on feature_extractor, so "
                f"the owner's rule retargets the optimizer to QR intent matching "
                f"(deterministic). That reframe goes to the owner before the run."
            ),
            reframe=(
                "The FE optimizer rung is withdrawn: the judged FE gap it was "
                "aimed at did not survive the second judge, and optimizing "
                "against an instrument two judges disagree on would harden a "
                "measurement artifact into a prompt. The optimizer is retargeted "
                "to query_rewriter intent matching, which is deterministic and "
                "does not depend on the disputed instrument. The FE analysis "
                "then reports the judge disagreement as the finding, per "
                "notes/day1_failures.md (c)."
            ),
        )
    if verdict == INSUFFICIENT:
        return TargetSelection(
            verdict=verdict,
            target_key=None,
            reason=(
                f"the cross-check returned {INSUFFICIENT}: it could not tell "
                f"either way, so neither branch of the owner's gate is open. "
                f"Nothing is optimized."
            ),
        )
    if verdict is None:
        return TargetSelection(
            verdict=None,
            target_key=None,
            reason=(
                "no cross-check artifact exists yet, so item 1's gate has not "
                "been decided. Run `python cli.py crosscheck` first; the target "
                "is chosen by its verdict, not by whoever launches the optimizer."
            ),
        )
    return TargetSelection(
        verdict=verdict,
        target_key=None,
        reason=(
            f"unrecognised cross-check verdict {verdict!r}; expected one of "
            f"{[VALIDATED, UNRELIABLE, INSUFFICIENT]}."
        ),
    )


# --------------------------------------------------------------------------
# the training set — real recorded outputs, never invented ones
# --------------------------------------------------------------------------


class TrainingExample(_Strict):
    """One row of the optimizer's examples dataframe.

    ``model_response`` is what the **current** instruction actually produced on
    this item, read out of ``artifacts/replay/``. SPIKE-S3 makes the point in
    its own comments: the optimizer works from the gap between current and
    target output, so a hand-written "current" output would produce a candidate
    instruction optimizing against a failure that never happened — a fabricated
    result wearing a real result's clothes (ground rule 1).
    """

    item_id: str
    prompt: str
    model_response: str
    target_response: str


class TrainingSet(_Strict):
    """The examples, plus everything a reader needs to re-derive them."""

    subagent: str
    base_variant: str
    seed: int
    requested: int
    examples: list[TrainingExample] = Field(default_factory=list)
    #: Items dropped because the base variant's call was never recorded, with
    #: the reason. Never silently skipped: a training set assembled from
    #: whatever happened to be on disk is not reproducible.
    skipped: dict[str, str] = Field(default_factory=dict)
    #: Training items that are also in the scored split. Fed to
    #: ``RungSpec.few_shot_item_ids`` so ``ablate.py``'s existing contamination
    #: machinery discloses the overlap the same way it does for the hand-tuned
    #: novelty rung — see :func:`installed_rung`.
    overlaps_scored_split: list[str] = Field(default_factory=list)
    #: True when the corpus had non-core items to train on, so the optimizer
    #: never saw an item the ladder scores. False is legal but must be said out
    #: loud — see the note :func:`run_optimizer_rung` attaches.
    held_out: bool = True

    @property
    def size(self) -> int:
        return len(self.examples)


def _render_target(item: DatasetItem) -> str:
    """The gold output as the model would have to emit it."""
    return json.dumps(item.gold, sort_keys=True, indent=2)


def _render_response(trace: Any) -> str:
    """What the base variant actually returned, as text.

    Structured payload first, prose second — the same preference (and the same
    reason) as :func:`amw.eval.runner.judge_candidate`: a model that answered
    in prose did answer, and showing the optimizer a blank would point it at
    the wrong defect.
    """
    payload = trace.output.json_
    if payload is not None:
        return json.dumps(payload, sort_keys=True, indent=2)
    return trace.output.text or ""


def build_training_set(
    target: OptimizerTarget,
    *,
    config: AppConfig | None = None,
    dataset_dir: str | Path | None = None,
    size: int = TRAINING_EXAMPLES,
    seed: int = TRAINING_SEED,
    store: ReplayStore | None = None,
) -> TrainingSet:
    """Assemble the examples dataframe from recorded calls and gold outputs.

    Held-out first, and on this corpus that is achievable:
    :func:`~amw.tuning.ablate.run_ladder` judges the **core** split, so the
    non-core items are ones no rung is scored on and the optimizer can train on
    them without contaminating anything. SPIKE-S3's caveat — never put a VAIPO
    candidate on the demo path without measuring it on items it did not see —
    is honoured by construction rather than by disclosure.

    The overlap path still exists because the corpus is not guaranteed to have
    a non-core pool: a ``-n`` too small, or a re-generation that marks
    everything core, and there is nothing held out. In that case the overlap is
    *recorded* rather than avoided, and :func:`installed_rung` hands it to
    ``RungSpec.few_shot_item_ids`` so the ladder prints it beside the score.
    Dropping the overlapping items instead would give this rung a different
    denominator from every other rung, which ``ablate.py`` already argues is
    the worse of the two problems.

    Runs entirely in replay: no model is called to build a training set.
    """
    cfg = config or load_all()
    directory = Path(dataset_dir) if dataset_dir else default_dataset_dir()
    path = directory / f"{target.subagent}.jsonl"
    if not path.exists():
        raise FileNotFoundError(
            f"no dataset at {path}. The optimizer trains on the corpus that was "
            f"already run and recorded; it does not create one."
        )

    items = list(read_items(path))
    scored = {item.item_id for item in items if item.core}
    pool = [item for item in items if not item.core]
    held_out = bool(pool)
    if not pool:
        # Every item is scored. Legal, disclosed, never silent.
        pool = list(items)

    # Deterministic draw: sorted, then strided, so the same corpus and seed
    # give the same set on any machine and a reader can reproduce it without
    # a random-number implementation detail.
    ordered = sorted(pool, key=lambda item: item.item_id)
    if size < len(ordered):
        offset = seed % len(ordered)
        stride = max(1, len(ordered) // size)
        picked = [ordered[(offset + n * stride) % len(ordered)] for n in range(size)]
        # A stride that wraps can repeat an item; de-duplicate, keeping order.
        seen: dict[str, DatasetItem] = {}
        for item in picked:
            seen.setdefault(item.item_id, item)
        chosen = sorted(seen.values(), key=lambda item: item.item_id)
    else:
        chosen = ordered

    # `store if store is not None` and not `store or ...`: ReplayStore defines
    # __len__, so an empty store is falsy and `or` would silently swap a
    # deliberately-empty test store for the real artifacts/replay/.
    router = AdapterRouter(
        mode="replay",
        models=cfg.models,
        store=store if store is not None else ReplayStore(),
    )
    from amw.eval.runner import prompt_view

    examples: list[TrainingExample] = []
    skipped: dict[str, str] = {}
    for item in chosen:
        request = build_request(
            target.subagent,
            target.base_variant,
            prompt_view(item),
            models=cfg.models,
            item_id=item.item_id,
        )
        try:
            trace = router.complete(request)
        except ReplayMissError as exc:
            skipped[item.item_id] = (
                f"the {target.base_variant} call for this item is not in "
                f"artifacts/replay/ ({str(exc).rstrip('.')})"
            )
            continue
        if trace.status != "ok":
            skipped[item.item_id] = f"the recorded call errored: {trace.error}"
            continue
        response = _render_response(trace)
        if not response.strip():
            skipped[item.item_id] = "the recorded call returned nothing to learn from"
            continue
        examples.append(
            TrainingExample(
                item_id=item.item_id,
                prompt="\n\n".join(item.input.messages),
                model_response=response,
                target_response=_render_target(item),
            )
        )

    return TrainingSet(
        subagent=target.subagent,
        base_variant=target.base_variant,
        seed=seed,
        requested=size,
        examples=examples,
        skipped=skipped,
        overlaps_scored_split=sorted(
            {example.item_id for example in examples} & scored
        ),
        held_out=held_out,
    )


# --------------------------------------------------------------------------
# the cutoff
# --------------------------------------------------------------------------


class Deadline:
    """The owner's two-hour budget, as an object rather than a comment.

    Checked *between* phases and enforced *on* the optimize call, which runs on
    a worker thread so a service that hangs cannot hang the run. The thread is
    not killed on expiry — Python cannot — so the call may still be in flight
    at the service when this returns; that is stated in the status text rather
    than pretended away. What matters for the owner's rule is that at the
    cutoff we stop waiting and the hand-tuned rung becomes the candidate.
    """

    def __init__(self, seconds: float = CUTOFF_SECONDS, *, clock=time.monotonic) -> None:
        if seconds <= 0:
            raise ValueError("the cutoff must be positive; 0 would refuse every run")
        self.seconds = float(seconds)
        self._clock = clock
        self._start = clock()

    @property
    def elapsed(self) -> float:
        return self._clock() - self._start

    def remaining(self) -> float:
        return max(0.0, self.seconds - self.elapsed)

    @property
    def expired(self) -> bool:
        return self.remaining() <= 0.0

    def run(self, call, *, executor: Any = None):
        """Run ``call`` on a worker thread, giving up at the cutoff.

        :raises TimeoutError: the budget ran out first.
        """
        if self.expired:
            raise TimeoutError(
                f"the {self.seconds:.0f}s optimizer budget was already spent "
                f"before the service call started"
            )
        pool = executor or ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(call)
            try:
                return future.result(timeout=self.remaining())
            except FutureTimeout as exc:
                raise TimeoutError(
                    f"no candidate instruction after {self.elapsed:.0f}s of a "
                    f"{self.seconds:.0f}s budget; the call may still be running "
                    f"at the service, but this run stops waiting for it"
                ) from exc
        finally:
            if executor is None:
                pool.shutdown(wait=False)


# --------------------------------------------------------------------------
# the job
# --------------------------------------------------------------------------

OptimizerStatus = Literal[
    "not_run",
    "refused",
    "optimized",
    "no_candidate",
    "timed_out",
    "service_error",
]


class OptimizationRun(_Strict):
    """One attempt at the optimizer, in whatever state it ended in.

    ``status`` has no success-shaped default: an :class:`OptimizationRun` that
    nobody filled in reports ``not_run``, and every field that would carry a
    result is ``None``. There is no state in which this object holds a number
    that was not produced by the service.
    """

    optimizer_version: str = OPTIMIZER_VERSION
    run_id: str
    surface: str = VAIPO_SURFACE
    target_key: str | None = None
    status: OptimizerStatus = "not_run"
    #: One sentence for the ladder report and the workshop screen.
    detail: str = "the optimizer has not been run"

    #: The candidate instruction, verbatim. Present only on ``optimized``.
    instruction: str | None = None
    #: The optimizer's own diagnosis of the starting instruction, when it
    #: returns one. Shown to the customer: it is the teaching moment, and it
    #: is the service's text, not ours.
    guidelines: list[str] = Field(default_factory=list)
    optimization_type: str | None = None

    training: TrainingSet | None = None
    cutoff_seconds: float = CUTOFF_SECONDS
    #: Wall-clock spent, measured. ``None`` when nothing was attempted.
    elapsed_seconds: float | None = None
    started_at: str | None = None
    project: str | None = None
    region: str | None = None
    instruction_path: str | None = None

    @property
    def delivered(self) -> bool:
        return self.status == "optimized" and bool(self.instruction)

    def status_line(self) -> str:
        """The "attempted, with status" line the owner asked for."""
        spent = (
            "" if self.elapsed_seconds is None else f" after {self.elapsed_seconds:.0f}s"
        )
        return f"rung {OPTIMIZER_RUNG_ID}: {self.status}{spent} — {self.detail}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _extract_candidate(response: Any) -> dict[str, Any]:
    """Pull the instruction and guidelines out of an ``OptimizeResponse``.

    Read defensively through ``getattr``, exactly as ``scripts/spike_s3_vaipo.py``
    does: this is a preview surface, and a field rename should degrade to "no
    candidate returned" rather than an ``AttributeError`` two hours into a
    workshop.
    """
    parsed = getattr(response, "parsed_response", None)
    instruction = getattr(parsed, "suggested_prompt", None) if parsed else None
    guidelines: list[str] = []
    raw = getattr(parsed, "applicable_guidelines", None) if parsed else None
    for entry in raw or []:
        text = (
            getattr(entry, "guideline", None)
            or getattr(entry, "description", None)
            or str(entry)
        )
        if text:
            guidelines.append(str(text))
    return {
        "instruction": (instruction or "").strip() or None,
        "guidelines": guidelines,
        "optimization_type": getattr(parsed, "optimization_type", None),
    }


def _import_vaipo() -> tuple[Any, Any, Any]:
    """``(pandas, vertexai, vertexai._genai.types.common)``, on first live use.

    Never at module scope: ``import amw.tuning.optimizer`` must work with no
    SDK and no ADC (ground rule 4). A missing SDK is reported as a config
    problem in the same voice as ``amw/adapters/gemini.py`` — one actionable
    line, not a traceback in front of a customer.
    """
    try:
        import pandas as pd
        import vertexai
        from vertexai._genai.types import common as vtypes
    except ImportError as exc:
        raise ConfigError(
            f"the Vertex AI Prompt Optimizer needs the google-cloud-aiplatform "
            f"SDK, which is not importable ({exc}). `pip install "
            f"google-cloud-aiplatform`. Nothing was submitted."
        ) from exc
    return pd, vertexai, vtypes


def submit_optimization(
    target: OptimizerTarget,
    training: TrainingSet,
    *,
    mode: str,
    deadline: Deadline | None = None,
    surface: str = VAIPO_SURFACE,
    project: str | None = None,
    region: str | None = None,
    client: Any = None,
) -> OptimizationRun:
    """Submit one synchronous VAIPO job and return its outcome.

    Never raises for a service problem: every failure becomes a status on the
    returned :class:`OptimizationRun`, because the owner's rule is that a
    failed optimizer is *reported as attempted*, not that it stops the ladder.
    Exceptions are reserved for caller errors — replay mode, an unspiked
    surface, a training set too small to be worth a job.

    :param client: a pre-built ``vertexai.Client`` (tests inject a stub;
        nothing else should need this).
    """
    if surface in REFUSED_SURFACES:
        raise ConfigError(
            f"VAIPO surface {surface!r} is not available to this build: "
            f"{REFUSED_SURFACES[surface]} Use {VAIPO_SURFACE!r}, which "
            f"SPIKES.md S3 marks GREEN."
        )
    if surface != VAIPO_SURFACE:
        raise ConfigError(
            f"unknown VAIPO surface {surface!r}; this build calls {VAIPO_SURFACE!r} only."
        )
    if mode == "replay":
        raise ConfigError(
            f"rung {OPTIMIZER_RUNG_ID} is real-run-only (T10 card, and the "
            f"'real run only' note in amw/tuning/ablate.py). There is no replay "
            f"fallback and there must not be one: a replayed optimizer rung "
            f"would be an instruction nobody optimized, printed beside a rung "
            f"that was. Use --mode live."
        )

    run = OptimizationRun(
        run_id=_run_id(),
        target_key=target.key,
        surface=surface,
        training=training,
        started_at=_now(),
    )
    if training.size < MIN_TRAINING_EXAMPLES:
        run.status = "refused"
        run.detail = (
            f"only {training.size} training example(s) could be assembled from "
            f"recorded {target.base_variant} calls, below the {MIN_TRAINING_EXAMPLES} "
            f"minimum. SPIKE-S3's caveat was that a small target set is exactly "
            f"how the optimizer encodes a confidently wrong rule; re-record the "
            f"base variant before trying again."
        )
        return run

    budget = deadline or Deadline()
    run.cutoff_seconds = budget.seconds

    import os

    run.project = project or os.environ.get("PROJECT_ID")
    run.region = region or os.environ.get("REGION")
    if not run.project or not run.region:
        run.status = "refused"
        run.detail = (
            "PROJECT_ID and REGION must be set for a live optimizer job (see "
            ".env.example). Nothing was submitted."
        )
        return run

    try:
        _pd, _vertexai, _vtypes = _import_vaipo()
    except ConfigError as exc:
        run.status = "refused"
        run.detail = str(exc)
        return run

    base = load_pack(target.subagent, target.base_variant)

    def _call() -> Any:
        pd, vertexai, vtypes = _pd, _vertexai, _vtypes

        frame = pd.DataFrame(
            {
                "prompt": [e.prompt for e in training.examples],
                "model_response": [e.model_response for e in training.examples],
                "target_response": [e.target_response for e in training.examples],
            }
        )
        vertex = client or vertexai.Client(project=run.project, location=run.region)
        return vertex.prompts.optimize(
            prompt=base.system,
            config=vtypes.OptimizeConfig(
                optimization_target=(
                    vtypes.OptimizeTarget.OPTIMIZATION_TARGET_FEW_SHOT_TARGET_RESPONSE
                ),
                examples_dataframe=frame,
            ),
        )

    try:
        response = budget.run(_call)
    except TimeoutError as exc:
        run.status = "timed_out"
        run.elapsed_seconds = budget.elapsed
        run.detail = (
            f"{exc} Per the owner's cutoff the hand-tuned rung "
            f"{target.hand_tuned_rung} is the candidate; this rung is reported "
            f"as attempted."
        )
        return run
    except Exception as exc:  # noqa: BLE001 — a service failure is a status
        run.status = "service_error"
        run.elapsed_seconds = budget.elapsed
        run.detail = (
            f"{type(exc).__name__}: {exc}. Per the owner's cutoff the hand-tuned "
            f"rung {target.hand_tuned_rung} is the candidate; this rung is "
            f"reported as attempted."
        )
        return run

    run.elapsed_seconds = budget.elapsed
    candidate = _extract_candidate(response)
    if not candidate["instruction"]:
        run.status = "no_candidate"
        run.detail = (
            "the service returned no suggested_prompt. Nothing was optimized and "
            f"no rung is added; {target.hand_tuned_rung} remains the candidate."
        )
        return run

    run.status = "optimized"
    run.instruction = candidate["instruction"]
    run.guidelines = candidate["guidelines"]
    run.optimization_type = (
        str(candidate["optimization_type"]) if candidate["optimization_type"] else None
    )
    run.detail = (
        f"a candidate instruction came back in {run.elapsed_seconds:.0f}s and is "
        f"about to be run as rung {OPTIMIZER_RUNG_ID} beside "
        f"{target.hand_tuned_rung}. Neither rung is dropped whichever wins."
    )
    return run


def candidate_rung(target: OptimizerTarget, run: OptimizationRun) -> str:
    """Which rung is the candidate right now, and why, in one sentence.

    The owner's fallback rule as code. Note what this does **not** do: when the
    optimizer *did* deliver, it does not pick a winner. Nothing here has been
    measured, and choosing between two rungs is the ladder's job, on numbers.
    """
    if run.delivered:
        return (
            f"{target.hand_tuned_rung} and {OPTIMIZER_RUNG_ID} are both in the "
            f"ladder and neither is the candidate until the ladder scores them. "
            f"Read them side by side."
        )
    return (
        f"{target.hand_tuned_rung} (hand-tuned) is the candidate: the optimizer "
        f"is {run.status} — {run.detail}"
    )


# --------------------------------------------------------------------------
# prompt-file plumbing
# --------------------------------------------------------------------------


def compose_prompt_file(base: PromptPack, instruction: str) -> str:
    """The generated pack: the optimizer's instruction, everything else A0's.

    Only the ``system`` section moves. The user template and the tool
    description are copied byte-for-byte from the base variant, so the
    ``A0`` → ``A4-optimizer`` delta is the instruction and nothing else — the
    same discipline the Feature Extractor 2×2 applies to its own cells
    (``prompt_packs.py``: the ``_schema`` files differ only in the lines that
    name the emission mechanism).
    """
    body = instruction.strip()
    if not body:
        raise PromptPackError("the optimizer returned an empty instruction")
    sections = [f"=== system ===\n{body}\n"]
    if base.chunk_template is not None:
        sections.append(f"=== chunk ===\n{base.chunk_template}\n")
    sections.append(f"=== user ===\n{base.user_template}\n")
    if base.tool_description is not None:
        sections.append(f"=== tool_description ===\n{base.tool_description}\n")
    return "\n".join(sections)


@contextlib.contextmanager
def installed_rung(
    target: OptimizerTarget,
    run: OptimizationRun,
    *,
    variant: str = OPTIMIZER_VARIANT,
    rung_id: str = OPTIMIZER_RUNG_ID,
) -> Iterator[RungSpec]:
    """Make the generated rung runnable for the duration of the block.

    Three registrations, all undone on the way out:

    1. the variant into :data:`~amw.agents.prompt_packs.VARIANT_SPECS`, at run
       time only — a variant declared at import time would need a prompt file
       committed beside the hand-written ones, and there is no honest file to
       commit before the optimizer has run;
    2. the prompt file, staged into the pack directory because
       :func:`~amw.agents.prompt_packs.load_pack` reads from there and takes no
       override. The durable copy is already in ``artifacts/optimizer/``; this
       one is scaffolding and is removed in ``finally``, so
       ``tests/test_prompts.py``'s stray-file guard still holds afterwards;
    3. the rung itself, through the public
       :func:`~amw.tuning.ablate.register_p1_rung` seam — ``ablate.py`` needed
       no edit.

    The training-set overlap with the scored split rides in on
    ``RungSpec.few_shot_item_ids``, which is what ``run_ladder`` turns into
    ``RungRecord.leaked_example_items`` and a note in the artifact. The
    optimizer saw those items' gold answers; the ladder says so next to the
    score, exactly as it does for the hand-tuned novelty rung.
    """
    if not run.delivered:
        raise ValueError(
            f"cannot install rung {rung_id}: the optimizer is {run.status!r} and "
            f"produced no instruction. A rung with no prompt has nothing to run."
        )
    assert run.instruction is not None  # delivered implies an instruction

    base = load_pack(target.subagent, target.base_variant)
    text = compose_prompt_file(base, run.instruction)

    durable = default_instruction_path(target.subagent, run.run_id)
    durable.parent.mkdir(parents=True, exist_ok=True)
    durable.write_text(text, encoding="utf-8")
    run.instruction_path = str(durable)

    staged = prompts_dir() / target.subagent / f"{variant}.txt"
    if staged.exists():
        raise PromptPackError(
            f"{staged} already exists. That file is staged per-run and removed "
            f"afterwards, so a leftover means a previous optimizer run died "
            f"mid-ladder. Inspect it against artifacts/optimizer/ and delete it "
            f"by hand — reusing it would run this rung on a stale instruction."
        )

    spec = VariantSpec(
        output_mode=base.output_mode,
        model_role=base.model_role,
        rung=rung_id,
        description=(
            f"Vertex AI Prompt Optimizer instruction (run {run.run_id}), "
            f"{target.base_variant}'s user turn and emission mechanism unchanged."
        ),
        subagents=(target.subagent,),
    )
    rung = RungSpec(
        rung=rung_id,
        label=(
            f"Optimizer: a VAIPO-suggested instruction over {target.base_variant}, "
            f"read against {target.metric}"
        ),
        variant=variant,
        branches_from=target.branches_from,
        few_shot_item_ids=tuple(run.training.overlaps_scored_split if run.training else ()),
    )

    previous_variant = VARIANT_SPECS.get(variant)
    previous_rungs = P1_RUNGS.get(target.subagent)
    VARIANT_SPECS[variant] = spec
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text(text, encoding="utf-8")
    load_pack.cache_clear()
    try:
        register_p1_rung(target.subagent, rung)
        yield rung
    finally:
        staged.unlink(missing_ok=True)
        if previous_variant is None:
            VARIANT_SPECS.pop(variant, None)
        else:
            VARIANT_SPECS[variant] = previous_variant
        if previous_rungs is None:
            P1_RUNGS.pop(target.subagent, None)
        else:
            P1_RUNGS[target.subagent] = previous_rungs
        load_pack.cache_clear()


# --------------------------------------------------------------------------
# the one command
# --------------------------------------------------------------------------


class OptimizerResult(_Strict):
    """``artifacts/results/optimizer_{subagent}.json`` — the attempt, recorded.

    Written whatever happened, including when nothing did. The owner asked for
    the optimizer to be "reported as attempted, with status"; a file that only
    exists on success cannot do that.
    """

    optimizer_version: str = OPTIMIZER_VERSION
    subagent: str | None = None
    selection: TargetSelection
    run: OptimizationRun
    candidate: str
    rung_id: str = OPTIMIZER_RUNG_ID
    legacy_rung_id: str = LEGACY_RUNG_ID
    mode: str = "live"
    #: The ladder run for this rung, when it ran. ``None`` means no rung was
    #: scored — never a rung scored at zero.
    ablation: AblationResult | None = None
    written_at: str = Field(default_factory=_now)
    notes: list[str] = Field(default_factory=list)


def run_optimizer_rung(
    *,
    mode: str,
    config: AppConfig | None = None,
    customer: str | None = None,
    subagent: str = "feature_extractor",
    verdict: str | None = None,
    crosscheck_path: str | Path | None = None,
    have_ruling: bool = False,
    dataset_dir: str | Path | None = None,
    n: int | None = None,
    size: int = TRAINING_EXAMPLES,
    seed: int = TRAINING_SEED,
    cutoff_seconds: float = CUTOFF_SECONDS,
    out_path: str | Path | None = None,
    write: bool = True,
    run_ladder_after: bool = True,
    client: Any = None,
    deadline: Deadline | None = None,
) -> OptimizerResult:
    """Optimize, then run the resulting rung on the full split, live.

    The single command behind ``cli.py optimize``. Every stop is a status on
    the returned result rather than an exception, except the two that are
    caller errors: replay mode and an unspiked surface.

    :param verdict: overrides the cross-check artifact. For tests and for an
        owner who has ruled but not yet re-run ``cli.py crosscheck``.
    :param have_ruling: acknowledges a retarget that
        :attr:`TargetSelection.requires_owner_signoff` flags. Without it the
        retargeted run refuses and prints the reframe instead.
    """
    if mode == "replay":
        raise ConfigError(
            f"rung {OPTIMIZER_RUNG_ID} is real-run-only and has no replay "
            f"fallback (T10 card; amw/tuning/ablate.py). Use --mode live."
        )
    cfg = config or load_all(customer=customer)

    ruling = verdict if verdict is not None else crosscheck_verdict(
        subagent, crosscheck_path
    )
    selection = select_target(ruling)
    notes: list[str] = []

    def _stop(status: OptimizerStatus, detail: str) -> OptimizerResult:
        run = OptimizationRun(
            run_id=_run_id(),
            target_key=selection.target_key,
            status=status,
            detail=detail,
        )
        target = selection.target
        result = OptimizerResult(
            subagent=target.subagent if target else None,
            selection=selection,
            run=run,
            candidate=(
                candidate_rung(target, run)
                if target
                else "no target is selected, so no rung changes hands."
            ),
            mode=mode,
            notes=notes,
        )
        return _write(result, out_path, write=write)

    target = selection.target
    if target is None:
        return _stop("refused", selection.reason)
    if selection.requires_owner_signoff and not have_ruling:
        notes.append(selection.reframe or "")
        return _stop(
            "refused",
            f"{selection.reason} Re-run with --i-have-the-ruling once the owner "
            f"has seen the reframe printed above.",
        )

    training = build_training_set(
        target, config=cfg, dataset_dir=dataset_dir, size=size, seed=seed
    )
    if not training.held_out:
        notes.append(
            f"{target.subagent}: no item in this corpus is outside the scored "
            f"split, so the optimizer's training items are also scored items. "
            f"The overlap is listed on the rung as leaked_example_items and its "
            f"score is optimistic on those items by construction — SPIKE-S3's "
            f"caveat about measuring a candidate on held-out items cannot be "
            f"honoured on this corpus."
        )
    for item_id, reason in sorted(training.skipped.items()):
        notes.append(f"training item {item_id} dropped: {reason}")

    run = submit_optimization(
        target,
        training,
        mode=mode,
        deadline=deadline or Deadline(cutoff_seconds),
        client=client,
    )
    result = OptimizerResult(
        subagent=target.subagent,
        selection=selection,
        run=run,
        candidate=candidate_rung(target, run),
        mode=mode,
        notes=notes,
    )
    if not run.delivered or not run_ladder_after:
        if run.delivered:
            notes.append(
                "the ladder was not run: the instruction is in "
                f"{run.instruction_path or 'artifacts/optimizer/'} and the rung "
                "is unmeasured."
            )
        return _write(result, out_path, write=write)

    with installed_rung(target, run) as rung:
        notes.append(
            f"rung {rung.rung} ran on variant {rung.variant}; the instruction is "
            f"kept at {run.instruction_path}. The staged copy under "
            f"amw/agents/prompts/ is removed once the rung has run."
        )
        result.ablation = run_ladder(
            target.subagent,
            mode=mode,
            config=cfg,
            n=n,
            rungs=[rung.rung],
            dataset_dir=dataset_dir,
            run_judge=target.objective == "judged",
        )
    return _write(result, out_path, write=write)


def _write(
    result: OptimizerResult, out_path: str | Path | None, *, write: bool
) -> OptimizerResult:
    if not write:
        return result
    path = Path(out_path) if out_path else default_result_path(
        result.subagent or "unselected"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return result


def render_lines(result: OptimizerResult) -> list[str]:
    """The attempt as console lines. Never prints a number nobody measured."""
    from amw.tuning.ablate import format_rung

    run = result.run
    lines = [
        f"=== {result.rung_id} (was {result.legacy_rung_id}) — "
        f"{result.subagent or 'no subagent selected'} ===",
        f"  ruling      : {result.selection.verdict or 'the cross-check has not run'}",
        f"  target      : {result.selection.target_key or 'none'}",
        f"  why         : {result.selection.reason}",
        f"  surface     : {run.surface}",
        f"  {run.status_line()}",
        f"  candidate   : {result.candidate}",
    ]
    if result.selection.reframe and result.selection.requires_owner_signoff:
        # Printed whether or not --i-have-the-ruling was passed: if it was not,
        # this is the thing the owner has to read; if it was, it is the record
        # of what they agreed to. The heading has to be true in both cases.
        lines += [
            "",
            "  THE RETARGET, AS PUT TO THE OWNER:",
            f"    {result.selection.reframe}",
        ]
    if run.training is not None:
        training = run.training
        lines.append(
            f"  training    : {training.size}/{training.requested} examples from "
            f"recorded {training.base_variant} calls, seed {training.seed}, "
            f"{'held out' if training.held_out else 'NOT held out'}"
        )
        if training.overlaps_scored_split:
            lines.append(
                f"                overlap with the scored split: "
                f"{', '.join(training.overlaps_scored_split)}"
            )
    if run.guidelines:
        lines += ["", "  optimizer's diagnosis of the starting instruction:"]
        lines += [f"    - {g}" for g in run.guidelines]
    if run.instruction_path:
        lines.append(f"  instruction : {run.instruction_path}")
    if result.ablation is not None:
        lines.append("")
        for record in result.ablation.rungs:
            lines += [f"  {line}" for line in format_rung(record)]
    else:
        lines.append("  ladder      : not run — this rung carries no numbers")
    return lines


def cmd_optimize(args: Any, cfg: Any) -> int:
    """``python cli.py optimize --subagent feature_extractor --mode live``.

    Exit codes: 0 when the optimizer delivered and the rung ran; 3 when the
    attempt is recorded but no rung exists (refused, timed out, service error)
    — non-zero because a caller scripting this should not read "the optimizer
    ran" out of a run that did not; 2 for a caller error.
    """
    subagent = getattr(args, "subagent", None) or "feature_extractor"
    if subagent not in SUBAGENTS:
        print(
            f"unknown subagent {subagent!r}; expected one of {list(SUBAGENTS)}",
            file=sys.stderr,
        )
        return 2
    try:
        result = run_optimizer_rung(
            mode=args.mode,
            config=cfg,
            subagent=subagent,
            verdict=getattr(args, "verdict", None),
            crosscheck_path=getattr(args, "crosscheck", None),
            have_ruling=bool(getattr(args, "i_have_the_ruling", False)),
            dataset_dir=getattr(args, "dataset_dir", None),
            n=getattr(args, "n", None),
            size=getattr(args, "examples", None) or TRAINING_EXAMPLES,
            cutoff_seconds=getattr(args, "cutoff", None) or CUTOFF_SECONDS,
            out_path=getattr(args, "out", None),
            run_ladder_after=not getattr(args, "no_ladder", False),
        )
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    for line in render_lines(result):
        print(line)
    for note in result.notes:
        if note:
            print(f"note: {note}", file=sys.stderr)
    return 0 if result.ablation is not None else 3
