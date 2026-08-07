"""Feature Extractor templates: patent text -> structured features.

``PatentFeatures`` uses ``null`` for "not stated in the source", and that is the
whole design of this template set. An extractor that fills every field is not a
better extractor; it is one that guesses. So roughly a third of these items have
a deliberately absent field, each with a *tempting* wrong answer sitting right
next to it:

* a document with a publication date but no filing date (the wrong answer is
  one line above the right one, which is ``null``);
* an unassigned application naming individual inventors (the wrong answer is to
  promote an inventor to assignee);
* an excerpt of dependent claims only, where the independent claim count is not
  countable from what was supplied;
* a document that cites prior art *and names that prior art's assignee*, which
  is the single most common real extraction error in patent RAG.

The document is delivered as separate blocks — bibliographic header, abstract
and description, claims — because that is how a patent-text retriever hands
sections over, and because it lets the optional realism pass rewrite the prose
without ever touching the bibliographic literals or the claim numbering that
the gold answer depends on.
"""

from __future__ import annotations

import random

from amw.agents.schemas import PatentFeatures
from amw.datasets.patents_bank import ASSIGNEES, TECHS
from amw.datasets.templates.common import (
    DATE_PATTERN,
    Draft,
    SurfaceTarget,
    Template,
    article,
    bare,
    body,
    cap,
    criterion,
    iso,
    pick,
    pick_tech,
)

__all__ = ["TEMPLATES"]

_ABSTRACT_STYLE = (
    "a patent abstract: one paragraph, formal and impersonal, present tense, "
    "no headings and no bullet points, roughly 50 to 80 words; state only what "
    "is already stated and invent no numbers, dates, names or classifications"
)

_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

_OFFICE_HEADERS = {
    "US": "United States Patent Application Publication",
    "EP": "European Patent Application",
    "WO": "PCT International Publication",
    "JP": "Japan Patent Office - Published Unexamined Application (translation)",
}


def _org(rng: random.Random, tech):
    return ASSIGNEES[pick(rng, tech.assignees or tuple(ASSIGNEES))]


def _date(rng: random.Random, lo: int = 2018, hi: int = 2023) -> tuple[str, str]:
    """``(iso, long-form)`` for the same day, e.g. ``2021-03-14`` / ``14 March 2021``."""
    year = rng.randrange(lo, hi + 1)
    month = rng.randrange(1, 13)
    day = rng.randrange(1, 29)
    return iso(year, month, day), f"{day} {_MONTHS[month - 1]} {year}"


def _year_of(date_iso: str) -> int:
    return int(date_iso[:4])


def _priority(rng: random.Random, filed_iso: str) -> str:
    """A provisional priority date one year before the filing date.

    Drawing it independently produced priority dates *after* the filing they
    are the priority for, which is impossible and is exactly the kind of detail
    a patent searcher notices first.
    """
    year = _year_of(filed_iso) - 1
    month = rng.randrange(1, 13)
    day = rng.randrange(1, 29)
    return f"{day} {_MONTHS[month - 1]} {year}"


def _pub(rng: random.Random, office: str, year: int | None = None) -> str:
    """A publication number. ``year`` must be the *publication* year.

    US, WO and JP publication numbers embed the year they published, so it
    cannot be drawn independently of the filing date — a document published in
    2019 and filed in 2022 does not exist. Callers pass filing year + 1, the
    usual 18-month lag rounded to a whole year.
    """
    if office == "US":
        stamp = year if year is not None else rng.randrange(2018, 2025)
        return f"US {stamp}/{rng.randrange(100000, 399999)} A1"
    if office == "EP":
        # EP A-numbers carry no year, but they are a running serial, so the
        # number still implies a publication date: EP 3.2M is 2017 and EP 4.3M
        # is 2024. Drawing across the whole range put an EP 43xxxxx A1 on a 2018
        # filing, which only happens for a divisional. Roughly 145k numbers a
        # year, anchored at 2017.
        stamp = year if year is not None else rng.randrange(2019, 2025)
        base = 3_200_000 + (stamp - 2017) * 145_000
        return f"EP {base + rng.randrange(0, 145_000)} A1"
    if office == "WO":
        stamp = year if year is not None else rng.randrange(2019, 2025)
        return f"WO {stamp}/{rng.randrange(100000, 199999)} A1"
    stamp = year if year is not None else rng.randrange(2019, 2025)
    return f"JP {stamp}-{rng.randrange(100000, 199999)} A"


