"""Equipment pages: each page renders for the roles it is meant for and refuses the others, the
forms post through the services, attachments download safely, and the readings CSV is exact."""

import csv
import io
import re
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from core.models import AuditEvent
from equipment import services
from equipment.models import (
    Equipment,
    Excursion,
    LN2Entry,
    ReadingImport,
    ServiceEvent,
    TemperatureReading,
)

from .conftest import ago, csv_file, place, register

pytestmark = pytest.mark.django_db
PDF = b"%PDF-1.4\n% synthetic test certificate\n%%EOF\n"


@pytest.fixture(autouse=True)
def plain_static_storage(settings):
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


def when(moment):
    return timezone.localtime(moment).strftime("%Y-%m-%dT%H:%M:%S")


def login(client, user):
    client.force_login(user)
    return client


@pytest.fixture
def records(people, tank, monitor, dewar):
    """One of each record, recorded by the technician."""
    tech = people["technician"]
    service = services.record_service_event(
        actor=tech,
        equipment_id=tank.pk,
        kind="calibration",
        performed_on=timezone.localdate(),
        performed_by=tech,
        outcome="acceptable",
        next_due_on=timezone.localdate(),
        attachment=SimpleUploadedFile("cert.pdf", PDF, "application/pdf"),
    )
    ln2 = services.record_ln2(
        actor=tech, equipment_id=tank.pk, kind="level", occurred_at=ago(60), level=Decimal("30")
    )
    reading = services.record_reading(
        actor=tech, equipment_id=tank.pk, measured_at=ago(50), value=Decimal("-140.25")
    )
    batch = services.validate_reading_import(
        actor=tech,
        file=csv_file(f"measured_at,temperature_c\n{when(ago(40))},-190.00\n"),
        equipment_id=tank.pk,
    )
    excursion = services.open_excursion(
        actor=tech, equipment_id=tank.pk, started_at=ago(55), detection="reading"
    )
    return {
        "service": service,
        "ln2": ln2,
        "reading": reading,
        "batch": batch,
        "excursion": excursion,
    }


def read_pages(tank, records):
    return [
        reverse("equipment:equipment-list"),
        reverse("equipment:equipment-detail", args=[tank.pk]),
        reverse("equipment:due-list"),
        reverse("equipment:ln2-list"),
        reverse("equipment:reading-list"),
        reverse("equipment:import-list"),
        reverse("equipment:import-detail", args=[records["batch"].pk]),
        reverse("equipment:excursion-list"),
        reverse("equipment:excursion-detail", args=[records["excursion"].pk]),
    ]


def test_read_pages_render_for_a_viewer_and_refuse_someone_without_a_role(
    client, people, tank, records
):
    login(client, people["viewer"])
    for url in read_pages(tank, records):
        response = client.get(url)
        assert response.status_code == 200, url
        service = reverse("equipment:service-create", args=[tank.pk]).encode()
        assert b'href="' + service not in response.content, url
    login(client, people["nobody"])
    for url in read_pages(tank, records):
        assert client.get(url).status_code == 403, url


def test_operator_forms_render_for_a_technician_and_refuse_a_viewer(client, people, tank, records):
    excursion = records["excursion"]
    urls = [
        reverse("equipment:service-create", args=[tank.pk]) + "?kind=maintenance",
        reverse("equipment:service-correct", args=[records["service"].pk]),
        reverse("equipment:ln2-create") + f"?equipment={tank.pk}&kind=fill",
        reverse("equipment:ln2-correct", args=[records["ln2"].pk]),
        reverse("equipment:reading-create") + f"?equipment={tank.pk}",
        reverse("equipment:reading-correct", args=[records["reading"].pk]),
        reverse("equipment:import-upload"),
        reverse("equipment:excursion-create") + f"?equipment={tank.pk}",
        reverse("equipment:excursion-edit", args=[excursion.pk]),
        reverse("equipment:excursion-submit", args=[excursion.pk]),
        reverse("equipment:excursion-note", args=[excursion.pk]),
    ]
    login(client, people["technician"])
    for url in urls:
        assert client.get(url).status_code == 200, url
    login(client, people["viewer"])
    for url in urls:
        assert client.get(url).status_code == 403, url


def test_manager_forms_refuse_a_technician(client, people, tank, records):
    excursion = records["excursion"]
    urls = [
        reverse("equipment:equipment-create"),
        reverse("equipment:equipment-edit", args=[tank.pk]),
        reverse("equipment:status-change", args=[tank.pk]),
        reverse("equipment:excursion-review", args=[excursion.pk]),
        reverse("equipment:excursion-reopen", args=[excursion.pk]),
    ]
    login(client, people["manager"])
    for url in urls:
        assert client.get(url).status_code == 200, url
    login(client, people["technician"])
    for url in urls:
        assert client.get(url).status_code == 403, url
    page = client.get(reverse("equipment:equipment-detail", args=[tank.pk])).content.decode()
    service = reverse("equipment:service-create", args=[tank.pk])
    status = reverse("equipment:status-change", args=[tank.pk])
    assert f'href="{service}' in page and f'href="{status}' not in page


