"""The A0–A4 ablation ladder: one subagent, several rungs, one artifact.

A rung is a prompt-pack variant run over the subagent's **core split** and
scored exactly the way ``cli.py phase2`` scores it — same
:func:`amw.eval.runner.run_arm`, same
:func:`amw.eval.metrics.deterministic_metrics`, same judge at k=2, same
bootstrap. That reuse is the whole design constraint: a rung measured by a
parallel eval path would not be comparable to the phase-2 baseline it is meant
to be read against, and the ladder's only job is to make those numbers
comparable.

The ladder per subagent
-----------------------

======================  ============================  ===================
rung                    variant                       what it adds
======================  ============================  ===================
``baseline``            ``claude_baseline``           the incumbent, for reference
``A0``                  ``gemini_naive``              endpoint swap, nothing else
``A1-A3``               ``gemini_tuned_v1``           Markdown + system/user split
                                                      + response_schema + few-shots,
                                                      as one bundle
======================  ============================  ===================

Feature Extractor carries three more rungs. See below.

The Feature Extractor 2×2 — and the hypothesis it tests
-------------------------------------------------------

Measured, from ``artifacts/results/phase2_n70.json``: FE deterministic accuracy
is saturated on both Gemini arms, while the judge puts Claude above Gemini
naive, and puts ``gemini_tuned_v1`` **below** naive. The bundled A1–A3 rung is
a regression on this subagent, and the failure cluster behind the gap is
``novelty_statement``: Gemini applies the pack's abstention rule correctly and
returns null on documents that have no discussion section, missing the patent
convention that independent claim 1 *is* the statement of novelty
(``notes/day1_failures.md``).

So the new rung branches from **A0, not from A1–A3** — building on a rung that
regressed would inherit the regression.

A1–A3 also changed the prompt and the output mode together, so their effects
are inseparable in that artifact. The four cells separate them:

============================  ==============  ===================  ===========
rung                          prompt          output mode          isolates
============================  ==============  ===================  ===========
``A0``                        naive           tool                 (reference)
``A0-schema``                 naive           ``response_schema``  the mode
``A4-novelty-tool``           naive + rule    tool                 the prompt
``A4-novelty-schema``         naive + rule    ``response_schema``  both
============================  ==============  ===================  ===========

**Hypothesis (NOT a result — nothing below has been measured).** Stating it
here, in code, rather than in an artifact, because an artifact is a record of
what happened and this is a prediction about what might:

    Adding the novelty rule to the *naive* prompt raises the FE judged score
    above ``gemini_naive``'s, and the effect of the output-mode change alone
    (``A0`` → ``A0-schema``) is small next to the effect of the prompt change
    alone (``A0`` → ``A4-novelty-tool``).

**Falsification.** Any one of these kills it, and the rung must then be
reported as tried-and-failed rather than quietly dropped:

1. ``A4-novelty-tool``'s judged CI lower bound does not clear ``A0``'s point
   estimate — the rule did not buy anything measurable.
2. ``hallucination_rate`` moves off 0.000, or ``answered_precision`` off 1.000,
   on either novelty rung — the rung bought judge score with fabrication, which
   is disqualifying regardless of the judged number (ground rule 1, and the
   guard named in ``notes/day1_failures.md``).
3. ``A0`` → ``A0-schema`` moves the judged score at least as much as ``A0`` →
   ``A4-novelty-tool`` does — then what A1–A3 measured was mostly the output
   mode, and the prompt-level story about ``novelty_statement`` is not the
   explanation.

The rung requires live calls that do not exist in ``artifacts/replay/``. Until
somebody records them, running the ladder in replay reports these rungs as
``no_recordings`` with no numbers attached — see :class:`RungRecord.status`.
That is the honest state, and it is why nothing here has a placeholder value.

Contamination to carry forward
------------------------------

The novelty prompt's worked example *was* corpus item ``fe-0003``, per the
owner's instruction in ``notes/day1_failures.md``. That item is in the scored
core split, so those two rungs quoted one of the items they are scored on. On
2026-08-11 — before either rung had been measured — the owner ruled it out and
it was replaced with a freshly authored example that is not drawn from the
corpus (``notes/fe_worked_example_swap.md``). The rungs therefore quote nothing
they are scored on, and :data:`FEW_SHOT_ITEM_IDS` is empty for both.

The machinery that caught it stays. :data:`FEW_SHOT_ITEM_IDS` drives
``RungRecord.leaked_example_items``, which lists whatever overlap a run
actually had. If overlap ever returns it is recorded, not auto-excluded —
dropping the overlapping items would give the affected rungs a different
denominator from every other rung, which is a worse problem than a disclosed
one.

Extension point: rung A4′ (VAIPO)
---------------------------------

:data:`P1_RUNGS` is empty and stays empty in this build. It is where rung
``A4-prime`` goes — a Vertex AI Prompt Optimizer instruction, optimised against
the *judged* FE score rather than the saturated deterministic one — once
``SPIKES.md`` marks SPIKE-S3 GREEN (CLAUDE.md ground rule 6: P1 only after its
spike is green, and never P2). Registering it is
:func:`register_p1_rung`; it needs a prompt file like any other rung, and it is
"real run only" per the T10 card, so it must never be given a replay fallback.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field

from amw.adapters import AdapterRouter, merge_windows
from amw.agents.prompt_packs import (
    VARIANT_SPECS,
    load_pack,
    resolve_model,
    variants_for,
)
from amw.agents.schemas import SUBAGENTS
from amw.config import AppConfig, load_all
from amw.datasets.schema import DatasetItem, read_items
from amw.eval.judge import Judge
from amw.eval.runner import ArmResult, run_arm
from amw.traces.store import ReplayMissError, ReplayStore

__all__ = [
    "ABLATION_VERSION",
    "VAIPO_RUNG_ID",
    "FEW_SHOT_ITEM_IDS",
    "COMMON_RUNGS",
    "SUBAGENT_RUNGS",
    "P1_RUNGS",
    "RungSpec",
    "RungProvenance",
    "RungRecord",
    "AblationResult",
    "ladder_for",
    "register_p1_rung",
    "run_ladder",
    "default_results_path",
    "format_rung",
]

#: Bumped when the artifact shape changes. Records are *appended* across runs,
#: so a file mixing shapes would be unreadable; :func:`run_ladder` refuses to
#: append to a file written under a different version.
ABLATION_VERSION = "1"

#: The P1 rung this module deliberately does not build. See the docstring.
VAIPO_RUNG_ID = "A4-prime"

REPO_ROOT = Path(__file__).resolve().parents[2]


def default_results_path(subagent: str) -> Path:
    return REPO_ROOT / "artifacts" / "results" / f"ablation_{subagent}.json"


def default_dataset_dir() -> Path:
    return REPO_ROOT / "datasets"


# --------------------------------------------------------------------------
# the ladder
# --------------------------------------------------------------------------


class RungSpec(BaseModel):
    """One rung: which variant, called what, branching from where."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Short id used as a column header and a dict key: "A0", "A1-A3".
    rung: str
    #: The sentence a customer reads next to the number.
    label: str
    variant: str
    #: Rung id this one is built on top of, so a reader can see the ladder is
    #: a tree and not a line. ``None`` for the incumbent and for A0.
    branches_from: str | None = None
    #: Corpus item ids used as few-shots in this rung's prompt. Empty for a
    #: prompt whose examples are invented rather than lifted from the corpus.
    few_shot_item_ids: tuple[str, ...] = ()

    @property
    def output_mode(self) -> str:
        return VARIANT_SPECS[self.variant].output_mode


