"""Route coverage is a release gate; help stays inside each authorized page response."""

from pathlib import Path

import pytest
from django.contrib.auth.models import Group
from django.core.exceptions import ImproperlyConfigured
from django.template.loader import render_to_string
from django.test import RequestFactory
from django.urls import URLResolver, get_resolver, resolve

from accounts.models import User
from core.page_docs import (
    FRIENDLY_FIELDS,
    document_for_route,
    documentation_for_request,
    read_documents,
)

# Each exclusion is deliberately named: a newly mounted page must acquire help.
# Actions/downloads are explained on their invoking page, not a second endpoint.
NON_PAGE_ROUTES = {
    "health": "JSON process health probe",
    "ready": "JSON database readiness probe",
    "lookup": "JSON option lookup for remote selects (core/lookups.py)",
    "preference": "JSON endpoint saving the signed-in user's list layout (core/preferences.py)",
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
    "specimens:disposal-export": "CSV download from Disposal requests",
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
    # The drawer's own icons are <svg class="vr-icon">; the injected one must stay text.
    for unsafe in ("<script>", "<img", "<iframe>", "<svg onload", "private-evidence-path"):
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
    assert set(data) <= {"id", "title", "purpose", "sections", *FRIENDLY_FIELDS}
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


# ---------------------------------------------------------------- the friendly layer

FRIENDLY = {
    "summary": "Find a specimen and see where it is.",
    "tasks": [
        {"title": "Find a specimen", "steps": ["Type an ID in [[Search]].", "Select it."]},
        {"title": "Register a specimen", "steps": ["Select [[Register specimen]]."]},
    ],
    "good_to_know": ["One.", "Two.", "Three."],
}


def friendly_document(**changes):
    document = {
        "id": "friendly",
        "title": "Specimens",
        "purpose": "Specimens.",
        "routes": ["friendly"],
        "sources": ["synthetic"],
        "sections": [{"title": "Fields", "items": [{"label": "Search", "description": "Finds."}]}],
        **FRIENDLY,
    }
    document.update(changes)
    return document


def check(document):
    from core.page_docs import check_friendly

    check_friendly("synthetic.json", document)


def test_friendly_layer_is_optional_but_all_or_none():
    check(friendly_document())
    plain = friendly_document()
    for key in FRIENDLY:
        del plain[key]
    check(plain)  # no layer at all is fine
    for key in FRIENDLY:
        partial = friendly_document()
        del partial[key]
        with pytest.raises(ImproperlyConfigured, match="come together"):
            check(partial)


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"summary": "x" * 241}, "summary"),
        ({"summary": "Select [[Apply]]."}, "summary is plain text"),
        ({"tasks": FRIENDLY["tasks"][:1]}, "tasks needs 2-5"),
        ({"tasks": FRIENDLY["tasks"] * 3}, "tasks needs 2-5"),
        ({"tasks": [{"title": "x" * 81, "steps": ["a"]}, FRIENDLY["tasks"][1]]}, "title"),
        ({"tasks": [{"title": "Do", "steps": []}, FRIENDLY["tasks"][1]]}, "1-4 steps"),
        ({"tasks": [{"title": "Do", "steps": ["a"] * 5}, FRIENDLY["tasks"][1]]}, "1-4 steps"),
        ({"tasks": [{"title": "Do", "steps": ["x" * 201]}, FRIENDLY["tasks"][1]]}, "over 200"),
        ({"tasks": [{"title": "Do", "steps": [" "]}, FRIENDLY["tasks"][1]]}, "non-blank"),
        ({"good_to_know": ["One.", "Two."]}, "good_to_know needs 3-5"),
        ({"good_to_know": ["a"] * 6}, "good_to_know needs 3-5"),
        ({"good_to_know": ["One.", "Two.", "x" * 201]}, "over 200"),
        ({"good_to_know": ["Open [[Columns.", "Two.", "Three."]}, "closing"),
        ({"good_to_know": ["[[a [[b]] c]]", "Two.", "Three."]}, "closing"),
        ({"good_to_know": ["Stray ]] here.", "Two.", "Three."]}, "closing"),
    ],
)
def test_friendly_layer_limits_and_labels_are_enforced(changes, message):
    with pytest.raises(ImproperlyConfigured, match=message):
        check(friendly_document(**changes))


# The appended workspace sections are not a page of their own.
FRIENDLY_EXEMPT = {"core-shared"}


def test_every_page_has_the_friendly_layer():
    # check_friendly (in read_documents) enforces the limits and labels; this enforces presence,
    # so no page falls back to the plain purpose and an open reference.
    missing = sorted(
        document["id"]
        for document in read_documents()
        if document["id"] not in FRIENDLY_EXEMPT and "tasks" not in document
    )
    assert missing == []


def test_the_drawer_receives_the_friendly_layer_but_not_evidence():
    from core.page_docs import documentation_for_route

    help_ = documentation_for_route("specimens:specimen-list")
    assert help_["summary"] and help_["tasks"] and help_["good_to_know"]
    assert "sources" not in help_ and "routes" not in help_


def test_labels_render_as_chips_after_escaping():
    from core.templatetags.documentation import help_text

    assert help_text("Select [[New request]].") == (
        'Select <span class="vr-help-ui">New request</span>.'
    )
    assert help_text("<b>[[x]]</b>") == '&lt;b&gt;<span class="vr-help-ui">x</span>&lt;/b&gt;'
    assert help_text("Use [[<i>]].") == 'Use <span class="vr-help-ui">&lt;i&gt;</span>.'
    assert help_text("no markers") == "no markers"


def test_friendly_page_renders_summary_tasks_tips_and_collapsed_reference():
    html = render_to_string(
        "documentation/content.html", {"page_documentation": friendly_document()}
    )
    assert "Find a specimen and see where it is." in html
    assert 'href="#vr-help-tasks"' in html and 'id="vr-help-know"' in html
    assert html.count('<span class="vr-help-ui">') == 2
    assert '<details class="vr-help-ref" data-vr-help-section>' in html  # closed
    assert 'id="vr-help-filter"' in html


def test_page_without_the_layer_shows_purpose_and_open_reference():
    document = friendly_document()
    for key in FRIENDLY:
        del document[key]
    html = render_to_string("documentation/content.html", {"page_documentation": document})
    assert '<p class="vr-help-summary">Specimens.</p>' in html
    assert "vr-help-jump" not in html and "vr-help-tasks" not in html
    assert '<details class="vr-help-ref" open data-vr-help-section>' in html
    assert "<dt>Search</dt>" in html