def _title(tech) -> str:
    """A front-page title.

    Patent titles are article-less noun phrases, and a title for a method claim
    does not also promise "method of manufacture thereof".
    """
    subject = bare(tech.claim_subject)
    if _is_method(subject):
        return cap(subject)
    if _is_article(subject):
        return f"{cap(subject)} and method of manufacture thereof"
    # Matter is produced, not manufactured; this is the usual title form in the
    # chemical and biotech arts.
    return f"{cap(subject)} and method for producing the same"


_PROCESS_WORDS = ("method", "process")

#: Words that end the head noun phrase. Everything after one of these modifies
#: the head rather than being it: "a membrane electrode assembly **for** water
#: electrolysis" is an assembly, "a lipid nanoparticle composition
#: **encapsulating** a messenger RNA" is a composition.
_NP_BOUNDARY = frozenset(
    {
        "of",
        "for",
        "in",
        "on",
        "to",
        "with",
        "from",
        "across",
        "between",
        "against",
        "comprising",
        "encapsulating",
        "containing",
        "including",
        "having",
        "storing",
        "using",
    }
)


def _is_method(subject: str) -> bool:
    return bare(subject).lower().startswith(_PROCESS_WORDS)


def _short_noun(subject: str) -> str:
    """What a dependent claim calls the thing it depends from.

    "The composition according to claim 1", not a verbatim repeat of a
    thirty-word preamble — repeating the preamble is legal but nobody drafts
    that way, and it was the most visible tell in an early draft. Takes the head
    of the subject's noun phrase, falling back to the whole preamble if that
    yields nothing.
    """
    stripped = bare(subject).replace(",", " ")
    words: list[str] = []
    for word in stripped.split():
        if word.lower() in _NP_BOUNDARY:
            break
        words.append(word)
    return words[-1] if words else stripped


#: CPC sections for data processing, computing and communications. A software
#: claim set — method, apparatus, computer-readable medium — is standard here
#: and nowhere else: nobody files a non-transitory medium claim on a flue-gas
#: scrubbing process.
_SOFTWARE_CPC = ("G06", "H04", "G16")

#: Subjects that are machines or articles. The rest are matter, and matter is
#: "prepared" rather than "manufactured".
_ARTICLE_HEADS = (
    "battery",
    "electrode",
    "device",
    "assembly",
    "pump",
    "system",
    "apparatus",
    "cell",
    "circuit",
    "module",
)


def _is_software(tech) -> bool:
    return tech.cpc[0].startswith(_SOFTWARE_CPC)


def _is_article(subject: str) -> bool:
    stripped = bare(subject).lower()
    return any(word in stripped for word in _ARTICLE_HEADS)


def _purpose(subject: str) -> str:
    """"a method of beam management in X" -> "beam management in X"."""
    stripped = bare(subject)
    for lead in ("method of ", "method for ", "process of ", "process for "):
        if stripped.lower().startswith(lead):
            return stripped[len(lead) :]
    return stripped


def max_independent(tech) -> int:
    """How many independent claims this subject can honestly support.

    A claim set is only as long as the statutory categories the invention
    actually occupies. A computer-implemented method supports the familiar
    method / apparatus / medium triad; a chemical process supports the process
    and a plant to run it, and nothing more. Generating a third claim anyway is
    what produced "A system comprising a lipid nanoparticle composition and a
    controller operatively coupled thereto" in an earlier draft.
    """
    if _is_method(tech.claim_subject):
        return 3 if _is_software(tech) else 2
    return 2


