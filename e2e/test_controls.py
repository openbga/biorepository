"""Shared form controls (static/js/controls.js) through the real UI: no browser-default control.

A select in a dialog (Escape closes its menu, not the dialog) and in a filter bar, the date
picker (typed entry, keyboard, shortcut, validation, inside a dialog), a decimal with its unit
addon, and the file drop zone. Each submission reaches the server unchanged.
"""

import re
from datetime import datetime
from decimal import Decimal

import pytest
from django.urls import reverse
from helpers import REPOSITORY_TZ, field, pick
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)


def menu_of(combobox):
    return combobox.page.locator(f"[id='{combobox.get_attribute('aria-controls')}']")


def test_dialog_select_escape_and_decimal_with_its_unit(page: Page, sign_in_as):
    from storage.models import StorageUnit

    sign_in_as("manager")
    page.goto(reverse("storage:unit-list"))
    page.get_by_role("link", name="New storage unit", exact=True).first.click()
    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()
    field(dialog, "Code").fill("E2E-CTL-01")
    field(dialog, "Name").fill("E2E controls tank")
    pick(field(dialog, "Room"), index=1)

    # The menu opens over the dialog; Escape closes the menu only.
    storage_class = dialog.get_by_role("combobox", name="Storage class")
    storage_class.click()
    expect(menu_of(storage_class)).to_be_visible()
    page.keyboard.press("Escape")
    expect(menu_of(storage_class)).to_be_hidden()
    expect(dialog).to_be_visible()
    pick(storage_class, index=1)
    pick(field(dialog, "Technology"), label="LN2 vapor phase")

    # A decimal is a text field with its unit as an addon; the typed string reaches the server.
    temperature = field(dialog, "Nominal temperature")
    expect(temperature).to_have_attribute("type", "text")
    expect(temperature).not_to_have_attribute("inputmode", re.compile("."))  # a minus is allowed
    unit = dialog.locator(f"#{temperature.get_attribute('id')}_unit")
    expect(unit).to_have_text("°C")
    temperature.fill("-160.50")
    dialog.get_by_role("button", name="Create storage unit", exact=True).click()
    expect(dialog).to_be_hidden()
    expect(page.get_by_role("heading", level=1)).to_have_text("E2E-CTL-01")
    assert StorageUnit.objects.get(code="E2E-CTL-01").nominal_temperature == Decimal("-160.50")
    assert page.console_errors == []


def test_filter_bar_select_waits_for_apply(page: Page, sign_in_as):
    sign_in_as("viewer")
    page.goto(reverse("specimens:specimen-list"))
    status = page.get_by_role("combobox", name="Status")
    expect(status).to_contain_text("Any status")
    status.click()
    menu = menu_of(status)
    menu.get_by_role("option", name="Available", exact=True).click()
    expect(menu).to_be_hidden()
    expect(page).not_to_have_url(re.compile(r"status="))
    page.get_by_role("button", name="Apply", exact=True).click()
    expect(page).to_have_url(re.compile(r"[?&]status=available\b"))
    expect(page.get_by_role("combobox", name="Status")).to_contain_text("Available")
    # The browser's own select never shows: the native element is the hidden submitted value.
    assert page.locator("select#id_status").evaluate("el => el.classList.contains('tomselected')")


