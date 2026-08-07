"""Rubric-anchored LLM judge — the metric that needs a reason attached.

The deterministic metrics in :mod:`amw.eval.metrics` answer "did it parse, did
it cite, did it extract". They cannot answer "is this rewritten query the query
a patent attorney meant". That is what this module is for, and it is built
around three constraints from CLAUDE.md and the T08 card.

**Rubric-anchored, not vibes.** The judge never scores on a free-form scale. It
receives the per-item rubric (3–5 pass/fail criteria, produced by T06) and
returns one verdict per criterion. The item's score is the fraction of criteria
passed, so a "0.6" is always decomposable into *which three of five criteria*
held.

**Rationale is captured.** Every criterion verdict carries a one-sentence
rationale naming the concrete defect, because T09 groups failed items by rubric
criterion into failure clusters. A score without a reason cannot be triaged,
and an untriageable failure cannot be fixed on Monday.

**Prompts are files.** ``amw/eval/judge_prompts/<version>/`` — see the README
there. They get shown to customers; a prompt inside a Python string literal is
a prompt nobody can audit. The pack's sha travels on every verdict.

Two further rules bite hard here:

* **No fabricated results.** A judge call that fails — API error, replay miss,
  unparseable response, a verdict naming a criterion that is not in the rubric
  — produces ``status="error"`` and ``score=None``. It never produces 0.0. A
  judge failure is *our* infrastructure failing; scoring it zero would blame
  the model under test for our outage and drag its mean down.
* **Zero credentials in replay.** The judge takes an adapter from
  ``amw.adapters.resolve``, so ``mode="replay"`` reads recorded judge calls out
  of the trace corpus and never touches the network.

The judge model is resolved from ``config/models.yaml`` role ``judge``
(currently ``gemini-pro``). No model ID appears in this file.
"""

from __future__ import annotations

import json
from pathlib import Path
from string import Template
from typing import Any, Iterable, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, model_validator

from amw.adapters import ModelAdapter, ModelRequest, resolve
from amw.config import ConfigError, ModelsConfig, load_all
from amw.traces.schema import Trace
from amw.traces.store import ReplayMissError, ReplayStore

__all__ = [
    "JUDGE_ROLE",
    "DEFAULT_PROMPT_VERSION",
    "JUDGE_SUBAGENT_PREFIX",
    "USER_TEMPLATE_FIELDS",
    "JUDGE_RESPONSE_SCHEMA",
    "RubricCriterion",
    "Rubric",
    "CriterionVerdict",
    "JudgeVerdict",
    "JudgeRequest",
    "PromptPack",
    "load_prompt_pack",
    "prompt_pack_dir",
    "Judge",
    "judge_subagent_name",
    "verdicts_to_repeat_scores",
    "cluster_failures",
    # Re-exported so a runner can catch a miss without importing the store.
    "ReplayMissError",
]

#: Logical role in config/models.yaml. Resolves to a Gemini Pro-class model.
JUDGE_ROLE = "judge"

#: Directory under judge_prompts/ used unless a caller pins another version.
DEFAULT_PROMPT_VERSION = "v1"

#: Judge traces are recorded under their own subagent namespace so they never
#: collide with the subagent's own calls in the replay store.
JUDGE_SUBAGENT_PREFIX = "judge"

#: Placeholders `user.txt` must consume. Asserted by tests/test_judge.py.
USER_TEMPLATE_FIELDS: tuple[str, ...] = (
    "subagent",
    "item_id",
    "task_input",
    "context_chunks",
    "reference",
    "candidate",
    "rubric",
)

_PROMPTS_ROOT = Path(__file__).resolve().parent / "judge_prompts"

#: Rubrics with fewer than 3 or more than 5 criteria are a generator bug
#: (master_plan §5.3: "per-item adaptive rubric (3-5 pass/fail criteria)").
MIN_CRITERIA = 3
MAX_CRITERIA = 5


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# the rubric contract (consumed here, produced by T06)
# --------------------------------------------------------------------------


