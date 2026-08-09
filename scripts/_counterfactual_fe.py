"""Recompute FE deterministic metrics under the PRE-2026-08-07 field split.

Answers one question: if `technical_field` and `novelty_statement` had stayed
on exact match, what would a deterministic-only scorecard have said about
Feature Extractor?

Method: replay the n=10 run's recorded subagent calls — the same candidate
outputs the published artifact scored — with `FE_SCALAR_FIELDS` restored to
its old membership and `FE_JUDGED_FIELDS` emptied. No judge, because the old
regime had no judge criterion for these fields; that absence is the whole
point of the counterfactual.

Not part of the build. Kept as the derivation behind
notes/counterfactual_scorecard.md so the numbers there are reproducible.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amw.config import load_all  # noqa: E402
from amw.eval import metrics as m  # noqa: E402

OLD_SCALAR = (
    "title",
    "assignee",
    "filing_date",
    "jurisdiction",
    "independent_claim_count",
    "technical_field",
    "novelty_statement",
)

assert set(m.FE_SCALAR_FIELDS) | set(m.FE_JUDGED_FIELDS) == set(OLD_SCALAR), (
    "the old split is no longer the union of today's two lists — re-derive it"
)

OLD_FIELDS = OLD_SCALAR + m.FE_LIST_FIELDS

# `fields: Sequence[str] = FE_FIELDS` is a *default argument*, bound when the
# function was defined. Rebinding the module attribute does nothing — the first
# attempt at this script did exactly that and silently reproduced the published
# post-reroute numbers, which is the failure mode worth guarding against.
m.extraction_field_verdicts.__defaults__ = (OLD_FIELDS,)
m.extraction_metrics.__kwdefaults__ = {**(m.extraction_metrics.__kwdefaults__ or {}),
                                       "fields": OLD_FIELDS}
m.extraction_field_verdicts.__kwdefaults__ = {
    **(m.extraction_field_verdicts.__kwdefaults__ or {}), "fields": OLD_FIELDS
}
m.FE_SCALAR_FIELDS = OLD_SCALAR
m.FE_FIELDS = OLD_FIELDS
m.FE_JUDGED_FIELDS = ()

# prove the patch took before trusting any number it produces
_probe = m.extraction_field_verdicts(
    {"technical_field": "solid-state lithium batteries"},
    {"technical_field": "lithium solid electrolytes"},
)
assert "technical_field" in _probe, "patch did not take: field is still unscored"
assert _probe["technical_field"] == "wrong", (
    f"expected exact match to call a paraphrase wrong, got {_probe['technical_field']}"
)

from amw.eval import runner as r  # noqa: E402

r.FE_JUDGED_FIELDS = ()  # rubric_of adds no fe_field_label criteria

cfg = load_all(customer="demo_patents")
result = r.run_phase2(
    config=cfg,
    mode="replay",
    n=10,
    subagents=["feature_extractor"],
    run_judge=False,
    write=False,
)

rows = {}
for arm in result.arms:
    rows[arm.variant] = {
        name: (rep.point if rep.point is not None else None)
        for name, rep in arm.metrics.items()
    }
    rows[arm.variant]["_calls_ok"] = arm.calls_ok
    rows[arm.variant]["_items"] = arm.items

print(json.dumps(rows, indent=2, sort_keys=True))

# ---------------------------------------------------------------------------
# per-field verdicts, so the note can say *which* fields exact match penalised
# ---------------------------------------------------------------------------
import collections  # noqa: E402

from amw.adapters import AdapterRouter  # noqa: E402
from amw.agents.prompt_packs import build_request  # noqa: E402

router = AdapterRouter(mode="replay", models=cfg.models)
items = r._load_dataset("feature_extractor", dataset_dir=r.default_dataset_dir(), limit=10)

print("\nper-field verdicts under exact match (10 items/arm):")
for variant in ("claude_baseline", "gemini_naive", "gemini_tuned_v1"):
    reqs = [build_request("feature_extractor", variant, r.prompt_view(i), item_id=i.item_id)
            for i in items]
    traces = router.complete_many(reqs)
    tally: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for item, trace in zip(items, traces):
        pred = r.judge_candidate(trace)
        pred = pred if isinstance(pred, dict) else None
        for field, verdict in m.extraction_field_verdicts(item.gold, pred).items():
            tally[field][verdict] += 1
    print(f"  {variant}")
    for field in OLD_FIELDS:
        c = tally[field]
        bad = c["wrong"] + c["hallucination"] + c["omission"]
        print(f"    {field:26s} not-correct {bad:2d}/10  {dict(c)}")
