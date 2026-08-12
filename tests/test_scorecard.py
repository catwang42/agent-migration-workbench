"""T12 — the rendered Migration Readiness Scorecard.

Four structural rules govern this report, and each is tested here against the
*rendered Markdown* rather than against the objects behind it. That is
deliberate: a caveat that exists on a model but not in the cell a customer
reads is not a caveat.

(a) Claude's ``json_schema_validity`` number and its mechanism caveat cannot be
    rendered apart — they occupy one table cell.
(b) The latency cell renders exactly ``not comparable — region split
    disclosed`` unless a same-region probe supplies the measurement, and the
    ``latency_p95`` gate is *not evaluated* rather than passed.
(c) Every judge score carries its judged n and split; every cost and savings
    cell is an em dash while ``pricing.yaml`` is unverified — never a zero,
    never a placeholder number.
(d) The footer carries the taxonomy line verbatim plus the ground-rule-2 set:
    provenance, seed, judge model and prompt version, pricing ``verified_on``
    and sources, region(s), run date, recording window, gates version hash.

The integration test renders the real ``phase2_n70.json`` offline, so the rules
are checked against the artifact that will actually be shown.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from amw.config import AppConfig, ConfigError, load_all
from amw.eval.runner import Phase2Result
from amw.eval.stats import Estimate
from amw.reporting.cells import (
    CLAUDE_SCHEMA_CAVEAT,
    EM_DASH,
    FAIL_IMPRECISE,
    FAIL_REGRESSION,
    NOT_MEASURED,
    REGION_SPLIT_DISCLOSURE,
    ClaudeSchemaValidityCell,
    JudgeScoreCell,
    cost_cell,
    delta_failure_kind,
    latency_cell,
)
from amw.reporting.evidence import (
    BASELINE_VARIANT,
    CANDIDATE_VARIANT,
    GATE_COST,
    GATE_LATENCY,
    GATE_SCHEMA,
    Regions,
    SameRegionLatencyProbe,
    SubagentEvidence,
    build_evidence,
    collect_samples,
)
from amw.reporting.scorecard import (
    ALT_EVALUATORS,
    INCOMPLETE,
    PARITY_SENTENCE,
    TAXONOMY_LINE,
    VerdictRules,
    apply_alt_clause,
    build_scorecard,
    decide_verdict,
    load_adjudications,
    parse_volume,
    render_markdown,
)
from amw.shadow.triage import MALFORMED_CAVEAT, TriageSummary

ARTIFACT = (
    Path(__file__).resolve().parents[1] / "artifacts" / "results" / "phase2_n70.json"
)


@pytest.fixture(scope="module")
def cfg() -> AppConfig:
    return load_all()


@pytest.fixture(scope="module")
def phase2() -> Phase2Result:
    if not ARTIFACT.is_file():
        pytest.skip(f"no phase-2 artifact at {ARTIFACT}")
    return Phase2Result.model_validate_json(ARTIFACT.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def report(cfg: AppConfig, phase2: Phase2Result) -> str:
    """The real scorecard, rendered offline.

    ``build_evidence`` is called without per-item samples, so the two paired
    delta gates come out unmeasured — which keeps this test fast and, more
    usefully, exercises the "gate absent" path on real data.
    """
    evidence = build_evidence(
        cfg,
        phase2,
        regions=Regions(baseline="global", candidate="us-central1", source="test"),
    )
    return render_markdown(build_scorecard(cfg, phase2, evidence=evidence))


@pytest.fixture(scope="module")
def report_with_deltas(cfg: AppConfig, phase2: Phase2Result) -> str:
    """The scorecard *with* the paired-delta gates measured.

    The ``report`` fixture above deliberately skips per-item samples to
    exercise the gate-absent path. The failure-kind wording only exists on a
    measured failing delta, so it needs the other path: samples recovered from
    the replay store, exactly as ``cli.py scorecard`` recovers them.
    """
    samples = collect_samples(
        cfg,
        phase2,
        mode="replay",
        arms=[
            (arm.subagent, arm.variant)
            for arm in phase2.arms
            if arm.variant in (BASELINE_VARIANT, CANDIDATE_VARIANT)
        ],
    )
    evidence = build_evidence(
        cfg,
        phase2,
        regions=Regions(baseline="global", candidate="us-central1", source="test"),
        samples=samples,
    )
    return render_markdown(build_scorecard(cfg, phase2, evidence=evidence))


def _cells(markdown: str) -> list[str]:
    """Every table cell in the report, as a flat list."""
    out: list[str] = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        out.extend(part.strip() for part in stripped.strip("|").split("|"))
    return out


# --------------------------------------------------------------------------
# (a) the schema-validity caveat is welded to the number
# --------------------------------------------------------------------------


def test_the_claude_schema_cell_has_no_way_to_render_the_number_alone() -> None:
    cell = ClaudeSchemaValidityCell(
        estimate=Estimate(metric=GATE_SCHEMA, point=0.814, lo=0.72, hi=0.9, n=70)
    )
    assert CLAUDE_SCHEMA_CAVEAT in cell.render()
    # Every string form of the cell carries it, including an accidental f-string.
    assert CLAUDE_SCHEMA_CAVEAT in f"{cell}"
    assert CLAUDE_SCHEMA_CAVEAT in str(cell)
    # And the type defines no method of its own that hands back a bare
    # formatted number — `render` is the only way out, and it is unbranched.
    own = {
        name
        for name, value in vars(ClaudeSchemaValidityCell).items()
        if callable(value) and not name.startswith("_")
    }
    assert own == {"render"}


def test_a_cell_with_nothing_to_caveat_is_refused() -> None:
    with pytest.raises(ValueError, match="nothing to caveat"):
        ClaudeSchemaValidityCell()


def test_rendered_claude_schema_number_and_caveat_share_one_cell(report: str) -> None:
    """The rule as a customer experiences it: you cannot copy the number out of
    the table without copying the caveat."""
    matching = [c for c in _cells(report) if CLAUDE_SCHEMA_CAVEAT in c]
    assert matching, "no Claude schema-validity cell was rendered at all"
    for cell in matching:
        number = re.search(r"\d\.\d{3}", cell)
        assert number, f"caveat rendered without a measurement: {cell!r}"
        assert cell.index(number.group(0)) < cell.index(CLAUDE_SCHEMA_CAVEAT)


def test_every_rendered_baseline_schema_number_is_caveated(
    cfg: AppConfig, phase2: Phase2Result, report: str
) -> None:
    """Scan the artifact for Claude's measured schema rates and assert each one,
    wherever it surfaces in the report, sits in a cell carrying the caveat."""
    seen = 0
    for arm in phase2.arms:
        if arm.variant != "claude_baseline" or GATE_SCHEMA not in arm.metrics:
            continue
        point = arm.metrics[GATE_SCHEMA].point
        if point is None:
            continue
        rendered = f"{point:.3f}"
        for cell in _cells(report):
            if rendered in cell and CLAUDE_SCHEMA_CAVEAT not in cell:
                # A Gemini cell may legitimately show the same rounded value.
                assert "1.000" == rendered, f"uncaveated Claude schema cell: {cell!r}"
            if rendered in cell and CLAUDE_SCHEMA_CAVEAT in cell:
                seen += 1
    assert seen >= 1


def test_the_caveat_is_scoped_to_this_org_not_to_the_model() -> None:
    # notes/org_policy_structured_outputs.md: this is an environment finding.
    # Generalising it would itself be a fabricated result.
    assert "this org's policy" in CLAUDE_SCHEMA_CAVEAT
    assert "not the model's ceiling" in CLAUDE_SCHEMA_CAVEAT


# --------------------------------------------------------------------------
# (b) latency for this measurement window
# --------------------------------------------------------------------------


def test_latency_cell_is_exactly_the_disclosure_string() -> None:
    estimate = Estimate(metric=GATE_LATENCY, point=800, lo=700, hi=900, n=70, unit="ms")
    cell = latency_cell(estimate, same_region_probe=False, candidate_region="us-central1")
    assert cell == REGION_SPLIT_DISCLOSURE
    assert not re.search(r"\d", cell), "no digit may appear in the disclosure cell"


def test_latency_cell_needs_both_a_probe_and_a_measurement() -> None:
    assert latency_cell(None, same_region_probe=True) == REGION_SPLIT_DISCLOSURE


def test_a_same_region_probe_renders_the_number(cfg: AppConfig) -> None:
    estimate = Estimate(metric=GATE_LATENCY, point=800, lo=700, hi=900, n=70, unit="ms")
    cell = latency_cell(estimate, same_region_probe=True, candidate_region="us-central1")
    assert "same-region probe" in cell and "800" in cell


def test_cross_region_run_leaves_the_latency_gate_unevaluated(report: str) -> None:
    assert REGION_SPLIT_DISCLOSURE in report
    for line in report.splitlines():
        if f"`{GATE_LATENCY}`" in line and line.strip().startswith("|"):
            assert "not evaluated" in line
            assert "PASS" not in line


def test_the_probe_override_produces_an_evaluated_gate(
    cfg: AppConfig, phase2: Phase2Result
) -> None:
    """The escape hatch exists and is explicit: a same-region probe, supplied by
    the caller, is the only thing that unlocks the gate."""
    probe = SameRegionLatencyProbe(
        region="us-central1",
        candidate_p95=Estimate(
            metric=GATE_LATENCY, point=800, lo=700, hi=900, n=70, unit="ms"
        ),
        baseline_p95_ms=1200.0,
        baseline_region="us-central1",
        candidate_region="us-central1",
        probed_on="2026-08-12",
    )
    subagent = sorted({arm.subagent for arm in phase2.arms})[0]
    evidence = build_evidence(
        cfg,
        phase2,
        latency_probes={subagent: probe},
        subagents=[subagent],
        regions=Regions(baseline="us-central1", candidate="us-central1", source="probe"),
    )
    card = build_scorecard(cfg, phase2, evidence=evidence)
    verdict = card.verdicts[subagent]
    assert GATE_LATENCY in verdict.checks
    assert verdict.checks[GATE_LATENCY].passed is True
    markdown = render_markdown(card)
    assert REGION_SPLIT_DISCLOSURE not in markdown
    assert "same-region probe" in markdown


# --------------------------------------------------------------------------
# (c) judged-n labels, and cost cells that stay em dashes
# --------------------------------------------------------------------------


def test_a_judge_score_cannot_be_built_without_its_n_and_split() -> None:
    with pytest.raises(ValueError):
        JudgeScoreCell(estimate=None, point=0.9)  # type: ignore[call-arg]


def test_judge_cell_renders_n_and_split() -> None:
    cell = JudgeScoreCell(split="core", items_scored=28, point=0.91)
    assert "judged n=28" in cell.render() and "split=core" in cell.render()
    assert "judged n=28" in f"{cell}"


def test_every_rendered_judge_score_states_its_n_and_split(report: str) -> None:
    rows = [
        line
        for line in report.splitlines()
        if line.strip().startswith("| Judge score")
    ]
    assert rows, "the evidence table rendered no judge score at all"
    for row in rows:
        value = row.strip().strip("|").split("|")[1].strip()
        if value == NOT_MEASURED:
            continue
        assert "judged n=" in value and "split=" in value, row


def test_the_registered_split_deviation_is_visible(
    cfg: AppConfig, phase2: Phase2Result, report: str
) -> None:
    """QR and CS were judged on the 28-item core split, FE on the full 70. The
    report has to show both, or the two scores read as one instrument."""
    splits = {
        arm.judge.split for arm in phase2.arms if arm.judge is not None
    }
    assert len(splits) > 1, "artifact no longer carries the registered deviation"
    for split in splits:
        assert f"split={split}" in report


def test_a_paired_delta_states_how_many_pairs_it_used() -> None:
    """Arms can score different numbers of items — an errored call scores
    nothing — so a delta's pair count and its dropped items travel with it."""
    from amw.reporting.cells import paired_delta_text

    delta = Estimate(
        metric="quality_delta_pp",
        point=1.34,
        lo=-2.68,
        hi=6.25,
        n=68,
        unit="percentage_points",
        method="paired_percentile_bootstrap",
        paired_n=68,
        dropped_unpaired=2,
    )
    text = paired_delta_text(delta)
    assert "paired n=68" in text and "2 unpaired dropped" in text
    # An unpaired estimate is left alone.
    plain = delta.model_copy(update={"method": "percentile_bootstrap"})
    assert "paired n=" not in paired_delta_text(plain)


