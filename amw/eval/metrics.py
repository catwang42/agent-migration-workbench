"""Deterministic metrics — the cheap, un-arguable layer of the eval stack.

These are the numbers a customer can recompute by hand from the recorded
traces, which is exactly why they come first (master_plan §5.4, "metric stack,
cheapest-first"). Four families live here:

* :func:`json_schema_validity` — did the response parse against the subagent's
  frozen ``response_schema`` in ``amw/agents/schemas.py``?
* :func:`filter_prf` / :func:`exact_key_match` — Query Rewriter filter
  extraction, compared key by key against the gold plan.
* :func:`citation_coverage` — Chunk Summarizer groundedness: does *every* key
  point cite at least one chunk, and only chunks that were actually supplied?
* :func:`extraction_field_verdicts` / :func:`extraction_metrics` — Feature
  Extractor, scored three ways (right / wrong / abstained) rather than two.

Two rules shape every signature in this module.

**No fabricated results (ground rule 1).** A metric never returns a number it
did not measure. :class:`MetricOutcome` can only carry a ``value`` when its
``status`` is ``ok``; "undefined" (precision with an empty prediction, citation
coverage over zero claims) is ``not_applicable`` with a reason, and a broken
input is ``error`` with a reason. Nothing here has a default score, and nothing
falls back to ``0.0`` to keep a column populated. The one case that *is* scored
zero is a call the model genuinely failed to answer (``status:"error"`` trace):
that is a measured failure to produce a valid response, not a placeholder, and
:class:`MetricSample` reports how many of those went into any rate.

**Absences must survive aggregation.** :func:`aggregate` keeps the measured
values and a per-reason census of everything it could not measure, so a rate
computed downstream can always print its own denominator. Silently dropping
unmeasurable items is how an eval flatters whichever model failed.

Normalisation is deliberately shallow and documented on
:func:`normalize_scalar`: whitespace collapse and case folding for free text,
upper-casing and space removal for classification codes, and nothing at all for
dates (a date that disagrees on format disagrees, and the prompt pack asks for
``YYYY-MM-DD``). Golden fixtures are hand-computed against these rules.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, model_validator

from amw.agents.schemas import schema_model
from amw.traces.schema import Trace

__all__ = [
    "MetricStatus",
    "MetricOutcome",
    "MetricSample",
    "FieldVerdict",
    "FIELD_VERDICTS",
    "normalize_scalar",
    "normalize_code",
    "extract_payload",
    "json_schema_validity",
    "filter_assertions",
    "filter_prf",
    "exact_key_match",
    "citation_coverage",
    "extraction_field_verdicts",
    "extraction_metrics",
    "deterministic_metrics",
    "aggregate",
    "QR_FILTER_FIELDS",
    "QR_LIST_FILTER_FIELDS",
    "FE_SCALAR_FIELDS",
    "FE_LIST_FIELDS",
    "FE_FIELDS",
    "FE_JUDGED_FIELDS",
    "FE_ALL_FIELDS",
]

MetricStatus = Literal["ok", "not_applicable", "error"]

_WHITESPACE = re.compile(r"\s+")


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# the result type
# --------------------------------------------------------------------------


class MetricOutcome(_Base):
    """One metric, on one item.

    The invariant enforced below is the whole point of the class: ``value`` is
    present **iff** ``status == "ok"``. There is no way to hand back a number
    for something that was not measured, and no way to signal "undefined" with
    a plausible-looking zero.

    :param counts: the raw tallies the value was computed from (``tp``/``fp``/
        ``fn``, ``grounded``/``total``…). Present so a reader can recompute the
        value by hand — the customer-facing promise of this layer.
    :param detail: why a metric is ``not_applicable`` or ``error``. Required in
        those states.
    """

    metric: str
    status: MetricStatus
    value: float | None = None
    counts: dict[str, int] = {}
    detail: str | None = None
    item_id: str | None = None

    @model_validator(mode="after")
    def _value_iff_ok(self) -> "MetricOutcome":
        if self.status == "ok":
            if self.value is None:
                raise ValueError(
                    f"metric {self.metric!r} is ok but carries no value; an "
                    "unmeasured metric must be not_applicable or error"
                )
        else:
            if self.value is not None:
                raise ValueError(
                    f"metric {self.metric!r} is {self.status} but carries "
                    f"value={self.value!r}; a number that was not measured must "
                    "never be emitted (ground rule 1)"
                )
            if not self.detail:
                raise ValueError(
                    f"metric {self.metric!r} is {self.status} without a detail "
                    "string; an absent measurement must say why"
                )
        return self

    @property
    def measured(self) -> bool:
        return self.status == "ok"


def _ok(
    metric: str,
    value: float,
    *,
    counts: Mapping[str, int] | None = None,
    item_id: str | None = None,
    detail: str | None = None,
) -> MetricOutcome:
    return MetricOutcome(
        metric=metric,
        status="ok",
        value=float(value),
        counts=dict(counts or {}),
        detail=detail,
        item_id=item_id,
    )


def _na(
    metric: str,
    detail: str,
    *,
    counts: Mapping[str, int] | None = None,
    item_id: str | None = None,
) -> MetricOutcome:
    return MetricOutcome(
        metric=metric,
        status="not_applicable",
        counts=dict(counts or {}),
        detail=detail,
        item_id=item_id,
    )


def _err(
    metric: str,
    detail: str,
    *,
    counts: Mapping[str, int] | None = None,
    item_id: str | None = None,
) -> MetricOutcome:
    return MetricOutcome(
        metric=metric,
        status="error",
        counts=dict(counts or {}),
        detail=detail,
        item_id=item_id,
    )


class MetricSample(_Base):
    """Every measurement of one metric over one arm, plus what was skipped.

    This is the hand-off to :mod:`amw.eval.stats`: ``values`` is the vector the
    bootstrap resamples, ``item_ids`` is the pairing key for a paired
    bootstrap, and ``excluded`` is the census that lets a report print an
    honest denominator instead of quietly shrinking one.
    """

    metric: str
    values: list[float] = []
    item_ids: list[str] = []
    #: reason -> count, for every item that produced no value.
    excluded: dict[str, int] = {}
    #: item_id -> reason, for the ones that were skipped.
    excluded_items: dict[str, str] = {}
    #: How many of the *scored* items were scored on a failed model call.
    #: A measured zero, not a placeholder — but it has to stay visible.
    call_errors: int = 0

    @model_validator(mode="after")
    def _aligned(self) -> "MetricSample":
        if len(self.values) != len(self.item_ids):
            raise ValueError(
                f"metric {self.metric!r}: {len(self.values)} values but "
                f"{len(self.item_ids)} item ids"
            )
        return self

    @property
    def n(self) -> int:
        """Items that produced a measurement — the real denominator."""
        return len(self.values)

    @property
    def n_excluded(self) -> int:
        return sum(self.excluded.values())

    @property
    def n_considered(self) -> int:
        return self.n + self.n_excluded


def aggregate(
    outcomes: Iterable[MetricOutcome], *, metric: str | None = None
) -> MetricSample:
    """Collect per-item outcomes into a sample for :mod:`amw.eval.stats`.

    Unmeasurable items are *counted*, never dropped: ``excluded`` carries a
    reason census and ``excluded_items`` names the items, so the scorecard can
    say "62 of 70 scored, 8 not applicable" rather than presenting 62 as 70.
    """
    outcomes = list(outcomes)
    names = {o.metric for o in outcomes}
    if metric is None:
        if len(names) > 1:
            raise ValueError(
                f"aggregate() got mixed metrics {sorted(names)}; pass metric= to "
                "be explicit, or group first"
            )
        metric = names.pop() if names else "unknown"
    elif names - {metric}:
        raise ValueError(
            f"aggregate(metric={metric!r}) got outcomes for {sorted(names - {metric})}"
        )

    sample = MetricSample(metric=metric)
    for index, outcome in enumerate(outcomes):
        item_id = outcome.item_id or f"#{index}"
        if outcome.measured:
            sample.values.append(float(outcome.value))  # type: ignore[arg-type]
            sample.item_ids.append(item_id)
            if outcome.counts.get("call_error"):
                sample.call_errors += 1
        else:
            reason = outcome.status
            sample.excluded[reason] = sample.excluded.get(reason, 0) + 1
            sample.excluded_items[item_id] = outcome.detail or reason
    return sample


# --------------------------------------------------------------------------
# normalisation
# --------------------------------------------------------------------------


def normalize_scalar(value: Any) -> Any:
    """Fold free-text differences that are not disagreements.

    Strings: strip, collapse internal whitespace runs to one space, case-fold.
    Everything else (``None``, ints, bools) is returned untouched — in
    particular ``None`` stays ``None``, because "not stated" is an answer and
    must never collapse into the empty string.

    Deliberately shallow. No stemming, no synonym table, no fuzzy match: a
    metric a customer cannot recompute on paper is not a deterministic metric.
    """
    if isinstance(value, str):
        return _WHITESPACE.sub(" ", value.strip()).casefold()
    return value


def normalize_code(value: Any) -> Any:
    """Normalise a classification code (CPC, jurisdiction): upper, no spaces.

    ``"h01m 10/052"`` and ``"H01M10/052"`` are the same code written twice;
    treating them as a false positive plus a false negative would double-count
    a formatting difference as two substantive errors.
    """
    if isinstance(value, str):
        return re.sub(r"\s+", "", value).upper()
    return value


# --------------------------------------------------------------------------
# payload extraction
# --------------------------------------------------------------------------


def extract_payload(source: Trace | Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Pull the structured payload out of a trace (or pass a dict through).

    Returns ``None`` when there is nothing structured to score: an errored
    call, a response the model emitted as prose when a schema was demanded, or
    a JSON array where an object was required. Callers must handle ``None``
    explicitly — this function never invents an empty object, because an empty
    object scores like a very cautious model rather than like a failure.
    """
    if source is None:
        return None
    if isinstance(source, Trace):
        payload = source.output.json_
    elif isinstance(source, Mapping):
        payload = source
    else:  # pragma: no cover - guarded by typing
        raise TypeError(f"cannot extract a payload from {type(source).__name__}")
    if isinstance(payload, Mapping):
        return dict(payload)
    return None


