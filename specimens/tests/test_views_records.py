"""Specimen read-side pages: lists, records, documents, downloads and dashboard queues.

Smoke and contract tests for specimens.views; the rules themselves are tested at the service
level. All data is synthetic.
"""

import re

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from specimens import importing, services
from specimens.dashboard import queues
from specimens.models import ImportBatch, Specimen

from .conftest import check_out, place, register

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def plain_static_storage(settings):
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


@pytest.fixture
def specimens(people, world):
    """One placed, one checked out, one never stored, and a pending disposal request."""
    tech = people["technician"]
    stored = register(tech, world)
    place(tech, stored, world.position("A1"))
    out = register(tech, world)
    place(tech, out, world.position("A2"))
    check_out(tech, out, tech)
    loose = register(tech, world)
    stored.refresh_from_db()
    request = services.request_disposal(
        actor=tech,
        specimen_id=stored.pk,
        expected_revision=stored.revision,
        method="Autoclave",
        destination="Biohazard waste",
        reason="Sample quality failure",
    )
    return {"stored": stored, "out": out, "loose": loose, "request": request}


def login(client, user):
    client.force_login(user)
    return client


def html(response):
    """The page's <main> only: the page-help panel elsewhere names every action by design."""
    page = response.content.decode()
    start = page.find("<main")
    return page[start : page.index("</main>", start)] if start >= 0 else page


def read_pages(world, s):
    return [
        reverse("specimens:specimen-list"),
        reverse("specimens:specimen-list") + "?status=available&sort=-registered_at",
        reverse("specimens:specimen-detail", args=[s["stored"].pk]),
        reverse("specimens:specimen-detail", args=[s["out"].pk]),
        reverse("specimens:specimen-detail", args=[s["loose"].pk]),
        reverse("specimens:disposal-list"),
        reverse("specimens:disposal-detail", args=[s["request"].pk]),
        reverse("specimens:label-preview") + f"?id={s['stored'].pk}&id={s['out'].pk}",
        reverse("specimens:label-preview"),
        reverse("specimens:participant-list"),
        reverse("specimens:collection-list"),
        reverse("specimens:specimen-type-list") + "?sort=-name",
        reverse("specimens:container-type-list"),
        reverse("specimens:unit-list"),
        reverse("dashboard"),
    ]


@pytest.mark.parametrize("who", ["viewer", "manager"])
def test_read_pages_render(client, people, world, specimens, who):
    login(client, people[who])
    for url in read_pages(world, specimens):
        response = client.get(url)
        assert response.status_code == 200, url


def test_manager_pages_render(client, people, world, specimens):
    login(client, people["manager"])
    for url in (
        reverse("specimens:import-list"),
        reverse("specimens:import-upload"),
        reverse("specimens:participant-create"),
        reverse("specimens:unit-create"),
        reverse("specimens:container-type-edit", args=[world.cryovial.pk]),
        reverse("specimens:unit-edit", args=[world.ml.pk]),
    ):
        assert client.get(url).status_code == 200, url


def test_access_is_refused_without_the_role(client, people, world, specimens):
    login(client, people["nobody"])
    for url in read_pages(world, specimens)[:-1]:
        assert client.get(url).status_code == 403, url
    login(client, people["viewer"])
    for url in (
        reverse("specimens:import-list"),
        reverse("specimens:import-upload"),
        reverse("specimens:import-template"),
        reverse("specimens:participant-create"),
        reverse("specimens:unit-edit", args=[world.ml.pk]),
    ):
        assert client.get(url).status_code == 403, url


def test_viewer_sees_no_action_controls(client, people, specimens):
    login(client, people["viewer"])
    page = html(client.get(reverse("specimens:specimen-detail", args=[specimens["stored"].pk])))
    assert "Print label" in page
    assert "Record check-out" not in page and "Request disposal" not in page
    assert "Register specimen" not in html(client.get(reverse("specimens:specimen-list")))


def test_record_primary_follows_the_state(client, people, specimens):
    login(client, people["technician"])
    detail = lambda s: html(client.get(reverse("specimens:specimen-detail", args=[s.pk])))  # noqa: E731
    stored = detail(specimens["stored"])
    assert "Record check-out" in stored
    assert "A disposal request is already open." in stored  # blocked with the reason
    out = detail(specimens["out"])
    assert "Record check-in" in out and "Time out of storage is running" in out
    assert "Place in storage" in detail(specimens["loose"])
    assert "Unknown before registration" not in stored  # baseline 0 is known


