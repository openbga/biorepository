"""Every platform page loads under the enforced policy: headers present, no inline script/style."""

import re

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse
from helpers import make_announcement, make_user, sign_in
from playwright.sync_api import expect

pytestmark = pytest.mark.django_db(transaction=True)


def assert_policy(response, status=200):
    assert response.status == status, response.url
    policy = response.headers["content-security-policy"]
    assert "script-src 'self'" in policy
    assert "unsafe-inline" not in policy
    assert "unsafe-eval" not in policy
    assert "camera=()" in response.headers["permissions-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"


def assert_no_inline_code(page, html):
    """``html`` is the served markup: script may set style properties through the CSSOM at run
    time (allowed), but the server must not send style attributes or inline handlers."""
    assert not re.search(r"<[^>]+\s(style|on[a-z]+)\s*=", html), "inline style or handler"
    # json_script data blocks and the presentation's structured data are inert, not script.
    executable = page.locator(
        'script:not([src]):not([type="application/json"]):not([type="application/ld+json"])'
    )
    assert executable.count() == 0, "inline script element"
    assert page.locator("style").count() == 0, "inline style element"


def test_anonymous_pages_load_under_the_policy(page, demo):
    make_announcement("Synthetic sign-in notice")
    for path in ("/accounts/login/", "/about/", "/health/", "/ready/", "/no-such-page/"):
        response = page.goto(path)
        assert_policy(response, 404 if "no-such" in path else 200)
        if "html" in response.headers.get("content-type", ""):
            assert_no_inline_code(page, response.text())
    # The entrance page keeps working scripts: the theme menu opens without a violation.
    page.goto("/accounts/login/")
    page.get_by_role("button", name="Theme", exact=True).click()
    expect(page.get_by_role("button", name="Dark", exact=True)).to_be_visible()


def test_staff_pages_dialogs_and_help_load_under_the_policy(page, demo):
    make_announcement("Synthetic staff notice")
    sign_in(page, "demo_admin", demo.password)
    assert page.evaluate("htmx.config.allowEval") is False
    assert page.evaluate("htmx.config.includeIndicatorStyles") is False
    for route in (
        reverse("dashboard"),
        reverse("record-retention"),
        reverse("announcement-list"),
        reverse("announcement-create"),
        reverse("account_change_password"),
        reverse("account_email"),
        reverse("mfa_index"),
        reverse("mfa_activate_totp"),
    ):
        response = page.goto(route)
        assert_policy(response)
        expect(page.locator("body")).to_be_visible()
        # A form route opened by address replaces itself with the page it belongs to, so its
        # served HTML is read through a request with the same session.
        assert_no_inline_code(page, page.request.get(route).text())
        assert page.console_errors == [], f"{route}: {page.console_errors}"
    # Dialog, help panel and confirm dialog run entirely from external scripts.
    page.goto(reverse("announcement-list"))
    page.get_by_role("link", name="New announcement", exact=True).first.click()
    expect(page.locator("#vr-form-dialog")).to_be_visible()
    page.locator("#vr-form-dialog [data-vr-documentation-toggle]").click()
    expect(page.locator("#vr-documentation")).to_be_visible()
    page.keyboard.press("Escape")
    page.goto(reverse("announcement-list"))
    page.get_by_role("button", name=re.compile("^Withdraw")).first.click()
    expect(page.get_by_role("dialog", name=re.compile("^Withdraw announcement"))).to_be_visible()
    assert page.console_errors == []


def test_admin_pages_use_local_scripts(page, demo):
    owner_password = "synthetic-owner-pass-1"
    owner = make_user("e2e_policy_owner", owner_password, "admin", superuser=True)
    sign_in(page, owner.username, owner_password)
    group = Group.objects.get(name="manager")
    routes = [
        reverse("admin:index"),
        reverse("admin:accounts_user_changelist"),
        reverse("admin:accounts_user_add"),
        reverse("admin:accounts_user_change", args=[owner.pk]),
        reverse("admin:auth_group_changelist"),
        reverse("admin:auth_group_change", args=[group.pk]),
        reverse("admin:core_repository_change", args=[1]),
    ]
    for route in routes:
        assert_policy(page.goto(route))
        expect(page.locator("body")).to_be_visible()
        # Admin filter_horizontal widgets are initialized by the external SelectFilter script.
        if route == reverse("admin:auth_group_change", args=[group.pk]):
            expect(page.locator("#id_permissions_from")).to_be_visible()
            page.locator("#id_permissions_input").fill("disposal")
        assert page.console_errors == [], f"{route}: {page.console_errors}"


def test_csrf_failure_page_has_no_inline_policy_violation(page, demo):
    page.goto("/accounts/login/")
    page.locator('[name="csrfmiddlewaretoken"]').evaluate("element => element.remove()")
    with page.expect_response(
        lambda response: response.request.method == "POST" and "/accounts/login/" in response.url
    ) as refused:
        sign_in(page, "demo_viewer", demo.password, visit=False)
    assert refused.value.status == 403
    expect(page.get_by_role("heading", name="The form could not be submitted")).to_be_visible()
    expect(page.get_by_text("This form could not be verified.", exact=False)).to_be_visible()
    assert page.locator("style").count() == 0
