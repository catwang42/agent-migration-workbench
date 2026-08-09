"""Resume the n=70 generation for the subagents that did not finish.

Throwaway driver, not part of the build. `cli.py gen` has no --subagent flag
and does not need one: this exists only because a background run was killed
mid-corpus, and regenerating query_rewriter would burn ~70 live calls to
reproduce a file that is already complete and valid.

Per-subagent seeds are derived as _rng(seed, subagent, ...), so generating a
subset yields byte-identical items to generating all three.

Kept as the provenance record for datasets/: the committed corpus was made in
two passes, and this file is the second one. Do not run it to make a new
corpus — the subagent list below is frozen to what was outstanding on
2026-08-09 and would leave query_rewriter stale. `cli.py gen` is the supported
entry point.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)

from amw.config import load_all  # noqa: E402
from amw.datasets import generate  # noqa: E402

result = generate(
    config=load_all(customer="demo_patents"),
    n=70,
    mode="live",
    subagents=("chunk_summarizer", "feature_extractor"),
    naturalise=True,
)
print(result.describe(), flush=True)
for reason in result.rewrite.reasons:
    print(f"  realism pass: {reason}", flush=True)
