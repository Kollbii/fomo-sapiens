"""Unusual option activity scanner (`Volume > OI` and intra-expiration Volume Z-score)."""

from __future__ import annotations

from typing import Literal

import polars as pl
from pydantic import BaseModel, ConfigDict

OptionRight = Literal["C", "P"]

DEFAULT_ZSCORE_THRESHOLD: float = 2.0
_STD_EPSILON: float = 1e-9


class ContractAnomaly(BaseModel):
    """Single option contract flagged for unusual volume activity."""

    model_config = ConfigDict(extra="forbid")

    option_conid: int
    expiry: str
    dte: int
    target_dte_bucket: int
    strike: float
    right: OptionRight
    moneyness_pct: float
    mark_price: float | None
    volume: int
    open_interest: int | None
    vol_oi_ratio: float | None
    volume_zscore: float
    is_vol_gt_oi: bool
    is_high_vol_zscore: bool


class AnomaliesReport(BaseModel):
    """Summary and ranked list of unusual activity anomalies across the option chain."""

    model_config = ConfigDict(extra="forbid")

    underlying: str
    total_flagged: int
    vol_gt_oi_count: int
    high_zscore_count: int
    anomalies: list[ContractAnomaly]


def scan_anomalies(
    df: pl.DataFrame,
    zscore_threshold: float = DEFAULT_ZSCORE_THRESHOLD,
) -> AnomaliesReport:
    """Detect `Volume > Open Interest` and cross-strike Volume Z-score outliers."""
    if zscore_threshold <= 0.0:
        raise ValueError("zscore_threshold must be > 0")

    if df.height == 0:
        return AnomaliesReport(
            underlying="",
            total_flagged=0,
            vol_gt_oi_count=0,
            high_zscore_count=0,
            anomalies=[],
        )

    underlying = str(df.row(0, named=True)["underlying"])

    active = df.filter(pl.col("volume").is_not_null() & (pl.col("volume") > 0))
    if active.height == 0:
        return AnomaliesReport(
            underlying=underlying,
            total_flagged=0,
            vol_gt_oi_count=0,
            high_zscore_count=0,
            anomalies=[],
        )

    with_stats = active.with_columns(
        pl.col("volume").cast(pl.Float64).mean().over(["expiry", "right"]).alias("_mean_vol"),
        pl.col("volume").cast(pl.Float64).std(ddof=0).over(["expiry", "right"]).alias("_std_vol"),
    )

    flagged: list[ContractAnomaly] = []
    vol_gt_oi_count = 0
    high_zscore_count = 0

    for row in with_stats.iter_rows(named=True):
        vol_int = int(row["volume"])
        raw_oi = row["open_interest"]
        oi_int: int | None = int(raw_oi) if raw_oi is not None else None

        if oi_int is not None and oi_int > 0:
            vol_oi_ratio: float | None = float(vol_int) / float(oi_int)
            is_vol_gt_oi = vol_int > oi_int
        else:
            vol_oi_ratio = None
            is_vol_gt_oi = False

        mean_val = row["_mean_vol"]
        std_val = row["_std_vol"]
        if mean_val is None or std_val is None or float(std_val) < _STD_EPSILON:
            zscore = 0.0
        else:
            zscore = (float(vol_int) - float(mean_val)) / float(std_val)

        is_high_zscore = zscore >= zscore_threshold

        if not (is_vol_gt_oi or is_high_zscore):
            continue

        if is_vol_gt_oi:
            vol_gt_oi_count += 1
        if is_high_zscore:
            high_zscore_count += 1

        raw_mark = row["mark_price"]
        mark_val: float | None = float(raw_mark) if raw_mark is not None else None
        right_str: OptionRight = "C" if str(row["right"]) == "C" else "P"

        flagged.append(
            ContractAnomaly(
                option_conid=int(row["option_conid"]),
                expiry=str(row["expiry"]),
                dte=int(row["dte"]),
                target_dte_bucket=int(row["target_dte_bucket"]),
                strike=float(row["strike"]),
                right=right_str,
                moneyness_pct=float(row["moneyness_pct"]),
                mark_price=mark_val,
                volume=vol_int,
                open_interest=oi_int,
                vol_oi_ratio=vol_oi_ratio,
                volume_zscore=zscore,
                is_vol_gt_oi=is_vol_gt_oi,
                is_high_vol_zscore=is_high_zscore,
            )
        )

    flagged.sort(
        key=lambda a: (
            a.is_vol_gt_oi,
            a.vol_oi_ratio is not None,
            a.vol_oi_ratio if a.vol_oi_ratio is not None else float("-inf"),
            a.volume_zscore,
        ),
        reverse=True,
    )

    return AnomaliesReport(
        underlying=underlying,
        total_flagged=len(flagged),
        vol_gt_oi_count=vol_gt_oi_count,
        high_zscore_count=high_zscore_count,
        anomalies=flagged,
    )


class AnomalyScanner:
    """Facade for scanning option chain DataFrames for unusual volume/OI activity."""

    def __init__(self, zscore_threshold: float = DEFAULT_ZSCORE_THRESHOLD) -> None:
        self.zscore_threshold = zscore_threshold

    @staticmethod
    def scan_anomalies(
        df: pl.DataFrame,
        zscore_threshold: float = DEFAULT_ZSCORE_THRESHOLD,
    ) -> AnomaliesReport:
        return scan_anomalies(df, zscore_threshold=zscore_threshold)

    def scan(self, df: pl.DataFrame) -> AnomaliesReport:
        return scan_anomalies(df, zscore_threshold=self.zscore_threshold)
