"""Put Wall and Call Wall identification across Open Interest and Volume bases."""

from __future__ import annotations

import heapq

import polars as pl
from pydantic import BaseModel, ConfigDict

from src.storage.schema import OptionRight, WallBasis


class WallLevel(BaseModel):
    """Single strike concentration level (Call or Put wall candidate)."""

    model_config = ConfigDict(extra="forbid")

    strike: float
    right: OptionRight
    open_interest: int
    volume: int
    metric_value: int
    distance_from_spot_pct: float
    concentration_ratio: float


class DTEWallSummary(BaseModel):
    """Call and Put wall summary for a single expiration date."""

    model_config = ConfigDict(extra="forbid")

    expiry: str
    dte: int
    primary_call_wall: WallLevel | None
    primary_put_wall: WallLevel | None
    top_call_walls: list[WallLevel]
    top_put_walls: list[WallLevel]


class ChainWallsReport(BaseModel):
    """Aggregate and per-DTE Put/Call walls report with automatic OI -> Volume fallback."""

    model_config = ConfigDict(extra="forbid")

    underlying: str
    underlying_price: float
    primary_basis: WallBasis
    total_call_oi: int
    total_put_oi: int
    total_call_volume: int
    total_put_volume: int
    aggregate_primary_call_wall: WallLevel | None
    aggregate_primary_put_wall: WallLevel | None
    aggregate_top_call_walls: list[WallLevel]
    aggregate_top_put_walls: list[WallLevel]
    by_dte: list[DTEWallSummary]


def _add_strike_totals(
    totals_by_strike: dict[float, tuple[int, int]],
    strike: float,
    oi: int,
    vol: int,
) -> None:
    cur = totals_by_strike.get(strike)
    totals_by_strike[strike] = (oi, vol) if cur is None else (cur[0] + oi, cur[1] + vol)


def _rank_strike_totals(
    totals_by_strike: dict[float, tuple[int, int]],
    *,
    right: OptionRight,
    spot: float,
    basis: WallBasis,
    top_n: int,
) -> list[WallLevel]:
    """Rank strikes for one option side (`C` or `P`) and return top `top_n` walls."""
    if not totals_by_strike:
        return []

    is_oi = basis == "OPEN_INTEREST"
    total_side_metric = sum(oi if is_oi else vol for oi, vol in totals_by_strike.values())
    denom = float(max(total_side_metric, 1))

    candidates: list[tuple[int, int, float, int, int, int]] = []
    for strike_val, (oi_val, vol_val) in totals_by_strike.items():
        metric_val = oi_val if is_oi else vol_val
        if metric_val <= 0:
            continue
        tie_val = vol_val if is_oi else oi_val
        candidates.append((-metric_val, -tie_val, strike_val, oi_val, vol_val, metric_val))
    if not candidates:
        return []

    levels: list[WallLevel] = []
    for _, _, strike_val, oi_val, vol_val, metric_val in heapq.nsmallest(top_n, candidates):
        dist_pct = ((strike_val - spot) / spot) * 100.0 if spot > 0.0 else 0.0
        levels.append(
            WallLevel(
                strike=strike_val,
                right=right,
                open_interest=oi_val,
                volume=vol_val,
                metric_value=metric_val,
                distance_from_spot_pct=dist_pct,
                concentration_ratio=float(metric_val) / denom,
            )
        )
    return levels


