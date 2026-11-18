"""A reported deviation retains corrections through independent review and reopening."""

import csv
import io
import re
from urllib.parse import parse_qs, urlsplit

import pytest
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


def test_deviation_report_investigation_review_and_reopening(page, people, tmp_path):
    from quality.models import Deviation, DeviationEvent

    sign_in(page, people["technician"].username, PASSWORD)
    page.get_by_role("link", name="Deviations", exact=True).click()
    expect(page.locator("main")).to_contain_text("No deviations")
    page.get_by_role("link", name="New deviation", exact=True).first.click()
    form = ready_dialog(page)
    field(form, "Title").fill("Synthetic work instruction discrepancy")
    field(form, "Occurred at").fill("2026-01-01T10:00")
    field(form, "Description").fill("Synthetic worksheet omitted a verification step.")
    field(form, "Immediate action").fill("Synthetic worksheet removed from use.")
    field(form, "Reason").fill("Synthetic reporting exercise")
    form.get_by_role("button", name="Create deviation", exact=True).click()
    expect(form).to_be_hidden()
    expect(page.get_by_role("heading", level=1)).to_have_text(re.compile(r"DEV-\d+"))
    deviation_url = page.url
    deviation = Deviation.objects.get()
    assert deviation.status == "open"

    page.get_by_role("link", name="Submit for review", exact=True).click()
    form = ready_dialog(page)
    field(form, "Reason").fill("Synthetic premature submission")
    with page.expect_response(
        lambda response: response.request.method == "POST" and response.url.endswith("/submit/")
    ) as rejected:
        form.get_by_role("button", name="Submit for review", exact=True).click()
    assert rejected.value.status == 200
    expect(form.get_by_role("alert")).to_contain_text("Complete investigation")
    expect(field(form, "Reason")).to_have_value("Synthetic premature submission")
    assert page.console_errors == []
    ready_dialog(page)
    page.keyboard.press("Escape")
    discard = form.locator("[data-vr-form-discard]")
    expect(discard).to_be_visible()
    discard.click()
    expect(form).to_be_hidden()

    page.get_by_role("link", name="Edit", exact=True).click()
    form = ready_dialog(page)
    field(form, "Investigation").fill("Synthetic review found an obsolete worksheet copy.")
    field(form, "Corrective action").fill("Synthetic obsolete copies were replaced.")
    field(form, "Effectiveness").fill("Synthetic follow-up found the verification step recorded.")
    field(form, "Reason").fill("Synthetic investigation completed")
    form.get_by_role("button", name="Save changes", exact=True).click()
    expect(form).to_be_hidden()
    page.get_by_role("link", name="Submit for review", exact=True).click()
    form = ready_dialog(page)
    field(form, "Reason").fill("Synthetic report ready for independent review")
    form.get_by_role("button", name="Submit for review", exact=True).click()
    expect(form).to_be_hidden()
    expect(page.locator(".vr-page-meta")).to_contain_text("Awaiting review")
    expect(page.get_by_role("link", name="Edit", exact=True)).to_have_count(0)
    expect(page.get_by_role("link", name="Record review", exact=True)).to_have_count(0)

    sign_out(page, who=r"^sp-")
    sign_in(page, people["manager"].username, PASSWORD)
    page.goto(deviation_url)
    page.get_by_role("link", name="Record review", exact=True).click()
    form = ready_dialog(page)
    field(form, "Decision").select_option("close")
    field(form, "Reason").fill("Synthetic evidence reviewed; close the exercise.")
    form.get_by_role("button", name="Record review", exact=True).click()
    expect(form).to_be_hidden()
    expect(page.locator(".vr-page-meta")).to_contain_text("Closed")
    deviation.refresh_from_db()
    assert deviation.closed_by == people["manager"]

    page.get_by_role("link", name="Reopen deviation", exact=True).click()
    form = ready_dialog(page)
    field(form, "Reason").fill("Synthetic follow-up needs another review.")
    form.get_by_role("button", name="Reopen deviation", exact=True).click()
    expect(form).to_be_hidden()
    expect(page.locator(".vr-page-meta")).to_contain_text("Open")
    expect(page.locator("main")).to_contain_text("Synthetic evidence reviewed; close the exercise.")
    assert list(DeviationEvent.objects.values_list("kind", flat=True)) == [
        "created",
        "updated",
        "submitted",
        "closed",
        "reopened",
    ]

    page.locator(".vr-topbar [data-vr-documentation-toggle]").click()
    help_panel = page.locator("#vr-documentation")
    expect(help_panel).to_be_visible()
    expect(help_panel).to_contain_text("effectiveness")
    sign_out(page, who=r"^sp-")
    sign_in(page, people["viewer"].username, PASSWORD)
    page.goto(deviation_url)
    expect(page.locator("main")).to_contain_text("Synthetic obsolete copies were replaced.")
    expect(page.get_by_role("link", name="Edit", exact=True)).to_have_count(0)
    expect(page.get_by_role("link", name="Submit for review", exact=True)).to_have_count(0)
    page.get_by_role("link", name="Deviations", exact=True).first.click()
    toolbar = page.get_by_role("search", name="Filter deviations")
    search = toolbar.get_by_role("searchbox")
    search.fill("Synthetic work instruction")
    toolbar.locator('[name="status"]').select_option("open")
    toolbar.get_by_role("button", name="Apply", exact=True).click()
    page.get_by_role("link", name="Deviation", exact=True).click()
    expect(page.get_by_role("link", name=deviation.number, exact=True)).to_be_visible()

    search.fill("Unsaved synthetic search")
    export = page.get_by_role("link", name="Export CSV", exact=True)
    assert parse_qs(urlsplit(export.get_attribute("href")).query) == {
        "q": ["Synthetic work instruction"],
        "status": ["open"],
        "sort": ["number"],
    }
    download = save_download(page, export.click, tmp_path / "deviations.csv")
    records = csv.DictReader(io.StringIO(download.decode(), newline=""))
    assert [record["Deviation"] for record in records] == [deviation.number]
    page.locator(".vr-topbar [data-vr-documentation-toggle]").click()
    expect(help_panel.get_by_role("heading", name="Export CSV", exact=True)).to_be_visible()
    assert page.console_errors == []
