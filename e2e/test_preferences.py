"""Per-user settings follow the user, not the workstation: grid columns and page size."""

import pytest
from helpers import column_headers, make_user, pick, sign_in, sign_out
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)

PASSWORD = "e2e-preferences-Password-1"
LIST = "/specimens/"


def settled(page: Page):
    """The setting's POST has reached the server (static/js/preferences.js)."""
    page.wait_for_load_state("networkidle")


def rows_per_page(page: Page):
    return page.get_by_role("combobox", name="Rows per page")


def test_grid_columns_and_page_size_are_saved_per_user(page: Page, demo):
    make_user("pref_first", PASSWORD, "technician")
    make_user("pref_second", PASSWORD, "technician")

    sign_in(page, "pref_first", PASSWORD)
    page.goto(LIST)
    defaults = column_headers(page)
    assert "Custodian" in defaults
    page.get_by_role("button", name="Columns", exact=True).click()
    dialog = page.get_by_role("dialog", name="Columns")
    dialog.get_by_role("checkbox", name="Custodian", exact=True).uncheck()
    page.keyboard.press("Escape")
    settled(page)
    pick(rows_per_page(page), label="50")
    page.get_by_role("button", name="Show", exact=True).click()
    expect(page).to_have_url(r"/specimens/?per_page=50")

    page.goto(LIST)  # no parameters: the saved page size and columns apply
    expect(rows_per_page(page)).to_contain_text("50")
    assert "Custodian" not in column_headers(page)
    assert page.evaluate("Object.keys(localStorage).filter(k => k.startsWith('vr-grid'))") == []
    sign_out(page)

    # The next person at the same workstation gets the defaults, not the previous layout.
    sign_in(page, "pref_second", PASSWORD)
    page.goto(LIST)
    expect(rows_per_page(page)).to_contain_text("25")
    assert column_headers(page) == defaults
    sign_out(page)

    sign_in(page, "pref_first", PASSWORD)  # and the first user's settings are still there
    page.goto(LIST)
    expect(rows_per_page(page)).to_contain_text("50")
    assert "Custodian" not in column_headers(page)
    assert page.console_errors == []
