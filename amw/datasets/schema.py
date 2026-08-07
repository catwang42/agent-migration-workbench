"""Dataset item schema — one evaluated case, with its gold answer and rubric.

A dataset item is the unit the whole workbench turns on. It carries, in one
record:

* the **input** in the same shape the adapters take (``messages`` +
  ``context_chunks``), so a runner never has to reformat anything;
* the **gold reference output**, which is an instance of the frozen contract in
  ``amw/agents/schemas.py`` — not a free-text answer. Deterministic metrics
  (filter precision/recall, citation coverage, exact-key match) compare against
  it field by field, which is only possible if it has the same shape as what the
  model is asked to emit;
* a **rubric** of 3-5 pass/fail criteria, which is what the judge scores. Item
  specific on purpose: "did it leave ``filing_date`` null?" is only a meaningful
  question for the item whose source text omits the filing date;
* **provenance and the generator seed** on every single item (CLAUDE.md ground
  rule 2), so a customer looking at any row of any report can ask where it came
  from and get an answer.

Two things deliberately absent:

* **No timestamp.** Same seed + same n must give byte-identical files, and a
  generation timestamp would break that for no benefit — the run date belongs to
  the report footer, which gets it from the run, not from the corpus.
* **No metric values, scores, or latencies.** Those come from executed calls
  only (ground rule 1). This file describes a question and its answer key.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Iterator, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from amw.agents.schemas import SUBAGENTS, schema_model

__all__ = [
    "Difficulty",
    "DIFFICULTIES",
    "Chunk",
    "DatasetInput",
    "RubricCriterion",
    "DatasetItem",
    "read_items",
    "write_items",
]

Difficulty = Literal["simple", "multi_hop", "extraction", "edge"]

#: Report order, and the order the 40/25/20/15 mix is declared in.
DIFFICULTIES: tuple[Difficulty, ...] = ("simple", "multi_hop", "extraction", "edge")


class _Base(BaseModel):
    # Same posture as the trace and config schemas: an unrecognised key is a
    # drift bug and should surface at load time, not be silently dropped.
    model_config = ConfigDict(extra="forbid")


class Chunk(_Base):
    """One retrieved passage, with the ID the model must cite it by.

    Kept structured rather than pre-rendered because two consumers need
    different things from it: the prompt packs need text to show the model, and
    the citation-coverage metric needs the set of IDs that were actually
    supplied, so it can tell a real citation from an invented one.
    """

    chunk_id: str = Field(
        description="Retrieval-system ID, e.g. 'US11842891B2::desc::p0031'."
    )
    text: str


class DatasetInput(_Base):
    """The model-facing input, in :class:`~amw.adapters.base.ModelRequest` shape."""

    messages: list[str] = Field(min_length=1)
    chunks: list[Chunk] = Field(default_factory=list)

    @property
    def chunk_ids(self) -> list[str]:
        """IDs supplied to the model — the universe a citation may reference."""
        return [chunk.chunk_id for chunk in self.chunks]

    def context_chunks(self) -> list[str]:
        """Chunks rendered one-per-string for ``ModelRequest.context_chunks``.

        The ``[id] text`` form is the only place the ID is glued to the text, so
        every lane shows the model the same thing and a citation metric can rely
        on the ID having been visible.
        """
        return [f"[{chunk.chunk_id}] {chunk.text}" for chunk in self.chunks]


class RubricCriterion(_Base):
    """One pass/fail question about an output. No partial credit, no 1-5 scale.

    Binary criteria are what make a judged score aggregable and a failure
    cluster readable: "11 items failed ``no_invented_dates``" is actionable in a
    way that "mean score 3.4" is not.
    """

    id: str = Field(description="Stable slug, e.g. 'no_invented_dates'.")
    criterion: str = Field(
        description="A yes/no question about the output, answerable from the "
        "item's input and gold alone."
    )


class DatasetItem(_Base):
    """One evaluated case."""

    item_id: str = Field(description="Stable and unique, e.g. 'qr-0007'.")
    subagent: str
    customer: str
    domain: str
    provenance: Literal["synthetic", "customer"]
    #: The generator seed this item was produced under (ground rule 2).
    seed: int
    #: Bumped when template semantics change, so a stale corpus is detectable.
    generator_version: str
    difficulty: Difficulty
    template_id: str
    #: True if the item is in the judged stratified core split.
    core: bool = False
    input: DatasetInput
    #: Gold reference output. Validated against ``schema_model(subagent)``.
    gold: dict[str, Any]
    rubric: list[RubricCriterion] = Field(min_length=3, max_length=5)
    #: "template" = prose composed from the seeded templates alone.
    #: "gemini"   = a recorded Gemini call naturalised the surface prose, after
    #:              passing the fact-preservation guard. Never means the *gold*
    #:              was model-authored: gold is derived from the scenario facts.
    surface_source: Literal["template", "gemini"] = "template"

    @model_validator(mode="after")
    def _check_contract(self) -> "DatasetItem":
        if self.subagent not in SUBAGENTS:
            raise ValueError(
                f"unknown subagent {self.subagent!r}; expected one of {list(SUBAGENTS)}"
            )
        model = schema_model(self.subagent)
        try:
            model.model_validate(self.gold)
        except Exception as exc:
            raise ValueError(
                f"{self.item_id}: gold output does not satisfy "
                f"{model.__name__} (amw/agents/schemas.py): {exc}"
            ) from exc

        ids = {c.id for c in self.rubric}
        if len(ids) != len(self.rubric):
            raise ValueError(f"{self.item_id}: duplicate rubric criterion ids")

        supplied = set(self.input.chunk_ids)
        if len(supplied) != len(self.input.chunks):
            raise ValueError(f"{self.item_id}: duplicate chunk_id in input")

        # A gold answer that cites a chunk nobody supplied would teach the
        # citation-coverage metric to accept invented citations.
        for point in self.gold.get("key_points") or []:
            cited = set(point.get("chunk_ids") or [])
            unknown = sorted(cited - supplied)
            if unknown:
                raise ValueError(
                    f"{self.item_id}: gold key_point cites chunk(s) {unknown} "
                    f"that were not supplied (supplied: {sorted(supplied)})"
                )
        return self

    def gold_model(self):
        """The gold output as its pydantic instance from the frozen contract."""
        return schema_model(self.subagent).model_validate(self.gold)

    def to_jsonl_line(self) -> str:
        """Canonical single-line JSON. Byte-stable across processes."""
        return self.model_dump_json() + "\n"

    @classmethod
    def from_jsonl_line(cls, line: str) -> "DatasetItem":
        return cls.model_validate_json(line)


# --------------------------------------------------------------------------
# JSONL I/O
# --------------------------------------------------------------------------


def iter_items(path: str | Path) -> Iterator[DatasetItem]:
    path = Path(path)
    with path.open("r", encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                yield DatasetItem.from_jsonl_line(line)
            except Exception as exc:
                raise ValueError(
                    f"{path}:{lineno}: not a valid dataset item: {exc}"
                ) from exc


def read_items(path: str | Path) -> list[DatasetItem]:
    """Parse a dataset file. Bad lines name themselves."""
    return list(iter_items(path))


def write_items(path: str | Path, items: Iterable[DatasetItem]) -> Path:
    """Write a dataset file, replacing it. Round-trips byte-stably."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for item in items:
            handle.write(item.to_jsonl_line())
    return path


def pretty(item: DatasetItem) -> str:
    """Human-readable dump, for the realism review checkpoint."""
    return json.dumps(item.model_dump(), indent=2, ensure_ascii=False)