def test_cost_cells_are_em_dashes_with_no_digits(report: str) -> None:
    cost = cost_cell(prices_verified=False)
    assert cost.startswith(EM_DASH)
    assert not re.search(r"\d", cost), "a reason with a number in it is a number"
    for label in ("Cost per call", "Monthly run rate", "Annual run rate",
                  "Cost savings vs Claude"):
        rows = [l for l in report.splitlines() if l.strip().startswith(f"| {label} |")]
        assert rows, f"no {label} row rendered"
        for row in rows:
            value = row.strip().strip("|").split("|")[1].strip()
            assert value == cost, row


def test_the_cost_gate_is_unevaluated_not_zero(report: str) -> None:
    rows = [
        line
        for line in report.splitlines()
        if f"| `{GATE_COST}` |" in line
    ]
    assert rows
    for row in rows:
        cells = [c.strip() for c in row.strip().strip("|").split("|")]
        measured, tested, result = cells[2], cells[3], cells[4]
        # The measured cell is the em-dash cost cell — no savings percentage,
        # not even a zero, is invented to fill it.
        assert measured == cost_cell(prices_verified=False)
        assert tested == result == "not evaluated"


def test_the_economics_section_refuses_rather_than_zeroes(report: str) -> None:
    assert "not computable" in report
    assert "pricing unverified" in report and "volumes unconfirmed" in report
    assert "volumes: illustrative" in report
    assert "$0" not in report


