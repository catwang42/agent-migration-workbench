"""Chunk Summarizer templates: retrieved passages -> a grounded summary.

The failure mode this subagent exists to avoid is a fluent summary that is not
in the chunks. So every template is built to make ungroundedness *detectable*
rather than merely discouraged:

* Chunk IDs are retrieval-system shaped (``US11842891B2::desc::p0031``), not
  ``c1``. A model that invents a citation invents something that looks like
  these, and the citation-coverage metric catches it because the ID was never
  supplied.
* Several templates plant a **distractor** — a passage from a different patent
  on a neighbouring topic. Citing it is a specific, countable error.
* One template asks a question the chunks **do not answer**. The correct output
  says so. This is the item that separates a model that abstains from one that
  smooths over the gap, and it is the single most valuable row in the set for a
  RAG customer.
* One template plants a **contradiction** between two passages. The correct
  output attributes both values rather than averaging them or silently picking
  one.

Gold ``key_points`` always cite chunks that were actually supplied — the item
schema enforces it — so the gold cannot teach the metric to accept invention.
"""

from __future__ import annotations

import random
import re

from amw.agents.schemas import ChunkSummary, KeyPoint
from amw.datasets.patents_bank import ASSIGNEES, TECHS
from amw.datasets.schema import Chunk
from amw.datasets.templates.common import (
    Draft,
    SurfaceTarget,
    Template,
    article,
    bare,
    body,
    cap,
    criterion,
    pick,
    pick_tech,
    sentence_safe,
)

__all__ = ["TEMPLATES"]

_SPEC_STYLE = (
    "a paragraph of a granted patent specification: formal, impersonal, "
    "present tense, the register of a patent attorney drafting a description; "
    "keep it to two or three sentences; do not add a heading, a paragraph "
    "number, or any fact that is not already stated"
)


def _pub_number(rng: random.Random, office: str = "US") -> str:
    if office == "US":
        return f"US{rng.randrange(10_500_000, 12_400_000)}B2"
    if office == "EP":
        return f"EP{rng.randrange(3_100_000, 4_400_000)}A1"
    return f"WO{rng.randrange(2019, 2025)}/{rng.randrange(100000, 199999)}"


def _chunk_id(pub: str, section: str, para: int) -> str:
    return f"{pub}::{section}::p{para:04d}"


#: The first numeric literal in a measured value, however it is written:
#: "3.2 mS/cm", "1,450 mAh/g", "1.4e-4", "epsilon = 2.1", "under 2%".
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?(?:e-?\d+)?")


def _perturb(value: str, rng: random.Random) -> str:
    """A different figure for the same property, in the same units and format.

    Used to plant a contradiction between two passages. Scaling the real number
    rather than substituting a canned one keeps the rival value plausible for
    the property in question — a rival ionic conductivity of "0.9 mS/cm" is a
    real disagreement a reader must notice, whereas "0.9 %" is just noise.
    """
    match = _NUMBER.search(value)
    if not match:
        return value
    try:
        number = float(match.group(0).replace(",", ""))
    except ValueError:  # pragma: no cover - the regex only matches numerals
        return value

    raw = match.group(0)
    is_percentage = "%" in value
    for factor in rng.sample([0.62, 0.75, 1.35, 1.7], 4):
        scaled = number * factor
        if is_percentage and scaled > 100:
            # An "encapsulation efficiency of 160%" is not a disagreement a
            # reader has to resolve, it is a typo they discount. The item only
            # works if both figures are individually credible.
            continue
        if "e" in raw:
            text = f"{scaled:.1e}"
        elif "." in raw:
            places = len(raw.split(".", 1)[1])
            text = f"{scaled:,.{places}f}" if "," in raw else f"{scaled:.{places}f}"
        else:
            text = f"{round(scaled):,}" if "," in raw else f"{round(scaled)}"
        if text != raw:
            return value[: match.start()] + text + value[match.end() :]
    return value  # pragma: no cover - some factor always shifts the digits


def _amount(metric) -> str:
    """A metric phrase in a running sentence: "an ionic conductivity of ...".

    ``Metric.phrase()`` is bare on purpose so it can sit in a table cell or a
    claim limitation. Anywhere it follows a verb it needs its article, and
    getting that wrong ("exhibited transfer yield of 99.994%") is exactly the
    tell a patent reader picks up on.
    """
    return f"{article(metric.name)} {metric.phrase()}"


