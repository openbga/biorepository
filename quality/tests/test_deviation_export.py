import csv
import io
from datetime import UTC, datetime, timedelta

import pytest
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils import timezone

from core.models import AuditEvent
from quality.deviation_exports import export_deviations
from quality.tests.test_deviations import create, submit

pytestmark = pytest.mark.django_db

HEADERS = ["Deviation", "Status", "Title", "Occurred at", "Updated", "Created by"]
EXPORT_ACTION = "quality.deviations_exported"


def csv_rows(response):
    return list(csv.reader(io.StringIO(response.content.decode(), newline="")))


def test_export_matches_register_selection_and_order_across_pages(client, people):
    earlier = timezone.now() - timedelta(days=2)
    later = earlier + timedelta(days=1)
    tied = [create(people, title="Synthetic review", occurred_at=earlier) for _ in range(25)]
    latest = create(people, description="Synthetic review found this issue.", occurred_at=later)
    create(people, occurred_at=later)
    submit(people, create(people, complete=True, title="Synthetic review", occurred_at=later))
    client.force_login(people["viewer"])
    params = {"q": "  Synthetic review  ", "status": "open", "sort": "-observed", "per_page": "25"}

    register_url = reverse("quality:deviation-list")
    first_page = client.get(register_url, params).context["page_obj"]
    second_page = client.get(register_url, {**params, "page": "2"}).context["page_obj"]
    response = client.get(reverse("quality:deviation-export"), {**params, "page": "2"})

    assert response.status_code == 200
    assert response["Content-Type"] == "text/csv; charset=utf-8"
    assert response["Content-Disposition"] == 'attachment; filename="deviations.csv"'
    assert response["Cache-Control"] == "no-store"
    headers, *rows = csv_rows(response)
    assert headers == HEADERS
    assert [row[0] for row in rows] == [latest.number, *(record.number for record in tied)]
    assert [row[0] for row in rows] == [record.number for record in [*first_page, *second_page]]
    event = AuditEvent.objects.get(action=EXPORT_ACTION)
    assert event.actor == people["viewer"]
    assert event.object_type == "quality.deviation_export"
    assert event.object_id == "deviations"
    assert event.after == {
        "filters": {"q": "Synthetic review", "status": "open"},
        "sort": "-observed",
        "rows": 26,
    }


def test_export_searches_by_deviation_number(client, people):
    selected = create(people)
    create(people)
    client.force_login(people["viewer"])
    url = reverse("quality:deviation-export")

    for query in (selected.number.lower(), str(selected.pk)):
        response = client.get(url, {"q": query})
        assert response.status_code == 200
        _, row = csv_rows(response)
        assert row[0] == selected.number


def test_export_formats_register_columns_and_excludes_detail_narratives(client, people):
    creator = people["technician"]
    creator.first_name, creator.last_name = "@Synthetic", "Example"
    creator.save(update_fields=["first_name", "last_name"])
    title = '=Synthetic résumé, "review"\ncontinued'
    occurred_at = datetime(2025, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)
    reviewed = submit(people, create(people, complete=True, title=title, occurred_at=occurred_at))
    opened = create(people, role="technician2", occurred_at=occurred_at)
    client.force_login(people["technician"])

    with timezone.override("Asia/Kolkata"):
        response = client.get(reverse("quality:deviation-export"), {"sort": "number"})
        assert response.status_code == 200
        headers, reviewed_row, opened_row = csv_rows(response)
        assert headers == HEADERS
        assert reviewed_row == [
            reviewed.number,
            "Awaiting review",
            "'" + title,
            "2025-01-02T08:34:05.123456+05:30",
            timezone.localtime(reviewed.updated_at).isoformat(),
            "'@Synthetic Example",
        ]
        assert opened_row == [
            opened.number,
            "Open",
            opened.title,
            "2025-01-02T08:34:05.123456+05:30",
            timezone.localtime(opened.updated_at).isoformat(),
            people["technician2"].username,
        ]
    for omitted in (
        reviewed.description,
        reviewed.investigation,
        reviewed.corrective_action,
        reviewed.effectiveness,
        creator.email,
        "Investigation ready for independent review",
    ):
        assert omitted not in response.content.decode()


@pytest.mark.parametrize("role", ["admin", "manager", "technician", "viewer"])
def test_export_allows_read_roles_and_audits_an_empty_selection(client, people, role):
    create(people)
    client.force_login(people[role])
    response = client.get(reverse("quality:deviation-export"), {"q": "no-such-report"})

    assert response.status_code == 200
    assert csv_rows(response) == [HEADERS]
    event = AuditEvent.objects.get(action=EXPORT_ACTION)
    assert event.actor == people[role]
    assert event.after == {"filters": {"q": "no-such-report"}, "sort": "-updated", "rows": 0}


def test_export_rejects_unauthorized_users_at_the_view_and_service(client, people):
    url = reverse("quality:deviation-export")
    assert client.get(url).status_code == 302
    actor = people["nobody"]
    actor.is_superuser = True
    actor.save(update_fields=["is_superuser"])
    client.force_login(actor)
    assert client.get(url).status_code == 403
    with pytest.raises(PermissionDenied):
        export_deviations(actor=actor, params={})
    assert not AuditEvent.objects.filter(action=EXPORT_ACTION).exists()


@pytest.mark.parametrize(
    "params",
    [
        {"status": "unknown"},
        {"sort": "unknown"},
        {"unexpected": "1"},
        {"per_page": "999"},
        {"q": "Synthetic\x00report"},
    ],
)
def test_export_rejects_invalid_parameters_without_an_audit(client, people, params):
    client.force_login(people["viewer"])
    response = client.get(reverse("quality:deviation-export"), params)

    assert response.status_code == 400
    assert "Content-Disposition" not in response
    assert not AuditEvent.objects.filter(action=EXPORT_ACTION).exists()


def test_export_rejects_selections_above_the_row_cap(client, people, monkeypatch):
    monkeypatch.setattr("quality.deviation_exports.EXPORT_CAP", 1)
    selected = create(people, title="Synthetic selected deviation")
    create(people)
    client.force_login(people["viewer"])
    url = reverse("quality:deviation-export")

    register = client.get(reverse("quality:deviation-list")).content.decode()
    assert "Export CSV</a>" not in register
    assert "Narrow the filters to export CSV. The row limit is 1." in register
    response = client.get(url)
    assert response.status_code == 400
    assert "Content-Disposition" not in response
    assert not AuditEvent.objects.filter(action=EXPORT_ACTION).exists()

    response = client.get(url, {"q": "selected deviation"})
    assert response.status_code == 200
    _, row = csv_rows(response)
    assert row[0] == selected.number
    assert AuditEvent.objects.get(action=EXPORT_ACTION).after["rows"] == 1
