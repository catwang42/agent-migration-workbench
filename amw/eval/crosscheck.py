"""Second-judge cross-check — a validating instrument, never a gating one.

The judged quality numbers on the scorecard come from one Gemini 2.5 Pro judge.
The obvious objection in the room is *"Gemini judged the Gemini-vs-Claude
comparison"*, and the honest answer is not an assurance, it is a second
instrument: a Claude-class judge, reached over the Vertex Model Garden path,
re-scoring the **already recorded** subagent outputs against the **same
rubrics** with the same prompt text.

The combination rule, which this module enforces structurally
-------------------------------------------------------------

**The Gemini judge remains the gated instrument.** It was registered before
results were seen; it is what ``config/gates.yaml`` is checked against. The
Claude judge validates it and nothing else:

* no averaging of the two judges into a blended score;
* no substitution of one for the other, on any subagent, for any reason;
* no writing back into ``phase2.json``.

That is why this module produces its own artifact
(``artifacts/results/crosscheck.json``) and a footer line, and why nothing here
returns anything shaped like a gate estimate. Picking a favourite judge after
seeing which one is kinder is the exact failure this instrument exists to rule
out, so the code is not given the option.

What agreement means here
-------------------------

The unit is one **rubric criterion on one item on one scoring pass** — the
smallest thing both judges actually decided, and the only unit on which
Cohen's kappa is defined at all (kappa needs categorical labels; an item score
is a fraction). Repeats are paired by index: pass 1 against pass 1, pass 2
against pass 2.

Item-level figures are reported beside it — the two judges' mean scores, how
often they landed on exactly the same item score, and the mean absolute gap —
because criterion agreement can look healthy while the arms' *ranking* moves,
and the ranking is what the migration decision rests on.

Kappa is prevalence-sensitive and this is a high-pass-rate corpus. When both
judges pass nearly everything, chance agreement is already near 1.0 and kappa
collapses toward 0 (or becomes undefined) at 95% raw agreement. Rather than
quietly print a scary number, :func:`cohens_kappa` returns the reason alongside
it and :class:`AgreementReport` carries both judges' pass rates so a reader can
see the prevalence for themselves.

Nothing here fabricates. A judge call that failed on either side is not scored
as a disagreement — it is excluded and counted in ``unusable``, with the reason.
"""

from __future__ import annotations

import random
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from pydantic import BaseModel, ConfigDict, Field

from amw.adapters import AdapterRouter, merge_windows
from amw.agents.prompt_packs import VARIANTS, build_request, load_pack
from amw.agents.schemas import SUBAGENTS
from amw.config import AppConfig, ConfigError, load_all
from amw.datasets.schema import DatasetItem, read_items
from amw.eval.judge import Judge, JudgeRequest, JudgeVerdict
from amw.eval.runner import (
    Phase2Result,
    judge_candidate,
    prompt_view,
    rubric_of,
)

__all__ = [
    "CROSSCHECK_VERSION",
    "INSUFFICIENT",
    "CROSSCHECK_ROLE",
    "CROSSCHECK_PROMPT_VERSION",
    "CROSSCHECK_OUTPUT_MODE",
    "FULL_CROSSCHECK_SUBAGENTS",
    "SAMPLE_FRACTION",
    "SAMPLE_SEED",
    "VALIDATION_THRESHOLD",
    "VALIDATED",
    "UNRELIABLE",
    "COMBINATION_RULE",
    "AgreementReport",
    "CriterionPair",
    "CrosscheckResult",
    "Disagreement",
    "ItemPair",
    "Unusable",
    "SubagentCrosscheck",
    "cohens_kappa",
    "crosscheck_footer_line",
    "default_crosscheck_path",
    "largest_disagreements",
    "pair_verdicts",
    "report_agreement",
    "run_crosscheck",
    "stratified_sample",
    "verdict_for",
]

#: Bumped when the artifact shape changes, so a stale crosscheck.json is
#: detectable the same way a stale phase2.json is.
CROSSCHECK_VERSION = "1"

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Role in config/models.yaml. Resolves to a Claude-class model; the access
#: path is CLAUDE_PATH=vertex (the standing demo-path decision), which
#: `resolve` handles — no path literal appears here.
CROSSCHECK_ROLE = "judge_crosscheck"