def _independent_claim(tech, index: int) -> tuple[str, str]:
    """``(claim text without its number, the noun its dependents refer back to)``.

    A second or third independent claim in the same application is normally a
    different statutory category covering the same invention, and which
    categories are available depends on the first: you cannot claim "a method
    of manufacturing a method". Each variant is phrased so the claim body fits
    its own preamble instead of being bolted on with a second "comprising".
    """
    subject = tech.claim_subject
    if index == 0:
        return f"{cap(subject)}, comprising {tech.claim_feature}", _short_noun(subject)
    if _is_method(subject):
        # Every method subject in the bank has a gerund claim_feature, so it
        # reads correctly after "the step of" — which is itself claim language,
        # not a workaround.
        if index == 1:
            return (
                f"An apparatus for {_purpose(subject)}, the apparatus comprising "
                f"a controller configured to perform the step of "
                f"{tech.claim_feature}",
                "apparatus",
            )
        return (
            f"A non-transitory computer-readable medium storing instructions "
            f"which, when executed by a processor, cause the processor to "
            f"perform the step of {tech.claim_feature}",
            "medium",
        )
    if _is_article(subject):
        return (
            f"A method of manufacturing {subject}, the method comprising "
            f"forming {tech.claim_feature}",
            "method",
        )
    return (
        f"A method of preparing {subject}, the method comprising providing "
        f"{tech.claim_feature}",
        "method",
    )


#: A metric only makes a usable claim limitation if its value opens with a
#: digit. "under 2%" and "epsilon = 2.1" do not survive being stated as one.
def _numeric_metrics(tech):
    return [m for m in tech.metrics if m.value[:1].isdigit()]


def _dependent_bodies(tech) -> list[str]:
    """Distinct dependent-claim limitations, in drafting order.

    Repeating a single refinement across claims 2, 3 and 4 is the thing that
    made the first draft of this dataset look generated: real dependent claims
    narrow different aspects. Performance limitations are phrased without a
    back-reference to the preamble so the same body works under any category.
    """
    bodies = [body(tech.claim_refinement)]
    for metric in _numeric_metrics(tech):
        context = f" {metric.context}" if metric.context else ""
        bodies.append(f"the {metric.name} is {metric.value}{context}")
    return bodies


def _claims_block(
    tech, independent: int, dependents_per: int = 2
) -> tuple[str, int, int]:
    """A numbered claim set with up to ``independent`` independent claims.

    Returns the block, the total claim count and the number of independent
    claims actually written — clamped by :func:`max_independent`, so a caller
    asking for three on a subject that only supports two gets a shorter claim
    set rather than an implausible one. The text and the gold count therefore
    come from the same place and cannot disagree.
    """
    lines: list[str] = []
    number = 0
    bodies = _dependent_bodies(tech)
    independent = min(independent, max_independent(tech))
    for i in range(independent):
        number += 1
        anchor = number
        claim, noun = _independent_claim(tech, i)
        lines.append(f"{number}. {claim}.")
        for j in range(dependents_per):
            number += 1
            lines.append(
                f"{number}. The {noun} according to claim {anchor}, "
                f"wherein {bodies[j % len(bodies)]}."
            )
    return "Claims:\n" + "\n".join(lines), number, independent


# --------------------------------------------------------------------------
# simple
# --------------------------------------------------------------------------


def fe_simple_full_front_page(rng: random.Random) -> Draft:
    tech = pick_tech(rng)
    org = _org(rng, tech)
    office = pick(rng, ("US", "EP"))
    filed_iso, filed_long = _date(rng)
    claims, _, _ = _claims_block(tech, independent=1)
    codes = list(tech.cpc[:2])

    header = (
        f"{_OFFICE_HEADERS[office]}\n"
        f"Publication No.: {_pub(rng, office, _year_of(filed_iso) + 1)}\n"
        f"Title: {_title(tech)}\n"
        f"Applicant: {org.name}\n"
        f"Filing Date: {filed_long}\n"
        f"Int. Cl. / CPC: {'; '.join(codes)}"
    )
    abstract = (
        f"Abstract: {cap(tech.claim_subject)} is disclosed. The "
        f"{bare(tech.claim_subject)} comprises {tech.claim_feature}. The "
        f"disclosed arrangement addresses {tech.problem}."
    )
    return Draft(
        template_id="fe_simple_full_front_page",
        difficulty="simple",
        messages=[header, abstract, claims],
        gold=PatentFeatures(
            title=_title(tech),
            assignee=org.name,
            filing_date=filed_iso,
            jurisdiction=office,
            cpc_codes=codes,
            technical_field=tech.field_phrase,
            independent_claim_count=1,
            novelty_statement=(
                f"{cap(tech.claim_subject)} comprising {tech.claim_feature}."
            ),
        ),
        rubric=[
            criterion(
                "date_normalised",
                f"Is filing_date {filed_iso!r}? The source writes it as "
                f"{filed_long!r} and it must be normalised to YYYY-MM-DD "
                "without shifting the day.",
            ),
            criterion(
                "assignee_verbatim",
                f"Is assignee {org.name!r}, including the legal suffix, not "
                "shortened to a trading name?",
            ),
            criterion(
                "cpc_complete",
                f"Does cpc_codes contain both {codes[0]!r} and {codes[1]!r} and "
                "nothing else?",
            ),
            criterion(
                "independent_count",
                "Is independent_claim_count 1? Claims 2 and 3 refer back to "
                "claim 1 and are dependent.",
            ),
            criterion(
                "jurisdiction_code",
                f"Is jurisdiction the two-letter code {office!r}, not the full "
                "office name?",
            ),
        ],
        surface=(
            SurfaceTarget(
                kind="message",
                index=1,
                style=_ABSTRACT_STYLE,
                must_keep=(tech.claim_subject,),
                forbid=(DATE_PATTERN,),
            ),
        ),
    )