# --------------------------------------------------------------------------
# 1. JSON schema validity
# --------------------------------------------------------------------------

_METRIC_SCHEMA_VALIDITY = "json_schema_validity"


def json_schema_validity(
    subagent: str,
    source: Trace | Mapping[str, Any] | None,
    *,
    item_id: str | None = None,
) -> MetricOutcome:
    """1.0 if the response validates against the subagent's frozen schema.

    Three inputs, three honest answers:

    * a payload that validates → ``1.0``;
    * a payload that does not validate, or prose where JSON was demanded →
      ``0.0``, with the pydantic complaint in ``detail``;
    * a ``status:"error"`` trace → ``0.0`` as well, tagged
      ``counts={"call_error": 1}``.

    That last one is the only place this module scores a failure it did not
    parse, so it is worth being explicit about why. The gate reads "the
    fraction of responses parsing against the subagent response_schema"; a call
    that exhausted its retry budget produced no such response, and the customer
    experienced exactly that. Excluding it would shrink the denominator and
    flatter whichever backend fell over. It is *counted separately* rather than
    hidden, so :class:`MetricSample.call_errors` can be printed next to the
    rate.
    """
    model = schema_model(subagent)  # raises KeyError on an unknown subagent
    trace_status = source.status if isinstance(source, Trace) else "ok"
    trace_error = source.error if isinstance(source, Trace) else None

    if trace_status == "error":
        return _ok(
            _METRIC_SCHEMA_VALIDITY,
            0.0,
            counts={"call_error": 1},
            item_id=item_id,
            detail=f"call failed, so no response could parse: {trace_error}",
        )

    payload = extract_payload(source)
    if payload is None:
        return _ok(
            _METRIC_SCHEMA_VALIDITY,
            0.0,
            counts={"no_json": 1},
            item_id=item_id,
            detail="response carried no JSON object (a schema was required)",
        )
    try:
        model.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 - any validation failure is a 0
        return _ok(
            _METRIC_SCHEMA_VALIDITY,
            0.0,
            counts={"invalid": 1},
            item_id=item_id,
            detail=f"{type(exc).__name__}: {exc}".split("\n")[0],
        )
    return _ok(_METRIC_SCHEMA_VALIDITY, 1.0, counts={"valid": 1}, item_id=item_id)


