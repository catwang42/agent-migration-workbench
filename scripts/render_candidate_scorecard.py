"""B3/B4 — score the gates against a *deployment candidate*, not the dev model.

The scorecard that shipped until now compares ``claude_baseline`` to
``gemini_tuned_v1`` on ``gemini-flash`` — Gemini 2.5 Flash, the model the
tuning ladder and the optimizer work were done on. Nobody would deploy it: it
is a development generation. This script re-bases the same six gates onto the
models a customer would actually put in production, one scorecard per
candidate.

Three things make that more than a flag flip.

**The candidate arms are not in any phase-2 artifact.** They were measured by
the ablation ladder, as the ``ship-<suffix>`` rungs, and each rung carries a
full ``ArmResult`` scored by the same code phase 2 uses. So the phase-2 record
this script scores is assembled here: the ``claude_baseline`` arms out of
``phase2_n70_widened.json`` (the full-70 widening, so every subagent is judged
on the same split), plus each subagent's ``ship-<suffix>`` arm lifted out of
its ladder. No number is recomputed in the assembling — the arms are copied
whole.

**The shipping prompt differs per subagent.** The ladder did not converge on
one winner: Query Rewriter ships ``gemini_targeted_v1``, Chunk Summarizer
``gemini_tuned_v1``, Feature Extractor ``gemini_optimizer_v1``. Scoring all
three against a single ``--candidate-variant`` would compare two of them to a
prompt that was never going to be deployed, so ``candidate_variant`` is passed
as a per-subagent mapping.

**Two of the six gates now have evidence they did not have before**, and both
arrive from outside the phase-2 artifact:

* ``latency_p95`` — from ``latency_probe_<region>.json`` (A4), which is the
  only input that can unlock the gate. It exists for ``gemini-flash-current``
  only; the other candidate's latency stays unmeasured rather than borrowing
  it.
* ``cost_savings_pct`` — from ``cost_savings.json`` (B4), on measured tokens.
  This is a **different basis** from the one ``gates.yaml`` registers, and
  :mod:`amw.economics.measured_savings` carries the sentence that says so.

Everything runs in replay. The per-item vectors behind the two paired-delta
gates are recovered by re-executing the arms against recorded calls, with the
candidate model passed explicitly so the replay cannot fall back to the 2.5
recordings that share the same variant name.

    .venv/bin/python scripts/render_candidate_scorecard.py --candidate gemini-flash-current
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv ships in requirements.txt
    pass
else:
    load_dotenv(ROOT / ".env", override=False)

from amw.config import load_all  # noqa: E402
from amw.eval.runner import Phase2Result  # noqa: E402
from amw.eval.stats import Estimate  # noqa: E402
from amw.reporting.evidence import (  # noqa: E402
    BASELINE_VARIANT,
    Regions,
    SameRegionLatencyProbe,
    collect_samples,
)
from amw.reporting.scorecard import (  # noqa: E402
    DEFAULT_SHADOW_METRIC,
    ConfigurationCost,
    CostPanel,
    build_scorecard,
    load_adjudications,
    load_shadow,
    render_markdown,
)
from amw.tuning.ablate import (  # noqa: E402
    AblationResult,
    CAPPED_GEN_MODEL,
    DEPLOYMENT_CANDIDATES,
    SHIPPING_VARIANT,
)

SUBAGENTS = ("query_rewriter", "chunk_summarizer", "feature_extractor")
BASELINE_MODEL = "claude-sonnet"
RESULTS = ROOT / "artifacts" / "results"
PHASE2 = RESULTS / "phase2_n70_widened.json"
COST = RESULTS / "cost_savings.json"
COST_CAPPED = RESULTS / "cost_savings_capped.json"

#: The capped configuration is a *configuration* of ``gemini-flash-current``,
#: not a fourth candidate model, so it is not in ``DEPLOYMENT_CANDIDATES``. It
#: still needs the three things a candidate needs to be scored — a rung id, an
#: artifact suffix and a base configuration to inherit unmeasured evidence
#: from — so they live here rather than being special-cased at four call sites.
CAPPED_BASE = "gemini-flash-current"
SCORABLE = sorted(set(DEPLOYMENT_CANDIDATES) | {CAPPED_GEN_MODEL})

#: ``claude-opus`` is priced in ``config/pricing.yaml`` and named on the models
#: page, and it is deliberately *not* rendered here. A projection panel sitting
#: beside measured panels blurs the line this whole build protects: every number
#: a customer sees came from a call that was made. Opus was never run, so it
#: gets a price and a sentence, not a table.


def _suffix(candidate: str) -> str:
    """Artifact suffix for a candidate: ``current``, ``35``, ``current-capped``."""
    if candidate == CAPPED_GEN_MODEL:
        return f"{DEPLOYMENT_CANDIDATES[CAPPED_BASE]}-capped"
    return DEPLOYMENT_CANDIDATES[candidate]


def _rung_id(candidate: str) -> str:
    return f"ship-{_suffix(candidate)}"


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _log(message: str) -> None:
    print(f"[{_stamp()}] {message}", flush=True)


def _widen_window(values: list[str | None], *, latest: bool) -> str | None:
    """The outer edge of a set of ISO timestamps, ignoring the unrecorded ones."""
    stamps = [v for v in values if v]
    if not stamps:
        return None
    return max(stamps) if latest else min(stamps)


def ship_arms(candidate: str) -> dict[str, tuple[str, object, object]]:
    """``subagent -> (variant, ArmResult, RungRecord)`` for the shipping rung.

    Raises rather than skipping a subagent whose rung is missing or was
    recorded as ``no_recordings``. A scorecard with two of three subagents on
    the candidate and one silently left on the development model is the exact
    mix-up this whole script exists to undo.
    """
    out: dict[str, tuple[str, object, object]] = {}
    rung_id = _rung_id(candidate)
    for subagent in SUBAGENTS:
        path = RESULTS / f"ablation_{subagent}.json"
        ladder = AblationResult.model_validate_json(path.read_text(encoding="utf-8"))
        rungs = [r for r in ladder.rungs if r.rung == rung_id]
        if not rungs:
            raise SystemExit(
                f"{path.name} has no {rung_id!r} rung, so {subagent} has no measured "
                f"{candidate} arm. Run scripts/measure_current_gen.py for this "
                f"candidate before scoring it."
            )
        rung = rungs[0]
        if rung.status != "measured" or rung.arm is None:
            raise SystemExit(
                f"{path.name}: {rung_id} is {rung.status!r} "
                f"({rung.unmeasured_reason or 'no reason recorded'}). Refusing to "
                f"score a gate against an arm that was not measured."
            )
        if rung.variant != SHIPPING_VARIANT[subagent]:
            raise SystemExit(
                f"{path.name}: {rung_id} runs {rung.variant!r} but SHIPPING_VARIANT "
                f"says {subagent} ships {SHIPPING_VARIANT[subagent]!r}."
            )
        out[subagent] = (rung.variant, rung.arm, rung)
    return out


def candidate_phase2(
    candidate: str, arms: dict[str, tuple[str, object, object]]
) -> Phase2Result:
    """The widened baseline arms plus this candidate's shipping arms, nothing else.

    The intermediate ladder rungs are deliberately left out. They belong in the
    ladder table, where they are labelled as rungs; dropped into an artifact
    called phase 2 they read as arms someone chose to evaluate.
    """
    widened = Phase2Result.model_validate_json(PHASE2.read_text(encoding="utf-8"))
    keep = [
        arm
        for arm in widened.arms
        if arm.variant == BASELINE_VARIANT and arm.subagent in arms
    ]
    missing = sorted(set(arms) - {a.subagent for a in keep})
    if missing:
        raise SystemExit(
            f"{PHASE2.name} has no {BASELINE_VARIANT} arm for {missing}; there is "
            "nothing to compare the candidate against."
        )
    note = (
        f"Arms assembled for the {candidate} scorecard: {BASELINE_VARIANT} from "
        f"{PHASE2.name} (full-70 widening) and each subagent's "
        f"{_rung_id(candidate)} rung from artifacts/results/"
        f"ablation_*.json. Both sets were scored by the phase-2 scorer; this "
        f"assembly copies them and recomputes nothing."
    )
    # The two arm sets were recorded days apart, so the footer's recording
    # window is the union of both. Leaving the widening's window in place would
    # print a REPLAY banner that stops before the candidate calls were made.
    windows = [rung.provenance for _, _, rung in arms.values()]
    return widened.model_copy(
        update={
            "arms": keep + [arm for _, arm, _ in arms.values()],
            "notes": list(widened.notes) + [note],
            "recorded_from": _widen_window(
                [widened.recorded_from, *(w.recorded_from for w in windows)],
                latest=False,
            ),
            "recorded_to": _widen_window(
                [widened.recorded_to, *(w.recorded_to for w in windows)], latest=True
            ),
        }
    )


def latency_probes(candidate: str, region: str) -> dict[str, SameRegionLatencyProbe]:
    """Probes for ``candidate`` from ``latency_probe_<region>.json``, or ``{}``.

    A probe recorded against a different candidate model is not reused. The
    p95 of a 3.6 Flash call says nothing about 3.5 Flash, and the gate is the
    one place where "close enough" turns into a passing verdict.
    """
    # scripts/probe_same_region_latency.py suffixes the filename with the
    # candidate for everything but the default one, so that a capped run cannot
    # overwrite the probe behind an already-shipped latency cell.
    slug = region if candidate == CAPPED_BASE else f"{region}_{candidate}"
    path = RESULTS / f"latency_probe_{slug}.json"
    if not path.is_file():
        _log(f"  latency: no {path.name}; latency_p95 stays unmeasured")
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("candidate_model") != candidate:
        _log(
            f"  latency: {path.name} probed {data.get('candidate_model')!r}, not "
            f"{candidate!r}; latency_p95 stays unmeasured for this candidate"
        )
        return {}
    out: dict[str, SameRegionLatencyProbe] = {}
    for subagent, record in data["subagents"].items():
        if not record.get("gate_eligible"):
            continue
        out[subagent] = SameRegionLatencyProbe(
            region=data["region"],
            candidate_p95=Estimate.model_validate(record["candidate"]["p95_estimate"]),
            baseline_p95_ms=float(record["baseline"]["total_ms_p95"]),
            baseline_region=record["baseline"]["region"],
            candidate_region=record["candidate"]["region"],
            probed_on=data["probed_on"],
            note=record.get("disposition"),
        )
    _log(f"  latency: {len(out)} gate-eligible probe(s) from {path.name}")
    return out


def latency_disclosure(candidate: str, region: str) -> str | None:
    """The sample size and the between-probe drift, in the report's own words.

    A p95 over ten calls is a thin instrument, and this project has two probes
    of the *same* baseline model in the *same* region three hours apart to prove
    it: the incumbent's own p95 moved by more than the gap the gate is being
    asked to rule on. That is not a reason to withhold the measurement — it
    shipped as measured — but a reader who sees a FAIL without the drift beside
    it will read a stable difference into a noisy one.
    """
    slug = region if candidate == CAPPED_BASE else f"{region}_{candidate}"
    path = RESULTS / f"latency_probe_{slug}.json"
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    parts = [
        f"latency_p95 sample size: n={data['n_per_subagent']} calls per arm per "
        f"subagent, both arms pinned to {data['region']}, probed "
        f"{data['probed_on']}."
    ]

    # Every other probe of the same baseline in the same region is a repeat
    # measurement of a value the gate treats as fixed. Where one exists, its
    # spread is the honest error bar on the comparison.
    drift: list[str] = []
    for other in sorted(RESULTS.glob(f"latency_probe_{region}*.json")):
        if other == path:
            continue
        prior = json.loads(other.read_text(encoding="utf-8"))
        if prior.get("baseline_model") != data.get("baseline_model"):
            continue
        if prior.get("region") != data.get("region"):
            continue
        for subagent, record in data["subagents"].items():
            was = prior.get("subagents", {}).get(subagent)
            if not was:
                continue
            drift.append(
                f"{subagent} {was['baseline']['total_ms_p95']:,.0f} -> "
                f"{record['baseline']['total_ms_p95']:,.0f} ms"
            )
        if drift:
            parts.append(
                f"The incumbent's own p95 moved between the {prior['probed_on']} "
                f"and {data['probed_on']} probes of the same model in the same "
                f"region ({'; '.join(drift)}), which is the size of the noise "
                f"this comparison sits on."
            )
            break

    parts.append(
        "Latency requires measurement on production infrastructure; "
        "demo-window figures are directional."
    )
    return " ".join(parts)


def _cost_file(candidate: str) -> Path:
    return COST_CAPPED if candidate == CAPPED_GEN_MODEL else COST


def cost_savings(candidate: str, path: Path | None = None) -> tuple[dict[str, Estimate], str]:
    """``{subagent: Estimate}`` from the cost artifact, plus the basis line."""
    path = path or _cost_file(candidate)
    if not path.is_file():
        return {}, ""
    data = json.loads(path.read_text(encoding="utf-8"))
    block = data.get("candidates", {}).get(candidate, {})
    out: dict[str, Estimate] = {}
    basis = ""
    for subagent, record in block.items():
        basis = basis or record.get("basis", "")
        estimate = record.get("estimate")
        if estimate:
            out[subagent] = Estimate.model_validate(estimate)
    _log(f"  cost: {len(out)} measured savings estimate(s) from {path.name}")
    return out, basis


def shadow_inputs(candidate: str, metric: str) -> tuple[dict, dict, list[str], str | None]:
    """Merge the per-subagent shadow artifacts for ``candidate``.

    ``cli.py shadow`` writes one file per subagent for the current-generation
    campaigns, so the agreement estimates and the adjudications are merged
    here. A subagent with no shadow file is simply absent, which renders as
    "not evaluated" — never as agreement.

    The capped configuration has no shadow campaign of its own — there was not
    time to run one before freeze — so it reads the default configuration's
    files and returns a label saying so. Fourth return value is that label, or
    ``None`` when the numbers came from the configuration being scored. It is
    not optional: an agreement figure measured at a different reasoning budget
    is a mixed-configuration number, and the ruling is that no such number
    renders unlabelled.
    """
    carried = candidate == CAPPED_GEN_MODEL
    source = CAPPED_BASE if carried else candidate
    suffix = DEPLOYMENT_CANDIDATES[source]
    agreements: dict = {}
    adjudications: dict = {}
    found: list[str] = []
    for subagent in SUBAGENTS:
        path = RESULTS / f"shadow_{suffix}_{subagent}.json"
        if not path.is_file():
            continue
        found.append(path.name)
        agreements.update(load_shadow(path, metric))
        adjudications.update(load_adjudications(path))
    label = None
    if carried and found:
        label = (
            "shadow_agreement is carried from the DEFAULT thinking configuration "
            f"({source}), not measured on the capped configuration: the shadow "
            f"campaign in {', '.join(found)} predates the capped arms and there was "
            "no time to re-run it before freeze. Same model, same prompt bytes, same "
            "shadow slice; different reasoning budget. Every other gate on this "
            "scorecard is measured on the capped configuration itself."
        )
    return agreements, adjudications, found, label


def _totals(path: Path, candidate: str) -> dict[str, dict[str, float]]:
    """Per-subagent corpus totals summed from a cost artifact's paired items."""
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, dict[str, float]] = {}
    for subagent, record in data.get("candidates", {}).get(candidate, {}).items():
        paired = record.get("paired") or []
        if not paired:
            continue
        out[subagent] = {
            "baseline_usd": sum(p["baseline_usd"] for p in paired),
            "candidate_usd": sum(p["candidate_usd"] for p in paired),
            "baseline_output_tokens": sum(p["baseline_output_tokens"] for p in paired),
            "candidate_output_tokens": sum(
                p["candidate_output_tokens"] for p in paired
            ),
            "baseline_input_tokens": sum(p["baseline_input_tokens"] for p in paired),
        }
    return out


