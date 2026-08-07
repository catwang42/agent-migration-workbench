"""Shared machinery for the per-subagent templates.

A template is a pure function ``(rng) -> Draft``. It gets a seeded
:class:`random.Random` and nothing else, which is what makes the whole
generator reproducible: no clock, no global RNG, no environment.

The important invariant lives here rather than in each template:

    **Gold is derived from the same facts as the input, never written twice.**

A template picks a scenario out of ``patents_bank``, builds the prose *from*
those facts, and builds the gold answer *from those same facts*. Nobody hand
writes a gold answer next to a hand-written question and hopes they agree —
that is how a dataset ends up quietly scoring the model against a typo.

:class:`SurfaceTarget` is the contract for the optional Gemini realism pass
(``amw/datasets/surface.py``). A template says which piece of prose may be
rewritten, which literals must survive the rewrite, and which patterns must
*not* appear afterwards. That last one matters most for the edge cases: an item
whose whole point is that the source text omits a filing date is destroyed if
the rewriter helpfully adds one, so the guard rejects the rewrite instead.
"""

from __future__ import annotations

import contextlib
import contextvars
import random
import re
from dataclasses import dataclass, field
from typing import Callable, Literal, Sequence

from pydantic import BaseModel

from amw.datasets.patents_bank import TECHS
from amw.datasets.schema import Chunk, Difficulty, RubricCriterion

__all__ = [
    "SurfaceTarget",
    "Draft",
    "Template",
    "TemplateFn",
    "criterion",
    "cap",
    "bare",
    "body",
    "sentence_safe",
    "article",
    "pick",
    "pick_tech",
    "tech_rotation",
    "iso",
    "DATE_PATTERN",
    "YEAR_PATTERN",
    "check_surface",
    "quantities",
    "unit_glyphs",
]

#: Anything that reads as a date. Used by edge templates that must stay
#: date-free through the realism pass.
DATE_PATTERN = (
    r"\b(?:\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4}"
    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4})\b"
)
YEAR_PATTERN = r"\b(?:19|20)\d{2}\b"


@dataclass(frozen=True)
class SurfaceTarget:
    """One piece of prose the realism pass is allowed to rewrite."""

    kind: Literal["message", "chunk"]
    index: int
    #: Register instruction handed to the rewriter, e.g. how a searcher types.
    style: str
    #: Substrings that must appear verbatim afterwards, case-insensitively.
    #: Every fact the gold answer depends on belongs here.
    must_keep: tuple[str, ...] = ()
    #: Regexes that must NOT match afterwards. Guards the deliberate absences.
    forbid: tuple[str, ...] = ()


@dataclass
class Draft:
    """A fully-formed item before it is stamped with ids, seed and provenance."""

    template_id: str
    difficulty: Difficulty
    messages: list[str]
    gold: BaseModel
    rubric: list[RubricCriterion]
    chunks: list[Chunk] = field(default_factory=list)
    surface: tuple[SurfaceTarget, ...] = ()


TemplateFn = Callable[[random.Random], Draft]


@dataclass(frozen=True)
class Template:
    """A registered template: its id, the bucket it fills, and its function."""

    template_id: str
    difficulty: Difficulty
    fn: TemplateFn


def criterion(cid: str, text: str) -> RubricCriterion:
    return RubricCriterion(id=cid, criterion=text)


def pick(rng: random.Random, options: Sequence):
    """``rng.choice`` with a clearer failure than IndexError on an empty bank."""
    if not options:
        raise ValueError("cannot pick from an empty option list")
    return rng.choice(list(options))


def cap(text: str) -> str:
    """Upper-case the first character only.

    ``str.capitalize`` lower-cases everything after it, which would turn
    ``Li6PS5Cl`` into ``li6ps5cl`` the first time a claim phrase starts with a
    chemical formula. The bank is full of them.
    """
    return text[:1].upper() + text[1:] if text else text


_LEADING_ARTICLE = re.compile(r"^(?:a|an|the)\s+", re.IGNORECASE)
_LEADING_WHEREIN = re.compile(r"^wherein\s+", re.IGNORECASE)


def bare(text: str) -> str:
    """Drop a leading article so a phrase can follow one already in the sentence.

    The bank stores claim subjects the way a claim states them ("an
    all-solid-state lithium secondary battery"), which reads correctly after
    "comprising" and wrongly after "The disclosed". Rather than storing each
    phrase twice, the templates strip the article at the point of use.
    """
    return _LEADING_ARTICLE.sub("", text, count=1)


