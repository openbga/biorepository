"""Navigation contract (docs/ui-standards.md section 3 and section 11).

Every named route is classified exactly once: a page that marks one sidebar item (the
mapping in core/templatetags/navigation.py), an account page in the user's shell that marks none,
an entrance page without the shell, or an endpoint that is not a page. Routes that render without
URL arguments (lists, overviews, create forms) are rendered and must mark exactly their declared
item; routes that need a record are checked through the declaration, because the same tag sets
the mark on every page from the resolved view name.
"""

import html
import re

import pytest
from django.core.exceptions import SuspiciousOperation
from django.test import RequestFactory
from django.urls import NoReverseMatch, get_resolver, reverse
from django.urls.resolvers import URLResolver
from django.views.defaults import bad_request

from core.templatetags.navigation import (
    ACCOUNT_ROUTES,
    ITEMS,
    ROUTE_ITEMS,
    SECTION_TABS,
    SHARED_ROUTES,
    STAFF_NAV,
)

pytestmark = pytest.mark.django_db

# allauth pages in the centred entrance layout: no shell, no navigation.
ENTRANCE_ROUTES = {
    "account_login",
    "account_request_login_code",
    "account_confirm_login_code",
    "account_signup",
    "account_inactive",
    "account_reset_password",
    "account_reset_password_done",
    "account_reset_password_from_key",
    "account_reset_password_from_key_done",
    "account_confirm_email",
    "account_email_verification_sent",
    "account_reauthenticate",
    "account_request_reauthentication_code",
    "account_confirm_reauthentication_code",
    "mfa_authenticate",
    "mfa_reauthenticate",
}

# Not pages: POST-only actions that redirect, and downloads, PDFs, archives and probes.
NON_PAGE_ROUTES = {
    "announcement-withdraw",
    "storage:export",
    "storage:catalogue-export",
    "specimens:lookup",
    "specimens:export",
    "specimens:label-pdf",
    "specimens:catalogue-export",
    "specimens:import-commit",
    "specimens:import-template",
    "health",
    "ready",
    "mfa_download_recovery_codes",
    "equipment:attachment",
    "quality:audit-export",
    "quality:deviation-export",
    "quality:training-export",
    "equipment:export",
    "distribution:pick-list-pdf",
    "distribution:manifest-pdf",
    "distribution:attachment",
    "distribution:export",
    "distribution:catalogue-export",
}

# Items whose label does not yet equal the title (h1) of the page they open. The page title is
# what changes (phase D); delete the entry with that change. Stale entries fail the test.
TITLE_MISMATCHES = {}

STAFF_KEYS = {item.key for item in STAFF_NAV}


@pytest.fixture(autouse=True)
def plain_static_storage(settings):
    """Render real templates without a collectstatic manifest."""
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


@pytest.fixture
def admin_user(staff):
    return staff("nav-admin", "admin")


def route_names(resolver=None, namespace=None):
    """Every named route outside Django admin, as view names (namespace:name)."""
    resolver = resolver or get_resolver()
    for pattern in resolver.url_patterns:
        if isinstance(pattern, URLResolver):
            ns = namespace
            if pattern.namespace:
                ns = f"{namespace}:{pattern.namespace}" if namespace else pattern.namespace
            if ns == "admin":
                continue
            yield from route_names(pattern, ns)
        elif pattern.name:
            yield f"{namespace}:{pattern.name}" if namespace else pattern.name


def no_argument_url(name):
    try:
        return reverse(name)
    except NoReverseMatch:
        return None


def text(fragment):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def region(content, start):
    page = content.decode()
    begin = page.find(start)
    return page[begin : page.index("</nav>", begin)] if begin >= 0 else None


def sidebar_current(response):
    nav = region(response.content, '<nav class="vr-sidenav')
    assert nav is not None, "staff shell expected"
    return [
        text(label)
        for label in re.findall(
            r'<a class="vr-navlink"[^>]*aria-current="page"[^>]*>(.*?)</a>', nav, re.S
        )
    ]


def heading(response):
    match = re.search(r"<h1\b[^>]*>(.*?)</h1>", response.content.decode(), re.S)
    return text(match.group(1)) if match else None