# --------------------------------------------------------------------------
# 2. Query Rewriter: filter precision / recall + exact-key match
# --------------------------------------------------------------------------

#: Scalar filters on :class:`~amw.agents.schemas.QueryFilters`. ``null`` means
#: "the user did not constrain this", so an unset scalar contributes nothing.
QR_SCALAR_FILTER_FIELDS: tuple[str, ...] = ("date_from", "date_to")
#: List-valued filters. Each element is one assertion.
QR_LIST_FILTER_FIELDS: tuple[str, ...] = ("assignees", "jurisdictions", "cpc_codes")
QR_FILTER_FIELDS: tuple[str, ...] = QR_SCALAR_FILTER_FIELDS + QR_LIST_FILTER_FIELDS

#: Fields normalised as codes rather than as free text.
_CODE_FIELDS = frozenset({"jurisdictions", "cpc_codes"})


def filter_assertions(
    filters: Mapping[str, Any] | None,
    *,
    fields: Sequence[str] = QR_FILTER_FIELDS,
) -> set[tuple[str, Any]]:
    """Flatten a filters object into a set of ``(field, value)`` assertions.

    This is what makes the metric "key by key" rather than "does the blob
    match": ``{"assignees": ["Toyota", "LG"]}`` becomes two assertions, so
    getting one of two right scores 0.5 recall instead of 0.

    A ``null`` scalar and an empty list both contribute nothing — the model
    asserted nothing about that dimension, which is neither a hit nor a miss.
    """
    filters = filters or {}
    out: set[tuple[str, Any]] = set()
    for field in fields:
        raw = filters.get(field)
        if raw is None:
            continue
        norm = normalize_code if field in _CODE_FIELDS else normalize_scalar
        if isinstance(raw, (list, tuple, set)):
            for element in raw:
                if element is None or element == "":
                    continue
                out.add((field, norm(element)))
        else:
            if raw == "":
                continue
            out.add((field, norm(raw)))
    return out


