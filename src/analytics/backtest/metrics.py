"""Portfolio summary aggregation and trade record finalization helpers."""

from __future__ import annotations

from datetime import date

from src.analytics.backtest.models import (
    BacktestConfig,
    BacktestTradeRecord,
    EquityCurvePoint,
    ExitReason,
    PortfolioSummary,
    TradeDailyStep,
    TradeStatus,
    deduplicate_flags,
)
from src.analytics.backtest.rules import DENOM_EPS, EntryBuildResult
from src.analytics.strategies.models import StrategyKind
from src.storage.backtest_store import DailyChainBar


def pct_or_none(num: float | None, denom: float | None) -> float | None:
    """Return `round((num / denom) * 100, 2)` with zero-denominator guard."""
    if num is None or denom is None or denom <= DENOM_EPS:
        return None
    return round((num / denom) * 100.0, 2)


def finalize_trade_record(
    *,
    trade_id: str,
    entry_date: str,
    entry_snapshot_id: str,
    strategy_kind: StrategyKind,
    qty: int,
    entry: EntryBuildResult,
    open_commission: float,
    steps: list[TradeDailyStep],
    flags: list[str],
    status: TradeStatus,
    exit_reason: ExitReason,
    exit_date: str,
    exit_snapshot_id: str,
    exit_value_dollars: float | None,
    commissions_dollars: float,
    gross_pnl_dollars: float | None,
    net_pnl_dollars: float | None,
    last_bar: DailyChainBar,
) -> BacktestTradeRecord:
    """Convert mutable trade state into an immutable `BacktestTradeRecord`."""
    if status == "OPEN":
        exit_reason = "END_OF_DATA"
        exit_date, exit_snapshot_id = last_bar.market_date, last_bar.snapshot_id
        last_step = steps[-1]
        gross_pnl_dollars = last_step.gross_pnl_dollars
        net_pnl_dollars = last_step.net_pnl_dollars
        exit_value_dollars = (
            round(last_step.gross_pnl_dollars - entry.entry_cash_mid_dollars, 2)
            if last_step.gross_pnl_dollars is not None
            else None
        )
        commissions_dollars = open_commission

    days_held = max(0, (date.fromisoformat(exit_date) - date.fromisoformat(entry_date)).days)
    risk_denom = None if entry.max_loss_unbounded else entry.max_loss_dollars
    return BacktestTradeRecord(
        trade_id=trade_id,
        entry_date=entry_date,
        entry_snapshot_id=entry_snapshot_id,
        exit_date=exit_date,
        exit_snapshot_id=exit_snapshot_id,
        exit_reason=exit_reason,
        strategy_kind=strategy_kind,
        legs=entry.legs,
        qty=qty,
        entry_cash_mid_dollars=entry.entry_cash_mid_dollars,
        entry_cash_natural_dollars=entry.entry_cash_natural_dollars,
        entry_basis_dollars=entry.entry_basis_dollars,
        max_profit_dollars=entry.max_profit_dollars,
        max_loss_dollars=entry.max_loss_dollars,
        max_loss_unbounded=entry.max_loss_unbounded,
        exit_value_dollars=exit_value_dollars,
        commissions_dollars=commissions_dollars,
        gross_pnl_dollars=gross_pnl_dollars,
        net_pnl_dollars=net_pnl_dollars,
        return_on_basis_pct=pct_or_none(net_pnl_dollars, entry.entry_basis_dollars),
        return_on_risk_pct=pct_or_none(net_pnl_dollars, risk_denom),
        days_held=days_held,
        status=status,
        flags=deduplicate_flags(flags),
        steps=steps,
    )


def build_portfolio_summary(
    config: BacktestConfig,
    trades: list[BacktestTradeRecord],
    equity_curve: list[EquityCurvePoint],
    ok_bars: int,
    flagged_bars: int,
) -> PortfolioSummary:
    """Compute portfolio KPIs and drawdown summary with strict zero-division guards."""
    final_equity = equity_curve[-1].equity_dollars if equity_curve else config.initial_capital
    net_pnl = round(final_equity - config.initial_capital, 2)
    total_comms = round(sum(t.commissions_dollars for t in trades), 2)
    if all(t.status != "UNRESOLVED" for t in trades):
        gross_pnl = round(net_pnl + total_comms, 2)
    else:
        gross_pnl = round(
            sum(t.gross_pnl_dollars for t in trades if t.gross_pnl_dollars is not None), 2
        )

    closed = [t for t in trades if t.status == "CLOSED"]
    open_cnt = sum(1 for t in trades if t.status == "OPEN")
    closed_pnls = [t.net_pnl_dollars for t in closed if t.net_pnl_dollars is not None]
    wins = [p for p in closed_pnls if p > 0.0]
    losses = [p for p in closed_pnls if p < 0.0]

    total_wins, total_losses_abs = sum(wins), sum(-p for p in losses)
    max_dd = round(max((pt.drawdown_dollars for pt in equity_curve), default=0.0), 2)
    max_dd_pct = round(max((pt.drawdown_pct for pt in equity_curve), default=0.0), 2)

    return PortfolioSummary(
        initial_capital=config.initial_capital,
        final_equity=round(final_equity, 2),
        gross_pnl_dollars=gross_pnl,
        net_pnl_dollars=net_pnl,
        total_commissions_dollars=total_comms,
        total_return_pct=round((net_pnl / config.initial_capital) * 100.0, 2),
        trade_count=len(trades),
        closed_trades=len(closed),
        open_trades=open_cnt,
        winning_trades=len(wins),
        losing_trades=len(losses),
        win_rate_pct=round((len(wins) / len(closed_pnls)) * 100.0, 2) if closed_pnls else None,
        max_drawdown_dollars=max_dd,
        max_drawdown_pct=max_dd_pct,
        profit_factor=(
            round(total_wins / total_losses_abs, 4) if total_losses_abs > DENOM_EPS else None
        ),
        expectancy_dollars=round(sum(closed_pnls) / len(closed_pnls), 2) if closed_pnls else None,
        avg_win_dollars=round(total_wins / len(wins), 2) if wins else None,
        avg_loss_dollars=round(sum(losses) / len(losses), 2) if losses else None,
        ok_bars=ok_bars,
        flagged_bars=flagged_bars,
    )
