"""Wall Strike Corridor: `<= 60`-contract targeted quotes around Put/Call Walls.

- `models`: limits, Pydantic frames, selection dataclasses, `WallAnchors`, `GreeksFallback`.
- `selection`: pure `select_wall_corridor` (wall anchors are supplied by the service layer).
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
from src.broker.corridor.selection import select_wall_corridor
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
]
