"""Disagreement triage: win / loss / tie, from recorded judge calls only.

Agreement says *that* two arms differed. It cannot say which one was right, and
"11% disagreement" is not a migration decision — ``config/gates.yaml`` says so
explicitly, giving ``shadow_agreement`` the alternative route *"on
disagreements, judge-adjudicated wins >= losses"*. This module produces that
adjudication.

Where the verdicts come from
----------------------------

**No new judge calls are issued.** The phase-2 baseline already judged these
arms on these items, and every one of those calls is on disk in
``artifacts/replay/judge_*.jsonl``. Adjudication here is a *calculation over
recorded calls* (CLAUDE.md ground rule 1): rebuild the identical judge request,
let the replay store serve the recording, re-parse it with the same
:class:`~amw.eval.judge.Judge` that produced the original score, and compare
the two arms' per-item scores.

Higher recorded score wins; equal scores tie. The one-line rationale is
**quoted from the recording** — the loser's own failed-criterion rationale,
condensed to one line — never composed here. A rationale this module wrote
itself would be an invented adjudication wearing the judge's clothes.

The coverage hole, stated rather than hidden
--------------------------------------------

Phase 2 judged Query Rewriter and Chunk Summarizer on the **28-item core
split** and Feature Extractor on the **full 70** (a registered deviation; see
``notes/phase2_n70_validation.md``). A disagreement on an item outside its
subagent's judged split therefore has *no recorded verdict to read*, and there
is no honest way to produce one without issuing live judge calls.

Those items are labelled :data:`NOT_ADJUDICATED` with the reason, and counted
separately in :class:`TriageSummary`. They must never fold into "tie": a tie is
a measured finding — the judge scored both arms the same — while an
unadjudicated item is an absence of measurement, and a scorecard that adds the
two together is reporting a coverage gap as evidence of parity.

The same label covers a live slice (``--live-slice``), whose outputs are new
and so have no recorded judge call at all.
"""

from __future__ import annotations

import re
from typing import Iterable, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from amw.eval.judge import Judge, JudgeRequest, JudgeVerdict
from amw.eval.runner import judge_candidate, rubric_of
from amw.datasets.schema import DatasetItem
from amw.shadow.agreement import ItemAgreement
from amw.shadow.emission import malformed_emission
from amw.traces.schema import Trace

__all__ = [
    "WIN",
    "LOSS",
    "TIE",
    "NOT_ADJUDICATED",
    "MALFORMED_CAVEAT",
    "TriageVerdict",
    "TriageRow",
    "TriageSummary",
    "adjudicate_item",
    "adjudicate",
    "summarize",
    "triage_table_markdown",
]

#: Verdicts are written from the **candidate's** point of view, the same sign
#: convention as ``amw.eval.stats.paired_bootstrap_delta`` (candidate minus
#: baseline): a "win" is a win for Gemini.
WIN = "win"
LOSS = "loss"
TIE = "tie"
NOT_ADJUDICATED = "not_adjudicated"

TriageVerdict = Literal["win", "loss", "tie", "not_adjudicated"]

#: Label carried on every row so a reader never has to remember which subagent
#: was judged on which split.
SPLIT_LABELS = {
    "core": "core split (judged)",
    "all": "full corpus (judged)",
}

#: The reason text that follows `not_adjudicated` when the item was never
#: judged in phase 2. The card requires this exact distinction to be visible.
OUTSIDE_SPLIT = "outside judged split"

_MAX_RATIONALE_CHARS = 180

