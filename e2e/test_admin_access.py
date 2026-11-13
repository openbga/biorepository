"""Django admin is maintenance tooling for superusers; its sign-in is the application's."""

import re
import secrets

import pytest
from helpers import make_user, sign_in
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)


def test_anonymous_admin_visit_goes_through_the_application_sign_in(page: Page, demo):
    owner_password = secrets.token_urlsafe(18)
    make_user("e2e_owner", owner_password, "admin", superuser=True)
    response = page.goto("/admin/")
    assert response.status == 200
    expect(page).to_have_url(re.compile(r"/accounts/login/\?next=/admin/$"))
    expect(page.get_by_role("heading", level=1, name="Sign in")).to_be_visible()
    # The admin's own login URL never shows Django's password-only form.
    page.goto("/admin/login/?next=/admin/")
    expect(page).to_have_url(re.compile(r"/accounts/login/\?next=/admin/$"))
    expect(page.locator("#login-form")).to_have_count(0)
    # Signing in through the application form returns to the admin.
    sign_in(page, "e2e_owner", owner_password, visit=False)
    expect(page).to_have_url(re.compile(r"/admin/$"))
    expect(page.get_by_role("link", name="Users", exact=True)).to_be_visible()
    expect(page.get_by_role("link", name="Groups", exact=True)).to_be_visible()
    assert page.console_errors == []


@pytest.mark.parametrize("role", ["manager", "technician", "viewer"])
def test_non_staff_roles_are_refused_by_the_admin(page: Page, sign_in_as, role):
    sign_in_as(role)
    assert page.goto("/admin/").status == 403
    assert page.goto("/admin/accounts/user/").status == 403
    expect(page.get_by_role("link", name="Accounts & roles", exact=True)).to_have_count(0)


def test_admin_role_without_superuser_sees_no_models_and_cannot_open_them(page: Page, sign_in_as):
    sign_in_as("admin")
    page.goto("/")
    expect(page.get_by_role("link", name="Accounts & roles", exact=True)).to_have_count(0)
    for path in ("/admin/accounts/user/", "/admin/auth/group/", "/admin/core/repository/"):
        assert page.goto(path).status == 403, path
    page.goto("/admin/")
    expect(page.locator("#content-main a.changelink, #content-main a.addlink")).to_have_count(0)


def test_superuser_opens_the_admin_from_the_sidebar(page: Page, demo):
    password = secrets.token_urlsafe(18)
    make_user("e2e_superuser", password, "admin", superuser=True)
    sign_in(page, "e2e_superuser", password)
    page.get_by_role("link", name="Accounts & roles", exact=True).click()
    expect(page).to_have_url(re.compile(r"/admin/$"))
    for name, path in (
        ("Users", "/admin/accounts/user/"),
        ("Groups", "/admin/auth/group/"),
    ):
        assert page.goto(path).status == 200, name
    assert page.console_errors == []
