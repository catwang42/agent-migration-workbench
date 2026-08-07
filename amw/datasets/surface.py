"""The optional Gemini realism pass over template prose.

TASKS.md T06 asks that generation go through the Gemini adapter so it is
recorded and replayable like everything else. This is where that happens — and
the scope of what the model is allowed to do is deliberately narrow.

**The model rewrites surface prose. It never authors gold, rubrics, filters,
dates, classifications or claim structure.** Templates derive the gold answer
from the same scenario facts they build the prose from; the rewrite only makes
the prose sound like a person wrote it. That split is what keeps two properties
true at once: the dataset reads as real patent traffic, and the answer key is
still provably consistent with the input.

Every rewrite is checked before it is accepted
(:func:`~amw.datasets.templates.common.check_surface`):

* every literal the gold depends on must survive verbatim, and
* nothing the item deliberately omits may appear.

The second check is the important one. Half the edge cases exist because a
field is missing — "in the last couple of years" with no anchor, a front page
with no filing date — and a helpful rewriter that supplies the missing value
destroys the item without any error being raised. A rejected rewrite is not a
failure: the item falls back to its template prose and says so in
``surface_source``.

Zero credentials is preserved: in replay mode a miss is a fallback, not a
crash, so ``cli.py gen --mode replay`` produces a complete, valid dataset on a
laptop with no ADC. What it does not produce is the naturalised phrasing, and
the run report says how many items that affected rather than hiding it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from amw.adapters.base import ModelAdapter, ModelRequest
from amw.datasets.templates.common import Draft, SurfaceTarget, check_surface
from amw.traces.store import ReplayMissError

__all__ = ["SurfaceRewriter", "RewriteStats", "SYSTEM_PROMPT", "DATAGEN_PREFIX"]

#: Generation calls are recorded under their own subagent name so they land in
#: ``artifacts/replay/datagen_<subagent>.jsonl`` and never contaminate the eval
#: corpus that the scorecard reads.
DATAGEN_PREFIX = "datagen_"

SYSTEM_PROMPT = """\
You rewrite one short passage so that it reads like authentic patent-domain \
text instead of a filled-in template.

Rules, in order of priority:
1. Change wording and sentence structure only. Do not add, remove or alter any \
fact: no new numbers, dates, names, organisations, classification codes, \
publication numbers, claim numbers or measured values, and none removed.
2. If the passage does not state something, your rewrite must not state it \
either. Do not resolve vagueness, do not supply a missing date, and do not \
complete a partial reference. Vagueness in the input is intentional.
3. Match the register described in the STYLE line exactly.
4. Return only the rewritten passage. No preamble, no quotation marks, no \
explanation, no markdown.
"""


@dataclass
class RewriteStats:
    """What the realism pass actually did, for the run report.

    Counts only. Nothing here is a measurement of model quality and none of it
    reaches a customer-facing metric.
    """

    attempted: int = 0
    accepted: int = 0
    rejected: int = 0
    missed: int = 0
    errored: int = 0
    reasons: list[str] = field(default_factory=list)

    def note(self, reason: str) -> None:
        # Keep the log bounded; the first few are enough to diagnose a
        # systematic problem, and an unbounded list would bloat the report.
        if len(self.reasons) < 20:
            self.reasons.append(reason)

    def summary(self) -> str:
        return (
            f"{self.accepted}/{self.attempted} passages naturalised "
            f"(rejected {self.rejected}, replay-miss {self.missed}, "
            f"errors {self.errored})"
        )


class SurfaceRewriter:
    """Runs template prose through a model adapter, guarded.

    :param adapter: any :class:`~amw.adapters.base.ModelAdapter`. Obtain it from
        ``amw.adapters.resolve`` so mode resolution and record-on-live stay in
        the one place that owns them.
    :param model: logical key from ``config/models.yaml``.
    :param on_miss: ``"fallback"`` (default) keeps the template prose when
        nothing was recorded; ``"raise"`` is for a live re-record run where a
        miss means the corpus is stale and you want to know.
    """

    def __init__(
        self,
        adapter: ModelAdapter,
        *,
        model: str,
        on_miss: Literal["fallback", "raise"] = "fallback",
    ) -> None:
        self.adapter = adapter
        self.model = model
        self.on_miss = on_miss
        self.stats = RewriteStats()

    # -- request ---------------------------------------------------------

    def _request(self, subagent: str, target: SurfaceTarget, text: str) -> ModelRequest:
        return ModelRequest(
            subagent=f"{DATAGEN_PREFIX}{subagent}",
            model=self.model,
            system_prompt=SYSTEM_PROMPT,
            messages=[f"STYLE: {target.style}", f"PASSAGE:\n{text}"],
            temperature=0.0,
            provenance="synthetic",
        )

    def _rewrite_one(self, subagent: str, target: SurfaceTarget, text: str) -> str:
        self.stats.attempted += 1
        request = self._request(subagent, target, text)
        try:
            trace = self.adapter.complete(request)
        except ReplayMissError as exc:
            if self.on_miss == "raise":
                raise
            self.stats.missed += 1
            self.stats.note(f"replay miss: {exc}")
            return text
        except Exception as exc:  # noqa: BLE001 - a bad rewrite must not kill gen
            self.stats.errored += 1
            self.stats.note(f"{type(exc).__name__}: {exc}")
            return text

        if trace.status != "ok" or not (trace.output.text or "").strip():
            self.stats.errored += 1
            self.stats.note(f"empty or errored trace: {trace.error or 'no text'}")
            return text

        candidate = (trace.output.text or "").strip()
        problem = check_surface(candidate, target, text)
        if problem is not None:
            # The guard did its job. Keep the template prose: a slightly stiffer
            # sentence is a far cheaper defect than a corrupted answer key.
            self.stats.rejected += 1
            self.stats.note(f"rejected rewrite: {problem}")
            return text

        self.stats.accepted += 1
        return candidate

    # -- the pass --------------------------------------------------------

    def apply(self, subagent: str, draft: Draft) -> bool:
        """Rewrite ``draft`` in place. Returns True if anything was accepted."""
        changed = False
        for target in draft.surface:
            if target.kind == "message":
                if not 0 <= target.index < len(draft.messages):
                    raise IndexError(
                        f"{draft.template_id}: surface target message "
                        f"{target.index} is out of range"
                    )
                original = draft.messages[target.index]
                rewritten = self._rewrite_one(subagent, target, original)
                if rewritten != original:
                    draft.messages[target.index] = rewritten
                    changed = True
            elif target.kind == "chunk":
                if not 0 <= target.index < len(draft.chunks):
                    raise IndexError(
                        f"{draft.template_id}: surface target chunk "
                        f"{target.index} is out of range"
                    )
                chunk = draft.chunks[target.index]
                rewritten = self._rewrite_one(subagent, target, chunk.text)
                if rewritten != chunk.text:
                    draft.chunks[target.index] = chunk.model_copy(
                        update={"text": rewritten}
                    )
                    changed = True
            else:  # pragma: no cover - SurfaceTarget.kind is a Literal
                raise ValueError(f"unknown surface target kind {target.kind!r}")
        return changed
