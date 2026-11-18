"""The product's first journey in the browser (docs/product.md), from registration to disposal.

Storage is defined through the storage services (its own journey covers those pages). A
technician registers a specimen, prints its label, stores it, finds it from the top bar, checks
it out, splits two stored aliquots, checks it back in with an observed thaw; the box then moves
with its contents; the technician requests disposal and another person holding the disposal
approval permission approves and records it. The record keeps every step in its history.
"""

import csv
import io
import re
from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth.models import Permission
from helpers import (
    field,
    local_input,
    make_user,
    nav,
    pdf_text,
    pick,
    save_download,
    sign_in,
    sign_out,
)
from playwright.sync_api import Page, expect

from specimens import importing
from specimens import services as specimen_services
from specimens.models import Specimen
from storage import services
from storage.models import Box, StorageGate

pytestmark = pytest.mark.django_db(transaction=True)

PASSWORD = "e2e-Specimens-pass-1"


def build_world(manager):
    site = services.create_site(
        actor=manager, code="EXAMPLE", name="Example site", timezone="America/New_York"
    )
    room = services.create_room(actor=manager, site_id=site.pk, code="CRYO", name="Cryo room")
    cls = services.save_storage_catalogue(
        actor=manager,
        kind="class",
        object_id=None,
        expected_revision=None,
        values={"code": "CRYOGENIC", "name": "Cryogenic", "description": ""},
        reason="",
    )
    unit = services.create_unit(
        actor=manager,
        room_id=room.pk,
        code="VP-01",
        name="Tank VP-01",
        storage_class_id=cls.pk,
        technology="ln2_vapor",
        nominal_temperature=Decimal("-160.00"),
    )
    shelves = []
    for number in (1, 2):
        tower = services.create_node(
            actor=manager,
            unit_id=unit.pk,
            parent_id=None,
            code=f"T-0{number}",
            name=f"Tower {number}",
            kind="tower",
            slot=number,
            child_slots=1,
            box_slots=0,
        )
        shelves.append(
            services.create_node(
                actor=manager,
                unit_id=unit.pk,
                parent_id=tower.pk,
                code=f"T-0{number}-S1",
                name="Shelf 1",
                kind="shelf",
                slot=1,
                child_slots=0,
                box_slots=1,
            )
        )
    grid = services.save_storage_catalogue(
        actor=manager,
        kind="container_type",
        object_id=None,
        expected_revision=None,
        values={
            "code": "GRID-9X9",
            "name": "9 x 9",
            "rows": 9,
            "columns": 9,
            "label_scheme": "alpha_numeric",
            "fill_order": "row_major",
        },
        reason="",
    )
    box = services.create_box(
        actor=manager,
        node_id=shelves[0].pk,
        code="BOX-001",
        name="Box 1",
        slot=1,
        container_type_id=grid.pk,
    )

    def catalogue(kind, **values):
        return specimen_services.save_specimen_catalogue(
            actor=manager,
            kind=kind,
            object_id=None,
            expected_revision=None,
            values=values,
            reason="",
        )

    ml = catalogue("unit", code="mL", name="millilitre", dimension="volume", fractional=True)
    catalogue("specimen_type", code="PLASMA", name="Plasma")
    catalogue(
        "container_type",
        code="CRYOVIAL-2ML",
        name="Cryovial 2 mL",
        nominal_capacity="2",
        capacity_unit=ml.pk,
    )
    catalogue("participant", code="P-000123")
    catalogue("collection", code="DEMO", name="Demonstration collection")
    return box, shelves[1]


def dialog(page: Page):
    form = page.locator("#vr-form-dialog")
    expect(form).to_be_visible()
    return form


def submit(scope, name):
    """Submit a dialog form and wait until it closes (an error keeps it open: show why)."""
    scope.get_by_role("button", name=name, exact=True).click()
    try:
        expect(scope).to_be_hidden()
    except AssertionError:
        raise AssertionError(f"The {name!r} dialog stayed open:\n{scope.inner_text()}") from None


def choose_position(scope, box, label, index=None):
    boxes = (
        scope.get_by_label(re.compile(r"^Box\s*\*?$"))
        if index is None
        else scope.locator(f"select[name='children-{index}-box']")
    )
    pick(boxes, value=str(box.pk))
    labels = (
        scope.get_by_label(re.compile(r"^Position\s*\*?$"))
        if index is None
        else (scope.locator(f"input[name='children-{index}-position_label']"))
    )
    labels.fill(label)