def _ask(tech) -> str:
    return (
        f"Summarise what these passages establish about {tech.field_phrase}, "
        f"citing the passage each statement comes from."
    )


# --------------------------------------------------------------------------
# simple
# --------------------------------------------------------------------------


def cs_simple_problem_solution_result(rng: random.Random) -> Draft:
    tech = pick_tech(rng)
    pub = _pub_number(rng)
    base = rng.randrange(20, 60)
    ids = [_chunk_id(pub, "desc", base + i * 3) for i in range(3)]
    metric = tech.metrics[0]

    chunks = [
        Chunk(
            chunk_id=ids[0],
            text=(
                f"A persistent difficulty in {tech.field_phrase} is "
                f"{tech.problem}. Known approaches address this only partially "
                f"and at a cost in manufacturability."
            ),
        ),
        Chunk(
            chunk_id=ids[1],
            text=(
                f"The present disclosure provides {tech.claim_subject} "
                f"comprising {tech.claim_feature}."
            ),
        ),
        Chunk(
            chunk_id=ids[2],
            text=(
                f"In Example 1, the resulting article exhibited "
                f"{_amount(metric)}, which the inventors attribute to the "
                f"configuration described above."
            ),
        ),
    ]
    gold = ChunkSummary(
        summary=(
            f"The passages describe {tech.claim_subject} directed at "
            f"{tech.problem}. The disclosure recites {tech.claim_feature}, "
            f"and a worked example reports {_amount(metric)}."
        ),
        key_points=[
            KeyPoint(
                text=f"The problem addressed is {tech.problem}.",
                chunk_ids=[ids[0]],
            ),
            KeyPoint(
                text=(
                    f"The disclosed {bare(tech.claim_subject)} comprises "
                    f"{tech.claim_feature}."
                ),
                chunk_ids=[ids[1]],
            ),
            KeyPoint(
                text=f"Example 1 reports {_amount(metric)}.",
                chunk_ids=[ids[2]],
            ),
        ],
    )
    return Draft(
        template_id="cs_simple_problem_solution_result",
        difficulty="simple",
        messages=[_ask(tech)],
        chunks=chunks,
        gold=gold,
        rubric=[
            criterion(
                "every_point_cited",
                "Does every key point cite at least one chunk ID, and is every "
                "cited ID one of the three that were supplied?",
            ),
            criterion(
                "all_three_covered",
                "Do the key points cover all three passages — the problem, the "
                "disclosed configuration, and the reported result?",
            ),
            criterion(
                "value_exact",
                f"Is the reported value stated as {metric.value!r}, unrounded "
                "and unconverted?",
            ),
            criterion(
                "no_added_facts",
                "Does the summary avoid any claim that is not in the passages "
                "— no invented advantage, no comparison to prior art that the "
                "passages do not make?",
            ),
        ],
        surface=tuple(
            SurfaceTarget(
                kind="chunk",
                index=i,
                style=_SPEC_STYLE,
                must_keep=keep,
            )
            for i, keep in enumerate(
                (
                    (),
                    (),
                    (metric.value,),
                )
            )
        ),
    )


