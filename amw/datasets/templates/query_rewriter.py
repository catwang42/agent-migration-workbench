"""Query Rewriter templates: a searcher's question -> a structured search plan.

What the subagent has to get right, and therefore what these templates
deliberately stress:

* **Filters must leave the query string.** A date range or an assignee left
  embedded in ``query`` is a silent retrieval bug — the search backend applies
  filters structurally. Several templates put the constraint in conversational
  form ("since the start of 2023") precisely to see whether it gets lifted out.
* **Constraints are spread across turns.** Real searchers refine. The multi-hop
  templates put the assignee in turn 1 and the technology in turn 2, so an
  agent that only reads the last message loses half the filter.
* **Abstention is an answer.** The edge templates ask questions whose honest
  answer includes an empty filter — a relative date with no anchor, a person's
  name where the schema only has ``assignees``. Inventing a plausible value
  there is worse than leaving it null, and the rubric says so explicitly.
"""

from __future__ import annotations

import random

from amw.agents.schemas import QueryFilters, QueryPlan
from amw.datasets.patents_bank import ASSIGNEES, TECHS
from amw.datasets.templates.common import (
    DATE_PATTERN,
    Draft,
    SurfaceTarget,
    Template,
    criterion,
    iso,
    pick,
    pick_tech,
)

__all__ = ["TEMPLATES"]

#: How a searcher types: lowercase-ish, elliptical, no bullet points.
_SEARCHER_STYLE = (
    "a professional patent searcher typing a request into a chat box: one or "
    "two sentences, plain prose, slightly informal, no bullet points, no "
    "greeting, no explanation of what they want it for"
)


#: How a person actually names the territory they want to sell in. The gold
#: still carries the two-letter office code, so mapping the phrase onto the
#: code is part of what the item tests.
_FTO_PHRASE = {
    "US": "in the United States",
    "EP": "in the EPO member states",
    "CN": "in China",
    "JP": "in Japan",
}


def _assignee_key(rng: random.Random, tech) -> str:
    return pick(rng, tech.assignees or tuple(ASSIGNEES))


def _assignee(rng: random.Random, tech):
    return ASSIGNEES[_assignee_key(rng, tech)]


def _plan(query: str, intent: str, **filters) -> QueryPlan:
    return QueryPlan(query=query, intent=intent, filters=QueryFilters(**filters))


# --------------------------------------------------------------------------
# simple
# --------------------------------------------------------------------------


def qr_simple_assignee_since(rng: random.Random) -> Draft:
    tech = pick_tech(rng)
    org = _assignee(rng, tech)
    year = pick(rng, (2019, 2020, 2021, 2022))
    question = (
        f"Pull together what {org.name} has filed on {tech.colloquial} "
        f"since the start of {year}."
    )
    return Draft(
        template_id="qr_simple_assignee_since",
        difficulty="simple",
        messages=[question],
        gold=_plan(
            " OR ".join(tech.terms[:4]),
            "landscape",
            assignees=[org.name],
            date_from=iso(year),
        ),
        rubric=[
            criterion(
                "assignee_lifted",
                f"Is {org.name!r} present in filters.assignees rather than left "
                "inside the query string?",
            ),
            criterion(
                "date_from_lifted",
                f"Is filters.date_from set to {iso(year)} (the start of {year}) "
                "and filters.date_to left null?",
            ),
            criterion(
                "query_is_technical",
                "Does the query string contain only technical subject-matter "
                "terms, with no company name and no date text?",
            ),
            criterion(
                "intent_landscape",
                "Is intent 'landscape'? The user is surveying one company's "
                "activity, not looking for prior art against a specific claim.",
            ),
        ],
        surface=(
            SurfaceTarget(
                kind="message",
                index=0,
                style=_SEARCHER_STYLE,
                must_keep=(org.name, str(year)),
            ),
        ),
    )


