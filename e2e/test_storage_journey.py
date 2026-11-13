"""Storage journey: a manager builds tank, tower, shelves and box; a technician moves the box."""

import re
from datetime import timedelta

import pytest
from helpers import field, local_input, make_user, sign_in, sign_out
from playwright.sync_api import Page, expect

from storage import services
from storage.models import Box, Position

pytestmark = pytest.mark.django_db(transaction=True)

PASSWORD = "e2e-Storage-pass-1"


def fact(page: Page, term: str):
    return (
        page.locator("dl.vr-facts div")
        .filter(has=page.locator("dt", has_text=re.compile(rf"^{term}$")))
        .locator("dd")
    )


def test_manager_defines_tank_views_occupancy_and_technician_moves_a_box(
    page: Page, transactional_db
):
    from django.utils import timezone

    manager = make_user("storage_manager", PASSWORD, "manager")
    tech = make_user("storage_tech", PASSWORD, "technician")
    site = services.create_site(
        actor=manager, code="EXAMPLE", name="Example Site", timezone="America/New_York"
    )
    services.create_room(actor=manager, site_id=site.pk, code="CRYO", name="Cryo room")
    services.save_storage_catalogue(
        actor=manager,
        kind="class",
        object_id=None,
        expected_revision=None,
        values={"code": "CRYOGENIC", "name": "Cryogenic", "description": ""},
        reason="",
    )
    services.save_storage_catalogue(
        actor=manager,
        kind="container_type",
        object_id=None,
        expected_revision=None,
        values={
            "code": "GRID-9X9",
            "name": "Grid 9x9",
            "rows": 9,
            "columns": 9,
            "label_scheme": "alpha_numeric",
            "fill_order": "row_major",
        },
        reason="",
    )

    sign_in(page, "storage_manager", PASSWORD)
    page.goto("/storage/units/")
    page.get_by_role("link", name="New storage unit", exact=True).first.click()
    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()
    field(dialog, "Code").fill("VP-01")
    field(dialog, "Name").fill("Tank VP-01")
    field(dialog, "Room").select_option(index=1)
    field(dialog, "Storage class").select_option(label="Cryogenic")
    field(dialog, "Technology").select_option(label="LN2 vapor phase")
    field(dialog, "Nominal temperature (°C)").fill("-160.00")
    dialog.get_by_role("button", name="Create storage unit", exact=True).click()
    expect(page.get_by_role("heading", level=1)).to_have_text("VP-01")
    unit_url = page.url
    expect(fact(page, "Nominal temperature")).to_contain_text("-160.00")

    # Tower with 2 node slots at the top level.
    page.get_by_role("link", name="Add rack or shelf").click()
    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()
    field(dialog, "Kind").select_option(label="Tower")
    field(dialog, "Code").fill("T-01")
    field(dialog, "Name").fill("Tower T-01")
    field(dialog, "Slot").fill("1")
    field(dialog, "Node slots").fill("2")
    field(dialog, "Box slots").fill("0")
    dialog.get_by_role("button", name="Create rack or shelf", exact=True).click()
    expect(page.get_by_role("heading", level=1)).to_have_text("T-01")
    tower_url = page.url

    for slot in (1, 2):
        page.get_by_role("link", name="Add shelf").click()
        dialog = page.locator("#vr-form-dialog")
        expect(dialog).to_be_visible()
        field(dialog, "Kind").select_option(label="Shelf")
        field(dialog, "Code").fill(f"T-01-S{slot}")
        field(dialog, "Name").fill(f"Shelf {slot}")
        field(dialog, "Slot").fill(str(slot))
        field(dialog, "Node slots").fill("0")
        field(dialog, "Box slots").fill("2")
        dialog.get_by_role("button", name="Create rack or shelf", exact=True).click()
        expect(page.get_by_role("heading", level=1)).to_have_text(f"T-01-S{slot}")
        page.goto(tower_url)
    expect(page.get_by_role("link", name="T-01-S2", exact=True)).to_be_visible()

    # Box on shelf 1.
    page.get_by_role("link", name="T-01-S1", exact=True).click()
    page.get_by_role("link", name="Add box").click()
    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()
    field(dialog, "Box slot").fill("1")
    field(dialog, "Container type").select_option(index=1)
    field(dialog, "Code").fill("BOX-001")
    dialog.get_by_role("button", name="Create box", exact=True).click()
    expect(page.get_by_role("heading", level=1)).to_have_text("BOX-001")
    box_url = page.url
    expect(fact(page, "Positions")).to_have_text("81")
    expect(page.locator("table caption")).to_contain_text("Row by column grid of BOX-001")
    expect(page.locator("td[id^=position-]")).to_have_count(81)
    assert page.console_errors == []

    # Two items placed from Python (specimens will own this).
    box = Box.objects.get(code="BOX-001")
    now = timezone.now()
    for label in ("A1", "A2"):
        position = Position.objects.get(box=box, label=label)
        with services.operation():
            services.lock_gate()
            occ = services.new_occupancy(actor=tech, occurred_at=now - timedelta(hours=1))
            services.change_occupancy(
                actor=tech,
                occupancy_id=occ.pk,
                expected_revision=occ.revision,
                position_id=position.pk,
                kind="place",
                occurred_at=now - timedelta(minutes=50),
                reason="Stored after processing",
            )
    page.goto(box_url)
    expect(page.locator("td[id^=position-]").filter(has_text="Occupied")).to_have_count(2)
    expect(fact(page, "Occupied")).to_have_text("2")
    expect(fact(page, "Free")).to_have_text("79")
    expect(page.get_by_role("heading", name="Occupied positions")).to_be_visible()

    page.goto(unit_url)
    expect(fact(page, "Occupied")).to_have_text("2")
    expect(fact(page, "Free")).to_have_text("79")
    page.goto("/storage/")
    row = page.locator("table.vr-table tbody tr").filter(has_text="VP-01")
    expect(row).to_contain_text("81")
    expect(row.get_by_role("link", name="79")).to_be_visible()
    expect(fact(page, "Free")).to_have_text("79")
    page.goto("/storage/free-positions/?q=BOX-001")
    expect(page.locator("table.vr-table tbody tr")).to_have_count(25)  # first page
    expect(page.locator("body")).to_contain_text("79")
    sign_out(page, r"^E2E ")

    # Technician moves the box to shelf 2.
    sign_in(page, "storage_tech", PASSWORD)
    page.goto(box_url)
    expect(page.get_by_role("link", name="Retire box")).to_have_count(0)
    page.get_by_role("link", name="Move box", exact=True).click()
    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()
    target = field(dialog, "Move to")
    option = target.locator("option", has_text="T-01-S2").first
    target.select_option(value=option.get_attribute("value"))
    field(dialog, "Slot").fill("1")
    field(dialog, "Moved at").fill(local_input(timedelta(minutes=-5)))
    field(dialog, "Reason for the move").fill("Consolidating shelves")
    dialog.get_by_role("button", name="Move box", exact=True).click()
    expect(page.get_by_role("heading", level=1)).to_have_text("BOX-001")
    expect(page.get_by_role("status").or_(page.get_by_role("alert")).first).to_contain_text(
        "Moved BOX-001"
    )
    expect(fact(page, "Location")).to_contain_text("T-01-S2")
    moves = page.locator("section[aria-labelledby=moves-heading] tbody tr")
    expect(moves).to_have_count(1)
    expect(moves.first).to_contain_text("Consolidating shelves")
    expect(moves.first.locator("td").nth(2)).to_have_text("2")
    expect(fact(page, "Occupied")).to_have_text("2")
    assert page.console_errors == []