def test_disposal_detail_wording_by_role(client, people, specimens):
    url = reverse("specimens:disposal-detail", args=[specimens["request"].pk])
    login(client, people["technician"])
    page = html(client.get(url))
    assert "You requested this. Another person with disposal approval must approve it." in page
    assert "Approve disposal" not in page and "Cancel disposal request" in page
    login(client, people["approver"])
    page = html(client.get(url))
    assert "Approve disposal" in page and "Reject" in page
    assert "You requested this" not in page


def test_an_approved_request_offers_no_cancel(client, people, specimens):
    """The page agrees with the service: a decided request takes no second decision."""
    request = specimens["request"]
    stored = Specimen.objects.get(pk=specimens["stored"].pk)
    services.decide_disposal(
        actor=people["approver"],
        request_id=request.pk,
        expected_revision=stored.revision,
        decision="approve",
        reason="Checked against the closure notice",
    )
    url = reverse("specimens:disposal-detail", args=[request.pk])
    for role in ("technician", "manager"):
        page = html(login(client, people[role]).get(url))
        assert "Record disposal" in page and "Cancel disposal request" not in page
    response = client.get(
        reverse("specimens:disposal-decide", args=[request.pk]), {"decision": "cancel"}
    )
    assert "This request was already approved. Record the disposal instead." in html(response)


def test_the_record_links_to_its_temperature_excursions(client, people, specimens, settings):
    stored = specimens["stored"]
    url = reverse("specimens:specimen-detail", args=[stored.pk])
    target = reverse("equipment:excursion-list") + f"?specimen={stored.accession}"
    assert f'href="{target}"' in html(login(client, people["viewer"]).get(url))
    settings.INSTALLED_APPS = [app for app in settings.INSTALLED_APPS if app != "equipment"]
    assert "Temperature excursions" not in html(client.get(url))


def test_lookup_opens_the_record_or_returns_to_the_list(client, people, specimens):
    login(client, people["viewer"])
    accession = specimens["stored"].accession
    response = client.get(reverse("specimens:lookup"), {"q": f"  {accession.lower()} "})
    assert response.url == reverse("specimens:specimen-detail", args=[specimens["stored"].pk])
    response = client.get(reverse("specimens:lookup"), {"q": "VP-999999"}, follow=True)
    assert response.redirect_chain[-1][0].startswith(reverse("specimens:specimen-list") + "?q=")
    assert "No specimen has ID VP-999999." in html(response)


def test_export_csv_matches_the_filters(client, people, specimens):
    login(client, people["viewer"])
    response = client.get(reverse("specimens:export"), {"checked_out": "yes"})
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/csv")
    assert re.match(
        r'attachment; filename="specimens-\d{8}-\d{4}\.csv"', response["Content-Disposition"]
    )
    lines = response.content.decode().splitlines()
    assert lines[0].startswith("accession,status,")
    assert len(lines) == 2 and lines[1].startswith(specimens["out"].accession)


def test_list_filters_sort_and_invalid_filter(client, people, specimens):
    login(client, people["viewer"])
    url = reverse("specimens:specimen-list")
    response = client.get(url, {"placed": "yes", "sort": "-accession"})
    rows = list(response.context["page_obj"])
    assert [r.accession for r in rows] == [specimens["stored"].accession]
    response = client.get(url, {"sort": "-accession"})
    accessions = [r.accession for r in response.context["page_obj"]]
    assert accessions == sorted(accessions, reverse=True) and len(accessions) == 3
    response = client.get(url, {"participant": "P-404404"})
    assert response.context["page_obj"].paginator.count == 0
    assert "These filters cannot be applied" in html(response)
    assert client.get(url, {"per_page": "7"}).status_code == 400


def test_disposal_list_filters(client, people, specimens):
    url = reverse("specimens:disposal-list")
    login(client, people["approver"])
    response = client.get(url, {"awaiting_me": "on"})
    assert response.context["page_obj"].paginator.count == 1
    login(client, people["technician"])
    response = client.get(url, {"mine": "on", "state": "pending"})
    assert response.context["page_obj"].paginator.count == 1
    assert client.get(url, {"state": "nonsense"}).context["page_obj"].paginator.count == 0


