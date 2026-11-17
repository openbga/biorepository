"""Equipment journeys for reading assessments, the LN2 log and excursion review."""

import csv
import io
import re
from datetime import timedelta
from decimal import Decimal

import pytest
from helpers import field, local_input, make_user, save_download, sign_in, sign_out
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)

PASSWORD = "e2e-Equipment-pass-1"


def dialog(page: Page):
    found = page.locator("#vr-form-dialog")
    expect(found).to_be_visible()
    return found


def test_reading_assessments_match_the_register_filter_and_download(page: Page, tmp_path):
    from django.urls import reverse
    from django.utils import timezone

    from equipment import services

    manager = make_user("limits_manager", PASSWORD, "manager")
    tech = make_user("limits_tech", PASSWORD, "technician")
    freezer = services.save_equipment(
        actor=manager, code="FZ-01", name="Freezer 01", kind="freezer"
    )
    now = timezone.now()
    services.record_reading(
        actor=tech,
        equipment_id=freezer.pk,
        measured_at=now - timedelta(minutes=30),
        value=Decimal("-80.00"),
        probe_position="Before the limit change",
    )
    services.save_equipment(
        actor=manager,
        equipment_id=freezer.pk,
        expected_revision=freezer.revision,
        reason="Set temperature limits",
        lower_limit=Decimal("-90.00"),
        upper_limit=Decimal("-80.00"),
    )
    for minutes, probe, value in (
        (20, "At the new upper limit", "-80.00"),
        (15, "Above the new range", "-79.99"),
        (10, "Below the new range", "-90.01"),
    ):
        services.record_reading(
            actor=tech,
            equipment_id=freezer.pk,
            measured_at=now - timedelta(minutes=minutes),
            value=Decimal(value),
            probe_position=probe,
        )
    excursion = services.open_excursion(
        actor=tech,
        equipment_id=freezer.pk,
        started_at=now - timedelta(minutes=35),
        detection="reading",
    )

    sign_in(page, "limits_tech", PASSWORD)
    for url, section in (
        (reverse("equipment:equipment-detail", args=[freezer.pk]), "Temperature readings"),
        (
            reverse("equipment:excursion-detail", args=[excursion.pk]),
            "Readings during the excursion",
        ),
        (reverse("equipment:reading-list"), "Readings"),
    ):
        page.goto(url)
        table = page.get_by_role("region", name=section, exact=True).get_by_role("table")
        for probe, assessment in (
            ("Before the limit change", "No limits recorded"),
            ("At the new upper limit", "Within recorded limits"),
            ("Above the new range", "Above upper limit"),
            ("Below the new range", "Below lower limit"),
        ):
            row = table.get_by_role("row").filter(has_text=probe)
            expect(row.get_by_role("cell", name=assessment, exact=True)).to_be_visible()

    toolbar = page.get_by_role("search", name="Filter readings")
    for choice, assessment in (
        ("within", "Within recorded limits"),
        ("none", "No limits recorded"),
    ):
        toolbar.locator('[name="outside"]').select_option(choice)
        toolbar.get_by_role("button", name="Apply").click()
        rows = page.get_by_role("table", name="Readings", exact=True).locator("tbody tr")
        expect(rows).to_have_count(1)
        expect(rows.get_by_role("cell", name=assessment, exact=True)).to_be_visible()

    download = save_download(
        page, page.get_by_role("link", name="Export CSV").click, tmp_path / "readings.csv"
    )
    records = list(csv.DictReader(io.StringIO(download.decode())))
    assert [(row["Probe position"], row["Limit assessment"]) for row in records] == [
        ("Before the limit change", "No limits recorded")
    ]