def fe_simple_abstract_and_bib(rng: random.Random) -> Draft:
    tech = pick_tech(rng)
    org = _org(rng, tech)
    filed_iso, filed_long = _date(rng)
    code = tech.cpc[0]
    header = (
        f"{_OFFICE_HEADERS['WO']}\n"
        f"Publication No.: {_pub(rng, 'WO', _year_of(filed_iso) + 1)}\n"
        f"Title: {_title(tech)}\n"
        f"Applicant: {org.name}\n"
        f"International Filing Date: {filed_long}\n"
        f"CPC: {code}"
    )
    description = (
        f"Technical Field: The present disclosure relates to "
        f"{tech.field_phrase}.\n"
        f"Abstract: There is provided {tech.claim_subject} comprising "
        f"{tech.claim_feature}. The disclosure thereby addresses "
        f"{tech.problem}."
    )
    return Draft(
        template_id="fe_simple_abstract_and_bib",
        difficulty="simple",
        messages=[header, description],
        gold=PatentFeatures(
            title=_title(tech),
            assignee=org.name,
            filing_date=filed_iso,
            jurisdiction="WO",
            cpc_codes=[code],
            technical_field=tech.field_phrase,
            independent_claim_count=None,
            novelty_statement=(
                f"{cap(tech.claim_subject)} comprising {tech.claim_feature}, "
                f"addressing {tech.problem}."
            ),
        ),
        rubric=[
            criterion(
                "claim_count_null",
                "Is independent_claim_count null? No claims were supplied, so "
                "any number here is invented.",
            ),
            criterion(
                "jurisdiction_wo",
                "Is jurisdiction 'WO'? This is a PCT publication, not a "
                "national filing.",
            ),
            criterion(
                "technical_field_short",
                f"Is technical_field a short phrase equivalent to "
                f"{tech.field_phrase!r}, not a copy of the whole abstract?",
            ),
            criterion(
                "date_normalised",
                f"Is filing_date {filed_iso!r}?",
            ),
        ],
        surface=(
            SurfaceTarget(
                kind="message",
                index=1,
                style=_ABSTRACT_STYLE,
                must_keep=(tech.field_phrase,),
                forbid=(DATE_PATTERN,),
            ),
        ),
    )


# --------------------------------------------------------------------------
# multi-hop
# --------------------------------------------------------------------------


def fe_multihop_count_independent_claims(rng: random.Random) -> Draft:
    tech = pick_tech(rng)
    org = _org(rng, tech)
    office = pick(rng, ("US", "EP"))
    filed_iso, filed_long = _date(rng)
    claims, total, independent = _claims_block(tech, independent=pick(rng, (2, 3)))
    header = (
        f"{_OFFICE_HEADERS[office]}\n"
        f"Publication No.: {_pub(rng, office, _year_of(filed_iso) + 1)}\n"
        f"Title: {_title(tech)}\n"
        f"Applicant: {org.name}\n"
        f"Filing Date: {filed_long}\n"
        f"CPC: {tech.cpc[0]}"
    )
    return Draft(
        template_id="fe_multihop_count_independent_claims",
        difficulty="multi_hop",
        messages=[header, claims],
        gold=PatentFeatures(
            title=_title(tech),
            assignee=org.name,
            filing_date=filed_iso,
            jurisdiction=office,
            cpc_codes=[tech.cpc[0]],
            technical_field=tech.field_phrase,
            independent_claim_count=independent,
            novelty_statement=(
                f"{cap(tech.claim_subject)} comprising {tech.claim_feature}."
            ),
        ),
        rubric=[
            criterion(
                "independent_count_exact",
                f"Is independent_claim_count {independent}? Of the {total} "
                "claims, only those that do not refer back to another claim are "
                "independent.",
            ),
            criterion(
                "not_total_claims",
                f"Is independent_claim_count NOT {total}, the total number of "
                "claims? That is the obvious wrong answer here.",
            ),
            criterion(
                "technical_field_inferred",
                f"Is technical_field a short phrase equivalent to "
                f"{tech.field_phrase!r}, derived from the claim subject matter?",
            ),
            criterion(
                "bibliographic_correct",
                f"Are assignee {org.name!r}, filing_date {filed_iso!r} and "
                f"jurisdiction {office!r} all correct?",
            ),
        ],
        surface=(),  # claim numbering is the item; nothing here may be rewritten
    )