def cs_simple_two_passage_claim(rng: random.Random) -> Draft:
    tech = pick_tech(rng)
    org = ASSIGNEES[pick(rng, tech.assignees or tuple(ASSIGNEES))]
    pub = _pub_number(rng)
    ids = [
        _chunk_id(pub, "claims", 1),
        _chunk_id(pub, "desc", rng.randrange(30, 70)),
    ]
    chunks = [
        Chunk(
            chunk_id=ids[0],
            text=(
                f"1. {cap(tech.claim_subject)}, comprising "
                f"{tech.claim_feature}."
            ),
        ),
        Chunk(
            chunk_id=ids[1],
            text=(
                f"The applicant, {org.name}, notes that the configuration of "
                f"claim 1 is intended to mitigate {tech.problem}."
            ),
        ),
    ]
    gold = ChunkSummary(
        summary=(
            f"Claim 1 recites {tech.claim_subject} comprising "
            f"{tech.claim_feature}. The description states that this "
            f"configuration is intended to mitigate {tech.problem}, and "
            f"identifies the applicant as {sentence_safe(org.name)}."
        ),
        key_points=[
            KeyPoint(
                text=f"Claim 1 recites {tech.claim_feature}.",
                chunk_ids=[ids[0]],
            ),
            KeyPoint(
                text=f"The stated purpose is to mitigate {tech.problem}.",
                chunk_ids=[ids[1]],
            ),
            KeyPoint(
                text=f"The applicant is {sentence_safe(org.name)}.",
                chunk_ids=[ids[1]],
            ),
        ],
        # 'intended to mitigate' is the passage's own hedge; the summary keeps it.
    )
    return Draft(
        template_id="cs_simple_two_passage_claim",
        difficulty="simple",
        messages=[_ask(tech)],
        chunks=chunks,
        gold=gold,
        rubric=[
            criterion(
                "claim_language_preserved",
                "Is the claim's limiting language reproduced or closely "
                "paraphrased, rather than generalised away?",
            ),
            criterion(
                "hedge_preserved",
                "Is the purpose reported as something the passage says the "
                "configuration is *intended* to do, rather than asserted as a "
                "demonstrated result?",
            ),
            criterion(
                "applicant_cited_correctly",
                f"If the applicant {org.name!r} is mentioned, is it cited to "
                f"{ids[1]!r}, the passage that actually names it?",
            ),
            criterion(
                "no_uncited_points",
                "Does every key point carry at least one supplied chunk ID?",
            ),
        ],
        surface=(
            SurfaceTarget(
                kind="chunk",
                index=1,
                style=_SPEC_STYLE,
                must_keep=(org.name,),
            ),
        ),
    )


# --------------------------------------------------------------------------
# multi-hop
# --------------------------------------------------------------------------


def cs_multihop_synthesis_across_chunks(rng: random.Random) -> Draft:
    tech = pick_tech(rng)
    pub = _pub_number(rng)
    base = rng.randrange(20, 55)
    ids = [_chunk_id(pub, "desc", base + i * 4) for i in range(3)]
    m_feature, m_result = tech.metrics[0], tech.metrics[1]

    chunks = [
        Chunk(
            chunk_id=ids[0],
            text=(
                f"In one embodiment, {tech.claim_subject} is provided, "
                f"comprising {tech.claim_feature}."
            ),
        ),
        Chunk(
            chunk_id=ids[1],
            text=(
                f"Comparative Example C1, which omits the feature described "
                f"above, exhibited {article(m_feature.name)} {m_feature.name} "
                f"of approximately half the value obtained in the Examples."
            ),
        ),
        Chunk(
            chunk_id=ids[2],
            text=(
                f"Example 3, prepared according to the embodiment, exhibited "
                f"{_amount(m_feature)} and {_amount(m_result)}."
            ),
        ),
    ]
    gold = ChunkSummary(
        summary=(
            f"The passages set out {tech.claim_subject} comprising "
            f"{tech.claim_feature}, and compare it against a control that omits "
            f"that feature. Example 3 reports {_amount(m_feature)} and "
            f"{_amount(m_result)}, while Comparative Example C1 reaches roughly "
            f"half the {m_feature.name}, so the passages together attribute the "
            f"improvement to the recited feature."
        ),
        key_points=[
            KeyPoint(
                text=f"The embodiment is characterised by {tech.claim_feature}.",
                chunk_ids=[ids[0]],
            ),
            KeyPoint(
                text=(
                    f"Example 3 achieves {_amount(m_feature)}, about twice the "
                    f"{m_feature.name} of Comparative Example C1, which omits "
                    f"the recited feature."
                ),
                # The comparison exists in neither passage alone.
                chunk_ids=[ids[1], ids[2]],
            ),
            KeyPoint(
                text=f"Example 3 also reports {_amount(m_result)}.",
                chunk_ids=[ids[2]],
            ),
        ],
    )
    return Draft(
        template_id="cs_multihop_synthesis_across_chunks",
        difficulty="multi_hop",
        messages=[_ask(tech)],
        chunks=chunks,
        gold=gold,
        rubric=[
            criterion(
                "comparison_cites_both",
                "Does the key point that compares Example 3 with Comparative "
                "Example C1 cite BOTH passages? Neither passage states the "
                "comparison on its own.",
            ),
            criterion(
                "relative_claim_hedged",
                "Is the C1 figure described as approximately half, matching the "
                "passage's own 'approximately', rather than given a precise "
                "number the passages never state?",
            ),
            criterion(
                "value_exact",
                f"Is {m_feature.value!r} reported exactly as written?",
            ),
            criterion(
                "attribution_supported",
                "Is the improvement attributed to the recited feature only to "
                "the extent the passages support it, with no added mechanism or "
                "explanation?",
            ),
        ],
        surface=(
            SurfaceTarget(
                kind="chunk",
                index=2,
                style=_SPEC_STYLE,
                must_keep=(m_feature.value, m_result.value),
            ),
        ),
    )


