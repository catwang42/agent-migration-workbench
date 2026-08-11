"""Shadow runner: the same inputs through both backends, paired.

A shadow run is not a second eval. Phase 2 asks "how good is each arm against
the answer key"; the shadow pass asks "if we swapped the backend under the
live pipeline tomorrow, how often would the downstream stage receive something
different, and when it did, who was right". So it produces three things:

* paired traces — one item, two arms, side by side;
* the agreement rate the ``shadow_agreement`` gate is checked on
  (:mod:`amw.shadow.agreement`);
* a judge-adjudicated triage of every disagreement
  (:mod:`amw.shadow.triage`).

Why the default path makes no model calls
-----------------------------------------

Every output this compares **already exists**. The n=70 baseline
(``notes/phase2_n70_validation.md``) ran all three arms over all three
subagents live on 2026-08-09/10 and, because record-on-live has no off switch
(ground rule 5), all 630 generation calls and the 785 judge calls that scored
them are on disk in ``artifacts/replay/`` (1,486 traces in that window, 0
errors). Re-running them live would spend money to re-measure a
constant and would change the numbers under the scorecard for no gain. In
``--mode replay`` this module resolves entirely from
:class:`~amw.traces.store.ReplayStore`.

Two consequences worth naming, because the T11 card assumes otherwise:

**Concurrency is moot here.** The card says "both backends concurrently
(asyncio)". Replaying is a dict lookup against an in-memory index of local
JSONL — there is no IO to overlap, and an event loop would add a failure mode
(and an un-orderable trace stream) to buy nothing. Concurrency would only pay
on ``--live-slice``, which is at most a few dozen calls on a stage; it is left
sequential so the demo prints in item order. No asyncio is used anywhere in
this package.

**Latency here is cross-region.** In the recorded corpus Claude ran in
``global`` because us-central1 Model Garden quota for Claude was exhausted,
while Gemini ran in ``us-central1``. Every latency number this module emits
carries :attr:`LatencyStats.disclosure` saying so, and the trace schema carries
no region field, so the region label is read from configuration
(``$CLAUDE_REGION`` / ``$REGION`` / the customer profile) and is labelled with
where it was read from. A p95 comparison across two regions is not a
same-region measurement and must never be presented as one.

The live slice
--------------

``--live-slice N`` is the in-session demo moment: the first N items per
subagent, both backends, genuinely called. It is opt-in, it is capped
(:data:`LIVE_SLICE_MAX`), and it goes through the ordinary adapter router — so
record-on-live applies automatically and there is no bypass to add. The judge
is a separate matter: see :data:`JUDGE_MODE`.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from amw.adapters import AdapterRouter, merge_windows
from amw.agents.prompt_packs import VARIANTS, build_request
from amw.agents.schemas import SUBAGENTS
from amw.config import AppConfig, ConfigError, ModelsConfig, load_all
from amw.datasets.schema import DatasetItem, read_items
from amw.eval.judge import Judge
from amw.eval.runner import prompt_view
from amw.shadow.agreement import (
    DEFAULT_PROSE_THRESHOLD,
    LEXICAL_SIMILARITY_NAME,
    ItemAgreement,
    SimilarityFn,
    SubagentAgreement,
    aggregate_agreement,
    compare_item,
    lexical_similarity,
)
from amw.shadow.triage import (
    TriageRow,
    TriageSummary,
    adjudicate,
    summarize,
    triage_table_markdown,
)
from amw.traces.schema import Trace
from amw.traces.store import ReplayMissError

__all__ = [
    "SHADOW_VERSION",
    "DEFAULT_BASELINE_ARM",
    "DEFAULT_CANDIDATE_ARM",
    "JUDGE_MODE",
    "LIVE_SLICE_MAX",
    "LatencyStats",
    "ArmShadow",
    "SubagentShadow",
    "ShadowResult",
    "percentile",
    "arm_region",
    "load_items",
    "run_shadow",
    "default_results_path",
    "default_triage_path",
]

#: Bumped when the result shape changes, so a stale shadow.json is detectable.
SHADOW_VERSION = "1"

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The incumbent, and the arm a migration would actually ship. The tuned arm is
#: the default candidate because that is the thing under consideration; the
#: naive arm (A0) stays selectable so a workshop can show the untuned swap.
DEFAULT_BASELINE_ARM = "claude_baseline"
DEFAULT_CANDIDATE_ARM = "gemini_tuned_v1"

#: The judge is **always** replayed, whatever mode the arms run in.
#:
#: Triage adjudicates from the judge calls phase 2 already made (ground rule 1:
#: a calculation over recorded calls). Letting the judge go live here would
#: silently issue ~4 new Pro-model calls per disagreement — on stage, mid-demo,
#: at a cost nobody budgeted — and would produce verdicts that are not the
#: verdicts the scorecard's quality numbers came from. A live slice's fresh
#: outputs simply have no recorded verdict, and are labelled `not_adjudicated`.
JUDGE_MODE = "replay"

#: Hard cap on ``--live-slice``. Five items x 2 backends x 3 subagents = 30
#: calls, which is the demo. Anything much larger is a phase-2 run wearing a
#: flag, and would burn the workshop's clock and quota by accident.
LIVE_SLICE_MAX = 10

#: Where the split each subagent was judged on is read from, in order. The
#: split is a property of the *recorded run*, not a policy this module gets to
#: assert, so it is read off the artifact rather than hardcoded here.
PHASE2_ARTIFACTS: tuple[str, ...] = ("phase2_n70.json", "phase2.json")


def default_results_path() -> Path:
    return REPO_ROOT / "artifacts" / "results" / "shadow.json"


def default_triage_path() -> Path:
    return REPO_ROOT / "artifacts" / "results" / "shadow_triage.md"


def default_dataset_dir() -> Path:
    return REPO_ROOT / "datasets"


def default_phase2_path() -> Path | None:
    """The most recent phase-2 artifact on disk, or None."""
    for name in PHASE2_ARTIFACTS:
        path = REPO_ROOT / "artifacts" / "results" / name
        if path.is_file():
            return path
    return None


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# latency
# --------------------------------------------------------------------------


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolated percentile, or None for an empty sample.

    None rather than 0.0: an arm whose traces recorded no latency was not
    infinitely fast, it was not measured (ground rule 1). ``numpy``'s default
    linear interpolation is used so the number is reproducible with one line of
    anyone else's code.
    """
    data = [float(v) for v in values if v is not None]
    if not data:
        return None
    return float(np.percentile(np.asarray(data, dtype=float), q))