def fe_multihop_assignee_not_the_citation(rng: random.Random) -> Draft:
    tech = pick_tech(rng)
    org = _org(rng, tech)
    # Prior art in the same field comes from someone who works in that field.
    # Drawing the cited owner from the whole roster produced citations like a
    # battery maker owning a microLED transfer patent.
    field_peers = [
        k for k in (tech.assignees or tuple(ASSIGNEES)) if ASSIGNEES[k].name != org.name
    ]
    cited_key = pick(
        rng,
        field_peers or [k for k in ASSIGNEES if ASSIGNEES[k].name != org.name],
    )
    cited = ASSIGNEES[cited_key]
    office = pick(rng, ("US", "EP"))
    filed_iso, filed_long = _date(rng)
    cited_number = f"US{rng.randrange(9_000_000, 10_400_000)}B2"

    header = (
        f"{_OFFICE_HEADERS[office]}\n"
        f"Publication No.: {_pub(rng, office, _year_of(filed_iso) + 1)}\n"
        f"Title: {_title(tech)}\n"
        f"Applicant: {org.name}\n"
        f"Filing Date: {filed_long}\n"
        f"CPC: {tech.cpc[0]}"
    )
    background = (
        f"Background: {cited_number} (assigned to {cited.name}) describes "
        f"{tech.claim_subject} in which a conventional arrangement is used. "
        f"That approach does not resolve {tech.problem}. The present "
        f"application instead relies on {tech.claim_feature}."
    )
    return Draft(
        template_id="fe_multihop_assignee_not_the_citation",
        difficulty="multi_hop",
        messages=[header, background],
        gold=PatentFeatures(
            title=_title(tech),
            assignee=org.name,
            filing_date=filed_iso,
            jurisdiction=office,
            cpc_codes=[tech.cpc[0]],
            technical_field=tech.field_phrase,
            independent_claim_count=None,
            novelty_statement=(
                f"Unlike the cited conventional arrangement, the application "
                f"relies on {tech.claim_feature}."
            ),
        ),
        rubric=[
            criterion(
                "assignee_is_applicant",
                f"Is assignee {org.name!r}, the applicant named in the header?",
            ),
            criterion(
                "cited_assignee_rejected",
                f"Is {cited.name!r} absent from the assignee field? It is the "
                f"owner of the cited prior-art document {cited_number}, not of "
                "this application.",
            ),
            criterion(
                "novelty_is_the_application",
                "Does novelty_statement describe what the present application "
                "provides, rather than what the cited document describes?",
            ),
            criterion(
                "claim_count_null",
                "Is independent_claim_count null? No claims were supplied.",
            ),
        ],
        surface=(
            SurfaceTarget(
                kind="message",
                index=1,
                style=(
                    "the background section of a patent specification: formal, "
                    "impersonal, two or three sentences, no headings"
                ),
                must_keep=(cited.name, cited_number),
                forbid=(DATE_PATTERN,),
            ),
        ),
    )


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------


