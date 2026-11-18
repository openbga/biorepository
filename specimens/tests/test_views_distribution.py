"""Specimen pages for distributed, returned and use-held specimens and linked holds.

States are built through the real services; the internal distribution services run inside a
storage operation holding the gate, as the distribution app calls them. All data is synthetic.
"""

import csv
import io
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.apps import apps as django_apps
from django.urls import NoReverseMatch, reverse

from specimens import services, views
from specimens.models import CustodyEvent, Hold, Specimen, ThawEvent
from storage import services as storage

from .conftest import ago, check_out, fresh, stored

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def plain_static_storage(settings):
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


def gated(fn, **kwargs):
    with storage.operation():
        storage.lock_gate()
        return fn(**kwargs)


def dispatched(tech, world):
    specimen = stored(tech, world)
    check_out(tech, specimen, tech, occurred_at=ago(200))
    gated(
        services.dispatch_specimen,
        actor=tech,
        specimen_id=specimen.pk,
        expected_revision=fresh(specimen).revision,
        destination="SH-000003 to Example University",
        occurred_at=ago(120),
        reason="Shipment dispatched",
    )
    return fresh(specimen)


def returned(tech, world):
    specimen = dispatched(tech, world)
    gated(
        services.record_return,
        actor=tech,
        specimen_id=specimen.pk,
        expected_revision=specimen.revision,
        quantity=Decimal("1.5"),
        supplied_text="1.5",
        recipient_id=tech.pk,
        thaw_count=None,
        source="Example University (SH-000003)",
        occurred_at=ago(60),
        reason="Returned unused",
    )
    return fresh(specimen)


def linked_hold(actor, specimen, scope="use", reference="Allocated to RQ-000012"):
    return gated(
        services.place_linked_hold,
        actor=actor,
        specimen_id=specimen.pk,
        scope=scope,
        reference=reference,
        occurred_at=ago(30),
        reason="Allocated to an approved request",
    )


def manual_hold(actor, specimen, scope):
    return services.place_hold(
        actor=actor,
        specimen_id=specimen.pk,
        expected_revision=fresh(specimen).revision,
        scope=scope,
        occurred_at=ago(30),
        reason="Study query",
    )


def main(response):
    """The page's <main> only: the page-help panel names every action by design."""
    page = response.content.decode()
    start = page.find("<main")
    return page[start : page.index("</main>", start)]


def detail(client, user, specimen):
    client.force_login(user)
    response = client.get(reverse("specimens:specimen-detail", args=[specimen.pk]))
    assert response.status_code == 200
    return main(response)


PHYSICAL = ("check-out", "check-in", "place", "relocate", "transfer", "consume", "derive")


def test_distributed_record_page_blocks_physical_actions(client, people, world):
    specimen = dispatched(people["technician"], world)
    page = detail(client, people["manager"], specimen)
    assert "Distributed" in page
    assert "Outside the repository" in page
    assert "Distributed: outside the repository" in page
    assert "SH-000003 to Example University" in page
    for label in (
        "Make aliquots or derivatives",
        "Record custody transfer",
        "Record consumption",
        "Record observed thaw",
        "Relocate",
        "Adjust amount",
    ):
        assert f'{label}<span class="d-block small">Distributed:' in page, label
    # Request disposal stays offered but disabled, with its reason, in the header's More menu.
    assert (
        'aria-disabled="true" data-vr-action="dispose">Request disposal'
        '<span class="d-block small">Distributed:'
    ) in page
    assert 'href="' not in page.split('data-vr-action="dispose"', 1)[1].split("</li>", 1)[0]
    assert "Record check-in" not in page and "Record check-out" not in page
    # Holds and record corrections stay available.
    assert reverse("specimens:hold-add", args=[specimen.pk]) in page
    assert reverse("specimens:correct", args=[specimen.pk]) in page
    # The dispatch row: from the custodian to outside the repository.
    dispatch = specimen.custody.get(kind=CustodyEvent.Kind.DISPATCH)
    assert dispatch.get_kind_display() in page


@pytest.mark.parametrize("route", PHYSICAL + ("thaw-record", "adjust", "disposal-request"))
def test_distributed_action_forms_are_blocked(client, people, world, route):
    specimen = dispatched(people["technician"], world)
    client.force_login(people["manager"])
    response = client.get(reverse(f"specimens:{route}", args=[specimen.pk]))
    assert response.status_code == 200
    page = main(response)
    assert "Not available" in page
    assert "Distributed: outside the repository" in page  # the context block
    if route not in ("check-in",):
        assert "was distributed" in page, route
    posted = client.post(reverse(f"specimens:{route}", args=[specimen.pk]), {"reason": "x"})
    assert posted.status_code == 409
    assert fresh(specimen).status == Specimen.Status.DISTRIBUTED


