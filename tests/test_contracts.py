from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from gridlens.contracts.gate import apply_gate
from gridlens.contracts.quarantine import FileQuarantine
from gridlens.contracts.records import GenerationRecord
from gridlens.contracts.reference import BiddingZone, ProductionType
from gridlens.ingest.entsoe_parse import parse_generation

FIXTURES = Path(__file__).parent / "fixtures" / "entsoe"
KNOWN_AT = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


def document(name: str):
    return parse_generation((FIXTURES / name).read_bytes())


def valid_payload(**overrides):
    payload = {
        "source": "entsoe",
        "document_mrid": "cb22637fcf6e40a59e840cc2ee7c5293",
        "zone": BiddingZone.FR,
        "production_type": ProductionType.NUCLEAR,
        "direction": "generation",
        "unit": "MAW",
        "resolution_minutes": 15,
        "valid_time": datetime(2026, 8, 4, 6, 30, tzinfo=UTC),
        "known_at": KNOWN_AT,
        "quantity_mw": Decimal("39733.29"),
    }
    return payload | overrides


def test_a_good_record_is_accepted():
    record = GenerationRecord(**valid_payload())
    assert record.zone is BiddingZone.FR
    assert record.quantity_mw == Decimal("39733.29")


def test_an_unknown_zone_is_refused():
    # the reason this enum exists. entsoe answers a mistyped zone with the same
    # "no matching data" it uses for an empty window, so nothing downstream can
    # tell them apart and the check has to happen here.
    with pytest.raises(ValidationError, match="zone"):
        GenerationRecord(**valid_payload(zone="10YNOTAREALZONE1"))


def test_an_italian_control_area_is_refused():
    # a real EIC code that is not a bidding zone
    with pytest.raises(ValidationError, match="zone"):
        GenerationRecord(**valid_payload(zone="10YIT-GRTN-----B"))


def test_naive_timestamps_are_refused():
    with pytest.raises(ValidationError, match="naive"):
        GenerationRecord(**valid_payload(valid_time=datetime(2026, 8, 4, 6, 30)))


def test_local_timestamps_are_refused():
    from zoneinfo import ZoneInfo

    paris = datetime(2026, 8, 4, 8, 30, tzinfo=ZoneInfo("Europe/Paris"))
    with pytest.raises(ValidationError, match="expected utc"):
        GenerationRecord(**valid_payload(valid_time=paris))


def test_a_value_off_the_resolution_boundary_is_refused():
    with pytest.raises(ValidationError, match="15 minute boundary"):
        GenerationRecord(
            **valid_payload(valid_time=datetime(2026, 8, 4, 6, 37, tzinfo=UTC))
        )


def test_learning_a_realised_value_before_it_happened_is_refused():
    with pytest.raises(ValidationError, match="before the period"):
        GenerationRecord(
            **valid_payload(
                valid_time=datetime(2027, 1, 1, tzinfo=UTC), known_at=KNOWN_AT
            )
        )


def test_negative_generation_is_refused():
    with pytest.raises(ValidationError, match="negative"):
        GenerationRecord(**valid_payload(quantity_mw=Decimal("-1")))


def test_a_kilowatt_figure_mislabelled_as_megawatts_is_refused():
    with pytest.raises(ValidationError, match="beyond anything"):
        GenerationRecord(**valid_payload(quantity_mw=Decimal("39733290")))


def test_unexpected_fields_are_refused():
    with pytest.raises(ValidationError, match="Extra inputs"):
        GenerationRecord(**valid_payload(vendor_note="added upstream"))


def test_the_whole_august_document_passes_the_gate():
    result = apply_gate(document("a75_fr_20260804.xml"), KNOWN_AT)

    assert result.accepted == 1388
    assert result.violations == ()
    assert len(result.gaps) == 15 * 96 - 1388


def test_gaps_are_reported_with_real_timestamps_not_dropped():
    result = apply_gate(document("a75_fr_20251026_dst.xml"), KNOWN_AT)
    biomass = [
        g
        for g in result.gaps
        if g.production_type == "B01" and g.direction == "generation"
    ]

    assert [g.position for g in biomass] == [25, 29, 34, 39, 43, 63, 82, 97]
    assert biomass[0].valid_time == datetime(2025, 10, 26, 4, 0, tzinfo=UTC)
    assert result.violations == ()


def test_the_dst_document_accepts_every_point_it_published():
    result = apply_gate(document("a75_fr_20251026_dst.xml"), KNOWN_AT)
    assert result.accepted + len(result.gaps) == 15 * 100


def test_known_at_is_ours_and_not_the_documents():
    # the october 2025 document was rendered today, so createdDateTime is a
    # year after the data it describes. known_at has to come from the caller.
    doc = document("a75_fr_20251026_dst.xml")
    assert doc.created_at.year == 2026

    result = apply_gate(doc, KNOWN_AT)
    assert {r.known_at for r in result.records} == {KNOWN_AT}


def test_quarantine_writes_one_line_per_violation(tmp_path: Path):
    doc = document("a75_fr_20260804.xml")
    broken = doc.series[0].__class__(
        **{**doc.series[0].__dict__, "unit": "KWT"},
    )
    result = apply_gate(
        doc.__class__(**{**doc.__dict__, "series": (broken,)}), KNOWN_AT
    )
    assert len(result.violations) == 93

    sink = FileQuarantine(tmp_path)
    written = sink.write(result.violations, KNOWN_AT)

    assert written == 93
    files = list(tmp_path.rglob("*.jsonl"))
    assert len(files) == 1
    assert files[0].parent.name == "seen_date=2026-08-11"

    rows = [
        json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == 93
    assert rows[0]["reason"] == "contract"
    assert "unit" in rows[0]["detail"]
    # the original value survives, so the row can be replayed
    assert rows[0]["payload"]["unit"] == "KWT"
    assert rows[0]["payload"]["quantity_mw"]


def test_quarantine_writes_nothing_when_there_is_nothing_to_write(tmp_path: Path):
    assert FileQuarantine(tmp_path).write([], KNOWN_AT) == 0
    assert list(tmp_path.rglob("*")) == []


def test_storage_is_counted_once_in_each_direction():
    result = apply_gate(document("a75_fr_20260804.xml"), KNOWN_AT)
    pumped = {
        r.direction
        for r in result.records
        if r.production_type is ProductionType.HYDRO_PUMPED_STORAGE
    }
    assert pumped == {"generation", "consumption"}


def test_records_are_frozen():
    record = GenerationRecord(**valid_payload())
    with pytest.raises(ValidationError):
        record.quantity_mw = Decimal("1")  # type: ignore[misc]


def test_resolution_must_be_whole_minutes():
    doc = document("a75_fr_20260804.xml")
    odd = doc.series[0].__class__(
        **{**doc.series[0].__dict__, "resolution": timedelta(seconds=90)},
    )
    result = apply_gate(doc.__class__(**{**doc.__dict__, "series": (odd,)}), KNOWN_AT)

    assert result.accepted == 0
    assert result.violations[0].reason == "sub_minute_resolution"