def test_manager_registers_and_edits_equipment_with_a_reason(client, people, world):
    login(client, people["manager"])
    response = client.post(
        reverse("equipment:equipment-create"),
        {"code": "frz-02", "name": "Freezer 2", "kind": "freezer", "upper_limit": "-70.00"},
    )
    equipment = Equipment.objects.get(code="FRZ-02")
    assert response.status_code == 302
    assert response["Location"] == reverse("equipment:equipment-detail", args=[equipment.pk])
    assert equipment.upper_limit == Decimal("-70.00") and equipment.lower_limit is None

    edit = reverse("equipment:equipment-edit", args=[equipment.pk])
    values = {"code": "FRZ-02", "name": "Freezer 2", "kind": "freezer", "upper_limit": "-75.50"}
    missing = client.post(edit, {**values, "expected_revision": 1})
    assert missing.status_code == 422 and "reason" in missing.context["form"].errors
    saved = client.post(edit, {**values, "expected_revision": 1, "reason": "Limit revised"})
    assert saved.status_code == 302
    equipment.refresh_from_db()
    assert equipment.upper_limit == Decimal("-75.50") and equipment.revision == 2
    stale = client.post(
        edit, {**values, "name": "Freezer B", "expected_revision": 1, "reason": "x"}
    )
    assert stale.status_code == 422
    assert "changed since you opened it" in stale.content.decode()
    assert 'name="expected_revision" value="2"' in stale.content.decode()


def test_status_change_records_the_effective_time_and_reason(client, people, tank):
    login(client, people["manager"])
    response = client.post(
        reverse("equipment:status-change", args=[tank.pk]),
        {
            "status": "out_of_service",
            "occurred_at": when(ago(5)),
            "reason": "Lid seal replaced",
            "expected_revision": tank.revision,
        },
    )
    assert response.status_code == 302
    tank.refresh_from_db()
    assert tank.status == "out_of_service"
    page = client.get(reverse("equipment:equipment-detail", args=[tank.pk])).content.decode()
    assert "Lid seal replaced" in page


def test_technician_records_ln2_entries_and_the_log_shows_the_change(client, people, tank, dewar):
    login(client, people["technician"])
    create = reverse("equipment:ln2-create")
    level = client.post(
        create + "?kind=level",
        {"equipment": tank.pk, "kind": "level", "occurred_at": when(ago(30)), "level": "31.50"},
    )
    assert level.status_code == 302
    assert level["Location"] == reverse("equipment:ln2-list") + f"?equipment={tank.pk}"
    fill = client.post(
        create,
        {
            "equipment": tank.pk,
            "kind": "fill",
            "occurred_at": when(ago(20)),
            "level_before": "18.00",
            "level": "42.00",
            "amount_added": "120",
            "amount_unit": "L",
            "supply": dewar.pk,
        },
    )
    assert fill.status_code == 302
    assert LN2Entry.objects.get(kind="fill").level_unit == "cm"  # the vessel's unit
    page = client.get(reverse("equipment:ln2-list") + "?kind=fill").content.decode()
    assert "-13.50 cm" in page and "DEWAR-01" in page
    # A filter on the kind of entry does not change the change since the previous entry.
    level_form = client.get(create + f"?equipment={tank.pk}").content.decode()
    assert 'name="amount_added"' not in level_form and 'name="level"' in level_form


def test_readings_are_recorded_and_corrected_by_their_recorder_or_a_manager(client, people, tank):
    login(client, people["technician"])
    response = client.post(
        reverse("equipment:reading-create"),
        {"equipment": tank.pk, "measured_at": when(ago(10)), "value": "-140.25"},
    )
    assert response.status_code == 302
    reading = TemperatureReading.objects.get()
    assert reading.upper_limit == Decimal("-150.00")
    uncovered = client.get(reverse("equipment:reading-list") + "?outside=uncovered")
    assert uncovered.context["page_obj"].paginator.count == 1

    correct = reverse("equipment:reading-correct", args=[reading.pk])
    login(client, people["technician2"])
    assert client.get(correct).status_code == 403
    login(client, people["technician"])
    done = client.post(
        correct,
        {"measured_at": when(reading.measured_at), "value": "-185.25", "reason": "Misread"},
    )
    assert done.status_code == 302
    assert TemperatureReading.objects.get(corrects=reading).value == Decimal("-185.25")
    again = client.get(correct)
    assert "already corrected" in again.content.decode()