# --------------------------------------------------------------------------
# (d) the footer
# --------------------------------------------------------------------------


def test_footer_carries_the_taxonomy_line_verbatim(report: str) -> None:
    assert TAXONOMY_LINE in report
    assert "receive no verdict today" in TAXONOMY_LINE


def test_footer_carries_the_ground_rule_two_set(
    cfg: AppConfig, phase2: Phase2Result, report: str
) -> None:
    required = [
        f"dataset seed `{cfg.provenance_footer()['seed']}`",  # provenance + seed
        f"{phase2.judge_model}",                              # judge model
        f"prompt `{phase2.judge_prompt_version}`",            # judge prompt version
        "| Prices verified on |",                             # pricing verified_on
        cfg.pricing.sources[0],                               # pricing sources
        "| Region(s) |",                                      # region(s)
        "| Run date |",                                       # run date
        "| Recording window |",                               # recording window
        f"hash `{cfg.gates.version_hash}`",                   # gates version hash
        cfg.customer.provenance,                              # provenance label
    ]
    for fragment in required:
        assert str(fragment) in report, f"footer is missing {fragment!r}"


def test_the_recording_window_is_stated_not_just_the_run_date(
    phase2: Phase2Result, report: str
) -> None:
    assert phase2.recorded_from and phase2.recorded_to
    assert str(phase2.recorded_from) in report
    assert str(phase2.recorded_to) in report
    assert "recorded" in report.lower()