def arm_region(
    model_key: str, *, models: ModelsConfig, customer_region: str
) -> tuple[str, str]:
    """``(region, where that came from)`` for one model key.

    Traces carry no region field, so this cannot be read off the recording; it
    is read from the same environment the run was configured with, and the
    source is returned alongside so a report can print "us-central1
    (config/customers/demo_patents.yaml)" rather than an unsourced label.

    Claude gets its own variable because quota is per-region *and*
    per-base-model: this project serves Gemini in us-central1 while Claude only
    works in ``global`` (see ``.env.example``). The precedence mirrors
    :class:`~amw.adapters.claude_vertex.ClaudeVertexAdapter` exactly, so the
    label matches where the call would actually go.

    When the Claude arm has no region of its own, the source string says so.
    Without that, an unset ``CLAUDE_REGION`` would make both arms report the
    same region and the report would claim a same-region latency comparison it
    has not established.
    """
    spec = models.spec(model_key)
    if spec.provider == "anthropic":
        # ClaudeVertexAdapter's order, including CLOUD_ML_REGION, which the
        # Gemini adapter does not consult.
        order = ("CLAUDE_REGION", "REGION", "CLOUD_ML_REGION")
        suffix = (
            ""
            if os.environ.get("CLAUDE_REGION")
            else " (no CLAUDE_REGION set — unverified for the Claude arm)"
        )
    else:
        order = ("REGION",)
        suffix = ""
    for name in order:
        region = os.environ.get(name)
        if region:
            return region, f"env:{name}" + ("" if name == "CLAUDE_REGION" else suffix)
    return customer_region, f"config/customers/*.yaml:region{suffix}"