def qr_simple_prior_art_before(rng: random.Random) -> Draft:
    tech = pick_tech(rng)
    year = pick(rng, (2018, 2019, 2020, 2021))
    question = (
        f"We need prior art on {tech.field_phrase} published before "
        f"{iso(year, 6, 1)}. Anything relevant?"
    )
    return Draft(
        template_id="qr_simple_prior_art_before",
        difficulty="simple",
        messages=[question],
        gold=_plan(
            " OR ".join(tech.terms[:4]),
            "prior_art",
            date_to=iso(year, 6, 1),
        ),
        rubric=[
            criterion(
                "intent_prior_art",
                "Is intent 'prior_art'?",
            ),
            criterion(
                "date_to_set",
                f"Is filters.date_to exactly {iso(year, 6, 1)}, with "
                "filters.date_from left null?",
            ),
            criterion(
                "no_invented_filters",
                "Are filters.assignees, filters.jurisdictions and "
                "filters.cpc_codes all empty? The question names none of them.",
            ),
            criterion(
                "query_expanded",
                f"Does the query cover the subject matter of "
                f"{tech.field_phrase!r} with at least one synonym or "
                "field-standard term rather than repeating the question?",
            ),
        ],
        surface=(
            SurfaceTarget(
                kind="message",
                index=0,
                style=_SEARCHER_STYLE,
                must_keep=(iso(year, 6, 1),),
            ),
        ),
    )


def qr_simple_cpc_area(rng: random.Random) -> Draft:
    tech = pick_tech(rng)
    code = tech.cpc[0]
    question = (
        f"What does the {tech.colloquial} landscape look like under {code}? "
        f"Just the classification, no other constraints."
    )
    return Draft(
        template_id="qr_simple_cpc_area",
        difficulty="simple",
        messages=[question],
        gold=_plan(
            " OR ".join(tech.terms[:3]),
            "landscape",
            cpc_codes=[code],
        ),
        rubric=[
            criterion(
                "cpc_exact",
                f"Is filters.cpc_codes exactly [{code!r}], with the code copied "
                "verbatim including punctuation?",
            ),
            criterion(
                "no_extra_cpc",
                "Were no additional CPC codes added? The user asked for one "
                "classification and broadening it silently changes the result set.",
            ),
            criterion(
                "dates_null",
                "Are filters.date_from and filters.date_to both null?",
            ),
            criterion(
                "intent_landscape",
                "Is intent 'landscape'?",
            ),
        ],
        surface=(
            SurfaceTarget(
                kind="message",
                index=0,
                style=_SEARCHER_STYLE,
                must_keep=(code,),
            ),
        ),
    )


# --------------------------------------------------------------------------
# multi-hop
# --------------------------------------------------------------------------


def qr_multihop_same_company_new_angle(rng: random.Random) -> Draft:
    tech = pick_tech(rng)
    org_key = _assignee_key(rng, tech)
    org = ASSIGNEES[org_key]
    # The second turn moves the same applicant to a different technology, so
    # that technology has to be one the applicant plausibly works in. Picking
    # freely from TECHS produced pairings like Toyota and federated learning,
    # which a patent searcher spots instantly and stops trusting the dataset.
    siblings = [t for t in TECHS if t.key != tech.key and org_key in t.assignees]
    other = pick(rng, siblings or [t for t in TECHS if t.key != tech.key])
    juris = pick(rng, ("EP", "US", "JP"))
    year = pick(rng, (2021, 2022, 2023))
    turn1 = (
        f"Last week we went through {org.name}'s {tech.colloquial} portfolio "
        f"and it was mostly older material."
    )
    turn2 = (
        f"Same applicant, but this time the {other.colloquial} side of the "
        f"house — and only {juris} filings from {year} onwards."
    )
    return Draft(
        template_id="qr_multihop_same_company_new_angle",
        difficulty="multi_hop",
        messages=[turn1, turn2],
        gold=_plan(
            " OR ".join(other.terms[:4]),
            "landscape",
            assignees=[org.name],
            jurisdictions=[juris],
            date_from=iso(year),
        ),
        rubric=[
            criterion(
                "assignee_from_turn_1",
                f"Does filters.assignees contain {org.name!r}? The applicant is "
                "only named in the first turn — carrying it forward is the "
                "whole point of this item.",
            ),
            criterion(
                "subject_from_turn_2",
                f"Is the query about {other.colloquial!r} and NOT about "
                f"{tech.colloquial!r}? The second turn replaces the subject "
                "matter, it does not add to it.",
            ),
            criterion(
                "jurisdiction_and_date",
                f"Is filters.jurisdictions [{juris!r}] and filters.date_from "
                f"{iso(year)}?",
            ),
            criterion(
                "intent_landscape",
                "Is intent 'landscape'?",
            ),
        ],
        surface=(
            SurfaceTarget(
                kind="message",
                index=0,
                style=_SEARCHER_STYLE,
                must_keep=(org.name, tech.colloquial),
            ),
            SurfaceTarget(
                kind="message",
                index=1,
                style=_SEARCHER_STYLE + ", continuing the previous message",
                must_keep=(other.colloquial, juris, str(year)),
            ),
        ),
    )