#: The cross-check judge's own published prompt pack. Same scoring text as the
#: gated judge's `v1`, differing only in the emission mechanism — see
#: judge_prompts/README.md and tests/test_crosscheck.py, which asserts the two
#: packs cannot drift apart in anything that affects scoring.
CROSSCHECK_PROMPT_VERSION = "v1_crosscheck"

#: Claude on Vertex emits through a tool call: under this demo organization's
#: Vertex AI policy configuration (`constraints/vertexai.allowedPartnerModelFeatures`),
#: partner-model structured outputs were unavailable.
CROSSCHECK_OUTPUT_MODE = "tool"

#: Cross-checked at full scope, because this is the subagent whose judged
#: finding the tuning work is about to be aimed at.
FULL_CROSSCHECK_SUBAGENTS: tuple[str, ...] = ("feature_extractor",)

#: Everything else gets a sample, for generalization: does the agreement seen
#: on the FE arms hold on subagents with a different task shape? Expressed as a
#: fraction of the **whole corpus**, not of the judged split, so the figure
#: means the same thing whatever split the gated judge happened to run on.
SAMPLE_FRACTION = 0.20

#: Sampling is seeded and stratified so the sample is reproducible and can be
#: re-drawn identically by anyone reading the artifact.
SAMPLE_SEED = 20260812

#: Pre-agreed with the owner before the cross-check was run: at or above this,
#: the judged finding is validated; below it, the judged metric is flagged.
VALIDATION_THRESHOLD = 0.85

VALIDATED = "VALIDATED"
UNRELIABLE = "UNRELIABLE"
#: Not enough paired verdicts to say either way. Never silently a pass.
INSUFFICIENT = "INSUFFICIENT"

COMBINATION_RULE = (
    "The Gemini 2.5 Pro judge is the gated instrument and was registered before "
    "results were seen. The Claude cross-check judge validates it. The two are "
    "never averaged and one is never substituted for the other."
)


def default_crosscheck_path() -> Path:
    return REPO_ROOT / "artifacts" / "results" / "crosscheck.json"


def default_dataset_dir() -> Path:
    return REPO_ROOT / "datasets"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# sampling
# --------------------------------------------------------------------------


def stratified_sample(
    items: Sequence[DatasetItem],
    *,
    size: int,
    seed: int = SAMPLE_SEED,
    stratum: Any = None,
) -> list[DatasetItem]:
    """``size`` items, proportionally allocated across strata, deterministically.

    Stratifies on ``difficulty`` by default: an easy-heavy sample would make the
    two judges look more alike than they are, because easy items are where any
    two competent judges agree. Allocation is proportional with largest-remainder
    rounding, so the returned sample is exactly ``size`` items whenever the pool
    is large enough, and the whole pool otherwise.

    Deterministic given ``seed``: the same draw can be reproduced by a reader
    from the artifact alone.
    """
    if size >= len(items):
        return sorted(items, key=lambda i: i.item_id)
    if size <= 0:
        return []
    key = stratum or (lambda item: item.difficulty)

    groups: dict[Any, list[DatasetItem]] = defaultdict(list)
    for item in sorted(items, key=lambda i: i.item_id):
        groups[key(item)].append(item)

    # Largest remainder: floor everything, then hand the leftover seats to the
    # strata with the biggest fractional claim. Ties break on the stratum name
    # so the allocation does not depend on dict ordering.
    exact = {name: len(group) * size / len(items) for name, group in groups.items()}
    quota = {name: int(value) for name, value in exact.items()}
    leftover = size - sum(quota.values())
    ranked = sorted(
        exact, key=lambda name: (-(exact[name] - quota[name]), str(name))
    )
    for name in ranked[:leftover]:
        quota[name] += 1

    rng = random.Random(seed)
    picked: list[DatasetItem] = []
    for name in sorted(groups, key=str):
        group = list(groups[name])
        rng.shuffle(group)
        picked.extend(group[: quota[name]])
    return sorted(picked, key=lambda i: i.item_id)


# --------------------------------------------------------------------------
# pairing
# --------------------------------------------------------------------------


