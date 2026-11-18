"""Manual observations retain discrepancies without changing the specimen's recorded position."""

import csv
import io
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from helpers import field, save_download, sign_in, sign_out
from playwright.sync_api import expect

from specimens.tests import conftest as specimen_fixtures
from specimens.tests.conftest import PASSWORD, register

pytestmark = pytest.mark.django_db(transaction=True)
people = specimen_fixtures.people
world = specimen_fixtures.world


def dialog(page):
    form = page.locator("#vr-form-dialog")
    expect(form).to_be_visible()
    return form


def test_inventory_audit_records_discrepancies_and_viewer_reads_the_evidence(
    page, people, world, tmp_path
):
    from quality.models import InventoryAudit

    specimen = register(people["technician"], world, position=world.position("A1"))
    original_position = specimen.occupancy.position_id
    sign_in(page, people["technician"].username, PASSWORD)
    page.get_by_role("link", name="Inventory audits", exact=True).click()
    page.get_by_role("link", name="New inventory audit", exact=True).first.click()
    form = dialog(page)
    field(form, "Box").select_option(str(world.box.pk))
    field(form, "Reason for the audit").fill("Synthetic scheduled box check")
    form.get_by_role("button", name="Create audit", exact=True).click()
    expect(page.get_by_role("heading", level=1)).to_have_text(re.compile(r"AUD-\d+"))
    audit_url = page.url
    audit = InventoryAudit.objects.get()

    page.get_by_role("link", name="Complete audit", exact=True).click()
    form = dialog(page)
    # Expected contents never become purported observations without an operator entering them.
    expect(field(form, "A1: observed contents")).to_have_value("")
    for position in audit.positions:
        field(form, f"{position['label']}: observed contents").select_option("empty")
    field(form, "A2: observed contents").select_option("accession")
    field(form, "A2: observed accession").fill("EXAMPLE-UNKNOWN-001")
    field(form, "Completion reason").fill("Synthetic physical inspection completed")
    form.get_by_role("button", name="Complete audit", exact=True).click()
    expect(form).to_contain_text("Explain the discrepancies")
    expect(field(form, "A2: observed accession")).to_have_value("EXAMPLE-UNKNOWN-001")
    field(form, "Discrepancy explanation").fill(
        "Synthetic missing vial and unknown label; investigate separately."
    )
    form.get_by_role("button", name="Complete audit", exact=True).click()
    expect(page).to_have_url(audit_url)
    expect(page.locator("main")).to_contain_text("EXAMPLE-UNKNOWN-001")
    expect(page.locator("main")).to_contain_text("Missing")
    expect(page.locator("main")).to_contain_text("Unexpected")
    specimen.occupancy.refresh_from_db()
    assert specimen.occupancy.position_id == original_position
    audit.refresh_from_db()
    assert audit.status == "completed"
    assert audit.finalization.counts == {"match": 7, "missing": 1, "unexpected": 1, "mismatch": 0}

    # Read the authored help beside the result, then verify read-only access through the UI.
    page.locator(".vr-topbar [data-vr-documentation-toggle]").click()
    help_panel = page.locator("#vr-documentation")
    expect(help_panel).to_be_visible()
    expect(help_panel).to_contain_text("inventory")
    sign_out(page, who=r"^sp-technician")
    sign_in(page, people["viewer"].username, PASSWORD)
    page.goto(audit_url)
    expect(page.locator("main")).to_contain_text("EXAMPLE-UNKNOWN-001")
    expect(page.get_by_role("link", name="Complete audit", exact=True)).to_have_count(0)
    expect(page.get_by_role("link", name="Cancel audit", exact=True)).to_have_count(0)
    page.get_by_role("link", name="Inventory audits", exact=True).first.click()
    toolbar = page.get_by_role("search", name="Filter inventory audits")
    search = toolbar.get_by_role("searchbox")
    search.fill("Synthetic scheduled")
    toolbar.locator('[name="status"]').select_option("completed")
    toolbar.get_by_role("button", name="Apply", exact=True).click()
    page.get_by_role("link", name="Audit", exact=True).click()
    expect(page.get_by_role("link", name=audit.number, exact=True)).to_be_visible()

    search.fill("Unsaved synthetic search")
    export = page.get_by_role("link", name="Export CSV", exact=True)
    assert parse_qs(urlsplit(export.get_attribute("href")).query) == {
        "q": ["Synthetic scheduled"],
        "status": ["completed"],
        "sort": ["number"],
    }
    download = save_download(page, export.click, tmp_path / "inventory-audits.csv")
    records = csv.DictReader(io.StringIO(download.decode(), newline=""))
    assert [(record["Audit"], record["Status"]) for record in records] == [
        (audit.number, "Completed"),
    ]
    page.locator(".vr-topbar [data-vr-documentation-toggle]").click()
    expect(help_panel.get_by_role("heading", name="Export CSV", exact=True)).to_be_visible()
    assert page.console_errors == []
