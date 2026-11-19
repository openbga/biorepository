"""CSV import (validate, then commit all or nothing) and the audited, safe exports."""

import csv
import io
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from core.models import AuditEvent
from specimens import exports, importing, services
from specimens.models import ImportBatch, ImportCommit, OutputRecord, Specimen
from storage.models import Occupancy

from .conftest import catalogue, codes, make_user, register, stored

pytestmark = pytest.mark.django_db

HEADER = ",".join(importing.COLUMNS)
GOOD = "R1,PLASMA,CRYOVIAL-2ML,1.5,mL,2026-01-01T09:00:00Z,,,P-000123,DEMO,,,0"


def csv_bytes(*rows, header=HEADER):
    return ("\n".join([header, *rows]) + "\n").encode()


def validate(actor, *rows, **kwargs):
    return importing.validate_import(actor=actor, csv_bytes=csv_bytes(*rows, **kwargs))


def commit(actor, batch, sha=None, reason="Shipment 7"):
    return importing.commit_import(
        actor=actor, batch_id=batch.pk, expected_sha256=sha or batch.sha256, reason=reason
    )


def messages(batch):
    return " | ".join(e["message"] for e in batch.errors)


# --- validation --------------------------------------------------------------------------------


def test_validation_registers_nothing_and_stores_the_normalized_rows(people, world):
    batch = validate(people["technician"], GOOD)
    assert batch.errors == [] and batch.row_count == 1
    assert not Specimen.objects.exists() and not Occupancy.objects.exists()
    row = batch.normalized_rows[0]
    assert row["specimen_type"]["code"] == "PLASMA" and row["quantity"] == "1.500000"
    assert AuditEvent.objects.filter(action="specimens.import_validated").count() == 1


def test_the_same_file_again_warns_but_is_not_deduplicated(people, world):
    first = validate(people["technician"], GOOD)
    second = validate(people["technician"], GOOD)
    assert first.sha256 == second.sha256 and first.pk != second.pk
    assert second.warnings and not first.warnings


def test_unknown_and_missing_headers_are_refused(people, world):
    unknown = importing.validate_import(
        actor=people["technician"], csv_bytes=csv_bytes(header=HEADER + ",donor_name")
    )
    assert "Unknown columns: donor_name" in messages(unknown)
    missing = importing.validate_import(
        actor=people["technician"], csv_bytes=b"row_key,specimen_type\nR1,PLASMA\n"
    )
    assert "Missing columns" in messages(missing)
    with pytest.raises(ValidationError) as exc:
        commit(people["technician"], unknown)
    assert codes(exc.value) == {"invalid_import"}


def test_more_than_1000_rows_are_refused(people, world):
    rows = [f"R{n},PLASMA,CRYOVIAL-2ML,1,mL,2026-01-01T09:00:00Z,,,,,,,0" for n in range(1001)]
    batch = validate(people["technician"], *rows)
    assert "1000" in messages(batch) and batch.row_count == 0
    assert not Specimen.objects.exists()


def test_non_utf8_and_oversized_files_are_refused(people, world):
    latin = importing.validate_import(
        actor=people["technician"], csv_bytes=(HEADER + "\nR1,PLASMA\xe9,x\n").encode("latin-1")
    )
    assert "UTF-8" in messages(latin)
    big = importing.validate_import(
        actor=people["technician"], csv_bytes=b"a" * (importing.MAX_BYTES + 1)
    )
    assert "2 MiB" in messages(big)
    assert not Specimen.objects.exists()


def test_duplicate_row_keys_and_duplicate_positions_in_the_file_are_errors(people, world):
    t = people["technician"]
    keys = validate(t, GOOD, GOOD)
    assert "Duplicate row key" in messages(keys)
    same_spot = [
        f"R{n},PLASMA,CRYOVIAL-2ML,1,mL,2026-01-01T09:00:00Z,,,,,BOX-001,C1,0" for n in (1, 2)
    ]
    positions = validate(t, *same_spot)
    assert [e["line"] for e in positions.errors] == [3]
    assert "line 2" in positions.errors[0]["message"]
    with pytest.raises(ValidationError):
        commit(t, positions)
    assert not Specimen.objects.exists()