def test_every_route_is_classified_exactly_once():
    names = set(route_names())
    classes = {
        "item": set(ROUTE_ITEMS),
        "account": set(ACCOUNT_ROUTES),
        "entrance": ENTRANCE_ROUTES,
        "not a page": NON_PAGE_ROUTES,
    }
    problems = []
    for name in sorted(names):
        found = [label for label, members in classes.items() if name in members]
        if len(found) != 1:
            problems.append(f"{name}: {found or 'unclassified'}")
    for label, members in classes.items():
        problems += [f"{name}: declared as {label} but not a route" for name in members - names]
    # Django admin renders its own chrome; its item only links there. allauth registers the
    # request-a-code page only when emailed sign-in codes are enabled.
    optional = {
        "admin:index: declared as item but not a route",
        "account_request_login_code: declared as entrance but not a route",
        "account_request_reauthentication_code: declared as entrance but not a route",
        "account_confirm_reauthentication_code: declared as entrance but not a route",
    }
    problems = [p for p in problems if p not in optional]
    assert not problems, "\n".join(problems)


def test_shared_routes_name_declared_routes_and_other_items():
    for route, alternatives in SHARED_ROUTES.items():
        assert route in ROUTE_ITEMS, route
        assert alternatives and alternatives <= STAFF_KEYS, route
        assert ROUTE_ITEMS[route] not in alternatives, route


def test_pages_without_arguments_mark_their_declared_item(client, admin_user):
    rendered = 0
    client.force_login(admin_user)
    for name, key in ROUTE_ITEMS.items():
        url = no_argument_url(name) if not name.startswith("admin:") else None
        if url is None:
            continue  # needs a record or is Django admin
        response = client.get(url)
        assert response.status_code == 200, (name, response.status_code)
        assert sidebar_current(response) == [ITEMS[key].label], (name, sidebar_current(response))
        rendered += 1
    assert rendered >= 4  # dashboard, record retention, announcement list and create form


def test_item_labels_equal_their_page_titles(client, admin_user):
    problems = []
    client.force_login(admin_user)
    for item in STAFF_NAV:
        if item.url_name.startswith("admin:"):
            continue
        title = heading(client.get(reverse(item.url_name)))
        expected = TITLE_MISMATCHES.get(item.key, item.label)
        if title != expected:
            problems.append(f"{item.key}: label {item.label!r}, page title {title!r}")
        if item.key in TITLE_MISMATCHES and title == item.label:
            problems.append(f"{item.key}: matches now; delete it from TITLE_MISMATCHES")
    assert not problems, "\n".join(problems)


def test_sidebar_offers_only_the_items_a_role_may_use(client, staff):
    def offered(user):
        client.force_login(user)
        nav = region(client.get(reverse("dashboard")).content, '<nav class="vr-sidenav')
        return {
            text(label) for label in re.findall(r'<a class="vr-navlink"[^>]*>(.*?)</a>', nav, re.S)
        }

    assert "Announcements" in offered(staff("nav-admin-2", "admin"))
    viewer = offered(staff("nav-viewer", "viewer"))
    assert "Announcements" not in viewer and "Accounts & roles" not in viewer
    assert {"Dashboard", "Record retention"} <= viewer


def test_account_pages_keep_the_shell_and_mark_nothing(client, admin_user):
    client.force_login(admin_user)
    assert sidebar_current(client.get(reverse("account_change_password"))) == []


def test_error_pages_keep_the_shell_for_signed_in_users(client, admin_user, staff):
    missing = "/no-such-page/"
    anonymous = client.get(missing)
    assert anonymous.status_code == 404
    assert b"vr-entrance" in anonymous.content and b"vr-sidenav" not in anonymous.content
    assert heading(anonymous) == "Page not found"

    client.force_login(admin_user)
    signed_in = client.get(missing)
    assert signed_in.status_code == 404
    assert sidebar_current(signed_in) == [] and heading(signed_in) == "Page not found"

    client.force_login(staff("nav-viewer-2", "viewer"))
    denied = client.get(reverse("announcement-list"))
    assert denied.status_code == 403
    assert sidebar_current(denied) == []
    assert heading(denied) == "You do not have access to this page"

    request = RequestFactory().get("/")
    request.user = admin_user
    request.session = {}
    rejected = bad_request(request, SuspiciousOperation())
    assert rejected.status_code == 400
    assert sidebar_current(rejected) == []
    assert heading(rejected) == "The request could not be accepted"


def test_section_tabs_stay_under_one_item_and_mark_the_open_list(client, admin_user):
    client.force_login(admin_user)
    for tab_set in SECTION_TABS.values():
        owners = {ROUTE_ITEMS[tab.url_name] for tab in tab_set.tabs}
        assert len(owners) == 1 and owners <= STAFF_KEYS, tab_set.label
        for tab in tab_set.tabs:
            nav = region(client.get(reverse(tab.url_name)).content, '<nav class="vr-section-tabs"')
            current = re.findall(r'aria-current="page">(.*?)</a>', nav)
            assert current == [tab.label], (tab.url_name, current)
