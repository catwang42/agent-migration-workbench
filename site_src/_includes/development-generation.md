!!! info "Which model these figures are from"

    Unless a table says otherwise, the Gemini arm on this page is **Gemini 2.5
    Flash** — the *development generation*, where the adaptation ladder, the
    prompt-optimizer work and the judge cross-check were done. It is **not** what
    the scorecard recommends deploying.

    The recommendation is **Gemini 3.6 Flash with the reasoning budget minimised**,
    and its measured results lead
    [module 04](../modules/04-the-naive-swap.md),
    [module 05](../modules/05-adaptation-ladder.md),
    [module 07](../modules/07-shadow-and-adjudication.md) and the
    [Results headline](../results/scorecard.md).

    The prompts tuned on the development generation were run **unchanged** on the
    deployment generation and the instruction rules kept working. That portability
    is a finding in its own right — the adaptation work is an asset that survives a
    model upgrade, not a tax you pay again on every new model — and it is why both
    generations stay on this site.

    Every model, its provider ID, its region and the window its calls were
    recorded in: [Models in this study](../models-in-this-study.md).
