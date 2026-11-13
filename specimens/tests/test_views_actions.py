"""Specimen action forms and the derivation entry grid, through the views (synthetic data only)."""

from decimal import Decimal

import pytest
from django.contrib.messages import get_messages
from django.urls import reverse
from django.utils import timezone

from specimens import services
from specimens.models import (
    CustodyEvent,
    DisposalDecision,
    Hold,
    Specimen,
    ThawEvent,
)
from storage.models import Occupancy

from .conftest import ago, check_out, place, register

pytestmark = pytest.mark.django_db

REQUESTER_SENTENCE = "You requested this. Another person with disposal approval must approve it."


def stamp(value):
    return timezone.localtime(value).strftime("%Y-%m-%dT%H:%M:%S")


def position_of(specimen):
    return Occupancy.objects.get(pk=specimen.occupancy_id).position


@pytest.fixture
def things(people, world):
    """A stored specimen, a checked-out one with a consumption entry and a hold, a request."""
    tech, manager = people["technician"], people["manager"]
    stored = register(tech, world)
    place(tech, stored, world.position("A1"))
    out = register(tech, world)
    place(tech, out, world.position("A2"))
    check_out(tech, out, tech)
    out.refresh_from_db()
    entry = services.consume_quantity(
        actor=tech,
        specimen_id=out.pk,
        expected_revision=out.revision,
        quantity="0.1",
        supplied_text="0.1",
        occurred_at=ago(150),
        reason="Tested",
    )
    out.refresh_from_db()
    hold = services.place_hold(
        actor=manager,
        specimen_id=out.pk,
        expected_revision=out.revision,
        scope=Hold.Scope.DISPOSAL,
        occurred_at=ago(140),
        reason="Study query",
    )
    stored.refresh_from_db()
    request = services.request_disposal(
        actor=tech,
        specimen_id=stored.pk,
        expected_revision=stored.revision,
        method="Autoclave",
        destination="Clinical waste",
        reason="Consent period ended",
    )
    for obj in (stored, out):
        obj.refresh_from_db()
    return {
        "stored": stored,
        "out": out,
        "entry": entry,
        "hold": hold,
        "thaw": stored.thaws.get(kind=ThawEvent.Kind.BASELINE),
        "custody": stored.custody.get(kind=CustodyEvent.Kind.REGISTER),
        "request": request,
    }


ROUTES = [
    # (route, object key or None, needs manager; None: decided by disposal approval)
    ("specimens:specimen-create", None, False),
    ("specimens:place", "stored", False),
    ("specimens:relocate", "stored", False),
    ("specimens:check-out", "stored", False),
    ("specimens:check-in", "out", False),
    ("specimens:transfer", "stored", False),
    ("specimens:consume", "out", False),
    ("specimens:adjust", "out", True),
    ("specimens:derive", "out", False),
    ("specimens:hold-add", "stored", False),
    ("specimens:thaw-record", "stored", False),
    ("specimens:correct", "stored", True),
    ("specimens:disposal-request", "stored", False),
    ("specimens:reverse", "entry", True),
    ("specimens:hold-release", "hold", True),
    ("specimens:custody-correct", "custody", True),
    ("specimens:thaw-correct", "thaw", True),
    ("specimens:disposal-decide", "request", None),
    ("specimens:disposal-execute", "request", False),
]


def route_url(name, key, things):
    return reverse(name, args=[things[key].pk]) if key else reverse(name)


@pytest.mark.parametrize("name,key,manage", ROUTES)
def test_every_action_opens_for_its_role_and_is_denied_otherwise(
    client, people, things, name, key, manage
):
    url = route_url(name, key, things)
    client.force_login(people["manager"])
    response = client.get(url)
    assert response.status_code == 200
    assert b'class="vr-form' in response.content
    for who in ("viewer", "nobody"):
        client.force_login(people[who])
        assert client.get(url).status_code == 403
    client.force_login(people["technician2"])
    assert client.get(url).status_code == (200 if manage is False else 403)
    if manage is None:
        client.force_login(people["approver"])
        assert client.get(url).status_code == 200