def test_import_upload_validate_and_commit(client, people, world):
    login(client, people["technician"])
    template = client.get(reverse("specimens:import-template"))
    assert template["Content-Type"].startswith("text/csv")
    content = (
        ",".join(importing.COLUMNS)
        + "\nR1,PLASMA,CRYOVIAL-2ML,1.5,mL,2026-01-01T09:00:00Z,,,P-000123,DEMO,BOX-001,C3,\n"
    ).encode()
    response = client.post(
        reverse("specimens:import-upload"),
        {"file": SimpleUploadedFile("shipment.csv", content, content_type="text/csv")},
    )
    batch = ImportBatch.objects.get()
    assert response.url == reverse("specimens:import-detail", args=[batch.pk])
    page = html(client.get(response.url))
    assert "Register 1 specimen" in page and "BOX-001 C3" in page
    response = client.post(
        reverse("specimens:import-commit", args=[batch.pk]),
        {"reason": "Shipment 7", "expected_sha256": batch.sha256},
        follow=True,
    )
    assert "Registered 1 specimen." in html(response)
    assert Specimen.objects.count() == 1
    assert "Print labels" in html(response)
    assert client.get(reverse("specimens:import-list")).context["page_obj"].paginator.count == 1


def test_import_upload_rejects_a_bad_file(client, people, world):
    login(client, people["technician"])
    response = client.post(
        reverse("specimens:import-upload"),
        {"file": SimpleUploadedFile("notes.txt", b"hello", content_type="text/plain")},
    )
    assert response.status_code == 422 and not ImportBatch.objects.exists()


def test_label_preview_shows_barcodes(client, people, specimens):
    login(client, people["viewer"])
    stored = specimens["stored"]
    page = html(client.get(reverse("specimens:label-preview"), {"id": [stored.pk, stored.pk]}))
    assert page.count("<svg") >= 2 and "Download PDF" in page
    page = html(client.get(reverse("specimens:label-preview"), {"id": "not-a-uuid"}))
    assert "No labels to preview" in page and "Download PDF" not in page


def test_catalogue_create_and_edit(client, people, world):
    login(client, people["manager"])
    response = client.post(reverse("specimens:participant-create"), {"code": "p-000777"})
    assert response.url == reverse("specimens:participant-list")
    from specimens.models import Participant

    participant = Participant.objects.get(code="P-000777")
    url = reverse("specimens:participant-edit", args=[participant.pk])
    response = client.post(url, {"code": "P-000778", "expected_revision": 1, "is_active": "on"})
    assert response.status_code == 422  # a reason is required when editing
    response = client.post(
        url,
        {"code": "P-000778", "expected_revision": 1, "is_active": "on", "reason": "Typo"},
    )
    assert response.status_code == 302
    participant.refresh_from_db()
    assert participant.code == "P-000778"
    page = html(client.get(reverse("specimens:unit-edit", args=[world.ml.pk])))
    assert "In use: its code, dimension and fractional can no longer change." in page
    export = client.get(reverse("specimens:catalogue-export", args=["participant"]))
    assert export["Content-Type"].startswith("text/csv")


def test_dashboard_queue_counts_equal_list_counts(client, people, specimens):
    for who in ("approver", "technician", "viewer"):
        user = people[who]
        login(client, user)
        found = queues(user)
        assert found, who
        for queue in found:
            response = client.get(queue["url"])
            assert response.context["page_obj"].paginator.count == queue["count"], queue["label"]
    labels = {q["label"]: q["count"] for q in queues(people["approver"])}
    assert labels["Checked out"] == 1
    assert labels["Registered, not stored"] == 1
    assert labels["Disposal requests awaiting my approval"] == 1
    assert "Disposal requests awaiting my approval" not in {
        q["label"] for q in queues(people["technician"])
    }
    page = html(client.get(reverse("dashboard")))
    assert specimens["stored"].accession in page  # recently stored


def test_label_pdf_download(client, people, specimens):
    from .test_outputs import weasyprint_or_skip

    weasyprint_or_skip()
    login(client, people["viewer"])
    response = client.get(reverse("specimens:label-pdf"), {"id": specimens["stored"].pk})
    assert response.status_code == 200 and response.content.startswith(b"%PDF")
    assert response["Content-Disposition"].startswith('inline; filename="labels-')
    response = client.get(reverse("specimens:label-pdf"))
    assert response.url == reverse("specimens:label-preview")