class LatencyStats(_Strict):
    """p50/p95 for one arm, with the region caveat welded on.

    ``disclosure`` is not optional prose: ``config/gates.yaml`` bounds
    ``latency_p95`` by the ``claude_baseline_p95`` sentinel with the basis
    "same region, same load profile", and this corpus is not same-region. A
    consumer that prints a number from this object without the disclosure is
    misreporting it.
    """

    arm: str
    model: str
    region: str
    region_source: str
    n_traces: int
    n_with_total: int
    n_with_ttft: int
    ttft_p50_ms: float | None = None
    ttft_p95_ms: float | None = None
    total_p50_ms: float | None = None
    total_p95_ms: float | None = None
    disclosure: str

    def describe(self) -> str:
        def fmt(value: float | None) -> str:
            return "—" if value is None else f"{value:,.0f}ms"

        return (
            f"{self.arm:18s} ttft p50 {fmt(self.ttft_p50_ms)} p95 "
            f"{fmt(self.ttft_p95_ms)} | total p50 {fmt(self.total_p50_ms)} p95 "
            f"{fmt(self.total_p95_ms)} | region {self.region} "
            f"({self.region_source}), n={self.n_with_total}"
        )


def _latency_stats(
    arm: str,
    model: str,
    traces: Sequence[Trace],
    *,
    region: str,
    region_source: str,
    disclosure: str,
) -> LatencyStats:
    totals = [t.latency_ms.total for t in traces if t.latency_ms.total is not None]
    ttfts = [t.latency_ms.ttft for t in traces if t.latency_ms.ttft is not None]
    return LatencyStats(
        arm=arm,
        model=model,
        region=region,
        region_source=region_source,
        n_traces=len(traces),
        n_with_total=len(totals),
        n_with_ttft=len(ttfts),
        ttft_p50_ms=percentile(ttfts, 50),
        ttft_p95_ms=percentile(ttfts, 95),
        total_p50_ms=percentile(totals, 50),
        total_p95_ms=percentile(totals, 95),
        disclosure=disclosure,
    )


def _latency_disclosure(
    *,
    baseline_region: str,
    candidate_region: str,
    baseline_model: str,
    candidate_model: str,
    baseline_source: str = "",
) -> str:
    if baseline_region != candidate_region:
        return (
            f"CROSS-REGION: {baseline_model} ran in {baseline_region}, "
            f"{candidate_model} in {candidate_region}. Latency here compares two "
            f"regions and is not a like-for-like p95; the gates.yaml basis for "
            f"latency_p95 asks for same region, same load profile. Regions are "
            f"read from configuration — traces carry no region field."
        )
    if "unverified" in baseline_source:
        return (
            f"REGION UNVERIFIED: both arms are labelled {baseline_region} only "
            f"because no CLAUDE_REGION is configured. This project's Claude "
            f"quota lives outside the Gemini region (see .env.example), so this "
            f"p95 pair may well be cross-region. Set CLAUDE_REGION before "
            f"presenting it as like-for-like — traces carry no region field, so "
            f"nothing here can confirm it after the fact."
        )
    return (
        f"Same region ({baseline_region}) for both arms, per configuration — "
        f"traces carry no region field, so this label describes how the run was "
        f"configured, not something recorded in the trace."
    )


# --------------------------------------------------------------------------
# result shapes
# --------------------------------------------------------------------------


class ArmShadow(_Strict):
    """One arm of the pair: what ran, and how it went."""

    arm: str
    model: str
    calls_ok: int
    calls_error: int
    latency: LatencyStats


