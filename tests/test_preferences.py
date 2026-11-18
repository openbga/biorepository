"""Per-user presentation preferences (core/preferences.py): kept on the server, never shared."""

import json

import pytest
from django.urls import reverse

from core.models import UserPreference
from core.preferences import clean

LAYOUT = {"order": ["status", "accession"], "hidden": ["collected"]}
GRID = "grid:specimens.specimens:v1"
PAGE = "page:specimens:specimen-list"
OPEN = '<script id="vr-preferences" type="application/json">'


def post(client, key, value):
    return client.post(
        reverse("preference"),
        json.dumps({"key": key, "value": value}),
        content_type="application/json",
    )


def page_prefs(client, path):
    """The preferences a page hands to its scripts ({% client_preferences %})."""
    html = client.get(path).content.decode()
    start = html.index(OPEN) + len(OPEN)
    return json.loads(html[start : html.index("</script>", start)])


@pytest.mark.django_db
def test_grid_layout_is_saved_per_user_and_isolated(client, staff):
    first, second = staff("tech-a", "technician"), staff("tech-b", "technician")
    client.force_login(first)
    assert post(client, GRID, LAYOUT).status_code == 204
    assert page_prefs(client, reverse("specimens:specimen-list")) == {GRID: LAYOUT}
    client.force_login(second)
    assert page_prefs(client, reverse("specimens:specimen-list")) == {}  # defaults for others
    client.force_login(first)
    assert post(client, GRID, None).status_code == 204  # reset
    assert not UserPreference.objects.filter(user=first, key=GRID).exists()


@pytest.mark.django_db
def test_bad_values_are_refused_and_stored_junk_falls_back(client, staff):
    user = staff("viewer-a", "viewer")
    client.force_login(user)
    assert post(client, GRID, {"order": "accession"}).status_code == 400
    assert post(client, "grid:x:v1", {"order": ["<b>"], "hidden": []}).status_code == 400
    assert post(client, PAGE, {"per_page": 50}).status_code == 400  # set by the server only
    assert post(client, "nav.collapsed", True).status_code == 400  # no sidebar here
    assert post(client, "theme", "dark").status_code == 400
    bad = client.post(reverse("preference"), "not json", content_type="text/plain")
    assert bad.status_code == 400
    assert client.get(reverse("preference")).status_code == 405
    # Something stored in an older format is ignored, not trusted (read at the next sign-in).
    UserPreference.objects.create(user=user, key=GRID, value=["accession"])
    UserPreference.objects.create(user=user, key=PAGE, value={"per_page": 7})
    client.logout()
    client.force_login(user)
    assert page_prefs(client, reverse("specimens:specimen-list")) == {}
    response = client.get(reverse("specimens:specimen-list"))
    assert response.context["page_obj"].paginator.per_page == 25


def test_clean_accepts_only_the_declared_shapes():
    assert clean("grid:a.b:v1", LAYOUT) == LAYOUT
    assert clean("grid:a.b:v1", {**LAYOUT, "extra": 1}) is None
    assert clean(PAGE, {"per_page": 100, "sort": "-registered_at"}) == {
        "per_page": 100,
        "sort": "-registered_at",
    }
    assert clean(PAGE, {"per_page": True}) is None
    assert clean(PAGE, {"sort": "x; drop"}) is None
    assert clean("nav.collapsed", True) is None


@pytest.mark.django_db
def test_page_size_and_sort_persist_per_user_and_page(client, staff):
    user, other = staff("tech-c", "technician"), staff("tech-d", "technician")
    client.force_login(user)
    url = reverse("specimens:specimen-list")
    assert client.get(url).context["page_obj"].paginator.per_page == 25  # default
    client.get(url, {"per_page": 50, "sort": "-registered_at"})
    response = client.get(url)  # no parameters: the saved ones apply
    assert response.context["page_obj"].paginator.per_page == 50
    assert response.context["request"].GET["sort"] == "-registered_at"
    assert client.get(url, {"per_page": 100}).context["page_obj"].paginator.per_page == 100
    assert client.get(url).context["page_obj"].paginator.per_page == 100
    client.get(url, {"per_page": 9})  # invalid: refused by the view, never saved
    assert UserPreference.objects.get(user=user, key=PAGE).value == {
        "per_page": 100,
        "sort": "-registered_at",
    }
    client.force_login(other)
    assert client.get(url).context["page_obj"].paginator.per_page == 25


@pytest.mark.django_db
def test_anonymous_requests_and_settings_read_once_per_sign_in(client, staff):
    assert post(client, GRID, LAYOUT).status_code == 302  # sign in first
    user = staff("tech-e", "technician")
    client.force_login(user)
    assert post(client, GRID, LAYOUT).status_code == 204
    client.get(reverse("dashboard"))
    UserPreference.objects.filter(user=user).delete()  # only the session copy remains
    assert page_prefs(client, reverse("dashboard")) == {GRID: LAYOUT}
    client.logout()
    client.force_login(user)  # a new sign-in reads the database again
    assert page_prefs(client, reverse("dashboard")) == {}