def test_row_errors_name_the_line_and_column(people, world):
    rows = [
        "R1,PLASMA,CRYOVIAL-2ML,1.1234567,mL,2026-01-01T09:00:00Z,,,,,,,0",  # seven places
        "R2,PLASMA,CRYOVIAL-2ML,1,ML,2026-01-01T09:00:00Z,,,,,,,0",  # unit case matters
        "R3,PLASMA,CRYOVIAL-2ML,1.5,count,2026-01-01T09:00:00Z,,,,,,,0",  # fractional count
        "R4,PLASMA,CRYOVIAL-2ML,1,mL,2999-01-01T09:00:00Z,,,,,,,0",  # future
        "R5,PLASMA,CRYOVIAL-2ML,1,mL,2026-01-01T09:00:00Z,2025-12-31T09:00:00Z,,,,,,0",
        "R6,PLASMA,CRYOVIAL-2ML,1,mL,2026-01-01T09:00:00Z,,,,,BOX-001,,0",  # box without label
        "R7,PLASMA,CRYOVIAL-2ML,1,mL,2026-01-01T09:00:00Z,,,,,BOX-001,Z9,0",  # no such position
    ]
    batch = validate(people["technician"], *rows)
    found = {(e["line"], e["column"]) for e in batch.errors}
    assert {
        (2, "quantity"),
        (3, "unit"),
        (4, "quantity"),
        (5, "collected_at"),
        (6, "processing_started_at"),
        (7, "position_label"),
        (8, "position_label"),
    } <= found


def test_naive_times_are_read_in_the_deployment_time_zone(people, world):
    t = people["technician"]
    batch = validate(t, "R1,PLASMA,CRYOVIAL-2ML,1,mL,2026-01-01 09:00:00,,,,,,,0")
    assert batch.errors == []
    commit(t, batch)
    expected = datetime(2026, 1, 1, 9, 0, tzinfo=ZoneInfo(settings.TIME_ZONE))
    assert Specimen.objects.get().collected_at == expected
    assert Specimen.objects.get().collected_at.astimezone(UTC).utcoffset() == timedelta(0)


def test_viewers_cannot_validate_or_commit(people, world):
    with pytest.raises(PermissionDenied):
        validate(people["viewer"], GOOD)
    batch = validate(people["technician"], GOOD)
    with pytest.raises(PermissionDenied):
        commit(people["viewer"], batch)
    with pytest.raises(PermissionDenied):
        validate(people["nobody"], GOOD)


# --- commit ------------------------------------------------------------------------------------


def test_commit_registers_every_row_with_the_committing_actor_as_custodian(people, world):
    t = people["technician"]
    batch = validate(
        t,
        "R1,PLASMA,CRYOVIAL-2ML,1.5,mL,2026-01-01T09:00:00Z,2026-01-01T09:30:00Z,"
        "2026-01-01T10:00:00Z,P-000123,DEMO,box-001,C3,2",
        "R2,plasma,CRYOVIAL-2ML,0.25,mL,2026-01-01T09:00:00Z,,,,,,,",
    )
    result = commit(t, batch)
    assert len(result.specimen_ids) == 2
    first = Specimen.objects.get(occupancy__position__label="C3")
    assert first.custodian_id == t.pk and first.occupancy.position.box.code == "BOX-001"
    assert first.processing_completed_at == datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    assert first.quantity_on_hand == Decimal("1.5") and first.ledger.count() == 1
    assert Specimen.objects.count() == 2
    event = AuditEvent.objects.get(action="specimens.import_committed")
    assert event.after["rows"] == 2 and len(event.after["accessions"]) == 2
    assert AuditEvent.objects.filter(action="specimens.specimen_registered").count() == 2


def test_commit_is_idempotent_and_the_replay_is_private_to_the_owner(people, world):
    t = people["technician"]
    batch = validate(t, GOOD)
    result = commit(t, batch)
    assert commit(t, batch).pk == result.pk
    assert ImportCommit.objects.count() == 1 and Specimen.objects.count() == 1
    with pytest.raises(PermissionDenied):
        commit(people["technician2"], batch)
    assert commit(people["manager"], batch).pk == result.pk


def test_only_the_owner_or_a_manager_commits_a_batch(people, world):
    batch = validate(people["technician"], GOOD)
    with pytest.raises(PermissionDenied):
        commit(people["technician2"], batch)
    assert not Specimen.objects.exists()
    result = commit(people["manager"], batch)  # a manager may commit someone else's batch
    spec = Specimen.objects.get(pk=result.specimen_ids[0])
    assert spec.custodian_id == people["manager"].pk  # the committer takes custody
    other = validate(people["technician"], GOOD.replace("R1", "R2"))
    assert commit(people["admin"], other).specimen_ids


def test_a_commit_needs_a_reason_and_the_validated_file(people, world):
    t = people["technician"]
    batch = validate(t, GOOD)
    with pytest.raises(ValidationError) as exc:
        commit(t, batch, reason=" ")
    assert codes(exc.value) == {"required"}
    with pytest.raises(ValidationError) as exc:
        commit(t, batch, sha="0" * 64)
    assert codes(exc.value) == {"invalid_import"}
    assert not Specimen.objects.exists() and not ImportCommit.objects.exists()


