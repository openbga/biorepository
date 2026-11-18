"""Route coverage is a release gate; help stays inside each authorized page response."""

from pathlib import Path

import pytest
from django.contrib.auth.models import Group
from django.template.loader import render_to_string
from django.test import RequestFactory
from django.urls import URLResolver, get_resolver, resolve

from accounts.models import User
from core.page_docs import (
    document_for_route,
    documentation_for_request,
    read_documents,
)

# Each exclusion is deliberately named: a newly mounted page must acquire help.
# Actions/downloads are explained on their invoking page, not a second endpoint.
NON_PAGE_ROUTES = {
    "health": "JSON process health probe",
    "ready": "JSON database readiness probe",
    "mfa_download_recovery_codes": "Private text download from recovery-code help",
    "admin:login": "Redirect to the application sign-in page",
    "admin:autocomplete": "JSON related-object lookup",
    "admin:jsi18n": "JavaScript translation asset",
    "admin:view_on_site": "Redirect to the object's application URL",
    "admin:accounts_user_delete": "Deletion disabled by AccountAdmin",
    "admin:mfa_authenticator_add": "Creation disabled by AuthenticatorAdmin",
    "admin:account_emailaddress_add": "Creation disabled by EmailAddressAdmin",
    "admin:account_emailaddress_delete": "Deletion disabled by EmailAddressAdmin",
    "admin:auth_group_delete": "Deletion disabled by RoleGroupAdmin",
    "admin:core_repository_add": "Creation disabled by RepositoryAdmin",
    "admin:core_repository_delete": "Deletion disabled by RepositoryAdmin",
    "announcement-withdraw": "POST action on the announcement list",
    "storage:export": "CSV download from the storage lists",
    "storage:catalogue-export": "CSV download from the storage catalogue lists",
    "specimens:lookup": "Redirect from the top-bar specimen lookup",
    "specimens:export": "CSV download from the specimen list",
    "specimens:label-pdf": "PDF download from the label preview",
    "specimens:catalogue-export": "CSV download from the specimen catalogue lists",
    "specimens:import-commit": "POST action on the import page",
    "specimens:import-template": "CSV template download from the import upload form",
    "equipment:attachment": "File download from the equipment and excursion pages",
    "quality:audit-export": "CSV download from the inventory audit register",
    "quality:deviation-export": "CSV download from the deviation register",
    "quality:training-export": "CSV download from the training register",
    "equipment:export": "CSV download from the equipment lists",
    "distribution:pick-list-pdf": "PDF download from the pick list preview",
    "distribution:manifest-pdf": "PDF download from the manifest preview",
    "distribution:attachment": "File download from the shipment, consent and agreement pages",
    "distribution:export": "CSV download from the request, shipment and return lists",
    "distribution:catalogue-export": "CSV download from the distribution lists and catalogues",
}


def named_routes(patterns, namespace=""):
    routes = set()
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            prefix = namespace + (pattern.namespace + ":" if pattern.namespace else "")
            routes |= named_routes(pattern.url_patterns, prefix)
        elif pattern.name:
            routes.add(namespace + pattern.name)
    return routes


def test_every_page_route_has_documentation_and_exclusions_are_current():
    routes = named_routes(get_resolver().url_patterns)
    covered = {route for doc in read_documents() for route in doc["routes"]}
    assert not routes - covered - NON_PAGE_ROUTES.keys(), "Add page help for these routes"
    assert not NON_PAGE_ROUTES.keys() - routes, "Remove stale non-page exclusions"
    assert not covered - routes - {"documentation:shared", "documentation:unavailable"}


def test_content_has_valid_sources_and_no_ambiguous_variant_matches():
    root = Path(__file__).resolve().parent.parent
    docs = read_documents()  # Also validates shape, nonempty entries and duplicate mappings.
    assert docs
    for doc in docs:
        for source in doc["sources"]:
            assert (root / source).is_file(), (doc["id"], source)
        # Two equally-specific predicates for one route must not overlap.
        for other in docs:
            if doc["id"] >= other["id"] or not set(doc["routes"]) & set(other["routes"]):
                continue
            left, right = doc.get("match", {}), other.get("match", {})
            if len(left) == len(right):
                assert any(k in right and right[k] != v for k, v in left.items()), (
                    doc["id"],
                    other["id"],
                )


