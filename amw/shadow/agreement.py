"""Task-level agreement between two shadow arms, field by field.

Agreement answers a different question from quality. Quality asks "is this
output good"; agreement asks "would swapping the backend change what the
downstream pipeline receives". A subagent can be *better* on the candidate and
still fail ``shadow_agreement`` in ``config/gates.yaml``, because a RAG stage
whose output shape shifts under every third query is an integration risk
regardless of who is right.

So the unit here is the **item**, and an item agrees only when *every*
comparable field agrees. Field-level rates are reported alongside, because
"the two arms differ on 11% of items, and 9 of those 11 points are
``filters.cpc_codes``" is a fixable finding where a bare 0.89 is not.

Structured vs prose
-------------------

Structured fields are compared by exact match **after** the same normalisation
the deterministic metrics use — :func:`~amw.eval.metrics.normalize_scalar` for
free text, :func:`~amw.eval.metrics.normalize_code` for classification codes,
set semantics for lists. That is deliberate reuse, not convenience: if shadow
agreement folded ``"H01M 10/052"`` and ``"h01m10/052"`` differently from
``extraction_accuracy``, the two numbers in the same scorecard row would be
measuring subtly different things.

Prose fields cannot be exact-matched, and this build has **no embedding
backend**: there is no embedding adapter, no recorded embedding calls, and
adding one would mean live calls plus a new dependency. So the T11 card's
"embedding cosine for prose" is substituted by :func:`lexical_similarity` — a
dependency-free token Jaccard — and named for what it is. It is a **proxy**:

* it has no notion of synonymy, so a faithful paraphrase scores low;
* it has no stopword list, so two unrelated sentences share "the" and "of" and
  score above zero.

Both errors are disclosed rather than tuned away, because the honest fix is an
embedding backend, and until there is one the **authoritative** prose
comparison is the judge adjudication in :mod:`amw.shadow.triage`, which reads
recorded per-criterion verdicts on the actual text. The ordering is: lexical
similarity flags *where* the two arms diverge; the recorded judge says *which
one was right*. Never quote the proxy as a quality claim.

:data:`DEFAULT_PROSE_THRESHOLD` is the one arbitrary constant in this module.
It is not a gate (gates live in ``config/gates.yaml`` and are checked on CI
bounds by the scorecard); it is the cut-off at which the proxy calls two
strings "the same answer". It was fixed **before** the corpus was scored and is
recorded on every result, and :attr:`SubagentAgreement.structured_agreement`
reports the same population with prose excluded entirely, so a reader can see
exactly how much of the headline number rests on the proxy.

Swapping in a real embedding backend
------------------------------------

Every entry point takes ``prose_similarity: SimilarityFn``. An embedding
backend implements ``(str, str) -> float in [0, 1]`` — resolve an embedding
model through ``amw.adapters``, cache per string, return cosine — and passes it
here. Nothing else in this module changes, and the recorded
``similarity_metric`` name on the result changes with it so no artifact can
claim cosine while carrying Jaccard.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Literal, Mapping, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field

from amw.eval.metrics import (
    MetricSample,
    extract_payload,
    normalize_code,
    normalize_scalar,
)
from amw.eval.stats import (
    DEFAULT_BOOTSTRAP_SEED,
    Estimate,
    InsufficientDataError,
    bootstrap_ci,
)
from amw.traces.schema import Trace

__all__ = [
    "AGREEMENT_METRIC",
    "STRUCTURED_AGREEMENT_METRIC",
    "DEFAULT_PROSE_THRESHOLD",
    "LEXICAL_SIMILARITY_NAME",
    "FIELD_KINDS",
    "FieldKind",
    "SimilarityFn",
    "FieldComparison",
    "ItemAgreement",
    "FieldRate",
    "SubagentAgreement",
    "lexical_similarity",
    "tokenize",
    "field_values",
    "compare_field",
    "compare_item",
    "aggregate_agreement",
]

#: Metric name on the primary estimate. Matches the gate in config/gates.yaml
#: so the scorecard can look it up without a translation table.
AGREEMENT_METRIC = "shadow_agreement"

#: The same population with prose fields dropped. Reported as a sensitivity
#: check, never as the gate input — dropping prose silently would make a
#: prose-heavy subagent look far more interchangeable than it is.
STRUCTURED_AGREEMENT_METRIC = "shadow_agreement_structured"

#: Token-Jaccard score at or above which the proxy calls two prose strings the
#: same answer. Fixed before scoring; recorded on every result. Not a gate.
DEFAULT_PROSE_THRESHOLD = 0.60

#: Goes into the artifact next to every number this module produces, so nobody
#: can read a Jaccard as a cosine.
LEXICAL_SIMILARITY_NAME = "token_jaccard_lexical_proxy"

FieldKind = Literal["scalar", "code", "scalar_set", "code_set", "prose"]


class SimilarityFn(Protocol):
    """``(a, b) -> similarity in [0, 1]``. The embedding extension point."""

    def __call__(self, left: str, right: str) -> float:  # pragma: no cover - protocol
        ...


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# the lexical proxy
# --------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> frozenset[str]:
    """Case-folded alphanumeric word tokens, as a set.

    Deliberately trivial, for the same reason
    :func:`~amw.eval.metrics.normalize_scalar` is: a customer has to be able to
    recompute the score on paper. No stemming, no stopwords, no n-grams.
    """
    return frozenset(_TOKEN_RE.findall(text.casefold()))


def lexical_similarity(left: str, right: str) -> float:
    """Jaccard overlap of the two token sets — the offline stand-in for cosine.

    ``1.0`` for identical token sets (including two empty strings, which agree
    on having said nothing), ``0.0`` when one side is empty and the other is
    not. Symmetric and deterministic: no seed, no model, no network.

    Read the module docstring before using this number for anything other than
    "these two outputs are worth looking at side by side".
    """
    a, b = tokenize(left), tokenize(right)
    if not a and not b:
        return 1.0
    union = a | b
    if not union:  # pragma: no cover - unreachable given the guard above
        return 1.0
    return len(a & b) / len(union)


# --------------------------------------------------------------------------
# what each subagent's payload is compared on
# --------------------------------------------------------------------------

#: Comparable field -> how to compare it, per subagent.
#:
#: Dotted names dig into the payload (``filters.cpc_codes``); the two
#: ``key_points.*`` names are derived views, see :func:`field_values`. The kinds
#: mirror the deterministic metrics: ``code``/``code_set`` are the fields
#: ``amw.eval.metrics`` normalises with :func:`~amw.eval.metrics.normalize_code`
#: (CPC codes, jurisdictions), everything else scalar is free text.
#:
#: Not every schema field is here by accident of listing — every field is here.
#: A field left out would be a place where two arms could differ and the
#: agreement rate would not notice.
FIELD_KINDS: dict[str, dict[str, FieldKind]] = {
    "query_rewriter": {
        "query": "prose",
        "intent": "scalar",
        "filters.date_from": "scalar",
        "filters.date_to": "scalar",
        "filters.assignees": "scalar_set",
        "filters.jurisdictions": "code_set",
        "filters.cpc_codes": "code_set",
    },
    "chunk_summarizer": {
        "summary": "prose",
        # The claims themselves, joined. Prose: two summarisers phrase the same
        # claim differently far more often than they pick different claims.
        "key_points.text": "prose",
        # The citation set is the part a downstream pipeline actually consumes,
        # and it is exactly comparable. Kept separate from the prose so a
        # disagreement can be attributed to wording or to grounding.
        "key_points.chunk_ids": "scalar_set",
    },
    "feature_extractor": {
        "title": "scalar",
        "assignee": "scalar",
        "filing_date": "scalar",
        "jurisdiction": "code",
        "cpc_codes": "code_set",
        "independent_claim_count": "scalar",
        "technical_field": "prose",
        "novelty_statement": "prose",
    },
}


def _dig(payload: Mapping[str, Any], path: str) -> Any:
    """Follow a dotted path. A missing key is ``None`` — "did not answer this".

    Absent and ``null`` are the same claim in these schemas (see
    ``amw/agents/schemas.py``: nullable means "not stated in the source"), and
    :func:`~amw.eval.metrics.extraction_field_verdicts` already treats them
    identically. Diverging here would score a model differently for omitting a
    key than for setting it null.
    """
    node: Any = payload
    for part in path.split("."):
        if not isinstance(node, Mapping):
            return None
        node = node.get(part)
    return node


def field_values(subagent: str, payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """The comparable value of every field of ``subagent``, pre-normalisation.

    ``payload`` of ``None`` (errored call, prose where JSON was demanded) gives
    every field ``None``. That is not a shortcut: an arm that produced nothing
    genuinely asserted nothing, and :func:`compare_item` refuses to call two
    such arms "in agreement" — see its ``comparable`` flag.
    """
    if subagent not in FIELD_KINDS:
        raise KeyError(
            f"no agreement field spec for subagent {subagent!r}; known: "
            f"{sorted(FIELD_KINDS)}"
        )
    payload = payload or {}
    out: dict[str, Any] = {}
    for name in FIELD_KINDS[subagent]:
        if name == "key_points.text":
            out[name] = "\n".join(
                str(point.get("text") or "")
                for point in _key_points(payload)
            )
        elif name == "key_points.chunk_ids":
            out[name] = [
                str(chunk_id)
                for point in _key_points(payload)
                for chunk_id in (point.get("chunk_ids") or [])
            ]
        else:
            out[name] = _dig(payload, name)
    return out


def _key_points(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    points = payload.get("key_points")
    if not isinstance(points, list):
        return []
    return [point for point in points if isinstance(point, Mapping)]


# --------------------------------------------------------------------------
# comparing one field
# --------------------------------------------------------------------------


def _normalized(kind: FieldKind, value: Any) -> Any:
    norm = normalize_code if kind in ("code", "code_set") else normalize_scalar
    if kind in ("scalar_set", "code_set"):
        if value is None:
            return frozenset()
        if isinstance(value, (list, tuple, set, frozenset)):
            return frozenset(norm(v) for v in value if v is not None and v != "")
        # A scalar where the schema promised a list: keep it comparable rather
        # than crashing the run, and let the mismatch show up as a difference.
        return frozenset({norm(value)})
    return norm(value)


def _abbrev(value: Any, limit: int = 60) -> str:
    """A short, single-line rendering for the triage table."""
    if isinstance(value, frozenset):
        text = "{" + ", ".join(sorted(str(v) for v in value)) + "}"
    else:
        text = "∅" if value is None else str(value)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


class FieldComparison(_Strict):
    """One field, on one item, on both arms."""

    field: str
    kind: FieldKind
    agreed: bool
    #: Prose only: the proxy score that produced ``agreed``. None for exact
    #: comparisons, so a reader can never mistake "1.0" for "exactly equal".
    similarity: float | None = None
    baseline: str
    candidate: str


def compare_field(
    field: str,
    kind: FieldKind,
    baseline_value: Any,
    candidate_value: Any,
    *,
    prose_similarity: SimilarityFn = lexical_similarity,
    prose_threshold: float = DEFAULT_PROSE_THRESHOLD,
) -> FieldComparison:
    """Compare one field on the two arms.

    Structured kinds are exact match after normalisation. Prose is the proxy:
    two absent values agree (both arms declined to state it), one absent value
    never does, and otherwise the similarity meets ``prose_threshold`` or it
    does not.
    """
    if kind == "prose":
        left = baseline_value if isinstance(baseline_value, str) else None
        right = candidate_value if isinstance(candidate_value, str) else None
        left_blank = not (left or "").strip()
        right_blank = not (right or "").strip()
        if left_blank and right_blank:
            similarity = 1.0
        elif left_blank or right_blank:
            similarity = 0.0
        else:
            similarity = float(prose_similarity(left or "", right or ""))
        return FieldComparison(
            field=field,
            kind=kind,
            agreed=similarity >= prose_threshold,
            similarity=similarity,
            baseline=_abbrev(baseline_value),
            candidate=_abbrev(candidate_value),
        )

    left_norm = _normalized(kind, baseline_value)
    right_norm = _normalized(kind, candidate_value)
    return FieldComparison(
        field=field,
        kind=kind,
        agreed=left_norm == right_norm,
        baseline=_abbrev(left_norm if kind.endswith("set") else baseline_value),
        candidate=_abbrev(right_norm if kind.endswith("set") else candidate_value),
    )


# --------------------------------------------------------------------------
# comparing one item
# --------------------------------------------------------------------------

#: Reasons an item produced no agreement observation. Counted, never dropped.
NO_PAYLOAD_EITHER = "no structured output from either arm"


class ItemAgreement(_Strict):
    """One dataset item, seen through both arms."""

    item_id: str
    subagent: str
    #: False when neither arm produced anything to compare. Such an item is
    #: excluded from the rate with a reason rather than counted as agreement:
    #: two backends that both fell over are not interchangeable, they are both
    #: broken, and scoring that 1.0 would be the single most flattering bug
    #: this module could have.
    comparable: bool = True
    agreed: bool | None = None
    reason: str | None = None
    fields: list[FieldComparison] = Field(default_factory=list)
    #: Set when exactly one arm produced no structured payload. The item is a
    #: measured disagreement — the downstream pipeline really would get
    #: different things — and this names which side was empty.
    empty_arm: Literal["baseline", "candidate"] | None = None

    @property
    def disagreeing_fields(self) -> list[str]:
        return [f.field for f in self.fields if not f.agreed]

    @property
    def structured_agreed(self) -> bool | None:
        """Agreement over the exactly-comparable fields only."""
        if not self.comparable:
            return None
        structured = [f for f in self.fields if f.kind != "prose"]
        if not structured:
            return None
        if self.empty_arm is not None:
            return False
        return all(f.agreed for f in structured)


def compare_item(
    subagent: str,
    item_id: str,
    baseline: Trace | Mapping[str, Any] | None,
    candidate: Trace | Mapping[str, Any] | None,
    *,
    prose_similarity: SimilarityFn = lexical_similarity,
    prose_threshold: float = DEFAULT_PROSE_THRESHOLD,
) -> ItemAgreement:
    """Field-by-field agreement for one item.

    Accepts traces (the normal path) or bare payload mappings (fixtures).
    Payload extraction goes through
    :func:`~amw.eval.metrics.extract_payload`, so "the model replied in prose
    when a schema was demanded" is ``None`` here exactly as it is for
    ``json_schema_validity`` — one definition of "produced no structured
    output" across the whole eval stack.
    """
    baseline_payload = extract_payload(baseline)
    candidate_payload = extract_payload(candidate)

    if baseline_payload is None and candidate_payload is None:
        return ItemAgreement(
            item_id=item_id,
            subagent=subagent,
            comparable=False,
            reason=NO_PAYLOAD_EITHER,
        )

    empty_arm: Literal["baseline", "candidate"] | None = None
    if baseline_payload is None:
        empty_arm = "baseline"
    elif candidate_payload is None:
        empty_arm = "candidate"

    left = field_values(subagent, baseline_payload)
    right = field_values(subagent, candidate_payload)
    fields = [
        compare_field(
            name,
            kind,
            left[name],
            right[name],
            prose_similarity=prose_similarity,
            prose_threshold=prose_threshold,
        )
        for name, kind in FIELD_KINDS[subagent].items()
    ]
    return ItemAgreement(
        item_id=item_id,
        subagent=subagent,
        comparable=True,
        # One arm empty is a disagreement even if the proxy happens to call the
        # two empty prose fields equal: the arms did not produce the same thing.
        agreed=False if empty_arm else all(f.agreed for f in fields),
        fields=fields,
        empty_arm=empty_arm,
    )


# --------------------------------------------------------------------------
# aggregating to a subagent
# --------------------------------------------------------------------------


class FieldRate(_Strict):
    """Per-field agreement, point only.

    No CI: these are diagnostics for triage, and thirteen more bootstrap
    intervals in a scorecard invite someone to gate on one of them. The gate
    reads :attr:`SubagentAgreement.agreement`.
    """

    field: str
    kind: FieldKind
    n: int
    n_agreed: int
    rate: float
    #: Mean proxy similarity across compared items. Prose fields only.
    mean_similarity: float | None = None


class SubagentAgreement(_Strict):
    """Everything the scorecard and the triage table need for one subagent."""

    subagent: str
    baseline_arm: str
    candidate_arm: str
    n_items: int
    n_compared: int
    n_agreed: int
    #: reason -> count for items that produced no observation.
    excluded: dict[str, int] = Field(default_factory=dict)
    #: Items where exactly one arm produced no structured payload.
    n_one_arm_empty: int = 0

    #: **The gate input.** ``shadow_agreement`` in config/gates.yaml is checked
    #: on this estimate's CI lower bound by the scorecard (T12), not here.
    agreement: Estimate | None = None
    point: float | None = None
    #: Same items, prose fields dropped. Sensitivity check for the proxy.
    structured_agreement: Estimate | None = None
    structured_point: float | None = None

    field_rates: list[FieldRate] = Field(default_factory=list)
    #: What produced the prose numbers, and at what cut-off. Printed wherever
    #: the agreement rate is printed.
    similarity_metric: str = LEXICAL_SIMILARITY_NAME
    prose_threshold: float = DEFAULT_PROSE_THRESHOLD
    #: Set when the sample was too small for a bootstrap interval.
    no_interval_reason: str | None = None

    @property
    def disagreements(self) -> int:
        return self.n_compared - self.n_agreed


def _sample(
    metric: str, observations: Sequence[tuple[str, bool]], excluded: Mapping[str, int]
) -> MetricSample:
    return MetricSample(
        metric=metric,
        values=[1.0 if agreed else 0.0 for _, agreed in observations],
        item_ids=[item_id for item_id, _ in observations],
        excluded=dict(excluded),
    )


def _estimate(sample: MetricSample, *, seed: int) -> tuple[Estimate | None, str | None]:
    """A bootstrap interval, or an explicit reason there is none.

    Mirrors ``MetricReport.of``: one observation has a mean but no interval,
    and resampling it 10,000 times would print a zero-width CI that reads as a
    precise measurement.
    """
    if sample.n < 2:
        return None, (
            f"{sample.n} comparable item(s); a bootstrap CI needs at least 2 "
            "(amw.eval.stats.MIN_OBSERVATIONS)"
        )
    try:
        return bootstrap_ci(sample, metric=sample.metric, seed=seed), None
    except InsufficientDataError as exc:  # pragma: no cover - guarded above
        return None, str(exc)


def aggregate_agreement(
    items: Iterable[ItemAgreement],
    *,
    subagent: str,
    baseline_arm: str,
    candidate_arm: str,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
    prose_threshold: float = DEFAULT_PROSE_THRESHOLD,
    similarity_metric: str = LEXICAL_SIMILARITY_NAME,
) -> SubagentAgreement:
    """Fold per-item agreement into the estimate the scorecard consumes.

    The bootstrap is the same one every other rate in this workbench uses
    (:func:`amw.eval.stats.bootstrap_ci`, 10,000 percentile resamples,
    ``DEFAULT_BOOTSTRAP_SEED`` unless told otherwise), so the interval on this
    row is comparable with the intervals on the others.
    """
    items = list(items)
    observations: list[tuple[str, bool]] = []
    structured_observations: list[tuple[str, bool]] = []
    excluded: dict[str, int] = {}

    for item in items:
        if not item.comparable:
            reason = item.reason or "not comparable"
            excluded[reason] = excluded.get(reason, 0) + 1
            continue
        observations.append((item.item_id, bool(item.agreed)))
        structured = item.structured_agreed
        if structured is not None:
            structured_observations.append((item.item_id, structured))

    sample = _sample(AGREEMENT_METRIC, observations, excluded)
    estimate, no_interval = _estimate(sample, seed=seed)
    structured_sample = _sample(
        STRUCTURED_AGREEMENT_METRIC, structured_observations, excluded
    )
    structured_estimate, _ = _estimate(structured_sample, seed=seed)

    return SubagentAgreement(
        subagent=subagent,
        baseline_arm=baseline_arm,
        candidate_arm=candidate_arm,
        n_items=len(items),
        n_compared=sample.n,
        n_agreed=sum(1 for _, agreed in observations if agreed),
        excluded=excluded,
        n_one_arm_empty=sum(1 for item in items if item.empty_arm is not None),
        agreement=estimate,
        point=(sum(sample.values) / sample.n) if sample.n else None,
        structured_agreement=structured_estimate,
        structured_point=(
            sum(structured_sample.values) / structured_sample.n
            if structured_sample.n
            else None
        ),
        field_rates=_field_rates(items, subagent),
        similarity_metric=similarity_metric,
        prose_threshold=prose_threshold,
        no_interval_reason=no_interval,
    )


def _field_rates(items: Sequence[ItemAgreement], subagent: str) -> list[FieldRate]:
    kinds = FIELD_KINDS[subagent]
    rates: list[FieldRate] = []
    for field, kind in kinds.items():
        comparisons = [
            comparison
            for item in items
            for comparison in item.fields
            if comparison.field == field
        ]
        if not comparisons:
            continue
        similarities = [
            c.similarity for c in comparisons if c.similarity is not None
        ]
        rates.append(
            FieldRate(
                field=field,
                kind=kind,
                n=len(comparisons),
                n_agreed=sum(1 for c in comparisons if c.agreed),
                rate=sum(1 for c in comparisons if c.agreed) / len(comparisons),
                mean_similarity=(
                    sum(similarities) / len(similarities) if similarities else None
                ),
            )
        )
    return rates
