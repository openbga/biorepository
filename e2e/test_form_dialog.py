"""Shared form dialogs keep page context, input, keyboard access and real POST semantics.

The announcement New/Edit pages are the platform's dialog surfaces (core/form_surfaces.py).
"""

import re
from datetime import timedelta

import pytest
from conftest import NARROW
from helpers import field, local_input, make_announcement, native_select
from playwright.sync_api import Locator, Page, expect

from core.models import Announcement

pytestmark = pytest.mark.django_db(transaction=True)

LIST = "/repository/announcements/"


def open_new_form(page: Page, sign_in_as) -> tuple[Locator, Locator, str]:
    sign_in_as("admin")
    page.goto(LIST)
    origin = page.url
    trigger = page.get_by_role("link", name="New announcement", exact=True).first
    trigger.click()
    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()
    expect(dialog.locator("#vr-form-title")).to_have_text("New announcement")
    expect(field(dialog, "Title")).to_be_focused()
    return dialog, trigger, origin


def no_document_overflow(page: Page):
    assert page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1"
    ), "The dialog must not introduce horizontal document scrolling."


def test_first_edit_waits_for_slow_form_initialization_without_losing_input(page: Page, sign_in_as):
    """A delayed fragment must finish initializing before its first field accepts typing."""
    sign_in_as("admin")
    page.goto(LIST)
    page.evaluate("htmx.config.defaultSettleDelay = 1000")
    page.get_by_role("link", name="New announcement", exact=True).first.click()
    dialog = page.locator("#vr-form-dialog")
    message = field(dialog, "Message")
    expect(message).to_be_disabled()
    # Start with a field other than the autofocus target. Late focus must not steal typing.
    message.fill("Synthetic first edit")
    expect(message).to_have_value("Synthetic first edit")
    expect(message).to_be_focused()
    field(dialog, "Title").fill("E2E slow dialog initialization")
    dialog.get_by_label("Staff dashboard").check()
    dialog.get_by_role("button", name="Post announcement", exact=True).click()
    expect(dialog).to_be_hidden()
    assert Announcement.objects.get(title="E2E slow dialog initialization").body == (
        "Synthetic first edit"
    )


def test_link_preserves_context_and_desktop_dialog_maximizes_restores_and_returns_focus(
    page: Page, sign_in_as
):
    dialog, trigger, origin = open_new_form(page, sign_in_as)
    expect(page).to_have_url(origin)
    frame = dialog.locator(".modal-content")
    normal = frame.bounding_box()
    viewport = page.viewport_size
    assert normal and viewport
    assert normal["x"] >= 23 and normal["y"] >= 23, normal
    assert normal["width"] <= viewport["width"] - 47, normal
    assert normal["height"] <= viewport["height"] - 47, normal
    no_document_overflow(page)

    maximize = dialog.locator("[data-vr-form-maximize]")
    maximize.click()
    expect(dialog.locator(".modal-dialog")).to_have_class(
        re.compile(r"(?:^|\s)modal-fullscreen(?:\s|$)")
    )
    full = frame.bounding_box()
    assert full
    assert full["x"] == pytest.approx(0, abs=1)
    assert full["y"] == pytest.approx(0, abs=1)
    assert full["width"] == pytest.approx(viewport["width"], abs=1)
    assert full["height"] == pytest.approx(viewport["height"], abs=1)
    maximize.click()
    expect(dialog.locator(".modal-dialog")).not_to_have_class(
        re.compile(r"(?:^|\s)modal-fullscreen(?:\s|$)")
    )
    restored = frame.bounding_box()
    assert restored
    assert restored["width"] == pytest.approx(normal["width"], abs=1)
    assert restored["x"] == pytest.approx(normal["x"], abs=1)

    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(page).to_have_url(origin)
    expect(trigger).to_be_focused()
    assert page.console_errors == []