def test_register_with_placement(client, people, world):
    client.force_login(people["technician"])
    url = reverse("specimens:specimen-create")
    page = client.get(url)
    assert page.status_code == 200
    assert "Register specimen" in page.content.decode()
    response = client.post(
        url,
        {
            "specimen_type": world.plasma.pk,
            "container_type": world.cryovial.pk,
            "quantity": "1.50",
            "unit": world.ml.pk,
            "participant": world.participant.pk,
            "collection": "",
            "collected_at": stamp(ago(60)),
            "processing_started_at": "",
            "processing_completed_at": "",
            "occurred_at": stamp(ago(30)),
            "baseline_unknown": "on",
            "custodian": people["technician"].pk,
            "box": world.box.pk,
            "position_label": "b2",
            "reason": "Received for storage",
        },
    )
    assert response.status_code == 302, response.context["form"].errors
    specimen = Specimen.objects.get(registered_by=people["technician"])
    assert response.url == reverse("specimens:specimen-detail", args=[specimen.pk])
    assert specimen.quantity_on_hand == Decimal("1.5")
    assert position_of(specimen) == world.position("B2")
    assert specimen.ledger.get().supplied_text == "1.50"
    baseline = specimen.thaws.get(kind=ThawEvent.Kind.BASELINE)
    assert baseline.count is None


def test_register_needs_a_thaw_count_or_unknown(client, people, world):
    client.force_login(people["technician"])
    response = client.post(
        reverse("specimens:specimen-create"),
        {
            "specimen_type": world.plasma.pk,
            "container_type": world.cryovial.pk,
            "quantity": "1",
            "unit": world.ml.pk,
            "collected_at": stamp(ago(60)),
            "occurred_at": stamp(ago(30)),
            "custodian": people["technician"].pk,
            "reason": "Received",
        },
    )
    assert response.status_code == 422
    assert "baseline_thaws" in response.context["form"].errors
    assert not Specimen.objects.exists()


def test_place_and_occupied_position_lands_on_the_position_field(client, people, world):
    tech = people["technician"]
    taken = register(tech, world)
    place(tech, taken, world.position("A1"))
    loose = register(tech, world)
    client.force_login(tech)
    url = reverse("specimens:place", args=[loose.pk])
    page = client.get(url)
    form = page.context["form"]
    assert "Free now: BOX-001 A2" in form.fields["position_label"].help_text
    data = {
        "box": world.box.pk,
        "position_label": "A1",
        "occurred_at": stamp(ago(5)),
        "reason": "Stored after processing",
        "expected_revision": form.initial["expected_revision"],
        "expected_occupancy_revision": form.initial["expected_occupancy_revision"],
    }
    response = client.post(url, data)
    assert response.status_code == 422
    errors = response.context["form"].errors
    assert "occupied" in " ".join(errors["position_label"])
    assert position_of(loose) is None
    response = client.post(url, {**data, "position_label": "Z9"})
    assert "BOX-001 has no position Z9" in " ".join(
        response.context["form"].errors["position_label"]
    )
    response = client.post(url, {**data, "position_label": "A3"})
    assert response.status_code == 302
    assert position_of(loose) == world.position("A3")


