"""The distribution demo seed runs through the services after the other demo seeds, once."""

import pytest
from django.core.management import call_command

from accounts.models import User
from accounts.roles import can_decide_requests
from distribution.models import (
    Agreement,
    Allocation,
    Consent,
    Shipment,
    ShipmentReceipt,
    SpecimenRequest,
    SpecimenReturn,
    Study,
)
from distribution.seed import seed_distribution, seed_full_distribution
from specimens.models import Specimen

pytestmark = pytest.mark.django_db


def test_seed_after_the_demo_seed_walks_the_journey_once(monkeypatch):
    monkeypatch.setenv("DEMO_PASSWORD", "synthetic-demo-password-1")
    call_command("seed_demo", stdout=None)
    manager = User.objects.get(username="demo_manager")
    technician = User.objects.get(username="demo_technician")
    assert seed_distribution(manager=manager, technician=technician) in (True, False)
    counts = (SpecimenRequest.objects.count(), Allocation.objects.count())
    assert seed_distribution(manager=manager, technician=technician) is False
    assert (SpecimenRequest.objects.count(), Allocation.objects.count()) == counts

    assert can_decide_requests(manager)
    assert not any(can_decide_requests(u) for u in User.objects.exclude(pk=manager.pk))
    assert Study.objects.get(code="DEMO-STUDY").consent_basis == "records"
    assert Consent.objects.get(participant__code="P-000123").status == "consented"
    assert Agreement.objects.get(institution__code="EXU").status == "executed"
    assert set(SpecimenRequest.objects.values_list("status", flat=True)) == {
        "approved",
        "submitted",
    }
    shipment = Shipment.objects.get()
    assert shipment.state == "received"
    assert ShipmentReceipt.objects.get().exceptions.get().outcome == "thawed"
    returned = SpecimenReturn.objects.get()
    assert not returned.reviews.exists()
    assert Specimen.objects.filter(status="distributed").count() == 1
    assert Allocation.objects.filter(state="allocated").count() == 0
    assert shipment.items.count() == 2
    # The tube the DEMO aliquots came from stays where the specimens demo left it.
    parent = Specimen.objects.filter(children__isnull=False).distinct().get()
    assert parent.status == "available" and parent.quantity_on_hand == 1
    assert parent.occupancy.position.box.code == "BOX-001"
    assert not Allocation.objects.filter(specimen=parent).exists()


def test_full_seed_adds_governance_and_a_packed_shipment_once(monkeypatch):
    monkeypatch.setenv("DEMO_PASSWORD", "synthetic-demo-password-1")
    call_command("seed_demo", "--full", stdout=None)
    manager = User.objects.get(username="demo_manager")
    technician = User.objects.get(username="demo_technician")
    admin = User.objects.get(username="demo_admin")
    seed_full_distribution(manager=manager, technician=technician, admin=admin)
    assert seed_full_distribution(manager=manager, technician=technician, admin=admin) is False
    study_a = Study.objects.get(code="STUDY-A")
    assert study_a.consents.filter(status="withdrawn").count() == 1
    assert study_a.consents.filter(status="refused").count() == 1
    assert Study.objects.get(code="STUDY-B").consent_basis == "determination"
    assert Shipment.objects.filter(state="packed").count() == 1
