!!! info "Which model these figures are from"

    The arm labelled "Gemini" on this page is **Gemini 2.5 Flash** — the
    *development generation*, where the adaptation ladder and the prompt-optimizer
    work were done. It is not what the scorecard recommends deploying.

    The prompts tuned here were then run **unchanged** on the deployment
    generation — Gemini 3.6 Flash, with Gemini 3.5 Flash as a second candidate
    column — and the instruction rules kept working across the generation gap.
    That portability is a finding in its own right: the adaptation work is an
    asset that survives a model upgrade, not a tax you pay again on every new
    model. It is also why both generations appear on this site rather than only
    the newest one — deleting the development generation would delete the
    evidence that the rules transfer.

    Every model, its provider ID, its region and the window its calls were
    recorded in: [Models in this study](../models-in-this-study.md).