def cs_multihop_two_patents_same_field(rng: random.Random) -> Draft:
    tech = pick_tech(rng)
    keys = list(tech.assignees or tuple(ASSIGNEES))
    key_a, key_b = rng.sample(keys, 2) if len(keys) > 1 else (keys[0], keys[0])
    org_a, org_b = ASSIGNEES[key_a], ASSIGNEES[key_b]
    pub_a, pub_b = _pub_number(rng), _pub_number(rng, "EP")
    ids = [
        _chunk_id(pub_a, "claims", 1),
        _chunk_id(pub_b, "claims", 1),
        _chunk_id(pub_b, "desc", rng.randrange(40, 80)),
    ]
    chunks = [
        Chunk(
            chunk_id=ids[0],
            text=(
                f"1. {cap(tech.claim_subject)}, comprising "
                f"{tech.claim_feature}. (Applicant: {sentence_safe(org_a.name)}.)"
            ),
        ),
        Chunk(
            chunk_id=ids[1],
            # The refinement already opens with "wherein", so it trails the
            # body rather than being introduced by a second "comprising" —
            # "comprising wherein" is not something an attorney would file.
            # The second claim recites the same body plus one more limitation,
            # which is the realistic shape of two applications racing in one
            # field, and gives the summariser a real distinction to draw.
            text=(
                f"1. {cap(tech.claim_subject)}, comprising "
                f"{tech.claim_feature}, {tech.claim_refinement}. "
                f"(Applicant: {sentence_safe(org_b.name)}.)"
            ),
        ),
        Chunk(
            chunk_id=ids[2],
            text=(
                f"The approach of the present application differs from earlier "
                f"proposals in that no additional processing step is required "
                f"to address {tech.problem}."
            ),
        ),
    ]
    gold = ChunkSummary(
        summary=(
            f"Two independent claims in {tech.field_phrase} are presented. "
            f"{org_a.name} claims {tech.claim_subject} comprising "
            f"{tech.claim_feature}; {org_b.name} claims the same combination "
            f"with the further limitation that {body(tech.claim_refinement)}. "
            f"The second application also states that its approach needs no "
            f"additional processing step to address {tech.problem}."
        ),
        key_points=[
            KeyPoint(
                text=(
                    f"{org_a.name} claims {tech.claim_subject} "
                    f"comprising {tech.claim_feature}."
                ),
                chunk_ids=[ids[0]],
            ),
            KeyPoint(
                text=(
                    f"{org_b.name} claims the same combination with the "
                    f"further limitation that {body(tech.claim_refinement)}."
                ),
                chunk_ids=[ids[1]],
            ),
            KeyPoint(
                text=(
                    "Both claims are directed to the same subject matter, "
                    f"{tech.claim_subject}, but the second recites an "
                    f"additional limitation the first does not."
                ),
                chunk_ids=[ids[0], ids[1]],
            ),
            KeyPoint(
                text=(
                    f"The second application states that its approach "
                    f"requires no additional processing step to address "
                    f"{tech.problem}."
                ),
                chunk_ids=[ids[2]],
            ),
        ],
    )
    return Draft(
        template_id="cs_multihop_two_patents_same_field",
        difficulty="multi_hop",
        messages=[
            f"These passages come from two different applications. Summarise "
            f"what each one claims about {tech.field_phrase} and how they "
            f"differ, citing the passage behind each statement."
        ],
        chunks=chunks,
        gold=gold,
        rubric=[
            criterion(
                "claims_not_merged",
                "Are the two applications' claims kept separate and attributed "
                "to the correct applicant, rather than merged into one "
                "description?",
            ),
            criterion(
                "difference_cites_both",
                "Does the statement that the two claims differ cite both claim "
                "passages?",
            ),
            criterion(
                "third_passage_attributed",
                f"Is the 'no additional processing step' statement attributed "
                f"to the second application and cited to {ids[2]!r}?",
            ),
            criterion(
                "no_novelty_verdict",
                "Does the summary avoid deciding which application is novel or "
                "which came first? The passages do not say.",
            ),
        ],
        surface=(
            SurfaceTarget(
                kind="chunk",
                index=2,
                style=_SPEC_STYLE,
                must_keep=(),
            ),
        ),
    )


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------


