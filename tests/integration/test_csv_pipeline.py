from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from pydantic import ValidationError

from src.core.config import StorageSettings
from src.core.exceptions import InvalidTickerError, SnapshotNotFoundError, StorageSchemaError
from src.storage.csv_repository import CSVRepository, make_snapshot_id
from src.storage.quality import evaluate_snapshot
from src.storage.schema import COLUMNS, OPTION_CHAIN_POLARS_SCHEMA, OptionContractRow, SnapshotMeta

SPOT = 142.65
WEEKLY_DTES = (37, 43, 50, 58)
MARKET_DATE = datetime(2026, 11, 11, 15, 50, 4, tzinfo=UTC)


def make_rows(ticker: str = "NVDA", ts: datetime = MARKET_DATE) -> list[dict[str, Any]]:
    sid = make_snapshot_id(ticker, ts)
    rows: list[dict[str, Any]] = []
    conid = 1000
    for dte in WEEKLY_DTES:
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
def test_schema_has_27_columns_matching_model() -> None:
    assert len(COLUMNS) == 27
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
        ("dte", -1),
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
    meta = repo.save_snapshot(
        "NVDA",
        make_rows(),
        range_pct=0.2,
        strike_step=5.0,
        requested_strike_step=5.0,
    )
    assert meta.snapshot_id == "NVDA_20261111_155004"
    assert meta.contract_count == 32
    assert meta.requested_strike_step == 5.0
    assert meta.file_path == "data/snapshots/NVDA/NVDA_20261111_155004.csv"
    assert len(meta.expirations) == 4

    latest = repo.load_latest("NVDA")
    assert latest.schema == OPTION_CHAIN_POLARS_SCHEMA
    assert latest.height == 32
    assert sorted(latest["dte"].unique().to_list()) == list(WEEKLY_DTES)
    assert latest["open_interest"].null_count() == 32
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
    assert repo.load_latest("NVDA").height == 32
    repo.latest_path("NVDA").unlink()
    assert repo.load_latest("NVDA").height == 32


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
        (pl.col("dte") == 50) & (pl.col("strike") == 145.0) & (pl.col("right") == "P")
    ).row(0, named=True)
    assert row["delta"] == -0.5
    assert math.isclose(row["moneyness_pct"], (145.0 - SPOT) / SPOT * 100.0)
    assert row["greeks_source"] == "IBKR_MODEL"


def test_ensure_layout_creates_dirs_idempotently_without_touching_files(tmp_path: Path) -> None:
    repo = CSVRepository(StorageSettings(), root=tmp_path)
    assert not repo.snapshots_dir.exists()

    repo.ensure_layout()
    for directory in (repo.snapshots_dir, repo.latest_dir, repo.index_path.parent):
        assert directory.is_dir()

    sentinel = repo.latest_dir / "keep.txt"
    sentinel.write_text("x", encoding="utf-8")
    repo.ensure_layout()
    assert sentinel.read_text(encoding="utf-8") == "x"


def test_snapshot_quality_metadata_roundtrip_and_legacy_index_compat(
    repo: CSVRepository,
) -> None:
    rows = make_rows()
    saved = repo.save_snapshot(
        "NVDA",
        rows,
        range_pct=0.20,
        strike_step=5.0,
        spot_source="LAST",
    )
    assert saved.spot_source == "LAST"
    assert saved.quoted_contract_count == 32
    assert saved.quote_coverage == pytest.approx(1.0)
    assert saved.prior_close_only_count == 0

    # Rows with prior `close` only (no live/delayed bid/ask) report zero quote coverage
    ts2 = MARKET_DATE + timedelta(minutes=1)
    stale_rows = [
        {
            **r,
            "snapshot_id": make_snapshot_id("NVDA", ts2),
            "bid": None,
            "ask": None,
            "last": None,
            "close": 4.0,
        }
        for r in make_rows()
    ]
    stale = repo.save_snapshot("NVDA", stale_rows, spot_source="CLOSE")
    assert stale.spot_source == "CLOSE"
    assert stale.quoted_contract_count == 0
    assert stale.quote_coverage == pytest.approx(0.0)
    assert stale.prior_close_only_count == 32

    # Legacy index entries without quality fields must still parse cleanly
    raw = json.loads(repo.index_path.read_text(encoding="utf-8"))
    nvda_entries = raw["tickers"]["NVDA"]["snapshots"]
    target_entry = next(e for e in nvda_entries if e["snapshot_id"] == saved.snapshot_id)
    for key in ("spot_source", "quoted_contract_count", "quote_coverage", "prior_close_only_count"):
        target_entry.pop(key, None)
    repo.index_path.write_text(json.dumps(raw), encoding="utf-8")

    entries = repo.list_snapshots("NVDA")
    legacy = next(e for e in entries if e.snapshot_id == saved.snapshot_id)
    assert isinstance(legacy, SnapshotMeta)
    assert legacy.spot_source is None
    assert legacy.quote_coverage is None

    rebuilt = repo.rebuild_index()
    rebuilt_snaps = rebuilt["tickers"]["NVDA"]["snapshots"]
    assert len(rebuilt_snaps) == 2
    assert all(item["quoted_contract_count"] is not None for item in rebuilt_snaps)