class RubricCriterion(_Base):
    """One pass/fail criterion.

    ``id`` is load-bearing: it is the key failures are clustered on, so it must
    be stable across items that test the same thing. ``c1``…``c5`` per item is
    fine; a ``tag`` shared across items ("filter_dates", "grounding") is what
    makes a cross-item cluster meaningful, so it is offered and optional.
    """

    id: str
    text: str
    #: Optional cross-item cluster label. T09 groups on this when present and
    #: falls back to `id`.
    tag: str | None = None


class Rubric(_Base):
    """The 3–5 criteria for one dataset item.

    Produced by T06's generator alongside the gold output; consumed here as the
    judge's entire scoring frame. Nothing outside these criteria is scored.
    """

    item_id: str
    subagent: str
    criteria: list[RubricCriterion]

    @model_validator(mode="after")
    def _shape(self) -> "Rubric":
        if not MIN_CRITERIA <= len(self.criteria) <= MAX_CRITERIA:
            raise ValueError(
                f"rubric for item {self.item_id!r} has {len(self.criteria)} "
                f"criteria; expected {MIN_CRITERIA}-{MAX_CRITERIA}"
            )
        ids = [c.id for c in self.criteria]
        if len(set(ids)) != len(ids):
            raise ValueError(
                f"rubric for item {self.item_id!r} has duplicate criterion ids: {ids}"
            )
        return self

    @property
    def ids(self) -> list[str]:
        return [c.id for c in self.criteria]

    def render(self) -> str:
        """The criteria block dropped into the user prompt."""
        return "\n".join(f"[{c.id}] {c.text}" for c in self.criteria)


# --------------------------------------------------------------------------
# the judge's answer
# --------------------------------------------------------------------------


class CriterionVerdict(_Base):
    criterion_id: str
    passed: bool
    rationale: str


class JudgeVerdict(_Base):
    """One judge call's result, or an explicit record that it failed.

    ``score`` is present **iff** ``status == "ok"``. There is no path to a
    zero-by-default: a failed judge call carries ``score=None`` and an
    ``error`` string, and :func:`~amw.eval.stats.aggregate_repeats` excludes it
    from the mean while counting it in ``failed_repeats``.
    """

    item_id: str
    subagent: str
    repeat: int = 1
    status: Literal["ok", "error"] = "ok"
    #: Fraction of rubric criteria passed. None when status == "error".
    score: float | None = None
    criteria: list[CriterionVerdict] = []
    overall_rationale: str | None = None
    error: str | None = None
    #: Provenance of the score, for the report footer.
    judge_model: str | None = None
    prompt_version: str | None = None
    prompt_sha: str | None = None
    trace_id: str | None = None
    trace_ts: str | None = None

    @model_validator(mode="after")
    def _score_iff_ok(self) -> "JudgeVerdict":
        if self.status == "ok":
            if self.score is None:
                raise ValueError(
                    f"judge verdict for {self.item_id!r} is ok with no score"
                )
            if not self.criteria:
                raise ValueError(
                    f"judge verdict for {self.item_id!r} is ok with no criterion "
                    "verdicts; a score with no reasons cannot be triaged"
                )
        else:
            if self.score is not None:
                raise ValueError(
                    f"judge verdict for {self.item_id!r} failed but carries "
                    f"score={self.score!r}; a failed judge call must not produce "
                    "a number (ground rule 1)"
                )
            if not self.error:
                raise ValueError("a failed judge verdict must say why")
        return self

    @property
    def failed_criteria(self) -> list[str]:
        return [c.criterion_id for c in self.criteria if not c.passed]


