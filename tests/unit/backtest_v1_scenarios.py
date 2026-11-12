"""Fixed engine scenarios whose engine-version-1 output is frozen in
`tests/fixtures/backtest_v1_golden.json` (regression: without tracked data the engine must give
the same output apart from the version and the new, empty-by-default fields).

The golden file was generated with engine version 1 by running this module as a script:
`uv run python -m tests.unit.backtest_v1_scenarios` (before the tracked-quote changes).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from src.analytics.backtest import (
    BacktestStep,
    backtest_bull_put_from_repo,
    run_bull_put_backtest,
)
from src.core.config import StorageSettings
from src.storage.csv_repository import CSVRepository
from src.storage.schema import SnapshotMeta
from tests.unit.test_backtest import LEGS, Quote, _auto_chain, _chain, _entry, _step, _ts

GOLDEN_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "backtest_v1_golden.json"

WIN: dict[tuple[float, str], Quote] = {
    (100.0, "P"): (0.95, 1.05, 1.0, 1.0),
    (95.0, "P"): (0.35, 0.45, 0.4, 0.4),
}
LOSE: dict[tuple[float, str], Quote] = {
    (100.0, "P"): (3.90, 4.10, 4.0, 4.0),
    (95.0, "P"): (1.40, 1.60, 1.5, 1.5),
}


def _settle(spot: float, when: str = "2026-12-18T21:30:00") -> BacktestStep:
    return _step(_chain(_ts(when), spot, expiries=("2027-01-15",)))


def _run(steps: list[BacktestStep], **kw: Any) -> dict[str, Any]:
    kw.setdefault("mode", "LEGS")
    if kw["mode"] == "LEGS":
        kw.setdefault("legs", LEGS)
    out: dict[str, Any] = run_bull_put_backtest(steps, ticker="NVDA", **kw).model_dump(mode="json")
    return out


def _repo_run() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        repo = CSVRepository(StorageSettings(), root=Path(tmp))
        entry = _entry()
        later = _chain(_ts("2026-11-20T18:00:00"), 106.0, WIN)
        settle = _settle(104.0)
        for i, step_df in enumerate((entry.df, later, settle.df)):
            repo.save_snapshot(
                "NVDA",
                step_df.to_dicts(),
                range_pct=0.10,
                strike_step=5.0,
                spot_source="LAST" if i != 1 else "MID",
                collection_mode="SCHEDULED",
            )
        out: dict[str, Any] = backtest_bull_put_from_repo(
            repo, "NVDA", entry.snapshot_id, mode="LEGS", legs=LEGS
        ).model_dump(mode="json")
        return out


def scenarios() -> dict[str, dict[str, Any]]:
    close_settle = _settle(104.0)
    close_meta = SnapshotMeta(
        snapshot_id=close_settle.snapshot_id,
        file_path="x.csv",
        snapshot_ts_utc="2026-12-18T21:30:00Z",
        market_date="2026-12-18",
        market_data_type=3,
        underlying_price=104.0,
        contract_count=10,
        expirations=["2027-01-15"],
        spot_source="CLOSE",
    )
    return {
        "entry_only": _run([_entry()]),
        "winning": _run([_entry(), _step(_chain(_ts("2026-11-20T18:00:00"), 106.0, WIN))]),
        "losing": _run([_entry(), _step(_chain(_ts("2026-11-20T18:00:00"), 97.0, LOSE))]),
        "qty3": _run([_entry(), _step(_chain(_ts("2026-11-20T18:00:00"), 106.0, WIN))], qty=3),
        "settle_worthless": _run([_entry(), _settle(104.0)]),
        "settle_itm": _run([_entry(), _settle(97.0)]),
        "settle_unresolved": _run(
            [
                _entry(),
                _settle(104.0, when="2026-12-18T21:05:00"),
                _settle(104.0, when="2026-12-21T15:00:00"),
            ]
        ),
        "settle_close_spot": _run(
            [
                _entry(),
                BacktestStep(
                    close_settle.snapshot_id, close_settle.ts_utc, close_settle.df, close_meta
                ),
            ]
        ),
        "outside_dte_window": _run(
            [
                _entry(),
                _step(_chain(_ts("2026-11-25T16:00:00"), 101.0, expiries=("2027-01-15",))),
            ]
        ),
        "not_listed": _run(
            [
                _entry(),
                _step(
                    _chain(
                        _ts("2026-11-20T18:00:00"),
                        101.0,
                        {(100.0, "P"): (1.9, 2.1, 2.0, 2.0)},
                        strikes=(90.0, 100.0, 110.0),
                    )
                ),
            ]
        ),
        "fallback_and_quality": _run(
            [
                _entry(),
                _step(
                    _chain(
                        _ts("2026-11-20T23:30:00"),
                        104.0,
                        {
                            (100.0, "P"): (1.40, 1.60, 1.5, 1.5),
                            (95.0, "P"): (None, None, 0.7, 0.65),
                        },
                        md_type=4,
                        unquoted_others=True,
                    )
                ),
            ]
        ),
        "auto_build": _run(
            [
                _step(_auto_chain("2026-11-11T15:30:00", 100.0)),
                _step(_auto_chain("2026-11-11T18:30:00", 103.0)),
            ],
            mode="AUTO_BUILD",
        ),
        "from_repo": _repo_run(),
    }


if __name__ == "__main__":
    GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN_PATH.write_text(
        json.dumps(scenarios(), indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"wrote {GOLDEN_PATH}")