def filter_prf(
    gold: Mapping[str, Any] | None,
    predicted: Mapping[str, Any] | None,
    *,
    fields: Sequence[str] = QR_FILTER_FIELDS,
    item_id: str | None = None,
) -> dict[str, MetricOutcome]:
    """Precision / recall / F1 over filter assertions.

    ``gold`` and ``predicted`` are :class:`~amw.agents.schemas.QueryPlan`
    payloads (or bare ``filters`` mappings — both are accepted).

    The undefined cases are reported as undefined, not as zero:

    * the model asserted no filters at all → **precision is not applicable**
      (there is nothing to be precise about). Recall is still 0.0 if the gold
      had filters, which is the real, measurable miss.
    * the gold item has no filters → **recall is not applicable**. Precision is
      still 0.0 if the model invented some, which is the real, measurable
      over-extraction.
    * neither side asserts anything → both are not applicable, and F1 with
      them. A model that correctly extracted nothing has not demonstrated
      filter skill, and scoring it 1.0 would inflate the arm's mean with items
      that tested nothing.
    """
    gold_set = filter_assertions(_filters_of(gold), fields=fields)
    pred_set = filter_assertions(_filters_of(predicted), fields=fields)

    tp = len(gold_set & pred_set)
    fp = len(pred_set - gold_set)
    fn = len(gold_set - pred_set)
    counts = {"tp": tp, "fp": fp, "fn": fn}

    if pred_set:
        precision = _ok("filter_precision", tp / (tp + fp), counts=counts, item_id=item_id)
    else:
        precision = _na(
            "filter_precision",
            "the response asserted no filters, so precision is undefined "
            "(0/0) — not zero",
            counts=counts,
            item_id=item_id,
        )

    if gold_set:
        recall = _ok("filter_recall", tp / (tp + fn), counts=counts, item_id=item_id)
    else:
        recall = _na(
            "filter_recall",
            "the gold plan asserts no filters, so recall is undefined (0/0) "
            "— not zero",
            counts=counts,
            item_id=item_id,
        )

    if precision.measured and recall.measured:
        p, r = precision.value, recall.value
        assert p is not None and r is not None
        f1_value = 0.0 if (p + r) == 0 else 2 * p * r / (p + r)
        f1 = _ok("filter_f1", f1_value, counts=counts, item_id=item_id)
    else:
        f1 = _na(
            "filter_f1",
            "F1 needs both precision and recall; "
            + "; ".join(
                o.detail
                for o in (precision, recall)
                if not o.measured and o.detail
            ),
            counts=counts,
            item_id=item_id,
        )

    return {"filter_precision": precision, "filter_recall": recall, "filter_f1": f1}


