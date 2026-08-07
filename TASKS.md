# TASKS.md — Act 1 Execution Order for Claude Code

Work top-to-bottom. One commit per task, task ID in the message. Every task ends with its **Verify** command passing plus the global gate: `pytest tests/ && python cli.py e2e --mode replay` (e2e applies once T09 exists). Tasks marked **[P1: needs SPIKE-x GREEN]** are skipped unless `SPIKES.md` says so.

## Session 1 kickoff prompt (paste into Claude Code)

> Read CLAUDE.md and act1_build_plan.md fully before writing code. Then execute TASKS.md starting at T01, strictly in order, committing per task. Ground rules in CLAUDE.md are non-negotiable — especially: no fabricated results, replay mode must always work without credentials, prices only from pricing.yaml. When you reach a spike task (T05), stop after the timebox and report GREEN/RED with evidence into SPIKES.md instead of pushing past failures. Ask me before any design decision that isn't covered by these three files.

---

## DAY 0 — Thu Aug 6

### T01 — Scaffold + config system  (est. 45m)
**Goal**: Repo skeleton per CLAUDE.md map; `config/models.yaml`, `pricing.yaml` (all values `VERIFY`, `verified_on: null`, source URLs), `gates.yaml` (thresholds from act1_build_plan §2 / the master plan), `customers/demo_patents.yaml` (domain, seed, volume profile placeholders); pydantic loaders with validation; `scripts/refresh_pricing.py` (interactive: prompts per price, writes values + `verified_on` + operator name).
**Verify**: `python -c "from amw.config import load_all; load_all()"` fails loudly on a malformed yaml fixture in `tests/`, passes on real config; `pytest tests/test_config.py`.

### T02 — Canonical trace schema + replay store  (est. 1h)
**Goal**: `amw/traces/schema.py` (pydantic `Trace`: trace_id, subagent, provenance, ts, model, system_prompt_sha, input{messages, context_chunks}, tools_offered, tool_calls, output{text,json}, usage{input,output,cached}, latency_ms{ttft,total}, status). JSONL read/write. `ReplayStore` keyed `(subagent, model, input_sha)` with `.get()` and `.append()`; miss raises `ReplayMissError` naming the key.
**Verify**: `pytest tests/test_traces.py` — round-trip a fixture trace file byte-stable; replay hit and miss paths covered.

### T03 — Gemini adapter + mode resolution + record-on-live  (est. 1.5h)
**Goal**: `adapters/base.py::ModelAdapter` (`.complete(request) -> Trace`); `adapters/gemini.py` (google-genai, system_instruction, `response_schema` strict, tool declarations, usage + TTFT capture); `adapters/replay.py`; mode resolution `live|replay|hybrid` in `adapters/__init__.py` only. Every live call auto-appends to `artifacts/replay/` — no off switch.
**Verify**: `pytest tests/test_adapters.py -k replay` offline; then human-run `python cli.py smoke --mode live -n 1 --backend gemini` (after T09 wires smoke; for now a `scripts/dev_call_gemini.py` one-shot) → trace lands in `artifacts/replay/` and replays identically.

### T04 — Claude adapter (chosen path)  (est. 1.5h)
**Goal**: Read `CLAUDE_PATH` from env. `adapters/claude_vertex.py` (Model Garden) and `adapters/claude_anthropic.py` (direct API) behind the same interface; XML-style prompt pass-through untouched (it IS the baseline); usage + latency capture; same record-on-live.
**Verify**: replay-mode test offline; human-run one live round trip on the chosen path → recorded → replayed byte-identical. Log result as **SPIKE-S1** in `SPIKES.md`.

### T05 — Platform spikes S2 + S3 (timeboxed 90m EACH — hard stop)  (est. 3h ceiling)
**S2 Vertex GenAI Eval Service**: run ONE rubric metric over 3 canned items; parse scores + rationales into plain dicts. GREEN = parsed results printed. RED = document the failure + exact error in `SPIKES.md`, move on.
**S3 VAIPO**: one optimization iteration on a toy instruction with a 5-item eval set; retrieve the candidate prompt. Same GREEN/RED protocol.
**Rule**: at timebox expiry, stop mid-anything and record status. RED locks the corresponding P1 items OFF for this build.
**Verify**: `SPIKES.md` contains S1/S2/S3 rows with status, evidence snippet, and the P1 scope decision.

---