class JudgeRequest(_Base):
    """Everything the judge needs to score one item once."""

    item_id: str
    subagent: str
    rubric: Rubric
    #: The candidate output under evaluation: the subagent's JSON payload, or
    #: its text when it produced no JSON.
    candidate: Any
    #: What the subagent was asked. Rendered verbatim.
    task_input: str | Sequence[str] = ""
    context_chunks: Sequence[str] = ()
    #: Gold reference from the dataset. Optional: an item may not have one.
    reference: Any = None
    repeat: int = 1
    repeats: int = 1
    #: Which arm produced the candidate ("claude_baseline", "gemini_naive"...).
    #: Carried for provenance only; it is NOT shown to the judge, so the judge
    #: cannot know which backend it is grading (master_plan §7, judge neutrality).
    arm: str | None = None

    @model_validator(mode="after")
    def _rubric_matches(self) -> "JudgeRequest":
        if self.rubric.item_id != self.item_id:
            raise ValueError(
                f"rubric is for item {self.rubric.item_id!r} but the request is "
                f"for {self.item_id!r}"
            )
        if self.repeat < 1 or self.repeat > max(self.repeats, 1):
            raise ValueError(
                f"repeat {self.repeat} out of range 1..{max(self.repeats, 1)}"
            )
        return self


# --------------------------------------------------------------------------
# prompt pack
# --------------------------------------------------------------------------


class PromptPack(_Base):
    """The versioned prompt text, plus the hash that pins it to a run."""

    version: str
    system: str
    user_template: str
    repeat_note_template: str
    #: sha256 over the pack's files, truncated. Printed with every score.
    sha: str

    def render_user(self, **fields: str) -> str:
        """Fill ``user.txt``. Raises on a missing or unknown placeholder."""
        return Template(self.user_template).substitute(**fields)

    def render_repeat_note(self, repeat: int, k: int) -> str:
        return Template(self.repeat_note_template).substitute(repeat=repeat, k=k)


def prompt_pack_dir(version: str = DEFAULT_PROMPT_VERSION) -> Path:
    return _PROMPTS_ROOT / version


def load_prompt_pack(version: str = DEFAULT_PROMPT_VERSION) -> PromptPack:
    """Read a judge prompt version off disk.

    :raises ~amw.config.ConfigError: the version directory or one of its files
        is missing. Loudly, because a judge with half a prompt would still
        return numbers.
    """
    import hashlib

    root = prompt_pack_dir(version)
    if not root.is_dir():
        available = sorted(p.name for p in _PROMPTS_ROOT.iterdir() if p.is_dir())
        raise ConfigError(
            f"no judge prompt pack {version!r} under {_PROMPTS_ROOT}; "
            f"available: {available}"
        )
    parts: dict[str, str] = {}
    for name in ("system", "user", "repeat_note"):
        path = root / f"{name}.txt"
        if not path.is_file():
            raise ConfigError(f"judge prompt pack {version!r} is missing {path.name}")
        parts[name] = path.read_text(encoding="utf-8")

    digest = hashlib.sha256()
    for name in sorted(parts):
        digest.update(name.encode("utf-8"))
        digest.update(parts[name].encode("utf-8"))

    missing = [
        field
        for field in USER_TEMPLATE_FIELDS
        if f"${field}" not in parts["user"] and f"${{{field}}}" not in parts["user"]
    ]
    if missing:
        raise ConfigError(
            f"judge prompt pack {version!r}: user.txt does not use {missing}. "
            "A placeholder that is never substituted means the judge scores "
            "without seeing that material."
        )

    return PromptPack(
        version=version,
        system=parts["system"],
        user_template=parts["user"],
        repeat_note_template=parts["repeat_note"],
        sha=digest.hexdigest()[:12],
    )


# --------------------------------------------------------------------------
# the response schema handed to the model
# --------------------------------------------------------------------------

#: OpenAPI-3-subset schema for the judge's reply. Written out flat rather than
#: generated from the pydantic model because Gemini's ``response_schema`` does
#: not resolve ``$ref``/``$defs`` (see amw/agents/schemas.py for the same
#: problem solved by inlining). ``tests/test_judge.py`` asserts this stays in
#: step with :class:`CriterionVerdict`, so the duplication cannot rot.
JUDGE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "criteria": {
            "type": "array",
            "description": "One entry per rubric criterion, in the order given.",
            "items": {
                "type": "object",
                "properties": {
                    "criterion_id": {
                        "type": "string",
                        "description": "The bracketed id from the rubric, e.g. c1.",
                    },
                    "passed": {"type": "boolean"},
                    "rationale": {
                        "type": "string",
                        "description": "One sentence naming the concrete evidence.",
                    },
                },
                "required": ["criterion_id", "passed", "rationale"],
            },
        },
        "overall_rationale": {
            "type": "string",
            "description": "One or two sentences summarising the decision.",
        },
    },
    "required": ["criteria", "overall_rationale"],
}


