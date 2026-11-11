from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from src.core.config import StorageSettings
from src.core.exceptions import InvalidTickerError, SnapshotNotFoundError, StorageSchemaError
from src.storage.csv_repository import CSVRepository, make_snapshot_id
from src.storage.schema import COLUMNS, OPTION_CHAIN_POLARS_SCHEMA, OptionContractRow

SPOT = 142.65
BUCKETS = (30, 35, 40, 45, 60)
MARKET_DATE = datetime(2026, 11, 11, 15, 50, 4, tzinfo=UTC)


def make_rows(ticker: str = "NVDA", ts: datetime = MARKET_DATE) -> list[dict[str, Any]]:
    sid = make_snapshot_id(ticker, ts)
    rows: list[dict[str, Any]] = []
    conid = 1000
    for dte in BUCKETS:
        expiry = (ts.date() + timedelta(days=dte)).isoformat()
        for strike in (130.0, 140.0, 145.0, 155.0):
            for right in ("C", "P"):
                conid += 1
                rows.append(
                    {
                        "snapshot_id": sid,
                        "snapshot_ts_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "market_date": "2026-11-11",
                        "market_data_type": 3,
                        "underlying": ticker,
                        "underlying_conid": 4815,
                        "underlying_price": SPOT,
                        "option_conid": conid,
                        "expiry": expiry,
                        "dte": dte,
                        "target_dte_bucket": dte,
                        "strike": strike,
                        "moneyness_pct": (strike - SPOT) / SPOT * 100.0,
                        "right": right,
                        "bid": 5.0,
                        "ask": 5.4,
                        "last": 5.1,
                        "close": 5.0,
                        "volume": 120,
                        "open_interest": None,
                        "iv": 0.45,
                        "delta": 0.5 if right == "C" else -0.5,
                        "gamma": 0.02,
                        "theta": -0.1,
                        "vega": 0.2,
                        "greeks_source": "IBKR_MODEL",
                    }
                )
    return rows


@pytest.fixture
def repo(tmp_path: Path) -> CSVRepository:
    return CSVRepository(StorageSettings(), root=tmp_path)


# ------------------------------------------------------------------ schema
def test_schema_has_28_columns_matching_model() -> None:
    assert len(COLUMNS) == 28
    assert tuple(OPTION_CHAIN_POLARS_SCHEMA.names()) == COLUMNS


def test_sentinel_sanitization_and_derivations() -> None:
    row = make_rows()[0]
    row.update(
        bid=-1.0,
        ask=float("nan"),
        last=-1.0,
        close=0.0,
        volume=2147483647,
        open_interest=-1,
        iv=-1.0,
        delta=float("nan"),
        gamma=-1.0,
        greeks_source="NONE",
    )
    m = OptionContractRow.model_validate(row)
    assert (m.bid, m.ask, m.mid, m.last, m.close, m.mark_price) == (None,) * 6
    assert (m.volume, m.open_interest, m.iv, m.delta, m.gamma) == (None,) * 5


def test_mid_and_mark_derivation() -> None:
    m = OptionContractRow.model_validate(make_rows()[0])
    assert m.mid == pytest.approx(5.2)
    assert m.mark_price == pytest.approx(5.2)
    row = make_rows()[0]
    row.update(bid=None, ask=None)
    assert OptionContractRow.model_validate(row).mark_price == 5.1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("greeks_source", "BOGUS"),
        ("right", "X"),
        ("target_dte_bucket", 50),
        ("expiry", "2026-13-40"),
        ("underlying_price", 0.0),
        ("strike", -5.0),
        ("underlying", "../etc"),
        ("snapshot_ts_utc", "2026-11-11T15:50:04"),
        ("market_data_type", 9),
    ],
)
def test_invalid_row_rejected(field: str, value: Any) -> None:
    row = make_rows()[0]
    row[field] = value
    with pytest.raises(ValueError):
        OptionContractRow.model_validate(row)


# ------------------------------------------------------------------ pipeline
def test_save_and_offline_read(repo: CSVRepository) -> None:
    meta = repo.save_snapshot("NVDA", make_rows(), range_pct=0.2, strike_step=5.0)
    assert meta.snapshot_id == "NVDA_20261111_155004"
    assert meta.contract_count == 40
    assert meta.file_path == "data/snapshots/NVDA/NVDA_20261111_155004.csv"
    assert len(meta.expirations) == 5

    latest = repo.load_latest("NVDA")
    assert latest.schema == OPTION_CHAIN_POLARS_SCHEMA
    assert latest.height == 40
    assert sorted(latest["target_dte_bucket"].unique().to_list()) == list(BUCKETS)
    assert latest["open_interest"].null_count() == 40
    assert latest["mid"].to_list()[0] == pytest.approx(5.2)
    assert repo.load_snapshot("NVDA", meta.snapshot_id).equals(latest)
    assert not list(repo.snapshots_dir.rglob(".*.tmp"))
    assert not list(repo.latest_dir.glob(".*.tmp"))


def test_snapshots_are_immutable(repo: CSVRepository) -> None:
    repo.save_snapshot("NVDA", make_rows())
    with pytest.raises(StorageSchemaError, match="immutable"):
        repo.save_snapshot("NVDA", make_rows())


