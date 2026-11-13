"""Equipment journey: a manager registers Tank VP-01 with its limits; a technician logs the LN2
level and a warm reading, the dashboard flags it, the technician records and submits an excursion;
a second manager closes it and the record lists the specimen that was in the tank."""

import re
from datetime import timedelta

import pytest
from helpers import field, local_input, make_user, sign_in, sign_out
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)

PASSWORD = "e2e-Equipment-pass-1"


def dialog(page: Page):
    found = page.locator("#vr-form-dialog")
    expect(found).to_be_visible()
    return found


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
