"""What's new: no indicator on a first visit, the unread indicator (static/js/whats-new.js) for a
change this browser has not seen, the list, and the indicator clearing once the list is opened."""

import re

import pytest
from helpers import make_user, sign_in
from playwright.sync_api import Page, expect

from core import changelog

pytestmark = pytest.mark.django_db(transaction=True)


FORGET = """id => {
  const key = "vr-whats-new:" + document.body.dataset.vrUser;
  const seen = JSON.parse(localStorage.getItem(key));
  seen.ids = seen.ids.filter(item => item !== id);
  localStorage.setItem(key, JSON.stringify(seen));
}"""


def test_open_whats_new_see_entries_and_the_indicator_clears(page: Page):
    entries = changelog.read_entries()
    assert entries, "changelog.d/ should carry entries"
    newest = entries[0]
    make_user("e2e_whats_new", "e2e-password-4821!", "technician")
    sign_in(page, "e2e_whats_new", "e2e-password-4821!")
    expect(page.get_by_role("heading", level=1, name="Dashboard")).to_be_visible()

    # First visit in this browser: nothing is new, and the current entries are stored as seen.
    link = page.locator(".vr-topbar").get_by_role("link", name=re.compile("^What's new"))
    expect(link).to_have_accessible_name("What's new")
    expect(link.locator(".vr-topbar-count")).to_be_hidden()
    stored = page.evaluate(
        "JSON.parse(localStorage.getItem('vr-whats-new:' + document.body.dataset.vrUser))"
    )
    assert stored == changelog.seen_marker(entries)

    # A change this browser has not seen (here: the newest entry forgotten) counts.
    page.evaluate(FORGET, newest.id)
    page.reload()
    link = page.locator(".vr-topbar").get_by_role("link", name=re.compile("^What's new"))
    expect(link).to_have_accessible_name("What's new: 1 new change")
    expect(link).to_have_attribute("title", "What's new: 1 new change")
    expect(link.locator(".vr-topbar-count")).to_have_text("1")

    link.click()
    expect(page.get_by_role("heading", level=1, name="What's new")).to_be_visible()
    month = page.get_by_role("region", name=newest.date.strftime("%B %Y"))
    expect(month.get_by_text(newest.title, exact=True)).to_be_visible()
    first_section = changelog.group_entries(entries)[0].sections[0][1]  # e.g. "High impact"
    expect(month.get_by_role("heading", level=3).first).to_have_text(first_section)
    expect(page.locator(".vr-change")).to_have_count(len(entries))  # the list shows every entry
    marked = page.locator("[data-vr-whats-new-mark]:visible")
    expect(marked).to_have_count(1)
    expect(page.locator(f'[data-vr-entry="{newest.id}"] [data-vr-whats-new-mark]')).to_be_visible()
    expect(page.get_by_role("status")).to_contain_text("1 change is new")
    expect(link).to_have_accessible_name("What's new")
    expect(link.locator(".vr-topbar-count")).to_be_hidden()

    page.goto("/")
    expect(page.locator(".vr-topbar").get_by_role("link", name="What's new")).to_be_visible()
    expect(page.locator(".vr-topbar-count")).to_be_hidden()
    assert page.console_errors == []
