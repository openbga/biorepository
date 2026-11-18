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


@pytest.mark.django_db
def test_the_endpoint_needs_a_csrf_token_and_bounds_its_body(staff):
    from django.test import Client

    from core.preferences import MAX_BODY

    strict = Client(enforce_csrf_checks=True)
    strict.force_login(staff("tech-f", "technician"))
    refused = strict.post(
        reverse("preference"), json.dumps({"key": GRID, "value": LAYOUT}), "application/json"
    )
    assert refused.status_code == 403
    assert not UserPreference.objects.exists()
    big = json.dumps({"key": GRID, "value": {"order": ["a"] * MAX_BODY, "hidden": []}})
    client = Client()
    client.force_login(staff("tech-g", "technician"))
    assert client.post(reverse("preference"), big, "application/json").status_code == 400


@pytest.mark.django_db
def test_a_full_preference_store_never_breaks_a_list(client, staff):
    user = staff("tech-h", "technician")
    UserPreference.objects.bulk_create(
        UserPreference(user=user, key=f"grid:g{n}:v1", value=LAYOUT) for n in range(300)
    )
    client.force_login(user)
    response = client.get(reverse("specimens:specimen-list"), {"per_page": 50})
    assert response.status_code == 200
    assert response.context["page_obj"].paginator.per_page == 50


@pytest.mark.django_db
def test_parameters_the_view_refuses_are_never_remembered(client, staff):
    user = staff("manager-i", "manager")
    client.force_login(user)
    url = reverse("specimens:import-export")  # refuses an unknown sort with 400
    assert client.get(url, {"sort": "unknown", "per_page": 50}).status_code == 400
    assert not UserPreference.objects.filter(user=user).exists()
    assert client.get(url).status_code == 200  # nothing bad was saved: the plain request works
    assert client.get(url, {"sort": "filename"}).status_code == 200  # accepted: remembered
    assert UserPreference.objects.get(user=user).value == {"sort": "filename"}
    assert client.get(url).status_code == 200


@pytest.mark.django_db
def test_a_sort_a_list_refuses_on_its_own_page_is_never_remembered(client, staff):
    user = staff("manager-j", "manager")
    client.force_login(user)
    url = reverse("specimens:disposal-list")  # refuses an unknown sort with a form error (200)
    response = client.get(url, {"sort": "unknown", "per_page": 50})
    assert response.status_code == 200 and "Choose Request or Specimen to sort." in response.text
    assert UserPreference.objects.get(user=user).value == {"per_page": 50}  # only what applied
    assert "Choose Request or Specimen" not in client.get(url).text
    # A saved sort the list no longer accepts (a removed column) is dropped, not kept failing.
    UserPreference.objects.filter(user=user).update(value={"per_page": 50, "sort": "gone"})
    client.logout()
    client.force_login(user)
    assert "Choose Request or Specimen" in client.get(url).text
    assert UserPreference.objects.get(user=user).value == {"per_page": 50}
    assert "Choose Request or Specimen" not in client.get(url).text


@pytest.mark.django_db
def test_views_that_do_not_take_grid_parameters_save_nothing(client, staff):
    user = staff("tech-k", "technician")
    client.force_login(user)
    response = client.get(reverse("dashboard"), {"sort": "accession", "per_page": 50})
    assert response.status_code == 200
    assert not UserPreference.objects.filter(user=user).exists()