def qr_multihop_fto_from_description(rng: random.Random) -> Draft:
    tech = pick_tech(rng)
    juris = pick(rng, ("US", "EP", "CN"))
    phrase = _FTO_PHRASE[juris]
    # "comprising X" is the only frame that fits every claim_feature in the
    # bank: some are noun phrases ("a solid electrolyte layer ...") and some
    # are gerunds from method claims ("picking up an array ...").
    turn1 = (
        f"Our next product line is in {tech.colloquial}. Technically it is "
        f"{tech.claim_subject}, comprising {tech.claim_feature}."
    )
    turn2 = f"Before we commit to the launch, are we clear to sell it {phrase}?"
    return Draft(
        template_id="qr_multihop_fto_from_description",
        difficulty="multi_hop",
        messages=[turn1, turn2],
        gold=_plan(
            " OR ".join(tech.terms[:4]),
            "freedom_to_operate",
            jurisdictions=[juris],
        ),
        rubric=[
            criterion(
                "intent_fto",
                "Is intent 'freedom_to_operate'? The user is asking whether "
                "they can ship, not searching for prior art to invalidate "
                "something.",
            ),
            criterion(
                "query_from_turn_1",
                "Is the query built from the technical description in the first "
                "turn? The second turn contains no subject matter at all.",
            ),
            criterion(
                "jurisdiction_only",
                f"Is filters.jurisdictions exactly [{juris!r}]? The user wrote "
                f"{phrase!r}, which has to be mapped onto the office code. "
                "Dates, assignees and CPC codes must all be left empty.",
            ),
            criterion(
                "no_self_assignee",
                "Was the user's own company NOT added to filters.assignees? An "
                "FTO search must look at everyone else's patents.",
            ),
        ],
        surface=(
            SurfaceTarget(
                kind="message",
                index=1,
                style=_SEARCHER_STYLE,
                must_keep=(phrase,),
            ),
        ),
    )


def qr_multihop_competitor_switch(rng: random.Random) -> Draft:
    tech = pick_tech(rng)
    first = _assignee(rng, tech)
    second_key = pick(
        rng, [k for k in (tech.assignees or tuple(ASSIGNEES)) if ASSIGNEES[k].name != first.name]
    )
    second = ASSIGNEES[second_key]
    code = tech.cpc[0]
    turn1 = (
        f"I have been reading {first.name}'s {code} filings on "
        f"{tech.colloquial}."
    )
    turn2 = (
        f"Now do the same classification but for {second.name} instead, and "
        f"drop {first.name} entirely."
    )
    return Draft(
        template_id="qr_multihop_competitor_switch",
        difficulty="multi_hop",
        messages=[turn1, turn2],
        gold=_plan(
            " OR ".join(tech.terms[:3]),
            "landscape",
            assignees=[second.name],
            cpc_codes=[code],
        ),
        rubric=[
            criterion(
                "assignee_replaced",
                f"Is filters.assignees exactly [{second.name!r}]? "
                f"{first.name!r} must NOT appear — the user explicitly dropped it.",
            ),
            criterion(
                "cpc_carried_forward",
                f"Is filters.cpc_codes [{code!r}]? The classification is only "
                "stated in the first turn.",
            ),
            criterion(
                "subject_carried_forward",
                f"Does the query still cover {tech.colloquial!r}, which is also "
                "only stated in the first turn?",
            ),
            criterion(
                "intent_landscape",
                "Is intent 'landscape'?",
            ),
        ],
        surface=(
            SurfaceTarget(
                kind="message",
                index=0,
                style=_SEARCHER_STYLE,
                must_keep=(first.name, code),
            ),
            SurfaceTarget(
                kind="message",
                index=1,
                style=_SEARCHER_STYLE + ", continuing the previous message",
                must_keep=(second.name, first.name),
            ),
        ),
    )


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------


