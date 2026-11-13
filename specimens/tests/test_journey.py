"""The product's first journey through the service layer, end to end (docs/product.md)."""

from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied
from django.utils import timezone

from specimens import queries, services
from specimens.models import CustodyEvent, QuantityEntry, Specimen
from storage import services as storage

from .conftest import ago, check_in, check_out, occ_rev, place, register

pytestmark = pytest.mark.django_db


def test_first_journey_registration_to_disposal(people, world):
    tech, approver = people["technician"], people["approver"]
    parent = register(tech, world, baseline=0)
    assert parent.accession.startswith("VP-") and len(parent.accession) >= 9
    assert parent.status == "available" and parent.quantity_on_hand == Decimal("2")
    place(tech, parent, world.position("A1"))
    check_out(tech, parent, tech, occurred_at=ago(200))
    parent.refresh_from_db()
    children = services.derive_specimens(
        actor=tech,
        parent_id=parent.pk,
        expected_revision=parent.revision,
        expected_occupancy_revision=occ_rev(parent),
        kind="aliquot",
        consumed=Decimal("1.000000"),
        children=tuple(
            services.ChildSpec(
                world.plasma.pk,
                world.cryovial.pk,
                Decimal("0.5"),
                "0.5",
                world.ml.pk,
                world.position(label).pk,
            )
            for label in ("B1", "B2")
        ),
        procedure="Split into 0.5 mL aliquots",
        occurred_at=ago(190),
        reason="Aliquoting",
    )
    assert len(children) == 2
    parent.refresh_from_db()
    assert parent.quantity_on_hand == Decimal("1")
    assert [e.kind for e in parent.ledger.order_by("recorded_at")] == ["initial", "derive"]
    assert all(c.parent_id == parent.pk and c.occupancy.position_id for c in children)
    check_in(tech, parent, world.position("A1"), thaw=True, occurred_at=ago(150))
    summary = queries.thaw_summary(parent)
    assert summary.total == 1
    out = queries.time_out_of_storage(parent)
    assert not out.open and 49 <= out.total.total_seconds() / 60 <= 51

    # The box moves with its contents in one storage event.
    world.box.refresh_from_db()
    with storage.operation():
        storage.lock_gate()
        gate = storage.lock_gate().revision
    storage.move_container(
        actor=tech,
        kind="box",
        object_id=world.box.pk,
        expected_revision=world.box.revision,
        expected_gate_revision=gate,
        target_kind="node",
        target_id=world.shelf2.pk,
        target_slot=1,
        occurred_at=ago(120),
        reason="Rebalancing shelves",
    )

    # Disposal: requested by one person, approved by another holding the permission.
    parent.refresh_from_db()
    request = services.request_disposal(
        actor=tech,
        specimen_id=parent.pk,
        expected_revision=parent.revision,
        method="Autoclave",
        destination="Biohazard waste",
        reason="Study closed",
    )
    parent.refresh_from_db()
    with pytest.raises(PermissionDenied):  # the requester holds no approval grant
        services.decide_disposal(
            actor=tech,
            request_id=request.pk,
            expected_revision=parent.revision,
            decision="approve",
            reason="Self",
        )
    services.decide_disposal(
        actor=approver,
        request_id=request.pk,
        expected_revision=parent.revision,
        decision="approve",
        reason="Checked against the closure notice",
    )
    parent.refresh_from_db()
    services.execute_disposal(
        actor=tech,
        request_id=request.pk,
        expected_revision=parent.revision,
        expected_occupancy_revision=occ_rev(parent),
        occurred_at=timezone.now(),
        reason="Autoclaved",
    )
    parent.refresh_from_db()
    assert parent.status == Specimen.Status.DISPOSED and parent.quantity_on_hand == 0
    assert parent.occupancy.position_id is None
    assert parent.ledger.filter(kind=QuantityEntry.Kind.DISPOSE).count() == 1
    kinds = [e.kind for e in parent.custody.order_by("occurred_at", "recorded_at")]
    assert kinds == ["register", "place", "check_out", "check_in", "dispose"]
    history = queries.specimen_history(actor=people["viewer"], specimen_id=parent.pk)
    titles = [item.title for item in history]
    assert "Container moved" in titles and "Disposal approved" in titles
    assert CustodyEvent.objects.filter(specimen__in=children).count() == 4