#: Corpus items quoted as worked examples, per variant.
#:
#: Both novelty rungs quoted ``fe-0003`` until 2026-08-11, per the owner's
#: original instruction in ``notes/day1_failures.md``. That item is scored, so
#: the rungs were shown one of their own answer keys. The owner ruled the
#: example out before these rungs were ever measured and it was replaced with a
#: freshly authored one — a clamp-on ultrasonic flow meter, subject matter that
#: appears nowhere in the corpus. See ``notes/fe_worked_example_swap.md``.
#:
#: The mapping stays, empty, rather than being deleted: it is what drives
#: ``RungRecord.leaked_example_items``, and a rung that quotes nothing should
#: say so by measuring zero overlap, not by having no check.
FEW_SHOT_ITEM_IDS: dict[str, tuple[str, ...]] = {
    "gemini_novelty_v1_tool": (),
    "gemini_novelty_v1_schema": (),
}

#: Every subagent runs these, in this order.
COMMON_RUNGS: tuple[RungSpec, ...] = (
    RungSpec(
        rung="baseline",
        label="Incumbent: the customer's XML prompt on Claude, emit_* tool",
        variant="claude_baseline",
    ),
    RungSpec(
        rung="A0",
        label="Naive endpoint swap: the same prompt bytes on Gemini, same tool",
        variant="gemini_naive",
    ),
    RungSpec(
        rung="A1-A3",
        label=(
            "Bundled: Markdown restructure + system/user split + enforced "
            "response_schema + two recalibrated few-shots"
        ),
        variant="gemini_tuned_v1",
        branches_from="A0",
    ),
)