class SubagentShadow(_Strict):
    """One subagent: paired arms, agreement, triage."""

    subagent: str
    items: int
    baseline: ArmShadow
    candidate: ArmShadow
    agreement: SubagentAgreement
    triage: list[TriageRow] = Field(default_factory=list)
    triage_summary: TriageSummary
    #: Which split phase 2 judged this subagent on, and how many of its items
    #: are in it. The denominator behind every adjudicated verdict.
    judged_split: str
    judged_items: int


class ShadowResult(_Strict):
    """Everything ``cli.py shadow`` produced, including how it was produced."""

    shadow_version: str = SHADOW_VERSION
    customer: str
    mode: str
    #: The judge is pinned to replay; recorded so a reader is not left to infer
    #: it from `mode`.
    judge_mode: str = JUDGE_MODE
    region: str
    provenance: str
    dataset_seed: int
    generator_version: str
    bootstrap_seed: int
    judge_repeats: int
    baseline_arm: str
    candidate_arm: str
    similarity_metric: str = LEXICAL_SIMILARITY_NAME
    prose_threshold: float = DEFAULT_PROSE_THRESHOLD
    live_slice: int = 0
    #: None in replay: no wall-clock run happened, the corpus carries its dates.
    run_started: str | None = None
    #: Span of the recordings this run actually served (ground rule 1).
    recorded_from: str | None = None
    recorded_to: str | None = None
    adapters: dict[str, str] = Field(default_factory=dict)
    judge_model: str | None = None
    judge_prompt_version: str | None = None
    subagents: list[SubagentShadow] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def triage_rows(self) -> list[TriageRow]:
        return [row for s in self.subagents for row in s.triage]

    def triage_markdown(self) -> str:
        return triage_table_markdown(
            self.triage_rows(),
            summaries=[s.triage_summary for s in self.subagents],
        )


# --------------------------------------------------------------------------
# dataset loading
# --------------------------------------------------------------------------


def load_items(
    subagent: str, *, dataset_dir: Path, limit: int | None = None
) -> list[DatasetItem]:
    """Read one subagent's corpus, core split first.

    Mirrors the ordering ``amw.eval.runner`` uses for ``-n`` (core items first,
    so a small run still has judgeable items). Duplicated rather than imported
    because that helper is private to the eval lane; if it is ever exported,
    this should call it instead.
    """
    path = dataset_dir / f"{subagent}.jsonl"
    if not path.exists():
        raise FileNotFoundError(
            f"no dataset at {path}. Run `python cli.py gen --customer <name> -n 70` "
            f"first — the shadow pass compares a corpus, it does not create one."
        )
    items = list(read_items(path))
    if limit is None:
        return items
    core = [item for item in items if item.core]
    rest = [item for item in items if not item.core]
    return (core + rest)[:limit]


def judged_splits(path: Path | None) -> dict[str, str]:
    """``subagent -> "core" | "all"`` as recorded in a phase-2 artifact.

    Phase 2 judged QR and CS on the core split and FE on the full corpus, and
    wrote that per arm into ``JudgeReport.split``. Triage has to know it to
    label the items it cannot adjudicate, and reading it from the artifact
    means the label follows the run rather than a constant in this file that
    could quietly go stale.
    """
    if path is None or not Path(path).is_file():
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    splits: dict[str, str] = {}
    for arm in data.get("arms", []):
        judge = arm.get("judge") or {}
        split = judge.get("split")
        if split:
            # "all" anywhere for a subagent wins: judging is per subagent, and
            # a wider split covers a narrower one.
            if splits.get(arm["subagent"]) != "all":
                splits[arm["subagent"]] = split
    return splits


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------