def fe_extraction_dense_bibliographic(rng: random.Random) -> Draft:
    tech = pick_tech(rng)
    org = _org(rng, tech)
    office = pick(rng, ("US", "EP", "JP"))
    filed_iso, filed_long = _date(rng)
    codes = list(tech.cpc)
    # Semicolons, not commas: these are "Surname, Forename" pairs, and a comma
    # join turns three inventors into six unparseable names.
    inventors = "; ".join(
        rng.sample(
            [
                "Nakamura, Hiroshi",
                "Vasquez, Elena",
                "Lindqvist, Anja",
                "Menon, Rajesh",
                "Liu, Wei-Chen",
                "Okonkwo, Chidi",
            ],
            3,
        )
    )
    header = (
        f"{_OFFICE_HEADERS[office]}\n"
        f"Publication No.: {_pub(rng, office, _year_of(filed_iso) + 1)}\n"
        f"Title: {_title(tech)}\n"
        f"Applicant: {org.name}\n"
        f"Inventors: {inventors}\n"
        f"Filing Date: {filed_long}\n"
        f"Priority: {_priority(rng, filed_iso)} (provisional)\n"
        f"CPC: {'; '.join(codes)}\n"
        f"Field of Search: {codes[0]}, {codes[1]}"
    )
    claims, _, _ = _claims_block(tech, independent=1, dependents_per=3)
    return Draft(
        template_id="fe_extraction_dense_bibliographic",
        difficulty="extraction",
        messages=[header, claims],
        gold=PatentFeatures(
            title=_title(tech),
            assignee=org.name,
            filing_date=filed_iso,
            jurisdiction=office,
            cpc_codes=codes,
            technical_field=tech.field_phrase,
            independent_claim_count=1,
            novelty_statement=(
                f"{cap(tech.claim_subject)} comprising {tech.claim_feature}."
            ),
        ),
        rubric=[
            criterion(
                "all_cpc_codes",
                f"Does cpc_codes contain all {len(codes)} codes "
                f"({', '.join(codes)}), verbatim, with no duplicates from the "
                "Field of Search line?",
            ),
            criterion(
                "filing_not_priority",
                f"Is filing_date {filed_iso!r} — the Filing Date line — and not "
                "the earlier provisional priority date?",
            ),
            criterion(
                "inventors_not_assignee",
                f"Is assignee {org.name!r} rather than any of the named "
                "inventors?",
            ),
            criterion(
                "jurisdiction_code",
                f"Is jurisdiction the two-letter code {office!r}?",
            ),
            criterion(
                "independent_count",
                "Is independent_claim_count 1?",
            ),
        ],
        surface=(),  # the whole item is literal extraction from fixed text
    )


# --------------------------------------------------------------------------
# edge
# --------------------------------------------------------------------------


def fe_edge_missing_filing_date(rng: random.Random) -> Draft:
    tech = pick_tech(rng)
    org = _org(rng, tech)
    office = pick(rng, ("US", "EP"))
    published_iso, published_long = _date(rng, 2020, 2024)
    header = (
        f"{_OFFICE_HEADERS[office]}\n"
        f"Publication No.: {_pub(rng, office, _year_of(published_iso))}\n"
        f"Publication Date: {published_long}\n"
        f"Title: {_title(tech)}\n"
        f"Applicant: {org.name}\n"
        f"CPC: {tech.cpc[0]}"
    )
    abstract = (
        f"Abstract: {cap(tech.claim_subject)} is provided, comprising "
        f"{tech.claim_feature}, thereby addressing {tech.problem}."
    )
    return Draft(
        template_id="fe_edge_missing_filing_date",
        difficulty="edge",
        messages=[header, abstract],
        gold=PatentFeatures(
            title=_title(tech),
            assignee=org.name,
            filing_date=None,
            jurisdiction=office,
            cpc_codes=[tech.cpc[0]],
            technical_field=tech.field_phrase,
            independent_claim_count=None,
            novelty_statement=(
                f"{cap(tech.claim_subject)} comprising {tech.claim_feature}."
            ),
        ),
        rubric=[
            criterion(
                "filing_date_null",
                "Is filing_date null? The document states a Publication Date "
                "only; no filing date appears anywhere in the source.",
            ),
            criterion(
                "publication_date_not_substituted",
                f"Is {published_iso!r} — the publication date — absent from "
                "filing_date? Substituting it is the specific error this item "
                "is looking for.",
            ),
            criterion(
                "other_fields_present",
                f"Are title, assignee ({org.name!r}), jurisdiction ({office!r}) "
                "and cpc_codes still extracted? Abstaining on one field must "
                "not cause the others to be dropped.",
            ),
            criterion(
                "claim_count_null",
                "Is independent_claim_count null? No claims were supplied.",
            ),
        ],
        surface=(
            SurfaceTarget(
                kind="message",
                index=1,
                style=_ABSTRACT_STYLE,
                must_keep=(tech.claim_subject,),
                forbid=(DATE_PATTERN, r"\bfil(?:ed|ing)\b"),
            ),
        ),
    )