def test_service_event_attachment_downloads_as_an_attachment(client, people, tank):
    login(client, people["technician"])
    response = client.post(
        reverse("equipment:service-create", args=[tank.pk]),
        {
            "kind": "maintenance",
            "performed_on": timezone.localdate().isoformat(),
            "outcome": "completed",
            "performed_by_external": "Example Cryo Service",
            "attachment": SimpleUploadedFile("report.pdf", PDF, "application/pdf"),
        },
    )
    assert response.status_code == 302
    event = ServiceEvent.objects.get()
    assert event.attachment.name == "report.pdf"
    page = client.get(reverse("equipment:equipment-detail", args=[tank.pk])).content.decode()
    url = reverse("equipment:attachment", args=["service", event.pk])
    assert url in page
    login(client, people["viewer"])
    download = client.get(url)
    assert download.status_code == 200 and download.content == PDF
    assert download["Content-Disposition"].startswith("attachment;")
    assert download["X-Content-Type-Options"] == "nosniff"
    assert client.get(reverse("equipment:attachment", args=["other", event.pk])).status_code == 404


def test_a_bad_attachment_is_refused_under_its_field(client, people, tank):
    login(client, people["technician"])
    response = client.post(
        reverse("equipment:service-create", args=[tank.pk]),
        {
            "kind": "maintenance",
            "performed_on": timezone.localdate().isoformat(),
            "outcome": "completed",
            "performed_by_external": "Example Cryo Service",
            "attachment": SimpleUploadedFile("report.pdf", b"not a pdf", "application/pdf"),
        },
    )
    assert response.status_code == 422
    assert "attachment" in response.context["form"].errors
    assert not ServiceEvent.objects.exists()


def test_readings_import_upload_check_and_commit(client, people, tank):
    login(client, people["technician"])
    text = (
        "equipment,measured_at,temperature_c,probe_position\n"
        f"TANK-VP-01,{when(ago(30))},-185.10,Top of rack\n"
        f"TANK-VP-01,{when(ago(20))},-184.90,Top of rack\n"
    )
    upload = client.post(
        reverse("equipment:import-upload"),
        {"file": SimpleUploadedFile("logger.csv", text.encode(), "text/csv")},
    )
    batch = ReadingImport.objects.get()
    detail = reverse("equipment:import-detail", args=[batch.pk])
    assert upload.status_code == 302 and upload["Location"] == detail
    page = client.get(detail)
    assert page.context["state"] == "validated" and "-185.10" in page.content.decode()
    missing = client.post(detail, {"reason": ""})
    assert missing.status_code == 422
    login(client, people["viewer"])
    assert client.post(detail, {"reason": "Logger download"}).status_code == 403
    login(client, people["technician"])
    assert client.post(detail, {"reason": "Logger download"}).status_code == 302
    assert TemperatureReading.objects.filter(batch=batch).count() == 2
    assert client.get(detail).context["state"] == "committed"
    imports = client.get(reverse("equipment:import-list") + "?state=committed")
    assert imports.context["page_obj"].paginator.count == 1


def test_excursion_is_opened_submitted_and_closed_by_another_manager(client, people, world, tank):
    specimen = register(people["technician"], world)
    place(people["technician"], specimen, world.position("A1"))
    login(client, people["technician"])
    create = client.post(
        reverse("equipment:excursion-create"),
        {
            "equipment": tank.pk,
            "started_at": when(ago(120)),
            "detection": "alarm",
            "cause_category": "unknown",
            "note": "Alarm at night",
        },
    )
    excursion = Excursion.objects.get()
    detail = reverse("equipment:excursion-detail", args=[excursion.pk])
    assert create.status_code == 302 and create["Location"] == detail
    page = client.get(detail).content.decode()
    assert specimen.accession in page and "Computed from the recorded movements" in page

    submit = reverse("equipment:excursion-submit", args=[excursion.pk])
    incomplete = client.post(submit, {"expected_revision": 1})
    assert incomplete.status_code == 422 and "before submitting" in incomplete.content.decode()
    edit = client.post(
        reverse("equipment:excursion-edit", args=[excursion.pk]),
        {
            "started_at": when(ago(120)),
            "ended_at": when(ago(90)),
            "detection": "alarm",
            "cause_category": "ln2_supply",
            "cause": "Supply valve closed",
            "action": "Valve opened, tank refilled",
            "impact": "Vapor stayed below -150 °C at the rack tops",
            "expected_revision": 1,
        },
    )
    assert edit.status_code == 302
    assert client.post(submit, {"expected_revision": 2}).status_code == 302
    review = reverse("equipment:excursion-review", args=[excursion.pk])
    assert client.get(review).status_code == 403  # technicians do not review

    # A manager who submitted an excursion is offered no close and cannot post one.
    manager, admin = people["manager"], people["admin"]
    excursion.refresh_from_db()
    services.return_excursion(
        actor=manager, excursion_id=excursion.pk, expected_revision=3, note="Add the readings"
    )
    services.submit_excursion(actor=manager, excursion_id=excursion.pk, expected_revision=4)
    login(client, manager)
    page = client.get(detail).content.decode()
    assert "you can return it for more work" in page
    form = client.get(review).content.decode()
    assert 'value="close"' not in form
    refused = client.post(review, {"decision": "close", "note": "Fine", "expected_revision": 5})
    assert refused.status_code == 422
    assert Excursion.objects.get().status == "review"

    login(client, admin)
    closed = client.post(review, {"decision": "close", "note": "Reviewed", "expected_revision": 5})
    assert closed.status_code == 302
    excursion.refresh_from_db()
    assert excursion.status == "closed" and excursion.closed_by == admin
    page = client.get(detail).content.decode()
    assert "Captured when it was closed" in page and specimen.accession in page
    by_specimen = client.get(
        reverse("equipment:excursion-list") + f"?specimen={specimen.accession.lower()}"
    )
    assert list(by_specimen.context["page_obj"]) == [excursion]