def judge_subagent_name(subagent: str) -> str:
    """Replay-store namespace for judge calls about ``subagent``.

    Judge traces live in ``artifacts/replay/judge_<subagent>.jsonl``, separate
    from the subagent's own recordings. Same corpus, no key collisions, and a
    reader can see at a glance how many judge calls a run cost.
    """
    return f"{JUDGE_SUBAGENT_PREFIX}_{subagent}"


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def _render_payload(value: Any, *, absent: str) -> str:
    """Serialise a candidate/reference for the prompt.

    An absent value renders as an explicit marker, never as an empty string:
    the judge must be able to tell "the model produced nothing" from "the
    section was accidentally left blank".
    """
    if value is None:
        return absent
    if isinstance(value, str):
        return value if value.strip() else absent
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)


def _render_chunks(chunks: Sequence[str]) -> str:
    if not chunks:
        return "(no context chunks were supplied for this item)"
    return "\n\n".join(chunks)


def _render_input(task_input: str | Sequence[str]) -> str:
    if isinstance(task_input, str):
        return task_input if task_input.strip() else "(no task input recorded)"
    joined = "\n".join(task_input)
    return joined if joined.strip() else "(no task input recorded)"


# --------------------------------------------------------------------------
# the judge
# --------------------------------------------------------------------------