def test_check_out_then_check_in_with_thaw_yes_and_unknown(client, people, world):
    tech = people["technician"]
    specimen = register(tech, world)
    place(tech, specimen, world.position("A1"))
    client.force_login(tech)

    def out(minutes):
        url = reverse("specimens:check-out", args=[specimen.pk])
        initial = client.get(url).context["form"].initial
        response = client.post(
            url,
            {
                "recipient": tech.pk,
                "destination": "Processing bench 2",
                "occurred_at": stamp(ago(minutes)),
                "reason": "Aliquoting",
                "expected_revision": initial["expected_revision"],
                "expected_occupancy_revision": initial["expected_occupancy_revision"],
            },
        )
        assert response.status_code == 302

    def back(minutes, thaw, label):
        url = reverse("specimens:check-in", args=[specimen.pk])
        initial = client.get(url).context["form"].initial
        response = client.post(
            url,
            {
                "box": world.box.pk,
                "position_label": label,
                "recipient": tech.pk,
                "thaw_observed": thaw,
                "occurred_at": stamp(ago(minutes)),
                "reason": "Returned",
                "expected_revision": initial["expected_revision"],
                "expected_occupancy_revision": initial["expected_occupancy_revision"],
            },
        )
        return response

    out(50)
    specimen.refresh_from_db()
    assert specimen.open_checkout_id and position_of(specimen) is None
    # The thaw answer is required: no default.
    missing = back(40, "", "A1")
    assert missing.status_code == 422 and "thaw_observed" in missing.context["form"].errors
    assert back(40, "yes", "A1").status_code == 302
    specimen.refresh_from_db()
    assert not specimen.open_checkout_id
    assert specimen.thaws.filter(kind=ThawEvent.Kind.THAW).count() == 1
    out(30)
    assert back(20, "unknown", "B1").status_code == 302
    checkin = specimen.custody.filter(kind=CustodyEvent.Kind.CHECK_IN).latest("occurred_at")
    assert checkin.thaw_observed is None
    assert specimen.thaws.filter(kind=ThawEvent.Kind.THAW).count() == 1
    assert position_of(specimen) == world.position("B1")


def test_check_in_is_shown_unavailable_when_not_checked_out(client, people, world):
    tech = people["technician"]
    specimen = register(tech, world)
    place(tech, specimen, world.position("A1"))
    client.force_login(tech)
    page = client.get(reverse("specimens:check-in", args=[specimen.pk]))
    html = page.content.decode()
    assert "not checked out" in html
    assert "Record check-in</button>" not in html


def test_stale_revision_keeps_input_and_asks_to_reload(client, people, world):
    tech = people["technician"]
    specimen = register(tech, world)
    place(tech, specimen, world.position("A1"))
    client.force_login(tech)
    url = reverse("specimens:check-out", args=[specimen.pk])
    initial = client.get(url).context["form"].initial
    specimen.refresh_from_db()
    services.place_hold(  # someone else changes the specimen meanwhile
        actor=people["manager"],
        specimen_id=specimen.pk,
        expected_revision=specimen.revision,
        scope=Hold.Scope.DISPOSAL,
        occurred_at=ago(10),
        reason="Query",
    )
    response = client.post(
        url,
        {
            "recipient": tech.pk,
            "destination": "Bench 7 example",
            "occurred_at": stamp(ago(5)),
            "reason": "Aliquoting",
            "expected_revision": initial["expected_revision"],
            "expected_occupancy_revision": initial["expected_occupancy_revision"],
        },
    )
    assert response.status_code == 422
    html = response.content.decode()
    assert "Reload it" in html
    assert 'value="Bench 7 example"' in html
    specimen.refresh_from_db()
    assert not specimen.open_checkout_id


def _derive_data(page, world, rows, **top):
    form = page.context["form"]
    data = {
        "kind": "aliquot",
        "consumed": "1.0",
        "procedure": "",
        "occurred_at": stamp(ago(5)),
        "reason": "Aliquoting",
        "expected_revision": form.initial["expected_revision"],
        "expected_occupancy_revision": form.initial["expected_occupancy_revision"],
        "children-TOTAL_FORMS": str(len(rows)),
        "children-INITIAL_FORMS": str(len(rows)),
        "children-MIN_NUM_FORMS": "1",
        "children-MAX_NUM_FORMS": "50",
        **top,
    }
    for i, row in enumerate(rows):
        values = {
            "specimen_type": world.plasma.pk,
            "container_type": world.cryovial.pk,
            "quantity": "0.5",
            "unit": world.ml.pk,
            "box": "",
            "position_label": "",
            **row,
        }
        data.update({f"children-{i}-{k}": v for k, v in values.items()})
    return data


