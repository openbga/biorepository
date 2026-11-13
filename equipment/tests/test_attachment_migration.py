"""Migration 0003 moves stored files into one immutable `attachment` record per distinct file and
back again, without changing what any history row shows. The later move of the model
state to `core` is tested in tests/test_core_attachment_migration.py."""

import hashlib
from datetime import timedelta

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

pytestmark = pytest.mark.django_db(transaction=True)

BEFORE = [("equipment", "0002_history_triggers")]
AFTER = [("equipment", "0003_attachment_records")]


def migrate(target=None):
    """Migrate to `target`, or to the latest migration of every app when None."""
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    target = target or executor.loader.graph.leaf_nodes()
    executor.migrate(target)
    return executor.loader.project_state(target).apps


def test_files_move_to_single_records_and_back(people):
    tech = people["technician"]
    apps = migrate(BEFORE)
    try:
        Equipment = apps.get_model("equipment", "Equipment")
        ServiceEvent = apps.get_model("equipment", "ServiceEvent")
        Excursion = apps.get_model("equipment", "Excursion")
        ExcursionEvent = apps.get_model("equipment", "ExcursionEvent")
        tank = Equipment.objects.create(
            code="TANK-MIG-01", name="Tank", kind="ln2_tank", created_by_id=tech.pk
        )
        data = b"%PDF-1.7\n" + bytes(range(256)) * 64
        old_file = {
            "attachment_name": "cert.pdf",
            "attachment_content_type": "application/pdf",
            "attachment_size": len(data),
            "attachment_sha256": hashlib.sha256(data).hexdigest(),
            "attachment_data": data,
        }
        today = timezone.localdate()
        event = ServiceEvent.objects.create(
            equipment=tank,
            kind="maintenance",
            performed_on=today,
            performed_by_external="Example Service Co.",
            outcome="completed",
            recorded_by_id=tech.pk,
            **old_file,
        )
        chain = [event]
        for n in range(2):  # before the migration each correction carried its own copy
            chain.append(
                ServiceEvent.objects.create(
                    equipment=tank,
                    kind="maintenance",
                    performed_on=today,
                    performed_by_external="Example Service Co.",
                    outcome="completed",
                    description=f"Correction {n}",
                    recorded_by_id=tech.pk,
                    corrects=chain[-1],
                    correction_reason="Typo",
                    **old_file,
                )
            )
        plain = ServiceEvent.objects.create(
            equipment=tank,
            kind="repair",
            performed_on=today,
            performed_by_external="Example Service Co.",
            outcome="completed",
            recorded_by_id=tech.pk,
        )
        excursion = Excursion.objects.create(
            equipment=tank,
            started_at=timezone.now() - timedelta(hours=1),
            detection="alarm",
            opened_by_id=tech.pk,
        )
        note_bytes = b"Synthetic note attachment.\n"
        note = ExcursionEvent.objects.create(
            excursion=excursion,
            kind="note",
            actor_id=tech.pk,
            attachment_name="note.txt",
            attachment_content_type="text/plain; charset=utf-8",
            attachment_size=len(note_bytes),
            attachment_sha256=hashlib.sha256(note_bytes).hexdigest(),
            attachment_data=note_bytes,
        )

        apps = migrate(AFTER)
        Attachment = apps.get_model("equipment", "Attachment")
        ServiceEvent = apps.get_model("equipment", "ServiceEvent")
        ExcursionEvent = apps.get_model("equipment", "ExcursionEvent")
        assert Attachment.objects.count() == 2  # three copies of cert.pdf became one record
        refs = set(
            ServiceEvent.objects.filter(pk__in=[e.pk for e in chain]).values_list(
                "attachment_id", flat=True
            )
        )
        assert len(refs) == 1
        cert = Attachment.objects.get(pk=refs.pop())
        assert (cert.name, bytes(cert.data), cert.created_by_id) == ("cert.pdf", data, tech.pk)
        assert ServiceEvent.objects.get(pk=plain.pk).attachment_id is None
        moved = Attachment.objects.get(pk=ExcursionEvent.objects.get(pk=note.pk).attachment_id)
        assert bytes(moved.data) == note_bytes
        with connection.cursor() as cursor:  # history protection is back on
            cursor.execute(
                "SELECT count(*) FROM pg_trigger WHERE tgname IN "
                "('equipment_serviceevent_immutable', 'equipment_excursionevent_immutable', "
                "'attachment_immutable') AND tgenabled = 'O'"
            )
            assert cursor.fetchone()[0] == 3

        apps = migrate(BEFORE)
        ServiceEvent = apps.get_model("equipment", "ServiceEvent")
        for row in ServiceEvent.objects.filter(pk__in=[e.pk for e in chain]):
            assert (row.attachment_name, bytes(row.attachment_data)) == ("cert.pdf", data)
        assert ServiceEvent.objects.get(pk=plain.pk).attachment_name == ""
    finally:
        migrate()  # back to the latest state, where core.Attachment owns the table