#: Extra rungs a single subagent has. Feature Extractor's four cells (A0 is in
#: COMMON_RUNGS and serves as the fourth) unbundle prompt from output mode.
SUBAGENT_RUNGS: dict[str, tuple[RungSpec, ...]] = {
    "feature_extractor": (
        RungSpec(
            rung="A0-schema",
            label="Mode only: A0's prompt, enforced response_schema instead of the tool",
            variant="gemini_naive_schema",
            branches_from="A0",
        ),
        RungSpec(
            rung="A4-novelty-tool",
            label=(
                "Prompt only: A0 plus the novelty_statement rule (claim 1 is the "
                "point of novelty; numeric limits survive) and one worked example"
            ),
            variant="gemini_novelty_v1_tool",
            branches_from="A0",
            few_shot_item_ids=FEW_SHOT_ITEM_IDS["gemini_novelty_v1_tool"],
        ),
        RungSpec(
            rung="A4-novelty-schema",
            label="Both: the novelty prompt under the enforced response_schema",
            variant="gemini_novelty_v1_schema",
            branches_from="A4-novelty-tool",
            few_shot_item_ids=FEW_SHOT_ITEM_IDS["gemini_novelty_v1_schema"],
        ),
    ),
}

#: P1 rungs, registered only once their spike is GREEN. Empty in this build —
#: see the module docstring's extension-point section.
P1_RUNGS: dict[str, tuple[RungSpec, ...]] = {}


def register_p1_rung(subagent: str, spec: RungSpec) -> None:
    """Add a P1 rung to the ladder for ``subagent``.

    The named extension point for rung ``A4-prime`` (VAIPO). Deliberately a
    function rather than a literal in :data:`SUBAGENT_RUNGS`: a P1 rung must
    not appear in the default ladder until ``SPIKES.md`` says its spike is
    GREEN, and a caller that has checked that can say so in one line.
    """
    if subagent not in SUBAGENTS:
        raise ValueError(f"unknown subagent {subagent!r}; expected one of {list(SUBAGENTS)}")
    P1_RUNGS[subagent] = P1_RUNGS.get(subagent, ()) + (spec,)


def ladder_for(subagent: str) -> tuple[RungSpec, ...]:
    """Every rung defined for ``subagent``, in report order."""
    rungs = COMMON_RUNGS + SUBAGENT_RUNGS.get(subagent, ()) + P1_RUNGS.get(subagent, ())
    available = set(variants_for(subagent))
    missing = [rung.variant for rung in rungs if rung.variant not in available]
    if missing:
        raise ValueError(
            f"ladder for {subagent!r} names variant(s) {missing} that the "
            f"subagent has no prompt pack for; it has {sorted(available)}."
        )
    return rungs


# --------------------------------------------------------------------------
# result shapes
# --------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RungProvenance(_Strict):
    """Where one rung's numbers came from.

    Per *record*, not per file: records accumulate across runs and days, so a
    reader looking at row four has to be able to date it, seed it and see
    whether it was live or replayed without reference to any other row
    (CLAUDE.md ground rule 2).
    """

    customer: str
    mode: str
    region: str
    dataset_provenance: str
    dataset_seed: int
    generator_version: str
    #: Items in the split this rung ran over, and which split that was.
    items: int
    split: str
    bootstrap_seed: int
    judge_repeats: int
    judge_model: str | None = None
    judge_prompt_version: str | None = None
    adapters: dict[str, str] = Field(default_factory=dict)
    #: Wall-clock start, live/hybrid only. Replay leaves it None: no run
    #: happened, and the recordings carry their own dates.
    run_started: str | None = None
    #: Span of the recordings this rung actually replayed (ground rule 1).
    recorded_from: str | None = None
    recorded_to: str | None = None
    #: When this record was appended to the artifact. A fact about the file,
    #: never about a measurement.
    written_at: str


