"""Sign-in, role access and server-side denial, exercised through the real browser."""

import re

import pytest
from conftest import Demo
from helpers import make_announcement, sign_in, sign_out
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)

ANNOUNCEMENT_PATHS = ("/repository/announcements/", "/repository/announcements/new/")


def test_anonymous_is_sent_to_sign_in_and_returns_after_authentication(page: Page, demo: Demo):
    page.goto("/repository/record-retention/")
    expect(page).to_have_url(re.compile(r"/accounts/login/\?next=/repository/record-retention/$"))

    # A wrong password is rejected inline and keeps the typed login.
    sign_in(page, "demo_viewer", "not-the-password", visit=False)
    expect(page.get_by_role("alert")).to_contain_text("not correct")
    expect(page.get_by_label(re.compile("^(Username or email|Login)"))).to_have_value("demo_viewer")

    sign_in(page, "demo_viewer", demo.password, visit=False)
    expect(page).to_have_url(re.compile(r"/repository/record-retention/$"))
    expect(page.get_by_role("heading", level=1, name="Record retention")).to_be_visible()
    assert page.console_errors == []


@pytest.mark.parametrize("role", ["manager", "technician", "viewer"])
def test_non_admin_roles_have_no_announcements_and_the_server_refuses_them(
    page: Page, sign_in_as, role
):
    sign_in_as(role)
    expect(page.get_by_role("heading", level=1, name="Dashboard")).to_be_visible()
    expect(page.get_by_role("link", name="Announcements", exact=True)).to_have_count(0)
    expect(page.get_by_role("link", name="Record retention", exact=True)).to_be_visible()
    expect(page.get_by_role("link", name="Accounts & roles", exact=True)).to_have_count(0)
    for path in ANNOUNCEMENT_PATHS:
        response = page.goto(path)
        assert response.status == 403, path
        expect(page.get_by_role("heading", level=1)).to_have_text(
            "You do not have access to this page"
        )
    # Record retention is open to every staff role.
    assert page.goto("/repository/record-retention/").status == 200
    expect(page.get_by_role("heading", level=1, name="Record retention")).to_be_visible()
    # A forged withdraw POST with a valid CSRF token is refused and changes nothing.
    notice = make_announcement("Synthetic role-check notice")
    token = next(c["value"] for c in page.context.cookies() if c["name"] == "csrftoken")
    refused = page.request.post(
        f"/repository/announcements/{notice.pk}/withdraw/", headers={"X-CSRFToken": token}
    )
    assert refused.status == 403
    notice.refresh_from_db()
    assert notice.withdrawn_at is None
    sign_out(page)


def test_admin_sees_and_opens_announcements(page: Page, sign_in_as):
    sign_in_as("admin")
    expect(page.get_by_role("heading", level=1, name="Dashboard")).to_be_visible()
    link = page.get_by_role("link", name="Announcements", exact=True)
    expect(link).to_be_visible()
    link.click()
    expect(page).to_have_url(re.compile(r"/repository/announcements/$"))
    expect(page.get_by_role("heading", level=1, name="Announcements")).to_be_visible()
    expect(page.get_by_role("link", name="Record retention", exact=True)).to_be_visible()
    # demo_admin is not a superuser: no Django admin entry in the sidebar.
    expect(page.get_by_role("link", name="Accounts & roles", exact=True)).to_have_count(0)
    assert page.console_errors == []
    sign_out(page)


@pytest.mark.parametrize(
    "path", ["/", "/repository/announcements/", "/repository/record-retention/"]
)
def test_anonymous_visitors_are_redirected_from_every_staff_page(page: Page, path):
    page.goto(path)
    expect(page).to_have_url(re.compile(r"/accounts/login/\?next=" + re.escape(path) + "$"))
    expect(page.get_by_role("heading", level=1, name="Sign in")).to_be_visible()
