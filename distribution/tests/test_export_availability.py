"""Distribution registers offer CSV only for valid selections within the download limit."""

import csv
import io
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from django.contrib.messages import get_messages
from django.urls import reverse
from django.utils.html import escape

from core.models import AuditEvent
from distribution import services
from distribution.models import Institution, Investigator, OutputRecord, SpecimenRequest

from .conftest import new_request
from .helpers import fast_password_hashing  # noqa: F401

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def without_page_help(monkeypatch):
    """Look for the export action on the register itself."""
    monkeypatch.setattr("core.context_processors.documentation_for_request", lambda request: {})


def export_link(response, export_path):
    url = response.context["export_url"]
    assert urlsplit(url).path == export_path
    href = re.escape(str(escape(url)))
    link = re.search(rf'<a\b[^>]*href="{href}"[^>]*>', response.content.decode())
    assert link and " download" not in link[0]
    return url


@pytest.mark.parametrize(
    ("register", "kind", "export_route", "invalid_field", "sort", "first_column"),
    [
        ("request", "requests", "export", "status", "-title", "request"),
        ("shipment", "shipments", "export", "state", "state", "shipment"),
        ("return", "returns", "export", "review", "specimen", "specimen"),
        ("study", "studies", "catalogue-export", "status", "title", "Code"),
        ("consent", "consents", "catalogue-export", "status", "study", "Participant"),
        ("withdrawal", "withdrawals", "catalogue-export", "kind", "effective", "Participant"),
        ("agreement", "agreements", "catalogue-export", "kind", "expires", "Agreement"),
        ("category", "categories", "catalogue-export", "active", "name", "Code"),
        ("consent-form", "consent-forms", "catalogue-export", "active", "study", "Study"),
        ("institution", "institutions", "catalogue-export", "active", "name", "Code"),
        ("investigator", "investigators", "catalogue-export", "active", "name", "Code"),
    ],
)
def test_register_distinguishes_empty_and_invalid_filters(
    client, people, register, kind, export_route, invalid_field, sort, first_column
):
    client.force_login(people["viewer"])
    list_url = reverse(f"distribution:{register}-list")
    export_path = reverse(f"distribution:{export_route}", args=[kind])
    invalid = {invalid_field: "unknown", "q": "SYNTHETIC", "sort": sort}

    error_page = client.get(list_url, invalid)
    assert error_page.status_code == 200
    assert invalid_field in error_page.context["filter_form"].errors
    assert b"Export CSV" not in error_page.content and b"row limit" not in error_page.content

    refused = client.get(export_path, {**invalid, "page": 2, "per_page": 25}, follow=True)
    assert len(refused.redirect_chain) == 1 and refused.status_code == 200
    target = urlsplit(refused.redirect_chain[0][0])
    assert target.path == list_url
    assert parse_qs(target.query) == {key: [value] for key, value in invalid.items()}
    assert any("cannot be applied" in str(m) for m in get_messages(refused.wsgi_request))

    unknown = client.get(export_path, {"unrecognized_filter": "synthetic"})
    assert unknown.status_code == 400 and b"Unknown filter" in unknown.content
    assert not OutputRecord.objects.exists()
    assert not AuditEvent.objects.filter(action="distribution.exported").exists()

    empty_page = client.get(list_url, {"q": "NO-SYNTHETIC-MATCH", "sort": "unrecognized"})
    download_url = export_link(empty_page, export_path)
    assert parse_qs(urlsplit(download_url).query)["sort"] == [empty_page.context["sort"]]
    download = client.get(download_url)
    assert download.status_code == 200 and download["Content-Type"].startswith("text/csv")
    (header,) = csv.reader(io.StringIO(download.content.decode(), newline=""))
    assert header[0] == first_column
    record = OutputRecord.objects.get()
    assert record.count == 0 and record.details["list"] == kind
    assert AuditEvent.objects.filter(
        action="distribution.exported", object_id=str(record.pk)
    ).exists()