class CriterionPair(_Strict):
    """One rubric criterion, as both judges called it."""

    criterion_id: str
    gated_passed: bool
    check_passed: bool
    gated_rationale: str
    check_rationale: str

    @property
    def agrees(self) -> bool:
        return self.gated_passed == self.check_passed


class ItemPair(_Strict):
    """One (item, scoring pass) that BOTH judges scored successfully."""

    subagent: str
    arm: str
    item_id: str
    repeat: int
    gated_score: float
    check_score: float
    criteria: list[CriterionPair] = Field(default_factory=list)

    @property
    def score_gap(self) -> float:
        return abs(self.gated_score - self.check_score)


class Unusable(_Strict):
    """A cell that could not be paired, and why. Never a disagreement."""

    subagent: str
    arm: str
    item_id: str
    repeat: int
    reason: str


def pair_verdicts(
    gated: Iterable[JudgeVerdict],
    check: Iterable[JudgeVerdict],
    *,
    subagent: str,
    arm: str,
) -> tuple[list[ItemPair], list[Unusable]]:
    """Match the two judges' verdicts on ``(item_id, repeat)``.

    A cell is paired only when both judges returned ``status="ok"`` over the
    same criterion ids. Anything else is unusable and is recorded as such: a
    judge outage on one side is our infrastructure failing, and counting it as
    a disagreement would make the instruments look further apart than they are
    — the mirror image of the zero-score rule in :mod:`amw.eval.judge`.
    """
    by_key: dict[tuple[str, int], dict[str, JudgeVerdict]] = defaultdict(dict)
    for verdict in gated:
        by_key[(verdict.item_id, verdict.repeat)]["gated"] = verdict
    for verdict in check:
        by_key[(verdict.item_id, verdict.repeat)]["check"] = verdict

    pairs: list[ItemPair] = []
    unusable: list[Unusable] = []
    for (item_id, repeat), sides in sorted(by_key.items()):
        def _drop(reason: str) -> None:
            unusable.append(
                Unusable(
                    subagent=subagent,
                    arm=arm,
                    item_id=item_id,
                    repeat=repeat,
                    reason=reason,
                )
            )

        a, b = sides.get("gated"), sides.get("check")
        if a is None or b is None:
            _drop("only one judge scored this cell")
            continue
        if a.status != "ok" or b.status != "ok":
            which = "gated" if a.status != "ok" else "cross-check"
            failing = a if a.status != "ok" else b
            _drop(f"{which} judge call failed: {failing.error}")
            continue

        gated_by_id = {c.criterion_id: c for c in a.criteria}
        check_by_id = {c.criterion_id: c for c in b.criteria}
        if set(gated_by_id) != set(check_by_id):
            # Both judges validated against the same rubric before getting
            # here, so this should be unreachable; if it ever fires, the two
            # sides were scored against different rubric revisions and no
            # comparison between them means anything.
            _drop("the two judges scored different criterion sets")
            continue

        assert a.score is not None and b.score is not None  # status == "ok"
        pairs.append(
            ItemPair(
                subagent=subagent,
                arm=arm,
                item_id=item_id,
                repeat=repeat,
                gated_score=a.score,
                check_score=b.score,
                criteria=[
                    CriterionPair(
                        criterion_id=cid,
                        gated_passed=gated_by_id[cid].passed,
                        check_passed=check_by_id[cid].passed,
                        gated_rationale=gated_by_id[cid].rationale,
                        check_rationale=check_by_id[cid].rationale,
                    )
                    for cid in sorted(gated_by_id)
                ],
            )
        )
    return pairs, unusable


# --------------------------------------------------------------------------
# agreement
# --------------------------------------------------------------------------