def _run_arm(
    subagent: str,
    arm: str,
    items: Sequence[DatasetItem],
    *,
    router: AdapterRouter,
    models: ModelsConfig,
) -> tuple[str, list[Trace]]:
    """One arm over ``items``. Sequential — see the module docstring on asyncio."""
    requests = [
        build_request(subagent, arm, prompt_view(item), models=models, item_id=item.item_id)
        for item in items
    ]
    traces = router.complete_many(requests)
    model = requests[0].model if requests else arm
    return model, list(traces)


def run_shadow(
    *,
    customer: str | None = None,
    mode: str = "replay",
    config: AppConfig | None = None,
    subagents: Sequence[str] | None = None,
    baseline_arm: str = DEFAULT_BASELINE_ARM,
    candidate_arm: str = DEFAULT_CANDIDATE_ARM,
    n: int | None = None,
    live_slice: int = 0,
    dataset_dir: str | Path | None = None,
    out_path: str | Path | None = None,
    triage_path: str | Path | None = None,
    write: bool = True,
    router: AdapterRouter | None = None,
    judge: Judge | None = None,
    run_judge: bool = True,
    phase2_path: str | Path | None = None,
    bootstrap_seed: int | None = None,
    prose_similarity: SimilarityFn = lexical_similarity,
    prose_threshold: float = DEFAULT_PROSE_THRESHOLD,
) -> ShadowResult:
    """Run the shadow comparison and write ``artifacts/results/shadow.json``.

    :param live_slice: 0 (default) compares the whole corpus. A positive N
        restricts the run to the first N items per subagent — the on-stage
        head-to-head. It does not change *how* calls are made: that is
        ``mode``. Record-on-live still applies, through the same router every
        other runner uses.
    :param judge: injected for tests. Otherwise a judge pinned to
        :data:`JUDGE_MODE`, because triage reads recorded verdicts and must not
        issue new ones.
    """
    cfg = config or load_all(customer=customer)
    subagents = tuple(subagents or SUBAGENTS)
    for arm in (baseline_arm, candidate_arm):
        if arm not in VARIANTS:
            raise ConfigError(
                f"unknown arm {arm!r}; config/models.yaml roles are wired to "
                f"variants {list(VARIANTS)}"
            )
    if baseline_arm == candidate_arm:
        raise ConfigError(
            f"baseline and candidate are both {baseline_arm!r}; a shadow run "
            "compares two backends"
        )
    if live_slice < 0:
        raise ConfigError("--live-slice must be >= 0")
    if live_slice > LIVE_SLICE_MAX:
        raise ConfigError(
            f"--live-slice {live_slice} exceeds the cap of {LIVE_SLICE_MAX}. The "
            f"slice is a demo of a live head-to-head, not a re-run of the "
            f"baseline: {live_slice} x 2 arms x {len(subagents)} subagents is "
            f"{live_slice * 2 * len(subagents)} live calls. Use `cli.py phase2` "
            "if a full live run is really what you want."
        )

    dataset_dir = Path(dataset_dir) if dataset_dir else default_dataset_dir()
    seed = bootstrap_seed if bootstrap_seed is not None else cfg.customer.seed
    repeats = cfg.customer.dataset.judge_repeats
    router = router or AdapterRouter(mode=mode, models=cfg.models)
    if judge is None and run_judge:
        # Pinned to replay on purpose (JUDGE_MODE). Not a mode the caller picks.
        judge = Judge(mode=JUDGE_MODE, models=cfg.models)
    elif not run_judge:
        judge = None

    splits = judged_splits(
        Path(phase2_path) if phase2_path else default_phase2_path()
    )

    notes: list[str] = [
        f"item-level agreement counts an item as agreeing only if EVERY field "
        f"matches, and prose fields are matched with {LEXICAL_SIMILARITY_NAME} "
        f">= {prose_threshold} — a lexical proxy, not an embedding cosine and "
        f"not a semantic judgement. Two faithful paraphrases score as a "
        f"disagreement. Read `structured_agreement` (prose fields dropped) as "
        f"the proxy-free sensitivity, and the judge-adjudicated triage as the "
        f"authoritative verdict on prose."
    ]
    limit = live_slice or n
    datasets = {
        subagent: load_items(subagent, dataset_dir=dataset_dir, limit=limit)
        for subagent in subagents
    }
    seeds = {item.seed for items in datasets.values() for item in items}
    versions = {item.generator_version for items in datasets.values() for item in items}
    provenances = {item.provenance for items in datasets.values() for item in items}

    if live_slice:
        notes.append(
            f"--live-slice {live_slice}: only the first {live_slice} item(s) per "
            f"subagent were compared, in mode={mode}. This is a demonstration "
            f"slice, not the measured population — the agreement rate below is "
            f"over {live_slice} items and its CI will be correspondingly wide."
        )
        if mode == "replay":
            notes.append(
                "--live-slice was given with --mode replay, so nothing was called "
                "live; the slice replayed recorded calls."
            )
    if judge is not None and (live_slice or mode != "replay"):
        notes.append(
            f"the judge runs in {JUDGE_MODE} whatever the arms do, so triage "
            f"adjudicates only outputs that already have a recorded judge call. "
            f"Freshly generated outputs come back as not_adjudicated."
        )
    if len(seeds) > 1:
        notes.append(
            f"dataset seeds disagree across subagents ({sorted(seeds)}) — the "
            f"corpus was generated in more than one run."
        )
    if not splits:
        notes.append(
            "no phase-2 artifact was found, so the judged split per subagent is "
            "unknown; every item is treated as core-split for labelling."
        )

    result = ShadowResult(
        customer=cfg.customer.customer,
        mode=mode,
        region=cfg.customer.region,
        provenance="/".join(sorted(provenances)) if provenances else "unknown",
        dataset_seed=min(seeds) if seeds else cfg.customer.seed,
        generator_version="/".join(sorted(versions)) if versions else "unknown",
        bootstrap_seed=seed,
        judge_repeats=repeats,
        baseline_arm=baseline_arm,
        candidate_arm=candidate_arm,
        prose_threshold=prose_threshold,
        live_slice=live_slice,
        adapters=router.describe(),
        judge_model=judge.describe().get("judge_model") if judge else None,
        judge_prompt_version=(
            judge.describe().get("judge_prompt_version") if judge else None
        ),
        run_started=(
            datetime.now(timezone.utc).isoformat(timespec="seconds")
            if mode != "replay"
            else None
        ),
        notes=notes,
    )

    for subagent in subagents:
        items = datasets[subagent]
        if not items:
            notes.append(f"{subagent}: dataset is empty, nothing was compared")
            continue
        result.subagents.append(
            _shadow_one(
                subagent,
                items,
                cfg=cfg,
                router=router,
                judge=judge,
                repeats=repeats,
                baseline_arm=baseline_arm,
                candidate_arm=candidate_arm,
                seed=seed,
                split=splits.get(subagent, "core"),
                prose_similarity=prose_similarity,
                prose_threshold=prose_threshold,
            )
        )

    # Stamp after the arms have run: the window must describe the calls this
    # run served, not everything in the store. Same defect, same fix, as
    # run_phase2 (see notes/phase2_n70_validation.md).
    window = merge_windows(
        [
            router.served_window(),
            getattr(getattr(judge, "adapter", None), "served_window", lambda: None)(),
        ]
    )
    if window is not None:
        result.recorded_from = window[0].isoformat(timespec="seconds")
        result.recorded_to = window[1].isoformat(timespec="seconds")
    elif mode == "replay":
        notes.append(
            "replay mode served no recordings — no number here is a measurement."
        )

    if write:
        path = Path(out_path) if out_path else default_results_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(result.model_dump_json(indent=2) + "\n")
        table = Path(triage_path) if triage_path else default_triage_path()
        table.parent.mkdir(parents=True, exist_ok=True)
        table.write_text(result.triage_markdown())
    return result


