"""Disposal register downloads use the visible selection, request states and staff identity."""

import csv
import io
from datetime import UTC

import pytest
from django.urls import reverse
from django.utils import timezone

from core.models import AuditEvent
from specimens import exports
from specimens.models import OutputRecord

from .conftest import ask_disposal, decide, execute, hold, register

pytestmark = pytest.mark.django_db

HEADINGS = ["Request", "State", "Specimen", "Method", "Destination", "Requested by", "Decided by"]


def download(client, params=None, *, url=None):
    response = client.get(url or reverse("specimens:disposal-export"), params)
    assert response.status_code == 200
    assert response["Content-Type"] == "text/csv; charset=utf-8"
    assert response["Content-Disposition"] == 'attachment; filename="disposal-requests.csv"'
    assert response["Cache-Control"] == "no-store"
    try:
        data = b"".join(response.streaming_content) if response.streaming else response.content
        reader = csv.DictReader(io.StringIO(data.decode(), newline=""))
        assert reader.fieldnames == HEADINGS
        return list(reader)
    finally:
        if not response.closed:
            response.close()


def test_download_matches_both_register_pages_including_sort_ties(client, people, world):
    technician = people["technician"]
    first = register(technician, world)
    second = register(technician, world)
    # The specimen sort ties all 25 requests on the first specimen. Each method identifies
    # one row so a different tie-breaker, a missing row or a duplicate is visible.
    for i, specimen in enumerate([first] * 25 + [second]):
        request = ask_disposal(technician, specimen, method=f"Synthetic method {i}")
        decide(technician, request, "cancel", "Synthetic cancellation")
    ask_disposal(technician, second, method="Unrelated pending request")

    viewer = people["viewer"]
    client.force_login(viewer)
    params = {"q": " vp- ", "state": "cancelled", "sort": "-specimen", "per_page": "25"}
    register_url = reverse("specimens:disposal-list")
    page_one = client.get(register_url, params).context["page_obj"]
    page_two = client.get(register_url, {**params, "page": 2}).context["page_obj"]
    assert len(page_one) == 25 and len(page_two) == 1
    rows = download(client, {**params, "page": 2})

    assert [row["Method"] for row in rows] == [req.method for req in [*page_one, *page_two]]
    assert [row["Specimen"] for row in rows] == [second.accession] + [first.accession] * 25
    record = OutputRecord.objects.get(kind=OutputRecord.Kind.EXPORT)
    assert record.actor == viewer and record.count == 26
    assert record.details == {
        "list": "disposal",
        "filters": {"q": "vp-", "state": "cancelled"},
        "sort": "-specimen",
    }
    event = AuditEvent.objects.get(action="specimens.export")
    assert event.actor == viewer and event.object_id == str(record.pk)
    assert event.after == {"count": 26, **record.details}


def test_current_states_names_and_csv_formatting(client, people, world):
    tech, approver = people["technician"], people["approver"]
    tech.first_name, tech.last_name = "@Synthetic", "Éxample"
    tech.save(update_fields=["first_name", "last_name"])
    method = '=Synthetic "method", sample'
    destination = "+Synthetic\nroom"
    pending, approved, stale, rejected, cancelled, disposed = [
        ask_disposal(tech, register(tech, world), method=method, destination=destination)
        for _ in range(6)
    ]
    decide(approver, approved)
    decide(approver, stale)
    hold(tech, stale.specimen, occurred_at=timezone.now())
    decide(approver, rejected, "reject", "Synthetic rejection")
    decide(tech, cancelled, "cancel", "Synthetic cancellation")
    decide(approver, disposed)
    execute(tech, disposed)
    expected = [
        (pending, "Awaiting approval", ""),
        (approved, "Approved, not yet disposed", approver.username),
        (stale, "Out of date", approver.username),
        (rejected, "Rejected", approver.username),
        (cancelled, "Cancelled", "'@Synthetic Éxample"),
        (disposed, "Disposed", approver.username),
    ]

    client.force_login(people["viewer"])
    with timezone.override("Asia/Kolkata"):
        rows = download(client, {"sort": "specimen"})

    assert rows == [
        {
            "Request": request.recorded_at.astimezone(UTC).isoformat(),
            "State": state,
            "Specimen": request.specimen.accession,
            "Method": "'" + method,
            "Destination": "'" + destination,
            "Requested by": "'@Synthetic Éxample",
            "Decided by": decider,
        }
        for request, state, decider in expected
    ]