def qr_extraction_boolean_brief(rng: random.Random) -> Draft:
    tech = pick_tech(rng)
    org_a = _assignee(rng, tech)
    # Both applicants have to be credible in this field; a brief pairing a
    # display-panel house with a battery maker is the tell that the data is made
    # up.
    peers = [
        k for k in (tech.assignees or tuple(ASSIGNEES)) if ASSIGNEES[k].name != org_a.name
    ]
    org_b_key = pick(
        rng, peers or [k for k in ASSIGNEES if ASSIGNEES[k].name != org_a.name]
    )
    org_b = ASSIGNEES[org_b_key]
    codes = list(tech.cpc[:2])
    y1, y2 = pick(rng, ((2019, 2022), (2020, 2023), (2018, 2021)))
    question = (
        f"Search brief: subject matter {tech.field_phrase}; classifications "
        f"{codes[0]} and {codes[1]}; applicants {org_a.name} or {org_b.name}; "
        f"jurisdictions US and EP; filing date between {iso(y1, 4, 1)} and "
        f"{iso(y2, 10, 31)}."
    )
    return Draft(
        template_id="qr_extraction_boolean_brief",
        difficulty="extraction",
        messages=[question],
        gold=_plan(
            " OR ".join(tech.terms[:4]),
            "landscape",
            assignees=[org_a.name, org_b.name],
            jurisdictions=["US", "EP"],
            cpc_codes=codes,
            date_from=iso(y1, 4, 1),
            date_to=iso(y2, 10, 31),
        ),
        rubric=[
            criterion(
                "all_cpc_codes",
                f"Does filters.cpc_codes contain both {codes[0]!r} and "
                f"{codes[1]!r}, verbatim and with nothing added?",
            ),
            criterion(
                "both_assignees",
                f"Does filters.assignees contain both {org_a.name!r} and "
                f"{org_b.name!r}, spelled as given including the legal suffix?",
            ),
            criterion(
                "both_jurisdictions",
                "Does filters.jurisdictions contain exactly 'US' and 'EP'?",
            ),
            criterion(
                "date_range_exact",
                f"Is filters.date_from {iso(y1, 4, 1)} and filters.date_to "
                f"{iso(y2, 10, 31)}?",
            ),
            criterion(
                "query_has_no_filters",
                "Is the query string free of company names, CPC codes, "
                "jurisdiction codes and dates?",
            ),
        ],
        surface=(),  # a formal search brief is already realistic; leave it alone
    )


def qr_extraction_citation_lookup(rng: random.Random) -> Draft:
    tech = pick_tech(rng)
    code = tech.cpc[0]
    number = f"US{rng.randrange(10_000_000, 12_500_000)}B2"
    question = (
        f"Take {number} and give me everything it cites that also sits in "
        f"{code}. I only care about the {tech.field_phrase} references."
    )
    return Draft(
        template_id="qr_extraction_citation_lookup",
        difficulty="extraction",
        messages=[question],
        gold=_plan(
            f"{number} cited references: " + " OR ".join(tech.terms[:3]),
            "citation_lookup",
            cpc_codes=[code],
        ),
        rubric=[
            criterion(
                "intent_citation_lookup",
                "Is intent 'citation_lookup'?",
            ),
            criterion(
                "publication_number_kept",
                f"Does the query retain the publication number {number!r} "
                "verbatim? The schema has no field for it, so dropping it from "
                "the query loses the anchor of the whole search.",
            ),
            criterion(
                "cpc_filter",
                f"Is filters.cpc_codes [{code!r}]?",
            ),
            criterion(
                "no_invented_assignee",
                "Is filters.assignees empty? No applicant is named — inferring "
                "one from the publication number would be a guess.",
            ),
        ],
        surface=(
            SurfaceTarget(
                kind="message",
                index=0,
                style=_SEARCHER_STYLE,
                must_keep=(number, code),
            ),
        ),
    )


