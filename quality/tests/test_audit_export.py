import csv
import io
from datetime import UTC, datetime
from tempfile import SpooledTemporaryFile
from unittest.mock import patch

import pytest
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils import timezone

from core.models import AuditEvent
from quality import services
from quality.audit_exports import export_audits
from quality.tests.test_audits import complete, expectations
from specimens.tests.conftest import stored
from storage.models import Box

pytestmark = pytest.mark.django_db

HEADERS = ["Audit", "Box", "Status", "Created", "Created by", "Reason"]
EXPORT_ACTION = "quality.inventory_audits_exported"


def csv_rows(response):
    try:
        content = b"".join(response.streaming_content).decode()
        return list(csv.reader(io.StringIO(content, newline="")))
    finally:
        if not response.closed:
            response.close()


def test_export_matches_register_selection_and_order_across_pages(client, people, world):
    actor = people["technician"]
    tied = [
        services.create_audit(actor=actor, box_id=world.box.pk, reason="Compare with BOX-002")
        for _ in range(25)
    ]
    selected = services.create_audit(
        actor=actor, box_id=world.box2.pk, reason="Synthetic weekly check"
    )
    services.create_audit(actor=actor, box_id=world.box.pk, reason="Synthetic unrelated check")
    cancelled = services.create_audit(
        actor=actor, box_id=world.box2.pk, reason="Synthetic duplicate check"
    )
    services.cancel_audit(actor=actor, audit_id=cancelled.pk, reason="Synthetic duplicate")
    client.force_login(people["viewer"])
    params = {"q": "  box-002  ", "status": "open", "sort": "-box", "per_page": "25"}

    register_url = reverse("quality:audit-list")
    first_page = client.get(register_url, params).context["page_obj"]
    second_page = client.get(register_url, {**params, "page": "2"}).context["page_obj"]
    response = client.get(reverse("quality:audit-export"), {**params, "page": "2"})

    assert response.status_code == 200
    assert response["Content-Type"] == "text/csv; charset=utf-8"
    assert response["Content-Disposition"] == 'attachment; filename="inventory-audits.csv"'
    assert response["Cache-Control"] == "no-store"
    headers, *rows = csv_rows(response)
    assert headers == HEADERS
    assert [row[0] for row in rows] == [selected.number, *(audit.number for audit in tied)]
    assert [row[0] for row in rows] == [audit.number for audit in [*first_page, *second_page]]
    event = AuditEvent.objects.get(action=EXPORT_ACTION)
    assert event.actor == people["viewer"]
    assert event.after == {
        "filters": {"q": "box-002", "status": "open"},
        "sort": "-box",
        "rows": 26,
    }


def test_export_searches_by_audit_number(client, people, world):
    actor = people["technician"]
    selected = services.create_audit(actor=actor, box_id=world.box.pk, reason="Synthetic check")
    services.create_audit(actor=actor, box_id=world.box.pk, reason="Another synthetic check")
    client.force_login(people["viewer"])
    url = reverse("quality:audit-export")

    for query in (selected.number.lower(), str(selected.pk)):
        response = client.get(url, {"q": query})
        assert response.status_code == 200
        _, row = csv_rows(response)
        assert row[0] == selected.number


