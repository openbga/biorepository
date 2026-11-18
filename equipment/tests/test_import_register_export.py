import csv
import io
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils import timezone

from core.models import AuditEvent
from equipment.exports import export_list
from equipment.models import ReadingImport, ReadingImportCommit

pytestmark = pytest.mark.django_db

HEADERS = ["File", "State", "Rows", "Errors", "Equipment", "Validated by", "Validated"]
EXPORT_ACTION = "equipment.exported"


def import_batch(actor, **values):
    return ReadingImport(
        **{
            "actor": actor,
            "filename": "logger.csv",
            "sha256": "a" * 64,
            "row_count": 1,
            "expires_at": timezone.now() + timedelta(hours=1),
            **values,
        }
    )


def csv_rows(response):
    return list(csv.reader(io.StringIO(response.content.decode(), newline="")))


def test_export_matches_filtered_register_across_pages_and_keeps_saved_sort(client, people):
    uploader, viewer = people["technician"], people["viewer"]
    selected = ReadingImport.objects.bulk_create(
        [import_batch(uploader, row_count=count) for count in range(1, 27)]
    )
    import_batch(uploader, filename="unrelated.csv", row_count=31).save()
    import_batch(uploader, row_count=30, expires_at=timezone.now() - timedelta(hours=1)).save()
    client.force_login(viewer)
    params = {"q": "  LOGGER  ", "state": "validated"}

    register_url = reverse("equipment:import-list")
    first = client.get(register_url, {**params, "sort": "-rows", "per_page": "25"})
    second = client.get(register_url, {**params, "page": "2", "source": "bookmark"})
    assert first.status_code == second.status_code == 200
    assert first.context["page_obj"].number == 1
    assert second.context["page_obj"].number == 2
    shown = [*first.context["page_obj"], *second.context["page_obj"]]
    assert shown == selected[::-1]

    url = second.context["export_url"]
    assert urlsplit(url).path == reverse("equipment:export", args=["import"])
    assert parse_qs(urlsplit(url).query) == {
        "q": ["LOGGER"],
        "state": ["validated"],
        "sort": ["-rows"],
    }
    response = client.get(url)
    assert response.status_code == 200
    assert response["Content-Type"] == "text/csv; charset=utf-8"
    assert response["Content-Disposition"] == 'attachment; filename="equipment-import.csv"'
    assert response["Cache-Control"] == "no-store"
    headers, *rows = csv_rows(response)
    assert headers == HEADERS
    assert [(row[0], row[1], int(row[2])) for row in rows] == [
        (batch.filename, "Ready to commit", batch.row_count) for batch in shown
    ]
    event = AuditEvent.objects.get(action=EXPORT_ACTION)
    assert event.actor == viewer
    assert event.object_type == "equipment.export"
    assert event.object_id == "import"
    assert event.after == {
        "kind": "import",
        "filters": {"q": "LOGGER", "state": "validated"},
        "sort": "-rows",
        "rows": 26,
    }


def test_export_matches_the_import_columns_and_state_labels(client, people, tank):
    uploader, reviewer = people["technician"], people["manager"]
    uploader.first_name, uploader.last_name = "@Synthetic", "Example"
    uploader.save(update_fields=["first_name", "last_name"])
    filename = '=logger résumé, "west"\ncontinued.csv'
    recorded = datetime(2025, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)
    ready = import_batch(
        uploader,
        filename=filename,
        recorded_at=recorded,
        equipment=tank,
        row_count=2,
        normalized_rows=[{"row": 2, "note": "Synthetic normalized row"}],
        warnings=["Synthetic warning detail"],
    )
    expired_at = timezone.now() - timedelta(hours=1)
    rejected = import_batch(
        reviewer,
        filename="rejected.csv",
        recorded_at=recorded + timedelta(minutes=1),
        row_count=1,
        errors=[{"row": 3, "message": "Synthetic error detail"}],
        expires_at=expired_at,
    )
    committed = import_batch(
        reviewer,
        filename="committed.csv",
        recorded_at=recorded + timedelta(minutes=2),
        expires_at=expired_at,
    )
    expired = import_batch(
        reviewer, filename="", recorded_at=recorded + timedelta(minutes=3), expires_at=expired_at
    )
    ReadingImport.objects.bulk_create([ready, rejected, committed, expired])
    ReadingImportCommit.objects.create(
        batch=committed, actor=uploader, reason="Synthetic commit reason", reading_count=1
    )
    client.force_login(people["viewer"])

    with timezone.override("Asia/Kolkata"):
        response = client.get(reverse("equipment:export", args=["import"]), {"sort": "validated"})

    assert response.status_code == 200
    assert csv_rows(response) == [
        HEADERS,
        [
            "'" + filename,
            "Ready to commit",
            "2",
            "0",
            tank.code,
            "'@Synthetic Example",
            "2025-01-02T08:34:05.123456+05:30",
        ],
        [
            "rejected.csv",
            "Rejected",
            "1",
            "1",
            "Named in the file",
            reviewer.username,
            "2025-01-02T08:35:05.123456+05:30",
        ],
        [
            "committed.csv",
            "Committed",
            "1",
            "0",
            "Named in the file",
            reviewer.username,
            "2025-01-02T08:36:05.123456+05:30",
        ],
        [
            "Import 2025-01-02 08:37",
            "Expired",
            "1",
            "0",
            "Named in the file",
            reviewer.username,
            "2025-01-02T08:37:05.123456+05:30",
        ],
    ]