def test_narrow_form_is_fullscreen_and_keeps_fields_and_actions_inside_viewport(
    page: Page, sign_in_as
):
    page.set_viewport_size(NARROW)
    dialog, _trigger, origin = open_new_form(page, sign_in_as)
    frame = dialog.locator(".modal-content").bounding_box()
    assert frame
    assert frame["x"] == pytest.approx(0, abs=1)
    assert frame["y"] == pytest.approx(0, abs=1)
    assert frame["width"] == pytest.approx(NARROW["width"], abs=1)
    assert frame["height"] == pytest.approx(NARROW["height"], abs=1)
    no_document_overflow(page)
    field(dialog, "Message").fill("Synthetic narrow-screen message")
    save = dialog.get_by_role("button", name="Post announcement", exact=True)
    save.scroll_into_view_if_needed()
    bounds = save.bounding_box()
    assert bounds and bounds["x"] >= 0
    assert bounds["x"] + bounds["width"] <= NARROW["width"] + 1
    assert bounds["y"] + bounds["height"] <= NARROW["height"] + 1
    expect(page).to_have_url(origin)
    assert page.console_errors == []


def test_dirty_dialog_keeps_edits_until_discard_is_explicit(page: Page, sign_in_as):
    dialog, trigger, origin = open_new_form(page, sign_in_as)
    title = "E2E unsaved modal announcement"
    field(dialog, "Title").fill(title)
    dialog.locator("[data-vr-form-close]").click()
    # Unsaved changes ask in a confirm pop-up over the form, whose safe choice has the focus.
    confirm = page.locator("#vr-form-discard")
    expect(confirm).to_be_visible()
    expect(confirm.get_by_role("heading", name="Discard unsaved changes?")).to_be_visible()
    keep = confirm.locator("[data-vr-form-keep]").last
    expect(keep).to_be_focused()
    keep.click()
    expect(confirm).to_be_hidden()
    expect(field(dialog, "Title")).to_have_value(title)
    expect(dialog).to_be_visible()

    # Escape in the pop-up means Keep editing.
    page.keyboard.press("Escape")
    expect(confirm).to_be_visible()
    page.keyboard.press("Escape")
    expect(confirm).to_be_hidden()
    expect(dialog).to_be_visible()

    page.keyboard.press("Escape")
    discard = confirm.locator("[data-vr-form-discard]")
    expect(discard).to_be_visible()
    discard.click()
    expect(dialog).to_be_hidden()
    expect(trigger).to_be_focused()
    expect(page).to_have_url(origin)
    assert not Announcement.objects.filter(title=title).exists()

    trigger.click()
    expect(dialog).to_be_visible()
    expect(field(dialog, "Title")).to_have_value("")
    assert page.console_errors == []