def test_export_formats_snapshot_and_register_fields(client, people, world, monkeypatch):
    monkeypatch.setattr("quality.audit_exports.SPOOL_MEMORY_LIMIT", 128)
    creator = people["technician"]
    creator.first_name, creator.last_name = "@Synthetic", "Example"
    creator.save(update_fields=["first_name", "last_name"])
    specimen = stored(creator, world)
    reason = '=Synthetic résumé, "verification"\n' + "Long synthetic creation reason. " * 8 + "End."
    completed = services.create_audit(actor=creator, box_id=world.box.pk, reason=reason)
    observed = expectations(completed)
    observed[completed.positions[1]["id"]] = "EXAMPLE-UNEXPECTED-001"
    complete(people, completed, observed, discrepancy_explanation="Synthetic detail narrative")
    operator = people["technician2"]
    cancelled = services.create_audit(
        actor=operator, box_id=world.box.pk, reason="Synthetic cancelled audit creation"
    )
    services.cancel_audit(actor=operator, audit_id=cancelled.pk, reason="Synthetic final reason")
    created = datetime(2025, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)
    with patch("quality.services.timezone.now", return_value=created):
        opened = services.create_audit(
            actor=operator, box_id=world.box2.pk, reason="Synthetic open audit creation"
        )
    Box.objects.filter(pk=world.box.pk).update(code="BOX-RENAMED")
    client.force_login(people["technician"])

    with (
        timezone.override("Asia/Kolkata"),
        patch.object(
            SpooledTemporaryFile,
            "rollover",
            autospec=True,
            side_effect=SpooledTemporaryFile.rollover,
        ) as rollover,
    ):
        response = client.get(reverse("quality:audit-export"), {"sort": "number"})
        assert response.status_code == 200
        rows = csv_rows(response)
        assert rows == [
            HEADERS,
            [
                completed.number,
                world.box.code,
                "Completed",
                timezone.localtime(completed.created_at).isoformat(),
                "'@Synthetic Example",
                "'" + reason,
            ],
            [
                cancelled.number,
                world.box.code,
                "Cancelled",
                timezone.localtime(cancelled.created_at).isoformat(),
                operator.username,
                cancelled.reason,
            ],
            [
                opened.number,
                world.box2.code,
                "Open",
                "2025-01-02T08:34:05.123456+05:30",
                operator.username,
                opened.reason,
            ],
        ]
    rollover.assert_called_once()
    (spool,) = rollover.call_args.args
    assert spool.closed

    for omitted in (
        specimen.accession,
        "EXAMPLE-UNEXPECTED-001",
        "Synthetic detail narrative",
        "Synthetic final reason",
        creator.email,
        "BOX-RENAMED",
    ):
        assert all(omitted not in cell for row in rows for cell in row)


@pytest.mark.parametrize("role", ["admin", "manager", "technician", "viewer"])
def test_export_allows_read_roles_and_audits_empty_selection(client, people, role):
    client.force_login(people[role])
    response = client.get(reverse("quality:audit-export"), {"status": "completed"})

    assert response.status_code == 200
    assert csv_rows(response) == [HEADERS]
    event = AuditEvent.objects.get(action=EXPORT_ACTION)
    assert event.actor == people[role]
    assert event.after == {"filters": {"status": "completed"}, "sort": "-created", "rows": 0}


def test_export_rejects_unauthorized_users_at_the_view_and_service(client, people):
    url = reverse("quality:audit-export")
    assert client.get(url).status_code == 302
    actor = people["nobody"]
    client.force_login(actor)
    assert client.get(url).status_code == 403
    actor.is_superuser = True
    actor.save(update_fields=["is_superuser"])
    assert client.get(url).status_code == 403
    with pytest.raises(PermissionDenied):
        export_audits(actor=actor, params={})
    assert not AuditEvent.objects.filter(action=EXPORT_ACTION).exists()


@pytest.mark.parametrize(
    "params",
    [
        {"status": "unknown"},
        {"sort": "unknown"},
        {"unexpected": "1"},
        {"per_page": "999"},
        {"q": "Synthetic\x00audit"},
    ],
)
def test_export_rejects_invalid_parameters_without_an_audit(client, people, params):
    client.force_login(people["viewer"])
    response = client.get(reverse("quality:audit-export"), params)

    assert response.status_code == 400
    assert "Content-Disposition" not in response
    assert not AuditEvent.objects.filter(action=EXPORT_ACTION).exists()


def test_export_rejects_selections_above_row_cap(client, people, world, monkeypatch):
    monkeypatch.setattr("quality.audit_exports.EXPORT_CAP", 1)
    actor = people["technician"]
    selected = services.create_audit(actor=actor, box_id=world.box.pk, reason="Synthetic check")
    services.create_audit(actor=actor, box_id=world.box2.pk, reason="Another synthetic check")
    client.force_login(people["viewer"])
    url = reverse("quality:audit-export")

    register = client.get(reverse("quality:audit-list")).content.decode()
    assert "Export CSV</a>" not in register
    assert "Narrow the filters to export CSV. The row limit is 1." in register
    response = client.get(url)
    assert response.status_code == 400
    assert "Content-Disposition" not in response
    assert not AuditEvent.objects.filter(action=EXPORT_ACTION).exists()

    response = client.get(url, {"q": world.box.code})
    assert response.status_code == 200
    _, row = csv_rows(response)
    assert row[0] == selected.number
    assert AuditEvent.objects.get(action=EXPORT_ACTION).after["rows"] == 1