## DAY 1 — Fri Aug 7

### T06 — Synthetic dataset generator + rubrics  (est. 2.5h)
**Goal**: `amw/datasets/generator.py` — patents domain, seeded, per-subagent templates producing: input payload, gold reference output, per-item rubric (3–5 pass/fail criteria), difficulty tag (mix 40/25/20/15 simple/multi-hop/extraction/edge), `provenance: synthetic`. Generation uses the Gemini adapter (so it's recorded/replayable too). `cli.py gen --customer demo_patents -n 70` writes `datasets/{subagent}.jsonl` + a 30-item stratified `core` split.
**Checkpoint (human)**: review 10 items/subagent for realism BEFORE the full run; adjust templates once.
**Verify**: `python cli.py gen ... -n 70` produces valid files (schema-checked); `pytest tests/test_datasets.py` (schema, mix proportions ±10%, seed determinism on a 5-item run in replay).

### T07 — Subagent prompt packs  (est. 2h)
**Goal**: `amw/agents/prompts/{subagent}/` with three variants each for QR / CS / FE: `claude_baseline.txt` (XML-style tags, faithful to how the customer's prompts look), `gemini_naive.txt` (the XML fed verbatim — rung A0), `gemini_tuned_v1.txt` (Markdown + system-instruction split + strict schema + 2 few-shots — rung A1–A3 starting point). Strict `response_schema` JSON per subagent in `amw/agents/schemas.py`. Root orchestrator: stub + docstring only (not evaluated).
**Verify**: `pytest tests/test_prompts.py` — every variant renders with a sample item, schemas compile, tuned variants contain no XML instruction tags.

### T08 — Eval engine: metrics + judge + stats + golden tests  (est. 3h)
**Goal**: `eval/metrics.py` — JSON schema validity, exact-key/filter precision-recall, citation coverage (every output claim maps to a provided chunk id). `eval/judge.py` — rubric-anchored judge (Gemini Pro-class from models.yaml), rationale captured, prompts in `eval/judge_prompts/` as files. `eval/stats.py` — mean, bootstrap 95% CI (10k resamples), paired-bootstrap delta; parity check = CI lower bound vs `gates.yaml`. Golden fixtures: hand-computed expected values for every metric.
**Verify**: `pytest tests/test_metrics.py tests/test_stats.py` — goldens exact; judge runs in replay against 3 recorded calls.

### T09 — Phase-2 runner + cli wiring + e2e  (est. 2h)
**Goal**: `eval/runner.py` — for each subagent × {claude_baseline, gemini_naive, gemini_tuned_v1}: deterministic metrics on full set, judge on core × k=2; results to `artifacts/results/phase2.json`. Wire `cli.py` subcommands `gen|phase2|smoke|e2e`; `e2e --mode replay` = tiny fixture dataset through the whole path offline.
**Human-run EOD**: `python cli.py phase2 --mode hybrid -n 10` → first real numbers; skim failure clusters (group failed items by rubric criterion) into `notes/day1_failures.md` to seed Monday's tuning.
**Verify**: `python cli.py e2e --mode replay` passes clean; phase2.json validates against its pydantic model.

---

## DAY 2 — Mon Aug 10

### T10 — Translator + ablation ladder  (est. 2.5h)
**Goal**: `tuning/translator.py` — mechanical Claude-XML → {system_instruction, Markdown body, response_schema, OpenAPI tool decls}; emits a side-by-side diff (md) for the teaching moment. `tuning/ablate.py` + `cli.py ablate --subagent X` — runs rungs A0–A4 on the core set (k=2 judged + deterministic), where A2/A3/A4 prompt edits are iterated by hand against `notes/day1_failures.md`. Results per rung appended to `artifacts/results/ablation_{subagent}.json`.
**[P1: needs SPIKE-S3 GREEN]** Add rung A4′ = VAIPO-optimized instruction, clearly labeled, real run only.
**Verify**: `pytest tests/test_translator.py` (XML fixtures → expected structure); `cli.py ablate --subagent query_rewriter --mode replay` completes on fixtures.

### T11 — Shadow runner + agreement + triage  (est. 2h)
**Goal**: `shadow/runner.py` — same inputs through both backends concurrently (asyncio), full traces recorded. `shadow/agreement.py` — exact-match for structured fields, embedding cosine for prose. `shadow/triage.py` — every disagreement judge-adjudicated to win/loss/tie with one-line rationale. Latency: TTFT + total, p50/p95 per backend.
**Verify**: replay-mode shadow run on fixtures produces agreement + a triage table; `pytest tests/test_shadow.py`.

### T12 — Gates → verdicts → scorecard + economics  (est. 2.5h)
**Goal**: `reporting/scorecard.py` — evaluate `gates.yaml` per subagent (CI lower bounds), emit MIGRATE/TUNE_FIRST/HOLD; Markdown scorecard: per-subagent table (quality Δpp with CI, schema validity, agreement w/ win-loss-tie, cost savings %, latency p50/p95, verdict) + auto footer (provenance+seed, judge info, pricing `verified_on`+sources, region, run date, gates hash). Cost columns render `—` if `pricing.verified_on` is null (never fake numbers). `economics/cost_model.py` — per-subagent daily/monthly/annual from customer volumes ×0.5/×1/×2, cached vs uncached separately; `economics/cache_breakeven.py` — write/storage vs read savings vs TTL → breakeven calls/day.
**Verify**: `pytest tests/test_gates.py tests/test_economics.py` (goldens incl. a forced TUNE_FIRST fixture); `cli.py scorecard --mode replay` renders complete markdown from fixtures.

### T13 — Thin notebooks  (est. 1.5h)
**Goal**: `notebooks/01_baseline_and_tuning.ipynb` (load phase2 + ablation artifacts → tables/charts with error bars; one live 10-case cell parameterized by `--mode`), `notebooks/02_shadow_scorecard.ipynb` (shadow results, triage browser via pandas, final scorecard render). Zero logic beyond display; all cells run headless.
**Verify**: `papermill` both notebooks in replay mode → clean execution, outputs saved.

### T14 — P1 items in priority order (only those GREEN / time-permitting)
1. **[SPIKE-S2]** Vertex Eval Service rubric metrics + loss clustering wired as an additional metrics source in runner + notebook cell.
2. Dual-judge cross-check: Claude-class judge re-scores 20% stratified sample; agreement % + Cohen's κ into the scorecard footer.
3. **Answer Drafter** subagent: prompts + 70 cases + inclusion in phase2/ablation/shadow → the deliberate TUNE_FIRST row.
4. Live context-caching demo: create cache with shared preamble, two calls, print `cached_tokens` delta + breakeven overlay.
5. HTML scorecard (same data, jinja template).
**Verify**: each sub-item has its own test or a replay-mode demo command noted in WORKSHOP_RUNBOOK.
**HARD STOP tonight — no feature work after Day 2.**

---

## DAY 3 — Tue Aug 11 (freeze + harden)

### T15 — Freeze-day run + docs  (est. full day, human-led, Claude Code assists)
**Goal AM**: run `scripts/refresh_pricing.py` (human verifies against live pricing pages; sets `verified_on`). Full live run: `gen` (if templates changed) → `phase2` full → `ablate` ×3(4) → `shadow` → `scorecard`. This run IS the replay corpus + final artifacts. Review judged CI widths (fallback per build plan §3).
**Goal PM**: finish `WORKSHOP_RUNBOOK.md` (run-of-show with per-segment commands, talk track bullets, fallback tree incl. the one-flag drill), `docs/objection_handling.md` (from master plan §7), gates one-pager, `data_request_onepager.md`. Full timed rehearsal in `--mode replay`; export notebook HTML backups to `artifacts/backup/`; drill `hybrid`→`replay` switch mid-notebook once.
**Verify**: rehearsal fits the 3h run-of-show with ≥15 min slack; `cli.py e2e --mode replay` green on the frozen corpus; scorecard footer fully populated (no `VERIFY`, no null `verified_on`).

## DAY 4 — Wed Aug 12 (delivery)

### T16 — Pre-flight  (T-2h)
`python cli.py smoke --mode live -n 2` per backend from the delivery machine/network; confirm `--mode replay` still green; open notebooks in **hybrid** mode. If smoke fails: flip to replay, deliver anyway — the corpus is Tuesday's real run and says so on screen.

---

## Backlog (P2 — do not start before Thursday Aug 13)
BYOT converters (anthropic logs / LangSmith / Langfuse / ADK / CSV) + PII scrub + notebook 04 · LLM Comparator export · multi-domain generator · local hill-climb APO · risk register · CI (pytest + papermill + e2e-replay on push).
