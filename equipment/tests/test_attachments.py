"""Attachments are stored once, bounded by an optional deployment
budget, append-only, and downloaded only through the record that refers to them."""

import hashlib
import random
from urllib.parse import urlencode

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection, transaction
from django.db.models import Sum
from django.db.utils import DatabaseError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from core.models import Attachment
from equipment import services
from equipment.models import ServiceEvent

pytestmark = pytest.mark.django_db


def pdf(size, seed=729):
    return b"%PDF-1.7\n" + random.Random(seed).randbytes(size - 9)


def test_metadata_corrections_over_http_store_no_more_bytes(people, tank):
    """A 512 KiB upload, then two small metadata-only corrections."""
    actor = people["technician"]
    client = Client(enforce_csrf_checks=True)
    client.force_login(actor)
    create_url = reverse("equipment:service-create", args=[tank.pk])
    assert client.get(create_url).status_code == 200
    csrf = client.cookies["csrftoken"].value
    data = pdf(512 * 1024)
    fields = {
        "performed_on": timezone.localdate().isoformat(),
        "outcome": "completed",
        "performed_by_external": "Synthetic reviewer",
        "csrfmiddlewaretoken": csrf,
    }
    upload = SimpleUploadedFile("synthetic-copy-probe.pdf", data, "application/pdf")
    response = client.post(create_url, {**fields, "kind": "maintenance", "attachment": upload})
    assert response.status_code == 302
    event = ServiceEvent.objects.get()

    def stored_bytes():
        with connection.cursor() as cursor:
            cursor.execute("SELECT coalesce(sum(octet_length(data)), 0) FROM attachment")
            return cursor.fetchone()[0]

    def event_table_size():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_total_relation_size('equipment_serviceevent'::regclass)")
            return cursor.fetchone()[0]

    assert stored_bytes() == len(data)
    before = event_table_size()
    for index in range(2):
        body = urlencode(
            {**fields, "description": f"Synthetic correction {index + 1}", "reason": "Check"}
        )
        assert len(body.encode()) < 400
        response = client.post(
            reverse("equipment:service-correct", args=[event.pk]),
            body,
            content_type="application/x-www-form-urlencoded",
        )
        assert response.status_code == 302
        event = ServiceEvent.objects.get(corrects=event)

    rows = list(ServiceEvent.objects.filter(equipment=tank))
    assert len(rows) == 3
    assert {row.attachment_id for row in rows} == {rows[0].attachment_id}
    assert Attachment.objects.count() == 1
    assert stored_bytes() == len(data)  # the bytes are stored once, not three times
    assert event_table_size() - before < 64 * 1024  # two small rows, no file copies
    stored = Attachment.objects.with_data().get()
    assert bytes(stored.data) == data and stored.sha256 == hashlib.sha256(data).hexdigest()
    download = client.get(reverse("equipment:attachment", args=["service", event.pk]))
    assert download.status_code == 200 and download.content == data


def test_a_correction_with_a_new_file_stores_that_file_once(people, tank):
    tech = people["technician"]
    first = services.record_service_event(
        actor=tech,
        equipment_id=tank.pk,
        kind="calibration",
        performed_on=timezone.localdate(),
        outcome="acceptable",
        performed_by=tech,
        attachment=SimpleUploadedFile("a.pdf", pdf(2048, 1)),
    )
    second = services.correct_service_event(
        actor=tech,
        event_id=first.pk,
        reason="Wrong certificate attached",
        attachment=SimpleUploadedFile("b.pdf", pdf(4096, 2)),
    )
    withdrawn = services.correct_service_event(
        actor=tech, event_id=second.pk, reason="Recorded on the wrong tank", void=True
    )
    assert first.attachment_id != second.attachment_id == withdrawn.attachment_id
    assert Attachment.objects.aggregate(total=Sum("size"))["total"] == 2048 + 4096
    assert ServiceEvent.objects.get(pk=first.pk).attachment.name == "a.pdf"  # history kept


def test_the_deployment_budget_refuses_an_upload_that_would_exceed_it(people, tank, settings):
    tech = people["technician"]

    def record(name, size):
        return services.record_service_event(
            actor=tech,
            equipment_id=tank.pk,
            kind="maintenance",
            performed_on=timezone.localdate(),
            outcome="completed",
            performed_by=tech,
            attachment=SimpleUploadedFile(name, pdf(size)),
        )

    settings.ATTACHMENT_STORAGE_LIMIT_BYTES = 6000
    record("one.pdf", 4000)
    with pytest.raises(ValidationError) as caught:
        record("two.pdf", 4000)
    assert "attachment" in caught.value.message_dict
    assert ServiceEvent.objects.count() == 1 and Attachment.objects.count() == 1
    # A correction that keeps the file stores nothing, so the budget does not stop it.
    event = ServiceEvent.objects.get()
    services.correct_service_event(actor=tech, event_id=event.pk, reason="Typo", description="x")
    assert Attachment.objects.count() == 1


def test_attachments_are_append_only(people, tank):
    services.record_service_event(
        actor=people["technician"],
        equipment_id=tank.pk,
        kind="maintenance",
        performed_on=timezone.localdate(),
        outcome="completed",
        performed_by_external="Example Service Co.",
        attachment=SimpleUploadedFile("note.txt", b"Synthetic.\n"),
    )
    for sql in ("UPDATE attachment SET name = 'other.txt'", "DELETE FROM attachment"):
        with pytest.raises(DatabaseError, match="Historical records cannot be"):
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(sql)
    assert Attachment.objects.get().name == "note.txt"


def test_list_queries_never_load_the_bytes(people, tank):
    services.record_service_event(
        actor=people["technician"],
        equipment_id=tank.pk,
        kind="maintenance",
        performed_on=timezone.localdate(),
        outcome="completed",
        performed_by_external="Example Service Co.",
        attachment=SimpleUploadedFile("note.txt", b"Synthetic.\n"),
    )
    assert "data" in Attachment.objects.get().get_deferred_fields()
    client = Client()
    client.force_login(people["viewer"])
    with CaptureBytes() as captured:
        assert client.get(reverse("equipment:equipment-detail", args=[tank.pk])).status_code == 200
    assert not any('"attachment"."data"' in sql for sql in captured.queries)


class CaptureBytes:
    """Record the SQL of a block."""

    def __enter__(self):
        from django.test.utils import CaptureQueriesContext

        self._context = CaptureQueriesContext(connection)
        self._context.__enter__()
        return self

    def __exit__(self, *exc):
        self._context.__exit__(*exc)
        self.queries = [q["sql"] for q in self._context.captured_queries]