# --------------------------------------------------------------------------
# edge
# --------------------------------------------------------------------------


def qr_edge_unanchored_relative_date(rng: random.Random) -> Draft:
    tech = pick_tech(rng)
    org = _assignee(rng, tech)
    question = (
        f"Anything new from {org.name} on {tech.colloquial} in the last couple "
        f"of years?"
    )
    return Draft(
        template_id="qr_edge_unanchored_relative_date",
        difficulty="edge",
        messages=[question],
        gold=_plan(
            " OR ".join(tech.terms[:3]),
            "landscape",
            assignees=[org.name],
        ),
        rubric=[
            criterion(
                "no_invented_dates",
                "Are filters.date_from and filters.date_to BOTH null? 'the last "
                "couple of years' has no absolute anchor in the conversation, "
                "so any concrete date is fabricated.",
            ),
            criterion(
                "recency_not_lost",
                "Is the recency requirement still visible somewhere — in the "
                "query text or by leaving the date filters open — rather than "
                "silently discarded?",
            ),
            criterion(
                "assignee_lifted",
                f"Is {org.name!r} in filters.assignees?",
            ),
            criterion(
                "intent_landscape",
                "Is intent 'landscape'?",
            ),
        ],
        surface=(
            SurfaceTarget(
                kind="message",
                index=0,
                style=_SEARCHER_STYLE,
                must_keep=(org.name,),
                # The realism pass must not resolve the vagueness for us.
                forbid=(DATE_PATTERN, r"\b(?:19|20)\d{2}\b"),
            ),
        ),
    )


def qr_edge_ambiguous_europe(rng: random.Random) -> Draft:
    tech = pick_tech(rng)
    question = (
        f"I need the {tech.colloquial} filings in Europe. Whatever is out there."
    )
    return Draft(
        template_id="qr_edge_ambiguous_europe",
        difficulty="edge",
        messages=[question],
        gold=_plan(
            " OR ".join(tech.terms[:3]),
            "landscape",
            jurisdictions=["EP"],
        ),
        rubric=[
            criterion(
                "ep_only",
                "Is filters.jurisdictions exactly ['EP']? 'Europe' maps to the "
                "European Patent Office code that the schema supports.",
            ),
            criterion(
                "no_national_expansion",
                "Were national office codes such as DE, FR, GB, IT or ES NOT "
                "invented? The user said 'Europe', and expanding it to a "
                "specific set of national offices is a guess that silently "
                "changes the result set.",
            ),
            criterion(
                "no_other_filters",
                "Are filters.assignees, filters.cpc_codes and both date fields "
                "empty or null?",
            ),
            criterion(
                "intent_landscape",
                "Is intent 'landscape'?",
            ),
        ],
        surface=(
            SurfaceTarget(
                kind="message",
                index=0,
                style=_SEARCHER_STYLE,
                must_keep=("Europe",),
                # If the rewrite says "EPO" the ambiguity — the entire item —
                # disappears.
                forbid=(r"\bEPO\b", r"\bEP\b", DATE_PATTERN),
            ),
        ),
    )