def cohens_kappa(
    labels: Sequence[tuple[bool, bool]],
) -> tuple[float | None, str | None]:
    """Cohen's kappa over paired binary labels, plus a caveat when one applies.

    Returns ``(kappa, note)``. ``kappa`` is ``None`` — never 0.0 — when the
    statistic is undefined, because "the judges never disagreed" and "the judges
    agreed no better than chance" are opposite findings and a zero would read as
    the second.

    The undefined case is real and likely here: when both judges pass (or fail)
    every criterion, expected chance agreement is 1.0 and the denominator
    vanishes. A note is also attached, without suppressing the number, whenever
    chance agreement is high enough that kappa understates a genuinely high
    raw agreement.
    """
    n = len(labels)
    if n == 0:
        return None, "no paired criterion verdicts"

    observed = sum(1 for a, b in labels if a == b) / n
    a_pass = sum(1 for a, _ in labels if a) / n
    b_pass = sum(1 for _, b in labels if b) / n
    expected = a_pass * b_pass + (1 - a_pass) * (1 - b_pass)

    if expected >= 1.0 - 1e-12:
        return None, (
            "kappa is undefined on this sample: both judges assigned the same "
            f"label to every criterion (pass rates {a_pass:.3f} and {b_pass:.3f}), "
            "so chance agreement is 1.0 and the statistic has no denominator. "
            f"Raw agreement is {observed:.4f}."
        )

    kappa = (observed - expected) / (1 - expected)
    note = None
    if expected >= 0.8:
        note = (
            f"chance agreement on this sample is already {expected:.3f} (pass "
            f"rates {a_pass:.3f} and {b_pass:.3f}), so kappa is heavily "
            "prevalence-deflated: it can read low beside a high raw agreement. "
            "Read the two together, not kappa alone."
        )
    return kappa, note


class AgreementReport(_Strict):
    """How the two instruments compared, on one subagent or one arm."""

    subagent: str
    #: None on a subagent-level roll-up across arms.
    arm: str | None = None
    scope: str
    items: int
    #: Paired (item, repeat) cells. Two per item at k=2.
    paired_cells: int
    criterion_pairs: int

    #: The headline. Fraction of criterion verdicts the judges called the same.
    criterion_agreement: float | None = None
    cohens_kappa: float | None = None
    kappa_note: str | None = None

    gated_pass_rate: float | None = None
    check_pass_rate: float | None = None

    #: Item-level: same score, and how far apart when not.
    item_score_agreement: float | None = None
    mean_abs_score_gap: float | None = None
    gated_mean_score: float | None = None
    check_mean_score: float | None = None

    unusable: int = 0
    unusable_reasons: dict[str, int] = Field(default_factory=dict)


def report_agreement(
    pairs: Sequence[ItemPair],
    unusable: Sequence[Unusable] = (),
    *,
    subagent: str,
    arm: str | None = None,
    scope: str,
) -> AgreementReport:
    """Roll paired verdicts into one report. Empty in, no numbers out."""
    reasons = Counter(u.reason.split(":", 1)[0] for u in unusable)
    base = AgreementReport(
        subagent=subagent,
        arm=arm,
        scope=scope,
        items=len({p.item_id for p in pairs}),
        paired_cells=len(pairs),
        criterion_pairs=sum(len(p.criteria) for p in pairs),
        unusable=len(unusable),
        unusable_reasons=dict(sorted(reasons.items())),
    )
    if not pairs:
        return base

    labels = [
        (c.gated_passed, c.check_passed) for pair in pairs for c in pair.criteria
    ]
    kappa, note = cohens_kappa(labels)
    n = len(labels)
    base.criterion_agreement = sum(1 for a, b in labels if a == b) / n if n else None
    base.cohens_kappa = kappa
    base.kappa_note = note
    base.gated_pass_rate = sum(1 for a, _ in labels if a) / n if n else None
    base.check_pass_rate = sum(1 for _, b in labels if b) / n if n else None
    base.item_score_agreement = sum(
        1 for p in pairs if p.gated_score == p.check_score
    ) / len(pairs)
    base.mean_abs_score_gap = sum(p.score_gap for p in pairs) / len(pairs)
    base.gated_mean_score = sum(p.gated_score for p in pairs) / len(pairs)
    base.check_mean_score = sum(p.check_score for p in pairs) / len(pairs)
    return base


# --------------------------------------------------------------------------
# disagreements, for a human to read
# --------------------------------------------------------------------------


class Disagreement(_Strict):
    """One item where the judges parted company, with both sides' reasoning."""

    subagent: str
    arm: str
    item_id: str
    repeat: int
    gated_score: float
    check_score: float
    score_gap: float
    criteria: list[CriterionPair] = Field(default_factory=list)