def test_derive_two_aliquots_with_positions(client, people, world):
    tech = people["technician"]
    parent = register(tech, world)
    place(tech, parent, world.position("A1"))
    check_out(tech, parent, tech)
    client.force_login(tech)
    url = reverse("specimens:derive", args=[parent.pk])
    page = client.get(url)
    assert page.status_code == 200
    formset = page.context["formset"]
    assert len(formset.forms) == 2
    assert formset.forms[0].initial["unit"] == str(world.ml.pk)
    rows = [
        {"box": world.box2.pk, "position_label": "A1"},
        {"box": world.box2.pk, "position_label": "A2"},
    ]
    response = client.post(url, _derive_data(page, world, rows))
    assert response.status_code == 302
    children = list(Specimen.objects.filter(parent=parent).order_by("accession"))
    assert len(children) == 2
    assert [position_of(c) for c in children] == [
        world.position("A1", world.box2),
        world.position("A2", world.box2),
    ]
    parent.refresh_from_db()
    assert parent.quantity_on_hand == Decimal("1")
    text = " ".join(str(m) for m in get_messages(response.wsgi_request))
    assert f"Made {children[0].accession}, {children[1].accession} from {parent.accession}" in text


def test_derive_rows_are_added_and_removed_without_javascript(client, people, world):
    tech = people["technician"]
    parent = register(tech, world)
    client.force_login(tech)  # never stored, so it is in hand
    url = reverse("specimens:derive", args=[parent.pk])
    page = client.get(url)
    rows = [{"quantity": "0.25"}, {"quantity": "0.75"}]
    added = client.post(url, {**_derive_data(page, world, rows, consumed="1"), "add_row": "1"})
    assert added.status_code == 200
    assert len(added.context["formset"].forms) == 3
    assert added.context["form"].initial["consumed"] == "1"
    assert not Specimen.objects.filter(parent=parent).exists()
    three = rows + [{"quantity": "0.5"}]
    removed = client.post(url, {**_derive_data(page, world, three), "remove_row": "0"})
    assert removed.status_code == 200
    forms = removed.context["formset"].forms
    assert [f.initial["quantity"] for f in forms] == ["0.75", "0.5"]
    assert not Specimen.objects.filter(parent=parent).exists()


def test_derive_row_errors_go_under_their_row(client, people, world):
    tech = people["technician"]
    parent = register(tech, world)
    client.force_login(tech)
    url = reverse("specimens:derive", args=[parent.pk])
    page = client.get(url)
    rows = [{"quantity": "0.5"}, {"quantity": "0.5", "specimen_type": world.serum.pk}]
    response = client.post(url, _derive_data(page, world, rows))
    assert response.status_code == 422
    forms = response.context["formset"].forms
    assert not forms[0].errors
    assert "keeps the parent's specimen type" in " ".join(forms[1].non_field_errors())


def test_derive_is_unavailable_while_stored(client, people, world):
    tech = people["technician"]
    parent = register(tech, world)
    place(tech, parent, world.position("A1"))
    client.force_login(tech)
    page = client.get(reverse("specimens:derive", args=[parent.pk]))
    html = page.content.decode()
    assert "Record a check-out before it is split" in html
    assert "Record split</button>" not in html


def test_hold_place_and_release(client, people, world):
    tech, manager = people["technician"], people["manager"]
    specimen = register(tech, world)
    client.force_login(tech)
    url = reverse("specimens:hold-add", args=[specimen.pk])
    initial = client.get(url).context["form"].initial
    response = client.post(
        url,
        {
            "scope": "all_use",
            "occurred_at": stamp(ago(5)),
            "reason": "Study query",
            "expected_revision": initial["expected_revision"],
        },
    )
    assert response.status_code == 302
    hold = Hold.objects.get(specimen=specimen)
    assert hold.scope == "all_use"
    release = reverse("specimens:hold-release", args=[hold.pk])
    assert client.get(release).status_code == 403
    client.force_login(manager)
    initial = client.get(release).context["form"].initial
    response = client.post(
        release,
        {
            "occurred_at": stamp(ago(1)),
            "reason": "Query resolved",
            "expected_revision": initial["expected_revision"],
        },
    )
    assert response.status_code == 302
    assert not services.active_holds(specimen).exists()