def test_multiple_snapshots_index_and_latest(repo: CSVRepository) -> None:
    repo.save_snapshot("NVDA", make_rows(ts=MARKET_DATE), range_pct=0.2, strike_step=5.0)
    later = MARKET_DATE + timedelta(hours=1)
    repo.save_snapshot("NVDA", make_rows(ts=later))
    repo.save_snapshot("AVGO", make_rows("AVGO"))

    metas = repo.list_snapshots("NVDA")
    assert [m.snapshot_id for m in metas] == ["NVDA_20261111_165004", "NVDA_20261111_155004"]
    assert metas[1].range_pct == 0.2
    assert repo.load_latest("NVDA")["snapshot_id"][0] == "NVDA_20261111_165004"
    assert repo.list_snapshots("AVGO")[0].snapshot_id == "AVGO_20261111_155004"
    assert repo.list_snapshots("PLTR") == []

    index = json.loads(repo.index_path.read_text(encoding="utf-8"))
    assert index["tickers"]["NVDA"]["latest_snapshot_id"] == "NVDA_20261111_165004"
    assert index["tickers"]["NVDA"]["latest_file"] == "data/latest/NVDA_latest.csv"


def test_rebuild_index_self_heals(repo: CSVRepository) -> None:
    repo.save_snapshot("NVDA", make_rows())
    repo.save_snapshot("NVDA", make_rows(ts=MARKET_DATE + timedelta(hours=1)))
    repo.index_path.write_text("{ not json", encoding="utf-8")
    assert len(repo.list_snapshots("NVDA")) == 2  # auto-heal on read
    repo.index_path.unlink()
    assert len(repo.list_snapshots("NVDA")) == 2
    rebuilt = repo.rebuild_index()
    assert list(rebuilt["tickers"]) == ["NVDA"]


def test_rebuild_skips_corrupt_and_stray_files(repo: CSVRepository) -> None:
    good = repo.save_snapshot("NVDA", make_rows())
    bad_dir = repo.snapshots_dir / "NVDA"
    (bad_dir / "NVDA_20261111_999999.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (bad_dir / ".NVDA_20261111_000001.csv.tmp").write_text("junk", encoding="utf-8")
    (repo.snapshots_dir / "bad..dir").mkdir()
    rebuilt = repo.rebuild_index()
    assert [s["snapshot_id"] for s in rebuilt["tickers"]["NVDA"]["snapshots"]] == [good.snapshot_id]


def test_corrupt_files_rejected(repo: CSVRepository) -> None:
    meta = repo.save_snapshot("NVDA", make_rows())
    path = repo.snapshots_dir / "NVDA" / f"{meta.snapshot_id}.csv"
    good_text = path.read_text(encoding="utf-8")

    path.write_text(good_text.replace("snapshot_id", "snapshot_idx", 1), encoding="utf-8")
    with pytest.raises(StorageSchemaError):
        repo.load_snapshot("NVDA", meta.snapshot_id)

    lines = good_text.splitlines()
    lines[1] = lines[1].replace("IBKR_MODEL", "IBKR_MODEL,extra")
    path.write_text("\n".join(lines), encoding="utf-8")
    with pytest.raises(StorageSchemaError):
        repo.load_snapshot("NVDA", meta.snapshot_id)

    path.write_bytes(b"\xff\xfe\x00garbage")
    with pytest.raises(StorageSchemaError):
        repo.load_snapshot("NVDA", meta.snapshot_id)

    path.write_text(lines[0] + "\n", encoding="utf-8")  # header only
    with pytest.raises(StorageSchemaError):
        repo.load_snapshot("NVDA", meta.snapshot_id)


def test_corrupt_latest_falls_back_to_archive(repo: CSVRepository) -> None:
    repo.save_snapshot("NVDA", make_rows())
    repo.latest_path("NVDA").write_text("garbage", encoding="utf-8")
    assert repo.load_latest("NVDA").height == 40
    repo.latest_path("NVDA").unlink()
    assert repo.load_latest("NVDA").height == 40


def test_missing_data_raises(repo: CSVRepository) -> None:
    with pytest.raises(SnapshotNotFoundError):
        repo.load_latest("NVDA")
    with pytest.raises(SnapshotNotFoundError):
        repo.load_snapshot("NVDA", "NVDA_20200101_000000")


def test_input_validation(repo: CSVRepository) -> None:
    with pytest.raises(InvalidTickerError):
        repo.load_latest("../etc")
    with pytest.raises(InvalidTickerError):
        repo.save_snapshot("NV/DA", make_rows())
    with pytest.raises(StorageSchemaError):
        repo.load_snapshot("NVDA", "../../secret")
    with pytest.raises(StorageSchemaError):
        repo.load_snapshot("NVDA", "AVGO_20261111_155004")
    with pytest.raises(StorageSchemaError):
        repo.save_snapshot("NVDA", [])
    with pytest.raises(StorageSchemaError):
        repo.save_snapshot("AVGO", make_rows("NVDA"))
    mixed = make_rows()
    mixed[1]["snapshot_id"] = "NVDA_20261111_000000"
    with pytest.raises(StorageSchemaError):
        repo.save_snapshot("NVDA", mixed)
    bad = make_rows()
    bad[0]["right"] = "Z"
    with pytest.raises(StorageSchemaError):
        repo.save_snapshot("NVDA", bad)
    assert not repo.snapshots_dir.exists()  # nothing written on failure


def test_values_survive_roundtrip(repo: CSVRepository) -> None:
    repo.save_snapshot("NVDA", make_rows())
    df = repo.load_latest("NVDA")
    row = df.filter(
        (pl.col("target_dte_bucket") == 45) & (pl.col("strike") == 145.0) & (pl.col("right") == "P")
    ).row(0, named=True)
    assert row["delta"] == -0.5
    assert math.isclose(row["moneyness_pct"], (145.0 - SPOT) / SPOT * 100.0)
    assert row["greeks_source"] == "IBKR_MODEL"