def cs_extraction_results_table(rng: random.Random) -> Draft:
    tech = pick_tech(rng)
    pub = _pub_number(rng)
    base = rng.randrange(60, 95)
    ids = [_chunk_id(pub, "desc", base + i * 2) for i in range(3)]
    m0, m1, m2 = tech.metrics[0], tech.metrics[1], tech.metrics[2]
    chunks = [
        Chunk(
            chunk_id=ids[0],
            text=(
                f"Table 2 summarises the measured properties of Example 5: "
                f"{m0.name}, {m0.value}{(' ' + m0.context) if m0.context else ''}; "
                f"{m1.name}, {m1.value}{(' ' + m1.context) if m1.context else ''}."
            ),
        ),
        Chunk(
            chunk_id=ids[1],
            text=(
                f"The same Example was further characterised, giving "
                f"{_amount(m2)}."
            ),
        ),
        Chunk(
            chunk_id=ids[2],
            text=(
                f"Measurements were carried out on three replicate runs per "
                f"condition, and the values reported are arithmetic means."
            ),
        ),
    ]
    gold = ChunkSummary(
        summary=(
            f"For Example 5, the passages report {_amount(m0)}, {_amount(m1)} "
            f"and {_amount(m2)}. The reported values are arithmetic means over "
            f"three replicate runs per condition."
        ),
        key_points=[
            KeyPoint(text=f"{cap(m0.name)}: {m0.value}.", chunk_ids=[ids[0]]),
            KeyPoint(text=f"{cap(m1.name)}: {m1.value}.", chunk_ids=[ids[0]]),
            KeyPoint(text=f"{cap(m2.name)}: {m2.value}.", chunk_ids=[ids[1]]),
            KeyPoint(
                text=(
                    "The reported values are arithmetic means over three "
                    "replicate runs per condition."
                ),
                chunk_ids=[ids[2]],
            ),
        ],
    )
    return Draft(
        template_id="cs_extraction_results_table",
        difficulty="extraction",
        messages=[
            "Pull out every measured value in these passages, with the "
            "passage each one comes from. Do not round or convert."
        ],
        chunks=chunks,
        gold=gold,
        rubric=[
            criterion(
                "all_values_present",
                f"Are all three values present: {m0.value!r}, {m1.value!r} and "
                f"{m2.value!r}?",
            ),
            criterion(
                "values_verbatim",
                "Is each value reproduced exactly as written — same digits, "
                "same units, no rounding, no unit conversion?",
            ),
            criterion(
                "correct_source_per_value",
                f"Are the first two values cited to {ids[0]!r} and the third to "
                f"{ids[1]!r}?",
            ),
            criterion(
                "measurement_caveat_kept",
                "Is the caveat that the values are means over three replicate runs "
                "retained and cited?",
            ),
        ],
        surface=(
            SurfaceTarget(
                kind="chunk",
                index=1,
                style=_SPEC_STYLE,
                must_keep=(m2.value,),
            ),
        ),
    )


# --------------------------------------------------------------------------
# edge
# --------------------------------------------------------------------------