def _shadow_one(
    subagent: str,
    items: Sequence[DatasetItem],
    *,
    cfg: AppConfig,
    router: AdapterRouter,
    judge: Judge | None,
    repeats: int,
    baseline_arm: str,
    candidate_arm: str,
    seed: int,
    split: str,
    prose_similarity: SimilarityFn,
    prose_threshold: float,
) -> SubagentShadow:
    """One subagent through both arms, compared and triaged."""
    baseline_model, baseline_traces = _run_arm(
        subagent, baseline_arm, items, router=router, models=cfg.models
    )
    candidate_model, candidate_traces = _run_arm(
        subagent, candidate_arm, items, router=router, models=cfg.models
    )

    baseline_region, baseline_source = arm_region(
        baseline_model, models=cfg.models, customer_region=cfg.customer.region
    )
    candidate_region, candidate_source = arm_region(
        candidate_model, models=cfg.models, customer_region=cfg.customer.region
    )
    disclosure = _latency_disclosure(
        baseline_region=baseline_region,
        candidate_region=candidate_region,
        baseline_model=baseline_model,
        candidate_model=candidate_model,
        baseline_source=baseline_source,
    )

    by_item_baseline = {item.item_id: t for item, t in zip(items, baseline_traces)}
    by_item_candidate = {item.item_id: t for item, t in zip(items, candidate_traces)}

    comparisons: list[ItemAgreement] = [
        compare_item(
            subagent,
            item.item_id,
            by_item_baseline[item.item_id],
            by_item_candidate[item.item_id],
            prose_similarity=prose_similarity,
            prose_threshold=prose_threshold,
        )
        for item in items
    ]
    agreement = aggregate_agreement(
        comparisons,
        subagent=subagent,
        baseline_arm=baseline_arm,
        candidate_arm=candidate_arm,
        seed=seed,
        prose_threshold=prose_threshold,
    )

    judged_items = [
        item.item_id for item in items if split == "all" or item.core
    ]
    disagreements = [c for c in comparisons if c.comparable and not c.agreed]
    rows = adjudicate(
        subagent,
        disagreements,
        items={item.item_id: item for item in items},
        baseline_traces=by_item_baseline,
        candidate_traces=by_item_candidate,
        baseline_arm=baseline_arm,
        candidate_arm=candidate_arm,
        judge=judge,
        repeats=repeats,
        judged_items=judged_items,
        judged_split=split,
    )

    return SubagentShadow(
        subagent=subagent,
        items=len(items),
        baseline=ArmShadow(
            arm=baseline_arm,
            model=baseline_model,
            calls_ok=sum(1 for t in baseline_traces if t.status == "ok"),
            calls_error=sum(1 for t in baseline_traces if t.status != "ok"),
            latency=_latency_stats(
                baseline_arm,
                baseline_model,
                baseline_traces,
                region=baseline_region,
                region_source=baseline_source,
                disclosure=disclosure,
            ),
        ),
        candidate=ArmShadow(
            arm=candidate_arm,
            model=candidate_model,
            calls_ok=sum(1 for t in candidate_traces if t.status == "ok"),
            calls_error=sum(1 for t in candidate_traces if t.status != "ok"),
            latency=_latency_stats(
                candidate_arm,
                candidate_model,
                candidate_traces,
                region=candidate_region,
                region_source=candidate_source,
                disclosure=disclosure,
            ),
        ),
        agreement=agreement,
        triage=rows,
        triage_summary=summarize(subagent, rows),
        judged_split=split,
        judged_items=len(judged_items),
    )


