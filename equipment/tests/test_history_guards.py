"""Append-only history and undeletable records, exercised with raw SQL that bypasses the models,
plus the attributed audit event every equipment mutation writes. All data is synthetic.
"""

import importlib
from datetime import timedelta
from decimal import Decimal

import pytest
from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from core.models import Attachment, AuditEvent
from equipment import services
from equipment.models import (
    AffectedSpecimen,
    Equipment,
    Excursion,
    ExcursionEvent,
    LN2Entry,
    ReadingImport,
    ReadingImportCommit,
    ServiceEvent,
    StatusChange,
    TemperatureReading,
)

from .conftest import ago, csv_file, hours_ago, place, register

pytestmark = pytest.mark.django_db

migration = importlib.import_module("equipment.migrations.0002_history_triggers")
HISTORY_TABLES = (
    migration.HISTORY_TABLES
    + importlib.import_module("equipment.migrations.0003_attachment_records").HISTORY_TABLES
)  # the stored files, append-only since 0003
UNDELETABLE_TABLES = migration.UNDELETABLE_TABLES
MESSAGE = "Historical records cannot be updated or deleted"

# A harmless column to set on each table, so the UPDATE is not a no-op by construction.
UPDATE_COLUMN = {
    "equipment_statuschange": "reason",
    "equipment_serviceevent": "description",
    "equipment_ln2entry": "note",
    "equipment_temperaturereading": "note",
    "equipment_readingimport": "filename",
    "equipment_readingimportcommit": "reason",
    "equipment_excursionevent": "note",
    "equipment_affectedspecimen": "location",
    "attachment": "name",
}
MODEL_FOR = {
    "equipment_statuschange": StatusChange,
    "equipment_serviceevent": ServiceEvent,
    "equipment_ln2entry": LN2Entry,
    "equipment_temperaturereading": TemperatureReading,
    "equipment_readingimport": ReadingImport,
    "equipment_readingimportcommit": ReadingImportCommit,
    "equipment_excursionevent": ExcursionEvent,
    "equipment_affectedspecimen": AffectedSpecimen,
    "attachment": Attachment,
}


@pytest.fixture(autouse=True)
def fast_password_hashing(settings):
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


def count(table):
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT count(*) FROM {table}")
        return cursor.fetchone()[0]


