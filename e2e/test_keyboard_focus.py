"""Keyboard focus order and visibility, explicit filter submission and text-resize regressions."""

import re

import pytest
from helpers import field, make_announcement, pick
from playwright.sync_api import Page, expect

from core.models import Announcement

pytestmark = pytest.mark.django_db(transaction=True)


def assert_focus_ring(control):
    control.focus()
    control.page.keyboard.press("Tab")
    control.page.keyboard.press("Shift+Tab")
    expect(control).to_be_focused()
    assert control.evaluate(
        "el => { const s = getComputedStyle(el); return (s.outlineStyle !== 'none' && "
        "parseFloat(s.outlineWidth) >= 2) || s.boxShadow !== 'none'; }"
    )


def active(page: Page) -> dict:
    return page.evaluate(
        "() => { const e = document.activeElement; return {tag: e.tagName, "
        "text: (e.innerText || e.value || e.getAttribute('aria-label') || '').trim()}; }"
    )


def test_sign_in_skip_link_is_first_stop_visible_and_moves_focus_to_main(page: Page, demo):
    page.goto("/accounts/login/")
    page.keyboard.press("Tab")
    skip = page.get_by_role("link", name="Skip to main content")
    expect(skip).to_be_focused()
    box = skip.bounding_box()
    assert box and box["width"] > 1 and box["y"] >= 0, "Skip link is not visible when focused"
    page.keyboard.press("Enter")
    expect(page.locator("main#main")).to_be_focused()


def test_sign_in_focus_order_reaches_every_control_in_reading_order(page: Page, demo):
    make_announcement("Synthetic sign-in notice")
    page.goto("/accounts/login/")
    expected = [
        "Skip to main content",
        "Page documentation",
        "Theme",
        "Login",
        "Password",
        "Sign in",
    ]
    seen = []
    for _ in range(12):
        page.keyboard.press("Tab")
        info = page.evaluate(
            "() => { const e = document.activeElement; const l = e.labels && e.labels[0];"
            " return (l ? l.innerText : (e.getAttribute('aria-label') || e.innerText || ''))"
            ".replace(/\\s*\\*\\s*$/, '').trim(); }"
        )
        seen.append(info)
    positions = [seen.index(name) if name in seen else None for name in expected]
    assert None not in positions, seen
    assert positions == sorted(positions), seen
    # Every stop shows a visible focus indicator.
    page.goto("/accounts/login/")
    for _ in range(6):
        page.keyboard.press("Tab")
        assert page.evaluate(
            "() => { const s = getComputedStyle(document.activeElement); return "
            "(s.outlineStyle !== 'none' && parseFloat(s.outlineWidth) > 0) || "
            "s.boxShadow !== 'none' || document.activeElement.classList.contains('vr-skip'); }"
        ), active(page)
    # The announcements box beside the form is a keyboard-focusable scroll region.
    expect(page.get_by_role("region", name="Announcements")).to_have_attribute("tabindex", "0")


def test_failed_sign_in_focuses_non_field_error(page: Page, demo):
    page.goto("/accounts/login/")
    page.get_by_label(re.compile("^(Username or email|Login)")).fill("demo_viewer")
    field(page, "Password").fill("synthetic-wrong-password")
    page.get_by_role("button", name="Sign in", exact=True).click()
    error = page.locator('.vr-entrance-panel .alert-danger[role="alert"]')
    expect(error).to_be_visible()
    expect(error).to_be_focused()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_sidebar_and_topbar_controls_have_visible_focus(page: Page, sign_in_as, theme):
    sign_in_as("admin")
    page.evaluate("theme => document.documentElement.dataset.bsTheme = theme", theme)
    assert_focus_ring(page.get_by_role("navigation", name="Primary").get_by_role("link").first)
    assert_focus_ring(page.get_by_role("button", name="Theme", exact=True))
    assert_focus_ring(page.get_by_role("button", name="Page documentation", exact=True))


def test_skip_link_and_landmarks_in_the_staff_shell(page: Page, sign_in_as):
    sign_in_as("viewer")
    page.keyboard.press("Tab")
    expect(page.get_by_role("link", name="Skip to main content")).to_be_focused()
    page.keyboard.press("Enter")
    expect(page.locator("main#main")).to_be_focused()
    expect(page.get_by_role("main")).to_have_count(1)
    expect(page.get_by_role("navigation", name="Primary")).to_have_count(1)
    expect(page.get_by_role("banner")).to_have_count(1)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_pager_has_visible_focus(page: Page, sign_in_as, theme):
    sign_in_as("admin")
    Announcement.objects.bulk_create(
        [
            Announcement(
                title=f"E2E pager notice {n:02d}",
                body="Example text only.",
                show_to_staff=True,
                starts_at=make_announcement("Synthetic template").starts_at,
            )
            for n in range(26)
        ]
    )
    page.goto("/repository/announcements/")
    page.evaluate("theme => document.documentElement.dataset.bsTheme = theme", theme)
    assert_focus_ring(page.get_by_role("link", name="Next page", exact=True))