def test_both_regions_and_where_they_came_from_are_printed(report: str) -> None:
    assert "Claude `global`" in report
    assert "us-central1" in report


def test_unverified_prices_are_named_as_such(cfg: AppConfig, report: str) -> None:
    assert "UNVERIFIED" in report
    assert str(len(cfg.pricing.unverified_keys())) in report
    assert "VERIFY" in report


# --------------------------------------------------------------------------
# parity language and overall shape
# --------------------------------------------------------------------------


def test_parity_language_is_the_pre_agreed_wording(report: str) -> None:
    assert PARITY_SENTENCE in report
    assert "quality parity within measurement under pre-agreed gates" in report
    # "zero quality drop" appears exactly once, inside the sentence that
    # forbids it — and nowhere as a claim.
    assert report.count("zero quality drop") == 1
    assert 'never "zero quality drop"' in report


def test_a_failing_delta_says_which_kind_of_failure_it_is() -> None:
    """The two ways a paired delta gate fails are different findings.

    Feature Extractor's ``quality_delta_pp`` is −10.44 pp [−13.78, −7.12]:
    every plausible value is a loss, so a regression was measured. Chunk
    Summarizer's is −2.32 pp [−5.00, +0.36]: the interval spans zero, so what
    the data shows is that parity was not *demonstrated* at the bound — not
    that quality dropped. Both are FAIL and neither verdict moves.

    Rendering one string for both would overstate the weaker finding, which is
    the same error as overstating a positive one and is barred by the same
    ground rule.
    """

    def delta(point: float, lo: float, hi: float) -> Estimate:
        return Estimate(
            metric="quality_delta_pp",
            point=point,
            lo=lo,
            hi=hi,
            n=70,
            unit="percentage_points",
            method="paired_percentile_bootstrap",
            paired_n=70,
        )

    assert delta_failure_kind(delta(-10.44, -13.78, -7.12)) == FAIL_REGRESSION
    assert delta_failure_kind(delta(-2.32, -5.00, 0.36)) == FAIL_IMPRECISE
    assert FAIL_REGRESSION != FAIL_IMPRECISE

    # An interval that clears zero entirely is neither, even if it fails a
    # positive bound: it is a small improvement, and both words would misread it.
    assert delta_failure_kind(delta(3.0, 1.0, 5.0)) is None

    # Zero is the reference point only for a paired delta. On a level metric
    # such as json_schema_validity, "spans zero" means nothing, so the cell
    # says nothing rather than inventing a reading.
    level = Estimate(metric="json_schema_validity", point=0.814, lo=0.714, hi=0.900, n=70)
    assert delta_failure_kind(level) is None
    assert delta_failure_kind(None) is None