def fe_edge_unassigned_individual_inventors(rng: random.Random) -> Draft:
    tech = pick_tech(rng)
    filed_iso, filed_long = _date(rng)
    inventors = rng.sample(
        ["Elena Vasquez", "Rajesh Menon", "Anja Lindqvist", "Chidi Okonkwo"], 2
    )
    header = (
        f"{_OFFICE_HEADERS['US']}\n"
        f"Publication No.: {_pub(rng, 'US', _year_of(filed_iso) + 1)}\n"
        f"Title: {_title(tech)}\n"
        f"Inventors: {inventors[0]}; {inventors[1]}\n"
        f"Applicant: (none recorded)\n"
        f"Filing Date: {filed_long}\n"
        f"CPC: {tech.cpc[0]}"
    )
    abstract = (
        f"Abstract: Disclosed is {tech.claim_subject} comprising "
        f"{tech.claim_feature}."
    )
    return Draft(
        template_id="fe_edge_unassigned_individual_inventors",
        difficulty="edge",
        messages=[header, abstract],
        gold=PatentFeatures(
            title=_title(tech),
            assignee=None,
            filing_date=filed_iso,
            jurisdiction="US",
            cpc_codes=[tech.cpc[0]],
            technical_field=tech.field_phrase,
            independent_claim_count=None,
            novelty_statement=(
                f"{cap(tech.claim_subject)} comprising {tech.claim_feature}."
            ),
        ),
        rubric=[
            criterion(
                "assignee_null",
                "Is assignee null? The applicant line reads '(none recorded)' — "
                "the application is unassigned.",
            ),
            criterion(
                "inventor_not_promoted",
                f"Are the named inventors ({inventors[0]}, {inventors[1]}) "
                "absent from the assignee field? An inventor is not an assignee.",
            ),
            criterion(
                "no_employer_guessed",
                "Was no institution or company inferred for these individuals?",
            ),
            criterion(
                "filing_date_correct",
                f"Is filing_date {filed_iso!r}? This field IS stated and must "
                "not be dropped along with the assignee.",
            ),
        ],
        surface=(
            SurfaceTarget(
                kind="message",
                index=1,
                style=_ABSTRACT_STYLE,
                must_keep=(tech.claim_subject,),
                forbid=(DATE_PATTERN, r"\bassign", r"\bInc\.|\bLtd|\bGmbH|\bCorp"),
            ),
        ),
    )


def fe_edge_description_fragment_only(rng: random.Random) -> Draft:
    tech = pick_tech(rng)
    metric = tech.metrics[0]
    fragment = (
        f"[0043] In a preferred embodiment, there is provided "
        f"{tech.claim_subject} comprising {tech.claim_feature}. It has been "
        f"found that this arrangement yields {article(metric.name)} "
        f"{metric.phrase()}, which is "
        f"sufficient to address {tech.problem}.\n"
        f"[0044] Other embodiments will be apparent to those skilled in the art "
        f"from consideration of the specification and practice of the "
        f"embodiments disclosed herein."
    )
    return Draft(
        template_id="fe_edge_description_fragment_only",
        difficulty="edge",
        messages=[fragment],
        gold=PatentFeatures(
            title=None,
            assignee=None,
            filing_date=None,
            jurisdiction=None,
            cpc_codes=[],
            technical_field=tech.field_phrase,
            independent_claim_count=None,
            novelty_statement=(
                f"An embodiment providing {tech.claim_subject} comprising "
                f"{tech.claim_feature}, yielding {article(metric.name)} "
                f"{metric.phrase()}."
            ),
        ),
        rubric=[
            criterion(
                "bibliographic_all_null",
                "Are title, assignee, filing_date and jurisdiction all null, "
                "and cpc_codes empty? This is two description paragraphs with "
                "no front page.",
            ),
            criterion(
                "no_cpc_inferred",
                "Was no CPC code inferred from the subject matter? "
                "Classification is assigned by an examiner and is not derivable "
                "from the text.",
            ),
            criterion(
                "technical_field_still_extracted",
                f"Is technical_field still populated with a phrase equivalent "
                f"to {tech.field_phrase!r}? It IS derivable from this text, so "
                "leaving it null is over-abstention.",
            ),
            criterion(
                "novelty_from_fragment",
                f"Does novelty_statement reflect the embodiment described, "
                f"including {metric.value!r} if a value is given?",
            ),
        ],
        surface=(
            SurfaceTarget(
                kind="message",
                index=0,
                style=(
                    "two numbered paragraphs of a patent description, keeping "
                    "the [0043] and [0044] paragraph numbers exactly where they "
                    "are; formal, impersonal; add no bibliographic information"
                ),
                must_keep=("[0043]", "[0044]", metric.value),
                forbid=(DATE_PATTERN, r"Applicant", r"\bCPC\b", r"\bTitle\b"),
            ),
        ),
    )