# --------------------------------------------------------------------------
# CLI entry point
# --------------------------------------------------------------------------


def _print_subagent(shadow: SubagentShadow) -> None:
    agreement = shadow.agreement
    print(f"{shadow.subagent:20s} {shadow.items} items")
    if agreement.agreement is not None:
        estimate = agreement.agreement
        print(
            f"    shadow_agreement            {estimate.point:.3f}  95% CI "
            f"[{estimate.lo:.3f}, {estimate.hi:.3f}]  n={estimate.n}"
        )
    elif agreement.point is not None:
        print(
            f"    shadow_agreement            {agreement.point:.3f}  no CI "
            f"({agreement.no_interval_reason})"
        )
    else:
        print(
            f"    shadow_agreement            not measured "
            f"({agreement.excluded or 'no items'})"
        )
    if agreement.structured_point is not None:
        print(
            f"    structured fields only      {agreement.structured_point:.3f}  "
            f"(prose excluded; prose proxy = {agreement.similarity_metric} "
            f">= {agreement.prose_threshold})"
        )
    worst = sorted(agreement.field_rates, key=lambda r: r.rate)[:3]
    if worst:
        print(
            "    lowest-agreement fields     "
            + ", ".join(f"{r.field} {r.rate:.2f}" for r in worst)
        )
    for arm in (shadow.baseline, shadow.candidate):
        print(f"    {arm.latency.describe()}")
    summary = shadow.triage_summary
    print(
        f"    triage                      {summary.disagreements} disagreement(s): "
        f"{summary.wins} win / {summary.losses} loss / {summary.ties} tie / "
        f"{summary.not_adjudicated} not adjudicated "
        f"[judged split: {shadow.judged_split}, {shadow.judged_items} items]"
    )