def test_a_failing_gate_row_carries_its_failure_kind_and_the_note(
    report_with_deltas: str,
) -> None:
    """The distinction has to survive into the Markdown a customer reads."""
    report = report_with_deltas
    failing = [
        line
        for line in report.splitlines()
        if line.startswith("| `quality_delta_pp` |") and "**FAIL**" in line
    ]
    assert failing, "expected at least one failing quality_delta_pp row to word"
    for line in failing:
        assert FAIL_REGRESSION in line or FAIL_IMPRECISE in line, line

    # Both kinds are present on the real artifact, which is the point: FE's
    # interval sits entirely below zero and CS's spans it. If a future artifact
    # ever renders them with one string, this is the assertion that catches it.
    assert any(FAIL_REGRESSION in line for line in failing)
    assert any(FAIL_IMPRECISE in line and FAIL_REGRESSION not in line
               for line in failing)

    # The qualifier is not self-explanatory, so the report that uses it
    # explains it.
    assert "fails on **precision**" in report
    assert "not because a drop was demonstrated" in report


def test_the_report_labels_itself_as_replay(report: str) -> None:
    assert report.startswith("# Migration Readiness Scorecard")
    assert "REPLAY" in report


def test_every_pre_agreed_gate_appears_in_every_subagent_table(
    cfg: AppConfig, phase2: Phase2Result, report: str
) -> None:
    subagents = {arm.subagent for arm in phase2.arms}
    for gate in cfg.gates.subagent_gates:
        assert report.count(f"| `{gate}` |") == len(subagents)


def test_todays_real_artifact_yields_no_migrate_verdict(
    cfg: AppConfig, phase2: Phase2Result, report: str
) -> None:
    """Not an aspiration — a statement about this build. Shadow has not run,
    prices are unverified and latency is not comparable, so no subagent can
    reach the verdict the customer pre-agreed to."""
    rules = VerdictRules.of(cfg.gates)
    assert f"**{rules.all_pass}**" not in report
    assert INCOMPLETE in report
    assert "provisional" in report


