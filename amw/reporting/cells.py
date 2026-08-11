"""Table cells that cannot be rendered without the thing that qualifies them.

Every cell in the Migration Readiness Scorecard is produced here, and the
module exists for one reason: on this build there are four numbers whose
caveat is not decoration but *part of the measurement*, and a caveat that
lives in a footnote is a caveat a reader can quote the number without.

So the qualification is welded into the cell by construction, not by
convention:

* :class:`ClaudeSchemaValidityCell` — Claude's ``json_schema_validity`` is
  0.814/0.971/0.957 against a 0.99 gate, and that gap is a *mechanism*
  artefact, not a model ceiling. This org's Vertex policy
  (``constraints/vertexai.allowedPartnerModelFeatures``) blocks partner-model
  structured outputs, so the Claude baseline emits through a tool call while
  Gemini's tuned rung uses an enforced ``response_schema``
  (``notes/org_policy_structured_outputs.md``). ``render()`` has no branch
  that omits :data:`CLAUDE_SCHEMA_CAVEAT`.
* :class:`JudgeScoreCell` — ``split`` and ``items_scored`` are *required*
  fields, so a judge score whose n and split are unknown cannot be
  constructed. Every **gated** judge score is now full-70 (QR and CS were
  widened on 2026-08-11, sizing deviation #2), but the **ablation ladder** is a
  separate instrument with its own split, so two judge scores can still come
  from different n. They are not directly comparable and the number alone does
  not say so.
* :func:`latency_cell` — Claude ran in ``global`` and Gemini in
  ``us-central1``, so p95 is a cross-region comparison. Without a same-region
  probe the cell is the disclosure string and nothing else.
* :func:`cost_cell` — ``config/pricing.yaml`` is unverified, so every cost and
  savings cell is an em dash. Deliberately digit-free: a reason containing a
  number is a number a reader can mistake for a measurement.
* :func:`delta_failure_kind` — a failing paired delta says *which* kind of
  failure it is. FE's ``quality_delta_pp`` is −10.44 pp [−13.78, −7.12], an
  interval entirely below zero: a regression was measured. CS's is
  −2.32 pp [−5.00, +0.36], an interval spanning zero: parity was not
  demonstrated, which is a failure of precision and not a demonstrated drop.
  Both are FAIL and the gate logic is identical; printing one string for both
  would let a reader take the weaker finding for the stronger one.

The scorecard renderer imports these and never formats a value itself.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from amw.eval.stats import Estimate

__all__ = [
    "CLAUDE_SCHEMA_CAVEAT",
    "REGION_SPLIT_DISCLOSURE",
    "EM_DASH",
    "NOT_MEASURED",
    "ClaudeSchemaValidityCell",
    "JudgeScoreCell",
    "cost_cell",
    "latency_cell",
    "estimate_text",
    "paired_delta_text",
    "gate_result_text",
    "delta_failure_kind",
    "FAIL_REGRESSION",
    "FAIL_IMPRECISE",
    "IMPRECISION_NOTE",
]

#: Verbatim, owner-specified. Scoped to this organization's policy configuration
#: exactly as ``notes/org_policy_structured_outputs.md`` requires: it is an
#: environment finding, and generalising it into a product limitation would be
#: the reporting equivalent of a fabricated result.
CLAUDE_SCHEMA_CAVEAT = (
    "tool-use JSON; native structured outputs unavailable under this org's "
    "policy — not the model's ceiling."
)

#: Verbatim, owner-specified. Rendered *instead of* a latency number whenever
#: the two arms were measured in different regions.
REGION_SPLIT_DISCLOSURE = "not comparable — region split disclosed"

EM_DASH = "—"

NOT_MEASURED = "not measured"


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _fmt(value: float, unit: str) -> str:
    """A single number in the unit it was measured in.

    Percentage-point deltas get an explicit sign because the gates are signed
    (``min: -2.0``): "0.4 pp" and "-0.4 pp" differ by one character and by the
    entire verdict.
    """
    if unit == "percentage_points":
        return f"{value:+.2f} pp"
    if unit == "percent":
        return f"{value:.1f}%"
    if unit == "ms":
        return f"{value:,.0f} ms"
    return f"{value:.3f}"


def estimate_text(estimate: Estimate | None, *, point: float | None = None) -> str:
    """``0.957 [0.900, 1.000]`` — or an explicit absence.

    Three states stay distinct (ground rule 1): no measurement, a bare mean
    with no interval (n=1, where a bootstrap would report a fake zero width),
    and a mean with its interval.
    """
    if estimate is None:
        if point is None:
            return NOT_MEASURED
        return f"{point:.3f} (no CI, n=1)"
    return (
        f"{_fmt(estimate.point, estimate.unit)} "
        f"[{_fmt(estimate.lo, estimate.unit)}, {_fmt(estimate.hi, estimate.unit)}]"
    )


class ClaudeSchemaValidityCell(_Base):
    """Claude's schema-validity number and its mechanism caveat, inseparably.

    There is no accessor that returns the number alone. That is the whole
    point of the type: the measured 0.814 on Query Rewriter is real and must be
    shown, and shown next to a 0.99 gate it reads as "Claude cannot hold a
    schema", which the evidence does not support.
    """

    estimate: Estimate | None = None
    point: float | None = None

    @model_validator(mode="after")
    def _something_to_qualify(self) -> "ClaudeSchemaValidityCell":
        if self.estimate is None and self.point is None:
            raise ValueError(
                "a ClaudeSchemaValidityCell with no measurement has nothing to "
                "caveat; render cells.NOT_MEASURED instead"
            )
        return self

    def render(self) -> str:
        return f"{estimate_text(self.estimate, point=self.point)} — {CLAUDE_SCHEMA_CAVEAT}"

    def __str__(self) -> str:  # so an accidental f-string still carries it
        return self.render()


class JudgeScoreCell(_Base):
    """A judged score that cannot exist without its judged n and split.

    ``split`` and ``items_scored`` have no defaults on purpose. A reader
    comparing two judged scores without seeing their n and split is comparing
    two instruments, and the score alone hides it. The gated scores are all
    full-70 as of 2026-08-11; ladder rungs carry whichever split they were run
    on, which is what makes this field load-bearing rather than decorative.
    """

    split: str
    items_scored: int
    estimate: Estimate | None = None
    point: float | None = None
    arm: str | None = None

    def render(self) -> str:
        return (
            f"{estimate_text(self.estimate, point=self.point)} "
            f"(judged n={self.items_scored}, split={self.split})"
        )

    def __str__(self) -> str:
        return self.render()


def paired_delta_text(estimate: Estimate) -> str:
    """A paired delta, with how many items it was actually paired over.

    ``n`` on a paired estimate is the number of *pairs*, and arms can differ in
    how many items they scored (an errored call scores nothing). "+1.34 pp over
    68 pairs, 2 items dropped" and "+1.34 pp over 70 pairs" are different
    findings, so the pairing travels with the number.
    """
    text = estimate_text(estimate)
    if estimate.method != "paired_percentile_bootstrap":
        return text
    detail = f"paired n={estimate.paired_n if estimate.paired_n is not None else estimate.n}"
    if estimate.dropped_unpaired:
        detail += f", {estimate.dropped_unpaired} unpaired dropped"
    return f"{text} ({detail})"


def cost_cell(*, prices_verified: bool, value_text: str | None = None) -> str:
    """A cost or savings cell.

    While ``config/pricing.yaml`` is unverified this is an em dash and a
    digit-free reason. Not a zero, not "TBD 30%", not a placeholder: ground
    rule 3 says prices come only from that file, and a reason carrying a number
    is a number somebody will screenshot.
    """
    if not prices_verified:
        return f"{EM_DASH} (prices unverified — run scripts/refresh_pricing.py)"
    if value_text is None:
        return NOT_MEASURED
    return value_text


def latency_cell(
    estimate: Estimate | None,
    *,
    same_region_probe: bool,
    candidate_region: str = "",
) -> str:
    """p95 latency, or the region-split disclosure — and nothing in between.

    A cross-region p95 is not a slower-or-faster finding, it is two different
    measurements, so this renders :data:`REGION_SPLIT_DISCLOSURE` *exactly*:
    no number, no interval, and no parenthetical that could be read as one.
    The regions themselves are stated once, in the footer, next to where they
    were read from. The ``latency_p95`` gate is reported as not evaluated in
    that case — never as passed (see :mod:`amw.reporting.evidence`).
    """
    if not same_region_probe or estimate is None:
        return REGION_SPLIT_DISCLOSURE
    return f"{estimate_text(estimate)} (same-region probe, {candidate_region})"


def gate_result_text(status: Literal["pass", "fail", "not_evaluated"]) -> str:
    return {"pass": "PASS", "fail": "FAIL", "not_evaluated": "not evaluated"}[status]


#: The two ways a paired delta gate fails, and the words for each.
#:
#: Both are FAIL. The gate logic does not move and neither does the gates hash;
#: what moves is the sentence next to it, because these are not the same
#: finding and a reader who sees one string for both will draw one conclusion
#: from two different measurements.
FAIL_REGRESSION = "measured regression"
FAIL_IMPRECISE = "parity not demonstrated"

#: Printed under any gate table containing an imprecise failure.
IMPRECISION_NOTE = (
    "A `{gate}` failure marked *{imprecise}* means the confidence interval "
    "spans zero: the gate fails on **precision**, because the data cannot rule "
    "out a drop larger than the bound — not because a drop was demonstrated. "
    "A failure marked *{regression}* is the stronger finding: the entire "
    "interval is below zero, so a real drop was measured. Reporting the first "
    "as though it were the second overstates a negative result, which is the "
    "same error as overstating a positive one."
)


def delta_failure_kind(estimate: Estimate | None) -> str | None:
    """Which kind of failure a paired delta is — or ``None`` if not applicable.

    The discriminator is whether the interval contains zero, and it is a real
    distinction rather than a softening of bad news. Feature Extractor's
    ``quality_delta_pp`` is −10.44 pp [−13.78, −7.12]: every plausible value is
    a loss, so a regression was *measured*. Chunk Summarizer's is
    −2.32 pp [−5.00, +0.36]: the interval includes zero and a little above it,
    so what the data establishes is that parity was **not demonstrated** at the
    pre-agreed bound — not that quality dropped.

    Both fail the gate, and neither verdict changes. Only the wording does.

    Returns ``None`` for anything that is not a paired bootstrap delta. On a
    level metric such as ``json_schema_validity`` zero is not the reference
    point, so "spans zero" carries no meaning and inventing one would be worse
    than saying nothing.
    """
    if estimate is None or estimate.method != "paired_percentile_bootstrap":
        return None
    if estimate.hi < 0:
        return FAIL_REGRESSION
    if estimate.lo <= 0 <= estimate.hi:
        return FAIL_IMPRECISE
    # Interval entirely above zero yet still short of the bound: an improvement
    # too small to clear a positive bound. Neither of the two words fits, and
    # the number in the adjacent cell already says it.
    return None