def test_variant_help_resolves_only_for_its_own_url_arguments():
    for doc in read_documents():
        match = doc.get("match", {})
        if not match:
            continue
        for route in doc["routes"]:
            assert document_for_route(route, {**match, "pk": 1})["id"] == doc["id"]
            other = {key: "not-a-real-value" for key in match}
            assert document_for_route(route, {**other, "pk": 1}) != doc


def test_documentation_is_escaped_and_evidence_is_not_exposed():
    content = {
        "id": "safe",
        "title": "<script>bad()</script>",
        "purpose": "<img src=x>",
        "sources": ["private-evidence-path"],
        "sections": [
            {
                "title": "<b>Heading</b>",
                "paragraphs": ["<iframe>"],
                "items": [{"label": "<i>Field</i>", "description": "<svg onload=x>"}],
            }
        ],
    }
    html = render_to_string("documentation/content.html", {"page_documentation": content})
    for unsafe in ("<script>", "<img", "<iframe>", "<svg", "private-evidence-path"):
        assert unsafe not in html
    assert "&lt;script&gt;" in html


def test_paragraph_only_sections_do_not_render_empty_dictionary_items_tables():
    paragraph_only = next(
        section
        for doc in read_documents()
        for section in doc["sections"]
        if section["paragraphs"] and not section["items"]
    )
    html = render_to_string(
        "documentation/content.html",
        {"page_documentation": {"title": "Notes", "sections": [paragraph_only]}},
    )
    assert "<table" not in html
    assert paragraph_only["title"] in html


@pytest.mark.django_db
def test_error_pages_replace_resolved_page_help_and_500_needs_no_database(
    django_assert_num_queries,
):
    from django.contrib.auth.models import AnonymousUser
    from django.views.defaults import page_not_found, permission_denied, server_error

    request = RequestFactory().get("/repository/record-retention/")
    request.user = AnonymousUser()
    request.resolver_match = resolve(request.path)
    for view in (permission_denied, page_not_found):
        response = view(request, Exception("Synthetic error"))
        assert b'data-vr-documentation-id="core-unavailable"' in response.content
        assert b'data-vr-documentation-id="core-retention"' not in response.content
    with django_assert_num_queries(0):
        response = server_error(request)
    assert b"data-vr-documentation-toggle" in response.content
    assert b'data-vr-documentation-id="core-unavailable"' in response.content


def test_request_help_uses_route_not_query_or_record_data():
    request = RequestFactory().get(
        "/repository/record-retention/?documentation=admin:accounts_user_change"
    )
    request.resolver_match = resolve("/repository/record-retention/")
    data = documentation_for_request(request)
    assert data["id"] == document_for_route("record-retention")["id"]
    assert set(data) == {"id", "title", "purpose", "sections"}
    request.resolver_match = None
    assert documentation_for_request(request)["id"] == "core-unavailable"


def _staff_user(name, role, *, superuser=False):
    user = User.objects.create_user(name, f"{name}@example.invalid", "test-password-729!")
    user.groups.add(Group.objects.get_or_create(name=role)[0])
    if superuser:
        user.is_staff = user.is_superuser = True
        user.save()
    return user


@pytest.mark.django_db
def test_help_follows_authorized_page_and_form_fragments(client):
    client.force_login(_staff_user("docs-admin", "admin"))
    for url, route in (
        ("/", "dashboard"),
        ("/repository/record-retention/", "record-retention"),
        ("/repository/announcements/", "announcement-list"),
        ("/repository/announcements/new/", "announcement-create"),
    ):
        response = client.get(url)
        assert response.status_code == 200
        assert response.context["page_documentation"]["id"] == document_for_route(route)["id"]
    fragment = client.get("/repository/announcements/new/", HTTP_X_VR_FORM="1")
    assert b"data-vr-documentation-source" in fragment.content
    client.force_login(_staff_user("docs-viewer", "viewer"))
    assert client.get("/repository/announcements/").status_code == 403


@pytest.mark.django_db
def test_account_and_administration_screens_include_help(client):
    response = client.get("/accounts/login/")
    assert response.context["page_documentation"]["id"] == "accounts-login"
    client.force_login(_staff_user("docs-owner", "admin", superuser=True))
    response = client.get("/admin/accounts/user/")
    assert response.status_code == 200
    assert response.context["page_documentation"]["id"] == "admin-users"
    assert b"data-vr-documentation-toggle" in response.content
    assert b'id="vr-documentation"' in response.content