class RungRecord(_Strict):
    """One rung, measured or explicitly not."""

    rung: str
    label: str
    variant: str
    output_mode: str
    model: str
    prompt_sha: str
    branches_from: str | None = None

    #: ``measured`` — the arm ran and :attr:`arm` carries its numbers.
    #: ``no_recordings`` — replay mode had nothing for this variant, so the
    #: rung has no numbers at all. Never a zero, never a placeholder.
    status: Literal["measured", "no_recordings"]
    #: Why, in one sentence, when ``status`` is not ``measured``.
    unmeasured_reason: str | None = None

    #: Judged sample size and split, surfaced from the arm so a reader can see
    #: two rungs are comparable without opening the nested report.
    judged_n: int | None = None
    judged_split: str | None = None

    #: Corpus items this rung's prompt quotes as a worked example *and* that
    #: were in the scored split. Non-empty means the rung saw the answer key
    #: for those items; read its judged score with that in mind.
    leaked_example_items: list[str] = Field(default_factory=list)

    provenance: RungProvenance
    #: The full phase-2 arm result, scored by the same code phase2 uses.
    arm: ArmResult | None = None


class AblationResult(_Strict):
    """``artifacts/results/ablation_{subagent}.json`` — appended to, not replaced."""

    ablation_version: str = ABLATION_VERSION
    subagent: str
    rungs: list[RungRecord] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# running the ladder
# --------------------------------------------------------------------------


def _core_items(
    subagent: str, *, dataset_dir: Path, limit: int | None
) -> list[DatasetItem]:
    """The core split for ``subagent``.

    The ladder runs on the core set (T10 card: "core set, k=2 judged +
    deterministic"), so the split is picked here and named in every record
    rather than being an argument a caller can vary silently. A corpus with no
    core flags at all falls back to the whole file — small fixture corpora
    exist and a ladder that measured nothing on them would fail the offline
    gate for the wrong reason — and that fallback is written into the notes.
    """
    path = dataset_dir / f"{subagent}.jsonl"
    if not path.exists():
        raise FileNotFoundError(
            f"no dataset at {path}. Run `python cli.py gen --customer <name> -n 70` "
            f"first — the ladder scores a corpus, it does not create one."
        )
    items = list(read_items(path))
    core = [item for item in items if item.core] or items
    return core[:limit] if limit is not None else core


def _judge_window(judge: Judge | None) -> tuple[datetime, datetime] | None:
    if judge is None:
        return None
    return getattr(getattr(judge, "adapter", None), "served_window", lambda: None)()


