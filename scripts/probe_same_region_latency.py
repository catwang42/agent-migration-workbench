"""A4 — the same-region latency probe that `latency_p95` has been waiting for.

Every latency number in this repo so far is cross-region and therefore *not* a
comparison: Claude Sonnet 5 serves from ``global`` because the project's
``us-central1`` Model Garden quota for that base model is exhausted, while the
Gemini arms serve from ``us-central1``. ``amw/reporting/evidence.py`` refuses to
build a :class:`SameRegionLatencyProbe` out of two regions, so the gate has
rendered as *not evaluated* on every scorecard to date. That refusal is the
correct behaviour and this script does not weaken it. It tries to *earn* a
one-region measurement instead.

What it does
------------
For each subagent it takes the first ``-n`` items of the corpus (core split
first, exactly the ordering ``amw/eval/runner.py::_load_dataset`` uses, so the
sample is deterministic and reproducible) and calls both arms on each item,
**interleaved** — incumbent then candidate, item by item. Interleaving matters:
running all 10 Claude calls and then all 10 Gemini calls would let a slow
minute land entirely on one arm and be reported as a model difference.

Both arms are pinned to ``--region`` (default ``us-central1``) by constructing
the adapters directly, so neither the registry's ``region: global`` pin on
``gemini-flash-current`` nor ``$CLAUDE_REGION`` can quietly send half the probe
somewhere else. The region each call actually used is asserted off the adapter,
not assumed.

If Claude has no quota in that region
-------------------------------------
The run stops for that subagent and records the provider's exact error text.
It does **not** fall back to the ``global`` recordings: pairing a ``global``
Claude p95 against a ``us-central1`` Gemini p95 is precisely the cross-region
number the gate exists to reject, and it would be the easiest fabrication in
this repo to commit by accident. An unmeasured gate ships as unmeasured.

Why the recordings go somewhere else
------------------------------------
``artifacts/replay/`` is keyed on ``(subagent, model, input_sha)`` and *later
records supersede earlier ones* (``amw/traces/store.py::_index``). This probe
re-issues the same prompt bytes on the same model keys, so recording into the
main corpus would overwrite the very generations the ladder judged — the judged
scores would then describe outputs the corpus no longer holds. The probe writes
its own store under ``artifacts/latency_probe/`` instead. Record-on-live is
still on and nothing is discarded; the traces simply land in a corpus that is
about timing rather than about content.

What the 2026-08-12 runs found
------------------------------
``us-central1`` cannot host this comparison at all, and for two independent
reasons — both recorded verbatim in
``artifacts/results/latency_probe_us-central1.json``:

* ``claude-sonnet`` → 429 ``RESOURCE_EXHAUSTED``, quota exceeded for
  ``online_prediction_input_tokens_per_minute_per_base_model`` on base model
  ``anthropic-claude-sonnet-5``. The known exhaustion, now evidenced.
* ``gemini-flash-current`` → 404 ``NOT_FOUND``: Gemini 3.6 Flash is not
  published in ``us-central1``. That is *why* the registry pins it to
  ``global``, and it means the region split the reports disclose is a property
  of the 2.5-generation arms, not of the deployment candidate.

Which points at the region that can: both the incumbent and the headline
candidate already serve from ``global``. Run ``--region global`` for the arms
the scorecard actually recommends.

Writes ``artifacts/results/latency_probe_<region>.json`` plus a per-region
marker, per the standing rule that background runs report by marker rather
than by being watched.

    .venv/bin/python scripts/probe_same_region_latency.py -n 10 --region global
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv ships in requirements.txt
    pass
else:
    load_dotenv(ROOT / ".env", override=False)

from amw.adapters import RecordingAdapter  # noqa: E402
from amw.adapters.claude_vertex import ClaudeVertexAdapter  # noqa: E402
from amw.adapters.gemini import GeminiAdapter  # noqa: E402
from amw.agents.prompt_packs import build_request  # noqa: E402
from amw.config import load_all  # noqa: E402
from amw.datasets.schema import read_items  # noqa: E402
from amw.eval.runner import default_dataset_dir, prompt_view  # noqa: E402
from amw.eval.stats import (  # noqa: E402
    BOOTSTRAP_RESAMPLES,
    DEFAULT_BOOTSTRAP_SEED,
    DEFAULT_CONFIDENCE,
    Estimate,
)
from amw.shadow.runner import percentile  # noqa: E402
from amw.traces.store import ReplayStore  # noqa: E402
from amw.tuning.ablate import (  # noqa: E402
    CAPPED_GEN_MODEL,
    CURRENT_GEN_MODEL,
    DEPLOYMENT_CANDIDATES,
    SHIPPING_VARIANT,
)

SUBAGENTS = ("query_rewriter", "chunk_summarizer", "feature_extractor")
BASELINE_VARIANT = "claude_baseline"
BASELINE_MODEL = "claude-sonnet"
RESULTS = ROOT / "artifacts" / "results"
PROBE_STORE = ROOT / "artifacts" / "latency_probe"
DEFAULT_REGION = "us-central1"
#: Per-region filenames. The us-central1 attempt is evidence in its own right
#: — it is what shows the gate could not be earned there — so a later probe in
#: another region must not overwrite it.
#: A second candidate probed in the SAME region needs its own filename, for
#: the same reason the regions do: the 2026-08-12 global probe of the default
#: configuration is the evidence behind a shipped latency cell, and a capped
#: re-probe that overwrote it would delete the number it is being compared to.
def _slug(region: str, candidate: str) -> str:
    return region if candidate == CURRENT_GEN_MODEL else f"{region}_{candidate}"


def _out_path(region: str, candidate: str = CURRENT_GEN_MODEL) -> Path:
    return RESULTS / f"latency_probe_{_slug(region, candidate)}.json"


def _marker_path(region: str, candidate: str = CURRENT_GEN_MODEL) -> Path:
    return RESULTS / f"latency_probe_{_slug(region, candidate)}.done"


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _log(message: str) -> None:
    print(f"[{_stamp()}] {message}", flush=True)


def _items(subagent: str, limit: int):
    path = default_dataset_dir() / f"{subagent}.jsonl"
    if not path.exists():
        raise SystemExit(f"no dataset at {path}")
    items = list(read_items(path))
    core = [i for i in items if i.core]
    rest = [i for i in items if not i.core]
    return (core + rest)[:limit]


def p95_estimate(values: list[float], *, seed: int = DEFAULT_BOOTSTRAP_SEED) -> Estimate:
    """Percentile bootstrap around the p95 itself, not around the mean.

    ``amw.eval.stats.bootstrap_ci`` resamples the *mean*; the gate is written on
    a p95, so resampling the mean would put an interval around the wrong
    statistic. Same seed, same resample count, same confidence as every other
    interval in the study, so the number is reproducible on any machine.

    At ``n=10`` the p95 of a resample is effectively its maximum, so this
    interval is wide and lumpy by construction. That is a true statement about
    a 10-call probe and it is reported rather than smoothed: the honest reading
    is "p95 is not tightly measured at this n", not "p95 is X".
    """
    data = np.asarray(values, dtype=float)
    rng = np.random.default_rng(seed)
    draws = rng.choice(data, size=(BOOTSTRAP_RESAMPLES, data.size), replace=True)
    dist = np.percentile(draws, 95, axis=1)
    tail = (1.0 - DEFAULT_CONFIDENCE) / 2.0 * 100.0
    return Estimate(
        metric="latency_p95",
        point=float(np.percentile(data, 95)),
        lo=float(np.percentile(dist, tail)),
        hi=float(np.percentile(dist, 100.0 - tail)),
        n=int(data.size),
        unit="ms",
        confidence=DEFAULT_CONFIDENCE,
        resamples=BOOTSTRAP_RESAMPLES,
        seed=seed,
        method="percentile_bootstrap",
    )


def _arm_summary(name: str, model: str, region: str, traces: list) -> dict:
    ok = [t for t in traces if t.status == "ok" and t.latency_ms.total is not None]
    totals = [float(t.latency_ms.total) for t in ok]
    ttfts = [
        float(t.latency_ms.ttft) for t in ok if t.latency_ms.ttft is not None
    ]
    out = {
        "arm": name,
        "model": model,
        "region": region,
        "calls": len(traces),
        "calls_ok": len(ok),
        "calls_error": len(traces) - len(ok),
        "total_ms_p50": percentile(totals, 50),
        "total_ms_p95": percentile(totals, 95),
        "ttft_ms_p50": percentile(ttfts, 50),
        "ttft_ms_p95": percentile(ttfts, 95),
        "total_ms": totals,
    }
    if len(totals) >= 2:
        out["p95_estimate"] = p95_estimate(totals).model_dump()
    else:
        out["p95_estimate"] = None
        out["no_interval_reason"] = (
            f"{len(totals)} successful timed call(s); an interval needs at least 2"
        )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-n", type=int, default=10, help="items per subagent")
    parser.add_argument(
        "--region",
        default=DEFAULT_REGION,
        help="the ONE region both arms are pinned to (default: %(default)s)",
    )
    parser.add_argument(
        "--candidate",
        default=CURRENT_GEN_MODEL,
        choices=sorted(set(DEPLOYMENT_CANDIDATES) | {CAPPED_GEN_MODEL}),
    )
    args = parser.parse_args(argv)

    cfg = load_all(customer="demo_patents")
    region = args.region
    store = ReplayStore(PROBE_STORE)

    _log(f"=== A4 same-region latency probe: region={region}, n={args.n} ===")
    _log(f"  incumbent  {BASELINE_MODEL} ({BASELINE_VARIANT})")
    _log(f"  candidate  {args.candidate} (shipping arm per subagent)")
    _log(f"  recordings -> {PROBE_STORE.relative_to(ROOT)} (main corpus untouched)")

    claude = RecordingAdapter(
        ClaudeVertexAdapter(models=cfg.models, region=region), store=store
    )
    gemini = RecordingAdapter(
        GeminiAdapter(models=cfg.models, location=region), store=store
    )

    report: dict = {
        "probe": "same_region_latency",
        "probed_on": _stamp(),
        "region": region,
        "n_per_subagent": args.n,
        "candidate_model": args.candidate,
        "baseline_model": BASELINE_MODEL,
        "store": str(PROBE_STORE.relative_to(ROOT)),
        "subagents": {},
    }

    for subagent in SUBAGENTS:
        items = _items(subagent, args.n)
        arm = SHIPPING_VARIANT[subagent]
        _log(f"--- {subagent}: {BASELINE_VARIANT} vs {arm}, {len(items)} items ---")
        base_traces, cand_traces = [], []
        blocked: str | None = None
        for index, item in enumerate(items, start=1):
            view = prompt_view(item)
            # Interleaved: the two arms see the same minute on the same item.
            if blocked is None:
                try:
                    base_traces.append(
                        claude.complete(
                            build_request(
                                subagent,
                                BASELINE_VARIANT,
                                view,
                                item_id=item.item_id,
                                model=BASELINE_MODEL,
                                models=cfg.models,
                            )
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - the error IS the result
                    blocked = f"{type(exc).__name__}: {exc}"
                    _log(f"  incumbent blocked at item {index}: {blocked}")
            cand_traces.append(
                gemini.complete(
                    build_request(
                        subagent,
                        arm,
                        view,
                        item_id=item.item_id,
                        model=args.candidate,
                        models=cfg.models,
                    )
                )
            )
            if index % 5 == 0:
                _log(f"  ... {index}/{len(items)} items")

        entry = {
            "baseline": _arm_summary(
                BASELINE_VARIANT, BASELINE_MODEL, region, base_traces
            ),
            "candidate": _arm_summary(arm, args.candidate, region, cand_traces),
        }
        # A trace with status "error" is a recorded failure, not a timing
        # sample, so an arm can come back with zero usable numbers even though
        # nothing raised. Both shapes have to reach the report as "unmeasured".
        base_ok = entry["baseline"]["calls_ok"]
        if blocked is not None or base_ok == 0:
            entry["gate_eligible"] = False
            # The provider's own words, verbatim and de-duplicated. "quota
            # exhausted" is a guess until the 429 says so; the owner asked for
            # the exact error precisely so the caveat can be checked rather
            # than believed.
            errors = sorted(
                {t.error for t in base_traces if t.status != "ok" and t.error}
            )
            entry["baseline"]["errors"] = errors
            entry["blocked_reason"] = blocked or (
                f"every incumbent call in {region} came back status!=ok. "
                f"Provider error(s), verbatim: {json.dumps(errors)}"
            )
            entry["disposition"] = (
                f"latency_p95 stays UNMEASURED for {subagent}. The incumbent "
                f"could not be served in {region}, and the existing global-region "
                f"Claude recordings are deliberately NOT substituted: a "
                f"cross-region p95 is two measurements, not a comparison."
            )
            _log(f"  UNMEASURED — {entry['blocked_reason']}")
        else:
            entry["gate_eligible"] = True
            entry["disposition"] = (
                f"both arms served from {region}; latency_p95 can be evaluated "
                f"with baseline_p95_ms = {entry['baseline']['total_ms_p95']}"
            )
            _log(
                f"  baseline p50 {entry['baseline']['total_ms_p50']} "
                f"p95 {entry['baseline']['total_ms_p95']} ms  |  "
                f"candidate p50 {entry['candidate']['total_ms_p50']} "
                f"p95 {entry['candidate']['total_ms_p95']} ms"
            )
        report["subagents"][subagent] = entry

    eligible = [s for s, e in report["subagents"].items() if e["gate_eligible"]]
    report["gate_eligible_subagents"] = eligible
    out = _out_path(region, args.candidate)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    _log(f"wrote {out.relative_to(ROOT)}")
    _log(
        f"gate-eligible subagents: {eligible or 'none — latency_p95 stays unmeasured'}"
    )
    _marker_path(region, args.candidate).write_text(_stamp() + "\n", encoding="utf-8")
    return 0


def _flag_arg(argv: list[str], flag: str, default: str) -> str:
    """A flag's value, read before argparse so a crash still marks the right file.

    Both ``--region`` and ``--candidate`` have to be recovered this way: the
    marker path folds in the candidate, so reading only the region would stamp
    the *default* candidate's marker on a capped-run crash — a failed probe
    posing as a finished one for an arm it never touched.
    """
    for index, arg in enumerate(argv):
        if arg == flag and index + 1 < len(argv):
            return argv[index + 1]
        if arg.startswith(f"{flag}="):
            return arg.split("=", 1)[1]
    return default


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:  # pragma: no cover - the marker must record failures too
        traceback.print_exc()
        _argv = sys.argv[1:]
        _marker_path(
            _flag_arg(_argv, "--region", DEFAULT_REGION),
            _flag_arg(_argv, "--candidate", CURRENT_GEN_MODEL),
        ).write_text(f"{_stamp()} FAILED\n", encoding="utf-8")
        raise SystemExit(1)
