#!/usr/bin/env python3
"""Agent Migration Workbench CLI.

    python cli.py gen --customer demo_patents -n 70
    python cli.py phase2 --mode hybrid -n 10
    python cli.py ablate --subagent query_rewriter
    python cli.py shadow --mode hybrid
    python cli.py scorecard
    python cli.py e2e --mode replay
    python cli.py smoke --mode live -n 2

Subcommands are wired up as their tasks land (T09 owns the phase runners and
the e2e pipeline). Until then an unimplemented subcommand exits non-zero
saying which task will deliver it — it never pretends to have run.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

# Importing the adapter package must not need credentials or provider SDKs —
# the lanes' tests assert that — so MODES/CLAUDE_PATHS are taken from the one
# place that defines them rather than restated here.
from amw.adapters import CLAUDE_PATHS, MODES
from amw.agents.prompt_packs import ALL_VARIANTS, VARIANTS
from amw.agents.schemas import SUBAGENTS
from amw.config import ConfigError, load_all
from amw.economics.cache_demo import (
    DEMO_SUBAGENT,
    DEMO_VARIANT,
    PREAMBLE_CHUNKS,
    cmd_cache_demo,
)
from amw.eval.crosscheck import INSUFFICIENT, SAMPLE_FRACTION, UNRELIABLE, VALIDATED
from amw.reporting import DEFAULT_SHADOW_METRIC, SHADOW_METRICS, cmd_scorecard
from amw.shadow import cmd_shadow
from amw.tuning import cmd_ablate
from amw.tuning.optimizer import CUTOFF_SECONDS, TRAINING_EXAMPLES, cmd_optimize

#: The e2e corpus is a *committed fixture*, not a freshly generated dataset.
#: Replay is keyed on the exact request bytes, so a corpus regenerated on the
#: fly would miss every recorded call. It is generated with `naturalise=False`
#: (templates only, no model calls) precisely so it is byte-stable offline.
E2E_DATASET_DIR = Path(__file__).resolve().parent / "tests" / "fixtures" / "e2e" / "datasets"

# subcommand -> (help text, task that implements it)
COMMANDS: dict[str, tuple[str, str]] = {
    "gen": ("generate the synthetic dataset + rubrics", "T06"),
    "phase2": ("baseline eval: Claude vs naive Gemini", "T09"),
    "ablate": ("run the A0-A4 prompt ablation ladder", "T10"),
    "optimize": ("VAIPO rung A4-optimizer (live only)", "P1/S3"),
    "shadow": ("shadow run + disagreement triage", "T11"),
    "crosscheck": ("second-judge cross-check of the judged metric", "P1/S1"),
    "scorecard": ("gates -> verdicts -> markdown report", "T12"),
    "cache-demo": ("live context-caching demo: real cached-token counts", "P1"),
    "e2e": ("full offline pipeline (CI gate)", "T09"),
    "smoke": ("pre-demo health check against live backends", "T16"),
    "adk-demo": ("thin ADK reference app on Gemini (demo only, never evaluated)", "T14-P1"),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cli.py", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    for name, (help_text, task) in COMMANDS.items():
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument(
            "--mode",
            choices=MODES,
            default="replay",
            help="execution mode (default: replay, the zero-credential path)",
        )
        sub.add_argument(
            "--customer",
            default=None,
            help="customer profile under config/customers/ (default: $AMW_CUSTOMER)",
        )
        sub.set_defaults(task=task)

    gen = subparsers.choices["gen"]
    gen.add_argument("-n", type=int, default=None)
    gen.add_argument(
        "--out-dir",
        default=None,
        help="where the JSONL corpus is written (default: datasets/)",
    )
    gen.add_argument(
        "--no-naturalise",
        action="store_true",
        help="skip the Gemini surface-realism pass; templates only, no model calls",
    )

    phase2 = subparsers.choices["phase2"]
    phase2.add_argument("-n", type=int, default=None)
    phase2.add_argument(
        "--subagent",
        action="append",
        choices=SUBAGENTS,
        default=None,
        help="restrict to one subagent (repeatable; default: all three)",
    )
    phase2.add_argument(
        "--variant",
        action="append",
        choices=VARIANTS,
        default=None,
        help="restrict to one arm (repeatable; default: all three)",
    )
    phase2.add_argument(
        "--no-judge",
        action="store_true",
        help="deterministic metrics only; skip the rubric judge",
    )
    phase2.add_argument(
        "--judge-all",
        action="append",
        choices=SUBAGENTS,
        default=None,
        metavar="SUBAGENT",
        help=(
            "judge this subagent on the full corpus instead of the core split "
            "(repeatable). Costs ~70/28 the judge calls for that subagent and "
            "is recorded in the results notes as a deviation from registered "
            "sizing."
        ),
    )
    phase2.add_argument(
        "--dataset-dir",
        default=None,
        help="where to read the corpus from (default: datasets/)",
    )
    phase2.add_argument(
        "--out",
        default=None,
        help="results path (default: artifacts/results/phase2.json)",
    )

    ablate = subparsers.choices["ablate"]
    ablate.add_argument("-n", type=int, default=None)
    ablate.add_argument(
        "--subagent",
        choices=SUBAGENTS,
        default=None,
        help="restrict to one subagent (default: all three)",
    )
    ablate.add_argument(
        "--rung",
        action="append",
        default=None,
        metavar="RUNG",
        help="run one rung only, e.g. A0 (repeatable; default: the whole ladder)",
    )
    ablate.add_argument(
        "--dataset-dir",
        default=None,
        help="where to read the corpus from (default: datasets/)",
    )
    ablate.add_argument(
        "--out",
        default=None,
        help=(
            "results path (default: artifacts/results/ablation_{subagent}.json); "
            "needs --subagent"
        ),
    )
    ablate.add_argument(
        "--no-judge",
        action="store_true",
        help="deterministic metrics only; skip the rubric judge",
    )
    ablate.add_argument(
        "--no-append",
        action="store_true",
        help="overwrite the artifact instead of appending this run's rungs",
    )

    shadow = subparsers.choices["shadow"]
    shadow.add_argument(
        "-n",
        type=int,
        default=None,
        help="compare only the first N items per subagent (core split first)",
    )
    shadow.add_argument(
        "--subagent",
        choices=SUBAGENTS,
        default=None,
        help="restrict to one subagent (default: all three)",
    )
    shadow.add_argument(
        "--baseline-arm",
        choices=VARIANTS,
        default="claude_baseline",
        help="incumbent arm (default: claude_baseline)",
    )
    shadow.add_argument(
        "--candidate-arm",
        choices=VARIANTS,
        default="gemini_tuned_v1",
        help="migration candidate (default: gemini_tuned_v1)",
    )
    shadow.add_argument(
        "--live-slice",
        type=int,
        default=0,
        metavar="N",
        help=(
            "opt in to a genuinely live head-to-head on the first N items per "
            "subagent (max 10; use with --mode live|hybrid). Default 0 = compare "
            "the whole recorded corpus in replay"
        ),
    )
    shadow.add_argument(
        "--dataset-dir",
        default=None,
        help="where to read the corpus from (default: datasets/)",
    )
    shadow.add_argument(
        "--out",
        default=None,
        help="results path (default: artifacts/results/shadow.json)",
    )
    shadow.add_argument(
        "--triage-out",
        default=None,
        help="markdown triage table (default: artifacts/results/shadow_triage.md)",
    )
    shadow.add_argument(
        "--no-judge",
        action="store_true",
        help="skip adjudication; every disagreement is labelled not_adjudicated",
    )
    shadow.add_argument(
        "--phase2",
        default=None,
        help=(
            "phase-2 artifact to read each subagent's judged split from "
            "(default: newest of artifacts/results/phase2_n70.json, phase2.json)"
        ),
    )

    crosscheck = subparsers.choices["crosscheck"]
    crosscheck.add_argument(
        "--results",
        default=None,
        help=(
            "phase-2 artifact being validated; its judged split is the eligible "
            "pool (default: newest of phase2_n70.json, phase2.json)"
        ),
    )
    crosscheck.add_argument(
        "--subagent",
        action="append",
        choices=SUBAGENTS,
        default=None,
        help="restrict to one subagent (repeatable; default: all three)",
    )
    crosscheck.add_argument(
        "--variant",
        action="append",
        choices=VARIANTS,
        default=None,
        help="restrict to one arm (repeatable; default: all three)",
    )
    crosscheck.add_argument(
        "--full",
        action="append",
        choices=SUBAGENTS,
        default=None,
        help=(
            "cross-check this subagent's whole judged split instead of a sample "
            "(repeatable; default: feature_extractor)"
        ),
    )
    crosscheck.add_argument(
        "--fraction",
        type=float,
        default=SAMPLE_FRACTION,
        help=f"sample fraction of the corpus for sampled subagents (default: {SAMPLE_FRACTION})",
    )
    crosscheck.add_argument(
        "--out",
        default=None,
        help="where crosscheck.json is written (default: artifacts/results/)",
    )
    crosscheck.add_argument(
        "--report",
        default=None,
        help=(
            "also write the agreement tables and the largest per-item "
            "disagreements, with both judges' rationales, as markdown"
        ),
    )
    crosscheck.add_argument(
        "--reuse",
        action="store_true",
        help=(
            "render from the existing --out artifact instead of running the "
            "judges again; needs no credentials"
        ),
    )

    scorecard = subparsers.choices["scorecard"]
    scorecard.add_argument(
        "--results",
        default=None,
        help=(
            "phase-2 artifact to score "
            "(default: newest of artifacts/results/phase2_n70.json, phase2.json)"
        ),
    )
    scorecard.add_argument(
        "--out",
        default=None,
        help="write the markdown here (default: stdout)",
    )
    scorecard.add_argument(
        "--dataset-dir",
        default=None,
        help="corpus to replay when re-deriving per-item scores (default: datasets/)",
    )
    scorecard.add_argument(
        "--no-recompute",
        action="store_true",
        help=(
            "skip replaying the arms to recover per-item vectors; the two paired "
            "delta gates then report as not evaluated"
        ),
    )
    scorecard.add_argument(
        "--shadow",
        default=None,
        help="shadow agreement artifact from `cli.py shadow`",
    )
    scorecard.add_argument(
        "--crosscheck",
        default=None,
        help=(
            "second-judge cross-check artifact from `cli.py crosscheck`; adds the "
            "agreement + combination-rule line to the footer"
        ),
    )
    scorecard.add_argument(
        "--shadow-metric",
        choices=SHADOW_METRICS,
        default=DEFAULT_SHADOW_METRIC,
        help=(
            "which agreement figure the shadow_agreement gate is checked "
            "against: `structured` drops prose fields; `item` requires every "
            "field to match and scores prose with a lexical proxy "
            f"(default: {DEFAULT_SHADOW_METRIC})"
        ),
    )
    scorecard.add_argument(
        "--cache-preamble-tokens",
        type=int,
        default=None,
        help="shared preamble size for the context-caching breakeven table",
    )
    scorecard.add_argument(
        "--baseline-variant", default=None, help="default: claude_baseline"
    )
    scorecard.add_argument(
        "--candidate-variant", default=None, help="default: gemini_tuned_v1"
    )
    scorecard.add_argument(
        "--claude-region",
        default=None,
        help="state the baseline's region explicitly (traces carry none)",
    )
    scorecard.add_argument(
        "--gemini-region",
        default=None,
        help="state the candidate's region explicitly (default: the customer profile)",
    )
    # The live volume override. Volumes stay illustrative unless BOTH of these
    # are given; --volume without --volumes-confirmed-by exits 2 rather than
    # recording an unattributed "the customer told us".
    scorecard.add_argument(
        "--volume",
        action="append",
        default=None,
        metavar="SUBAGENT:CALLS_PER_DAY[:AVG_IN:AVG_OUT]",
        help=(
            "customer-stated call volume, entered in-session (repeatable). "
            "Flips the footer to `volumes: customer-provided`. Does NOT unlock "
            "dollar figures while config/pricing.yaml is unverified."
        ),
    )
    scorecard.add_argument(
        "--volumes-confirmed-by",
        default=None,
        metavar="NAME",
        help="who supplied --volume figures; required whenever --volume is used",
    )

    optimize = subparsers.choices["optimize"]
    optimize.add_argument(
        "--subagent",
        choices=SUBAGENTS,
        default=None,
        help=(
            "subagent whose cross-check verdict selects the optimizer target "
            "(default: feature_extractor, the subject of item 1's gate)"
        ),
    )
    optimize.add_argument(
        "--verdict",
        choices=[VALIDATED, UNRELIABLE, INSUFFICIENT],
        default=None,
        help=(
            "override the cross-check artifact; for an owner who has ruled but "
            "not yet re-run `cli.py crosscheck`"
        ),
    )
    optimize.add_argument(
        "--crosscheck",
        default=None,
        help=(
            "cross-check artifact to read the ruling from "
            "(default: artifacts/results/crosscheck.json)"
        ),
    )
    optimize.add_argument(
        "--i-have-the-ruling",
        action="store_true",
        help=(
            "proceed with a retarget the owner has already seen; without it a "
            "retargeted run prints the reframe and refuses"
        ),
    )
    optimize.add_argument(
        "--examples",
        type=int,
        default=None,
        help=f"worked examples sent to the optimizer (default: {TRAINING_EXAMPLES})",
    )
    optimize.add_argument(
        "--cutoff",
        type=float,
        default=None,
        help=(
            "seconds before the hand-tuned rung becomes the candidate "
            f"(default: {CUTOFF_SECONDS:.0f}, the owner's two hours)"
        ),
    )
    optimize.add_argument("-n", type=int, default=None)
    optimize.add_argument("--dataset-dir", default=None, help="default: datasets/")
    optimize.add_argument(
        "--no-ladder",
        action="store_true",
        help="optimize and keep the instruction, but do not run the rung",
    )
    optimize.add_argument(
        "--out",
        default=None,
        help="where optimizer_<subagent>.json is written (default: artifacts/results/)",
    )

    cache_demo = subparsers.choices["cache-demo"]
    cache_demo.add_argument(
        "--subagent",
        choices=SUBAGENTS,
        default=None,
        help=f"whose preamble is cached (default: {DEMO_SUBAGENT})",
    )
    cache_demo.add_argument(
        "--variant",
        choices=ALL_VARIANTS,
        default=None,
        help=f"pack the cached system instruction comes from (default: {DEMO_VARIANT})",
    )
    cache_demo.add_argument(
        "--chunks",
        type=int,
        default=None,
        help=(
            "corpus chunks pooled into the shared preamble; raise it if the "
            f"service rejects the cache as too small (default: {PREAMBLE_CHUNKS})"
        ),
    )
    cache_demo.add_argument(
        "--ttl-hours",
        type=float,
        default=None,
        help="cache TTL, and the window the breakeven is computed over (default: 1.0)",
    )
    cache_demo.add_argument("--dataset-dir", default=None, help="default: datasets/")
    cache_demo.add_argument(
        "--out",
        default=None,
        help="where cache_demo.json is written (default: artifacts/results/)",
    )

    smoke = subparsers.choices["smoke"]
    smoke.add_argument("-n", type=int, default=2)
    smoke.add_argument(
        "--backend",
        choices=("gemini", "claude"),
        default=None,
        help="check one backend only (default: both)",
    )
    smoke.add_argument(
        "--claude-path",
        choices=CLAUDE_PATHS,
        default=None,
        help="override $CLAUDE_PATH for this check (default: $CLAUDE_PATH)",
    )

    adk_demo = subparsers.choices["adk-demo"]
    adk_demo.add_argument(
        "--query",
        action="append",
        default=None,
        metavar="TEXT",
        help=(
            "question to run end to end (repeatable; default: the two sample "
            "queries in amw/agents/adk_app.py)"
        ),
    )
    adk_demo.add_argument(
        "--variant",
        choices=VARIANTS,
        default=None,
        help="prompt pack the leaf agents load (default: gemini_tuned_v1)",
    )

    return parser


def region_warnings(cfg, mode: str) -> list[str]:
    """Flag region settings that would make a report footer misstate the run.

    The footer prints ``cfg.customer.region`` (ground rule 2), but live calls go
    wherever ``$REGION`` / ``$CLAUDE_REGION`` point. If those disagree the
    report claims a region the numbers did not come from — and a cross-region
    Claude/Gemini split additionally makes the ``latency_p95`` gate a
    cross-region comparison, which has to be disclosed rather than silently
    compared. Warn loudly; do not "fix" it by overriding either value, since
    which one is wrong is the operator's call.
    """
    if mode == "replay":
        return []  # nothing is dialled anywhere; the corpus carries its own labels

    profile = cfg.customer.region
    env_region = os.environ.get("REGION")
    claude_region = os.environ.get("CLAUDE_REGION") or env_region

    warnings: list[str] = []
    if env_region and env_region != profile:
        warnings.append(
            f"REGION={env_region} but config/customers/{cfg.customer.customer}.yaml "
            f"says region: {profile}. Gemini calls will run in {env_region} while "
            f"report footers print {profile}. Align them before a customer run."
        )
    if claude_region and env_region and claude_region != env_region:
        warnings.append(
            f"Claude runs in {claude_region}, Gemini in {env_region}. Quality and "
            f"cost gates are unaffected, but latency_p95 becomes a cross-region "
            f"comparison — the scorecard must disclose this, not present it as a "
            f"same-region measurement."
        )
    return warnings


def cmd_gen(args, cfg) -> int:
    from amw.datasets import generate

    result = generate(
        config=cfg,
        n=args.n,
        mode=args.mode,
        out_dir=args.out_dir,
        naturalise=not args.no_naturalise,
    )
    print(result.describe())
    # Why the realism pass declined a passage matters: a rejection means the
    # guard kept the template prose rather than let a rewrite touch the answer
    # key. A silent count invites the reading that those items are damaged.
    for reason in result.rewrite.reasons:
        print(f"  realism pass: {reason}", file=sys.stderr)
    return 0


def _format_value(name: str, point, estimate, n: int, excluded=None) -> str:
    """One metric line. Never prints a number that was not measured.

    Three distinct states, kept distinct on purpose (ground rule 1): nothing
    measurable, a bare mean with no interval (n=1), and a mean with a CI.
    """
    if point is None:
        reason = ", ".join(f"{k}={v}" for k, v in sorted((excluded or {}).items()))
        return f"    {name:28s} not measured ({reason or 'no items'})"
    if estimate is None:
        return f"    {name:28s} {point:.3f}  no CI (n={n})"
    return (
        f"    {name:28s} {estimate.point:.3f}  95% CI "
        f"[{estimate.lo:.3f}, {estimate.hi:.3f}]  n={estimate.n}"
    )


def _print_arm(arm) -> None:
    head = f"{arm.subagent:20s} {arm.variant:18s} {arm.model:14s}"
    calls = f"{arm.calls_ok}/{arm.items} ok"
    if arm.calls_error:
        calls += f", {arm.calls_error} error"
    print(f"{head} {calls}")
    for name, report in arm.metrics.items():
        print(_format_value(name, report.point, report.estimate, report.n, report.excluded))
    if arm.judge is not None:
        # Name the split on screen. Two judge scores with different n are not
        # directly comparable, and the number alone does not say so.
        label = "judge_score" if arm.judge.split == "core" else "judge_score[all]"
        print(
            _format_value(
                label,
                arm.judge.point,
                arm.judge.estimate,
                arm.judge.items_scored,
                {"no_repeat_completed": arm.judge.failed_repeats},
            )
        )


def cmd_phase2(args, cfg) -> int:
    from amw.eval.runner import run_phase2

    result = run_phase2(
        config=cfg,
        mode=args.mode,
        n=args.n,
        subagents=args.subagent,
        variants=args.variant,
        run_judge=not args.no_judge,
        judge_all=args.judge_all or (),
        dataset_dir=args.dataset_dir,
        out_path=args.out,
    )
    for arm in result.arms:
        _print_arm(arm)
    for note in result.notes:
        print(f"note: {note}", file=sys.stderr)
    print(
        f"\nprovenance={result.provenance} seed={result.dataset_seed} "
        f"mode={result.mode} region={result.region}"
    )
    # Ground rule 1: replayed numbers say on screen when the calls were made.
    if result.recorded_from:
        print(
            f"REPLAY — every number above comes from calls recorded "
            f"{result.recorded_from} to {result.recorded_to}, not from a run just now."
        )
    elif result.run_started:
        print(f"run_started={result.run_started}")
    return 0


def cmd_crosscheck(args, cfg) -> int:
    """Re-score recorded outputs with a second, Claude-class judge.

    The generations are never re-run and the gated judge is always replayed;
    only the cross-check judge honours ``--mode``. So `--mode live` here means
    "one new instrument over old data", not "a second measurement run".
    """
    from amw.eval.crosscheck import (
        FULL_CROSSCHECK_SUBAGENTS,
        CrosscheckResult,
        crosscheck_footer_line,
        default_crosscheck_path,
        render_markdown,
        run_crosscheck,
    )
    from amw.reporting.scorecard import _default_results_path

    if args.reuse:
        artifact = Path(args.out) if args.out else default_crosscheck_path()
        if not artifact.is_file():
            print(
                f"error: no cross-check artifact at {artifact}; --reuse renders "
                "an existing one, it does not create it.",
                file=sys.stderr,
            )
            return 2
        result = CrosscheckResult.model_validate_json(artifact.read_text())
    else:
        results_path = Path(args.results) if args.results else _default_results_path()
        if not results_path.is_file():
            print(
                f"error: no phase-2 artifact at {results_path}. The cross-check "
                "validates an existing judged run; run `python cli.py phase2` first.",
                file=sys.stderr,
            )
            return 2

        result = run_crosscheck(
            config=cfg,
            results_path=results_path,
            mode=args.mode,
            subagents=args.subagent,
            variants=args.variant,
            full_scope=tuple(args.full or FULL_CROSSCHECK_SUBAGENTS),
            sample_fraction=args.fraction,
            out_path=args.out,
        )

    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(render_markdown(result), encoding="utf-8")
        print(f"wrote {report_path}\n")

    print(f"gated judge      : {result.gated_judge}")
    print(f"cross-check judge: {result.check_judge}\n")
    for entry in result.subagents:
        overall = entry.overall
        agreement = (
            "not measured"
            if overall.criterion_agreement is None
            else f"{overall.criterion_agreement:.4f}"
        )
        kappa = (
            "undefined" if overall.cohens_kappa is None else f"{overall.cohens_kappa:.4f}"
        )
        print(
            f"{entry.subagent:<18} agreement={agreement}  kappa={kappa}  "
            f"{entry.verdict}  ({overall.criterion_pairs} criterion pairs over "
            f"{overall.paired_cells} cells, {overall.unusable} unusable)"
        )
        print(f"  scope: {entry.scope}")
        if overall.kappa_note:
            print(f"  note: {overall.kappa_note}")
        for arm in entry.per_arm:
            value = (
                "not measured"
                if arm.criterion_agreement is None
                else f"{arm.criterion_agreement:.4f}"
            )
            print(
                f"    {arm.arm:<18} agreement={value}  "
                f"gated mean={_fmt(arm.gated_mean_score)} "
                f"check mean={_fmt(arm.check_mean_score)}"
            )
    for note in result.notes[1:]:  # notes[0] is the combination rule, printed below
        print(f"note: {note}", file=sys.stderr)
    print(f"\n{crosscheck_footer_line(result)}")
    return 0


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"


def cmd_e2e(args, cfg) -> int:
    """The offline CI gate: a tiny committed corpus through the whole path.

    Deliberately runs the *same* code as `phase2`, not a parallel imitation of
    it — a smoke test that exercises a different path proves nothing about the
    path a customer will use. It is small (a fixture corpus, no judge by
    default) so it stays fast enough to run before every commit.
    """
    from amw.eval.runner import run_phase2

    fixture_dir = E2E_DATASET_DIR
    if not fixture_dir.exists():
        print(
            f"e2e fixture corpus missing at {fixture_dir}. It is committed to the "
            f"repo; restore it rather than regenerating, so the replay keys match.",
            file=sys.stderr,
        )
        return 4

    with tempfile.TemporaryDirectory() as tmp:
        result = run_phase2(
            config=cfg,
            mode=args.mode,
            dataset_dir=fixture_dir,
            out_path=Path(tmp) / "phase2.json",
            run_judge=False,
        )

    measured = sum(
        1
        for arm in result.arms
        for report in arm.metrics.values()
        if report.point is not None
    )
    errors = sum(arm.calls_error for arm in result.arms)
    print(f"e2e: {len(result.arms)} arms, {measured} metrics measured, {errors} call errors")
    if not result.arms:
        print("e2e: no arm ran — the fixture corpus is empty", file=sys.stderr)
        return 4
    if errors:
        # In replay a call error means the corpus does not cover this request.
        # That is a broken CI gate, not a passing one: the whole point is that
        # the offline path works with zero credentials.
        print(
            f"e2e: {errors} call(s) did not resolve. In replay this means the "
            f"recorded corpus does not cover them — re-record with "
            f"`python cli.py e2e --mode live`.",
            file=sys.stderr,
        )
        for arm in result.arms:
            for kind, count in arm.error_kinds.items():
                print(f"  {arm.subagent}/{arm.variant}: {kind} x{count}", file=sys.stderr)
        return 5

    # The gate is named "full offline pipeline", so it has to reach the end of
    # the pipeline. Rendering the scorecard here is what catches a report that
    # only assembles when a full n=70 artifact and a shadow run happen to be
    # sitting on disk. No recompute: the fixture run is the evidence, and this
    # is a does-it-render check, not a measurement.
    from amw.reporting.scorecard import build_scorecard, render_markdown

    try:
        markdown = render_markdown(build_scorecard(cfg, result))
    except Exception as exc:  # noqa: BLE001 — any failure here fails the gate
        print(f"e2e: the scorecard did not render: {exc!r}", file=sys.stderr)
        return 6
    print(f"e2e: scorecard rendered, {len(markdown.splitlines())} lines")
    return 0


#: Which prompt-pack variant exercises which backend. Smoke checks a backend by
#: sending the real thing — same pack, same tool, same schema — because a
#: bespoke "hello" probe can pass while the actual request shape 400s.
SMOKE_VARIANT = {"claude": "claude_baseline", "gemini": "gemini_naive"}


def cmd_smoke(args, cfg) -> int:
    """Pre-demo health check: can each backend still take a real request?

    Not an eval. It scores nothing and writes no results — it answers one
    question, "will the demo run", and says which backend broke if not.
    """
    from amw.adapters import AdapterRouter
    from amw.agents.prompt_packs import build_request
    from amw.datasets.schema import read_items
    from amw.eval.runner import prompt_view

    if args.claude_path:
        os.environ["CLAUDE_PATH"] = args.claude_path

    backends = [args.backend] if args.backend else sorted(SMOKE_VARIANT)
    router = AdapterRouter(mode=args.mode, models=cfg.models)
    failures = 0

    for backend in backends:
        variant = SMOKE_VARIANT[backend]
        for subagent in SUBAGENTS:
            path = E2E_DATASET_DIR / f"{subagent}.jsonl"
            items = list(read_items(path))[: args.n]
            requests = [
                build_request(subagent, variant, prompt_view(i), item_id=i.item_id)
                for i in items
            ]
            traces = router.complete_many(requests)
            ok = sum(1 for t in traces if t.status == "ok")
            failures += len(traces) - ok
            line = f"{backend:8s} {subagent:20s} {ok}/{len(traces)} ok"
            if ok < len(traces):
                first = next(t.error for t in traces if t.status != "ok")
                line += f"  — {first[:160]}"
            print(line)

    if failures:
        print(
            f"\nsmoke FAILED: {failures} call(s). Per WORKSHOP_RUNBOOK, fall back to "
            f"`--mode replay` and say on screen that the numbers are the recorded run.",
            file=sys.stderr,
        )
        return 5
    print("\nsmoke OK")
    return 0


def cmd_adk_demo(args, cfg) -> int:
    """The ADK reference app: one query, end to end, on Gemini.

    Demo only. It is not an instrument and it is deliberately unreachable from
    the eval path — phase2/ablate/shadow/scorecard go through amw/adapters/,
    which records every call so the whole pipeline replays offline. ADK's
    runtime has no such hook.

    A one-line lazy import on purpose: amw.agents.adk_app is the only module
    that knows about google-adk, so every other subcommand still works on a
    machine that does not have it (CLAUDE.md ground rule 4).
    """
    from amw.agents.adk_app import cmd_adk_demo as run_adk_demo

    return run_adk_demo(args, cfg)


HANDLERS = {
    "gen": cmd_gen,
    "phase2": cmd_phase2,
    "ablate": cmd_ablate,
    "optimize": cmd_optimize,
    "shadow": cmd_shadow,
    "crosscheck": cmd_crosscheck,
    "scorecard": cmd_scorecard,
    "cache-demo": cmd_cache_demo,
    "e2e": cmd_e2e,
    "smoke": cmd_smoke,
    "adk-demo": cmd_adk_demo,
}


def load_env() -> None:
    """Read `.env` into the environment, without overriding what is already set.

    CLAUDE.md's setup step is `cp .env.example .env`, so the CLI has to honour
    that file or the documented setup does not work. Real environment variables
    win over the file, so `REGION=... python cli.py ...` still overrides for a
    one-off run. Missing file and missing dotenv are both fine — replay mode
    needs neither.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - dotenv ships in requirements.txt
        return
    load_dotenv(Path(__file__).resolve().parent / ".env", override=False)


def main(argv: list[str] | None = None) -> int:
    load_env()
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 1

    try:
        cfg = load_all(customer=args.customer)
    except ConfigError as exc:
        print(f"config error:\n{exc}", file=sys.stderr)
        return 2

    for warning in region_warnings(cfg, args.mode):
        print(f"warning: {warning}", file=sys.stderr)

    handler = HANDLERS.get(args.command)
    if handler is None:
        print(
            f"`{args.command}` is not implemented yet — it lands in {args.task}. "
            "Nothing was run.",
            file=sys.stderr,
        )
        return 3

    try:
        return handler(args, cfg)
    except (ConfigError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