def largest_disagreements(
    pairs: Iterable[ItemPair], *, limit: int = 10
) -> list[Disagreement]:
    """The ``limit`` widest per-item gaps, each carrying only the criteria that
    actually differed — a disagreement report padded with the criteria both
    judges agreed on is unreadable, which is how it goes unread.
    """
    ranked = sorted(
        (p for p in pairs if p.score_gap > 0),
        key=lambda p: (-p.score_gap, p.subagent, p.arm, p.item_id, p.repeat),
    )
    return [
        Disagreement(
            subagent=p.subagent,
            arm=p.arm,
            item_id=p.item_id,
            repeat=p.repeat,
            gated_score=p.gated_score,
            check_score=p.check_score,
            score_gap=p.score_gap,
            criteria=[c for c in p.criteria if not c.agrees],
        )
        for p in ranked[:limit]
    ]


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------


class SubagentCrosscheck(_Strict):
    """Everything the cross-check found about one subagent."""

    subagent: str
    scope: str
    item_ids: list[str] = Field(default_factory=list)
    sampled_from: int = 0
    corpus_size: int = 0
    #: Per-arm, then the roll-up across arms that the threshold is read against.
    per_arm: list[AgreementReport] = Field(default_factory=list)
    overall: AgreementReport
    verdict: str = INSUFFICIENT
    threshold: float = VALIDATION_THRESHOLD
    disagreements: list[Disagreement] = Field(default_factory=list)


class CrosscheckResult(_Strict):
    """``artifacts/results/crosscheck.json``.

    Deliberately a separate file from ``phase2.json``. The gated numbers live
    there; nothing in here is allowed to change them, and keeping the two apart
    is the cheapest way to make that true rather than merely intended.
    """

    crosscheck_version: str = CROSSCHECK_VERSION
    customer: str
    mode: str
    combination_rule: str = COMBINATION_RULE
    #: The instrument being validated, and the one doing the validating.
    gated_judge: dict[str, str] = Field(default_factory=dict)
    check_judge: dict[str, str] = Field(default_factory=dict)
    sample_seed: int = SAMPLE_SEED
    sample_fraction: float = SAMPLE_FRACTION
    judge_repeats: int
    scored_artifact: str | None = None
    run_started: str | None = None
    recorded_from: str | None = None
    recorded_to: str | None = None
    subagents: list[SubagentCrosscheck] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def for_subagent(self, subagent: str) -> SubagentCrosscheck | None:
        for entry in self.subagents:
            if entry.subagent == subagent:
                return entry
        return None


def _judged_items(
    items: Sequence[DatasetItem], phase2: Phase2Result, subagent: str
) -> tuple[list[DatasetItem], str]:
    """The items the GATED judge actually scored, and which split that was.

    Cross-checking an item the gated judge never saw produces no pair, so the
    eligible pool is exactly its split — read off the artifact rather than
    assumed, because the split differs by subagent (T09 widened
    feature_extractor to the full corpus and left the others on core).
    """
    splits = {
        arm.judge.split
        for arm in phase2.arms
        if arm.subagent == subagent and arm.judge is not None
    }
    if not splits:
        return [], "unjudged"
    if len(splits) > 1:
        raise ConfigError(
            f"{subagent}: the artifact's arms were judged on different splits "
            f"({sorted(splits)}); there is no single eligible pool to sample from."
        )
    split = splits.pop()
    if split == "all":
        return list(items), split
    return [item for item in items if item.core], split


def _load_dataset(subagent: str, *, dataset_dir: Path) -> list[DatasetItem]:
    path = dataset_dir / f"{subagent}.jsonl"
    if not path.exists():
        raise FileNotFoundError(
            f"no dataset at {path}. The cross-check re-scores a corpus that has "
            f"already been run; it does not create one."
        )
    return list(read_items(path))