def body(text: str) -> str:
    """Drop a leading "wherein" so a refinement can follow "comprising"."""
    return _LEADING_WHEREIN.sub("", text, count=1)


def sentence_safe(text: str) -> str:
    """Trim a trailing period so an embedded name does not produce "Ltd.."."""
    return text[:-1] if text.endswith(".") else text


def article(following: str) -> str:
    """"a" or "an" for the word that follows. Crude but right for this bank."""
    return "an" if following[:1].lower() in "aeiou" else "a"


def iso(year: int, month: int = 1, day: int = 1) -> str:
    return f"{year:04d}-{month:02d}-{day:02d}"


#: Per-run rotation of technology areas, installed by the generator.
#:
#: Templates each pick their own scenario, so with fourteen areas and ten items
#: independent draws collide: an early sample had three of ten feature-extractor
#: documents claiming the same membrane electrode assembly under three different
#: applicants. A patent searcher reads that as generated data, which is exactly
#: the impression the whole dataset has to avoid. The generator therefore deals
#: out a shuffled rotation so consecutive items land on different technologies.
#: A ContextVar rather than a module global so the state cannot leak between
#: concurrently generated subagents.
_TECH_ROTATION: contextvars.ContextVar[list | None] = contextvars.ContextVar(
    "amw_tech_rotation", default=None
)


@contextlib.contextmanager
def tech_rotation(techs: Sequence):
    """Deal ``techs`` out to :func:`pick_tech`, in order, for the duration."""
    token = _TECH_ROTATION.set(list(techs))
    try:
        yield
    finally:
        _TECH_ROTATION.reset(token)


def pick_tech(rng: random.Random):
    """The primary technology area for one item.

    Takes the next entry from an installed rotation, or falls back to a random
    draw so that a template called directly in a test still works.
    """
    rotation = _TECH_ROTATION.get()
    if rotation:
        return rotation.pop(0)
    return pick(rng, TECHS)


#: Every digit run in a passage: "30", "1.4e-4", "0.15", "99.994", "0562".
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*(?:[eE]-?\d+)?")

#: Characters that only ever show up in a unit or a symbol, never in prose.
#:
#: Numbers and units are what the gold answer is pinned to, so the realism pass
#: may not touch them — not even cosmetically. The rewrite that motivated this
#: guard turned "30 um" into "30 µm", which reads better and is still wrong: the
#: gold key point says "30 um", and an extraction rubric demanding a value
#: "exactly as written" cannot survive the passage and the answer key spelling
#: the same unit two different ways. Comparing the *set* of these characters
#: catches a re-spelled unit in either direction while staying blind to the
#: wording changes the pass exists to make.
_UNIT_GLYPHS = frozenset("µμ°²³⁻Ωωπ±×Å")


def quantities(text: str) -> set[str]:
    """Every number in ``text``, as written."""
    return {m.group(0) for m in _NUMBER.finditer(text)}


def unit_glyphs(text: str) -> set[str]:
    """The unit-only characters present in ``text``."""
    return {ch for ch in text if ch in _UNIT_GLYPHS}


def check_surface(
    text: str, target: SurfaceTarget, original: str | None = None
) -> str | None:
    """Return a reason the rewritten ``text`` is unusable, or None if it is fine.

    Used by the realism pass. Kept next to :class:`SurfaceTarget` so the rule
    and its enforcement cannot drift apart.

    ``must_keep``/``forbid`` are the per-template contract: they name the exact
    literals a particular item's gold depends on. Passing ``original`` adds the
    guard that holds for every item regardless of template — the numbers and the
    unit glyphs must come through untouched in both directions. Nothing may be
    dropped or re-spelled, and nothing new may appear.
    """
    lowered = text.lower()
    for literal in target.must_keep:
        if literal.lower() not in lowered:
            return f"dropped required literal {literal!r}"
    for pattern in target.forbid:
        match = re.search(pattern, text)
        if match:
            return f"introduced forbidden content {match.group(0)!r} (/{pattern}/)"
    if original is not None:
        before, after = quantities(original), quantities(text)
        if lost := sorted(before - after):
            return f"dropped number {lost[0]!r}"
        if gained := sorted(after - before):
            return f"introduced number {gained[0]!r}"
        before, after = unit_glyphs(original), unit_glyphs(text)
        if lost := sorted(before - after):
            return f"dropped unit character {lost[0]!r}"
        if gained := sorted(after - before):
            return f"re-spelled a unit using {gained[0]!r}"
    return None
