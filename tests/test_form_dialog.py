"""Dialog transport retains normal form validation, access checks and redirect semantics."""

import json

import pytest
from django.contrib.messages import get_messages
from django.http import HttpResponseRedirect
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from core.form_dialog import FormDialogMiddleware
from core.models import Announcement


@pytest.mark.parametrize("enhanced", [False, True])
def test_redirect_to_next_form_keeps_native_or_dialog_transport(enhanced):
    destination = reverse("announcement-create") + "?next=another"
    request = RequestFactory().post("/", headers={"X-VR-Form": "1"} if enhanced else {})
    response = FormDialogMiddleware(lambda request: HttpResponseRedirect(destination))(request)
    assert response.status_code == (204 if enhanced else 302)
    if enhanced:
        location = json.loads(response["HX-Location"])
        assert location == {
            "path": destination,
            "target": "#vr-form-content",
            "push": "false",
            "headers": {"X-VR-Form": "1"},
        }
        assert "Location" not in response
    else:
        assert response["Location"] == destination
    assert "X-VR-Form" in response["Vary"]


@pytest.mark.django_db
def test_form_fragment_retains_validation_and_success_message(client, staff):
    client.force_login(staff("dialog_admin", "admin"))
    url = reverse("announcement-create")
    ordinary = client.get(url)
    assert b"<!doctype html>" in ordinary.content
    assert b"data-vr-form-page" in ordinary.content
    fragment = client.get(url, headers={"X-VR-Form": "1"})
    assert fragment.status_code == 200
    assert b"<!doctype html>" not in fragment.content
    assert b"data-vr-form-page" in fragment.content
    assert b"csrfmiddlewaretoken" in fragment.content
    now = timezone.localtime().strftime("%Y-%m-%dT%H:%M")
    data = {"title": "", "body": "Synthetic body kept", "tone": "info", "starts_at": now}
    invalid = client.post(url, data, headers={"X-VR-Form": "1"})
    assert invalid.status_code in (200, 422)
    assert b"Synthetic body kept" in invalid.content
    assert b'aria-invalid="true"' in invalid.content
    assert not Announcement.objects.exists()
    saved = client.post(
        url,
        data | {"title": "Synthetic dialog notice", "audiences": ["staff"]},
        headers={"X-VR-Form": "1"},
    )
    assert saved.status_code == 204
    assert saved["HX-Redirect"] == reverse("announcement-list")
    assert Announcement.objects.get().title == "Synthetic dialog notice"
    destination = client.get(saved["HX-Redirect"])
    assert "Announcement posted." in [
        str(message) for message in get_messages(destination.wsgi_request)
    ]


@pytest.mark.django_db
def test_dialog_header_does_not_bypass_access_or_csrf(client, staff):
    url = reverse("announcement-create")
    response = client.get(url, headers={"X-VR-Form": "1"})
    assert response.status_code == 204
    assert response["HX-Redirect"].startswith(reverse("account_login"))
    client.force_login(staff("dialog_technician", "technician"))
    denied = client.get(url)
    assert denied.status_code == 403
    assert b"data-vr-form-page" not in denied.content
    assert client.get(url, headers={"X-VR-Form": "1"}).status_code == 403
    assert client.post(url, {"title": "Forbidden"}, headers={"X-VR-Form": "1"}).status_code == 403
    assert not Announcement.objects.exists()
    from django.test import Client as HttpClient

    strict_client = HttpClient(enforce_csrf_checks=True)
    strict_client.force_login(staff("dialog_csrf", "admin"))
    assert (
        strict_client.post(url, {"title": "No token"}, headers={"X-VR-Form": "1"}).status_code
        == 403
    )
