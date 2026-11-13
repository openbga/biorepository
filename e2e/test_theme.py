"""Light/dark/system theme: choice, persistence across pages, live system following, legibility."""

from datetime import timedelta

import pytest
from django.utils import timezone
from helpers import contrast_failures, make_announcement, sign_in, sign_out, theme_samples
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)

# Records which theme the root element carried when <body> first existed, i.e. before first
# paint of content and before any deferred script ran.
THEME_AT_BODY = """
new MutationObserver((_, observer) => {
  if (document.body) {
    window.__themeAtBody = document.documentElement.getAttribute('data-bs-theme');
    observer.disconnect();
  }
}).observe(document, {childList: true, subtree: true});
"""


def root_theme(page: Page) -> str:
    return page.evaluate("document.documentElement.getAttribute('data-bs-theme')")


def choose_theme(page: Page, label: str) -> None:
    page.get_by_role("button", name="Theme", exact=True).click()
    page.get_by_role("button", name=label, exact=True).click()


def stored_choice(page: Page):
    return page.evaluate("localStorage.getItem('vr-theme')")


def sample_page(page: Page, path: str, theme: str, failures: list) -> dict:
    """Visit ``path`` and record contrast failures in the current theme."""
    page.goto(path)
    samples = theme_samples(page)
    assert samples["scheme"] == theme
    failures.extend(contrast_failures(samples))
    return samples


@pytest.mark.browser_context_args(color_scheme="dark")
def test_theme_choice_persists_follows_system_and_stays_legible(page: Page, demo):
    page.add_init_script(THEME_AT_BODY)
    make_announcement("Synthetic notice on sign-in", tone="warning")

    # No saved choice: the system preference (dark) applies before content paints.
    page.goto("/accounts/login/")
    assert page.evaluate("window.__themeAtBody") == "dark"
    assert stored_choice(page) is None
    failures = contrast_failures(theme_samples(page))  # sign-in, announcement beside the form

    # An explicit Light choice beats the dark system setting and survives sign-in and reload.
    choose_theme(page, "Light")
    assert root_theme(page) == "light"
    assert stored_choice(page) == "light"
    failures += contrast_failures(theme_samples(page))
    sign_in(page, "demo_admin", demo.password, visit=False)
    expect(page.get_by_role("heading", level=1, name="Dashboard")).to_be_visible()
    assert page.evaluate("window.__themeAtBody") == "light"
    light = theme_samples(page)
    page.reload()
    assert page.evaluate("window.__themeAtBody") == "light"

    # Dark, chosen explicitly while the system is light, is also kept across pages.
    page.emulate_media(color_scheme="light")
    choose_theme(page, "Dark")
    assert root_theme(page) == "dark"
    page.get_by_role("link", name="Record retention", exact=True).click()
    expect(page.get_by_role("heading", level=1, name="Record retention")).to_be_visible()
    assert page.evaluate("window.__themeAtBody") == "dark"
    failures += contrast_failures(theme_samples(page))
    page.get_by_role("link", name="Dashboard", exact=True).click()
    dark = theme_samples(page)

    # Printing from dark mode uses the light paper palette, without the ruled background.
    page.emulate_media(media="print")
    printed = theme_samples(page)
    assert page.evaluate("getComputedStyle(document.body).backgroundImage") == "none"
    page.emulate_media(media="screen")

    # System follows the browser preference live, without reloading.
    choose_theme(page, "System")
    assert stored_choice(page) == "system"
    assert root_theme(page) == "light"
    page.emulate_media(color_scheme="dark")
    expect(page.locator("html")).to_have_attribute("data-bs-theme", "dark")
    page.get_by_role("button", name="Theme", exact=True).click()
    expect(page.get_by_role("button", name="System", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )
    page.keyboard.press("Escape")

    # Legibility in both themes: WCAG AA text contrast (4.5:1) and non-text contrast (3:1).
    assert light["scheme"] == "light" and dark["scheme"] == "dark"
    assert light["pairs"]["body text"]["candidates"] != dark["pairs"]["body text"]["candidates"]
    failures += contrast_failures(light) + contrast_failures(dark) + contrast_failures(printed)
    assert printed["pairs"]["body text"]["candidates"] == [[[0, 0, 0], [255, 255, 255]]]

    # Keyboard focus is visibly indicated in the current (dark) theme.
    page.keyboard.press("Tab")
    focus = page.evaluate(
        """() => { const el = document.activeElement, s = getComputedStyle(el);
        return {tag: el.tagName, outline: s.outlineStyle, width: parseFloat(s.outlineWidth),
                shadow: s.boxShadow}; }"""
    )
    assert focus["outline"] != "none" and focus["width"] >= 2 or focus["shadow"] != "none", focus
    assert page.console_errors == []
    assert not failures, "\n".join(dict.fromkeys(failures))
    sign_out(page)


def test_contrast_on_list_empty_state_and_form_dialog_in_both_themes(page: Page, demo):
    sign_in(page, "demo_admin", demo.password)
    failures: list[str] = []

    def use_theme(theme: str):
        page.goto("/")
        choose_theme(page, theme.capitalize())
        assert root_theme(page) == theme

    # Empty state of the announcements list, before any announcement exists.
    for theme in ("light", "dark"):
        use_theme(theme)
        samples = sample_page(page, "/repository/announcements/", theme, failures)
        expect(page.locator(".vr-empty-title")).to_be_visible()
        assert "empty state title" in samples["pairs"]

    make_announcement("Synthetic showing notice", tone="info")
    make_announcement("Synthetic important notice", tone="warning", show_on_sign_in=False)
    make_announcement("Synthetic ended notice", ends_at=timezone.now() - timedelta(minutes=5))
    for theme in ("light", "dark"):
        use_theme(theme)
        samples = sample_page(page, "/repository/announcements/", theme, failures)
        assert "table cell text" in samples["pairs"] and "table link" in samples["pairs"]
        assert "status text" in samples["pairs"]

        # Dashboard (staff announcements, empty queue states) and record retention.
        samples = sample_page(page, "/", theme, failures)
        assert "announcement notice" in samples["pairs"]
        sample_page(page, "/repository/record-retention/", theme, failures)

        # The shared form dialog, opened from the list.
        page.goto("/repository/announcements/")
        page.get_by_role("link", name="New announcement", exact=True).first.click()
        expect(page.locator("#vr-form-dialog .modal-title")).to_have_text("New announcement")
        expect(page.locator("#vr-form-dialog")).to_be_visible()
        samples = theme_samples(page)
        failures.extend(contrast_failures(samples))
        assert "dialog title" in samples["pairs"]
        page.keyboard.press("Escape")
        expect(page.locator("#vr-form-dialog")).to_be_hidden()
    assert page.console_errors == []
    assert not failures, "\n".join(dict.fromkeys(failures))
