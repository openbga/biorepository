"""Training history and filtered register downloads through the staff workspace."""

import csv
import io
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from django.utils import timezone
from helpers import field, save_download, sign_in, sign_out
from playwright.sync_api import expect

from specimens.tests import conftest as specimen_fixtures
from specimens.tests.conftest import PASSWORD

pytestmark = pytest.mark.django_db(transaction=True)
people = specimen_fixtures.people


def ready_dialog(page):
    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()
    expect(page.locator("#vr-form-content")).to_have_attribute("aria-busy", "false")
    return dialog


def test_training_record_completion_correction_cancellation_and_read_only(page, people):
    from quality.models import TrainingEvent, TrainingRecord

    sign_in(page, people["manager"].username, PASSWORD)
    page.get_by_role("link", name="Training records", exact=True).click()
    expect(page.locator("main")).to_contain_text("No training records yet")
    page.get_by_role("link", name="New training record", exact=True).first.click()
    form = ready_dialog(page)
    field(form, "Trainee").select_option(str(people["technician"].pk))
    field(form, "Topic").fill("Synthetic LN2 observation training")
    field(form, "Procedure / version reference").fill("SYN-LN2 v1")
    field(form, "Assignment due").fill("2026-01-02")
    field(form, "Reason").fill("Synthetic training assignment")
    form.get_by_role("button", name="Create training record", exact=True).click()
    expect(form).to_be_hidden()
    record = TrainingRecord.objects.get()
    detail_url = page.url
    expect(page.get_by_role("heading", level=1)).to_have_text(record.number)
    expect(page.locator(".vr-page-meta")).to_contain_text("Assigned")

    page.get_by_role("link", name="Record completion", exact=True).click()
    form = ready_dialog(page)
    field(form, "Completed on").fill("2026-01-01")
    field(form, "Trainer / provider").fill("Synthetic Example Trainer")
    field(form, "Reason").fill("Synthetic completion evidence")
    form.get_by_role("button", name="Record completion", exact=True).click()
    expect(form.get_by_role("alert")).to_contain_text("Your entries were kept")
    expect(field(form, "Completion evidence")).to_have_attribute("aria-invalid", "true")
    expect(field(form, "Trainer / provider")).to_have_value("Synthetic Example Trainer")
    field(form, "Completion evidence").fill("Synthetic supervised demonstration")
    field(form, "Competency assessment").fill("Synthetic assessment recorded by trainer")
    field(form, "Renewal due").fill("2027-01-01")
    form.get_by_role("button", name="Record completion", exact=True).click()
    expect(form).to_be_hidden()
    expect(page.locator(".vr-page-meta")).to_contain_text("Completed")
    expect(page.get_by_role("link", name="Record completion", exact=True)).to_have_count(0)

    page.get_by_role("link", name="Correct training record", exact=True).click()
    form = ready_dialog(page)
    field(form, "Completion evidence").fill("Synthetic corrected supervised demonstration")
    field(form, "Procedure / version reference").fill("SYN-LN2 v2")
    field(form, "Renewal due").fill("")
    field(form, "Reason").fill("Synthetic source evidence corrected; follow-up recorded separately")
    form.get_by_role("button", name="Correct training record", exact=True).click()
    expect(form).to_be_hidden()
    expect(page.locator("main")).to_contain_text("Synthetic corrected supervised demonstration")
    expect(page.locator("main")).to_contain_text("Synthetic supervised demonstration")

    # One batched desktop/mobile inspection, preserving the established shared UI.
    output = Path(__file__).resolve().parent.parent / ".local"
    output.mkdir(exist_ok=True)
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.screenshot(path=str(output / "training-desktop.png"), full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    page.screenshot(path=str(output / "training-mobile.png"), full_page=True)
    page.set_viewport_size({"width": 1440, "height": 1000})

    page.get_by_role("link", name="Cancel training record", exact=True).click()
    form = ready_dialog(page)
    field(form, "Reason").fill("Synthetic duplicate record; retain evidence")
    form.get_by_role("button", name="Cancel training record", exact=True).click()
    expect(form).to_be_hidden()
    expect(page.locator(".vr-page-meta")).to_contain_text("Cancelled")
    expect(page.locator("main")).to_contain_text("Synthetic corrected supervised demonstration")
    expect(page.get_by_role("link", name="Correct training record", exact=True)).to_have_count(0)
    record.refresh_from_db()
    assert record.status == "cancelled" and record.renewal_due is None
    assert list(TrainingEvent.objects.values_list("kind", flat=True)) == [
        "created",
        "completed",
        "corrected",
        "cancelled",
    ]

    page.locator(".vr-topbar [data-vr-documentation-toggle]").click()
    help_panel = page.locator("#vr-documentation")
    expect(help_panel).to_be_visible()
    expect(help_panel).to_contain_text("human competency assessments")
    sign_out(page, who=r"^sp-")
    sign_in(page, people["viewer"].username, PASSWORD)
    page.goto(detail_url)
    expect(page.locator("main")).to_contain_text("Read-only access")
    expect(page.get_by_role("link", name="Cancel training record", exact=True)).to_have_count(0)
    page.get_by_role("link", name="Training records", exact=True).first.click()
    page.locator('main input[name="q"]').fill("SYN-LN2 v2")
    page.get_by_role("button", name="Apply", exact=True).click()
    expect(page.get_by_role("link", name=record.number, exact=True)).to_be_visible()
    expect(page.get_by_role("link", name="New training record", exact=True)).to_have_count(0)
    assert page.console_errors == []


def test_viewer_downloads_the_applied_training_selection(page, people, tmp_path):
    from quality.training_services import create_training

    overdue = timezone.localdate() - timedelta(days=2)

    def create(topic, due_on):
        return create_training(
            actor=people["manager"],
            trainee_id=people["technician"].pk,
            values={"topic": topic, "due_on": due_on},
            reason="Synthetic assignment",
        )

    first = create("Synthetic CSV refresher A", overdue)
    second = create("Synthetic CSV refresher B", overdue)
    upcoming = create("Synthetic CSV refresher C", overdue + timedelta(days=10))
    unrelated = create("Synthetic other course", overdue)
    sign_in(page, people["viewer"].username, PASSWORD)
    page.get_by_role("link", name="Training records", exact=True).click()
    toolbar = page.get_by_role("search", name="Filter training records")
    search = toolbar.get_by_role("searchbox")
    search.fill("CSV refresher")
    toolbar.locator('[name="status"]').select_option("assigned")
    toolbar.locator('[name="due"]').select_option("assignment_overdue")
    toolbar.get_by_role("button", name="Apply", exact=True).click()
    sort = page.get_by_role("link", name="Training record", exact=True)
    sort.click()
    sort.click()
    expect(page.get_by_role("link", name=first.number, exact=True)).to_be_visible()
    expect(page.get_by_role("link", name=second.number, exact=True)).to_be_visible()
    expect(page.get_by_role("link", name=upcoming.number, exact=True)).to_have_count(0)
    expect(page.get_by_role("link", name=unrelated.number, exact=True)).to_have_count(0)

    search.fill("Unsaved synthetic search")
    export = page.get_by_role("link", name="Export CSV", exact=True)
    assert parse_qs(urlsplit(export.get_attribute("href")).query) == {
        "q": ["CSV refresher"],
        "status": ["assigned"],
        "due": ["assignment_overdue"],
        "sort": ["-number"],
    }
    download = save_download(page, export.click, tmp_path / "training-records.csv")

    records = list(csv.DictReader(io.StringIO(download.decode(), newline="")))
    assert [row["Training record"] for row in records] == [second.number, first.number]
    assert page.console_errors == []
