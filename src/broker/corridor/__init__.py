"""Wall Strike Corridor: `<= 90`-contract targeted quotes around Put/Call Walls.

- `models`: limits, Pydantic frames, selection dataclasses, `WallAnchors`, `GreeksFallback`.
- `selection`: pure `select_wall_corridor` (wall anchors are supplied by the service layer) and
  `strategy_leg_spec_from_row` (exact-contract line spec of one snapshot row).
- `lines`: shared line primitives (paced subscribe, initial-poll cascade, quote building,
  cleanup) used by `service` and the live screener leg stream (`src/broker/live_legs.py`).
- `service`: `CorridorQuoteService` subscription lifecycle (one-shot + streaming).
"""

from __future__ import annotations

from src.broker.corridor.models import (
    CORRIDOR_MAX_CONTRACTS,
    DEFAULT_CORRIDOR_MAX_CONTRACTS,
    DEFAULT_WINDOW_STRIKES,
    BoolArray,
    CorridorContractSpec,
    CorridorLegRef,
    CorridorQuoteFrame,
    CorridorQuoteRow,
    CorridorSelection,
    CorridorZone,
    FloatArray,
    GreeksFallback,
    WallAnchors,
    corridor_quote_key,
)
from src.broker.corridor.selection import select_wall_corridor, strategy_leg_spec_from_row
from src.broker.corridor.service import CorridorQuoteService

__all__ = [
    "BoolArray",
    "CORRIDOR_MAX_CONTRACTS",
    "CorridorContractSpec",
    "CorridorLegRef",
    "CorridorQuoteFrame",
    "CorridorQuoteRow",
    "CorridorQuoteService",
    "CorridorSelection",
    "CorridorZone",
    "DEFAULT_CORRIDOR_MAX_CONTRACTS",
    "DEFAULT_WINDOW_STRIKES",
    "FloatArray",
    "GreeksFallback",
    "WallAnchors",
    "corridor_quote_key",
    "select_wall_corridor",
    "strategy_leg_spec_from_row",
]
