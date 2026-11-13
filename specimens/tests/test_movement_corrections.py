"""Correcting a recorded movement is for repository managers only.

A technician may relocate a stored specimen, but marking an earlier movement as corrected is a
correction of history and needs the admin or manager role, at the service and in the form.
"""

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse
from django.utils import timezone

from specimens import services
from specimens.models import CustodyEvent
from storage.models import MoveEvent, MoveMember

from .conftest import ago, occ_rev, place, register

pytestmark = pytest.mark.django_db

CORRECTS_LABEL = "This corrects a wrong movement"


def main(response):
    """The page body only: the page-help panel describes the control for every reader."""
    page = response.content.decode()
    start = page.index("<main")
    return page[start : page.index("</main>", start)]


@pytest.fixture(autouse=True)
def plain_static_storage(settings):
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


def stored(people, world):
    specimen = register(people["technician"], world)
    place(people["technician"], specimen, world.position("A1"))
    specimen.refresh_from_db()
    return specimen, MoveMember.objects.get(occupancy_id=specimen.occupancy_id).event


def relocate(actor, specimen, position, corrects=None):
    specimen.refresh_from_db()
    return services.relocate_specimen(
        actor=actor,
        specimen_id=specimen.pk,
        expected_revision=specimen.revision,
        expected_occupancy_revision=occ_rev(specimen),
        position_id=position.pk if position else None,
        occurred_at=ago(250),
        reason="Correct mistaken placement",
        corrects_id=corrects.pk if corrects else None,
    )


def test_technician_cannot_correct_a_movement_but_may_relocate(people, world):
    specimen, original = stored(people, world)
    before = (MoveEvent.objects.count(), CustodyEvent.objects.count(), occ_rev(specimen))
    for role in ("technician", "viewer"):
        with pytest.raises(PermissionDenied):
            relocate(people[role], specimen, world.position("A2"), corrects=original)
    assert (MoveEvent.objects.count(), CustodyEvent.objects.count(), occ_rev(specimen)) == before
    assert not MoveEvent.objects.filter(corrects=original).exists()
    relocate(people["technician"], specimen, world.position("A2"))  # an ordinary move is fine
    assert MoveEvent.objects.filter(kind="relocate").count() == 1


@pytest.mark.parametrize("role", ["manager", "admin"])
def test_manager_or_admin_records_the_correction(people, world, role):
    specimen, original = stored(people, world)
    relocate(people[role], specimen, world.position("A2"), corrects=original)
    correction = MoveEvent.objects.get(corrects=original)
    assert correction.kind == "correction" and correction.actor == people[role]


def test_a_relocation_always_ends_in_a_position(people, world):
    specimen, original = stored(people, world)
    with pytest.raises(ValidationError) as excinfo:
        relocate(people["manager"], specimen, None, corrects=original)
    assert "position_id" in excinfo.value.error_dict
    specimen.refresh_from_db()
    assert specimen.occupancy.position == world.position("A1")


def test_the_form_offers_the_correction_only_to_managers(client, people, world):
    specimen, original = stored(people, world)
    url = reverse("specimens:relocate", args=[specimen.pk])
    client.force_login(people["technician"])
    page = client.get(url)
    assert page.status_code == 200 and CORRECTS_LABEL not in main(page)
    # A crafted post naming the earlier movement records a plain relocation, never a correction.
    initial = page.context["form"].initial
    response = client.post(
        url,
        {
            "box": str(world.box.pk),
            "position_label": "A2",
            "corrects": str(original.pk),
            "occurred_at": timezone.localtime(ago(250)).strftime("%Y-%m-%dT%H:%M:%S"),
            "reason": "Moved to make room",
            "expected_revision": initial["expected_revision"],
            "expected_occupancy_revision": initial["expected_occupancy_revision"],
        },
    )
    assert response.status_code == 302
    assert not MoveEvent.objects.filter(corrects=original).exists()
    assert MoveEvent.objects.filter(kind="relocate").count() == 1

    client.force_login(people["manager"])
    assert CORRECTS_LABEL in main(client.get(url))
