"""Explicit excursion holds retain provenance and survive review until a manager releases them."""

import pytest
from helpers import field, sign_in, sign_out
from playwright.sync_api import expect

from equipment import services
from equipment.tests import conftest as equipment_fixtures
from specimens.models import Hold, HoldRelease
from specimens.tests.conftest import PASSWORD, ago, register

pytestmark = pytest.mark.django_db(transaction=True)
people = equipment_fixtures.people
world = equipment_fixtures.world
tank = equipment_fixtures.tank


def test_selected_excursion_hold_is_reviewed_and_released_from_specimen(page, people, world, tank):
    tech = people["technician"]
    selected = register(tech, world, position=world.position("A1"))
    unselected = register(tech, world, position=world.position("A2"))
    original_position = selected.occupancy.position_id
    original_quantity = selected.quantity_on_hand
    excursion = services.open_excursion(
        actor=tech,
        equipment_id=tank.pk,
        started_at=ago(200),
        ended_at=ago(190),
        detection="reading",
        cause="Synthetic lid opening",
        action="Synthetic lid closure",
        impact="Synthetic assessment pending",
    )
    sign_in(page, tech.username, PASSWORD)
    page.goto(f"/equipment/excursions/{excursion.pk}/")
    excursion_url = page.url
    page.get_by_role("link", name="Place holds", exact=True).click()
    form = page.locator("#vr-form-dialog")
    expect(form).to_be_visible()
    field(form, "Specimen ID").fill("SYNTHETIC-NO-MATCH")
    form.get_by_role("button", name="Search", exact=True).click()
    expect(form).to_contain_text("No matching affected specimens")
    expect(form.get_by_role("button", name="Place holds", exact=True)).to_have_count(0)
    form.get_by_role("link", name="Clear search", exact=True).click()
    counter = form.locator('[data-vr-select-count="specimen_ids"]')
    expect(counter).to_have_text("0 of 2 selected")
    select_all = form.get_by_role("checkbox", name="Select all eligible specimens on this page")
    select_all.check()
    expect(counter).to_have_text("2 of 2 selected")
    select_all.uncheck()
    expect(counter).to_have_text("0 of 2 selected")
    expect(
        form.get_by_role("checkbox", name=f"Select {selected.accession}", exact=True)
    ).not_to_be_checked()
    expect(
        form.get_by_role("checkbox", name=f"Select {unselected.accession}", exact=True)
    ).not_to_be_checked()
    field(form, "Hold scope").select_option("use")
    field(form, "Reason").fill("Synthetic review hold; no physical quality finding.")
    with page.expect_response(
        lambda response: response.request.method == "POST" and response.url.endswith("/holds/")
    ) as rejected:
        form.get_by_role("button", name="Place holds", exact=True).click()
    assert rejected.value.status == 422
    expect(form).to_contain_text("Select at least one specimen")
    # Chromium reports the deliberately rejected submission as a failed resource.
    assert len(page.console_errors) == 1 and "status of 422" in page.console_errors[0]
    page.console_errors.clear()
    expect(field(form, "Hold scope")).to_have_value("use")
    expect(field(form, "Reason")).to_have_value(
        "Synthetic review hold; no physical quality finding."
    )
    assert not Hold.objects.exists()
    expect(counter).to_have_text("0 of 2 selected")

    form.get_by_role("checkbox", name=f"Select {selected.accession}", exact=True).check()
    expect(counter).to_have_text("1 of 2 selected")
    form.locator("[data-vr-documentation-toggle]").click()
    help_panel = page.locator("#vr-documentation")
    expect(help_panel).to_be_visible()
    expect(help_panel).to_contain_text("manual holds")
    help_panel.get_by_role("button", name="Close documentation", exact=True).click()
    form.get_by_role("button", name="Place holds", exact=True).click()
    expect(form).to_be_hidden()
    expect(page).to_have_url(excursion_url)
    hold = Hold.objects.get(specimen=selected)
    assert hold.reference == "" and hold.scope == "use"
    assert excursion.number in hold.reason
    assert not Hold.objects.filter(specimen=unselected).exists()
    expect(page.locator("#timeline-heading").locator("..").locator("..")).to_contain_text(
        selected.accession
    )

    # A normal excursion review does not release the explicit hold.
    page.get_by_role("link", name="Submit for review", exact=True).click()
    form.get_by_role("button", name="Submit for review", exact=True).click()
    expect(form).to_be_hidden()
    sign_out(page, who=r"^sp-")
    sign_in(page, people["manager"].username, PASSWORD)
    page.goto(excursion_url)
    page.get_by_role("link", name="Review excursion", exact=True).click()
    form.get_by_label("Close the excursion").check()
    field(form, "Review note").fill("Synthetic review complete; retain the manual hold.")
    form.get_by_role("button", name="Record review", exact=True).click()
    expect(form).to_be_hidden()
    excursion.refresh_from_db()
    assert excursion.status == "closed"
    assert not HoldRelease.objects.filter(hold=hold).exists()

    page.goto(f"/specimens/{selected.pk}/")
    expect(page.locator("main")).to_contain_text(hold.reason)
    page.get_by_role("link", name="Release hold", exact=True).click()
    expect(form).to_be_visible()
    field(form, "Reason").fill("Synthetic evaluation complete; release this hold.")
    form.get_by_role("button", name="Release hold", exact=True).click()
    expect(form).to_be_hidden()
    release = HoldRelease.objects.get(hold=hold)
    assert release.actor == people["manager"]
    selected.refresh_from_db()
    assert selected.occupancy.position_id == original_position
    assert selected.quantity_on_hand == original_quantity
    assert Hold.objects.filter(pk=hold.pk).exists()
    assert page.console_errors == []
