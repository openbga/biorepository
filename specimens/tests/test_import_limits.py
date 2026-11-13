"""CSV import bounds.

Every nonblank record counts against MAX_ROWS whatever its width, a batch keeps at most MAX_ERRORS
errors (with the full count), echoed values are shortened, and the imports list reads summaries
only. All data is synthetic.
"""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from specimens import importing
from specimens.models import ImportBatch

pytestmark = pytest.mark.django_db

HEADER = ",".join(importing.COLUMNS) + "\n"


@pytest.fixture(autouse=True)
def plain_static_storage(settings):
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


def main(response):
    page = response.content.decode()
    start = page.index("<main")
    return page[start : page.index("</main>", start)]


def upload(client, body, name="rows.csv"):
    response = client.post(
        reverse("specimens:import-upload"),
        {"file": SimpleUploadedFile(name, body.encode(), content_type="text/csv")},
    )
    assert response.status_code == 302
    return ImportBatch.objects.get(pk=response.url.rstrip("/").split("/")[-1])


def test_malformed_records_count_against_the_row_limit(client, people):
    client.force_login(people["technician"])
    batch = upload(client, HEADER + "x\n" * (importing.MAX_ROWS + 1))
    assert batch.row_count == 0 and batch.error_count == 1
    assert (
        batch.errors[0]["message"] == f"The file has more than {importing.MAX_ROWS} rows. Split it."
    )
    # Blank lines still do not count.
    batch = upload(client, HEADER + "x\n" * importing.MAX_ROWS + "\n , ,\n" * 50)
    assert batch.error_count == importing.MAX_ROWS


def test_a_batch_keeps_a_bounded_error_list_and_says_so(client, people):
    client.force_login(people["technician"])
    batch = upload(client, HEADER + "x\n" * importing.MAX_ROWS)
    assert len(batch.errors) == importing.MAX_ERRORS < batch.error_count == importing.MAX_ROWS
    assert batch.errors_truncated and not batch.is_valid
    page = main(client.get(reverse("specimens:import-detail", args=[batch.pk])))
    assert f"Showing the first {importing.MAX_ERRORS} of {importing.MAX_ROWS} errors" in page
    assert page.count("has 1 cells") == importing.MAX_ERRORS


def test_echoed_values_are_shortened(people):
    long = "Z" * 5000
    row = [
        long,
        long,
        "CRYOVIAL-2ML",
        "1",
        "mL",
        "2026-11-01T09:30:00",
        "",
        "",
        "",
        "",
        long,
        long,
        "",
    ]
    batch = importing.validate_import(
        actor=people["technician"], csv_bytes=(HEADER + ",".join(row) + "\n").encode()
    )
    assert batch.error_count >= 3
    assert all(len(e["row_key"]) <= importing.SHOWN for e in batch.errors)
    assert all(len(e["message"]) < 200 for e in batch.errors)
    assert len(batch.normalized_rows[0]["row_key"]) == importing.SHOWN
    wide = importing.validate_import(
        actor=people["technician"], csv_bytes=(",".join([long] * 50) + "\n").encode()
    )
    assert all(len(e["message"]) < 1000 for e in wide.errors)


def test_the_imports_list_reads_summaries_only(client, people):
    client.force_login(people["technician"])
    batch = upload(client, HEADER + "x\n" * importing.MAX_ROWS)
    with CaptureQueriesContext(connection) as queries:
        response = client.get(reverse("specimens:import-list"))
    assert not any('"normalized_rows"' in q["sql"] for q in queries)
    (row,) = response.context["page_obj"].object_list
    assert {"errors", "normalized_rows", "warnings"} <= row.get_deferred_fields()
    assert row.error_total == importing.MAX_ROWS and row.state == "invalid"
    assert str(batch.pk) in main(response)
