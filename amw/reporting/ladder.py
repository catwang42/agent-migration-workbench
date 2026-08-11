"""The ablation ladder, rendered for the scorecard.

``artifacts/results/ablation_{subagent}.json`` is the record of every prompt
variant that was tried. Until now nothing in :mod:`amw.reporting` read it, so
the ladder existed only in the notebook and in ``cli.py ablate``'s stdout —
which meant the two rungs that answer the Feature Extractor regression
(``A4-novelty-*`` by hand, ``A4-optimizer`` by Vertex AI Prompt Optimizer) were
invisible to the artefact the customer keeps.

Three things this module refuses to do, because the ladder is the part of the
report most likely to be read as a leaderboard:

**It does not name a winner.** There is no "best" column, no bolding of the top
row, no sorting by score. Rungs render in ladder order. The hand-tuned rung and
the optimizer-generated rung sit next to each other and the reader compares
them (owner's B1 ruling, rider 2: both stay in the ladder permanently, neither
is labelled the winner). The selection is the owner's to make and it is made
against the gated rows, not against this table.

**It does not silently collapse re-measurements.** The artifact is append-only,
so one rung can appear several times — most often because it was first measured
on the 28-item core split and later widened to the full 70. Those are two
measurements of two different things, so the key is ``(rung, split)`` and both
survive. Only a genuine re-run of the *same* rung on the *same* split
supersedes its predecessor.

**It does not print a score without its n, its split, and its contamination.**
:class:`~amw.reporting.cells.JudgeScoreCell` already welds n and split into the
number. ``leaked_example_items`` is carried the same way: a rung whose prompt
quotes items it is scored on gets a marker in the row and a sentence under the
table, because that score is optimistic by construction and the number alone
does not say so.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from pydantic import BaseModel, ConfigDict, Field

from amw.reporting.cells import EM_DASH, JudgeScoreCell, NOT_MEASURED, estimate_text

__all__ = [
    "CONTAMINATION_MARKER",
    "MIXED_SPLIT_NOTE",
    "NO_WINNER_NOTE",
    "LadderRow",
    "Ladder",
    "ladder_rows",
    "build_ladder",
    "render_ladder",
]

#: Appended to a rung whose prompt quotes items in its own scored split.
CONTAMINATION_MARKER = "†"

#: Printed under any ladder whose rungs were not all scored the same way.
MIXED_SPLIT_NOTE = (
    "Rungs in this ladder were scored on **different splits** ({splits}). A "
    "score at one n and a score at another are two instruments, not two points "
    "on one curve: read down a split, not across the table. The split travels "
    "with every number above for exactly this reason."
)

#: Printed under every ladder, always.
NO_WINNER_NOTE = (
    "The ladder reports every rung that was run, including rungs that did not "
    "help and rungs that were never measured. It ranks nothing and selects "
    "nothing — the shipping arm is chosen against the pre-agreed gates on the "
    "gated rows above, not against this table."
)


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LadderRow(_Base):
    """One rung as the report shows it."""

    rung: str
    label: str
    variant: str
    output_mode: str
    branches_from: str | None = None
    status: str
    unmeasured_reason: str | None = None
    judge: JudgeScoreCell | None = None
    #: metric name -> rendered value, for the metrics this ladder prints.
    metrics: dict[str, str] = Field(default_factory=dict)
    leaked_example_items: list[str] = Field(default_factory=list)

    @property
    def split(self) -> str | None:
        return self.judge.split if self.judge else None

    @property
    def contaminated(self) -> bool:
        return bool(self.leaked_example_items)

    def judge_text(self) -> str:
        """The judged score, marked if the rung saw part of its own answer key."""
        if self.judge is None:
            return NOT_MEASURED
        text = self.judge.render()
        return f"{text} {CONTAMINATION_MARKER}" if self.contaminated else text


class Ladder(_Base):
    """One subagent's ladder, ready to render."""

    subagent: str
    rows: list[LadderRow] = Field(default_factory=list)
    #: Deterministic metric columns, in the order they are printed.
    metric_columns: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @property
    def splits(self) -> list[str]:
        seen: list[str] = []
        for row in self.rows:
            if row.split and row.split not in seen:
                seen.append(row.split)
        return seen


def _judge_cell(record: Any) -> JudgeScoreCell | None:
    """The rung's judged score — or None, when it has no judged score at all.

    A rung can be ``measured`` (its arm ran, deterministic metrics exist) and
    still carry no judge report, because judging is a separate opt-in pass.
    That is a real state and it renders as an absent judge score, never as a
    zero and never as the arm's deterministic accuracy standing in for one.
    """
    arm = getattr(record, "arm", None)
    judge = getattr(arm, "judge", None) if arm is not None else None
    if judge is None:
        return None
    return JudgeScoreCell(
        split=judge.split,
        items_scored=judge.items_scored,
        estimate=judge.estimate,
        point=judge.point,
        arm=getattr(arm, "variant", None),
    )


def _metric_text(arm: Any, name: str) -> str:
    report = (getattr(arm, "metrics", None) or {}).get(name) if arm else None
    if report is None:
        return EM_DASH
    return estimate_text(report.estimate, point=report.point)