def _score_arm(
    subagent: str,
    variant: str,
    items: Sequence[DatasetItem],
    *,
    router: AdapterRouter,
    gated: Judge,
    check: Judge,
    repeats: int,
) -> tuple[list[ItemPair], list[Unusable]]:
    """Re-score one arm's ALREADY RECORDED outputs with both judges.

    ``router`` is expected to be in replay: this instrument re-scores existing
    generations and must not produce new ones, or the two judges would be
    comparing notes on different outputs than the scorecard reports.
    """
    pack = load_pack(subagent, variant)
    requests = [
        build_request(subagent, variant, prompt_view(item), item_id=item.item_id)
        for item in items
    ]
    traces = router.complete_many(requests)

    judge_requests = [
        JudgeRequest(
            item_id=item.item_id,
            subagent=subagent,
            rubric=rubric_of(item),
            candidate=judge_candidate(trace),
            task_input=list(item.input.messages),
            context_chunks=item.input.context_chunks(),
            reference=item.gold,
            repeat=repeat,
            repeats=repeats,
            arm=variant,
        )
        for item, trace in zip(items, traces)
        for repeat in range(1, repeats + 1)
    ]
    # Identical JudgeRequests to the ones phase2 built, so the gated side
    # replays the very calls the scorecard was computed from rather than
    # re-asking Gemini and quietly getting a second opinion of its own.
    return pair_verdicts(
        gated.score_many(judge_requests),
        check.score_many(judge_requests),
        subagent=subagent,
        arm=variant,
    )


def run_crosscheck(
    *,
    customer: str | None = None,
    config: AppConfig | None = None,
    phase2: Phase2Result | None = None,
    results_path: str | Path | None = None,
    mode: str = "live",
    subagents: Sequence[str] | None = None,
    variants: Sequence[str] | None = None,
    full_scope: Sequence[str] = FULL_CROSSCHECK_SUBAGENTS,
    sample_fraction: float = SAMPLE_FRACTION,
    sample_seed: int = SAMPLE_SEED,
    dataset_dir: str | Path | None = None,
    out_path: str | Path | None = None,
    write: bool = True,
    gated_judge: Judge | None = None,
    check_judge: Judge | None = None,
    router: AdapterRouter | None = None,
    disagreement_limit: int = 10,
) -> CrosscheckResult:
    """Run the second-judge cross-check and write ``crosscheck.json``.

    ``mode`` is the cross-check judge's mode only. Generations are always
    replayed — this instrument scores what was already recorded — and the gated
    judge is always replayed too, for the same reason.
    """
    cfg = config or load_all(customer=customer)
    subagents = tuple(subagents or SUBAGENTS)
    variants = tuple(variants or VARIANTS)
    dataset_dir = Path(dataset_dir) if dataset_dir else default_dataset_dir()
    repeats = cfg.customer.dataset.judge_repeats

    if phase2 is None:
        path = Path(results_path) if results_path else None
        if path is None:
            raise ValueError(
                "run_crosscheck needs the phase-2 artifact it is validating: "
                "pass phase2= or results_path=. The eligible item pool is the "
                "gated judge's split, which is recorded there."
            )
        phase2 = Phase2Result.model_validate_json(path.read_text())

    # Generations: replay only. Never live — see the docstring.
    router = router or AdapterRouter(mode="replay", models=cfg.models)
    gated = gated_judge or Judge(mode="replay", models=cfg.models)
    check = check_judge or Judge(
        mode=mode,
        models=cfg.models,
        role=CROSSCHECK_ROLE,
        prompt_version=CROSSCHECK_PROMPT_VERSION,
        output_mode=CROSSCHECK_OUTPUT_MODE,
    )
    if gated.model_key == check.model_key:
        raise ConfigError(
            f"the cross-check judge resolved to the same model as the gated "
            f"judge ({gated.model_key}). A model cross-checking itself measures "
            f"nothing; fix the {CROSSCHECK_ROLE!r} role in config/models.yaml."
        )

    notes: list[str] = [COMBINATION_RULE]
    result = CrosscheckResult(
        customer=cfg.customer.customer,
        mode=mode,
        gated_judge=gated.describe(),
        check_judge=check.describe(),
        sample_seed=sample_seed,
        sample_fraction=sample_fraction,
        judge_repeats=repeats,
        scored_artifact=str(results_path) if results_path else None,
        run_started=(
            datetime.now(timezone.utc).isoformat(timespec="seconds")
            if mode != "replay"
            else None
        ),
        notes=notes,
    )

    for subagent in subagents:
        items = _load_dataset(subagent, dataset_dir=dataset_dir)
        eligible, split = _judged_items(items, phase2, subagent)
        if not eligible:
            notes.append(
                f"{subagent}: the gated judge scored nothing in "
                f"{result.scored_artifact or 'the supplied artifact'}, so there "
                f"is nothing to cross-check against."
            )
            continue

        if subagent in full_scope:
            selected = sorted(eligible, key=lambda i: i.item_id)
            scope = f"full gated split ({split}), all arms"
        else:
            target = round(sample_fraction * len(items))
            selected = stratified_sample(eligible, size=target, seed=sample_seed)
            scope = (
                f"{sample_fraction:.0%} stratified sample of the {len(items)}-item "
                f"corpus ({len(selected)} items), drawn from the {len(eligible)} "
                f"items in the gated judge's {split} split"
            )
            if target > len(eligible):
                notes.append(
                    f"{subagent}: a {sample_fraction:.0%} sample of the corpus is "
                    f"{target} items but only {len(eligible)} were judged by the "
                    f"gated judge ({split} split), so all {len(eligible)} were "
                    f"cross-checked. The sample is {len(eligible)/len(items):.0%} "
                    f"of the corpus, not {sample_fraction:.0%}."
                )

        per_arm: list[AgreementReport] = []
        all_pairs: list[ItemPair] = []
        all_unusable: list[Unusable] = []
        for variant in variants:
            pairs, unusable = _score_arm(
                subagent,
                variant,
                selected,
                router=router,
                gated=gated,
                check=check,
                repeats=repeats,
            )
            per_arm.append(
                report_agreement(
                    pairs, unusable, subagent=subagent, arm=variant, scope=scope
                )
            )
            all_pairs.extend(pairs)
            all_unusable.extend(unusable)

        overall = report_agreement(
            all_pairs, all_unusable, subagent=subagent, scope=scope
        )
        result.subagents.append(
            SubagentCrosscheck(
                subagent=subagent,
                scope=scope,
                item_ids=[item.item_id for item in selected],
                sampled_from=len(eligible),
                corpus_size=len(items),
                per_arm=per_arm,
                overall=overall,
                verdict=verdict_for(overall),
                disagreements=largest_disagreements(
                    all_pairs, limit=disagreement_limit
                ),
            )
        )

    window = merge_windows(
        [router.served_window(), getattr(gated.adapter, "served_window", lambda: None)()]
    )
    if window is not None:
        result.recorded_from = window[0].isoformat(timespec="seconds")
        result.recorded_to = window[1].isoformat(timespec="seconds")

    if write:
        path = Path(out_path) if out_path else default_crosscheck_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(result.model_dump_json(indent=2) + "\n")
    return result


