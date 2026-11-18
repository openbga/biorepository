"""Remote select lookups (core/lookups.py): every read is authorized on the server."""

import pytest
from django import forms
from django.urls import reverse

from accounts.models import User
from core.lookups import LOOKUPS, RemoteSelect
from specimens.models import Participant


@pytest.fixture
def participants(staff):
    creator = staff("registrar", "manager")
    active = Participant.objects.create(code="P-000101", created_by=creator)
    inactive = Participant.objects.create(code="P-000102", created_by=creator, is_active=False)
    other = Participant.objects.create(code="P-000203", created_by=creator)
    return active, inactive, other


def lookup(client, name, **params):
    return client.get(reverse("lookup", args=[name]), params)


def texts(response_or_payload):
    payload = getattr(response_or_payload, "json", lambda: response_or_payload)()
    return [row["text"] for row in payload["results"]]


def test_search_offers_only_active_records(client, staff, participants):
    client.force_login(staff("viewer1", "viewer"))
    assert texts(lookup(client, "active-participants")) == ["P-000101", "P-000203"]
    # Every word must match a search field; case does not matter.
    assert texts(lookup(client, "active-participants", q="p-0002")) == ["P-000203"]
    assert texts(lookup(client, "active-participants", q="nothing")) == []


def test_staff_lookup_lists_active_people_with_a_role(client, staff, db):
    client.force_login(staff("tech1", "technician"))
    User.objects.create_user("norole", "norole@example.invalid", "test-password-729!")
    gone = staff("gone", "technician")
    gone.is_active = False
    gone.save()
    assert texts(lookup(client, "staff")) == ["tech1"]
    assert texts(lookup(client, "staff", q="TECH")) == ["tech1"]


def test_pagination(client, staff, db):
    creator = staff("registrar2", "manager")
    Participant.objects.bulk_create(
        Participant(code=f"P-{n:06}", created_by=creator) for n in range(25)
    )
    client.force_login(creator)
    first = lookup(client, "active-participants").json()
    assert len(first["results"]) == LOOKUPS["active-participants"].page_size == 20
    assert first["more"]
    second = lookup(client, "active-participants", page=2).json()
    assert texts(second) == [f"P-{n:06}" for n in range(20, 25)] and not second["more"]
    assert lookup(client, "active-participants", page=9).json() == {"results": [], "more": False}


@pytest.mark.parametrize("name", sorted(LOOKUPS))
def test_every_lookup_refuses_users_without_an_active_role(client, staff, db, name):
    assert lookup(client, name).status_code == 302  # anonymous: sign in first
    nobody = User.objects.create_user("norole", "norole@example.invalid", "test-password-729!")
    client.force_login(nobody)
    assert lookup(client, name).status_code == 403
    former = staff("former", "admin")
    former.is_active = False
    former.save()
    client.force_login(former)
    assert lookup(client, name).status_code in (302, 403)
    client.force_login(staff("reader", "viewer"))
    assert lookup(client, name).status_code == 200


def test_unknown_lookup_and_method(client, staff, db):
    client.force_login(staff("viewer2", "viewer"))
    assert lookup(client, "secrets").status_code == 404
    assert client.post(reverse("lookup", args=["staff"])).status_code == 405


class ParticipantForm(forms.Form):
    participant = forms.ModelChoiceField(
        queryset=Participant.objects.all(),
        required=False,
        widget=RemoteSelect("active-participants"),
    )


def test_remote_select_renders_only_the_selected_option(participants):
    active, inactive, _ = participants
    html = " ".join(
        str(ParticipantForm(initial={"participant": inactive.pk})["participant"]).split()
    )
    assert f'data-vr-lookup="{reverse("lookup", args=["active-participants"])}"' in html
    # The edit form keeps its current value even though the lookup no longer offers it.
    assert f'<option value="{inactive.pk}" selected >P-000102</option>' in html
    assert active.code not in html
    assert html.count("<option") == 2  # the empty choice and the selection


def test_remote_select_validates_with_the_field_queryset(participants):
    active, *_ = participants
    form = ParticipantForm({"participant": str(active.pk)})
    assert form.is_valid() and form.cleaned_data["participant"] == active
    assert not ParticipantForm({"participant": "00000000-0000-0000-0000-000000000000"}).is_valid()


def test_unknown_lookup_name_fails_at_import():
    with pytest.raises(ValueError):
        RemoteSelect("no-such-lookup")