def test_every_unevaluated_gate_carries_a_reason(
    cfg: AppConfig, phase2: Phase2Result
) -> None:
    evidence = build_evidence(cfg, phase2)
    for item in evidence:
        for gate in cfg.gates.subagent_gates:
            assert (gate in item.estimates) ^ (gate in item.unmeasured), gate
            if gate in item.unmeasured:
                assert item.unmeasured[gate].strip()


# --------------------------------------------------------------------------
# forced fixtures, rendered end to end
# --------------------------------------------------------------------------


def _forced(
    cfg: AppConfig,
    phase2: Phase2Result,
    subagent: str,
    estimates,
    *,
    adjudication: TriageSummary | None = None,
) -> list:
    return [
        SubagentEvidence(
            subagent=subagent,
            baseline_variant="claude_baseline",
            candidate_variant="gemini_tuned_v1",
            estimates=estimates,
            adjudication=adjudication,
            unmeasured={
                g: "not supplied by this fixture"
                for g in cfg.gates.subagent_gates
                if g not in estimates
            },
            sentinel_values={"claude_baseline_p95": 1200.0},
            claude_schema_validity=ClaudeSchemaValidityCell(point=0.814),
            judge_baseline=JudgeScoreCell(split="core", items_scored=28, point=0.91),
            judge_candidate=JudgeScoreCell(split="core", items_scored=28, point=0.88),
            regions=Regions(baseline="global", candidate="us-central1", source="fixture"),
        )
    ]


def _all_gates(cfg: AppConfig, *, failing: tuple[str, ...]) -> dict[str, Estimate]:
    """Estimates positioned either side of the bounds read from gates.yaml."""
    units = {
        "quality_delta_pp": "percentage_points",
        "groundedness_delta_pp": "percentage_points",
        "json_schema_validity": "fraction",
        "shadow_agreement": "fraction",
        "cost_savings_pct": "percent",
        "latency_p95": "ms",
    }
    out: dict[str, Estimate] = {}
    for name in cfg.gates.subagent_gates:
        gate = cfg.gates.gate(name)
        bound = 1200.0 if gate.is_sentinel else float(gate.bound)
        unit = units[name]
        step = {"fraction": 0.005, "percentage_points": 0.5, "percent": 5.0, "ms": 50.0}[
            unit
        ]
        passes = name not in failing
        if gate.direction == "min":
            lo = bound + step if passes else bound - step
            hi = lo + step
        else:
            hi = bound - step if passes else bound + step
            lo = hi - step
        if unit == "fraction":
            lo, hi = min(max(lo, 0.0), 1.0), min(max(hi, 0.0), 1.0)
        out[name] = Estimate(
            metric=name, point=(lo + hi) / 2, lo=lo, hi=hi, n=28, unit=unit
        )
    return out


def test_forced_tune_first_renders(cfg: AppConfig, phase2: Phase2Result) -> None:
    rules = VerdictRules.of(cfg.gates)
    evidence = _forced(
        cfg,
        phase2,
        "query_rewriter",
        _all_gates(cfg, failing=tuple(rules.quality_gates)),
    )
    markdown = render_markdown(build_scorecard(cfg, phase2, evidence=evidence))
    assert f"**{rules.only_quality_gates_fail}**" in markdown
    assert "**FAIL**" in markdown
    # Even a rendered pass keeps the structural rules.
    assert TAXONOMY_LINE in markdown
    assert CLAUDE_SCHEMA_CAVEAT in markdown
    assert cost_cell(prices_verified=False) in markdown


def test_forced_hold_renders(cfg: AppConfig, phase2: Phase2Result) -> None:
    rules = VerdictRules.of(cfg.gates)
    evidence = _forced(
        cfg,
        phase2,
        "query_rewriter",
        _all_gates(cfg, failing=(rules.blocking_gates[0],)),
    )
    markdown = render_markdown(build_scorecard(cfg, phase2, evidence=evidence))
    assert f"**{rules.any_blocking_gate_fails}**" in markdown