def cmd_shadow(args, cfg) -> int:
    """``python cli.py shadow`` — same ``(args, cfg)`` shape as ``cmd_phase2``.

    Every extra argument is read with a default, so the command works before
    the main session has wired all of its flags.
    """
    import sys

    def opt(name: str, default: Any = None) -> Any:
        return getattr(args, name, default)

    # `--subagent query_rewriter` arrives as a bare string; iterating it would
    # ask for one shadow run per *letter*.
    subagent = opt("subagent")
    subagents = (subagent,) if isinstance(subagent, str) else subagent

    try:
        result = run_shadow(
            config=cfg,
            mode=args.mode,
            n=opt("n"),
            subagents=subagents,
            baseline_arm=opt("baseline_arm") or DEFAULT_BASELINE_ARM,
            candidate_arm=opt("candidate_arm") or DEFAULT_CANDIDATE_ARM,
            live_slice=opt("live_slice") or 0,
            dataset_dir=opt("dataset_dir"),
            out_path=opt("out"),
            triage_path=opt("triage_out"),
            run_judge=not opt("no_judge", False),
            phase2_path=opt("phase2"),
        )
    except ReplayMissError as exc:
        # In replay this means the corpus does not cover the pair being asked
        # for — the honest answer is to say which call is missing, not to
        # compare the items that happen to be there.
        print(
            f"replay miss: {exc}\nThe shadow pass compares recorded calls; "
            f"re-record with `python cli.py phase2 --mode live` or pick arms "
            f"that were recorded.",
            file=sys.stderr,
        )
        return 5

    for shadow in result.subagents:
        _print_subagent(shadow)
    for note in result.notes:
        print(f"note: {note}", file=sys.stderr)

    disclosures = {s.baseline.latency.disclosure for s in result.subagents}
    for disclosure in sorted(disclosures):
        print(f"\nlatency: {disclosure}")

    print(
        f"\nprovenance={result.provenance} seed={result.dataset_seed} "
        f"mode={result.mode} judge_mode={result.judge_mode} "
        f"region={result.region}"
    )
    print(
        f"prose fields compared with {result.similarity_metric} >= "
        f"{result.prose_threshold} (a lexical proxy, NOT an embedding cosine); "
        f"the authoritative prose comparison is the judge adjudication in the "
        f"triage table."
    )
    if result.recorded_from:
        print(
            f"REPLAY — every number above comes from calls recorded "
            f"{result.recorded_from} to {result.recorded_to}, not from a run just now."
        )
    elif result.run_started:
        print(f"run_started={result.run_started}")
    return 0
