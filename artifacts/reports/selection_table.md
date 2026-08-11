# tuned_v2 selection table

Every rung that was run, per subagent, at the n it was actually run on.

Rows are grouped by split because the two splits have different incumbents: Feature Extractor's Claude baseline is **0.903** on the core 28 and **0.900** on the full 70. A rung measured at n=70 compared against 0.903 is being scored against a different corpus.

Comparison column: the rung's 95% CI lower bound against the **incumbent's point estimate on the same split**. It is *unpaired* and it is not the `quality_delta_pp` gate, which is a paired bootstrap over per-item differences and is what the verdict is decided on. This table informs the selection; it does not make it.

## query_rewriter

### split = full 70

| Rung / arm | Source | Mode | Judged score (95% CI) | `exact_match_intent` | `filter_f1` | `filter_precision` | `filter_recall` | `json_schema_validity` | vs incumbent |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `A4-targeted` | ladder | `response_schema` | 0.963 [0.934, 0.986] (judged n=70, split=all) | 0.957 [0.900, 1.000] | 0.969 [0.941, 0.992] | 0.933 [0.881, 0.978] | 0.985 [0.962, 1.000] | 1.000 [1.000, 1.000] | **clears** (lo 0.934 > 0.886) |
| `gated:claude_baseline` | gated run | `tool` | 0.886 [0.838, 0.932] (judged n=70, split=all) | 0.729 [0.629, 0.829] | 0.973 [0.927, 1.000] | 0.915 [0.840, 0.981] | 0.754 [0.646, 0.862] | 0.814 [0.714, 0.900] | incumbent |
| `gated:gemini_naive` | gated run | `tool` | 0.831 [0.797, 0.865] (judged n=70, split=all) | 0.571 [0.457, 0.686] | 0.950 [0.909, 0.981] | 0.914 [0.854, 0.965] | 0.965 [0.927, 0.992] | 0.971 [0.929, 1.000] | below incumbent (hi 0.865 < 0.886) |
| `gated:gemini_tuned_v1` | gated run | `response_schema` | 0.879 [0.843, 0.914] (judged n=70, split=all) | 0.814 [0.714, 0.900] | 0.950 [0.918, 0.978] | 0.914 [0.858, 0.963] | 0.965 [0.937, 0.988] | 1.000 [1.000, 1.000] | **recovery to parity** (lo 0.843 <= 0.886 <= hi 0.914) |

### split = core 28

| Rung / arm | Source | Mode | Judged score (95% CI) | `exact_match_intent` | `filter_f1` | `filter_precision` | `filter_recall` | `json_schema_validity` | vs incumbent |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `baseline` | ladder | `tool` | 0.911 [0.839, 0.964] (judged n=28, split=core) | 0.786 [0.607, 0.929] | 0.985 [0.955, 1.000] | 0.977 [0.932, 1.000] | 0.815 [0.667, 0.963] | 0.893 [0.750, 1.000] | incumbent |
| `A0` | ladder | `tool` | 0.836 [0.787, 0.886] (judged n=28, split=core) | 0.536 [0.357, 0.714] | 0.961 [0.921, 0.991] | 0.949 [0.894, 0.991] | 0.986 [0.972, 1.000] | 0.964 [0.893, 1.000] | below incumbent (hi 0.886 < 0.911) |
| `A1-A3` | ladder | `response_schema` | 0.852 [0.796, 0.905] (judged n=28, split=core) | 0.714 [0.536, 0.893] | 0.948 [0.895, 0.988] | 0.935 [0.866, 0.986] | 0.972 [0.931, 1.000] | 1.000 [1.000, 1.000] | below incumbent (hi 0.905 < 0.911) |

## chunk_summarizer

### split = full 70

| Rung / arm | Source | Mode | Judged score (95% CI) | `citation_coverage` | `fabricated_citation_rate` | `json_schema_validity` | `uncited_claim_rate` | vs incumbent |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `gated:claude_baseline` | gated run | `tool` | 0.918 [0.879, 0.954] (judged n=70, split=all) | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] | 0.971 [0.929, 1.000] | 0.000 [0.000, 0.000] | incumbent |
| `gated:gemini_naive` | gated run | `tool` | 0.868 [0.820, 0.911] (judged n=70, split=all) | 0.940 [0.881, 0.985] | 0.000 [0.000, 0.000] | 0.943 [0.886, 0.986] | 0.000 [0.000, 0.000] | below incumbent (hi 0.911 < 0.918) |
| `gated:gemini_tuned_v1` | gated run | `response_schema` | 0.895 [0.857, 0.929] (judged n=70, split=all) | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] | **recovery to parity** (lo 0.857 <= 0.918 <= hi 0.929) |

### split = core 28