def qr_edge_inventor_not_assignee(rng: random.Random) -> Draft:
    tech = pick_tech(rng)
    person = pick(
        rng,
        (
            "Hiroshi Nakamura",
            "Elena Vasquez",
            "Anja Lindqvist",
            "Rajesh Menon",
            "Wei-Chen Liu",
        ),
    )
    question = (
        f"Find the {tech.colloquial} work by {person} — that is the named "
        f"inventor, I do not know who it is assigned to."
    )
    return Draft(
        template_id="qr_edge_inventor_not_assignee",
        difficulty="edge",
        messages=[question],
        gold=_plan(
            f"{' OR '.join(tech.terms[:3])} inventor:{person}",
            "landscape",
        ),
        rubric=[
            criterion(
                "assignees_empty",
                f"Is filters.assignees empty? {person!r} is an inventor, not an "
                "organisation, and the user says outright that the assignee is "
                "unknown. Putting a person there would filter on the wrong field.",
            ),
            criterion(
                "inventor_preserved",
                f"Does the query string still carry {person!r}? The schema has "
                "no inventor filter, so the query is the only place the "
                "constraint can survive.",
            ),
            criterion(
                "no_invented_org",
                "Was no employer or institution guessed for this person "
                "anywhere in the output?",
            ),
            criterion(
                "subject_preserved",
                f"Does the query also cover {tech.colloquial!r}?",
            ),
        ],
        surface=(
            SurfaceTarget(
                kind="message",
                index=0,
                style=_SEARCHER_STYLE,
                must_keep=(person,),
            ),
        ),
    )


def qr_edge_two_questions_one_message(rng: random.Random) -> Draft:
    tech = pick_tech(rng)
    org = _assignee(rng, tech)
    question = (
        f"Two things: who owns the core {tech.colloquial} patents, and "
        f"separately, is {org.name} litigating in that space? Start with "
        f"ownership."
    )
    return Draft(
        template_id="qr_edge_two_questions_one_message",
        difficulty="edge",
        messages=[question],
        gold=_plan(
            " OR ".join(tech.terms[:3]),
            "ownership",
        ),
        rubric=[
            criterion(
                "intent_ownership",
                "Is intent 'ownership'? The user asked for two things and then "
                "said which one to start with.",
            ),
            criterion(
                "no_litigation_invention",
                "Did the output avoid inventing a filter for litigation? The "
                "schema cannot express it, and encoding it as a fake CPC code "
                "or assignee would corrupt the search.",
            ),
            criterion(
                "assignee_not_a_filter",
                f"Is {org.name!r} absent from filters.assignees? The company is "
                "named only in the deferred second question, so filtering the "
                "ownership search to it would answer the wrong question.",
            ),
            criterion(
                "single_plan",
                "Is exactly one query plan returned, rather than an attempt to "
                "answer both questions in one object?",
            ),
        ],
        surface=(
            SurfaceTarget(
                kind="message",
                index=0,
                style=_SEARCHER_STYLE,
                must_keep=(org.name,),
            ),
        ),
    )


TEMPLATES: tuple[Template, ...] = (
    Template("qr_simple_assignee_since", "simple", qr_simple_assignee_since),
    Template("qr_simple_prior_art_before", "simple", qr_simple_prior_art_before),
    Template("qr_simple_cpc_area", "simple", qr_simple_cpc_area),
    Template(
        "qr_multihop_same_company_new_angle",
        "multi_hop",
        qr_multihop_same_company_new_angle,
    ),
    Template(
        "qr_multihop_fto_from_description",
        "multi_hop",
        qr_multihop_fto_from_description,
    ),
    Template(
        "qr_multihop_competitor_switch", "multi_hop", qr_multihop_competitor_switch
    ),
    Template("qr_extraction_boolean_brief", "extraction", qr_extraction_boolean_brief),
    Template(
        "qr_extraction_citation_lookup", "extraction", qr_extraction_citation_lookup
    ),
    Template(
        "qr_edge_unanchored_relative_date", "edge", qr_edge_unanchored_relative_date
    ),
    Template("qr_edge_ambiguous_europe", "edge", qr_edge_ambiguous_europe),
    Template("qr_edge_inventor_not_assignee", "edge", qr_edge_inventor_not_assignee),
    Template(
        "qr_edge_two_questions_one_message", "edge", qr_edge_two_questions_one_message
    ),
)