def _filters_of(payload: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    """Accept either a whole QueryPlan payload or a bare filters mapping."""
    if payload is None:
        return None
    if "filters" in payload and isinstance(payload["filters"], Mapping):
        return payload["filters"]
    return payload


def exact_key_match(
    gold: Mapping[str, Any] | None,
    predicted: Mapping[str, Any] | None,
    key: str,
    *,
    item_id: str | None = None,
    metric: str | None = None,
) -> MetricOutcome:
    """1.0 if ``predicted[key]`` equals ``gold[key]`` after normalisation.

    Used for closed-vocabulary fields such as ``QueryPlan.intent``. If the gold
    does not carry the key there is nothing to compare against, so the outcome
    is ``not_applicable`` — never a free 1.0.
    """
    name = metric or f"exact_match_{key}"
    if gold is None or key not in gold:
        return _na(
            name,
            f"the gold output does not carry {key!r}, so there is nothing to "
            "match against",
            item_id=item_id,
        )
    if predicted is None:
        return _ok(
            name,
            0.0,
            counts={"missing_prediction": 1},
            item_id=item_id,
            detail="no structured response to compare",
        )
    want = normalize_scalar(gold.get(key))
    got = normalize_scalar(predicted.get(key))
    return _ok(name, 1.0 if want == got else 0.0, item_id=item_id)


# --------------------------------------------------------------------------
# 3. Chunk Summarizer: citation coverage
# --------------------------------------------------------------------------


def citation_coverage(
    payload: Mapping[str, Any] | None,
    provided_chunk_ids: Iterable[str],
    *,
    item_id: str | None = None,
) -> dict[str, MetricOutcome]:
    """Is *every* claim grounded in a chunk that was actually supplied?

    A key point counts as grounded when it cites at least one chunk id **and**
    every id it cites was in ``provided_chunk_ids``. Both halves matter:
    citing nothing is an unsupported claim, and citing ``c9`` when only
    ``c1..c3`` were supplied is a fabricated citation, which is worse.

    Returns three outcomes:

    ``citation_coverage``
        grounded points / total points.
    ``uncited_claim_rate``
        points citing no chunk at all / total points.
    ``fabricated_citation_rate``
        points citing at least one id that was never supplied / total points.

    A summary with **no key points** yields ``not_applicable`` for all three,
    not 1.0. Vacuous perfection is exactly the kind of number that would make a
    degenerate empty response look like the best model in the run.
    """
    provided = {str(c) for c in provided_chunk_ids}
    metrics = ("citation_coverage", "uncited_claim_rate", "fabricated_citation_rate")

    if payload is None:
        # Coverage is a measured 0: the customer got no grounded claim, and
        # excluding failed calls would let a backend that falls over on half
        # its traffic look perfectly grounded on the other half. The two
        # *diagnostic* rates are genuinely undefined — a response that made no
        # claim did not make an uncited one either, and scoring those 0.0 would
        # credit a failure as good behaviour.
        return {
            "citation_coverage": _ok(
                "citation_coverage",
                0.0,
                counts={"missing_response": 1, "call_error": 1},
                item_id=item_id,
                detail="no structured response, so no claim was grounded",
            ),
            "uncited_claim_rate": _na(
                "uncited_claim_rate",
                "no structured response, so there were no claims to leave uncited",
                counts={"missing_response": 1},
                item_id=item_id,
            ),
            "fabricated_citation_rate": _na(
                "fabricated_citation_rate",
                "no structured response, so there were no citations to fabricate",
                counts={"missing_response": 1},
                item_id=item_id,
            ),
        }

    points = payload.get("key_points")
    if not isinstance(points, list):
        return {
            name: _err(
                name,
                f"key_points is {type(points).__name__}, not a list; the "
                "response does not have a scoreable claim structure",
                item_id=item_id,
            )
            for name in metrics
        }
    if not points:
        return {
            name: _na(
                name,
                "the response contains no key points, so per-claim "
                "groundedness is undefined — an empty summary is not fully "
                "grounded, it is unscoreable here (see json_schema_validity "
                "and the judge for whether it is an acceptable answer)",
                counts={"total_points": 0},
                item_id=item_id,
            )
            for name in metrics
        }

    total = len(points)
    grounded = uncited = fabricated = 0
    for point in points:
        cited = point.get("chunk_ids") if isinstance(point, Mapping) else None
        cited = [str(c) for c in cited] if isinstance(cited, list) else []
        if not cited:
            uncited += 1
            continue
        unknown = [c for c in cited if c not in provided]
        if unknown:
            fabricated += 1
            continue
        grounded += 1

    counts = {
        "total_points": total,
        "grounded_points": grounded,
        "uncited_points": uncited,
        "fabricated_citation_points": fabricated,
    }
    return {
        "citation_coverage": _ok(
            "citation_coverage", grounded / total, counts=counts, item_id=item_id
        ),
        "uncited_claim_rate": _ok(
            "uncited_claim_rate", uncited / total, counts=counts, item_id=item_id
        ),
        "fabricated_citation_rate": _ok(
            "fabricated_citation_rate", fabricated / total, counts=counts, item_id=item_id
        ),
    }


# --------------------------------------------------------------------------
# 4. Feature Extractor: right / wrong / abstained
# --------------------------------------------------------------------------

FieldVerdict = Literal[
    "correct",
    "correct_abstention",
    "wrong",
    "hallucination",
    "omission",
    "unscoreable",
]

#: Verdict order used in reports and in the ``counts`` maps below.
FIELD_VERDICTS: tuple[str, ...] = (
    "correct",
    "correct_abstention",
    "wrong",
    "hallucination",
    "omission",
    "unscoreable",
)

#: Free-text fields scored by the rubric judge, not by exact match.
#:
#: **Changed 2026-08-07, before any baseline number was produced.**
#: ``technical_field`` and ``novelty_statement`` are open-vocabulary
#: characterisations — the schema asks for "one short phrase" and "one
#: sentence, quoted or closely paraphrased". Exact match cannot score those.
#: In the review sample a gold ``technical_field`` of
#: ``"millimetre-wave hybrid beamforming"`` would mark a model answering
#: ``"beam management in wireless networks"`` *wrong*, though both are correct
#: labels for the same document. Across the sample, 20 of 80 scoreable fields
#: (25%) were open text, so a quarter of ``extraction_accuracy`` was measuring
#: phrasing agreement rather than extraction.
#:
#: Worse for ground rule 1: a correct in-source paraphrase was being tallied as
#: a ``hallucination``, filing it beside genuine fabrication. That is the one
#: distinction ``hallucination_rate`` exists to make, so the two had to be
#: separated. The judge scores these fields 0/1 — right meaning in different
#: words is right, wrong or unsupported-by-the-source is 0 — which keeps the
#: fabrication check without pretending phrasing is a measurement.
#:
#: The change is symmetric across arms, so it moves no delta; it was made
#: pre-baseline so no reported comparison was ever computed the old way.
FE_JUDGED_FIELDS: tuple[str, ...] = ("technical_field", "novelty_statement")

#: Nullable scalars on :class:`~amw.agents.schemas.PatentFeatures` whose value
#: is closed-vocabulary or verbatim, and so is exact-matchable.
FE_SCALAR_FIELDS: tuple[str, ...] = (
    "title",
    "assignee",
    "filing_date",
    "jurisdiction",
    "independent_claim_count",
)
#: List fields, where "empty" plays the role ``null`` plays for a scalar.
FE_LIST_FIELDS: tuple[str, ...] = ("cpc_codes",)
#: What :func:`extraction_metrics` scores by default.
FE_FIELDS: tuple[str, ...] = FE_SCALAR_FIELDS + FE_LIST_FIELDS
#: Every field on the schema, in schema order. For callers that need the whole
#: surface — a coverage check, a report table — rather than the scored subset.
FE_ALL_FIELDS: tuple[str, ...] = (
    "title",
    "assignee",
    "filing_date",
    "jurisdiction",
    "technical_field",
    "independent_claim_count",
    "novelty_statement",
    "cpc_codes",
)


def _is_abstention(value: Any) -> bool:
    """``null`` for a scalar, ``[]`` for a list: "not stated in the source"."""
    if value is None:
        return True
    if isinstance(value, (list, tuple, set)) and len(value) == 0:
        return True
    return False


#: Feature Extractor fields normalised as classification codes, not free text.
_FE_CODE_FIELDS = frozenset({"cpc_codes", "jurisdiction"})


def _comparable(field: str, value: Any) -> Any:
    norm = normalize_code if field in _FE_CODE_FIELDS else normalize_scalar
    if isinstance(value, (list, tuple, set)):
        # Set semantics: the schema does not promise an order, so two
        # extractions listing the same CPC codes differently are one answer.
        return frozenset(norm(v) for v in value)
    return norm(value)


def extraction_field_verdicts(
    gold: Mapping[str, Any] | None,
    predicted: Mapping[str, Any] | None,
    *,
    fields: Sequence[str] = FE_FIELDS,
) -> dict[str, FieldVerdict]:
    """Classify each field into the five-way table below.

    ``null`` in this schema means "not stated in the source"
    (``amw/agents/schemas.py``), so abstaining is a *claim* and has to be
    scored as one. Collapsing it into "wrong" would make an extractor that
    invents a plausible assignee indistinguishable from one that correctly says
    it does not know — and the fabricator is strictly the more dangerous model
    to put in front of a customer's RAG pipeline (ground rule 1).

    ==================  ==========================  ======================
    gold \\ predicted    abstained (null / [])       asserted a value
    ==================  ==========================  ======================
    not stated (null)   ``correct_abstention``      ``hallucination``
    stated, matches     —                           ``correct``
    stated, differs     ``omission``                ``wrong``
    ==================  ==========================  ======================

    A field missing from ``gold`` altogether is ``unscoreable`` — the gold does
    not say what the right answer is, and guessing would be fabrication.
    """
    gold = gold or {}
    verdicts: dict[str, FieldVerdict] = {}
    for field in fields:
        if field not in gold:
            verdicts[field] = "unscoreable"
            continue
        if predicted is None or field not in predicted:
            # The response never mentioned the key. For a schema whose optional
            # scalars default to null, that is the same claim as null.
            pred_value = None
        else:
            pred_value = predicted[field]
        gold_value = gold[field]

        gold_abstains = _is_abstention(gold_value)
        pred_abstains = _is_abstention(pred_value)

        if gold_abstains and pred_abstains:
            verdicts[field] = "correct_abstention"
        elif gold_abstains and not pred_abstains:
            verdicts[field] = "hallucination"
        elif not gold_abstains and pred_abstains:
            verdicts[field] = "omission"
        elif _comparable(field, gold_value) == _comparable(field, pred_value):
            verdicts[field] = "correct"
        else:
            verdicts[field] = "wrong"
    return verdicts


def extraction_metrics(
    gold: Mapping[str, Any] | None,
    predicted: Mapping[str, Any] | None,
    *,
    fields: Sequence[str] = FE_FIELDS,
    item_id: str | None = None,
) -> dict[str, MetricOutcome]:
    """Turn the five-way field table into four metrics that separate the modes.

    ``extraction_accuracy``
        ``(correct + correct_abstention) / scoreable``. The headline number.
        Abstaining on a field the source never stated is *right*, so it counts.

    ``answered_precision``
        ``correct / (fields the model chose to answer)``. Undefined — not zero
        — if the model answered nothing.

    ``hallucination_rate``
        ``hallucination / (fields the gold says are not stated)``. Undefined if
        the gold states every field, which is a real property of the item, not
        a score of 0.

    ``omission_rate``
        ``omission / (fields the gold does state)``. Undefined if the gold
        states nothing.

    One number cannot separate an abstainer from a fabricator: both can land at
    the same accuracy. ``answered_precision`` and ``hallucination_rate`` split
    them — a fabricator answers everything with low precision and a high
    hallucination rate, an over-cautious model has high answered precision and
    a high omission rate. That distinction is the point of the ``null``
    convention in the schema, so the metrics have to preserve it.
    """
    verdicts = extraction_field_verdicts(gold, predicted, fields=fields)
    counts = {name: 0 for name in FIELD_VERDICTS}
    for verdict in verdicts.values():
        counts[verdict] += 1

    scoreable = sum(v for k, v in counts.items() if k != "unscoreable")
    gold_null = counts["correct_abstention"] + counts["hallucination"]
    gold_stated = counts["correct"] + counts["wrong"] + counts["omission"]
    answered = counts["correct"] + counts["wrong"] + counts["hallucination"]
    counts_out = dict(counts)
    counts_out.update(
        scoreable=scoreable,
        gold_null=gold_null,
        gold_stated=gold_stated,
        answered=answered,
    )

    out: dict[str, MetricOutcome] = {}

    if scoreable:
        out["extraction_accuracy"] = _ok(
            "extraction_accuracy",
            (counts["correct"] + counts["correct_abstention"]) / scoreable,
            counts=counts_out,
            item_id=item_id,
        )
    else:
        out["extraction_accuracy"] = _na(
            "extraction_accuracy",
            "the gold output scores no field for this item",
            counts=counts_out,
            item_id=item_id,
        )

    if answered:
        out["answered_precision"] = _ok(
            "answered_precision",
            counts["correct"] / answered,
            counts=counts_out,
            item_id=item_id,
        )
    else:
        out["answered_precision"] = _na(
            "answered_precision",
            "the response asserted no field values, so answered precision is "
            "undefined (0/0) — abstaining everywhere is measured by "
            "omission_rate, not by a precision of zero",
            counts=counts_out,
            item_id=item_id,
        )

    if gold_null:
        out["hallucination_rate"] = _ok(
            "hallucination_rate",
            counts["hallucination"] / gold_null,
            counts=counts_out,
            item_id=item_id,
        )
    else:
        out["hallucination_rate"] = _na(
            "hallucination_rate",
            "the gold output states every scoreable field, so this item offers "
            "no opportunity to hallucinate",
            counts=counts_out,
            item_id=item_id,
        )

    if gold_stated:
        out["omission_rate"] = _ok(
            "omission_rate",
            counts["omission"] / gold_stated,
            counts=counts_out,
            item_id=item_id,
        )
    else:
        out["omission_rate"] = _na(
            "omission_rate",
            "the gold output states no field, so there is nothing to omit",
            counts=counts_out,
            item_id=item_id,
        )

    return out


# --------------------------------------------------------------------------
# convenience: everything deterministic for one subagent item
# --------------------------------------------------------------------------


def deterministic_metrics(
    subagent: str,
    *,
    gold: Mapping[str, Any] | None,
    source: Trace | Mapping[str, Any] | None,
    provided_chunk_ids: Iterable[str] = (),
    item_id: str | None = None,
) -> dict[str, MetricOutcome]:
    """Every deterministic metric that applies to one item of one subagent.

    A thin composition helper for T09's runner: it decides *which* metrics a
    subagent has, and nothing else. Unknown subagents raise rather than
    returning an empty dict, so a typo cannot silently produce an item with no
    measurements.
    """
    schema_model(subagent)  # validate the name up front
    predicted = extract_payload(source)
    out = {
        "json_schema_validity": json_schema_validity(
            subagent, source, item_id=item_id
        )
    }

    if subagent == "query_rewriter":
        out.update(filter_prf(gold, predicted, item_id=item_id))
        out["exact_match_intent"] = exact_key_match(
            gold, predicted, "intent", item_id=item_id
        )
    elif subagent == "chunk_summarizer":
        out.update(
            citation_coverage(predicted, provided_chunk_ids, item_id=item_id)
        )
    elif subagent == "feature_extractor":
        out.update(extraction_metrics(gold, predicted, item_id=item_id))
    return out
