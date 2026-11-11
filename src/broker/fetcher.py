"""Paged option chain qualification and market-data collection (`<= 80` contracts/page)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ib_async import Option

from src.broker.client import DELAYED_PRIMARY_TYPE, IBKRBrokerClient, SleepFn, poll_until
from src.broker.contracts import ChainPlan, ContractDiscoveryService
from src.broker.parser import (
    SnapshotContext,
    TickerParser,
    has_live_opra_data,
    has_usable_quote,
)
from src.core.config import Settings
from src.core.exceptions import OptionChainFetchError, PacingViolationError
from src.storage.csv_repository import make_snapshot_id
from src.storage.schema import OptionContractRow

logger = logging.getLogger(__name__)

DELAYED_GENERIC_TICKS = "100,101,106"
LIVE_OPRA_GENERIC_TICKS = "100,101,104,106"
EMPTY_QUOTE_FALLBACK_THRESHOLD = 0.80
PACING_COOLDOWN_SEC = 1.0


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
        """Build the `(expiry, strike, right)` candidate `Option` grid for `plan`."""
        candidates: list[Option] = []
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
        """Qualify candidate `Option` contracts in micro-batches (`<= 80` per call)."""
        if not candidates:
            raise OptionChainFetchError("No candidate option contracts to qualify")
        pages = paginate_contracts(
            candidates, max_per_page=self.max_contracts_per_page, max_pages=None
        )
        qualified: list[Any] = []
        for batch in pages:
            res = await self.client.ib.qualifyContractsAsync(*batch)
            for item in res or []:
                if (
                    item is not None
                    and not isinstance(item, list)
                    and getattr(item, "conId", 0) > 0
                ):
                    qualified.append(item)
        if not qualified:
            raise OptionChainFetchError("IBKR failed to qualify any option contracts in chain")
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
        subscribed: list[Any] = []
        tickers: list[Any] = []
        try:
            for contract in page_contracts:
                ticker = ib.reqMktData(
                    contract,
                    genericTickList=generic_tick_list,
                    snapshot=False,
                    regulatorySnapshot=False,
                )
                subscribed.append(contract)
                tickers.append(ticker)

            await poll_until(
                lambda: all(ready_predicate(t) for t in tickers),
                oc.page_collect_timeout_sec,
                oc.page_poll_interval_sec,
                self.sleep,
            )
            return tickers
        finally:
            for contract in subscribed:
                try:
                    ib.cancelMktData(contract)
                except Exception:  # pragma: no cover - defensive
                    logger.exception("cancelMktData failed for %s", contract)

    async def _fetch_page_delayed(
        self,
        page_contracts: Sequence[Any],
        *,
        allow_fallback: bool = True,
    ) -> list[Any]:
        """Collect one page (`<= 80` contracts) in Type 3/4 Delayed mode.

        - Subscribes with `genericTickList="100,101,106"`, `snapshot=False`,
          `regulatorySnapshot=False`.
        - Polls every `page_poll_interval_sec` (`0.25s`) up to `page_collect_timeout_sec` (`2.25s`).
        - Guarantees `cancelMktData` for all subscribed contracts in `finally:`.
        - If `>80%` of contracts have no usable quote under Type 3 and fallback is available,
          cascades to Type 4 (`Delayed-Frozen`) and re-polls the page once.
        - If a pacing warning (`100`/`162`) occurred, widens cooldown to `1.0s` and retries once.
        """
        tickers = await self._collect_page_once(
            page_contracts,
            generic_tick_list=DELAYED_GENERIC_TICKS,
            ready_predicate=has_usable_quote,
        )

        if self.client.consume_pacing_warning():
            self.active_cooldown_sec = max(self.active_cooldown_sec, PACING_COOLDOWN_SEC)
            await self.sleep(self.active_cooldown_sec)
            tickers = await self._collect_page_once(
                page_contracts,
                generic_tick_list=DELAYED_GENERIC_TICKS,
                ready_predicate=has_usable_quote,
            )

        if (
            allow_fallback
            and self.client.current_type == DELAYED_PRIMARY_TYPE
            and self.client.can_fallback()
            and empty_quote_ratio(tickers) > EMPTY_QUOTE_FALLBACK_THRESHOLD
        ):
            new_type = self.client.switch_to_fallback_data_type()
            notice = (
                f"Over 80% of Type 3 delayed quotes were empty; switched to "
                f"MarketDataType({new_type}) Delayed-Frozen"
            )
            self._notices.append(notice)
            logger.info(notice)
            tickers = await self._collect_page_once(
                page_contracts,
                generic_tick_list=DELAYED_GENERIC_TICKS,
                ready_predicate=has_usable_quote,
            )

        return tickers

    async def _fetch_page_live_opra(
        self,
        page_contracts: Sequence[Any],
        *,
        allow_fallback: bool = True,
    ) -> list[Any]:
        """Scaffolded collector for `MARKET_DATA_MODE=LIVE` (Type 1 Live OPRA -> Type 2 Frozen).

        Subscribes with `genericTickList="100,101,104,106"` and waits for live quotes,
        `modelGreeks`, and generic tick `101` (`callOpenInterest` / `putOpenInterest`).
        If IBKR emits `Error 10089`/`354` (missing OPRA subscription), `IBKRBrokerClient`
        automatically drops back to Type 3 Delayed.
        """
        tickers = await self._collect_page_once(
            page_contracts,
            generic_tick_list=LIVE_OPRA_GENERIC_TICKS,
            ready_predicate=has_live_opra_data,
        )

        if self.client.consume_pacing_warning():
            self.active_cooldown_sec = max(self.active_cooldown_sec, PACING_COOLDOWN_SEC)
            await self.sleep(self.active_cooldown_sec)
            tickers = await self._collect_page_once(
                page_contracts,
                generic_tick_list=LIVE_OPRA_GENERIC_TICKS,
                ready_predicate=has_live_opra_data,
            )

        if (
            allow_fallback
            and self.client.can_fallback()
            and empty_quote_ratio(tickers) > EMPTY_QUOTE_FALLBACK_THRESHOLD
        ):
            new_type = self.client.switch_to_fallback_data_type()
            notice = f"Over 80% of live quotes were empty; switched to MarketDataType({new_type})"
            self._notices.append(notice)
            logger.info(notice)
            tickers = await self._collect_page_once(
                page_contracts,
                generic_tick_list=LIVE_OPRA_GENERIC_TICKS,
                ready_predicate=has_usable_quote,
            )

        return tickers

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
    ) -> FetchResult:
        """Execute the full 5-stage pipeline and return validated `OptionContractRow` rows."""
        t0 = time.perf_counter()
        self._notices = []
        await self.client.ensure_connected()
        self.client.reset_market_data_type()

        plan = await self.discovery.build_plan(ticker, range_pct=range_pct, strike_step=strike_step)
        candidates = self.build_candidate_contracts(plan)
        qualified = await self.qualify_option_contracts(candidates)
        tickers, pages_fetched = await self.fetch_pages(qualified)

        now_utc = self.clock().astimezone(UTC)
        snapshot_id = make_snapshot_id(plan.underlying.symbol, now_utc)
        snapshot_ts_utc = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        market_date_str = plan.market_date.isoformat()

        ctx = SnapshotContext(
            snapshot_id=snapshot_id,
            snapshot_ts_utc=snapshot_ts_utc,
            market_date=market_date_str,
            market_data_type=self.client.current_type,
            underlying=plan.underlying.symbol,
            underlying_conid=plan.underlying.conid,
            underlying_price=plan.underlying.spot,
        )
        dte_map = {m.ib_expiry: m.dte for m in plan.expirations}

        rows: list[OptionContractRow] = []
        for contract, tk in zip(qualified, tickers, strict=True):
            ib_exp = str(getattr(contract, "lastTradeDateOrContractMonth", ""))[:8]
            dte = dte_map.get(ib_exp)
            rows.append(
                TickerParser.parse_ticker(
                    tk,
                    contract=contract,
                    context=ctx,
                    dte=dte,
                )
            )

        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        source_counts = dict(Counter(r.greeks_source for r in rows))
        dtes_matched = [m.dte for m in plan.expirations]
        logger.info(
            "refresh_summary ticker=%s market_data_type=%d spot_price=%.4f "
            "dtes_matched=%s range_pct=%.4f strike_step=%.4f total_contracts=%d "
            "pages_fetched=%d elapsed_ms=%.2f greeks_source_counts=%s snapshot_id=%s",
            plan.underlying.symbol,
            self.client.current_type,
            plan.underlying.spot,
            dtes_matched,
            plan.range_pct,
            plan.strikes.effective_step,
            len(rows),
            pages_fetched,
            elapsed_ms,
            source_counts,
            snapshot_id,
        )

        notices = (*plan.notices, *self._notices)
        return FetchResult(
            snapshot_id=snapshot_id,
            snapshot_ts_utc=snapshot_ts_utc,
            market_date=market_date_str,
            market_data_type=self.client.current_type,
            plan=plan,
            rows=rows,
            pages_fetched=pages_fetched,
            elapsed_ms=elapsed_ms,
            notices=notices,
        )
