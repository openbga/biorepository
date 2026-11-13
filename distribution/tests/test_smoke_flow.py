"""One pass through the whole service journey (detailed rules live in the other test files)."""

from decimal import Decimal

from django.utils import timezone

from distribution import queries, services
from distribution.models import Allocation, ShipmentItem
from specimens.models import Specimen

from .conftest import allocate, approved_request, dispatch, fresh, packed_shipment, pick


def test_request_to_return(people, dist, world):
    request = approved_request(people, dist, world)
    allocations = allocate(people, request, dist.specimens[:2])
    assert {a.state for a in allocations} == {"allocated"}
    rows = queries.pick_list(actor=people["viewer"], request_id=request.pk)
    assert [r.blockers for r in rows] == [(), ()]
    pick(people, request, allocations)
    shipment = packed_shipment(people, request, allocations)
    dispatch(people, shipment)
    shipment.refresh_from_db()
    assert shipment.state == "dispatched"
    items = list(ShipmentItem.objects.filter(shipment=shipment).order_by("line"))
    assert len(items) == 2 and items[0].participant_code == ""
    assert {fresh(s).status for s in dist.specimens[:2]} == {Specimen.Status.DISTRIBUTED}
    assert set(Allocation.objects.values_list("state", flat=True)) == {"shipped"}
    receipt = services.record_receipt(
        actor=people["technician"],
        shipment_id=shipment.pk,
        expected_revision=shipment.revision,
        values=services.ReceiptValues(
            received_at=timezone.now(),
            received_by_name="Example Receiver",
            unpacked_by_name="",
            condition="discrepancy",
            refrigerant_on_arrival="adequate",
            temperature_note="",
            discrepancies="One vial thawed",
            received_via="email",
            exceptions=((items[0].pk, "thawed", "Arrived thawed"),),
        ),
        reason="Recipient's email of today",
    )
    assert receipt.exceptions.count() == 1
    specimen = fresh(items[1].specimen)
    returned = services.record_return(
        actor=people["technician"],
        item_id=items[1].pk,
        expected_revision=specimen.revision,
        quantity=Decimal("1.5"),
        supplied_text="1.5",
        thaw_count=None,
        condition="Unopened",
        recipient_id=people["technician"].pk,
        occurred_at=timezone.now(),
        reason="Returned unused",
    )
    specimen = fresh(specimen)
    assert specimen.status == "available" and specimen.open_checkout_id
    services.review_return(
        actor=people["manager"],
        return_id=returned.pk,
        expected_revision=specimen.revision,
        decision="release",
        occurred_at=timezone.now(),
        reason="Seal intact",
    )


def test_withdrawal_holds_and_releases(people, dist, world):
    request = approved_request(people, dist, world)
    allocate(people, request, dist.specimens[:1])
    consent = dist.consent
    consent.refresh_from_db()
    withdrawal = services.withdraw_consent(
        actor=people["manager"],
        consent_id=consent.pk,
        expected_revision=consent.revision,
        kind="no_use",
        effective_on=timezone.localdate(),
        received_via="Letter (synthetic)",
        occurred_at=timezone.now(),
        reason="Participant asked to stop use",
    )
    assert withdrawal.held_count == 3 and withdrawal.released_count == 1
    found = queries.blockers(
        actor=people["viewer"], request_id=request.pk, specimen_ids=[dist.specimens[0].pk]
    )
    assert {b.code for b in found[dist.specimens[0].pk]} >= {"held", "consent_withdrawn"}