def test_invalid_post_preserves_input_and_focus_then_save_closes_and_reloads_the_list(
    page: Page, sign_in_as
):
    dialog, _trigger, origin = open_new_form(page, sign_in_as)
    title = "E2E validated modal announcement"
    field(dialog, "Title").fill(title)
    field(dialog, "Message").fill("Example text only.")
    # No place chosen and an end before the start: the server reports both inline.
    field(dialog, "Show from").fill(local_input(timedelta(hours=1)))
    field(dialog, "Show until").fill(local_input(timedelta(minutes=-30)))
    dialog.get_by_role("button", name="Post announcement", exact=True).click()
    until = field(dialog, "Show until")
    expect(until).to_have_attribute("aria-invalid", "true")
    expect(dialog.get_by_text("Show until must be later than Show from.")).to_be_visible()
    expect(dialog.get_by_text("Choose at least one place").first).to_be_visible()
    expect(field(dialog, "Title")).to_have_value(title)
    expect(field(dialog, "Message")).to_have_value("Example text only.")
    # The submitted value keeps the server format; the visible field shows the typed one.
    expect(native_select(until)).to_have_value(re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d$"))
    expect(dialog).to_be_visible()
    expect(page).to_have_url(origin)
    assert not Announcement.objects.filter(title=title).exists()
    # Focus moves to the first problem so a keyboard user lands on it.
    assert dialog.evaluate("el => el.contains(document.activeElement)")

    dialog.get_by_label("Staff dashboard").check()
    field(dialog, "Show until").fill("")
    field(dialog, "Show from").fill(local_input(timedelta(minutes=-5)))
    dialog.get_by_role("button", name="Post announcement", exact=True).click()
    expect(dialog).to_be_hidden()
    expect(page.get_by_text("Announcement posted.", exact=True)).to_be_visible()
    expect(page).to_have_url(re.compile(re.escape(LIST) + r"$"))
    expect(page.get_by_role("link", name=title, exact=True)).to_be_visible()
    assert Announcement.objects.get(title=title).show_to_staff
    assert page.console_errors == []


def test_edit_opens_in_the_dialog_saves_and_shows_history(page: Page, sign_in_as):
    notice = make_announcement("E2E editable notice", show_on_sign_in=False)
    sign_in_as("admin")
    page.goto(LIST)
    page.get_by_role("link", name="E2E editable notice", exact=True).click()
    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()
    expect(dialog.locator("#vr-form-title")).to_have_text("Edit E2E editable notice")
    expect(field(dialog, "Title")).to_have_value("E2E editable notice")
    expect(dialog.get_by_label("Staff dashboard")).to_be_checked()
    field(dialog, "Title").fill("E2E edited notice")
    dialog.get_by_role("button", name="Save changes", exact=True).click()
    expect(dialog).to_be_hidden()
    expect(page.get_by_text("Announcement saved.", exact=True)).to_be_visible()
    expect(page.get_by_role("link", name="E2E edited notice", exact=True)).to_be_visible()
    notice.refresh_from_db()
    assert notice.title == "E2E edited notice"
    assert page.console_errors == []


def test_direct_form_url_opens_over_its_list_and_cancel_closes_it(page: Page, sign_in_as):
    """Opened by its address, a form opens in the dialog over the page it belongs to."""
    sign_in_as("admin")
    path = "/repository/announcements/new/"
    page.goto(path)
    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()
    expect(dialog.locator("#vr-form-title")).to_have_text("New announcement")
    expect(field(dialog, "Title")).to_be_visible()
    # Behind it is the list, at the list's clean address.
    expect(page).to_have_url(re.compile(re.escape(LIST) + r"$"))
    expect(page.get_by_role("heading", name="Announcements", exact=True, level=1)).to_be_attached()
    dialog.get_by_role("button", name="Cancel", exact=True).or_(
        dialog.get_by_role("link", name="Cancel", exact=True)
    ).click()
    expect(dialog).to_be_hidden()
    expect(page).to_have_url(re.compile(re.escape(LIST) + r"$"))
    expect(page.get_by_role("heading", name="Announcements", exact=True, level=1)).to_be_visible()
    assert page.console_errors == []


def test_without_scripts_the_form_url_is_a_complete_page(browser, browser_context_args, demo):
    """The dialog is an enhancement: the same URL is a normal page that posts and redirects."""
    context = browser.new_context(**{**browser_context_args, "java_script_enabled": False})
    try:
        page = context.new_page()
        page.set_default_timeout(10_000)
        page.goto("/accounts/login/")
        page.get_by_label(re.compile("^(Username or email|Login)")).fill("demo_admin")
        page.get_by_label("Password").fill(demo.password)
        page.get_by_role("button", name="Sign in").click()
        page.goto("/repository/announcements/new/")
        expect(page.get_by_role("heading", level=1, name="New announcement")).to_be_visible()
        expect(page.get_by_role("link", name="Announcements", exact=True).first).to_be_visible()
        title = "E2E no-script announcement"
        field(page, "Title").fill(title)
        field(page, "Message").fill("Example text only.")
        page.get_by_label("Staff dashboard").check()
        page.get_by_role("button", name="Post announcement", exact=True).click()
        expect(page).to_have_url(re.compile(re.escape(LIST) + r"$"))
        expect(page.get_by_role("link", name=title, exact=True)).to_be_visible()
    finally:
        context.close()


def test_dialog_blocks_background_focus_and_traps_keyboard_in_both_directions(
    page: Page, sign_in_as
):
    dialog, trigger, _origin = open_new_form(page, sign_in_as)
    # Check inertness and actual focus without coupling to the shell wrapper IDs.
    assert trigger.evaluate("el => Boolean(el.closest('[inert]'))")
    trigger.evaluate("el => el.focus()")
    expect(trigger).not_to_be_focused()
    assert dialog.evaluate("el => el.contains(document.activeElement)"), page.evaluate(
        "({tag: document.activeElement.tagName, id: document.activeElement.id})"
    )

    controls = dialog.locator(
        'a[href], button:not([disabled]), input:not([type="hidden"]):not([disabled]), '
        'select:not([disabled]), textarea:not([disabled]), [tabindex="0"]'
    ).filter(visible=True)
    last = controls.last
    last.focus()
    page.keyboard.press("Tab")
    assert dialog.evaluate("el => el.contains(document.activeElement)"), page.evaluate(
        "({tag: document.activeElement.tagName, id: document.activeElement.id})"
    )
    expect(last).not_to_be_focused()
    first = controls.first
    first.focus()
    page.keyboard.press("Shift+Tab")
    assert dialog.evaluate("el => el.contains(document.activeElement)"), page.evaluate(
        "({tag: document.activeElement.tagName, id: document.activeElement.id})"
    )
    expect(first).not_to_be_focused()
    assert page.console_errors == []


def test_withdraw_confirm_dialog_cancels_and_confirms(page: Page, sign_in_as):
    notice = make_announcement("E2E withdrawable notice")
    sign_in_as("admin")
    page.goto(LIST)
    row = page.get_by_role("row", name=re.compile("E2E withdrawable notice"))
    button = row.get_by_role("button", name=re.compile("^Withdraw"))
    button.click()
    confirm = page.get_by_role("dialog", name="Withdraw announcement E2E withdrawable notice?")
    expect(confirm).to_be_visible()
    expect(confirm).to_contain_text("cannot be shown again or changed")
    confirm.locator(".modal-footer").get_by_role("button", name="Cancel", exact=True).click()
    expect(confirm).to_be_hidden()
    expect(button).to_be_focused()
    notice.refresh_from_db()
    assert notice.withdrawn_at is None

    button.click()
    expect(confirm).to_be_visible()
    page.keyboard.press("Escape")
    expect(confirm).to_be_hidden()
    notice.refresh_from_db()
    assert notice.withdrawn_at is None

    button.click()
    confirm.get_by_role("button", name="Withdraw announcement E2E withdrawable notice").click()
    expect(page.get_by_text("Withdrew “E2E withdrawable notice”.", exact=False)).to_be_visible()
    notice.refresh_from_db()
    assert notice.withdrawn_at is not None
    expect(
        page.get_by_role("row", name=re.compile("E2E withdrawable notice")).get_by_role("button")
    ).to_have_count(0)
    # A withdrawn announcement opens read-only: every field disabled and no save button.
    page.get_by_role("link", name="E2E withdrawable notice", exact=True).click()
    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()
    expect(field(dialog, "Title")).to_be_disabled()
    expect(dialog.get_by_role("button", name="Save changes")).to_have_count(0)
    assert page.console_errors == []


@pytest.fixture
def header_action_route(settings, monkeypatch):
    """The fixture route (e2e/fixture_urls.py) registered as a dialog form for this test only."""
    from core import form_surfaces

    settings.ROOT_URLCONF = "fixture_urls"
    monkeypatch.setitem(form_surfaces.FORM_SURFACES, "e2e-header-action", "lg")
    form_surfaces.form_route_patterns.cache_clear()
    yield "/e2e/form-with-header-action/"
    form_surfaces.form_route_patterns.cache_clear()


def test_page_header_actions_move_into_the_dialog_header(
    page: Page, sign_in_as, header_action_route
):
    sign_in_as("admin")
    page.goto(header_action_route)  # by its address: it opens over its Cancel page, the list
    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()
    expect(page).to_have_url(re.compile(re.escape(LIST) + r"$"))
    tools = dialog.locator("[data-vr-form-tools]")
    action = tools.get_by_role("link", name="Open the list", exact=True)
    expect(action).to_be_visible()
    expect(action).to_have_attribute("data-vr-page-action", "")
    # The action sits before the dialog's own tools, and nowhere else in the dialog.
    assert action.evaluate("el => el.parentElement.firstElementChild === el")
    expect(
        dialog.locator("#vr-form-content").get_by_role("link", name="Open the list")
    ).to_have_count(0)
    # The form's action row stays the dialog body's last element.
    assert dialog.locator(".vr-form-actions").evaluate(
        "el => { const body = el.closest('.modal-body');"
        " return el.getBoundingClientRect().bottom <= body.getBoundingClientRect().bottom + 1; }"
    )
    dialog.get_by_role("link", name="Cancel", exact=True).click()
    expect(dialog).to_be_hidden()
    expect(tools.locator("[data-vr-page-action]")).to_have_count(0)
    assert page.console_errors == []