| Rung / arm | Source | Mode | Judged score (95% CI) | `citation_coverage` | `fabricated_citation_rate` | `json_schema_validity` | `uncited_claim_rate` | vs incumbent |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `baseline` | ladder | `tool` | 0.902 [0.830, 0.956] (judged n=28, split=core) | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] | 0.964 [0.893, 1.000] | 0.000 [0.000, 0.000] | incumbent |
| `A0` | ladder | `tool` | 0.897 [0.830, 0.955] (judged n=28, split=core) | 0.963 [0.889, 1.000] | 0.000 [0.000, 0.000] | 0.964 [0.893, 1.000] | 0.000 [0.000, 0.000] | **recovery to parity** (lo 0.830 <= 0.902 <= hi 0.955) |
| `A1-A3` | ladder | `response_schema` | 0.915 [0.871, 0.955] (judged n=28, split=core) | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] | **recovery to parity** (lo 0.871 <= 0.902 <= hi 0.955) |

## feature_extractor

### split = full 70

| Rung / arm | Source | Mode | Judged score (95% CI) | `answered_precision` | `extraction_accuracy` | `hallucination_rate` | `json_schema_validity` | `omission_rate` | vs incumbent |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `A0-schema` | ladder | `response_schema` | 0.831 [0.798, 0.862] (judged n=70, split=all) | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] | below incumbent (hi 0.862 < 0.900) |
| `A4-novelty-tool` | ladder | `tool` | 0.892 [0.864, 0.918] (judged n=70, split=all) | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] | **recovery to parity** (lo 0.864 <= 0.900 <= hi 0.918) |
| `A4-novelty-schema` | ladder | `response_schema` | 0.903 [0.876, 0.929] (judged n=70, split=all) | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] | **recovery to parity** (lo 0.876 <= 0.900 <= hi 0.929) |
| `A4-optimizer` | ladder | `tool` | 0.951 [0.929, 0.970] (judged n=70, split=all) † | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] | **clears** (lo 0.929 > 0.900) |
| `gated:claude_baseline` | gated run | `tool` | 0.900 [0.868, 0.929] (judged n=70, split=all) | 1.000 [1.000, 1.000] | 0.971 [0.929, 1.000] | 0.000 [0.000, 0.000] | 0.957 [0.900, 1.000] | 0.029 [0.000, 0.074] | incumbent |
| `gated:gemini_naive` | gated run | `tool` | 0.821 [0.787, 0.854] (judged n=70, split=all) | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] | below incumbent (hi 0.854 < 0.900) |
| `gated:gemini_tuned_v1` | gated run | `response_schema` | 0.795 [0.760, 0.828] (judged n=70, split=all) | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] | below incumbent (hi 0.828 < 0.900) |

### split = core 28

| Rung / arm | Source | Mode | Judged score (95% CI) | `answered_precision` | `extraction_accuracy` | `hallucination_rate` | `json_schema_validity` | `omission_rate` | vs incumbent |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `baseline` | ladder | `tool` | 0.903 [0.857, 0.946] (judged n=28, split=core) | 1.000 [1.000, 1.000] | 0.929 [0.821, 1.000] | 0.000 [0.000, 0.000] | 0.929 [0.821, 1.000] | 0.074 [0.000, 0.185] | incumbent |
| `A0` | ladder | `tool` | 0.837 [0.791, 0.882] (judged n=28, split=core) | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] | below incumbent (hi 0.882 < 0.903) |
| `A1-A3` | ladder | `response_schema` | 0.807 [0.756, 0.853] (judged n=28, split=core) | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] | below incumbent (hi 0.853 < 0.903) |
| `A0-schema` | ladder | `response_schema` | 0.826 [0.773, 0.875] (judged n=28, split=core) | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] | below incumbent (hi 0.875 < 0.903) |
| `A4-novelty-tool` | ladder | `tool` | 0.901 [0.859, 0.940] (judged n=28, split=core) | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] | **recovery to parity** (lo 0.859 <= 0.903 <= hi 0.940) |
| `A4-novelty-schema` | ladder | `response_schema` | 0.920 [0.878, 0.957] (judged n=28, split=core) | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] | **recovery to parity** (lo 0.878 <= 0.903 <= hi 0.957) |
| `A4-optimizer` | ladder | `tool` | 0.949 [0.911, 0.979] (judged n=28, split=core) | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] | **clears** (lo 0.911 > 0.903) |

† `A4-optimizer` quotes `fe-0004`, `fe-0021`, `fe-0029`, `fe-0032`, `fe-0037`, `fe-0041`, `fe-0046`, `fe-0051`, `fe-0056`, `fe-0060`, `fe-0064`, `fe-0067` as a worked example and those items are inside the split it was scored on. Its judged score is optimistic there by construction; the items are not excluded, because that would give this rung a different denominator from every other rung.