# ------------------------------------------------------------------ partial snapshots
T1 = MARKET_DATE + timedelta(minutes=30)
T2 = MARKET_DATE + timedelta(hours=1)


def _index_snapshot(repo: CSVRepository, ticker: str, snapshot_id: str) -> dict[str, Any]:
    raw = json.loads(repo.index_path.read_text(encoding="utf-8"))
    entry: dict[str, Any] = next(
        s for s in raw["tickers"][ticker]["snapshots"] if s["snapshot_id"] == snapshot_id
    )
    return entry


def _index_latest_id(repo: CSVRepository, ticker: str) -> str:
    raw = json.loads(repo.index_path.read_text(encoding="utf-8"))
    return str(raw["tickers"][ticker]["latest_snapshot_id"])


def test_partial_snapshot_archived_flagged_and_keeps_existing_latest(repo: CSVRepository) -> None:
    complete = repo.save_snapshot_result("NVDA", make_rows(), spot_source="LAST")
    assert complete.latest_updated is True
    assert complete.meta.partial is False
    assert complete.meta.partial_reason is None
    latest_before = repo.latest_path("NVDA").read_bytes()

    rows = make_rows(ts=T1)[:8]  # only the finished pages arrived
    partial = repo.save_snapshot_result(
        "NVDA", rows, spot_source="LAST", partial_reason="cancelled"
    )
    assert partial.latest_updated is False
    assert partial.meta.partial is True
    assert partial.meta.partial_reason == "cancelled"
    assert partial.meta.is_partial
    assert partial.meta.contract_count == 8

    # Archive written as a new immutable Mode 1 snapshot (27 columns, nothing extra).
    archive = repo.snapshots_dir / "NVDA" / f"{partial.meta.snapshot_id}.csv"
    assert archive.is_file()
    assert tuple(repo.load_snapshot("NVDA", partial.meta.snapshot_id).columns) == COLUMNS
    with pytest.raises(StorageSchemaError, match="immutable"):
        repo.save_snapshot("NVDA", rows, partial_reason="cancelled")

    # Index carries the flag; latest cache + index pointer keep the complete snapshot.
    entry = _index_snapshot(repo, "NVDA", partial.meta.snapshot_id)
    assert entry["partial"] is True
    assert entry["partial_reason"] == "cancelled"
    assert repo.latest_path("NVDA").read_bytes() == latest_before
    assert repo.load_latest("NVDA")["snapshot_id"][0] == complete.meta.snapshot_id
    assert repo.latest_cache_snapshot_id("NVDA") == complete.meta.snapshot_id
    assert _index_latest_id(repo, "NVDA") == complete.meta.snapshot_id

    # Listing (GET /api/snapshots/{ticker}) is newest-first and carries the flag through.
    listed = repo.list_snapshots("NVDA")
    assert [m.snapshot_id for m in listed] == [
        partial.meta.snapshot_id,
        complete.meta.snapshot_id,
    ]
    assert listed[0].partial is True
    assert listed[0].partial_reason == "cancelled"
    assert listed[1].partial is False

    # A later complete refresh replaces the latest cache again.
    newer = repo.save_snapshot_result("NVDA", make_rows(ts=T2))
    assert newer.latest_updated is True
    assert repo.load_latest("NVDA")["snapshot_id"][0] == newer.meta.snapshot_id
    assert _index_latest_id(repo, "NVDA") == newer.meta.snapshot_id


def test_partial_snapshot_becomes_latest_only_when_none_exists(repo: CSVRepository) -> None:
    first = repo.save_snapshot_result("CRDO", make_rows("CRDO"), partial_reason="timeout")
    assert first.latest_updated is True
    assert repo.latest_path("CRDO").is_file()
    assert repo.load_latest("CRDO")["snapshot_id"][0] == first.meta.snapshot_id
    assert _index_latest_id(repo, "CRDO") == first.meta.snapshot_id

    second = repo.save_snapshot_result(
        "CRDO", make_rows("CRDO", ts=T1), partial_reason="connection_lost"
    )
    assert second.latest_updated is False
    assert repo.load_latest("CRDO")["snapshot_id"][0] == first.meta.snapshot_id
    assert _index_latest_id(repo, "CRDO") == first.meta.snapshot_id
    second_entry = _index_snapshot(repo, "CRDO", second.meta.snapshot_id)
    assert second_entry["partial_reason"] == "connection_lost"

    # The plain `save_snapshot` wrapper accepts the same keyword and returns the meta.
    meta = repo.save_snapshot("CRDO", make_rows("CRDO", ts=T2), partial_reason="cancelled")
    assert isinstance(meta, SnapshotMeta)
    assert meta.partial_reason == "cancelled"
    assert repo.load_latest("CRDO")["snapshot_id"][0] == first.meta.snapshot_id