def test_personal_filters_follow_the_downloaders_identity(client, people, world):
    first, approver = people["technician"], people["approver"]
    ours = ask_disposal(approver, register(approver, world))
    theirs = ask_disposal(first, register(first, world))
    rejected = ask_disposal(first, register(first, world))
    decide(approver, rejected, "reject", "Synthetic rejection")
    client.force_login(approver)
    for checkbox, selected in (("awaiting_me", theirs), ("mine", ours)):
        page = client.get(reverse("specimens:disposal-list"), {checkbox: "on"})
        assert [req.pk for req in page.context["page_obj"]] == [selected.pk]
        rows = download(client, url=page.context["export_url"])
        assert [row["Specimen"] for row in rows] == [selected.specimen.accession]
        assert OutputRecord.objects.first().details["filters"] == {checkbox: True}

    client.force_login(first)
    assert {row["Specimen"] for row in download(client, {"mine": "on"})} == {
        theirs.specimen.accession,
        rejected.specimen.accession,
    }


def test_all_read_roles_can_export_and_record_an_empty_selection(client, people):
    roles = ("admin", "manager", "technician", "viewer")
    for role in roles:
        actor = people[role]
        client.force_login(actor)
        register = client.get(reverse("specimens:disposal-list"))
        assert not register.context["active_filters"]
        assert "No disposal requests yet" in register.content.decode()
        assert download(client, {"state": "pending"}) == []
        record = OutputRecord.objects.get(actor=actor, kind=OutputRecord.Kind.EXPORT)
        assert record.count == 0
        assert record.details["filters"] == {"state": "pending"}
        assert record.details["sort"] == "-recorded_at"
        assert AuditEvent.objects.get(actor=actor, action="specimens.export").object_id == str(
            record.pk
        )


def test_access_and_invalid_selections_leave_no_export_record(client, people):
    url = reverse("specimens:disposal-export")
    assert client.get(url).status_code == 302
    client.force_login(people["nobody"])
    assert client.get(url).status_code == 403

    client.force_login(people["viewer"])
    for params in (
        {"state": "invalid"},
        {"sort": "unknown"},
        {"per_page": "10"},
        {"actor": people["approver"].pk},
        {"awaiting_me": "on"},
    ):
        response = client.get(url, params)
        assert response.status_code in (400, 403)
        assert "Content-Disposition" not in response
    # A bookmarked approver queue opened by a viewer must not offer a wider download.
    register = client.get(reverse("specimens:disposal-list"), {"awaiting_me": "on"})
    assert register.context["filter_form"].errors and not register.context["export_url"]
    assert client.post(url).status_code == 405
    assert not OutputRecord.objects.exists()
    assert not AuditEvent.objects.filter(action="specimens.export").exists()


def test_download_refuses_over_limit_and_accepts_exact_limit(client, people, world, monkeypatch):
    tech = people["technician"]
    selected = ask_disposal(tech, register(tech, world))
    ask_disposal(tech, register(tech, world))
    client.force_login(people["viewer"])
    monkeypatch.setattr(exports, "MAX_ROWS", 1)

    response = client.get(reverse("specimens:disposal-export"), {"page": 2})
    assert response.status_code == 400
    assert "Content-Disposition" not in response
    assert not OutputRecord.objects.exists()
    assert not AuditEvent.objects.filter(action="specimens.export").exists()

    rows = download(client, {"q": selected.specimen.accession})
    assert [row["Specimen"] for row in rows] == [selected.specimen.accession]
    assert OutputRecord.objects.get().count == 1