def test_disposal_request_decide_and_execute(client, people, world):
    tech, approver = people["technician"], people["approver"]
    specimen = register(tech, world)
    place(tech, specimen, world.position("A1"))
    client.force_login(tech)
    url = reverse("specimens:disposal-request", args=[specimen.pk])
    initial = client.get(url).context["form"].initial
    response = client.post(
        url,
        {
            "method": "Autoclave",
            "destination": "Clinical waste",
            "reason": "Consent period ended",
            "expected_revision": initial["expected_revision"],
        },
    )
    assert response.status_code == 302
    request = specimen.disposals.get()

    # The requester sees the sentence and cannot approve.
    decide = reverse("specimens:disposal-decide", args=[request.pk]) + "?decision=approve"
    page = client.get(decide)
    html = page.content.decode()
    assert page.status_code == 200
    assert REQUESTER_SENTENCE in html
    assert "Approve disposal</button>" not in html
    response = client.post(
        decide,
        {
            "decision": "approve",
            "reason": "Self",
            "expected_revision": page.context["form"].initial["expected_revision"],
        },
    )
    assert response.status_code == 409
    assert not DisposalDecision.objects.exists()

    # A technician without disposal approval may not approve.
    client.force_login(people["technician2"])
    assert client.get(decide).status_code == 403

    client.force_login(approver)
    page = client.get(decide)
    assert "Approve disposal</button>" in page.content.decode()
    response = client.post(
        decide,
        {
            "decision": "approve",
            "reason": "Checked against the consent register",
            "expected_revision": page.context["form"].initial["expected_revision"],
        },
    )
    assert response.status_code == 302
    assert response.url == reverse("specimens:disposal-detail", args=[request.pk])
    assert request.__class__.objects.get(pk=request.pk).decision.actor == approver

    client.force_login(tech)
    execute = reverse("specimens:disposal-execute", args=[request.pk])
    page = client.get(execute)
    assert "Record disposal</button>" in page.content.decode()
    initial = page.context["form"].initial
    response = client.post(
        execute,
        {
            "occurred_at": stamp(timezone.now()),
            "reason": "Autoclaved",
            "expected_revision": initial["expected_revision"],
            "expected_occupancy_revision": initial["expected_occupancy_revision"],
        },
    )
    assert response.status_code == 302, response.context["form"].errors
    specimen.refresh_from_db()
    assert specimen.status == Specimen.Status.DISPOSED
    assert position_of(specimen) is None


def test_decision_choices_offer_only_what_the_user_may_take(client, people, things):
    request = things["request"]
    url = reverse("specimens:disposal-decide", args=[request.pk])
    client.force_login(people["approver"])
    choices = dict(client.get(url).context["form"].fields["decision"].choices)
    assert set(choices) == {"approve", "reject"}
    client.force_login(people["technician"])  # the requester
    choices = dict(client.get(url).context["form"].fields["decision"].choices)
    assert set(choices) == {"cancel"}
    client.force_login(people["technician2"])
    assert client.get(url).status_code == 403


def test_correct_specimen_sends_only_changed_fields(client, people, world):
    tech, manager = people["technician"], people["manager"]
    specimen = register(tech, world)
    client.force_login(manager)
    url = reverse("specimens:correct", args=[specimen.pk])
    page = client.get(url)
    form = page.context["form"]
    data = {
        name: (stamp(value) if hasattr(value, "tzinfo") else value or "")
        for name, value in form.initial.items()
    }
    unchanged = client.post(url, {**data, "reason": "Check"})
    assert unchanged.status_code == 422
    assert "unchanged" in unchanged.content.decode()
    response = client.post(
        url,
        {
            **data,
            "container_type": world.cryovial.pk,
            "specimen_type": world.serum.pk,
            "reason": "Mislabelled at receipt",
        },
    )
    assert response.status_code == 302
    specimen.refresh_from_db()
    assert specimen.specimen_type == world.serum
    correction = specimen.corrections.get()
    assert set(correction.after) == {"specimen_type"}