def record_action(page: Page, name: str):
    """A record action: a header button, or an item of the header's More menu."""
    header = page.locator(".vr-page-actions")
    direct = header.get_by_role("link", name=name, exact=True)
    if direct.count():
        direct.first.click()
        return
    header.get_by_role("button", name="More").click()
    header.get_by_role("link", name=name, exact=True).click()


def test_first_journey_from_registration_to_disposal(page: Page, tmp_path, transactional_db):
    manager = make_user("specimens_manager", PASSWORD, "manager")
    tech = make_user("specimens_tech", PASSWORD, "technician")
    approver = make_user("specimens_approver", PASSWORD, "technician")
    approver.user_permissions.add(
        Permission.objects.get(content_type__app_label="accounts", codename="approve_disposal")
    )
    box, other_shelf = build_world(manager)

    # Register.
    sign_in(page, tech.username, PASSWORD)
    nav(page, "Specimens")
    expect(page.get_by_role("heading", level=1, name="Specimens")).to_be_visible()
    page.locator(".vr-page-actions").get_by_role("link", name="Register specimen").click()
    form = dialog(page)
    pick(field(form, "Specimen type"), label="PLASMA · Plasma")
    pick(field(form, "Specimen container"), label="CRYOVIAL-2ML · Cryovial 2 mL")
    field(form, "Amount").fill("2.000000")
    pick(field(form, "Unit"), label="mL")
    pick(field(form, "Participant"), label="P-000123")
    pick(field(form, "Collection"), label="DEMO")
    field(form, "Collected").fill(local_input(-timedelta(hours=3)))
    submit(form, "Register specimen")
    heading = page.get_by_role("heading", level=1)
    expect(heading).to_have_text(re.compile(r"^VP-\d{6,}$"))
    accession = heading.inner_text().strip()
    specimen = Specimen.objects.get(accession=accession)
    assert specimen.quantity_on_hand == Decimal("2") and specimen.participant.code == "P-000123"

    # Label: preview, then the PDF carries the ID.
    record_action(page, "Print label")
    expect(page.get_by_role("heading", level=1)).to_contain_text("label", ignore_case=True)
    href = page.get_by_role("link", name="Download PDF").get_attribute("href")
    response = page.request.get(href)
    assert response.ok and response.body().startswith(b"%PDF")
    target = tmp_path / "labels.pdf"
    target.write_bytes(response.body())
    assert accession in pdf_text(target)
    page.go_back()

    # Store it, then find it by scanning (typing) the ID in the top bar.
    record_action(page, "Place in storage")
    form = dialog(page)
    choose_position(form, box, "A1")
    submit(form, "Place in storage")
    expect(page.locator(".alert-success")).to_contain_text(accession)
    page.goto("/")
    page.locator("#vr-lookup-q").fill(f"  {accession.lower()} ")
    page.locator("#vr-lookup-q").press("Enter")
    expect(page.get_by_role("heading", level=1)).to_have_text(accession)

    # Check out (time out of storage starts), split two stored aliquots, check back in.
    record_action(page, "Record check-out")
    form = dialog(page)
    field(form, "Destination").fill("Processing bench 2")
    field(form, "Reason").fill("Aliquoting")
    submit(form, "Record check-out")
    expect(page.locator(".vr-page-meta .is-warm")).to_contain_text("Out of storage")
    expect(page.locator('.vr-page-meta .vr-clock[data-state="running"]')).to_be_visible()

    record_action(page, "Make aliquots or derivatives")
    form = dialog(page)
    field(form, "Taken from the parent").fill("1")
    form.locator("input[name='children-0-quantity']").fill("0.5")
    form.locator("input[name='children-1-quantity']").fill("0.5")
    choose_position(form, box, "B1", index=0)
    choose_position(form, box, "B2", index=1)
    field(form, "Reason").fill("Aliquoting")
    submit(form, "Record split")
    expect(page.get_by_role("heading", level=1)).to_have_text(accession)
    children = list(specimen.children.order_by("accession"))
    assert len(children) == 2 and all(c.occupancy.position_id for c in children)

    record_action(page, "Record check-in")
    form = dialog(page)
    choose_position(form, box, "A1")
    form.get_by_label("Yes, a thaw was observed").check()
    field(form, "Reason").fill("Returned after aliquoting")
    submit(form, "Record check-in")
    facts = page.locator("dl.vr-facts")
    expect(facts).to_contain_text("1 mL")
    specimen.refresh_from_db()
    assert specimen.open_checkout_id is None

    # The box moves to the other tower with its three specimens in one event.
    box.refresh_from_db()
    from django.utils import timezone

    services.move_container(
        actor=tech,
        kind="box",
        object_id=box.pk,
        expected_revision=box.revision,
        expected_gate_revision=StorageGate.objects.get(pk=1).revision,
        target_kind="node",
        target_id=other_shelf.pk,
        target_slot=1,
        occurred_at=timezone.now(),
        reason="Consolidating towers",
    )
    page.reload()
    expect(facts).to_contain_text("T-02")

    # Disposal: requested by the technician, approved and recorded by another person.
    record_action(page, "Request disposal")
    form = dialog(page)
    field(form, "Method").fill("Autoclave, then clinical waste")
    field(form, "Destination").fill("Biohazard waste stream")
    field(form, "Reason").fill("Study closed (synthetic)")
    submit(form, "Request disposal")
    sign_out(page, r"specimens_tech")

    sign_in(page, approver.username, PASSWORD)
    queues = page.get_by_role("region", name="Your work queues")
    queues.get_by_role("link", name=re.compile("awaiting my approval", re.I)).click()
    export = page.get_by_role("link", name="Export CSV", exact=True)
    expect(page.get_by_label("Awaiting my approval", exact=True)).to_be_checked()
    data = save_download(page, export.click, tmp_path / "disposal-requests.csv")
    rows = list(csv.DictReader(io.StringIO(data.decode(), newline="")))
    assert [(row["Specimen"], row["State"], row["Decided by"]) for row in rows] == [
        (accession, "Awaiting approval", "")
    ]
    page.get_by_role("link", name=re.compile(r"^Request \d{4}-")).first.click()
    page.locator(".vr-page-actions").get_by_role("link", name="Approve disposal").click()
    form = dialog(page)
    field(form, "Reason").fill("Checked against the closure notice")
    submit(form, "Approve disposal")
    page.locator(".vr-page-actions").get_by_role("link", name="Record disposal").click()
    form = dialog(page)
    field(form, "Reason").fill("Autoclaved and discarded")
    submit(form, "Record disposal")

    page.goto(f"/specimens/{specimen.pk}/")
    expect(page.locator(".vr-page-meta")).to_contain_text("Disposed")
    history = page.locator(".vr-timeline").last
    for step in (
        "Registered",
        "Placed in storage",
        "Checked out",
        "Aliquots made",
        "Checked in",
        "Container moved",
        "Disposal requested",
        "Disposal approved",
        "Disposed",
    ):
        expect(history).to_contain_text(step)
    specimen.refresh_from_db()
    assert specimen.status == "disposed" and specimen.occupancy.position_id is None
    assert specimen.disposal.request.requested_by == tech
    assert specimen.disposal.decision.actor == approver
    assert Box.objects.get(pk=box.pk).node_id == other_shelf.pk