def test_an_expired_validation_cannot_be_committed(people, world, monkeypatch):
    t = people["technician"]
    batch = validate(t, GOOD)
    assert batch.expires_at - batch.recorded_at == timedelta(hours=24)

    class Later:
        def __getattr__(self, name):
            return getattr(timezone, name)

        @staticmethod
        def now():
            return timezone.now() + timedelta(hours=25)

    monkeypatch.setattr(importing, "tz", Later())
    with pytest.raises(ValidationError) as exc:
        commit(t, batch)
    assert codes(exc.value) == {"invalid_import"}
    assert not Specimen.objects.exists() and not ImportCommit.objects.exists()


def test_a_position_taken_after_validation_registers_nothing_at_all(people, world):
    t = people["technician"]
    batch = validate(
        t,
        "R1,PLASMA,CRYOVIAL-2ML,1,mL,2026-01-01T09:00:00Z,,,,,,,0",
        "R2,PLASMA,CRYOVIAL-2ML,1,mL,2026-01-01T09:00:00Z,,,,,BOX-001,C3,0",
        "R3,PLASMA,CRYOVIAL-2ML,1,mL,2026-01-01T09:00:00Z,,,,,,,0",
    )
    assert batch.errors == []
    taker = stored(t, world, "C3")  # someone else takes the position first
    before = (Specimen.objects.count(), Occupancy.objects.count())
    with pytest.raises(ValidationError) as exc:
        commit(t, batch)
    assert "R2" in " ".join(exc.value.messages) and "occupied" in " ".join(exc.value.messages)
    # Not even rows 1 and 3, which were fine, remain.
    assert (Specimen.objects.count(), Occupancy.objects.count()) == before
    assert list(Specimen.objects.values_list("pk", flat=True)) == [taker.pk]
    assert not ImportCommit.objects.exists()
    assert not AuditEvent.objects.filter(action="specimens.import_committed").exists()


def test_a_catalogue_entry_retired_after_validation_stops_the_commit(people, world):
    t, m = people["technician"], people["manager"]
    batch = validate(t, GOOD)
    services.save_specimen_catalogue(
        actor=m,
        kind="collection",
        object_id=world.collection.pk,
        expected_revision=world.collection.revision,
        values={"is_active": False},
        reason="Collection closed",
    )
    with pytest.raises(ValidationError):
        commit(t, batch)
    assert not Specimen.objects.exists()
    assert ImportBatch.objects.filter(pk=batch.pk).exists()  # the validation record stays


def test_a_changed_position_is_reported_with_the_invalid_import_code(people, world):
    t = people["technician"]
    batch = validate(t, "R1,PLASMA,CRYOVIAL-2ML,1,mL,2026-01-01T09:00:00Z,,,,,BOX-001,C3,0")
    stored(t, world, "C3")
    with pytest.raises(ValidationError) as exc:
        commit(t, batch)
    assert codes(exc.value) == {"invalid_import"}


# --- exports -----------------------------------------------------------------------------------


def rows_of(data):
    return list(csv.DictReader(io.StringIO(data.decode())))


def test_export_respects_the_filters_and_states_units_and_utc_times(people, world):
    t = people["technician"]
    plasma = register(t, world, quantity="2")
    serum = register(t, world, quantity="0.5", specimen_type_id=world.serum.pk)
    everything = rows_of(exports.export_specimens(actor=people["viewer"], filters={}))
    assert {r["accession"] for r in everything} == {plasma.accession, serum.accession}
    only = rows_of(
        exports.export_specimens(actor=people["viewer"], filters={"type": world.serum.pk})
    )
    assert [r["accession"] for r in only] == [serum.accession]
    row = only[0]
    assert (row["quantity_on_hand"], row["unit"], row["specimen_type"]) == ("0.5", "mL", "SERUM")
    assert row["collected_at_utc"].endswith("+00:00") and row["participant_code"] == "P-000123"
    assert row["location"] == "" and row["checked_out"] == "no"
    unplaced = {"placed": "no", "status": "available"}
    assert len(rows_of(exports.export_specimens(actor=people["viewer"], filters=unplaced))) == 2
    assert (
        rows_of(exports.export_specimens(actor=people["viewer"], filters={"placed": "yes"})) == []
    )