def cs_edge_distractor_chunk(rng: random.Random) -> Draft:
    tech = pick_tech(rng)
    other = pick(rng, [t for t in TECHS if t.key != tech.key])
    pub, pub_other = _pub_number(rng), _pub_number(rng)
    base = rng.randrange(20, 50)
    ids = [
        _chunk_id(pub, "desc", base),
        _chunk_id(pub, "desc", base + 5),
        _chunk_id(pub_other, "desc", rng.randrange(10, 40)),
    ]
    chunks = [
        Chunk(
            chunk_id=ids[0],
            text=(
                f"The disclosure relates to {tech.claim_subject}, comprising "
                f"{tech.claim_feature}."
            ),
        ),
        Chunk(
            chunk_id=ids[1],
            text=(
                f"This configuration is said to reduce the impact of "
                f"{tech.problem}."
            ),
        ),
        # Same document family conventions, neighbouring shelf, wrong subject.
        Chunk(
            chunk_id=ids[2],
            text=(
                f"In a further aspect, {other.claim_subject} is provided "
                f"comprising {other.claim_feature}. Suitable applications "
                f"include {other.colloquial}."
            ),
        ),
    ]
    gold = ChunkSummary(
        summary=(
            f"The relevant passages describe {tech.claim_subject} comprising "
            f"{tech.claim_feature}, and state that this configuration reduces "
            f"the impact of {tech.problem}. A third passage concerns "
            f"{other.colloquial} and does not bear on the question."
        ),
        key_points=[
            KeyPoint(
                text=(
                    f"The subject matter is {tech.claim_subject} comprising "
                    f"{tech.claim_feature}."
                ),
                chunk_ids=[ids[0]],
            ),
            KeyPoint(
                text=f"The configuration is said to reduce the impact of {tech.problem}.",
                chunk_ids=[ids[1]],
            ),
        ],
    )
    return Draft(
        template_id="cs_edge_distractor_chunk",
        difficulty="edge",
        messages=[_ask(tech)],
        chunks=chunks,
        gold=gold,
        rubric=[
            criterion(
                "distractor_not_cited",
                f"Is {ids[2]!r} absent from every key point's chunk_ids? That "
                f"passage is about {other.colloquial}, not the question asked.",
            ),
            criterion(
                "distractor_content_absent",
                f"Is {other.colloquial!r} absent from the key points? Including "
                "it would make the summary answer a question nobody asked.",
            ),
            criterion(
                "relevant_points_kept",
                "Are both relevant passages summarised and cited?",
            ),
            criterion(
                "hedge_preserved",
                "Is the benefit reported as something the passage 'says' or "
                "'is said to' do, rather than as a measured result?",
            ),
        ],
        surface=(
            SurfaceTarget(
                kind="chunk",
                index=2,
                style=_SPEC_STYLE,
                # The distractor only distracts while it is about something
                # else. A rewrite that drops its own subject, or that drifts it
                # toward the question's field, turns the item into an ordinary
                # three-passage summary and the rubric stops testing anything.
                must_keep=(other.colloquial,),
                forbid=(re.escape(tech.field_phrase),),
            ),
        ),
    )


def cs_edge_question_not_answered(rng: random.Random) -> Draft:
    tech = pick_tech(rng)
    pub = _pub_number(rng)
    base = rng.randrange(15, 45)
    ids = [_chunk_id(pub, "desc", base + i * 3) for i in range(2)]
    chunks = [
        Chunk(
            chunk_id=ids[0],
            text=(
                f"The disclosure relates generally to {tech.field_phrase} and, "
                f"more particularly, to {tech.claim_subject}."
            ),
        ),
        Chunk(
            chunk_id=ids[1],
            text=(
                f"Conventional approaches to {tech.field_phrase} are described "
                f"in the background section and are not repeated here."
            ),
        ),
    ]
    question = (
        f"According to these passages, what manufacturing cost per unit does "
        f"the disclosed {tech.claim_subject} achieve?"
    )
    gold = ChunkSummary(
        summary=(
            f"The supplied passages do not state a manufacturing cost per unit. "
            f"They establish only the field of the disclosure — "
            f"{tech.field_phrase}, and more particularly {tech.claim_subject} — "
            f"and note that conventional approaches are described elsewhere in "
            f"the document. The question cannot be answered from these passages."
        ),
        key_points=[
            KeyPoint(
                text=(
                    f"The passages identify the field as {tech.field_phrase}, "
                    f"more particularly {tech.claim_subject}."
                ),
                chunk_ids=[ids[0]],
            ),
            KeyPoint(
                text=(
                    "The passages state that conventional approaches are "
                    "described in the background section, which was not supplied."
                ),
                chunk_ids=[ids[1]],
            ),
        ],
    )
    return Draft(
        template_id="cs_edge_question_not_answered",
        difficulty="edge",
        messages=[question],
        chunks=chunks,
        gold=gold,
        rubric=[
            criterion(
                "states_not_answerable",
                "Does the summary say plainly that the passages do not provide "
                "a manufacturing cost?",
            ),
            criterion(
                "no_cost_figure",
                "Is there no cost figure, cost range, or cost estimate anywhere "
                "in the output? Any number here is fabricated.",
            ),
            criterion(
                "no_external_knowledge",
                "Does the output avoid answering from general knowledge about "
                "the technology instead of from the passages?",
            ),
            criterion(
                "points_still_grounded",
                "Do the key points that are made still cite supplied chunk IDs?",
            ),
        ],
        surface=(
            SurfaceTarget(
                kind="chunk",
                index=0,
                style=_SPEC_STYLE,
                # Nothing in these passages may acquire a cost or a currency.
                forbid=(r"[$€£¥]", r"\bcost\b", r"\bper unit\b", r"\bUSD\b"),
            ),
        ),
    )