class Judge:
    """Scores items against their rubrics, through the normal adapter path.

    :param mode: ``live | replay | hybrid``, passed straight to
        :func:`amw.adapters.resolve`. ``replay`` needs no credentials.
    :param models: model registry; defaults to ``load_all().models``. The judge
        model comes from role ``judge`` — never a literal ID.
    :param model_key: override the role lookup (the P1 dual-judge cross-check
        passes ``judge_crosscheck``'s model here).
    :param temperature: forwarded to the adapter. Left at ``None`` (which the
        Gemini adapter reads as 0.0) so replayed and live scores match by
        default; a runner that wants genuine sampling spread across repeats
        should pass a non-zero value and say so in the report.
    :param adapter: inject a ready-made adapter (tests, and the dual-judge
        lane). When given, ``mode``/``store`` are not consulted.
    """

    def __init__(
        self,
        *,
        mode: str = "replay",
        models: ModelsConfig | None = None,
        model_key: str | None = None,
        role: str = JUDGE_ROLE,
        prompt_version: str = DEFAULT_PROMPT_VERSION,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        store: ReplayStore | None = None,
        adapter: ModelAdapter | None = None,
    ) -> None:
        self.models = models if models is not None else load_all().models
        if model_key is None:
            model_key, _spec = self.models.for_role(role)
        else:
            self.models.spec(model_key)  # ConfigError on an unknown key
        self.model_key = model_key
        self.mode = mode
        self.prompt_version = prompt_version
        self.prompts = load_prompt_pack(prompt_version)
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self._store = store
        self._adapter = adapter

    # -- wiring ----------------------------------------------------------

    @property
    def adapter(self) -> ModelAdapter:
        if self._adapter is None:
            self._adapter = resolve(
                self.model_key, self.mode, models=self.models, store=self._store
            )
        return self._adapter

    def describe(self) -> dict[str, str]:
        """Judge provenance for the scorecard footer."""
        spec = self.models.spec(self.model_key)
        return {
            "judge_model_key": self.model_key,
            "judge_model": spec.display_name,
            "judge_mode": self.adapter.mode,
            "judge_prompt_version": self.prompts.version,
            "judge_prompt_sha": self.prompts.sha,
        }

    # -- request building -------------------------------------------------

    def build_request(self, request: JudgeRequest) -> ModelRequest:
        """The :class:`~amw.adapters.base.ModelRequest` for one judge call.

        Exposed (rather than private) so a runner can compute a replay key
        ahead of time, and so tests can assert on the exact rendered prompt.
        """
        user = self.prompts.render_user(
            subagent=request.subagent,
            item_id=request.item_id,
            task_input=_render_input(request.task_input),
            context_chunks=_render_chunks(list(request.context_chunks)),
            reference=_render_payload(
                request.reference, absent="(no reference output for this item)"
            ),
            candidate=_render_payload(
                request.candidate,
                absent="(the subagent produced no output for this item)",
            ),
            rubric=request.rubric.render(),
        )
        messages = [user]
        if request.repeats > 1:
            # See judge_prompts/README.md: without this the two repeats share
            # an input_sha and the replay store serves one recording twice.
            messages.append(
                self.prompts.render_repeat_note(request.repeat, request.repeats)
            )
        return ModelRequest(
            subagent=judge_subagent_name(request.subagent),
            model=self.model_key,
            system_prompt=self.prompts.system,
            messages=messages,
            response_schema=JUDGE_RESPONSE_SCHEMA,
            temperature=self.temperature,
            max_output_tokens=self.max_output_tokens,
            item_id=f"{request.item_id}-r{request.repeat}",
        )

    # -- scoring ----------------------------------------------------------

    def score(self, request: JudgeRequest) -> JudgeVerdict:
        """Score one item once.

        Returns an ``error`` verdict — never a zero — for a failed model call,
        an unparseable response, or a response that does not match the rubric.

        :raises ~amw.traces.store.ReplayMissError: in replay mode with nothing
            recorded for this call. Deliberately *not* swallowed here: a bare
            ``score()`` should tell you the corpus is missing rather than hand
            back a plausible error verdict. :meth:`score_many` converts it, so
            a batch survives.
        """
        model_request = self.build_request(request)
        trace = self.adapter.complete(model_request)
        return self.verdict_from_trace(request, trace)

    def score_many(self, requests: Iterable[JudgeRequest]) -> list[JudgeVerdict]:
        """Score a batch. One bad call does not kill the run.

        Adapter errors are already error traces (the adapters retry ×2 and then
        record ``status:"error"``); a replay miss is converted here, so an
        incomplete corpus costs you the items it is missing and not the batch.
        """
        out: list[JudgeVerdict] = []
        for request in requests:
            try:
                out.append(self.score(request))
            except ReplayMissError as exc:
                out.append(self._error(request, f"replay miss: {exc}"))
            except ConfigError:
                # Bad model key / unreadable config: the run is misconfigured,
                # not flaky. Do not paper over it with N error verdicts.
                raise
        return out

    # -- trace -> verdict --------------------------------------------------

    def verdict_from_trace(self, request: JudgeRequest, trace: Trace) -> JudgeVerdict:
        """Parse one recorded judge call into a verdict.

        Separate from :meth:`score` so the same parsing is used on live,
        replayed, and fixture traces — and so a corpus can be re-parsed without
        re-calling anything.
        """
        provenance = {
            "judge_model": self.model_key,
            "prompt_version": self.prompts.version,
            "prompt_sha": self.prompts.sha,
            "trace_id": trace.trace_id,
            "trace_ts": trace.ts.isoformat(),
        }

        if trace.status == "error":
            return self._error(
                request, f"judge call failed: {trace.error}", **provenance
            )

        payload = trace.output.json_
        if payload is None and trace.output.text:
            try:
                payload = json.loads(trace.output.text)
            except ValueError:
                payload = None
        if not isinstance(payload, Mapping):
            return self._error(
                request,
                "judge returned no JSON object "
                f"(text={(trace.output.text or '')[:120]!r})",
                **provenance,
            )

        raw_criteria = payload.get("criteria")
        if not isinstance(raw_criteria, list) or not raw_criteria:
            return self._error(
                request, "judge response carried no criteria array", **provenance
            )

        verdicts: list[CriterionVerdict] = []
        try:
            for entry in raw_criteria:
                verdicts.append(CriterionVerdict.model_validate(entry))
        except Exception as exc:  # noqa: BLE001 - malformed judge output
            return self._error(
                request, f"unparseable criterion verdict: {exc}", **provenance
            )

        expected = request.rubric.ids
        got = [v.criterion_id for v in verdicts]
        if sorted(got) != sorted(expected):
            # Scoring a partial rubric would silently change the denominator
            # between items, so the item is an error rather than a smaller
            # fraction.
            return self._error(
                request,
                f"judge scored criteria {got}, rubric asks for {expected}",
                **provenance,
            )
        if len(set(got)) != len(got):
            return self._error(
                request, f"judge returned duplicate criterion ids: {got}", **provenance
            )

        blank = [v.criterion_id for v in verdicts if not v.rationale.strip()]
        if blank:
            return self._error(
                request,
                f"criteria {blank} were scored with no rationale; a score with "
                "no reason cannot be triaged",
                **provenance,
            )

        by_id = {v.criterion_id: v for v in verdicts}
        ordered = [by_id[cid] for cid in expected]
        score = sum(1 for v in ordered if v.passed) / len(ordered)
        overall = payload.get("overall_rationale")

        return JudgeVerdict(
            item_id=request.item_id,
            subagent=request.subagent,
            repeat=request.repeat,
            status="ok",
            score=score,
            criteria=ordered,
            overall_rationale=str(overall) if overall else None,
            **provenance,
        )

    def _error(self, request: JudgeRequest, message: str, **provenance: Any) -> JudgeVerdict:
        base = {
            "judge_model": self.model_key,
            "prompt_version": self.prompts.version,
            "prompt_sha": self.prompts.sha,
        }
        base.update(provenance)
        return JudgeVerdict(
            item_id=request.item_id,
            subagent=request.subagent,
            repeat=request.repeat,
            status="error",
            error=message,
            **base,
        )