@pytest.mark.parametrize("role", ["admin", "manager", "technician", "viewer"])
def test_read_roles_can_download_and_audit_a_valid_empty_selection(client, people, role):
    import_batch(people["technician"]).save()
    client.force_login(people[role])
    response = client.get(reverse("equipment:export", args=["import"]), {"state": "rejected"})

    assert response.status_code == 200
    assert csv_rows(response) == [HEADERS]
    event = AuditEvent.objects.get(action=EXPORT_ACTION)
    assert event.actor == people[role]
    assert event.after == {
        "kind": "import",
        "filters": {"state": "rejected"},
        "sort": "-validated",
        "rows": 0,
    }


def test_export_uses_and_audits_the_default_when_sort_is_unknown(client, people):
    uploader = people["technician"]
    earlier = import_batch(
        uploader, filename="a.csv", recorded_at=timezone.now() - timedelta(days=1)
    )
    later = import_batch(uploader, filename="z.csv")
    ReadingImport.objects.bulk_create([earlier, later])
    client.force_login(people["viewer"])
    params = {"sort": "unknown", "per_page": "25", "page": "2"}

    register = client.get(reverse("equipment:import-list"), params)
    response = client.get(reverse("equipment:export", args=["import"]), params)

    assert register.context["sort"] == "-validated"
    assert response.status_code == 200
    assert [row[0] for row in csv_rows(response)[1:]] == [later.filename, earlier.filename]
    assert AuditEvent.objects.get(action=EXPORT_ACTION).after == {
        "kind": "import",
        "filters": {},
        "sort": "-validated",
        "rows": 2,
    }


def test_export_requires_a_read_role_in_the_view_and_service(client, people):
    url = reverse("equipment:export", args=["import"])
    assert client.get(url).status_code == 302
    actor = people["nobody"]
    actor.is_superuser = True
    actor.save(update_fields=["is_superuser"])
    client.force_login(actor)

    response = client.get(url)
    assert response.status_code == 403
    assert "Content-Disposition" not in response
    with pytest.raises(PermissionDenied):
        export_list(actor=actor, kind="import", filters={})
    assert not AuditEvent.objects.filter(action=EXPORT_ACTION).exists()


@pytest.mark.parametrize(
    "params",
    [{"state": "unknown"}, {"unexpected": "1"}, {"per_page": "999"}, {"q": "logger\x00file"}],
)
def test_invalid_selections_do_not_download_or_audit(client, people, params):
    client.force_login(people["viewer"])
    response = client.get(reverse("equipment:export", args=["import"]), params)

    assert response.status_code == 400
    assert response["Content-Type"] == "text/plain; charset=utf-8"
    assert "Content-Disposition" not in response
    assert not AuditEvent.objects.filter(action=EXPORT_ACTION).exists()


def test_register_hides_export_for_an_invalid_state(client, people):
    client.force_login(people["viewer"])
    response = client.get(reverse("equipment:import-list"), {"state": "unknown"})

    assert response.status_code == 200
    assert "state" in response.context["filter_form"].errors
    assert response.context["export_url"] == ""
    export_path = reverse("equipment:export", args=["import"])
    assert f'href="{export_path}' not in response.content.decode()


def test_export_refuses_more_than_ten_thousand_rows_without_an_audit(client, people):
    uploader = people["technician"]
    ReadingImport.objects.bulk_create(
        [import_batch(uploader) for _ in range(10_000)], batch_size=1000
    )
    import_batch(uploader, filename="other.csv").save()
    client.force_login(people["viewer"])
    url = reverse("equipment:export", args=["import"])

    register = client.get(reverse("equipment:import-list"))
    assert register.context["export_url"] == ""
    assert "Narrow the filters to export CSV. The row limit is 10,000." in register.content.decode()

    response = client.get(url)
    assert response.status_code == 400
    assert response["Content-Type"] == "text/plain; charset=utf-8"
    assert "Content-Disposition" not in response
    assert not AuditEvent.objects.filter(action=EXPORT_ACTION).exists()

    response = client.get(url, {"q": "logger.csv"})
    assert response.status_code == 200
    assert len(csv_rows(response)) == 10_001
    assert AuditEvent.objects.get(action=EXPORT_ACTION).after["rows"] == 10_000