def test_forced_migrate_still_renders_the_latency_probe_rule(
    cfg: AppConfig, phase2: Phase2Result
) -> None:
    """Even with every gate passing, latency without a probe is a disclosure —
    so an all-pass fixture is INCOMPLETE unless the probe is supplied."""
    rules = VerdictRules.of(cfg.gates)
    estimates = _all_gates(cfg, failing=())
    markdown = render_markdown(
        build_scorecard(
            cfg, phase2, evidence=_forced(cfg, phase2, "query_rewriter", estimates)
        )
    )
    # The gate has an estimate, but no probe object, so the *cell* still
    # discloses the split even though the gate was evaluated.
    assert REGION_SPLIT_DISCLOSURE in markdown
    assert f"**{rules.all_pass}**" in markdown


# --------------------------------------------------------------------------
# the pre-registered alt clause
#
# `shadow_agreement` is the one gate gates.yaml gave a second route: "on
# disagreements, judge-adjudicated wins >= losses". It was written before
# anything was measured, which is the only reason a missed CI bound is allowed
# to become a pass. These tests pin the three things that keeps honest — the
# route is visible in the cell, the clause is decided on the tally it names,
# and an absent adjudication is not a silent pass.
# --------------------------------------------------------------------------


def _shadow_only(cfg: AppConfig, agreement: Estimate) -> dict[str, Estimate]:
    return {"shadow_agreement": agreement}


def _failing_agreement() -> Estimate:
    return Estimate(
        metric="shadow_agreement", point=0.643, lo=0.529, hi=0.757, n=70, unit="fraction"
    )


def _triage(wins: int, losses: int, **kw) -> TriageSummary:
    return TriageSummary(
        subagent="query_rewriter",
        disagreements=wins + losses + kw.pop("ties", 33),
        wins=wins,
        losses=losses,
        ties=33,
        **kw,
    )


def _qr(cfg, phase2, summary, agreement=None):
    evidence = _forced(
        cfg,
        phase2,
        "query_rewriter",
        _shadow_only(cfg, agreement or _failing_agreement()),
        adjudication=summary,
    )
    return evidence, decide_verdict(evidence[0], cfg.gates)


def test_a_missed_bound_clears_on_the_pre_registered_alt_clause(cfg, phase2) -> None:
    _, verdict = _qr(cfg, phase2, _triage(15, 3, wins_baseline_malformed=6))
    check = verdict.checks["shadow_agreement"]
    assert check.passed is False, "the CI bound was and remains missed"
    assert check.alt_passed is True
    assert check.effective_passed is True
    assert verdict.passed_by_alt == ["shadow_agreement"]
    assert "shadow_agreement" not in verdict.failed


def test_the_result_cell_is_never_a_bare_pass(cfg, phase2) -> None:
    """A reader scanning the Result column has to see the route.

    "PASS" alone would say the gate cleared its bound. It did not.
    """
    evidence, _ = _qr(cfg, phase2, _triage(15, 3, wins_baseline_malformed=6))
    markdown = render_markdown(build_scorecard(cfg, phase2, evidence=evidence))
    row = next(
        line for line in markdown.splitlines() if line.startswith("| `shadow_agreement`")
    )
    assert "PASS (by pre-registered alt clause: adjudication 15W/3L)" in row
    assert "| PASS |" not in row


def test_the_footnote_carries_both_figures_and_the_mechanism(cfg, phase2) -> None:
    evidence, _ = _qr(cfg, phase2, _triage(15, 3, wins_baseline_malformed=6))
    markdown = render_markdown(build_scorecard(cfg, phase2, evidence=evidence))
    assert "15W/3L overall" in markdown
    assert "9W/3L excluding items" in markdown
    assert MALFORMED_CAVEAT in markdown
    assert "did not clear its CI bound" in markdown
    assert "pre-agreed second route, not a threshold chosen after seeing" in markdown


def test_the_clause_is_decided_on_the_tally_it_names(cfg, phase2) -> None:
    """gates.yaml's clause has no exclusion in it, so neither does the check.

    Deciding it on the quality-only tally would be applying a rule the
    customer never agreed to — even though here it would be the stricter one.
    """
    _, verdict = _qr(cfg, phase2, _triage(4, 3, wins_baseline_malformed=3))
    assert verdict.checks["shadow_agreement"].alt_passed is True, "4 >= 3"
    assert "1W/3L excluding items" in verdict.checks["shadow_agreement"].alt_evidence