def verdict_for(
    report: AgreementReport, *, threshold: float = VALIDATION_THRESHOLD
) -> str:
    """:data:`VALIDATED` / :data:`UNRELIABLE` / :data:`INSUFFICIENT`.

    Read off ``criterion_agreement`` — the raw agreement rate, which is what
    the threshold was agreed against. Kappa is reported beside it and is
    deliberately not part of this test: it is prevalence-sensitive on a
    high-pass-rate corpus, and swapping the criterion after seeing the numbers
    is precisely the move this instrument exists to foreclose.
    """
    if report.criterion_agreement is None:
        return INSUFFICIENT
    return VALIDATED if report.criterion_agreement >= threshold else UNRELIABLE


# --------------------------------------------------------------------------
# the scorecard footer line
# --------------------------------------------------------------------------


def crosscheck_footer_line(result: CrosscheckResult) -> str:
    """One line for the scorecard footer: what was cross-checked, and the rule.

    Names every subagent's agreement so a reader cannot come away thinking the
    validated one speaks for the others.
    """
    parts = []
    for entry in result.subagents:
        agreement = entry.overall.criterion_agreement
        kappa = entry.overall.cohens_kappa
        shown = "not measured" if agreement is None else f"{agreement:.1%}"
        kappa_shown = "kappa undefined" if kappa is None else f"kappa {kappa:.3f}"
        parts.append(f"{entry.subagent} {shown} ({kappa_shown}, {entry.verdict})")
    checked = result.check_judge.get("judge_model", "cross-check judge")
    gated = result.gated_judge.get("judge_model", "gated judge")
    body = "; ".join(parts) if parts else "nothing was cross-checked"
    return (
        f"Second-judge cross-check: {checked} re-scored the recorded outputs "
        f"against the same rubrics — criterion-level agreement with the gated "
        f"{gated} judge: {body}. {COMBINATION_RULE}"
    )