def test_date_picker_typed_entry_keyboard_shortcut_and_validation_in_a_dialog(
    page: Page, sign_in_as
):
    from core.models import Announcement

    sign_in_as("admin")
    page.goto(reverse("announcement-list"))
    page.get_by_role("link", name=re.compile("^New announcement")).first.click()
    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()
    field(dialog, "Title").fill("E2E controls notice")
    field(dialog, "Message").fill("Synthetic announcement for the date picker journey.")
    dialog.get_by_label("Staff dashboard").check()

    starts = field(dialog, "Show from")
    until = field(dialog, "Show until")
    starts_value = dialog.locator("input[name=starts_at]")
    until_value = dialog.locator("input[name=ends_at]")
    expect(starts_value).to_have_attribute("type", "hidden")
    expect(dialog).to_contain_text(re.compile(r"Repository time, E[SD]T"))

    # Typed entry: a US date and a compact time become the server's own format.
    starts.fill("11/13/2026 0930")
    starts.press("Tab")
    expect(starts).to_have_value("2026-11-13 09:30")
    expect(starts_value).to_have_value("2026-11-13T09:30")

    # Keyboard: Alt+Down opens the calendar on a day; arrows move; Enter picks; the time follows.
    until.click()
    until.fill("2026-11-20 08:00")
    until.press("Alt+ArrowDown")
    calendar = dialog.get_by_role("dialog", name="Choose a date and time")
    expect(calendar).to_be_visible()
    expect(calendar.get_by_role("button", name="November 20, 2026")).to_be_focused()
    page.keyboard.press("ArrowRight")
    page.keyboard.press("Enter")
    time = calendar.get_by_label("Time")
    expect(time).to_be_focused()
    time.fill("17:45")
    time.press("Enter")
    expect(calendar).to_be_hidden()
    expect(until).to_be_focused()
    expect(until_value).to_have_value("2026-11-21T17:45")

    # Escape closes the calendar only, and returns focus to the field.
    until.press("Alt+ArrowDown")
    expect(calendar).to_be_visible()
    page.keyboard.press("Escape")
    expect(calendar).to_be_hidden()
    expect(dialog).to_be_visible()
    expect(until).to_be_focused()

    # Shortcut: Now uses the repository's clock.
    dialog.locator(".vr-date-input:has(input[name=starts_at]) .vr-date-icon").click()
    calendar.get_by_role("button", name="Now", exact=True).click()
    today = datetime.now(REPOSITORY_TZ).strftime("%Y-%m-%d")
    expect(starts_value).to_have_value(re.compile(rf"^{today}T\d\d:\d\d$"))

    # An impossible date is refused beside the field and nothing is submitted.
    until.fill("2026-02-30 10:00")
    dialog.get_by_role("button", name="Post announcement", exact=True).click()
    expect(dialog.get_by_text("Enter a date and time as YYYY-MM-DD HH:MM.")).to_be_visible()
    expect(until).to_have_attribute("aria-invalid", "true")
    assert not Announcement.objects.filter(title="E2E controls notice").exists()

    until.fill("2099-12-01 18:00")
    dialog.get_by_role("button", name="Post announcement", exact=True).click()
    expect(dialog).to_be_hidden()
    saved = Announcement.objects.get(title="E2E controls notice")
    assert saved.ends_at.astimezone(REPOSITORY_TZ).strftime("%Y-%m-%d %H:%M") == "2099-12-01 18:00"


def test_file_component_names_the_chosen_file_and_accepts_a_drop(page: Page, sign_in_as):
    sign_in_as("technician")
    page.goto(reverse("specimens:import-upload"))
    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()  # opened by its address, over the import list
    zone = dialog.locator("[data-vr-file]")
    expect(zone.locator(".vr-drop")).to_contain_text("Choose a file")
    expect(zone.locator(".vr-drop-hint")).to_have_text("CSV files")
    native = zone.locator("input[type=file]")
    native.focus()  # visually hidden, still the keyboard target; the zone shows the focus
    expect(native).to_be_focused()
    native.set_input_files({"name": "first.csv", "mimeType": "text/csv", "buffer": b"a,b\n1,2\n"})
    expect(zone.locator("[data-vr-file-name]")).to_contain_text("first.csv")

    # Dropping a file onto the zone fills the same input.
    zone.locator(".vr-drop").evaluate(
        """drop => {
            const transfer = new DataTransfer();
            transfer.items.add(new File(["a,b\\n"], "dropped.csv", {type: "text/csv"}));
            drop.dispatchEvent(new DragEvent("drop", {dataTransfer: transfer, bubbles: true}));
        }"""
    )
    expect(zone.locator("[data-vr-file-name]")).to_contain_text("dropped.csv")
    assert native.evaluate("el => el.files[0].name") == "dropped.csv"
