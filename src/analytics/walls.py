"""Put Wall and Call Wall identification across Open Interest and Volume bases."""

from __future__ import annotations

from typing import Literal

import polars as pl
from pydantic import BaseModel, ConfigDict

WallBasis = Literal["OPEN_INTEREST", "VOLUME"]
OptionRight = Literal["C", "P"]


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
    """Call and Put wall summary for a single expiration / target DTE bucket."""

    model_config = ConfigDict(extra="forbid")

    target_dte_bucket: int
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
    by_dte_bucket: list[DTEWallSummary]


def _rank_side_walls(
    df_scope: pl.DataFrame,
    *,
    right: OptionRight,
    spot: float,
    basis: WallBasis,
    top_n: int,
) -> list[WallLevel]:
    """Aggregate by strike for one option side (`C` or `P`) and return top `top_n` walls."""
    side_df = df_scope.filter(pl.col("right") == right)
    if side_df.height == 0:
        return []

    grouped = side_df.group_by("strike", maintain_order=True).agg(
        pl.col("oi_clean").sum().alias("open_interest"),
        pl.col("vol_clean").sum().alias("volume"),
    )

    metric_col = "open_interest" if basis == "OPEN_INTEREST" else "volume"
    tie_col = "volume" if basis == "OPEN_INTEREST" else "open_interest"

    total_side_metric = int(grouped[metric_col].sum())
    denom = max(total_side_metric, 1)

    positive = grouped.filter(pl.col(metric_col) > 0).sort(
        by=[metric_col, tie_col, "strike"],
        descending=[True, True, False],
    )
    if positive.height == 0:
        return []

    levels: list[WallLevel] = []
    for row in positive.head(top_n).iter_rows(named=True):
        strike_val = float(row["strike"])
        oi_val = int(row["open_interest"])
        vol_val = int(row["volume"])
        metric_val = int(row[metric_col])
        dist_pct = ((strike_val - spot) / spot) * 100.0 if spot > 0.0 else 0.0
        conc_ratio = float(metric_val) / float(denom)
        levels.append(
            WallLevel(
                strike=strike_val,
                right=right,
                open_interest=oi_val,
                volume=vol_val,
                metric_value=metric_val,
                distance_from_spot_pct=dist_pct,
                concentration_ratio=conc_ratio,
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
            by_dte_bucket=[],
        )

    first = df.row(0, named=True)
    underlying = str(first["underlying"])
    spot = float(first["underlying_price"])

    prepared = df.with_columns(
        pl.col("open_interest").fill_null(0).cast(pl.Int64).alias("oi_clean"),
        pl.col("volume").fill_null(0).cast(pl.Int64).alias("vol_clean"),
    )

    calls_df = prepared.filter(pl.col("right") == "C")
    puts_df = prepared.filter(pl.col("right") == "P")

    total_call_oi = int(calls_df["oi_clean"].sum()) if calls_df.height > 0 else 0
    total_put_oi = int(puts_df["oi_clean"].sum()) if puts_df.height > 0 else 0
    total_call_volume = int(calls_df["vol_clean"].sum()) if calls_df.height > 0 else 0
    total_put_volume = int(puts_df["vol_clean"].sum()) if puts_df.height > 0 else 0

    primary_basis: WallBasis = "OPEN_INTEREST" if (total_call_oi + total_put_oi) > 0 else "VOLUME"

    agg_call_walls = _rank_side_walls(
        prepared, right="C", spot=spot, basis=primary_basis, top_n=top_n
    )
    agg_put_walls = _rank_side_walls(
        prepared, right="P", spot=spot, basis=primary_basis, top_n=top_n
    )

    sorted_df = prepared.sort(["dte", "target_dte_bucket", "expiry", "strike"])
    by_dte: list[DTEWallSummary] = []

    for key, group_df in sorted_df.group_by(
        ["target_dte_bucket", "expiry", "dte"], maintain_order=True
    ):
        bucket_int = int(key[0])
        expiry_str = str(key[1])
        dte_int = int(key[2])

        dte_call_walls = _rank_side_walls(
            group_df, right="C", spot=spot, basis=primary_basis, top_n=top_n
        )
        dte_put_walls = _rank_side_walls(
            group_df, right="P", spot=spot, basis=primary_basis, top_n=top_n
        )
        by_dte.append(
            DTEWallSummary(
                target_dte_bucket=bucket_int,
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
        by_dte_bucket=by_dte,
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
