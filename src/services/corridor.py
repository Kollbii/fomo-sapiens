"""Wall Corridor glue between analytics and the broker subscription layer.

- `wall_anchors_from_report`: analytics walls report -> broker `WallAnchors`.
- `make_bsm_greeks_fallback`: binds the vectorized BSM fallback to the broker `GreeksFallback`.
- `CorridorQuoteCache`: in-memory live-quote overlay applied to cached chains for Strategy Lab;
  Mode 1 CSV archives on disk are never mutated.
"""

from __future__ import annotations

import polars as pl

from src.analytics.bsm_fallback import QuoteGreeks, greeks_for_quotes
from src.analytics.walls import ChainWallsReport
from src.broker.corridor import (
    BoolArray,
    CorridorQuoteFrame,
    CorridorQuoteRow,
    FloatArray,
    GreeksFallback,
    WallAnchors,
)

QuoteKey = tuple[str, float, str]


def _quote_key(expiry: str, strike: float, right: str) -> QuoteKey:
    return (expiry, round(float(strike), 6), right.upper())


def wall_anchors_from_report(report: ChainWallsReport) -> WallAnchors:
    """Primary Put/Call Wall strikes per expiry plus aggregate fallbacks."""
    by_expiry = {
        dte.expiry: (
            dte.primary_put_wall.strike if dte.primary_put_wall else None,
            dte.primary_call_wall.strike if dte.primary_call_wall else None,
        )
        for dte in report.by_dte
    }
    return WallAnchors(
        basis=report.primary_basis,
        aggregate_put=(
            report.aggregate_primary_put_wall.strike if report.aggregate_primary_put_wall else None
        ),
        aggregate_call=(
            report.aggregate_primary_call_wall.strike
            if report.aggregate_primary_call_wall
            else None
        ),
        by_expiry=by_expiry,
    )


def make_bsm_greeks_fallback(risk_free_rate: float) -> GreeksFallback:
    """Vectorized BSM Greeks fallback bound to the configured risk-free rate."""

    def _fallback(
        *,
        mark_price: FloatArray,
        spot: FloatArray,
        strike: FloatArray,
        dte: FloatArray,
        is_call: BoolArray,
        fallback_iv: FloatArray,
    ) -> QuoteGreeks:
        return greeks_for_quotes(
            mark_price=mark_price,
            spot=spot,
            strike=strike,
            dte=dte,
            is_call=is_call,
            fallback_iv=fallback_iv,
            risk_free_rate=risk_free_rate,
        )

    return _fallback


class CorridorQuoteCache:
    """Latest live corridor quotes per `(ticker, snapshot_id)`, overlaid onto cached chains."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], tuple[float, dict[QuoteKey, CorridorQuoteRow]]] = {}

    def record(self, frame: CorridorQuoteFrame) -> None:
        key = (frame.ticker, frame.snapshot_id)
        existing_spot, quote_map = self._entries.get(key, (0.0, {}))
        updated = dict(quote_map)
        for q in frame.quotes:
            updated[_quote_key(q.expiry, q.strike, q.right)] = q
        spot = frame.underlying_price if frame.underlying_price > 0.0 else existing_spot
        self._entries[key] = (spot, updated)

    def overlay(self, df: pl.DataFrame, symbol: str, snapshot_id: str) -> pl.DataFrame:
        """Return `df` with live spot/quotes/Greeks applied (unchanged when nothing cached)."""
        entry = self._entries.get((symbol, snapshot_id))
        if entry is None or not entry[1]:
            return df
        live_spot, quote_map = entry
        rows = df.to_dicts()
        changed = False
        for row in rows:
            if live_spot > 0.0:
                row["underlying_price"] = live_spot
                row["moneyness_pct"] = ((float(row["strike"]) - live_spot) / live_spot) * 100.0
                changed = True
            live_q = quote_map.get(
                _quote_key(str(row["expiry"]), float(row["strike"]), str(row["right"]))
            )
            if live_q is None:
                continue
            changed = True
            row["bid"] = live_q.bid
            row["ask"] = live_q.ask
            row["mid"] = live_q.mid
            row["last"] = live_q.last
            row["close"] = live_q.close
            for col in ("volume", "open_interest", "iv", "delta", "gamma", "theta", "vega"):
                value = getattr(live_q, col)
                if value is not None:
                    row[col] = value
            if live_q.greeks_source != "NONE":
                row["greeks_source"] = live_q.greeks_source
        if not changed:
            return df
        return pl.DataFrame(rows, schema=df.schema)
