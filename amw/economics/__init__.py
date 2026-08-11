"""Cost model and caching breakeven — both gated on verified prices.

Nothing in this package will emit a dollar figure while
``config/pricing.yaml`` is unverified (CLAUDE.md ground rule 3), and
:mod:`amw.economics.cost_model` additionally refuses while the customer's call
volumes are illustrative. Both refusals are explicit states carrying the reason
and what would clear it — never zeros.
"""

from amw.economics.cache_breakeven import (
    CacheBreakeven,
    breakeven_curve,
    cache_breakeven,
)
from amw.economics.cost_model import (
    Blocker,
    CostModelResult,
    CostRow,
    SubagentVolume,
    VolumeSet,
    VolumeSource,
    confirm_volumes,
    cost_model,
)

__all__ = [
    "Blocker",
    "CacheBreakeven",
    "CostModelResult",
    "CostRow",
    "SubagentVolume",
    "VolumeSet",
    "VolumeSource",
    "breakeven_curve",
    "cache_breakeven",
    "confirm_volumes",
    "cost_model",
]