def test_excursion_is_recorded_reviewed_by_a_second_person_and_lists_the_specimens(
    page: Page, transactional_db
):
    from django.contrib.auth.models import Permission

    from specimens.models import Specimen
    from specimens.seed import seed_specimens
    from storage.seed import seed_storage

    manager = make_user("equipment_manager", PASSWORD, "manager")
    manager.user_permissions.add(  # the specimen seed's disposal approval
        Permission.objects.get(content_type__app_label="accounts", codename="approve_disposal")
    )
    tech = make_user("equipment_tech", PASSWORD, "technician")
    make_user("equipment_reviewer", PASSWORD, "admin")
    seed_storage(actor=manager)
    seed_specimens(manager=manager, technician=tech)
    stored = Specimen.objects.filter(
        occupancy__position__box__node__unit__code="VP-01"
    ).values_list("accession", flat=True)
    assert stored

    # The manager registers the tank with the limits the repository chose.
    sign_in(page, "equipment_manager", PASSWORD)
    page.goto("/equipment/")
    page.get_by_role("link", name="New equipment", exact=True).first.click()
    form = dialog(page)
    field(form, "Asset ID").fill("TANK-VP-01")
    field(form, "Kind").select_option(label="LN2 storage tank")
    field(form, "Name").fill("Tank VP-01")
    field(form, "Storage unit").select_option(label="VP-01")
    field(form, "Upper limit").fill("-150.00")
    field(form, "Level unit").select_option(value="cm")
    form.get_by_role("button", name="Create equipment", exact=True).click()
    expect(page.get_by_role("heading", level=1)).to_have_text("TANK-VP-01")
    tank_url = page.url
    sign_out(page)

    # The technician logs a level check and a warm reading.
    sign_in(page, "equipment_tech", PASSWORD)
    page.goto(tank_url)
    page.get_by_role("link", name="Record LN2 entry").click()
    form = dialog(page)
    field(form, "Level read").fill("24.50")
    form.get_by_role("button", name="Record LN2 entry", exact=True).click()
    expect(page.get_by_role("heading", level=1)).to_have_text("LN2 log")
    expect(page.locator("main")).to_contain_text("24.50")
    page.goto(tank_url)
    page.get_by_role("link", name="Record reading").click()
    form = dialog(page)
    field(form, "Read at").fill(local_input(-timedelta(minutes=50)))
    field(form, "Temperature").fill("-141.30")
    field(form, "Probe position").fill("Top of rack")
    form.get_by_role("button", name="Record reading", exact=True).click()
    expect(page.locator("main")).to_contain_text("-141.30")

    page.goto("/")
    queue = page.get_by_role("link", name="Readings outside limits without an excursion")
    expect(queue).to_be_visible()
    queue.click()
    expect(page.get_by_text("-141.30")).to_be_visible()

    # ... and records the excursion, then submits it for review.
    page.goto(tank_url)
    page.get_by_role("link", name="New excursion").click()
    form = dialog(page)
    field(form, "Started").fill(local_input(-timedelta(minutes=55)))
    field(form, "Ended").fill(local_input(-timedelta(minutes=40)))
    field(form, "Extreme reached").fill("-141.30")
    field(form, "Detected by").select_option(label="Temperature reading")
    field(form, "Cause category").select_option(label="Door or lid left open")
    field(form, "Cause").fill("Lid left open during a retrieval.")
    field(form, "Action taken").fill("Lid closed; readings watched until back under the limit.")
    field(form, "Impact assessment").fill("Top shelf exposed; specimens listed for QC review.")
    form.get_by_role("button", name="Create excursion", exact=True).click()
    expect(page.get_by_role("heading", level=1)).to_have_text(re.compile(r"^EX-\d{6}$"))
    excursion_url = page.url
    page.get_by_role("link", name="Submit for review", exact=True).click()
    dialog(page).get_by_role("button", name="Submit for review", exact=True).click()
    expect(page.locator("main")).to_contain_text("Awaiting review")
    sign_out(page)

    # A second person (an administrator) reviews and closes it.
    sign_in(page, "equipment_reviewer", PASSWORD)
    page.goto(excursion_url)
    page.get_by_role("link", name="Review excursion", exact=True).click()
    form = dialog(page)
    form.get_by_label("Close the excursion").check()
    field(form, "Review note").fill("Reviewed against the readings; QC review requested.")
    form.get_by_role("button", name="Record review", exact=True).click()
    main = page.locator("main")
    expect(main).to_contain_text("Closed")
    expect(main).to_contain_text("Captured when it was closed")
    from equipment.models import AffectedSpecimen

    captured = set(AffectedSpecimen.objects.values_list("specimen__accession", flat=True))
    # In VP-01 during the excursion: includes one disposed since, excludes ones placed after it.
    assert captured and captured != set(stored)
    for accession in captured:
        expect(main.get_by_role("link", name=accession, exact=True)).to_be_visible()