def test_saved_decision_queue_export_recovers_after_decision_permission_is_revoked(
    client, people, dist, world
):
    technician = people["technician"]
    pending = new_request(technician, dist, world)
    services.submit_request(
        actor=technician, request_id=pending.pk, expected_revision=pending.revision
    )
    new_request(technician, dist, world)
    decider = people["decider"]
    client.force_login(decider)
    list_url = reverse("distribution:request-list")
    export_path = reverse("distribution:export", args=["requests"])
    retained_filters = {"status": "submitted", "sort": "-title"}
    params = {**retained_filters, "awaiting_me": "on"}
    page = client.get(list_url, params)
    assert [row.pk for row in page.context["page_obj"]] == [pending.pk]
    saved_link = export_link(page, export_path)
    selection = {key: [value] for key, value in params.items()}
    assert parse_qs(urlsplit(saved_link).query) == selection

    permission = decider.user_permissions.get(
        codename="approve_distribution", content_type__app_label="accounts"
    )
    decider.user_permissions.remove(permission)
    refused = client.get(saved_link, follow=True)
    assert len(refused.redirect_chain) == 1 and refused.status_code == 200
    target = urlsplit(refused.redirect_chain[0][0])
    assert target.path == list_url and parse_qs(target.query) == selection
    errors = refused.context["filter_form"].non_field_errors()
    assert any("Awaiting my decision" in error for error in errors)
    assert b"Awaiting my decision" in refused.content
    assert not list(refused.context["page_obj"]) and b"Export CSV" not in refused.content
    assert not OutputRecord.objects.exists()
    assert not AuditEvent.objects.filter(action="distribution.exported").exists()

    recovered = client.get(list_url, retained_filters)
    assert [row.pk for row in recovered.context["page_obj"]] == [pending.pk]
    recovered_link = export_link(recovered, export_path)
    assert parse_qs(urlsplit(recovered_link).query) == {
        key: [value] for key, value in retained_filters.items()
    }


@pytest.mark.parametrize("kind", ["requests", "institutions"])
def test_register_uses_all_matching_pages_and_recovers_if_the_download_grows(
    client, people, monkeypatch, kind
):
    manager = people["manager"]
    if kind == "requests":
        limit = 26
        monkeypatch.setattr("distribution.exports.MAX_ROWS", limit)
        institution = Institution.objects.create(
            code="SYNTHETIC", name="Synthetic institution", created_by=manager
        )
        investigator = Investigator.objects.create(
            code="SYNTHETIC",
            name="Synthetic investigator",
            institution=institution,
            created_by=manager,
        )

        def row(code, **extra):
            return SpecimenRequest(
                title=code,
                purpose="Synthetic request",
                received_via="form",
                investigator=investigator,
                institution=institution,
                created_by=manager,
                **extra,
            )

        model, register, export_route = SpecimenRequest, "request", "export"
        field, column, count_key = "title", "title", "rows"
        filters = {"sort": "-title", "status": "draft"}
        excluded = row("SYNTHETIC-EXCLUDED", status="cancelled")
    else:
        limit = 10_000

        def row(code, **extra):
            return Institution(code=code, name=code, created_by=manager, **extra)

        model, register, export_route = Institution, "institution", "catalogue-export"
        field, column, count_key = "code", "Code", "count"
        filters = {"sort": "-name"}
        excluded = row("SYNTHETIC-EXCLUDED", is_active=False)

    expected = [f"SYNTHETIC-{i:05d}" for i in reversed(range(limit))]
    model.objects.bulk_create([*(row(code) for code in expected), excluded, row("OTHER")])
    client.force_login(people["viewer"])
    list_url = reverse(f"distribution:{register}-list")
    export_path = reverse(f"distribution:{export_route}", args=[kind])

    oversized = client.get(list_url, {**filters, "page": 2, "per_page": 25})
    assert oversized.context["page_obj"].paginator.count == limit + 1
    limit_note = f"The row limit is {limit:,}.".encode()
    assert limit_note in oversized.content and b"Export CSV" not in oversized.content

    params = {**filters, "q": "SYNTHETIC-"}
    narrowed = client.get(
        list_url,
        {**params, "q": " SYNTHETIC- ", "page": 2, "per_page": 25, "unrelated": "ignored"},
    )
    page = narrowed.context["page_obj"]
    assert page.number == 2 and page.paginator.count == limit
    assert [getattr(obj, field) for obj in page] == expected[25:50]
    download_url = export_link(narrowed, export_path)
    selection = {key: [value] for key, value in params.items()}
    assert parse_qs(urlsplit(download_url).query) == selection
    download = client.get(download_url)
    assert download.status_code == 200 and download["Content-Type"].startswith("text/csv")
    rows = csv.DictReader(io.StringIO(download.content.decode(), newline=""))
    assert [row[column] for row in rows] == expected

    row(f"SYNTHETIC-{limit:05d}").save()
    refused = client.get(download_url, follow=True)
    assert len(refused.redirect_chain) == 1 and refused.status_code == 200
    target = urlsplit(refused.redirect_chain[0][0])
    assert target.path == list_url and parse_qs(target.query) == selection
    assert limit_note in refused.content and b"Export CSV" not in refused.content
    assert any(
        f"Exports are limited to {limit:,} rows" in str(m)
        for m in get_messages(refused.wsgi_request)
    )
    record = OutputRecord.objects.get()
    event = AuditEvent.objects.get(action="distribution.exported")
    assert event.object_id == str(record.pk) and event.after[count_key] == record.count == limit