def test_legacy_index_without_partial_fields_reads_as_complete(repo: CSVRepository) -> None:
    saved = repo.save_snapshot("NVDA", make_rows(), spot_source="LAST")
    raw = json.loads(repo.index_path.read_text(encoding="utf-8"))
    for snap in raw["tickers"]["NVDA"]["snapshots"]:
        snap.pop("partial")
        snap.pop("partial_reason")
    repo.index_path.write_text(json.dumps(raw), encoding="utf-8")

    (legacy,) = repo.list_snapshots("NVDA")
    assert legacy.snapshot_id == saved.snapshot_id
    assert legacy.partial is None
    assert legacy.partial_reason is None
    assert legacy.is_partial is False
    assert evaluate_snapshot(legacy).usable is True
    assert repo.latest_snapshots(["NVDA"], include_partial=False)["NVDA"] == legacy
    # The legacy index was read as-is (valid), not rebuilt.
    reread = json.loads(repo.index_path.read_text(encoding="utf-8"))
    assert "partial" not in reread["tickers"]["NVDA"]["snapshots"][0]


def test_latest_snapshots_freshness_ignores_partial(repo: CSVRepository) -> None:
    complete = repo.save_snapshot("NVDA", make_rows())
    partial = repo.save_snapshot("NVDA", make_rows(ts=T1), partial_reason="timeout")
    only_partial = repo.save_snapshot("AVGO", make_rows("AVGO"), partial_reason="cancelled")

    newest = repo.latest_snapshots(["NVDA", "AVGO", "PLTR"])
    assert newest["NVDA"].snapshot_id == partial.snapshot_id
    assert newest["AVGO"].snapshot_id == only_partial.snapshot_id
    assert "PLTR" not in newest

    fresh = repo.latest_snapshots(["NVDA", "AVGO", "PLTR"], include_partial=False)
    assert fresh["NVDA"].snapshot_id == complete.snapshot_id
    assert "AVGO" not in fresh  # a partial snapshot never counts as fresh
    assert "PLTR" not in fresh


def test_corrupt_latest_fallback_prefers_complete_snapshot(repo: CSVRepository) -> None:
    complete = repo.save_snapshot("NVDA", make_rows())
    repo.save_snapshot("NVDA", make_rows(ts=T1), partial_reason="connection_lost")
    repo.latest_path("NVDA").write_text("garbage", encoding="utf-8")
    assert repo.latest_cache_snapshot_id("NVDA") is None
    assert repo.load_latest("NVDA")["snapshot_id"][0] == complete.snapshot_id

    only = repo.save_snapshot("AVGO", make_rows("AVGO"), partial_reason="timeout")
    repo.latest_path("AVGO").unlink()
    assert repo.load_latest("AVGO")["snapshot_id"][0] == only.snapshot_id  # last resort


def test_partial_flag_survives_save_into_corrupt_index(repo: CSVRepository) -> None:
    complete = repo.save_snapshot("NVDA", make_rows())
    repo.index_path.write_text("{ not json", encoding="utf-8")
    partial = repo.save_snapshot("NVDA", make_rows(ts=T1), partial_reason="cancelled")

    entry = _index_snapshot(repo, "NVDA", partial.snapshot_id)
    assert entry["partial"] is True
    assert entry["partial_reason"] == "cancelled"
    assert _index_latest_id(repo, "NVDA") == complete.snapshot_id  # read from the cache file
    assert repo.list_snapshots("NVDA")[1].partial is None  # rescanned entry: flag unknown

    # A plain rebuild keeps the pointer on the snapshot actually held by the latest cache.
    repo.rebuild_index()
    assert _index_latest_id(repo, "NVDA") == complete.snapshot_id


def test_partial_metadata_validation(repo: CSVRepository) -> None:
    base: dict[str, Any] = {
        "snapshot_id": "NVDA_20261111_155004",
        "file_path": "data/snapshots/NVDA/NVDA_20261111_155004.csv",
        "snapshot_ts_utc": "2026-11-11T15:50:04Z",
        "market_date": "2026-11-11",
        "market_data_type": 3,
        "underlying_price": SPOT,
        "contract_count": 1,
        "expirations": ["2026-12-18"],
    }
    assert SnapshotMeta.model_validate({**base, "partial": True}).partial_reason is None
    with pytest.raises(ValidationError):
        SnapshotMeta.model_validate({**base, "partial_reason": "cancelled"})
    with pytest.raises(ValidationError):
        SnapshotMeta.model_validate({**base, "partial": False, "partial_reason": "timeout"})
    with pytest.raises(ValidationError):
        SnapshotMeta.model_validate({**base, "partial": True, "partial_reason": "boom"})

    with pytest.raises(StorageSchemaError, match="metadata validation"):
        repo.save_snapshot("NVDA", make_rows(), partial_reason="boom")  # type: ignore[arg-type]
    assert not repo.snapshot_path("NVDA", make_snapshot_id("NVDA", MARKET_DATE)).exists()