def test_menus_open_from_the_keyboard_and_return_focus(page: Page, sign_in_as):
    sign_in_as("technician")
    toggle = page.get_by_role("button", name="Theme", exact=True)
    toggle.focus()
    toggle.press("Enter")
    page.keyboard.press("ArrowDown")
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    expect(page.locator(".vr-theme-option:visible")).to_have_count(0)
    expect(toggle).to_be_focused()

    account = page.get_by_role("button", name=re.compile("^Demo "))
    account.focus()
    account.press("Enter")
    expect(page.get_by_role("button", name="Sign out")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.get_by_role("button", name="Sign out")).to_be_hidden()
    expect(account).to_be_focused()


def test_help_panel_takes_focus_and_returns_it_on_escape(page: Page, sign_in_as):
    sign_in_as("viewer")
    toggle = page.locator(".vr-topbar [data-vr-documentation-toggle]")
    toggle.click()
    panel = page.locator("#vr-documentation")
    expect(panel).to_be_visible()
    expect(panel.get_by_role("heading", level=2)).to_be_focused()
    page.keyboard.press("Escape")
    expect(panel).to_be_hidden()
    expect(toggle).to_be_focused()
    toggle.press("Enter")
    expect(panel).to_be_visible()
    panel.get_by_role("button", name="Close documentation", exact=True).press("Enter")
    expect(panel).to_be_hidden()
    expect(toggle).to_be_focused()


def test_form_dialog_focus_goes_in_stays_in_and_returns_to_the_trigger(page: Page, sign_in_as):
    sign_in_as("admin")
    page.goto("/repository/announcements/")
    trigger = page.get_by_role("link", name="New announcement", exact=True).first
    trigger.focus()
    trigger.press("Enter")
    dialog = page.locator("#vr-form-dialog")
    expect(field(dialog, "Title")).to_be_focused()
    for _ in range(25):  # more Tab presses than the dialog has stops: focus never escapes
        page.keyboard.press("Tab")
        assert dialog.evaluate("el => el.contains(document.activeElement)"), active(page)
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(trigger).to_be_focused()


def test_selects_wait_for_explicit_submission(page: Page, sign_in_as):
    sign_in_as("admin")
    make_announcement("Synthetic notice")
    page.goto("/repository/announcements/")
    control = page.get_by_role("combobox", name="Rows per page", exact=True)
    control.focus()
    before = page.url
    pick(control, "50")
    # A frame boundary allows change handlers and navigation to settle.
    page.evaluate(
        "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
    )
    assert page.url == before
    expect(control).to_be_focused()
    button = control.locator("xpath=ancestor::form").get_by_role("button", name="Show", exact=True)
    expect(button).to_be_visible()
    button.click()
    expect(page).to_have_url(re.compile(r"[?&]per_page=50(?:&|$)"))
    page.locator(".vr-topbar [data-vr-documentation-toggle]").click()
    expect(page.locator("#vr-documentation")).to_be_visible()

    # The status and audience filters wait for Apply too.
    status = page.get_by_label("Status", exact=True)
    pick(status, "showing")
    page.evaluate(
        "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
    )
    assert "status=" not in page.url
    page.get_by_role("button", name="Apply", exact=True).click()
    expect(page).to_have_url(re.compile(r"[?&]state=showing(?:&|$)"))


@pytest.mark.parametrize("role", ["admin", "viewer"])
def test_staff_shell_fits_enlarged_text(page: Page, sign_in_as, role):
    sign_in_as(role)
    page.goto("/repository/record-retention/")
    page.evaluate("document.documentElement.style.fontSize = '175%'")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
        "Page overflows at 175% text"
    )
    assert page.locator(".vr-topbar-end").evaluate(
        "el => el.getBoundingClientRect().right <= innerWidth"
    )
    assert page.locator(".vr-sidenav .vr-navlink").evaluate_all(
        "els => els.every(el => el.scrollWidth <= el.clientWidth)"
    ), "Sidebar labels clip at 175% text"


@pytest.mark.parametrize("width,height", [(1366, 860), (820, 1180), (390, 844)])
def test_form_opened_by_address_is_a_dialog_over_its_list_with_clean_edges(
    page: Page, sign_in_as, width, height
):
    sign_in_as("admin")
    page.set_viewport_size({"width": width, "height": height})
    page.goto("/repository/announcements/new/")
    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()
    expect(dialog.locator("#vr-form-title")).to_have_text("New announcement")
    expect(dialog).to_have_attribute("aria-modal", "true")
    # The page behind is the list it belongs to: one main, the list's, outside the dialog.
    expect(page.get_by_role("heading", level=1, name="Announcements")).to_be_attached()
    expect(dialog.get_by_role("main")).to_have_count(0)
    body = dialog.locator("#vr-form-content")
    body.evaluate("el => el.scrollTop = el.scrollHeight / 2")
    actions = dialog.locator(".vr-form-actions")
    assert actions.evaluate(
        "el => Math.abs(el.getBoundingClientRect().bottom - "
        "el.closest('.modal-body').getBoundingClientRect().bottom) <= 1"
    ), "The dialog action row leaves a strip of content underneath"
    assert page.console_errors == []