def test_export_refuses_unknown_filters_and_unauthorised_people(people, world):
    with pytest.raises(ValidationError):
        exports.export_specimens(actor=people["viewer"], filters={"donor_name": "x"})
    with pytest.raises(ValidationError):
        exports.export_specimens(actor=people["viewer"], filters={}, sort="participant")
    with pytest.raises(PermissionDenied):
        exports.export_specimens(actor=people["nobody"], filters={})
    assert not OutputRecord.objects.exists()


def test_export_is_capped_and_a_refused_export_leaves_no_record(people, world, monkeypatch):
    for _ in range(3):
        register(people["technician"], world)
    monkeypatch.setattr(exports, "MAX_ROWS", 2)
    with pytest.raises(ValidationError) as exc:
        exports.export_specimens(actor=people["viewer"], filters={})
    assert "narrow the filters" in " ".join(exc.value.messages)
    assert not OutputRecord.objects.exists()
    narrow = {"type": world.serum.pk}  # matches nothing, so under the cap
    assert rows_of(exports.export_specimens(actor=people["viewer"], filters=narrow)) == []
    assert OutputRecord.objects.get().count == 0
    assert AuditEvent.objects.get(action="specimens.export").after["count"] == 0
    monkeypatch.setattr(exports, "MAX_ROWS", 3)
    assert len(rows_of(exports.export_specimens(actor=people["viewer"], filters={}))) == 3
    record = OutputRecord.objects.get(count=3)
    event = AuditEvent.objects.get(action="specimens.export", object_id=str(record.pk))
    assert event.after["count"] == 3


def test_export_escapes_cells_a_spreadsheet_would_run_as_formulas(people, world):
    odd = make_user("-formula-user", "technician")
    register(odd, world)
    data = exports.export_specimens(actor=people["viewer"], filters={})
    assert rows_of(data)[0]["custodian"] == "'-formula-user"
    catalogue(people["manager"], "specimen_type", code="ODD", name="=HYPERLINK(1)")
    types = rows_of(
        exports.export_catalogue(actor=people["viewer"], kind="specimen_type", filters={})
    )
    assert "'=HYPERLINK(1)" in {r["name"] for r in types}
    for value in ("=1+1", "+1", "-2", "@x", "\tx", "\rx"):
        assert exports.safe_cell(value) == "'" + value
    assert exports.safe_cell("plain") == "plain" and exports.safe_cell(None) == ""


def test_export_records_the_selection_and_count_without_the_file(people, world):
    register(people["technician"], world)
    viewer = people["viewer"]
    data = exports.export_specimens(actor=viewer, filters={"status": "available"}, sort="-quantity")
    record = OutputRecord.objects.get(kind="export")
    assert record.actor_id == viewer.pk and record.count == 1
    assert record.details == {"filters": {"status": "available"}, "sort": "-quantity"}
    event = AuditEvent.objects.get(action="specimens.export")
    assert event.object_id == str(record.pk) and event.after["count"] == 1
    assert data.decode() not in str(event.after) and "VP-" not in str(event.after)


def test_catalogue_export_lists_fields_filters_and_is_audited(people, world):
    m, viewer = people["manager"], people["viewer"]
    mg = catalogue(m, "unit", code="mg", name="milligram", dimension="mass", fractional=True)
    rows = rows_of(exports.export_catalogue(actor=viewer, kind="unit", filters={}))
    by_code = {r["code"]: r for r in rows}
    assert {"mL", "uL", "count", "mg"} <= set(by_code)  # unit codes keep their case
    assert by_code["count"]["fractional"] == "no" and by_code["mL"]["fractional"] == "yes"
    assert list(rows[0]) == list(services.CATALOGUE_FIELDS["unit"])
    containers = rows_of(exports.export_catalogue(actor=viewer, kind="container_type", filters={}))
    assert containers[0]["nominal_capacity"] == "2" and containers[0]["capacity_unit"] == "mL"
    services.save_specimen_catalogue(
        actor=m,
        kind="unit",
        object_id=mg.pk,
        expected_revision=mg.revision,
        values={"is_active": False},
        reason="Not used here",
    )
    active = rows_of(exports.export_catalogue(actor=viewer, kind="unit", filters={"active": True}))
    assert "mg" not in {r["code"] for r in active}
    record = OutputRecord.objects.filter(kind="catalogue_export").first()
    assert record.details["catalogue"] == "unit"
    assert AuditEvent.objects.filter(action="specimens.catalogue_export").exists()
    with pytest.raises(ValidationError):
        exports.export_catalogue(actor=viewer, kind="nonsense", filters={})
    with pytest.raises(PermissionDenied):
        exports.export_catalogue(actor=people["nobody"], kind="unit", filters={})