def test_list_filters_match_the_dashboard_queues(client, people, tank, records):
    login(client, people["manager"])
    dashboard = client.get(reverse("dashboard")).content.decode()
    for queue in ("Open temperature excursions", "Readings outside limits without an excursion"):
        assert queue in dashboard
    excursions = client.get(reverse("equipment:excursion-list") + "?status=active")
    assert excursions.context["page_obj"].paginator.count == 1
    due = client.get(reverse("equipment:due-list") + "?state=upcoming&within=7")
    assert due.context["page_obj"].paginator.count == 1
    services.record_ln2(
        actor=people["technician"],
        equipment_id=tank.pk,
        kind="level",
        occurred_at=ago(5),
        level=Decimal("12.00"),
    )
    low = client.get(reverse("equipment:equipment-list") + "?low_level=on")
    assert [row.code for row in low.context["page_obj"]] == ["TANK-VP-01"]
    bad = client.get(reverse("equipment:reading-list") + "?date_from=2026-11-12&date_to=2026-11-01")
    assert bad.status_code == 200 and bad.context["page_obj"].paginator.count == 0


def test_readings_csv_is_exact_filtered_and_audited(client, people, tank, monitor):
    tech = people["technician"]
    services.record_reading(
        actor=tech,
        equipment_id=tank.pk,
        measured_at=ago(30),
        value=Decimal("-185.10"),
        probe_position="=cmd",
        device=monitor,
    )
    services.record_reading(
        actor=tech, equipment_id=tank.pk, measured_at=ago(20), value=Decimal("-140.25")
    )
    login(client, people["viewer"])
    url = reverse("equipment:export", args=["reading"]) + "?outside=any"
    response = client.get(url)
    assert response.status_code == 200
    assert response["Content-Disposition"] == 'attachment; filename="equipment-reading.csv"'
    rows = list(csv.reader(io.StringIO(response.content.decode())))
    assert rows[0][:3] == ["Measured at", "Asset ID", "Temperature (°C)"]
    assert [row[2] for row in rows[1:]] == ["-140.25"]
    everything = list(
        csv.reader(
            io.StringIO(client.get(reverse("equipment:export", args=["reading"])).content.decode())
        )
    )
    assert "-185.10" in [row[2] for row in everything] and "'=cmd" in [row[3] for row in everything]
    event = AuditEvent.objects.filter(action="equipment.exported").first()
    assert event.after["rows"] == 1 and event.after["filters"] == {"outside": "any"}
    assert client.get(reverse("equipment:export", args=["reading"]) + "?bogus=1").status_code == 400
    assert client.get(reverse("equipment:export", args=["nothing"])).status_code == 404


def test_list_pages_link_rows_by_their_identifier(client, people, tank, records):
    login(client, people["viewer"])
    page = client.get(reverse("equipment:equipment-list")).content.decode()
    link = reverse("equipment:equipment-detail", args=[tank.pk])
    assert re.search(rf'<th scope="row"><a class="vr-row-link vr-id" href="{link}">', page)
    excursions = client.get(reverse("equipment:excursion-list")).content.decode()
    assert records["excursion"].number in excursions
    readings = client.get(reverse("equipment:reading-list")).content.decode()
    correct = reverse("equipment:reading-correct", args=[records["reading"].pk])
    assert f'href="{correct}' not in readings  # no correction link for a viewer