def fe_edge_dependent_claims_only(rng: random.Random) -> Draft:
    tech = pick_tech(rng)
    org = _org(rng, tech)
    office = pick(rng, ("US", "EP"))
    filed_iso, filed_long = _date(rng)
    start = pick(rng, (4, 5, 6))
    noun = _short_noun(tech.claim_subject)
    bodies = _dependent_bodies(tech)
    lines = [
        f"{start + i}. The {noun} according to claim {1 if i % 2 == 0 else 2}, "
        f"wherein {bodies[i % len(bodies)]}."
        for i in range(3)
    ]
    header = (
        f"{_OFFICE_HEADERS[office]}\n"
        f"Publication No.: {_pub(rng, office, _year_of(filed_iso) + 1)}\n"
        f"Title: {_title(tech)}\n"
        f"Applicant: {org.name}\n"
        f"Filing Date: {filed_long}\n"
        f"CPC: {tech.cpc[0]}"
    )
    claims = (
        f"Claims (excerpt, claims {start}-{start + 2} of the granted set):\n"
        + "\n".join(lines)
    )
    return Draft(
        template_id="fe_edge_dependent_claims_only",
        difficulty="edge",
        messages=[header, claims],
        gold=PatentFeatures(
            title=_title(tech),
            assignee=org.name,
            filing_date=filed_iso,
            jurisdiction=office,
            cpc_codes=[tech.cpc[0]],
            technical_field=tech.field_phrase,
            independent_claim_count=None,
            novelty_statement=None,
        ),
        rubric=[
            criterion(
                "claim_count_null",
                "Is independent_claim_count null? The excerpt contains only "
                "dependent claims; claims 1 and 2 are referred to but not "
                "supplied, so the count is not determinable from this source.",
            ),
            criterion(
                "count_not_guessed",
                "Is independent_claim_count neither 2 (guessed from the two "
                "claim numbers referred back to) nor 3 (the number of claims "
                "shown)?",
            ),
            criterion(
                "novelty_null_or_hedged",
                "Is novelty_statement null, or explicitly limited to the "
                "dependent refinement that was actually supplied? The "
                "independent claims are not in the source.",
            ),
            criterion(
                "header_fields_extracted",
                f"Are title, assignee ({org.name!r}), filing_date "
                f"({filed_iso!r}) and jurisdiction ({office!r}) all extracted "
                "from the header?",
            ),
        ],
        surface=(),
    )


TEMPLATES: tuple[Template, ...] = (
    Template("fe_simple_full_front_page", "simple", fe_simple_full_front_page),
    Template("fe_simple_abstract_and_bib", "simple", fe_simple_abstract_and_bib),
    Template(
        "fe_multihop_count_independent_claims",
        "multi_hop",
        fe_multihop_count_independent_claims,
    ),
    Template(
        "fe_multihop_assignee_not_the_citation",
        "multi_hop",
        fe_multihop_assignee_not_the_citation,
    ),
    Template(
        "fe_extraction_dense_bibliographic",
        "extraction",
        fe_extraction_dense_bibliographic,
    ),
    Template("fe_edge_missing_filing_date", "edge", fe_edge_missing_filing_date),
    Template(
        "fe_edge_unassigned_individual_inventors",
        "edge",
        fe_edge_unassigned_individual_inventors,
    ),
    Template(
        "fe_edge_description_fragment_only",
        "edge",
        fe_edge_description_fragment_only,
    ),
    Template("fe_edge_dependent_claims_only", "edge", fe_edge_dependent_claims_only),
)
