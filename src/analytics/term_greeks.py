"""Multi-DTE option chain structure alignment across weekly `[30, 120]` expirations."""

from __future__ import annotations

from collections import Counter
from typing import Any

import polars as pl
from pydantic import BaseModel, ConfigDict


class StrikeProfilePoint(BaseModel):
    """Call/Put aligned quote, Greek, OI, and Volume profile at a single strike."""

    model_config = ConfigDict(extra="forbid")

    strike: float
    moneyness_pct: float
    call_mark: float | None = None
    put_mark: float | None = None
    call_iv: float | None = None
    put_iv: float | None = None
    call_delta: float | None = None
    put_delta: float | None = None
    call_gamma: float | None = None
    put_gamma: float | None = None
    call_theta: float | None = None
    put_theta: float | None = None
    call_vega: float | None = None
    put_vega: float | None = None
    call_oi: int | None = None
    put_oi: int | None = None
    call_volume: int | None = None
    put_volume: int | None = None


class DTEProfile(BaseModel):
    """Aligned strike profile and ATM summary for one expiration date."""

    model_config = ConfigDict(extra="forbid")

    expiry: str
    dte: int
    atm_strike: float
    atm_iv: float | None
    strikes: list[StrikeProfilePoint]


class MultiDTEProfilesReport(BaseModel):
    """Complete multi-DTE term and strike structure payload for UI / Plotly rendering."""

    model_config = ConfigDict(extra="forbid")

    underlying: str
    underlying_price: float
    snapshot_id: str
    market_data_type: int
    greeks_source_counts: dict[str, int]
    expiries: list[DTEProfile]


def _opt_float(val: Any) -> float | None:
    return float(val) if val is not None else None


def _opt_int(val: Any) -> int | None:
    return int(val) if val is not None else None


def build_multi_dte_profiles(
    df: pl.DataFrame, *, default_market_data_type: int = 3
) -> MultiDTEProfilesReport:
    """Align Call and Put chains by `(expiry, dte)` and `strike`."""
    if df.height == 0:
        return MultiDTEProfilesReport(
            underlying="",
            underlying_price=0.0,
            snapshot_id="",
            market_data_type=default_market_data_type,
            greeks_source_counts={},
            expiries=[],
        )

    first = df.row(0, named=True)
    underlying = str(first["underlying"])
    spot = float(first["underlying_price"])
    snapshot_id = str(first["snapshot_id"])
    market_data_type = int(first["market_data_type"])

    source_counts: dict[str, int] = dict(Counter(str(s) for s in df["greeks_source"].to_list()))

    sorted_df = df.sort(["dte", "expiry", "strike", "right"])
    expiries: list[DTEProfile] = []

    for key, group_df in sorted_df.group_by(["expiry", "dte"], maintain_order=True):
        expiry_str = str(key[0])
        dte_int = int(key[1])

        by_strike: dict[float, dict[str, Any]] = {}
        for row in group_df.iter_rows(named=True):
            strike_val = float(row["strike"])
            point_data = by_strike.get(strike_val)
            if point_data is None:
                moneyness = float(row["moneyness_pct"])
                point_data = {
                    "strike": strike_val,
                    "moneyness_pct": moneyness,
                }
                by_strike[strike_val] = point_data

            prefix = "call" if str(row["right"]) == "C" else "put"
            point_data[f"{prefix}_mark"] = _opt_float(row["mark_price"])
            point_data[f"{prefix}_iv"] = _opt_float(row["iv"])
            point_data[f"{prefix}_delta"] = _opt_float(row["delta"])
            point_data[f"{prefix}_gamma"] = _opt_float(row["gamma"])
            point_data[f"{prefix}_theta"] = _opt_float(row["theta"])
            point_data[f"{prefix}_vega"] = _opt_float(row["vega"])
            point_data[f"{prefix}_oi"] = _opt_int(row["open_interest"])
            point_data[f"{prefix}_volume"] = _opt_int(row["volume"])

        strike_points = [StrikeProfilePoint.model_validate(by_strike[s]) for s in sorted(by_strike)]

        atm_point = min(strike_points, key=lambda p: (abs(p.strike - spot), p.strike))
        atm_ivs = [v for v in (atm_point.call_iv, atm_point.put_iv) if v is not None]
        atm_iv: float | None = (sum(atm_ivs) / len(atm_ivs)) if atm_ivs else None

        expiries.append(
            DTEProfile(
                expiry=expiry_str,
                dte=dte_int,
                atm_strike=atm_point.strike,
                atm_iv=atm_iv,
                strikes=strike_points,
            )
        )

    return MultiDTEProfilesReport(
        underlying=underlying,
        underlying_price=spot,
        snapshot_id=snapshot_id,
        market_data_type=market_data_type,
        greeks_source_counts=source_counts,
        expiries=expiries,
    )


class MultiDTEStructureAnalyzer:
    """Facade for building multi-DTE strike and Greek profiles from an option chain DataFrame."""

    @staticmethod
    def build_multi_dte_profiles(df: pl.DataFrame) -> MultiDTEProfilesReport:
        return build_multi_dte_profiles(df)

    def analyze(self, df: pl.DataFrame) -> MultiDTEProfilesReport:
        return build_multi_dte_profiles(df)