def run_ladder(
    subagent: str,
    *,
    mode: str = "replay",
    config: AppConfig | None = None,
    customer: str | None = None,
    n: int | None = None,
    rungs: Sequence[str] | None = None,
    dataset_dir: str | Path | None = None,
    out_path: str | Path | None = None,
    write: bool = True,
    append: bool = True,
    run_judge: bool = True,
    router: AdapterRouter | None = None,
    judge: Judge | None = None,
    bootstrap_seed: int | None = None,
) -> AblationResult:
    """Run the ladder for one subagent and append the records to its artifact.

    ``router`` / ``judge`` are injection points for tests. When they are not
    given, a fresh one is built **per rung** over a shared
    :class:`~amw.traces.store.ReplayStore`, so each record's
    ``recorded_from``/``recorded_to`` describes the calls *that rung* replayed
    rather than everything the run touched.
    """
    cfg = config or load_all(customer=customer)
    dataset_dir = Path(dataset_dir) if dataset_dir else default_dataset_dir()
    seed = bootstrap_seed if bootstrap_seed is not None else cfg.customer.seed
    repeats = cfg.customer.dataset.judge_repeats

    specs = ladder_for(subagent)
    if rungs is not None:
        wanted = list(dict.fromkeys(rungs))
        known = {spec.rung for spec in specs}
        unknown = [name for name in wanted if name not in known]
        if unknown:
            raise ValueError(
                f"unknown rung(s) {unknown} for {subagent}; the ladder has "
                f"{sorted(known)}."
            )
        specs = tuple(spec for spec in specs if spec.rung in wanted)

    items = _core_items(subagent, dataset_dir=dataset_dir, limit=n)
    if not items:
        raise ValueError(f"{subagent}: the corpus at {dataset_dir} is empty.")

    notes: list[str] = []
    if not any(item.core for item in items):
        notes.append(
            f"{subagent}: no item in this corpus is flagged core, so the ladder "
            f"ran over the whole file ({len(items)} items). Ladder numbers from "
            f"this run are not comparable to a core-split run."
        )

    store = ReplayStore()
    shared_router, shared_judge = router, judge
    scored_ids = {item.item_id for item in items}
    records: list[RungRecord] = []

    for spec in specs:
        pack = load_pack(subagent, spec.variant)
        rung_router = shared_router or AdapterRouter(
            mode=mode, models=cfg.models, store=store
        )
        rung_judge = shared_judge
        if rung_judge is None and run_judge:
            rung_judge = Judge(mode=mode, models=cfg.models, store=store)

        run_started = (
            datetime.now(timezone.utc).isoformat(timespec="seconds")
            if mode != "replay"
            else None
        )
        arm: ArmResult | None = None
        status: Literal["measured", "no_recordings"] = "measured"
        reason: str | None = None
        try:
            arm, _traces = run_arm(
                subagent,
                spec.variant,
                items,
                router=rung_router,
                judge=rung_judge,
                repeats=repeats,
                bootstrap_seed=seed,
                # The ladder judges everything it ran, and it ran the core
                # split. "core" is therefore the honest label even when the
                # fallback above widened the item list.
                judge_split="core",
            )
        except ReplayMissError as exc:
            # The one place a replay miss is survivable: a rung whose calls
            # nobody has recorded yet is a *known unmeasured* rung, not a
            # failure of the run. It gets a row with no numbers on it — never
            # a zero, never a placeholder (ground rule 1).
            status = "no_recordings"
            reason = (
                f"replay mode has no recorded calls for variant "
                f"{spec.variant!r} ({str(exc).rstrip('.')}). This rung is a "
                f"hypothesis until it is run live; nothing was measured and no "
                f"number is reported."
            )

        window = merge_windows(
            [
                getattr(rung_router, "served_window", lambda: None)(),
                _judge_window(rung_judge),
            ]
        )
        provenance = RungProvenance(
            customer=cfg.customer.customer,
            mode=mode,
            region=cfg.customer.region,
            dataset_provenance="/".join(sorted({item.provenance for item in items})),
            dataset_seed=min(item.seed for item in items),
            generator_version="/".join(
                sorted({item.generator_version for item in items})
            ),
            items=len(items),
            split="core",
            bootstrap_seed=seed,
            judge_repeats=repeats,
            judge_model=(
                rung_judge.describe().get("judge_model") if rung_judge else None
            ),
            judge_prompt_version=(
                rung_judge.describe().get("judge_prompt_version")
                if rung_judge
                else None
            ),
            adapters=rung_router.describe(),
            run_started=run_started,
            recorded_from=window[0].isoformat(timespec="seconds") if window else None,
            recorded_to=window[1].isoformat(timespec="seconds") if window else None,
            written_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        leaked = sorted(scored_ids & set(spec.few_shot_item_ids))
        records.append(
            RungRecord(
                rung=spec.rung,
                label=spec.label,
                variant=spec.variant,
                output_mode=pack.output_mode,
                # The logical model key, from models.yaml roles — recorded even
                # for an unmeasured rung, because "which model would this have
                # run on" is part of what the rung *is*.
                model=(
                    arm.model
                    if arm is not None
                    else resolve_model(spec.variant, cfg.models)
                ),
                prompt_sha=pack.sha256,
                branches_from=spec.branches_from,
                status=status,
                unmeasured_reason=reason,
                judged_n=(arm.judge.items_scored if arm and arm.judge else None),
                judged_split=(arm.judge.split if arm and arm.judge else None),
                leaked_example_items=leaked,
                provenance=provenance,
                arm=arm,
            )
        )
        if leaked:
            notes.append(
                f"rung {spec.rung}: its prompt quotes {', '.join(leaked)} as a "
                f"worked example and those items are also in the scored split. "
                f"Its judged score is optimistic on them by construction. Not "
                f"excluded — that would change this rung's denominator only."
            )

    unmeasured = [record.rung for record in records if record.status != "measured"]
    if unmeasured:
        notes.append(
            f"rung(s) {', '.join(unmeasured)} have no recorded calls in mode "
            f"{mode!r} and carry no numbers. Run them live to measure them."
        )

    result = AblationResult(subagent=subagent, rungs=records, notes=notes)
    if write:
        path = Path(out_path) if out_path else default_results_path(subagent)
        result = _write(result, path, append=append)
    return result


def _write(result: AblationResult, path: Path, *, append: bool) -> AblationResult:
    """Append this run's records to the subagent's artifact.

    Appending, not replacing: the ladder is run repeatedly as rungs are edited,
    and a file that kept only the last run would erase the history the ladder
    exists to show. Notes are carried forward with the records that produced
    them.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if append and path.exists():
        previous = AblationResult.model_validate_json(path.read_text(encoding="utf-8"))
        if previous.ablation_version != ABLATION_VERSION:
            raise ValueError(
                f"{path} was written by ablation_version "
                f"{previous.ablation_version!r} and this build writes "
                f"{ABLATION_VERSION!r}. Move the old file aside rather than "
                f"mixing shapes in one artifact."
            )
        if previous.subagent != result.subagent:
            raise ValueError(
                f"{path} holds rungs for {previous.subagent!r}, not "
                f"{result.subagent!r}."
            )
        result = AblationResult(
            subagent=result.subagent,
            rungs=previous.rungs + result.rungs,
            notes=previous.notes + result.notes,
        )
    path.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return result


# --------------------------------------------------------------------------
# console rendering
# --------------------------------------------------------------------------

#: Printed next to every rung. The judged score is the ladder's headline, and
#: these two are the guard metrics the FE novelty rung must not move
#: (``notes/day1_failures.md``): a rung that buys judged score with fabrication
#: is rejected whatever the judge says.
GUARD_METRICS: tuple[str, ...] = ("hallucination_rate", "answered_precision")


def _estimate_text(point: float | None, estimate: Any, n: int) -> str:
    """Three states, kept distinct: unmeasured, a bare mean, a mean with a CI."""
    if point is None:
        return "not measured"
    if estimate is None:
        return f"{point:.3f}  no CI (n={n})"
    return f"{estimate.point:.3f}  95% CI [{estimate.lo:.3f}, {estimate.hi:.3f}]  n={estimate.n}"


def format_rung(record: RungRecord) -> list[str]:
    """One rung as console lines. Never prints a number that was not measured."""
    head = (
        f"{record.rung:20s} {record.variant:26s} "
        f"{record.output_mode:16s} {record.model}"
    )
    lines = [head, f"    {record.label}"]
    if record.status != "measured":
        lines.append(f"    NOT MEASURED — {record.unmeasured_reason}")
        return lines

    arm = record.arm
    assert arm is not None  # status == "measured" implies an arm
    calls = f"{arm.calls_ok}/{arm.items} ok"
    if arm.calls_error:
        calls += f", {arm.calls_error} error"
    lines.append(f"    calls {calls}")
    if arm.judge is not None:
        label = f"judge_score[{arm.judge.split}]"
        lines.append(
            f"    {label:28s} "
            f"{_estimate_text(arm.judge.point, arm.judge.estimate, arm.judge.items_scored)}"
        )
    for name in GUARD_METRICS:
        report = arm.metrics.get(name)
        if report is not None:
            lines.append(
                f"    {name:28s} {_estimate_text(report.point, report.estimate, report.n)}"
            )
    if record.leaked_example_items:
        lines.append(
            f"    few-shot overlap with scored items: "
            f"{', '.join(record.leaked_example_items)}"
        )
    return lines


def error_kinds(records: Sequence[RungRecord]) -> dict[str, int]:
    """Distinct call errors across the run, for a one-line summary."""
    counter: Counter[str] = Counter()
    for record in records:
        if record.arm is not None:
            counter.update(record.arm.error_kinds)
    return dict(counter)
