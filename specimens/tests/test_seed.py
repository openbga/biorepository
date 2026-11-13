"""The demo seed walks the first journey through the services, once."""

import pytest
from django.core.management import call_command

from specimens.models import ImportBatch, Specimen
from specimens.queries import thaw_summary
from storage.models import MoveEvent

pytestmark = pytest.mark.django_db


def test_demo_seed_completes_the_first_journey_once(monkeypatch):
    monkeypatch.setenv("DEMO_PASSWORD", "synthetic-demo-password-1")
    call_command("seed_demo", stdout=None)
    count = Specimen.objects.count()
    call_command("seed_demo", stdout=None)
    assert Specimen.objects.count() == count == 7

    parent = Specimen.objects.filter(children__isnull=False).distinct().get()
    assert parent.quantity_on_hand == 1 and parent.occupancy.position.box.code == "BOX-001"
    assert parent.occupancy.position.box.node.code == "T-02-S1"  # moved with its contents
    assert thaw_summary(parent).total == 1
    disposed = Specimen.objects.get(status="disposed")
    assert disposed.parent_id == parent.pk and disposed.occupancy.position_id is None
    assert disposed.disposal.decision.actor.username == "demo_manager"
    assert disposed.disposal.request.requested_by.username == "demo_technician"
    assert MoveEvent.objects.filter(kind="container_move", box__code="BOX-001").count() == 1
    assert Specimen.objects.filter(occupancy__position__isnull=True, status="available").count()
    assert {bool(b.errors) for b in ImportBatch.objects.all()} == {True, False}