def compute_walls(df: pl.DataFrame, top_n: int = 3) -> ChainWallsReport:
    """Compute aggregate and per-DTE Put/Call walls with automatic Volume fallback."""
    if top_n < 1:
        raise ValueError("top_n must be >= 1")

    if df.height == 0:
        return ChainWallsReport(
            underlying="",
            underlying_price=0.0,
            primary_basis="VOLUME",
            total_call_oi=0,
            total_put_oi=0,
            total_call_volume=0,
            total_put_volume=0,
            aggregate_primary_call_wall=None,
            aggregate_primary_put_wall=None,
            aggregate_top_call_walls=[],
            aggregate_top_put_walls=[],
            by_dte=[],
        )

    underlying = str(df["underlying"][0])
    spot = float(df["underlying_price"][0])

    projected = df.select(
        pl.col("dte").cast(pl.Int64),
        pl.col("expiry").cast(pl.Utf8),
        pl.col("right").cast(pl.Utf8),
        pl.col("strike").cast(pl.Float64),
        pl.col("open_interest").fill_null(0).cast(pl.Int64),
        pl.col("volume").fill_null(0).cast(pl.Int64),
    )

    agg_calls: dict[float, tuple[int, int]] = {}
    agg_puts: dict[float, tuple[int, int]] = {}
    dte_groups: dict[
        tuple[int, str],
        tuple[dict[float, tuple[int, int]], dict[float, tuple[int, int]]],
    ] = {}
    total_call_oi = 0
    total_put_oi = 0
    total_call_volume = 0
    total_put_volume = 0

    for dte_raw, expiry_raw, right_raw, strike_raw, oi_raw, vol_raw in projected.iter_rows():
        key = (int(dte_raw), str(expiry_raw))
        group = dte_groups.get(key)
        if group is None:
            group = ({}, {})
            dte_groups[key] = group
        strike_val = float(strike_raw)
        oi_val = int(oi_raw)
        vol_val = int(vol_raw)
        if right_raw == "C":
            total_call_oi += oi_val
            total_call_volume += vol_val
            _add_strike_totals(agg_calls, strike_val, oi_val, vol_val)
            _add_strike_totals(group[0], strike_val, oi_val, vol_val)
        elif right_raw == "P":
            total_put_oi += oi_val
            total_put_volume += vol_val
            _add_strike_totals(agg_puts, strike_val, oi_val, vol_val)
            _add_strike_totals(group[1], strike_val, oi_val, vol_val)

    primary_basis: WallBasis = "OPEN_INTEREST" if (total_call_oi + total_put_oi) > 0 else "VOLUME"

    agg_call_walls = _rank_strike_totals(
        agg_calls, right="C", spot=spot, basis=primary_basis, top_n=top_n
    )
    agg_put_walls = _rank_strike_totals(
        agg_puts, right="P", spot=spot, basis=primary_basis, top_n=top_n
    )

    by_dte: list[DTEWallSummary] = []
    for dte_int, expiry_str in sorted(dte_groups):
        c_map, p_map = dte_groups[(dte_int, expiry_str)]
        dte_call_walls = _rank_strike_totals(
            c_map, right="C", spot=spot, basis=primary_basis, top_n=top_n
        )
        dte_put_walls = _rank_strike_totals(
            p_map, right="P", spot=spot, basis=primary_basis, top_n=top_n
        )
        by_dte.append(
            DTEWallSummary(
                expiry=expiry_str,
                dte=dte_int,
                primary_call_wall=dte_call_walls[0] if dte_call_walls else None,
                primary_put_wall=dte_put_walls[0] if dte_put_walls else None,
                top_call_walls=dte_call_walls,
                top_put_walls=dte_put_walls,
            )
        )

    return ChainWallsReport(
        underlying=underlying,
        underlying_price=spot,
        primary_basis=primary_basis,
        total_call_oi=total_call_oi,
        total_put_oi=total_put_oi,
        total_call_volume=total_call_volume,
        total_put_volume=total_put_volume,
        aggregate_primary_call_wall=agg_call_walls[0] if agg_call_walls else None,
        aggregate_primary_put_wall=agg_put_walls[0] if agg_put_walls else None,
        aggregate_top_call_walls=agg_call_walls,
        aggregate_top_put_walls=agg_put_walls,
        by_dte=by_dte,
    )


class WallsAnalyzer:
    """Facade for computing Put/Call walls across an option chain DataFrame."""

    def __init__(self, top_n: int = 3) -> None:
        self.top_n = top_n

    @staticmethod
    def compute_walls(df: pl.DataFrame, top_n: int = 3) -> ChainWallsReport:
        return compute_walls(df, top_n=top_n)

    def analyze(self, df: pl.DataFrame) -> ChainWallsReport:
        return compute_walls(df, top_n=self.top_n)
