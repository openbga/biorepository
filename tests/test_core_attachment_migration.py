"""core 0003 and equipment 0004 move the Attachment model state from equipment to core without
touching the database: on a fresh database (the test database is built from migrations) and on
one where equipment 0003 was already applied with stored files.

Other apps (distribution) refer to the table too. The checks compare the attachment table itself
(constraints, indexes, trigger) exactly, and its references against the foreign keys the installed
models declare, so a new referencing app is covered rather than being a false failure.
"""

import hashlib
from io import StringIO

import pytest
from django.apps import apps as current_apps
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder
from django.utils import timezone

from core.models import Attachment
from equipment.models import ServiceEvent

# equipment 0003 applied, the move not yet: the state of a database upgraded from that release.
EQUIPMENT_0003 = [("equipment", "0003_attachment_records"), ("core", "0002_audit_immutable")]


def migrate(target=None):
    """Migrate to `target`, or to the latest migration of every app when None."""
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    target = target or executor.loader.graph.leaf_nodes()
    executor.migrate(target)
    return executor.loader.project_state(target).apps


def declared_references():
    """Tables whose installed models declare a foreign key to core.Attachment."""
    return sorted(
        model._meta.db_table
        for model in current_apps.get_models()
        if any(
            field.is_relation and field.related_model is Attachment
            for field in model._meta.concrete_fields
        )
    )


def applied(app):
    return {name for a, name in MigrationRecorder(connection).applied_migrations() if a == app}


def table_exists(table):
    with connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass(%s) IS NOT NULL", [table])
        return cursor.fetchone()[0]


def schema():
    """What the database itself holds for the attachment table, by name: (checks, indexes,
    triggers) of the table itself, and the tables with a foreign key to it."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT conname FROM pg_constraint WHERE conrelid = 'attachment'::regclass "
            "AND contype = 'c' ORDER BY conname"
        )
        checks = [row[0] for row in cursor.fetchall()]
        cursor.execute("SELECT indexname FROM pg_indexes WHERE tablename = 'attachment'")
        indexes = {row[0] for row in cursor.fetchall()}
        cursor.execute(
            "SELECT tgname, tgenabled FROM pg_trigger "
            "WHERE tgrelid = 'attachment'::regclass AND NOT tgisinternal"
        )
        triggers = cursor.fetchall()
        cursor.execute(
            "SELECT conrelid::regclass::text FROM pg_constraint "
            "WHERE confrelid = 'attachment'::regclass AND contype = 'f' ORDER BY 1"
        )
        references = [row[0] for row in cursor.fetchall()]
    return (checks, indexes, triggers), references


@pytest.mark.django_db
def test_the_move_is_state_only():
    for app, name in (("core", "0003_attachment"), ("equipment", "0004_attachment_to_core")):
        out = StringIO()
        call_command("sqlmigrate", app, name, stdout=out)
        statements = [
            line for line in out.getvalue().splitlines() if line and not line.startswith("--")
        ]
        assert statements == ["BEGIN;", "COMMIT;"], (app, name, statements)


@pytest.mark.django_db
def test_a_fresh_database_has_the_table_owned_by_core():
    assert Attachment._meta.app_label == "core" and Attachment._meta.db_table == "attachment"
    assert "attachment" not in current_apps.all_models["equipment"]
    assert ServiceEvent._meta.get_field("attachment").related_model is Attachment
    (checks, indexes, triggers), references = schema()
    assert checks == ["attachment_described", "attachment_sha256", "attachment_size_check"]
    assert {"attachment_pkey", "attachment_sha256_idx"} <= indexes
    assert triggers == [("attachment_immutable", "O")]
    assert {"equipment_excursionevent", "equipment_serviceevent"} <= set(references)
    assert references == declared_references()  # every model reference exists, nothing else


@pytest.mark.django_db(transaction=True)
def test_a_database_with_equipment_0003_keeps_its_files_and_references(staff):
    tech = staff("move_tech", "technician")
    table, references = schema()
    assert references == declared_references()
    equipment_references = ["equipment_excursionevent", "equipment_serviceevent"]
    # Apps whose migrations depend on core 0003 (distribution) are rolled back with it; that
    # drops their tables and data, as any rollback below a dependency does.
    dependents = sorted({t for t in references if not t.startswith("equipment_")})
    try:
        old = migrate(EQUIPMENT_0003)
        OldAttachment = old.get_model("equipment", "Attachment")
        Equipment = old.get_model("equipment", "Equipment")
        OldServiceEvent = old.get_model("equipment", "ServiceEvent")
        data = b"%PDF-1.7\nsynthetic certificate\n"
        stored = OldAttachment.objects.create(
            name="cert.pdf",
            content_type="application/pdf",
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            data=data,
            created_by_id=tech.pk,
        )
        tank = Equipment.objects.create(
            code="TANK-MOVE-01", name="Tank", kind="ln2_tank", created_by_id=tech.pk
        )
        event = OldServiceEvent.objects.create(
            equipment=tank,
            kind="calibration",
            performed_on=timezone.localdate(),
            performed_by_external="Example Service Co.",
            outcome="acceptable",
            recorded_by_id=tech.pk,
            attachment=stored,
        )
        # Rolling the state back changed nothing on the attachment table or equipment's references.
        assert schema() == (table, equipment_references)
        assert not applied("distribution") and not any(map(table_exists, dependents))
        assert "0003_attachment" not in applied("core")

        migrate()
        assert schema() == (table, references)  # dependents re-created, references restored
        moved = Attachment.objects.with_data().get()
        assert (moved.pk, moved.name, bytes(moved.data)) == (stored.pk, "cert.pdf", data)
        assert moved.created_by_id == tech.pk
        assert ServiceEvent.objects.get(pk=event.pk).attachment == moved
    finally:
        migrate()