def test_distributed_hold_can_be_placed(client, people, world):
    specimen = dispatched(people["technician"], world)
    client.force_login(people["technician"])
    response = client.get(reverse("specimens:hold-add", args=[specimen.pk]))
    assert response.status_code == 200 and "Not available" not in main(response)


def test_returned_specimen_offers_check_in(client, people, world):
    specimen = returned(people["technician"], world)
    page = detail(client, people["manager"], specimen)
    assert "Returned, in hand" in page
    # The header's warm cell names the clock; the state stays in the Location fact.
    assert 'class="is-warm"><span class="vr-meta-label">Out of storage</span>' in page
    listed = client.get(reverse("specimens:specimen-list"), {"q": specimen.accession})
    assert "Returned, in hand</span> · " in listed.content.decode()
    assert "from Example University (SH-000003)" in page
    assert "Checked out since" not in page
    assert reverse("specimens:check-in", args=[specimen.pk]) in page
    assert "Returned to repository" in page and "Returned from Example University" in page
    assert ThawEvent.Kind.EXTERNAL.label in page
    assert "unknown while away" in page
    assert "Time away" in page and "not counted in time out of storage" in page
    client.force_login(people["technician"])
    response = client.get(reverse("specimens:check-in", args=[specimen.pk]))
    form = main(response)
    assert "Not available" not in form
    assert "Returned, in hand of" in form and "returned to the repository" in form


def test_use_hold_blocks_use_but_not_disposal(client, people, world):
    tech = people["technician"]
    specimen = stored(tech, world)
    manual_hold(people["manager"], specimen, Hold.Scope.USE)
    page = detail(client, people["manager"], specimen)
    assert "Use hold" in page
    assert 'disabled aria-describedby="primary-why">Record check-out' in page
    assert "An active use hold blocks this" in page
    assert 'href="' + reverse("specimens:disposal-request", args=[specimen.pk]) in page
    client.force_login(tech)
    response = client.get(reverse("specimens:check-out", args=[specimen.pk]))
    assert "active use hold" in main(response)
    response = client.get(reverse("specimens:disposal-request", args=[specimen.pk]))
    assert "Not available" not in main(response)


def test_disposal_hold_blocks_disposal_only(client, people, world):
    specimen = stored(people["technician"], world)
    manual_hold(people["manager"], specimen, Hold.Scope.DISPOSAL)
    page = detail(client, people["manager"], specimen)
    assert "An active disposal hold blocks disposal" in page
    assert 'href="' + reverse("specimens:check-out", args=[specimen.pk]) in page


def test_linked_hold_has_no_release_and_its_dialog_is_blocked(client, people, world):
    manager = people["manager"]
    specimen = stored(people["technician"], world)
    hold = linked_hold(manager, specimen)
    page = detail(client, manager, specimen)
    assert "Allocated to RQ-000012" in page
    assert "Released through its workflow: Allocated to RQ-000012" in page
    assert reverse("specimens:hold-release", args=[hold.pk]) not in page
    assert "An active use hold (Allocated to RQ-000012) blocks this" in page
    url = reverse("specimens:hold-release", args=[hold.pk])
    response = client.get(url)
    assert "released through that workflow" in main(response)
    posted = client.post(url, {"occurred_at": "", "reason": "Try"})
    assert posted.status_code == 409
    assert services.active_holds(specimen).filter(pk=hold.pk).exists()


def test_hold_form_offers_three_scopes(client, people, world):
    specimen = stored(people["technician"], world)
    client.force_login(people["technician"])
    page = main(client.get(reverse("specimens:hold-add", args=[specimen.pk])))
    for value in ("disposal", "use", "all_use"):
        assert f'value="{value}"' in page
    assert "Use hold" in page