def test_download_filtered_specimen_import_register(page: Page, tmp_path):
    tech = make_user("specimen_import_tech", PASSWORD, "technician")
    importing.validate_import(actor=tech, filename="Synthetic Monday.csv", csv_bytes=b"")
    sign_in(page, tech.username, PASSWORD)
    page.goto("/specimens/imports/")
    page.get_by_role("link", name="Upload CSV", exact=True).first.click()
    form = dialog(page)
    filename = "Synthetic Tuesday.csv"
    field(form, "CSV file").set_input_files(
        {
            "name": filename,
            "mimeType": "text/csv",
            "buffer": b"unrecognized_header\nSYNTHETIC-ROW\n",
        }
    )
    form.get_by_role("button", name="Validate file", exact=True).click()
    expect(page.get_by_role("heading", level=1)).to_have_text(filename)
    page.get_by_role("link", name="Imports", exact=True).first.click()

    toolbar = page.get_by_role("search", name="Filter imports")
    search = toolbar.get_by_role("searchbox")
    search.fill("tuesday")
    toolbar.get_by_role("button", name="Apply", exact=True).click()
    page.get_by_role("link", name="Import", exact=True).click()
    expect(page.get_by_role("link", name=filename)).to_be_visible()
    expect(page.get_by_role("link", name="Synthetic Monday.csv")).to_have_count(0)
    search.fill("unapplied change")
    export = page.get_by_role("link", name="Export CSV", exact=True)
    content = save_download(page, export.click, tmp_path / "specimen-imports.csv")
    rows = list(csv.DictReader(io.StringIO(content.decode(), newline="")))
    assert [(row["Import"], row["State"], row["Validated by"]) for row in rows] == [
        (filename, "Has errors", tech.get_full_name()),
    ]
    page.locator(".vr-topbar [data-vr-documentation-toggle]").click()
    # Reference sections are closed groups under the friendly help: the group names the export.
    expect(page.locator("#vr-documentation summary", has_text="Export CSV").first).to_be_visible()
    assert page.console_errors == []