def test_losing_the_clause_leaves_the_gate_failed(cfg, phase2) -> None:
    rules = VerdictRules.of(cfg.gates)
    _, verdict = _qr(cfg, phase2, _triage(14, 20))
    check = verdict.checks["shadow_agreement"]
    assert check.alt_passed is False
    assert check.effective_passed is False
    assert check.result_text() == "**FAIL**"
    assert verdict.verdict == rules.any_blocking_gate_fails


def test_no_adjudication_is_not_a_pass(cfg, phase2) -> None:
    """An unevaluated route is not a cleared one — the gate stands as measured."""
    _, verdict = _qr(cfg, phase2, None)
    check = verdict.checks["shadow_agreement"]
    assert check.alt_passed is None
    assert check.effective_passed is False
    assert "was not evaluated" in check.alt_evidence
    assert verdict.passed_by_alt == []


def test_nothing_adjudicated_is_not_a_vacuous_pass(cfg, phase2) -> None:
    summary = TriageSummary(
        subagent="query_rewriter", disagreements=51, not_adjudicated=51
    )
    _, verdict = _qr(cfg, phase2, summary)
    assert verdict.checks["shadow_agreement"].alt_passed is None, "0 >= 0 is not evidence"


def test_a_gate_that_already_passed_is_left_alone(cfg, phase2) -> None:
    passing = Estimate(
        metric="shadow_agreement", point=0.97, lo=0.94, hi=0.99, n=70, unit="fraction"
    )
    _, verdict = _qr(cfg, phase2, _triage(0, 51, ties=0), agreement=passing)
    check = verdict.checks["shadow_agreement"]
    assert check.passed is True
    assert check.alt_passed is None, "the alt clause is a rescue, not a second hurdle"
    assert check.result_text() == "PASS"


def test_an_alt_clause_with_no_evaluator_is_refused_not_ignored(cfg, phase2) -> None:
    """A pre-agreed route nobody can evaluate is a gate quietly disappearing."""
    evidence, _ = _qr(cfg, phase2, None)
    check = decide_verdict(evidence[0], cfg.gates).checks["shadow_agreement"]
    orphan = check.model_copy(update={"gate": "quality_delta_pp", "alt": "some clause"})
    assert "quality_delta_pp" not in ALT_EVALUATORS
    with pytest.raises(ConfigError, match="knows how to evaluate"):
        apply_alt_clause(orphan, evidence[0])


def test_the_adjudication_is_read_off_the_same_run_as_the_agreement() -> None:
    """One file, one corpus: the clause and the gate describe the same items."""
    path = ARTIFACT.parent / "shadow_qr_targeted.json"
    if not path.is_file():  # pragma: no cover - artifact-dependent
        pytest.skip("no query_rewriter shadow artifact in this checkout")
    summaries = load_adjudications(path)
    assert set(summaries) == {"query_rewriter"}
    summary = summaries["query_rewriter"]
    assert summary.wins == 15 and summary.losses == 3
    assert summary.wins_baseline_malformed == 6
    assert summary.wins_ge_losses is True


# --------------------------------------------------------------------------
# CLI helpers
# --------------------------------------------------------------------------


def test_parse_volume_accepts_the_short_and_long_forms() -> None:
    assert parse_volume("query_rewriter:250000") == (
        "query_rewriter",
        {"calls_per_day": 250000},
    )
    assert parse_volume("chunk_summarizer:1200000:2400:320") == (
        "chunk_summarizer",
        {"calls_per_day": 1200000, "avg_input_tokens": 2400, "avg_output_tokens": 320},
    )


@pytest.mark.parametrize("spec", ["query_rewriter", "qr:1:2", "qr:lots"])
def test_parse_volume_rejects_malformed_specs(spec: str) -> None:
    with pytest.raises(ValueError):
        parse_volume(spec)


def test_cmd_scorecard_is_importable_from_the_package() -> None:
    from amw.reporting import cmd_scorecard

    assert callable(cmd_scorecard)