def test_custody_correction_of_dispatch_and_return_hides_the_outside_party(client, people, world):
    specimen = returned(people["technician"], world)
    client.force_login(people["manager"])
    dispatch = specimen.custody.get(kind=CustodyEvent.Kind.DISPATCH)
    page = main(client.get(reverse("specimens:custody-correct", args=[dispatch.pk])))
    assert 'name="to_custodian"' not in page and 'name="from_custodian"' in page
    assert "A dispatch has no staff recipient" in page
    back = specimen.custody.get(kind=CustodyEvent.Kind.RETURN)
    page = main(client.get(reverse("specimens:custody-correct", args=[back.pk])))
    assert 'name="from_custodian"' not in page and 'name="to_custodian"' in page
    assert "Returned from" in page
    response = client.post(
        reverse("specimens:custody-correct", args=[back.pk]),
        {
            "expected_revision": fresh(specimen).revision,
            "destination": "Example Institute (SH-000003)",
            "to_custodian": people["technician"].pk,
            "reason": "Typo in the sender",
        },
    )
    assert response.status_code == 302
    assert specimen.custody.filter(corrects=back).exists()


def test_external_thaw_correction_takes_a_count_or_unknown(client, people, world):
    specimen = returned(people["technician"], world)
    client.force_login(people["manager"])
    external = specimen.thaws.get(kind=ThawEvent.Kind.EXTERNAL)
    page = main(client.get(reverse("specimens:thaw-correct", args=[external.pk])))
    assert "Thaws while away" in page and 'name="unknown"' in page


def test_list_filter_and_export_of_distributed(client, people, world):
    tech = people["technician"]
    specimen = dispatched(tech, world)
    other = stored(tech, world, position="B1")
    client.force_login(people["viewer"])
    page = main(client.get(reverse("specimens:specimen-list") + "?status=distributed"))
    assert specimen.accession in page and other.accession not in page
    assert "Outside the repository" in page
    response = client.get(reverse("specimens:export") + "?status=distributed")
    assert response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(response.content.decode())))
    assert [r["accession"] for r in rows] == [specimen.accession]
    assert rows[0]["status"] == "distributed" and rows[0]["custodian"] == ""
    assert rows[0]["checked_out"] == "no" and rows[0]["location"] == ""


def test_held_filter_includes_use_holds(client, people, world):
    specimen = stored(people["technician"], world)
    manual_hold(people["manager"], specimen, Hold.Scope.USE)
    client.force_login(people["viewer"])
    page = main(client.get(reverse("specimens:specimen-list") + "?held=yes"))
    assert specimen.accession in page


def without_distribution(monkeypatch):
    """Simulate a deployment without the distribution app (as storage's tests do for specimens)."""
    installed = django_apps.is_installed
    monkeypatch.setattr(
        django_apps, "is_installed", lambda label: label != "distribution" and installed(label)
    )


def test_distribution_links_absent_without_the_app(client, people, world, monkeypatch):
    specimen = stored(people["technician"], world)
    without_distribution(monkeypatch)
    assert not views.apps.is_installed("distribution")
    assert "Distribution" not in detail(client, people["viewer"], specimen).split("History")[0]
    assert views.distribution_links(specimen) == []


def test_distribution_links_when_installed(client, people, world, monkeypatch):
    specimen = stored(people["technician"], world)
    real_reverse = views.reverse

    def fake_reverse(name, *args, **kwargs):
        if name.startswith("distribution:"):
            return "/distribution/" + name.split(":")[1] + "/"
        return real_reverse(name, *args, **kwargs)

    fake_apps = SimpleNamespace(
        is_installed=lambda name: name == "distribution" or django_apps.is_installed(name)
    )
    monkeypatch.setattr(views, "apps", fake_apps)
    monkeypatch.setattr(views, "reverse", fake_reverse)
    page = detail(client, people["viewer"], specimen)
    assert f"/distribution/request-list/?specimen={specimen.accession}" in page
    assert f"/distribution/shipment-list/?specimen={specimen.accession}" in page
    code = specimen.participant.code
    assert f"/distribution/consent-list/?participant={code}" in page


def test_distribution_links_skip_unresolved_routes(people, world, monkeypatch):
    specimen = stored(people["technician"], world)
    real_reverse = views.reverse

    def unresolved(name, *args, **kwargs):
        # Installed, but without these routes (e.g. a version that lacks a list filter page).
        if name.startswith("distribution:"):
            raise NoReverseMatch(name)
        return real_reverse(name, *args, **kwargs)

    monkeypatch.setattr(views, "apps", SimpleNamespace(is_installed=lambda name: True))
    monkeypatch.setattr(views, "reverse", unresolved)
    assert views.distribution_links(specimen) == []  # no distribution URLs: NoReverseMatch
