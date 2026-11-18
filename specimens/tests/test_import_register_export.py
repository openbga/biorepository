"""Operator downloads of the specimen import register."""

import csv
import io
from datetime import UTC, timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from core.models import AuditEvent
from specimens import exports
from specimens.models import ImportBatch, ImportCommit, OutputRecord

pytestmark = pytest.mark.django_db

HEADINGS = ["Import", "State", "Rows", "Errors", "Validated by", "Validated"]


def batch(actor, filename, **values):
    now = timezone.now()
    fields = {
        "actor": actor,
        "filename": filename,
        "recorded_at": now,
        "expires_at": now + timedelta(days=1),
        "sha256": "a" * 64,
        "row_count": 1,
        "error_count": 0,
        **values,
    }
    return ImportBatch.objects.create(**fields)


def download(client, params):
    response = client.get(reverse("specimens:import-export"), params)
    assert response.status_code == 200
    assert response["Content-Type"] == "text/csv; charset=utf-8"
    assert response["Content-Disposition"].startswith("attachment;")
    assert response["Cache-Control"] == "no-store"
    reader = csv.DictReader(io.StringIO(response.content.decode(), newline=""))
    assert reader.fieldnames == HEADINGS
    return list(reader)


def test_download_matches_filtered_register_including_sort_ties_across_pages(client, people):
    tech = people["technician"]
    for n in range(26):
        batch(tech, f"Synthetic weekly {n}.csv", row_count=1 if n < 25 else 2)
    batch(tech, "Synthetic monthly.csv", row_count=3)
    client.force_login(tech)
    params = {"q": "  WEEKLY  ", "sort": "-rows", "per_page": "25"}
    register = reverse("specimens:import-list")
    first = client.get(register, params).context["page_obj"]
    second = client.get(register, {**params, "page": 2}).context["page_obj"]
    assert len(first) == 25 and len(second) == 1
    rows = download(client, {**params, "page": 2})
    assert [row["Import"] for row in rows] == [item.filename for item in [*first, *second]]
    assert [row["Rows"] for row in rows] == ["2"] + ["1"] * 25

    output = OutputRecord.objects.get(kind=OutputRecord.Kind.EXPORT)
    assert output.actor == tech and output.count == 26
    assert output.details == {"list": "imports", "filters": {"q": "WEEKLY"}, "sort": "-rows"}
    event = AuditEvent.objects.get(action="specimens.export")
    assert event.actor == tech and event.object_id == str(output.pk)
    assert event.after == {"count": 26, **output.details}


def test_exported_states_error_counts_and_summary_fields(client, people):
    tech, manager = people["technician"], people["manager"]
    tech.first_name, tech.last_name = "@Synthetic", "Éxample"
    tech.save(update_fields=["first_name", "last_name"])
    filename = '=Synthetic "file", résumé.csv'
    ready = batch(tech, filename, normalized_rows=[{"row_key": "SYNTHETIC-PRIVATE-ROW"}])
    yesterday = timezone.now() - timedelta(days=1)
    expired = batch(manager, "", recorded_at=yesterday - timedelta(days=1), expires_at=yesterday)
    committed = batch(
        manager, "Committed.csv", recorded_at=expired.recorded_at, expires_at=yesterday
    )
    ImportCommit.objects.create(
        actor=manager, batch=committed, reason="Synthetic commit", specimen_ids=["SYNTHETIC-ID"]
    )
    # Old batches keep all errors. Newer batches can retain only a sample of the total.
    error = {"message": "Synthetic validation detail"}
    legacy = batch(manager, "Legacy.csv", errors=[error, error], error_count=None)
    invalid = batch(manager, "Capped.csv", errors=[error], error_count=125)
    client.force_login(manager)
    with timezone.override("Asia/Kolkata"):
        fallback = f"Import {timezone.localtime(expired.recorded_at):%Y-%m-%d %H:%M}"
        rows = download(client, {})
    expected = [
        (ready, "'" + filename, "Ready to commit", 0, "'@Synthetic Éxample"),
        (expired, fallback, "Expired", 0, manager.username),
        (committed, committed.filename, "Committed", 0, manager.username),
        (legacy, legacy.filename, "Has errors", 2, manager.username),
        (invalid, invalid.filename, "Has errors", 125, manager.username),
    ]
    assert len(rows) == len(expected)
    by_name = {row["Import"]: row for row in rows}
    for item, name, state, errors, who in expected:
        assert by_name[name] == {
            "Import": name,
            "State": state,
            "Rows": "1",
            "Errors": str(errors),
            "Validated by": who,
            "Validated": item.recorded_at.astimezone(UTC).isoformat(),
        }


def test_only_import_operators_can_download_audited_empty_selection(client, people):
    url = reverse("specimens:import-export")
    assert client.get(url).status_code == 302
    for role in ("viewer", "nobody"):
        client.force_login(people[role])
        assert client.get(url).status_code == 403
    assert not OutputRecord.objects.exists()
    assert not AuditEvent.objects.filter(action="specimens.export").exists()

    for role in ("admin", "manager", "technician"):
        actor = people[role]
        client.force_login(actor)
        assert download(client, {"q": "unmatched"}) == []
        record = OutputRecord.objects.get(actor=actor)
        assert record.count == 0
        assert record.details == {
            "list": "imports",
            "filters": {"q": "unmatched"},
            "sort": "-recorded_at",
        }
        assert AuditEvent.objects.get(actor=actor, action="specimens.export").object_id == str(
            record.pk
        )


def test_invalid_and_oversized_selections_refused_before_recording_output(
    client, people, monkeypatch
):
    actor = people["manager"]
    batch(actor, "Selected.csv")
    batch(actor, "Other.csv")
    client.force_login(actor)
    monkeypatch.setattr(exports, "MAX_ROWS", 1)
    url = reverse("specimens:import-export")
    for params in ({}, {"sort": "unknown", "q": "Selected"}, {"q": "s" * 201}, {"per_page": "1"}):
        response = client.get(url, params)
        assert response.status_code == 400
        assert "Content-Disposition" not in response
    assert client.post(url).status_code == 405
    assert not OutputRecord.objects.exists()
    assert not AuditEvent.objects.filter(action="specimens.export").exists()
    assert [row["Import"] for row in download(client, {"q": "selected"})] == ["Selected.csv"]
    assert OutputRecord.objects.get().count == 1