# --------------------------------------------------------------------------
# hand-off helpers
# --------------------------------------------------------------------------


def verdicts_to_repeat_scores(
    verdicts: Iterable[JudgeVerdict],
) -> dict[str, list[float | None]]:
    """``item_id -> [score per repeat]``, ready for
    :func:`amw.eval.stats.aggregate_repeats`.

    Failed repeats become ``None``, not 0.0, which is what keeps a judge outage
    out of the model's score. Repeats are ordered by their ``repeat`` index.
    """
    by_item: dict[str, dict[int, float | None]] = {}
    for verdict in verdicts:
        by_item.setdefault(verdict.item_id, {})[verdict.repeat] = verdict.score
    return {
        item_id: [repeats[k] for k in sorted(repeats)]
        for item_id, repeats in by_item.items()
    }


def cluster_failures(
    verdicts: Iterable[JudgeVerdict],
    rubrics: Mapping[str, Rubric] | None = None,
) -> dict[str, list[str]]:
    """``criterion tag/id -> item_ids that failed it``.

    The seed for ``notes/day1_failures.md`` and for T09's failure clusters:
    "eleven items failed *the same* criterion" is an actionable prompt fix,
    where "quality is 0.71" is not. Clusters on a criterion's ``tag`` when the
    rubric supplies one, else on its id. Error verdicts contribute nothing —
    they are not failures of the model.
    """
    tag_for: dict[tuple[str, str], str] = {}
    for item_id, rubric in (rubrics or {}).items():
        for criterion in rubric.criteria:
            tag_for[(item_id, criterion.id)] = criterion.tag or criterion.id

    clusters: dict[str, list[str]] = {}
    for verdict in verdicts:
        if verdict.status != "ok":
            continue
        for criterion_id in verdict.failed_criteria:
            key = tag_for.get((verdict.item_id, criterion_id), criterion_id)
            bucket = clusters.setdefault(key, [])
            if verdict.item_id not in bucket:
                bucket.append(verdict.item_id)
    return dict(sorted(clusters.items()))