#: Printed wherever the structural exclusion is quoted. The mechanism is the
#: same one disclosed beside the Claude baseline everywhere else in the report,
#: and naming it here is what stops "excluded" reading as "discarded because it
#: was inconvenient".
MALFORMED_CAVEAT = (
    "Under this demo organization's Vertex AI policy configuration "
    "(`constraints/vertexai.allowedPartnerModelFeatures`), partner-model "
    "structured outputs were unavailable, so the Claude baseline was measured "
    "using tool-call structured emission; the excluded items are ones where "
    "that emission was structurally broken (see `amw/shadow/emission.py`), not "
    "ones where the baseline merely answered worse."
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _one_line(text: str | None, limit: int = _MAX_RATIONALE_CHARS) -> str:
    """Condense recorded rationale text to a single table cell.

    Whitespace collapse, pipe escaping (it lands in a Markdown table) and a
    hard truncation. Condensing is allowed; rewriting is not — the words are
    the judge's.
    """
    if not text:
        return ""
    flat = re.sub(r"\s+", " ", str(text)).strip().replace("|", "\\|")
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


# --------------------------------------------------------------------------
# result shapes
# --------------------------------------------------------------------------


class TriageRow(_Strict):
    """One disagreeing item, adjudicated or explicitly not."""

    item_id: str
    subagent: str
    #: Fields the two arms differed on, from the agreement pass.
    fields: list[str] = Field(default_factory=list)
    verdict: TriageVerdict
    #: Quoted (condensed) from the recorded judge call. Empty only when the
    #: item was not adjudicated.
    rationale: str = ""
    #: "core split (judged)" / "full corpus (judged)" / "outside judged split".
    judged_split: str
    #: Mean recorded judge score per arm over the successful repeats. None when
    #: there was nothing recorded to read.
    baseline_score: float | None = None
    candidate_score: float | None = None
    #: How many recorded judge repeats were actually read, per arm.
    baseline_repeats: int = 0
    candidate_repeats: int = 0
    #: Why an item was not adjudicated. Always set when verdict is
    #: ``not_adjudicated``, never set otherwise.
    reason: str | None = None
    #: One line saying why the *baseline* arm's emission was structurally
    #: broken on this item, or None when it emitted a well-formed payload. See
    #: :mod:`amw.shadow.emission`. Set on adjudicated rows only — a row with no
    #: recorded verdict has nothing to attribute.
    #:
    #: This is what separates "the candidate wrote a better plan" from "the
    #: incumbent emitted a broken object and anything would have beaten it".
    #: Both are real; only one is a quality claim.
    baseline_malformed: str | None = None

    @property
    def adjudicated(self) -> bool:
        return self.verdict != NOT_ADJUDICATED


class TriageSummary(_Strict):
    """Win/loss/tie counts for one subagent's disagreements.

    ``wins_ge_losses`` is the ``alt`` clause of the ``shadow_agreement`` gate.
    It is computed over the **adjudicated** rows only and reported next to
    ``not_adjudicated``, so the scorecard can see how much of the disagreement
    set the claim actually rests on. This module does not evaluate the gate —
    that is the scorecard's job (T12).
    """

    subagent: str
    disagreements: int
    wins: int = 0
    losses: int = 0
    ties: int = 0
    not_adjudicated: int = 0
    #: reason -> count over the not-adjudicated rows.
    not_adjudicated_reasons: dict[str, int] = Field(default_factory=dict)
    #: Of ``wins`` / ``losses``, how many landed on an item where the baseline
    #: arm's emission was structurally broken (:mod:`amw.shadow.emission`).
    #: Subtracting them gives the quality-only tally.
    wins_baseline_malformed: int = 0
    losses_baseline_malformed: int = 0

    @property
    def adjudicated(self) -> int:
        return self.wins + self.losses + self.ties

    @property
    def wins_ge_losses(self) -> bool | None:
        """None when nothing was adjudicated — not True by vacuity."""
        if not self.adjudicated:
            return None
        return self.wins >= self.losses

    @property
    def quality_wins(self) -> int:
        """Wins on items where the baseline emitted a well-formed payload."""
        return self.wins - self.wins_baseline_malformed

    @property
    def quality_losses(self) -> int:
        return self.losses - self.losses_baseline_malformed

    @property
    def quality_wins_ge_losses(self) -> bool | None:
        """The alt clause again, with the structural failures taken out.

        Reported beside :attr:`wins_ge_losses`, never instead of it. The gate's
        ``alt`` text says "judge-adjudicated wins >= losses" with no exclusion,
        so the overall tally is the one the clause is evaluated on; this is the
        honesty check that says how much of it is a quality claim.
        """
        if not self.adjudicated:
            return None
        return self.quality_wins >= self.quality_losses

    @property
    def clause_text(self) -> str:
        """Whether the pre-registered alt clause holds — on each figure reported.

        Stating this per figure is the point. "Passes on either figure" is a
        much stronger claim than "passes", and where the two figures disagree
        the sentence has to say which one carried it and which one did not,
        rather than quietly reporting the flattering half.
        """
        overall = self.wins_ge_losses
        if overall is None:
            return "the alt clause cannot be evaluated"
        quality = self.quality_wins_ge_losses
        if not (self.wins_baseline_malformed or self.losses_baseline_malformed):
            # One figure, so "either" would be an odd thing to say.
            return (
                "passes the pre-registered alt clause"
                if overall
                else "fails the pre-registered alt clause"
            )
        if overall == quality:
            return "passes on either figure" if overall else "fails on either figure"
        if overall:
            return (
                "passes on the overall tally, which is the figure the clause "
                "names; the quality-only tally does not clear it"
            )
        return (
            "fails on the overall tally, which is the figure the clause names, "
            "though the quality-only tally would clear it"
        )

    def adjudication_text(self, *, baseline_label: str = "baseline") -> str:
        """The one wording used everywhere the adjudication is quoted.

        Fixed by the 2026-08-12 ruling: overall tally first, because that is
        the figure the pre-registered clause names; then the quality-only
        tally with the reason for the exclusion; then whether the clause holds
        on each. One method so the three cannot drift apart across the report.
        """
        if not self.adjudicated:
            return (
                f"no disagreement was adjudicated "
                f"({self.not_adjudicated} not adjudicated)"
            )
        parts = [f"{self.wins}W/{self.losses}L overall"]
        if self.wins_baseline_malformed or self.losses_baseline_malformed:
            parts.append(
                f"{self.quality_wins}W/{self.quality_losses}L excluding "
                f"structurally malformed {baseline_label} emissions"
            )
        else:
            parts.append(
                f"no item was excluded — {baseline_label} emitted a "
                f"well-formed payload on every adjudicated disagreement"
            )
        parts.append(self.clause_text)
        return "; ".join(parts)


# --------------------------------------------------------------------------
# adjudicating one item
# --------------------------------------------------------------------------


def _mean_score(verdicts: Sequence[JudgeVerdict]) -> tuple[float | None, int]:
    """Mean over the repeats that succeeded, and how many those were.

    A failed repeat contributes nothing rather than a zero, exactly as
    :func:`amw.eval.stats.aggregate_repeats` treats it: a judge outage is our
    infrastructure failing, not the arm scoring badly.
    """
    scores = [v.score for v in verdicts if v.status == "ok" and v.score is not None]
    if not scores:
        return None, 0
    return sum(scores) / len(scores), len(scores)


def _judge_requests(
    item: DatasetItem, trace: Trace, *, arm: str, repeats: int
) -> list[JudgeRequest]:
    """Rebuild phase 2's judge requests for one (item, arm), byte for byte.

    This has to match ``amw.eval.runner._judge_arm`` exactly or the replay key
    changes and every lookup misses — the request's ``input_sha`` hashes the
    rendered judge prompt, which embeds the candidate output. It is built from
    the same three helpers that runner uses (:func:`~amw.eval.runner.rubric_of`,
    :func:`~amw.eval.runner.judge_candidate`, the item's own input) rather than
    from a local copy of the logic, so it cannot drift independently.
    """
    return [
        JudgeRequest(
            item_id=item.item_id,
            subagent=item.subagent,
            rubric=rubric_of(item),
            candidate=judge_candidate(trace),
            task_input=list(item.input.messages),
            context_chunks=item.input.context_chunks(),
            reference=item.gold,
            repeat=repeat,
            repeats=repeats,
            arm=arm,
        )
        for repeat in range(1, repeats + 1)
    ]


def _losing_rationale(
    loser: Sequence[JudgeVerdict],
    winner: Sequence[JudgeVerdict],
    *,
    loser_arm: str,
) -> str:
    """The recorded sentence that explains why the loser lost.

    Preference order, all of it quoted from the recording:

    1. a criterion the loser failed and the winner passed — that *is* the
       difference between the two scores;
    2. any criterion the loser failed;
    3. the loser's overall rationale.

    Only the first successful repeat is read. Repeats can disagree (that is why
    ``mean_within_item_sd`` is reported in phase 2), and splicing two repeats'
    sentences together would produce a rationale no judge call ever gave.

    The arm is named in the string because a rationale about "the candidate
    output" is ambiguous once it appears in a table where a win means the
    *baseline* was the one criticised — the judge called both arms "the
    candidate output" in its own prompt.
    """
    loser_ok = next((v for v in loser if v.status == "ok"), None)
    if loser_ok is None:
        return ""
    winner_ok = next((v for v in winner if v.status == "ok"), None)
    winner_passed = (
        {c.criterion_id for c in winner_ok.criteria if c.passed} if winner_ok else set()
    )
    failed = [c for c in loser_ok.criteria if not c.passed]
    for criterion in failed:
        if criterion.criterion_id in winner_passed:
            return _one_line(
                f"{loser_arm} lost on [{criterion.criterion_id}]: {criterion.rationale}"
            )
    if failed:
        return _one_line(
            f"{loser_arm} failed [{failed[0].criterion_id}]: {failed[0].rationale}"
        )
    return _one_line(f"{loser_arm} scored lower; judge said: {loser_ok.overall_rationale}")


def _tie_rationale(candidate: Sequence[JudgeVerdict], *, candidate_arm: str) -> str:
    """For a tie, quote the candidate arm's own summary of its answer."""
    first = next((v for v in candidate if v.status == "ok"), None)
    if first is None:
        return ""
    if first.overall_rationale:
        return _one_line(f"equal scores; on {candidate_arm}: {first.overall_rationale}")
    failed = first.failed_criteria
    return _one_line(
        f"equal scores; {candidate_arm} failed {failed}" if failed else ""
    )


def adjudicate_item(
    item: DatasetItem,
    disagreement: ItemAgreement,
    *,
    baseline_trace: Trace,
    candidate_trace: Trace,
    baseline_arm: str,
    candidate_arm: str,
    judge: Judge,
    repeats: int,
    split_label: str,
) -> TriageRow:
    """Adjudicate one disagreeing item from its recorded judge calls.

    ``judge`` must be in replay mode on the normal demo path; nothing here
    forces that, because the same code adjudicates a live run once its judge
    calls exist. What it will not do is invent a verdict: a judge call that
    cannot be served comes back as an error verdict from
    :meth:`~amw.eval.judge.Judge.score_many` and produces
    :data:`NOT_ADJUDICATED` with the reason attached.
    """
    baseline_verdicts = judge.score_many(
        _judge_requests(item, baseline_trace, arm=baseline_arm, repeats=repeats)
    )
    candidate_verdicts = judge.score_many(
        _judge_requests(item, candidate_trace, arm=candidate_arm, repeats=repeats)
    )
    baseline_score, baseline_n = _mean_score(baseline_verdicts)
    candidate_score, candidate_n = _mean_score(candidate_verdicts)
    baseline_malformed = malformed_emission(item.subagent, baseline_trace)

    row = TriageRow(
        item_id=item.item_id,
        subagent=item.subagent,
        fields=list(disagreement.disagreeing_fields),
        verdict=NOT_ADJUDICATED,
        judged_split=split_label,
        baseline_score=baseline_score,
        candidate_score=candidate_score,
        baseline_repeats=baseline_n,
        candidate_repeats=candidate_n,
        baseline_malformed=baseline_malformed,
    )

    if baseline_score is None or candidate_score is None:
        missing = [
            name
            for name, score in (
                (baseline_arm, baseline_score),
                (candidate_arm, candidate_score),
            )
            if score is None
        ]
        errors = [
            v.error
            for v in (*baseline_verdicts, *candidate_verdicts)
            if v.status != "ok" and v.error
        ]
        row.reason = _one_line(
            f"no recorded judge verdict for {', '.join(missing)}"
            + (f": {errors[0]}" if errors else "")
        )
        return row

    if candidate_score > baseline_score:
        row.verdict = WIN
        row.rationale = _losing_rationale(
            baseline_verdicts, candidate_verdicts, loser_arm=baseline_arm
        )
    elif candidate_score < baseline_score:
        row.verdict = LOSS
        row.rationale = _losing_rationale(
            candidate_verdicts, baseline_verdicts, loser_arm=candidate_arm
        )
    else:
        row.verdict = TIE
        row.rationale = _tie_rationale(candidate_verdicts, candidate_arm=candidate_arm)
    return row


# --------------------------------------------------------------------------
# adjudicating a subagent
# --------------------------------------------------------------------------


def adjudicate(
    subagent: str,
    disagreements: Sequence[ItemAgreement],
    *,
    items: Mapping[str, DatasetItem],
    baseline_traces: Mapping[str, Trace],
    candidate_traces: Mapping[str, Trace],
    baseline_arm: str,
    candidate_arm: str,
    judge: Judge | None,
    repeats: int,
    judged_items: Iterable[str],
    judged_split: str,
) -> list[TriageRow]:
    """Every disagreement for one subagent, adjudicated or labelled.

    ``judged_items`` is the set of item ids phase 2 actually judged for this
    subagent (its core split, or all 70 for Feature Extractor). Anything
    outside it is labelled, not guessed.

    ``judge=None`` labels every row :data:`NOT_ADJUDICATED` — the shape a
    ``--no-judge`` or live-slice run produces. The table still renders, and it
    still says what is missing.
    """
    judged = set(judged_items)
    split_label = SPLIT_LABELS.get(judged_split, judged_split)
    rows: list[TriageRow] = []

    for disagreement in disagreements:
        item_id = disagreement.item_id
        item = items.get(item_id)
        baseline_trace = baseline_traces.get(item_id)
        candidate_trace = candidate_traces.get(item_id)

        if judge is None:
            rows.append(
                TriageRow(
                    item_id=item_id,
                    subagent=subagent,
                    fields=list(disagreement.disagreeing_fields),
                    verdict=NOT_ADJUDICATED,
                    judged_split=OUTSIDE_SPLIT,
                    reason="the judge was not run for this shadow pass",
                )
            )
            continue

        if item_id not in judged or item is None:
            rows.append(
                TriageRow(
                    item_id=item_id,
                    subagent=subagent,
                    fields=list(disagreement.disagreeing_fields),
                    verdict=NOT_ADJUDICATED,
                    judged_split=OUTSIDE_SPLIT,
                    reason=(
                        f"{OUTSIDE_SPLIT}: phase 2 judged {subagent} on the "
                        f"{judged_split} split, which does not include this item, "
                        "so there is no recorded verdict to read"
                    ),
                )
            )
            continue

        if baseline_trace is None or candidate_trace is None:
            rows.append(
                TriageRow(
                    item_id=item_id,
                    subagent=subagent,
                    fields=list(disagreement.disagreeing_fields),
                    verdict=NOT_ADJUDICATED,
                    judged_split=split_label,
                    reason="one arm has no trace for this item",
                )
            )
            continue

        rows.append(
            adjudicate_item(
                item,
                disagreement,
                baseline_trace=baseline_trace,
                candidate_trace=candidate_trace,
                baseline_arm=baseline_arm,
                candidate_arm=candidate_arm,
                judge=judge,
                repeats=repeats,
                split_label=split_label,
            )
        )
    return rows


def summarize(subagent: str, rows: Sequence[TriageRow]) -> TriageSummary:
    """Count the verdicts, keeping the unadjudicated ones visibly separate."""
    summary = TriageSummary(subagent=subagent, disagreements=len(rows))
    for row in rows:
        if row.verdict == WIN:
            summary.wins += 1
            if row.baseline_malformed:
                summary.wins_baseline_malformed += 1
        elif row.verdict == LOSS:
            summary.losses += 1
            if row.baseline_malformed:
                summary.losses_baseline_malformed += 1
        elif row.verdict == TIE:
            summary.ties += 1
        else:
            summary.not_adjudicated += 1
            reason = (row.reason or "unknown").split(":")[0]
            summary.not_adjudicated_reasons[reason] = (
                summary.not_adjudicated_reasons.get(reason, 0) + 1
            )
    return summary


# --------------------------------------------------------------------------
# the table
# --------------------------------------------------------------------------

_TABLE_HEADER = (
    "| item | subagent | disagreeing field(s) | verdict | judged split | "
    "recorded rationale (quoted) |"
)
_TABLE_RULE = "|---|---|---|---|---|---|"


def triage_table_markdown(
    rows: Sequence[TriageRow],
    *,
    summaries: Sequence[TriageSummary] = (),
    title: str = "Disagreement triage",
) -> str:
    """The triage table, Markdown, with its own caveats attached.

    The caveat block is part of the table, not a nicety: the win/loss counts
    are read off a subset of the disagreements, and a table that showed the
    counts without the coverage line would invite exactly the reading the
    coverage line exists to prevent.
    """
    lines = [f"## {title}", ""]
    if summaries:
        lines += [
            "| subagent | disagreements | win | loss | tie | not adjudicated "
            "| of which baseline emission malformed |",
            "|---|---|---|---|---|---|---|",
        ]
        for summary in summaries:
            malformed = (
                f"{summary.wins_baseline_malformed}W/"
                f"{summary.losses_baseline_malformed}L"
            )
            lines.append(
                f"| {summary.subagent} | {summary.disagreements} | {summary.wins} "
                f"| {summary.losses} | {summary.ties} | {summary.not_adjudicated} "
                f"| {malformed} |"
            )
        lines.append("")
        lines.append(
            "Verdicts are from the **candidate's** point of view (win = the Gemini "
            "arm scored higher). Scores are the recorded phase-2 judge scores for "
            "these exact outputs, replayed — no judge call was made to build this "
            "table."
        )
        lines.append("")
        for summary in summaries:
            if not (
                summary.wins_baseline_malformed or summary.losses_baseline_malformed
            ):
                continue
            lines.append(
                f"**{summary.subagent}** adjudication: "
                f"{summary.adjudication_text(baseline_label='Claude baseline')}. "
                + MALFORMED_CAVEAT
            )
            lines.append("")

    lines += [_TABLE_HEADER, _TABLE_RULE]
    for row in rows:
        fields = ", ".join(row.fields) or "—"
        detail = row.rationale or (row.reason or "")
        if row.baseline_malformed:
            # Prefixed, not appended: the reader has to see that the baseline's
            # output was broken before reading the judge's words about it.
            detail = f"[baseline emission malformed — {row.baseline_malformed}] {detail}"
        scores = (
            f"{row.baseline_score:.2f} vs {row.candidate_score:.2f}"
            if row.baseline_score is not None and row.candidate_score is not None
            else "—"
        )
        lines.append(
            f"| {row.item_id} | {row.subagent} | {fields} | {row.verdict} "
            f"({scores}) | {row.judged_split} | {_one_line(detail)} |"
        )
    if not rows:
        lines.append("| — | — | — | — | — | no disagreements to triage |")

    lines += [
        "",
        f"`{NOT_ADJUDICATED}` rows are **not ties.** A tie means the recorded "
        "judge scored both arms equally; not-adjudicated means no recorded "
        "verdict exists for that item — phase 2 judged Query Rewriter and Chunk "
        "Summarizer on the 28-item core split and Feature Extractor on the full "
        "70 (`notes/phase2_n70_validation.md`). Adjudicating the rest would need "
        "new judge calls.",
    ]
    return "\n".join(lines) + "\n"