def refused(sql):
    """The trigger itself refuses (not a foreign key or some other error)."""
    with (
        pytest.raises(DatabaseError, match=MESSAGE),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute(sql)


@pytest.fixture
def history(people, world, tank, monitor):
    """At least one row in every append-only table, made through the services."""
    t, m, a = people["technician"], people["manager"], people["admin"]
    specimen = register(t, world, occurred_at=ago(272))
    place(t, specimen, world.position("A1"), occurred_at=ago(270))
    services.change_status(
        actor=m,
        equipment_id=monitor.pk,
        expected_revision=monitor.revision,
        status="out_of_service",
        occurred_at=hours_ago(30),
        reason="Battery change",
    )
    services.record_service_event(
        actor=t,
        equipment_id=tank.pk,
        kind="calibration",
        performed_on=timezone.localdate(),
        outcome="acceptable",
        performed_by=t,
        description="Probe check",
        attachment=csv_file("probe,ok\n", "probe-check.csv"),
    )
    services.record_ln2(
        actor=t, equipment_id=tank.pk, kind="level", occurred_at=ago(280), level=Decimal("30")
    )
    services.record_reading(
        actor=t, equipment_id=tank.pk, measured_at=ago(290), value=Decimal("-180.00")
    )
    batch = services.validate_reading_import(
        actor=t,
        file=csv_file(f"measured_at,temperature_c\n{ago(60).isoformat()},-181.00\n"),
        equipment_id=tank.pk,
    )
    services.commit_reading_import(actor=t, batch_id=batch.pk, reason="Logger download")
    excursion = services.open_excursion(
        actor=t,
        equipment_id=tank.pk,
        started_at=ago(300),
        detection="reading",
        ended_at=ago(250),
        cause="Lid left ajar",
        action="Lid closed",
        impact="Top boxes exposed",
    )
    excursion = services.submit_excursion(
        actor=t, excursion_id=excursion.pk, expected_revision=excursion.revision
    )
    services.close_excursion(
        actor=a, excursion_id=excursion.pk, expected_revision=excursion.revision, note="Reviewed"
    )
    return excursion


def test_every_append_only_table_has_rows_in_the_scenario(history):
    assert [table for table in HISTORY_TABLES if count(table) == 0] == []


@pytest.mark.parametrize("table", HISTORY_TABLES)
def test_history_rows_cannot_be_updated_or_deleted(history, table):
    before = count(table)
    refused(f"UPDATE {table} SET {UPDATE_COLUMN[table]} = {UPDATE_COLUMN[table]}")
    refused(f"UPDATE {table} SET id = id")
    refused(f"DELETE FROM {table}")
    assert count(table) == before


@pytest.mark.parametrize("table", HISTORY_TABLES)
def test_history_rows_cannot_be_saved_over_through_the_models_either(history, table):
    row = MODEL_FOR[table].objects.first()
    with pytest.raises(DatabaseError, match=MESSAGE), transaction.atomic():
        row.save()
    assert MODEL_FOR[table].objects.filter(pk=row.pk).exists()


@pytest.mark.parametrize("table", HISTORY_TABLES)
def test_a_changed_value_is_refused_not_just_a_no_op_update(history, table):
    column = UPDATE_COLUMN[table]
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT {column} FROM {table} LIMIT 1")
        original = cursor.fetchone()[0]
    refused(f"UPDATE {table} SET {column} = 'tampered'")
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT {column} FROM {table} LIMIT 1")
        assert cursor.fetchone()[0] == original


@pytest.mark.parametrize("table", UNDELETABLE_TABLES)
def test_equipment_and_excursions_cannot_be_deleted(history, table):
    before = count(table)
    refused(f"DELETE FROM {table}")
    assert count(table) == before


def test_deleting_an_unreferenced_equipment_row_is_refused_by_the_trigger(people, monitor):
    """No history refers to this monitor, so only the trigger stands in the way."""
    refused(f"DELETE FROM equipment_equipment WHERE id = '{monitor.pk}'")
    assert Equipment.objects.filter(pk=monitor.pk).exists()
    with pytest.raises(DatabaseError, match=MESSAGE), transaction.atomic():
        monitor.delete()


def test_deleting_an_excursion_through_the_model_is_refused(history):
    excursion = Excursion.objects.get()
    with pytest.raises(DatabaseError), transaction.atomic():
        excursion.delete()
    assert Excursion.objects.filter(pk=excursion.pk).exists()


def test_equipment_and_excursion_rows_may_still_be_updated_through_the_services(
    history, people, tank
):
    tank.refresh_from_db()
    updated = services.save_equipment(
        actor=people["manager"],
        equipment_id=tank.pk,
        expected_revision=tank.revision,
        reason="Relabelled",
        name="Tank VP-01 (north)",
    )
    assert updated.revision == tank.revision + 1
    excursion = Excursion.objects.get()
    reopened = services.reopen_excursion(
        actor=people["manager"],
        excursion_id=excursion.pk,
        expected_revision=excursion.revision,
        reason="New evidence",
    )
    assert reopened.status == "open" and reopened.revision == excursion.revision + 1
    with connection.cursor() as cursor:
        cursor.execute("UPDATE equipment_equipment SET revision = revision + 1")
        assert cursor.rowcount > 0
        cursor.execute("UPDATE equipment_excursion SET revision = revision + 1")
        assert cursor.rowcount > 0


def test_every_equipment_table_is_guarded_or_deliberately_mutable(history):
    mutable = set(UNDELETABLE_TABLES)
    tables = {model._meta.db_table for model in apps.get_app_config("equipment").get_models()}
    assert tables - set(HISTORY_TABLES) - mutable == set()


def test_a_correction_is_a_new_row_and_leaves_the_original_untouched(history, people):
    original = TemperatureReading.objects.filter(source="manual").get()
    services.correct_reading(
        actor=people["technician"],
        reading_id=original.pk,
        reason="Misread",
        value=Decimal("-179.00"),
    )
    assert TemperatureReading.objects.filter(source="manual").count() == 2
    assert TemperatureReading.objects.get(pk=original.pk).value == Decimal("-180.00")


# ------------------------------------------------------------------ audit events


def test_an_audit_event_commits_with_the_change_or_not_at_all(people, tank):
    """A failed service leaves neither the row nor an audit event."""
    audit_before = AuditEvent.objects.count()
    with pytest.raises(ValidationError):
        services.record_reading(
            actor=people["technician"],
            equipment_id=tank.pk,
            measured_at=ago(5),
            value=Decimal("-300"),
        )
    assert AuditEvent.objects.count() == audit_before
    assert not TemperatureReading.objects.exists()


def _audited(action, actor, object_type=None):
    events = AuditEvent.objects.filter(action=action)
    assert events.count() == 1, (action, list(events.values_list("action", flat=True)))
    event = events.get()
    assert event.actor == actor
    if object_type:
        assert event.object_type == object_type
    return event


def test_every_mutation_writes_an_attributed_audit_event(people, world, tank, dewar, monitor):
    t, m, a = people["technician"], people["manager"], people["admin"]
    equipment = "equipment.equipment"

    # register and edit (tank was registered by the manager in the fixture)
    assert AuditEvent.objects.filter(action="equipment.registered", actor=m).count() == 3
    tank.refresh_from_db()
    services.save_equipment(
        actor=a,
        equipment_id=tank.pk,
        expected_revision=tank.revision,
        reason="Serial recorded",
        serial_number="SN-0001",
    )
    event = _audited("equipment.updated", a, equipment)
    assert event.after["serial_number"] == "SN-0001" and event.reason == "Serial recorded"

    services.change_status(
        actor=m,
        equipment_id=dewar.pk,
        expected_revision=dewar.revision,
        status="out_of_service",
        occurred_at=hours_ago(2),
        reason="Valve service",
    )
    event = _audited("equipment.status_changed", m, equipment)
    assert (event.before["status"], event.after["status"]) == ("in_service", "out_of_service")
    assert event.reason == "Valve service"

    service = services.record_service_event(
        actor=t,
        equipment_id=tank.pk,
        kind="calibration",
        performed_on=timezone.localdate(),
        outcome="acceptable",
        performed_by=t,
    )
    assert _audited("equipment.calibration_recorded", t).after["service_event"] == str(service.pk)

    services.correct_service_event(actor=m, event_id=service.pk, reason="Wrong tank", void=True)
    event = _audited("equipment.service_withdrawn", m)
    assert event.reason == "Wrong tank" and event.after["voided"] is True

    entry = services.record_ln2(
        actor=t, equipment_id=tank.pk, kind="level", occurred_at=ago(60), level=Decimal("30")
    )
    assert _audited("equipment.level_check_recorded", t).after["ln2_entry"] == str(entry.pk)
    services.correct_ln2(actor=t, entry_id=entry.pk, reason="Misread", level=Decimal("29"))
    event = _audited("equipment.level_log_entry_corrected", t)
    assert event.before["level"] == "30.00" and event.after["level"] == "29.00"

    sample = services.record_reading(
        actor=t, equipment_id=tank.pk, measured_at=ago(50), value=Decimal("-180.00")
    )
    _audited("equipment.reading_recorded", t)
    services.correct_reading(actor=m, reading_id=sample.pk, reason="Misread", void=True)
    _audited("equipment.reading_withdrawn", m)

    batch = services.validate_reading_import(
        actor=t,
        file=csv_file(f"measured_at,temperature_c\n{ago(40).isoformat()},-181.00\n"),
        equipment_id=tank.pk,
    )
    event = _audited("equipment.import_validated", t, "equipment.readingimport")
    assert event.after["rows"] == 1 and event.after["errors"] == 0
    services.commit_reading_import(actor=m, batch_id=batch.pk, reason="Download")
    event = _audited("equipment.import_committed", m, "equipment.readingimport")
    assert event.after["readings"] == 1 and event.reason == "Download"

    excursion = services.open_excursion(
        actor=t, equipment_id=tank.pk, started_at=ago(300), detection="alarm"
    )
    _audited("excursion.opened", t, "equipment.excursion")
    excursion = services.update_excursion(
        actor=t,
        excursion_id=excursion.pk,
        expected_revision=excursion.revision,
        ended_at=ago(250),
        cause="Power cut",
        action="Generator started",
        impact="None expected",
    )
    _audited("excursion.updated", t)
    services.add_excursion_note(actor=t, excursion_id=excursion.pk, note="Supplier called")
    _audited("excursion.note_added", t)
    excursion = services.submit_excursion(
        actor=t, excursion_id=excursion.pk, expected_revision=excursion.revision
    )
    _audited("excursion.submitted", t)
    excursion = services.return_excursion(
        actor=m, excursion_id=excursion.pk, expected_revision=excursion.revision, note="Add log"
    )
    assert _audited("excursion.returned", m).reason == "Add log"
    excursion = services.submit_excursion(
        actor=t, excursion_id=excursion.pk, expected_revision=excursion.revision
    )
    excursion = services.close_excursion(
        actor=a, excursion_id=excursion.pk, expected_revision=excursion.revision, note="Reviewed"
    )
    event = _audited("excursion.closed", a)
    assert event.reason == "Reviewed" and event.before["status"] == "review"
    assert event.after["status"] == "closed"
    services.reopen_excursion(
        actor=m, excursion_id=excursion.pk, expected_revision=excursion.revision, reason="Evidence"
    )
    assert _audited("excursion.reopened", m).reason == "Evidence"


def test_audit_events_cannot_be_changed_either(people, tank):
    services.record_reading(
        actor=people["technician"], equipment_id=tank.pk, measured_at=ago(5), value=Decimal("-180")
    )
    refused("DELETE FROM core_auditevent")
    refused("UPDATE core_auditevent SET reason = 'tampered'")


def test_stored_times_never_run_ahead_of_recording(history):
    """Database checks back the service rules: nothing is stored as measured in the future."""
    now = timezone.now()
    assert not TemperatureReading.objects.filter(measured_at__gt=now).exists()
    assert not LN2Entry.objects.filter(occurred_at__gt=now).exists()
    assert not StatusChange.objects.filter(occurred_at__gt=now + timedelta(seconds=1)).exists()