def _metric_columns(records: Sequence[Any], requested: Sequence[str] | None) -> list[str]:
    if requested is not None:
        return list(requested)
    columns: list[str] = []
    for record in records:
        arm = getattr(record, "arm", None)
        for name in getattr(arm, "metrics", None) or {}:
            if name not in columns:
                columns.append(name)
    return columns


def ladder_rows(records: Iterable[Any]) -> list[Any]:
    """The current record for each rung, in first-seen rung order.

    The artifact is appended to, never rewritten, so one rung can have several
    records. Two different things cause that and they resolve differently:

    * **The same rung measured on two splits.** Widening a rung from the core
      28 to the full 70 does not supersede the core measurement — it adds a
      second one — so measured records are keyed on ``(rung, split)`` and both
      survive, each carrying its own n.
    * **A rung that was unmeasured and later ran.** ``no_recordings`` is the
      absence of a measurement, not a measurement of absence. Any measured
      record for the rung supersedes every unmeasured one; an unmeasured record
      survives only when the rung was never measured at all.
    """
    measured: dict[tuple[str, str | None], Any] = {}
    unmeasured: dict[str, Any] = {}
    rung_order: list[str] = []
    split_order: dict[str, list[str | None]] = {}

    for record in records:
        rung = record.rung
        if rung not in split_order:
            rung_order.append(rung)
            split_order[rung] = []
        if getattr(record, "status", None) != "measured":
            unmeasured[rung] = record
            continue
        arm = getattr(record, "arm", None)
        judge = getattr(arm, "judge", None) if arm is not None else None
        split = getattr(judge, "split", None)
        if split not in split_order[rung]:
            split_order[rung].append(split)
        measured[(rung, split)] = record

    out: list[Any] = []
    for rung in rung_order:
        splits = split_order[rung]
        if splits:
            out.extend(measured[(rung, split)] for split in splits)
        else:
            out.append(unmeasured[rung])
    return out


def build_ladder(
    result: Any, *, metrics: Sequence[str] | None = None
) -> Ladder:
    """A :class:`Ladder` from an ``AblationResult``.

    Typed as ``Any`` on purpose: :mod:`amw.tuning.ablate` imports the eval
    runner, and the scorecard is rendered on every ``cli.py`` invocation. The
    reporting layer reads the shape, not the class.
    """
    records = ladder_rows(result.rungs)
    columns = _metric_columns(records, metrics)
    rows = [
        LadderRow(
            rung=record.rung,
            label=record.label,
            variant=record.variant,
            output_mode=record.output_mode,
            branches_from=record.branches_from,
            status=record.status,
            unmeasured_reason=record.unmeasured_reason,
            judge=_judge_cell(record),
            metrics={
                name: _metric_text(getattr(record, "arm", None), name)
                for name in columns
            },
            leaked_example_items=list(record.leaked_example_items),
        )
        for record in records
    ]
    return Ladder(
        subagent=result.subagent,
        rows=rows,
        metric_columns=columns,
        notes=list(getattr(result, "notes", []) or []),
    )


def render_ladder(ladder: Ladder) -> list[str]:
    """The ladder as Markdown lines. Pure formatting."""
    if not ladder.rows:
        return []
    header = ["Rung", "Variant", "Output mode", "Judged score (95% CI)"]
    header += [f"`{name}`" for name in ladder.metric_columns]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    for row in ladder.rows:
        cells = [
            f"`{row.rung}`",
            f"`{row.variant}`",
            f"`{row.output_mode}`",
            row.judge_text() if row.status == "measured" else f"**{NOT_MEASURED}**",
        ]
        cells += [row.metrics.get(name, EM_DASH) for name in ladder.metric_columns]
        lines.append("| " + " | ".join(cells) + " |")

    lines += ["", NO_WINNER_NOTE]

    # The reason a rung was not measured goes under the table, not in it. It is
    # a replay-miss diagnostic several lines long, and inlining it turns one
    # cell into the width of the report — which is how a reader ends up
    # skipping the row that says a rung has no numbers.
    unmeasured = [row for row in ladder.rows if row.status != "measured"]
    if unmeasured:
        lines += ["", "**Rungs with no measurement, and why**", ""]
        for row in unmeasured:
            lines.append(
                f"- `{row.rung}` — {row.unmeasured_reason or row.status}"
            )

    splits = ladder.splits
    if len(splits) > 1:
        lines += [
            "",
            MIXED_SPLIT_NOTE.format(splits=", ".join(f"`{s}`" for s in splits)),
        ]

    contaminated = [row for row in ladder.rows if row.contaminated]
    if contaminated:
        lines += [""]
        for row in contaminated:
            leaked = row.leaked_example_items
            items = ", ".join(f"`{i}`" for i in leaked)
            subject = "those items are" if len(leaked) > 1 else "that item is"
            scored = row.judge.items_scored if row.judge else None
            share = f"{len(leaked)} of {scored}" if scored else f"{len(leaked)} item(s)"
            lines.append(
                f"{CONTAMINATION_MARKER} `{row.rung}` quotes {items} as a worked "
                f"example, and {subject} inside the split it was scored on "
                f"({share}). Its judged score is optimistic there by "
                f"construction. The items are not excluded — that would give "
                f"this rung a different denominator from every other rung, "
                f"which is a worse problem than a disclosed one."
            )
    return lines