def _savings_text(estimate: Estimate | None) -> str:
    if estimate is None:
        return "not estimated"
    return f"{estimate.point:+.1f}% [{estimate.lo:+.1f}, {estimate.hi:+.1f}]"


def cost_panel(cfg) -> CostPanel | None:
    """The capped and default configurations side by side, capped first.

    Both rows are measurements. The default row stays on the scorecard because
    it is what every arm recorded before 2026-08-12 ran on, and because the gap
    between the two rows *is* the finding.
    """
    capped_totals = _totals(COST_CAPPED, CAPPED_GEN_MODEL)
    default_totals = _totals(COST, CAPPED_BASE)
    if not capped_totals or not default_totals:
        return None
    capped_est, _ = cost_savings(CAPPED_GEN_MODEL)
    default_est, _ = cost_savings(CAPPED_BASE)
    rows: list[ConfigurationCost] = []
    for subagent in SUBAGENTS:
        for name, totals, estimates, recommended in (
            ("reasoning budget minimised", capped_totals, capped_est, True),
            ("default reasoning budget", default_totals, default_est, False),
        ):
            record = totals.get(subagent)
            if not record:
                continue
            rows.append(
                ConfigurationCost(
                    subagent=subagent,
                    configuration=name,
                    savings_text=_savings_text(estimates.get(subagent)),
                    baseline_usd=record["baseline_usd"],
                    candidate_usd=record["candidate_usd"],
                    output_tokens=int(record["candidate_output_tokens"]),
                    baseline_output_tokens=int(record["baseline_output_tokens"]),
                    recommended=recommended,
                )
            )
    spec = cfg.models.spec(CAPPED_GEN_MODEL)
    notes = [
        "**The thinking tax.** Both rows are the same provider model ID "
        f"(`{spec.ids.get('vertex')}`) on the same prompt bytes over the same 70 "
        "items. The only difference is `thinking_config.thinking_budget`. The "
        "default-budget row bills reasoning tokens that are never returned to the "
        "caller — they are folded into billable output tokens and cannot be "
        "separated after the fact — which is what moves the savings column across "
        "the gate line.",
        "**Market context.** Reasoning-by-default is the current direction of "
        "travel for frontier models on both sides of this comparison. A cost "
        "estimate built on a default-settings call is therefore an estimate of a "
        "moving target; the recommended row is the configuration a deployment "
        "would actually pin.",
        "Neither row is a quality claim. Quality, schema validity and latency for "
        "the recommended configuration are measured in the gate table above, on "
        "the same recorded calls these dollar figures are summed from.",
    ]
    return CostPanel(rows=rows, notes=notes)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--candidate",
        default="gemini-flash-current",
        choices=SCORABLE,
        help="deployment candidate or configuration to score (default: %(default)s)",
    )
    parser.add_argument(
        "--cost",
        default=None,
        type=Path,
        help="cost artifact to read (default: cost_savings_capped.json for the "
        "capped configuration, cost_savings.json otherwise)",
    )
    parser.add_argument(
        "--no-panels",
        action="store_true",
        help="omit the configuration-cost panel",
    )
    parser.add_argument(
        "--latency-region",
        default="global",
        help="which latency_probe_<region>.json to read (default: %(default)s)",
    )
    parser.add_argument("--out", default=None, help="write markdown here")
    parser.add_argument(
        "--shadow-metric",
        default=DEFAULT_SHADOW_METRIC,
        help="registered gated agreement metric (default: %(default)s)",
    )
    args = parser.parse_args(argv)
    candidate = args.candidate

    cfg = load_all(customer="demo_patents")
    _log(f"=== scorecard: {BASELINE_MODEL} vs {candidate} ===")

    arms = ship_arms(candidate)
    variants = {subagent: variant for subagent, (variant, _, _) in arms.items()}
    for subagent, variant in variants.items():
        _log(f"  {subagent:20s} candidate arm {variant} on {candidate}")
    phase2 = candidate_phase2(candidate, arms)

    samples = collect_samples(
        cfg,
        phase2,
        mode="replay",
        arms=(
            [(s, BASELINE_VARIANT, BASELINE_MODEL) for s in variants]
            + [(s, v, candidate) for s, v in variants.items()]
        ),
    )
    _log(f"  samples: {len(samples)} arms replayed, 0 live calls")

    agreements, adjudications, shadow_files, carried_label = shadow_inputs(
        candidate, args.shadow_metric
    )
    _log(
        f"  shadow: {len(agreements)} {args.shadow_metric} agreement estimate(s), "
        f"{len(adjudications)} adjudication(s) from {shadow_files or 'no files'}"
    )
    if carried_label:
        _log("  shadow: CARRIED from the default configuration — labelled in-report")
    savings, basis = cost_savings(candidate, args.cost)
    probes = latency_probes(candidate, args.latency_region)

    # Where each arm ran, resolved the same way the adapters resolve it: a
    # registry pin beats the environment. The deployment candidates carry a
    # `region: global` pin and Claude is sent to `global` by $CLAUDE_REGION
    # (us-central1 Model Garden quota is exhausted), so this pairing is *not*
    # subject to the region split the 2.5-generation reports disclose. Derived
    # rather than asserted: a re-pin cannot leave a stale claim behind, and the
    # A4 probe — which refuses to exist across two regions — independently
    # recorded both arms in `global`.
    env = Regions.from_env(cfg)
    base_region = cfg.models.spec(BASELINE_MODEL).region or env.baseline
    cand_region = cfg.models.spec(candidate).region or env.candidate
    regions = Regions(
        baseline=base_region,
        candidate=cand_region,
        source=(
            f"config/models.yaml region pin where set, else {env.source}; "
            f"{BASELINE_MODEL} -> {base_region}, {candidate} -> {cand_region}"
        ),
    )
    _log(f"  regions: baseline {base_region}, candidate {cand_region}")

    card = build_scorecard(
        cfg,
        phase2,
        ladders=None,
        samples=samples,
        shadow=agreements or None,
        shadow_metric=args.shadow_metric if agreements else None,
        adjudications=adjudications or None,
        regions=regions,
        baseline_variant=BASELINE_VARIANT,
        candidate_variant=variants,
        latency_probes=probes or None,
        cost_savings=savings or None,
    )
    if basis:
        card.notes.append(f"cost_savings_pct basis: {basis}")
    if probes:
        disclosure = latency_disclosure(candidate, args.latency_region)
        if disclosure:
            card.notes.append(disclosure)
    if carried_label:
        # First note, not last: it qualifies a cell in the gate table, and the
        # notes render in order.
        card.notes.insert(0, carried_label)
    if not args.no_panels:
        # Only the 3.6 Flash scorecards get the configuration panel: it compares
        # two configurations of *that* model, and hanging it off the 3.5 Flash
        # card would put a table of another model's economics under its gates.
        if candidate in (CAPPED_GEN_MODEL, CAPPED_BASE):
            card.cost_panel = cost_panel(cfg)
        _log(f"  panels: cost_panel={'yes' if card.cost_panel else 'no'}")
    markdown = render_markdown(card)

    out = Path(args.out or RESULTS / f"scorecard_{_suffix(candidate)}.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(markdown, encoding="utf-8")
    _log(f"wrote {os.path.relpath(out, ROOT)} ({len(markdown.splitlines())} lines)")
    for subagent, verdict in sorted(card.verdicts.items()):
        _log(f"  VERDICT {subagent:20s} {getattr(verdict, 'verdict', verdict)}")
    for note in card.notes:
        print(f"note: {note}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:  # pragma: no cover
        traceback.print_exc()
        raise SystemExit(1)