def cs_edge_contradictory_values(rng: random.Random) -> Draft:
    tech = pick_tech(rng)
    metric = tech.metrics[0]
    pub_a, pub_b = _pub_number(rng), _pub_number(rng, "EP")
    ids = [
        _chunk_id(pub_a, "desc", rng.randrange(50, 80)),
        _chunk_id(pub_b, "desc", rng.randrange(50, 80)),
    ]
    # A genuinely different number in the same units, stated by another document.
    rival = _perturb(metric.value, rng)
    chunks = [
        Chunk(
            chunk_id=ids[0],
            text=(
                f"Example 2 of the present application exhibited "
                f"{_amount(metric)}."
            ),
        ),
        Chunk(
            chunk_id=ids[1],
            text=(
                f"A comparable material is reported in the literature as having "
                f"{article(metric.name)} {metric.name} of {rival}, measured "
                f"under conditions that are not specified."
            ),
        ),
    ]
    gold = ChunkSummary(
        summary=(
            f"The two passages give different figures for {metric.name}. The "
            f"present application reports {metric.value} for Example 2, while "
            f"the second passage reports {rival} for a comparable material "
            f"under unspecified conditions. The passages do not reconcile the "
            f"difference."
        ),
        key_points=[
            KeyPoint(
                text=f"Example 2 of the present application: {metric.name} of {metric.value}.",
                chunk_ids=[ids[0]],
            ),
            KeyPoint(
                text=(
                    f"A comparable material is reported at {rival}, under "
                    f"conditions the passage does not specify."
                ),
                chunk_ids=[ids[1]],
            ),
            KeyPoint(
                text=(
                    f"The two reported {metric.name} figures differ and the "
                    f"passages do not reconcile them."
                ),
                chunk_ids=[ids[0], ids[1]],
            ),
        ],
    )
    return Draft(
        template_id="cs_edge_contradictory_values",
        difficulty="edge",
        messages=[
            f"What do these passages report for {metric.name}? Cite the passage "
            f"behind each figure."
        ],
        chunks=chunks,
        gold=gold,
        rubric=[
            criterion(
                "both_values_reported",
                f"Are both figures present — {metric.value!r} and {rival!r} — "
                "each attributed to its own passage?",
            ),
            criterion(
                "no_averaging",
                "Did the output avoid averaging, splitting the difference, or "
                "silently preferring one figure over the other?",
            ),
            criterion(
                "conflict_flagged",
                "Does the output note that the two figures differ?",
            ),
            criterion(
                "conditions_caveat",
                "Is it noted that the second figure's measurement conditions "
                "are unspecified?",
            ),
        ],
        surface=(
            SurfaceTarget(
                kind="chunk",
                index=1,
                style=_SPEC_STYLE,
                must_keep=(rival,),
            ),
        ),
    )


TEMPLATES: tuple[Template, ...] = (
    Template(
        "cs_simple_problem_solution_result",
        "simple",
        cs_simple_problem_solution_result,
    ),
    Template("cs_simple_two_passage_claim", "simple", cs_simple_two_passage_claim),
    Template(
        "cs_multihop_synthesis_across_chunks",
        "multi_hop",
        cs_multihop_synthesis_across_chunks,
    ),
    Template(
        "cs_multihop_two_patents_same_field",
        "multi_hop",
        cs_multihop_two_patents_same_field,
    ),
    Template("cs_extraction_results_table", "extraction", cs_extraction_results_table),
    Template("cs_edge_distractor_chunk", "edge", cs_edge_distractor_chunk),
    Template("cs_edge_question_not_answered", "edge", cs_edge_question_not_answered),
    Template("cs_edge_contradictory_values", "edge", cs_edge_contradictory_values),
)
