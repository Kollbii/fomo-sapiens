"""Paged option chain qualification and market-data collection (`<= 80` contracts/page)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Literal

from ib_async import Option, Stock

from src.broker.client import (
    IBKRBrokerClient,
    SleepFn,
    market_data_type_label,
    poll_until,
)
from src.broker.contracts import ChainPlan, ContractDiscoveryService, market_date_for
from src.broker.parser import (
    SnapshotContext,
    TickerParser,
    has_live_opra_data,
    has_usable_quote,
)
from src.core.config import Settings
from src.core.exceptions import OptionChainFetchError, PacingViolationError
from src.core.security import TickerValidator
from src.storage.csv_repository import make_snapshot_id
from src.storage.schema import OptionContractRow, SpotSource, sanitize_price

logger = logging.getLogger(__name__)

DELAYED_GENERIC_TICKS = "100,101,106"
LIVE_OPRA_GENERIC_TICKS = "100,101,104,106"
EMPTY_QUOTE_FALLBACK_THRESHOLD = 0.80
PACING_COOLDOWN_SEC = 1.0
# ib_async sends at most 45 messages per 1 s window (`Client.MaxRequests`) and queues the rest.
# Requests and cancels go out in bursts of 20 with a 0.55 s pause before each burst, so any
# 1 s window holds at most 2 bursts (40 messages) and nothing waits in the ib_async queue.
PAGE_PACING_CHUNK_SIZE = 20
PAGE_PACING_PAUSE_SEC = 0.55


@dataclass(frozen=True)
class FetchResult:
    """Result of a full 5-stage option chain refresh for one ticker."""

    snapshot_id: str
    snapshot_ts_utc: str
    market_date: str
    market_data_type: int
    plan: ChainPlan
    rows: list[OptionContractRow]
    pages_fetched: int
    elapsed_ms: float
    notices: tuple[str, ...] = ()
    page_quote_counts: tuple[int, ...] = ()
    primary_contract_count: int | None = None
    short_dte_contract_count: int = 0
    short_dte_range: tuple[int, int] | None = None


def _row_quote_score(row: OptionContractRow) -> int:
    """Rank quote richness so deduplication keeps better-quoted rows."""
    if row.bid is not None and row.ask is not None:
        return 3
    if row.bid is not None or row.ask is not None or row.last is not None:
        return 2
    if row.close is not None:
        return 1
    return 0


def _merge_and_dedupe_rows(
    pass1_rows: Sequence[OptionContractRow],
    pass2_rows: Sequence[OptionContractRow],
) -> list[OptionContractRow]:
    """Deduplicate merged Pass 1 + Pass 2 rows by `(expiry, round(strike, 4), right)`."""
    best: dict[tuple[str, float, str], tuple[tuple[int, int], OptionContractRow]] = {}
    for is_pass1, batch in ((1, pass1_rows), (0, pass2_rows)):
        for row in batch:
            key = (row.expiry, round(row.strike, 4), row.right)
            rank = (_row_quote_score(row), is_pass1)
            existing = best.get(key)
            if existing is None or rank > existing[0]:
                best[key] = (rank, row)
    return sorted(
        (pair[1] for pair in best.values()),
        key=lambda r: (r.expiry, r.strike, r.right),
    )


@dataclass(frozen=True)
class ExactContractSpec:
    """One specific option contract to quote (no chain discovery)."""

    option_conid: int
    expiry: str  # ISO YYYY-MM-DD
    strike: float
    right: Literal["C", "P"]

    def key(self) -> tuple[str, float, str]:
        return (self.expiry, round(self.strike, 4), self.right)


@dataclass(frozen=True)
class ExactQuoteResult:
    """Quotes for specific contracts: rows in the normal 27-column schema plus the spot."""

    snapshot_id: str
    snapshot_ts_utc: str
    market_date: str
    market_data_type: int
    underlying_price: float
    spot_source: SpotSource
    rows: tuple[OptionContractRow, ...]
    missing: tuple[ExactContractSpec, ...]
    notices: tuple[str, ...] = ()


def _contract_key(contract: Any) -> tuple[str, float, str]:
    raw = str(getattr(contract, "lastTradeDateOrContractMonth", ""))[:8]
    iso = f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}" if len(raw) == 8 else raw
    return (iso, round(float(getattr(contract, "strike", 0.0)), 4), str(contract.right))


def paginate_contracts(
    contracts: Sequence[Any],
    max_per_page: int = 80,
    max_pages: int | None = 5,
) -> list[list[Any]]:
    """Split `contracts` into pages of `<= max_per_page`, enforcing the page/contract ceiling."""
    if max_per_page < 1:
        raise ValueError("max_per_page must be >= 1")
    if max_pages is not None and max_pages < 1:
        raise ValueError("max_pages must be >= 1")
    if max_pages is not None and len(contracts) > max_per_page * max_pages:
        raise PacingViolationError(
            f"Contract count {len(contracts)} exceeds hard ceiling "
            f"{max_per_page * max_pages} ({max_pages} pages x {max_per_page}/page)"
        )
    return [list(contracts[i : i + max_per_page]) for i in range(0, len(contracts), max_per_page)]


def empty_quote_ratio(tickers: Sequence[Any]) -> float:
    """Fraction of tickers on a page lacking any usable `bid`, `ask`, `last`, or `close`."""
    if not tickers:
        return 0.0
    empty = sum(1 for t in tickers if not has_usable_quote(t))
    return empty / len(tickers)


def has_real_quote(ticker: Any) -> bool:
    """True when a ticker has a live or delayed bid or ask (not solely prior session `close`)."""
    return any(
        sanitize_price(getattr(ticker, attr, None)) is not None
        for attr in ("bid", "ask", "delayedBid", "delayedAsk")
    )


def empty_real_quote_ratio(tickers: Sequence[Any]) -> float:
    """Fraction of tickers on a page lacking a current live or delayed `bid`/`ask`."""
    if not tickers:
        return 0.0
    empty = sum(1 for t in tickers if not has_real_quote(t))
    return empty / len(tickers)


def _is_qualified_contract(item: Any) -> bool:
    return (
        item is not None and not isinstance(item, list) and int(getattr(item, "conId", 0) or 0) > 0
    )


class PagedOptionChainFetcher:
    """Downloads option chain market data in bounded pages (`<= 80` contracts/page)."""

    def __init__(
        self,
        client: IBKRBrokerClient,
        settings: Settings | None = None,
        discovery: ContractDiscoveryService | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: SleepFn = asyncio.sleep,
    ) -> None:
        self.client = client
        self.settings = settings or client.settings
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sleep = sleep
        self.discovery = discovery or ContractDiscoveryService(
            client, self.settings, clock=self.clock, sleep=self.sleep
        )
        self.active_cooldown_sec: float = self.settings.options_chain.inter_page_cooldown_sec
        self._notices: list[str] = []

    @property
    def max_contracts_per_page(self) -> int:
        return self.settings.options_chain.max_contracts_per_page

    @property
    def max_pages_per_refresh(self) -> int:
        return self.settings.options_chain.max_pages_per_refresh

    def build_candidate_contracts(self, plan: ChainPlan) -> list[Option]:
        """Build exchange-verified `(expiry, strike, right)` candidate `Option` contracts."""
        candidates: list[Option] = []
        if plan.expiration_specs:
            for spec in plan.expiration_specs:
                for strike in spec.strikes:
                    for right in ("C", "P"):
                        if spec.exact_strikes_verified:
                            prequalified = spec.qualified_contracts.get((round(strike, 6), right))
                            if prequalified is not None:
                                candidates.append(prequalified)
                            continue
                        candidates.append(
                            Option(
                                symbol=plan.underlying.symbol,
                                lastTradeDateOrContractMonth=spec.match.ib_expiry,
                                strike=strike,
                                right=right,
                                exchange=plan.exchange,
                                multiplier=spec.multiplier,
                                currency="USD",
                                tradingClass=spec.trading_class,
                            )
                        )
            return candidates

        for exp in plan.expirations:
            for strike in plan.strikes.strikes:
                for right in ("C", "P"):
                    candidates.append(
                        Option(
                            symbol=plan.underlying.symbol,
                            lastTradeDateOrContractMonth=exp.ib_expiry,
                            strike=strike,
                            right=right,
                            exchange=plan.exchange,
                            multiplier=plan.multiplier,
                            currency="USD",
                            tradingClass=plan.trading_class,
                        )
                    )
        return candidates

    def partition_pages(self, contracts: Sequence[Any]) -> list[list[Any]]:
        """Partition `contracts` into pages of `<= max_contracts_per_page` (`80`)."""
        return paginate_contracts(
            contracts,
            max_per_page=self.max_contracts_per_page,
            max_pages=self.max_pages_per_refresh,
        )

    async def qualify_option_contracts(self, candidates: Sequence[Any]) -> list[Any]:
        """Qualify any un-qualified `Option` contracts in micro-batches (`<= 80` per call)."""
        if not candidates:
            raise OptionChainFetchError("No candidate option contracts to qualify")

        qualified_by_idx: dict[int, Any] = {}
        unqualified_items: list[tuple[int, Any]] = []
        for idx, candidate in enumerate(candidates):
            if int(getattr(candidate, "conId", 0) or 0) > 0:
                qualified_by_idx[idx] = candidate
            else:
                unqualified_items.append((idx, candidate))

        extra_qualified: list[Any] = []
        if unqualified_items:
            pages = paginate_contracts(
                unqualified_items, max_per_page=self.max_contracts_per_page, max_pages=None
            )
            for batch in pages:
                raw_batch = [contract for _, contract in batch]
                res = list(
                    await self.client.run_stage(
                        f"contract qualification ({len(raw_batch)} contracts)",
                        self.client.ib.qualifyContractsAsync(*raw_batch),
                    )
                    or []
                )
                if len(res) == len(batch):
                    for (orig_idx, _), item in zip(batch, res, strict=True):
                        if _is_qualified_contract(item):
                            qualified_by_idx[orig_idx] = item
                else:
                    extra_qualified.extend(item for item in res if _is_qualified_contract(item))

        qualified = [
            qualified_by_idx[idx] for idx in range(len(candidates)) if idx in qualified_by_idx
        ]
        qualified.extend(extra_qualified)
        if not qualified:
            raise OptionChainFetchError("IBKR failed to qualify any option contracts in chain")
        dropped = len(candidates) - len(qualified)
        if dropped > 0:
            drop_msg = (
                f"Filtered out {dropped} unqualified contract(s) rejected by IBKR security "
                f"definition lookup; continuing with {len(qualified)} verified contract(s)."
            )
            self._notices.append(drop_msg)
            logger.warning(drop_msg)
        return qualified

    async def _collect_page_once(
        self,
        page_contracts: Sequence[Any],
        *,
        generic_tick_list: str,
        ready_predicate: Callable[[Any], bool],
    ) -> list[Any]:
        """Subscribe a single page, poll until `ready_predicate` or timeout, and always cancel."""
        if len(page_contracts) > self.max_contracts_per_page:
            raise PacingViolationError(
                f"Page size {len(page_contracts)} exceeds max_contracts_per_page "
                f"({self.max_contracts_per_page})"
            )
        oc = self.settings.options_chain
        ib = self.client.ib
        stage = f"market-data collection ({len(page_contracts)} contracts)"
        subscribed: list[Any] = []
        tickers: list[Any] = []
        try:
            for idx, contract in enumerate(page_contracts):
                if idx % PAGE_PACING_CHUNK_SIZE == 0:
                    await self.sleep(PAGE_PACING_PAUSE_SEC)
                ticker = ib.reqMktData(
                    contract,
                    genericTickList=generic_tick_list,
                    snapshot=False,
                    regulatorySnapshot=False,
                )
                subscribed.append(contract)
                tickers.append(ticker)

            # The poll window starts only after the last request of the page was sent.
            await poll_until(
                lambda: all(ready_predicate(t) for t in tickers),
                oc.page_collect_timeout_sec,
                oc.page_poll_interval_sec,
                self.sleep,
            )
            self.client.ensure_still_connected(stage=stage)
            return tickers
        except Exception as exc:
            self.client.handle_stage_error(exc, stage=stage)
            raise
        finally:
            await self._cancel_paced(subscribed)

    async def _cancel_paced(self, subscribed: Sequence[Any]) -> None:
        """Cancel every subscription in paced bursts; never skips a cancel.

        If a pause is interrupted, the remaining cancels are sent without pauses. A
        `CancelledError` (or other `BaseException`) is re-raised after all cancels are sent.
        """
        ib = self.client.ib
        pacing = True
        deferred: BaseException | None = None
        for idx, contract in enumerate(subscribed):
            if pacing and idx % PAGE_PACING_CHUNK_SIZE == 0:
                try:
                    await self.sleep(PAGE_PACING_PAUSE_SEC)
                except Exception:
                    pacing = False
                except BaseException as exc:  # e.g. asyncio.CancelledError
                    pacing = False
                    deferred = exc
            try:
                ib.cancelMktData(contract)
            except Exception:  # pragma: no cover - defensive
                logger.exception("cancelMktData failed for %s", contract)
        if deferred is not None:
            raise deferred

    async def _collect_page_with_fallback(
        self,
        page_contracts: Sequence[Any],
        *,
        generic_tick_list: str,
        initial_ready: Callable[[Any], bool],
        allow_fallback: bool = True,
        check_live_downgrade: bool = False,
    ) -> list[Any]:
        """Collect one page with pacing retry, live->delayed downgrade, and empty-quote fallback."""
        tickers = await self._collect_page_once(
            page_contracts,
            generic_tick_list=generic_tick_list,
            ready_predicate=initial_ready,
        )

        if check_live_downgrade and self.client.live_subscription_missing:
            sub_notice = self.client.consume_subscription_fallback_notice()
            if sub_notice and sub_notice not in self._notices:
                self._notices.append(sub_notice)
            return await self._fetch_page_delayed(page_contracts, allow_fallback=allow_fallback)

        if self.client.consume_pacing_warning():
            self.active_cooldown_sec = max(self.active_cooldown_sec, PACING_COOLDOWN_SEC)
            await self.sleep(self.active_cooldown_sec)
            tickers = await self._collect_page_once(
                page_contracts,
                generic_tick_list=generic_tick_list,
                ready_predicate=initial_ready,
            )

        if (
            allow_fallback
            and self.client.can_fallback()
            and empty_real_quote_ratio(tickers) > EMPTY_QUOTE_FALLBACK_THRESHOLD
        ):
            prev_type = self.client.current_type
            new_type = self.client.switch_to_fallback_data_type()
            notice = (
                f"Over 80% of Type {prev_type} ({market_data_type_label(prev_type)}) contracts "
                f"had no bid/ask; switched to MarketDataType({new_type}) "
                f"{market_data_type_label(new_type)}"
            )
            self._notices.append(notice)
            logger.info(notice)
            tickers = await self._collect_page_once(
                page_contracts,
                generic_tick_list=generic_tick_list,
                ready_predicate=has_real_quote,
            )

        self.client.observe_tickers_market_data_type(tickers)
        return tickers

    async def _fetch_page_delayed(
        self,
        page_contracts: Sequence[Any],
        *,
        allow_fallback: bool = True,
    ) -> list[Any]:
        """Collect one page (`<= 80` contracts) under Delayed market data cascade (`3 -> 4`)."""
        return await self._collect_page_with_fallback(
            page_contracts,
            generic_tick_list=DELAYED_GENERIC_TICKS,
            initial_ready=has_real_quote,
            allow_fallback=allow_fallback,
            check_live_downgrade=False,
        )

    async def _fetch_page_live_opra(
        self,
        page_contracts: Sequence[Any],
        *,
        allow_fallback: bool = True,
    ) -> list[Any]:
        """Collect one page (`<= 80` contracts) in Live mode (`1 -> 2` or `10089/354 -> 3`)."""
        return await self._collect_page_with_fallback(
            page_contracts,
            generic_tick_list=LIVE_OPRA_GENERIC_TICKS,
            initial_ready=has_live_opra_data,
            allow_fallback=allow_fallback,
            check_live_downgrade=True,
        )

    async def fetch_pages(self, contracts: Sequence[Any]) -> tuple[list[Any], int]:
        """Partition `contracts` into `<= 80`-contract pages and collect all tickers."""
        pages = self.partition_pages(contracts)
        self.active_cooldown_sec = self.settings.options_chain.inter_page_cooldown_sec
        all_tickers: list[Any] = []

        for idx, page in enumerate(pages):
            use_live = self.client.mode == "LIVE" and not self.client.live_subscription_missing
            if use_live:
                page_tickers = await self._fetch_page_live_opra(page, allow_fallback=(idx == 0))
            else:
                page_tickers = await self._fetch_page_delayed(page, allow_fallback=(idx == 0))
            all_tickers.extend(page_tickers)

            if self.client.consume_pacing_warning():
                self.active_cooldown_sec = max(self.active_cooldown_sec, PACING_COOLDOWN_SEC)

            if idx + 1 < len(pages) and self.active_cooldown_sec > 0:
                await self.sleep(self.active_cooldown_sec)

        return all_tickers, len(pages)

    async def fetch_option_chain(
        self,
        ticker: str,
        range_pct: float | None = None,
        strike_step: float | None = None,
        dte_min: int | None = None,
        dte_max: int | None = None,
        *,
        include_short_dte: bool = False,
        short_dte_min: int | None = None,
        short_dte_max: int | None = None,
    ) -> FetchResult:
        """Execute the full 5-stage pipeline (and optional sequential short-DTE pass)."""
        t0 = time.perf_counter()
        self._notices = []
        oc = self.settings.options_chain
        eff_dte_min = oc.dte_min if dte_min is None else int(dte_min)
        eff_dte_max = oc.dte_max if dte_max is None else int(dte_max)
        eff_short_min = oc.short_dte_min if short_dte_min is None else int(short_dte_min)
        eff_short_max = oc.short_dte_max if short_dte_max is None else int(short_dte_max)
        if eff_short_min > eff_short_max:
            raise OptionChainFetchError(
                f"Invalid short DTE bounds: {eff_short_min} > {eff_short_max} for {ticker}"
            )

        await self.client.ensure_connected()
        self.client.reset_market_data_type()

        plan = await self.discovery.build_plan(
            ticker,
            range_pct=range_pct,
            strike_step=strike_step,
            dte_min=eff_dte_min,
            dte_max=eff_dte_max,
        )
        candidates = self.build_candidate_contracts(plan)
        qualified = await self.qualify_option_contracts(candidates)
        tickers, pages_fetched = await self.fetch_pages(qualified)

        sub_notice = self.client.consume_subscription_fallback_notice()
        if sub_notice and sub_notice not in self._notices:
            self._notices.append(sub_notice)
        active_md_type = self.client.get_active_market_data_type()

        now_utc = self.clock().astimezone(UTC)
        snapshot_id = make_snapshot_id(plan.underlying.symbol, now_utc)
        snapshot_ts_utc = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        market_date_str = plan.market_date.isoformat()

        ctx = SnapshotContext(
            snapshot_id=snapshot_id,
            snapshot_ts_utc=snapshot_ts_utc,
            market_date=market_date_str,
            market_data_type=active_md_type,
            underlying=plan.underlying.symbol,
            underlying_conid=plan.underlying.conid,
            underlying_price=plan.underlying.spot,
        )
        dte_map = {m.ib_expiry: m.dte for m in plan.expirations}

        rows1: list[OptionContractRow] = []
        for contract, tk in zip(qualified, tickers, strict=True):
            ib_exp = str(getattr(contract, "lastTradeDateOrContractMonth", ""))[:8]
            dte = dte_map.get(ib_exp)
            rows1.append(
                TickerParser.parse_ticker(
                    tk,
                    contract=contract,
                    context=ctx,
                    dte=dte,
                )
            )

        page_size = self.max_contracts_per_page
        page_quote_counts1 = tuple(
            sum(1 for tk in tickers[i : i + page_size] if has_real_quote(tk))
            for i in range(0, len(tickers), page_size)
        )

        # Sequential Pass 2 for [short_dte_min, min(short_dte_max, eff_dte_min - 1)] after Pass 1
        # has completed 100% of its cancelMktData calls inside _collect_page_once's finally:.
        pass2_min = eff_short_min
        pass2_max = min(eff_short_max, eff_dte_min - 1)
        rows2: list[OptionContractRow] = []
        pages2 = 0
        page_quote_counts2: tuple[int, ...] = ()
        short_range_used: tuple[int, int] | None = None
        all_dtes_matched = [m.dte for m in plan.expirations]

        if include_short_dte and pass2_min <= pass2_max:
            try:
                plan2 = await self.discovery.build_plan(
                    ticker,
                    range_pct=range_pct,
                    strike_step=strike_step,
                    dte_min=pass2_min,
                    dte_max=pass2_max,
                    underlying_info=plan.underlying,
                    smart_chains=plan.smart_chains or None,
                    market_date=plan.market_date,
                )
                candidates2 = self.build_candidate_contracts(plan2)
                qualified2 = await self.qualify_option_contracts(candidates2)
                if self.active_cooldown_sec > 0:
                    await self.sleep(self.active_cooldown_sec)
                tickers2, pages2 = await self.fetch_pages(qualified2)

                sub_notice2 = self.client.consume_subscription_fallback_notice()
                if sub_notice2 and sub_notice2 not in self._notices:
                    self._notices.append(sub_notice2)

                dte_map2 = {m.ib_expiry: m.dte for m in plan2.expirations}
                for contract, tk in zip(qualified2, tickers2, strict=True):
                    ib_exp = str(getattr(contract, "lastTradeDateOrContractMonth", ""))[:8]
                    dte2 = dte_map2.get(ib_exp)
                    rows2.append(
                        TickerParser.parse_ticker(
                            tk,
                            contract=contract,
                            context=ctx,
                            dte=dte2,
                        )
                    )
                page_quote_counts2 = tuple(
                    sum(1 for tk in tickers2[i : i + page_size] if has_real_quote(tk))
                    for i in range(0, len(tickers2), page_size)
                )
                short_range_used = (pass2_min, pass2_max)
                all_dtes_matched = sorted({*all_dtes_matched, *(m.dte for m in plan2.expirations)})
                for notice2 in plan2.notices:
                    tagged = f"[Backtest {pass2_min}–{pass2_max}d] {notice2}"
                    if tagged not in self._notices:
                        self._notices.append(tagged)
            except OptionChainFetchError as exc:
                skip_msg = f"Backtest short-DTE [{pass2_min}, {pass2_max}] sync skipped: {exc}"
                self._notices.append(skip_msg)
                logger.info("%s", skip_msg)

        if rows2:
            rows = _merge_and_dedupe_rows(rows1, rows2)
            self._notices.append(
                f"Synced {eff_dte_min}–{eff_dte_max}d ({len(rows1)} contracts) + "
                f"{pass2_min}–{pass2_max}d for Backtest ({len(rows2)} contracts) -> "
                f"{len(rows)} total archived"
            )
        else:
            rows = rows1

        total_pages = pages_fetched + pages2
        page_quote_counts = (*page_quote_counts1, *page_quote_counts2)
        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        source_counts = dict(Counter(r.greeks_source for r in rows))
        logger.info(
            "refresh_summary ticker=%s market_data_type=%d spot_price=%.4f "
            "dtes_matched=%s range_pct=%.4f strike_step=%.4f total_contracts=%d "
            "pages_fetched=%d page_quote_counts=%s spot_source=%s elapsed_ms=%.2f "
            "greeks_source_counts=%s snapshot_id=%s",
            plan.underlying.symbol,
            active_md_type,
            plan.underlying.spot,
            all_dtes_matched,
            plan.range_pct,
            plan.strikes.effective_step,
            len(rows),
            total_pages,
            list(page_quote_counts),
            plan.underlying.spot_source,
            elapsed_ms,
            source_counts,
            snapshot_id,
        )

        notices = (*plan.notices, *self._notices)
        return FetchResult(
            snapshot_id=snapshot_id,
            snapshot_ts_utc=snapshot_ts_utc,
            market_date=market_date_str,
            market_data_type=active_md_type,
            plan=plan,
            rows=rows,
            pages_fetched=total_pages,
            elapsed_ms=elapsed_ms,
            notices=notices,
            page_quote_counts=page_quote_counts,
            primary_contract_count=len(rows1),
            short_dte_contract_count=len(rows2),
            short_dte_range=short_range_used,
        )

    async def fetch_exact_contracts(
        self,
        ticker: str,
        specs: Sequence[ExactContractSpec],
        underlying_conid: int = 0,
    ) -> ExactQuoteResult:
        """Quote specific contracts only (tracked trade legs); never requests a chain.

        Read-only: `reqMktData` / `cancelMktData` through the same paced pages as a chain
        refresh, plus `qualifyContractsAsync` only for contracts lacking a known `conId`.
        Duplicate legs are deduplicated by `(expiry, strike, right)`. Expired contracts are
        not requested and are returned in `missing`.
        """
        symbol = TickerValidator.validate(ticker)
        if not specs:
            raise OptionChainFetchError(f"No contracts to quote for {symbol}")
        deduped: dict[tuple[str, float, str], ExactContractSpec] = {}
        for spec in specs:
            key = spec.key()
            existing = deduped.get(key)
            if existing is None or (existing.option_conid <= 0 and spec.option_conid > 0):
                deduped[key] = spec
        unique_specs = tuple(deduped.values())

        cap = self.max_contracts_per_page * self.max_pages_per_refresh
        if len(unique_specs) > cap:
            raise PacingViolationError(
                f"{len(unique_specs)} tracked contracts for {symbol} exceed the {cap}-contract cap"
            )
        self._notices = []
        await self.client.ensure_connected()
        self.client.reset_market_data_type()

        if underlying_conid > 0:
            stock: Any = Stock(
                conId=underlying_conid, symbol=symbol, exchange="SMART", currency="USD"
            )
        else:
            stock = await self.discovery.qualify_underlying(symbol)
        spot, spot_source = await self.discovery.resolve_spot_with_source(stock)

        market_day = market_date_for(self.clock())
        missing: list[ExactContractSpec] = []
        active_specs: list[ExactContractSpec] = []
        candidates: list[Option] = []
        for spec in unique_specs:
            expiry = date.fromisoformat(spec.expiry)
            if expiry < market_day:
                missing.append(spec)
                continue
            active_specs.append(spec)
            candidates.append(
                Option(
                    conId=spec.option_conid if spec.option_conid > 0 else 0,
                    symbol=symbol,
                    lastTradeDateOrContractMonth=expiry.strftime("%Y%m%d"),
                    strike=spec.strike,
                    right=spec.right,
                    exchange="SMART",
                    multiplier="100",
                    currency="USD",
                )
            )
        if not candidates:
            raise OptionChainFetchError(f"Every requested {symbol} contract has expired")
        qualified = await self.qualify_option_contracts(candidates)
        found = {_contract_key(c) for c in qualified}
        missing.extend(spec for spec in active_specs if spec.key() not in found)
        tickers, _pages = await self.fetch_pages(qualified)

        sub_notice = self.client.consume_subscription_fallback_notice()
        if sub_notice and sub_notice not in self._notices:
            self._notices.append(sub_notice)
        md_type = self.client.get_active_market_data_type()
        now_utc = self.clock().astimezone(UTC)
        ctx = SnapshotContext(
            snapshot_id=make_snapshot_id(symbol, now_utc),
            snapshot_ts_utc=now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            market_date=market_day.isoformat(),
            market_data_type=md_type,
            underlying=symbol,
            underlying_conid=int(getattr(stock, "conId", 0) or underlying_conid),
            underlying_price=spot,
        )
        rows = tuple(
            TickerParser.parse_ticker(
                tk,
                contract=contract,
                context=ctx,
                dte=(date.fromisoformat(_contract_key(contract)[0]) - market_day).days,
            )
            for contract, tk in zip(qualified, tickers, strict=True)
        )
        logger.info(
            "exact_quotes ticker=%s contracts=%d missing=%d market_data_type=%d spot=%.4f "
            "spot_source=%s snapshot_id=%s",
            symbol,
            len(rows),
            len(missing),
            md_type,
            spot,
            spot_source,
            ctx.snapshot_id,
        )
        return ExactQuoteResult(
            snapshot_id=ctx.snapshot_id,
            snapshot_ts_utc=ctx.snapshot_ts_utc,
            market_date=ctx.market_date,
            market_data_type=md_type,
            underlying_price=spot,
            spot_source=spot_source,
            rows=rows,
            missing=tuple(missing),
            notices=tuple(self._notices),
        )
