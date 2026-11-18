"""Scanner observations are reviewed before the immutable audit is completed."""

import csv
import io

import pytest
from helpers import field, native_select, pick, sign_in
from playwright.sync_api import expect

from core.models import AuditEvent
from quality import services
from quality.models import AuditFinalization
from specimens.tests import conftest as specimen_fixtures
from specimens.tests.conftest import PASSWORD, register

pytestmark = pytest.mark.django_db(transaction=True)
people = specimen_fixtures.people
world = specimen_fixtures.world


def scanner_csv(audit, *, box_code=None):
    data = io.StringIO(newline="")
    writer = csv.writer(data)
    writer.writerow(["box", "position", "state", "accession"])
    for index, position in enumerate(audit.positions):
        writer.writerow(
            [
                box_code or audit.box_code,
                position["label"],
                "accession" if index == 1 else "empty",
                "SYNTHETIC-SCANNER-001" if index == 1 else "",
            ]
        )
    return data.getvalue().encode("utf-8-sig")


def test_scanner_upload_preview_errors_and_reviewed_completion(page, people, world):
    specimen = register(people["technician"], world, position=world.position("A1"))
    original_position = specimen.occupancy.position_id
    audit = services.create_audit(
        actor=people["technician"], box_id=world.box.pk, reason="Synthetic scanner check"
    )
    events = AuditEvent.objects.filter(
        object_type="quality.inventoryaudit", object_id=str(audit.pk)
    )
    events_before = events.count()
    sign_in(page, people["technician"].username, PASSWORD)
    page.goto(f"/quality/audits/{audit.pk}/")
    audit_url = page.url
    page.get_by_role("link", name="Complete audit", exact=True).click()
    form = page.locator("#vr-form-dialog")
    expect(form).to_be_visible()
    field(form, "Completion reason").fill("Synthetic scanner review")
    pick(field(form, "A1: observed contents"), "accession")
    field(form, "A1: observed accession").fill("SYNTHETIC-RETAINED")
    form.locator('input[name="scanner_file"]').set_input_files(
        {
            "name": "wrong-box.csv",
            "mimeType": "text/csv",
            "buffer": scanner_csv(audit, box_code="WRONG"),
        }
    )
    form.get_by_role("button", name="Load observations", exact=True).click()
    expect(form).to_contain_text(f"the box must exactly match {audit.box_code}")
    expect(field(form, "A1: observed accession")).to_have_value("SYNTHETIC-RETAINED")
    expect(field(form, "Completion reason")).to_have_value("Synthetic scanner review")
    assert not AuditFinalization.objects.exists()

    form.locator('input[name="scanner_file"]').set_input_files(
        {"name": "observations.csv", "mimeType": "text/csv", "buffer": scanner_csv(audit)}
    )
    form.get_by_role("button", name="Load observations", exact=True).click()
    # Wait for the re-rendered form (field() re-resolves) before reading a select's native value.
    expect(field(form, "A2: observed accession")).to_have_value("SYNTHETIC-SCANNER-001")
    expect(native_select(field(form, "A1: observed contents"))).to_have_value("empty")
    expect(field(form, "Completion reason")).to_have_value("Synthetic scanner review")
    assert not AuditFinalization.objects.exists()
    assert events.count() == events_before
    form.locator("[data-vr-documentation-toggle]").click()
    help_panel = page.locator("#vr-documentation")
    expect(help_panel).to_be_visible()
    expect(help_panel).to_contain_text("Scanner CSV")
    help_panel.get_by_role("button", name="Close documentation", exact=True).click()

    form.get_by_role("button", name="Complete audit", exact=True).click()
    expect(form).to_contain_text("Explain the discrepancies")
    expect(field(form, "A2: observed accession")).to_have_value("SYNTHETIC-SCANNER-001")
    # Review can correct imported observations before the final evidence is saved.
    field(form, "A2: observed accession").fill("SYNTHETIC-REVIEWED-002")
    field(form, "Discrepancy explanation").fill(
        "Artificial scanner findings; no physical inspection."
    )
    form.get_by_role("button", name="Complete audit", exact=True).click()
    expect(page).to_have_url(audit_url)
    expect(page.locator("main")).to_contain_text("SYNTHETIC-REVIEWED-002")
    audit.refresh_from_db()
    assert audit.status == "completed"
    assert audit.finalization.counts == {"match": 7, "missing": 1, "unexpected": 1, "mismatch": 0}
    specimen.occupancy.refresh_from_db()
    assert specimen.occupancy.position_id == original_position
    assert page.console_errors == []
