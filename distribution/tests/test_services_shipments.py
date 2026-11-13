"""Shipments: contents, packing declarations, dispatch snapshot, receipt, returns. Synthetic."""

from decimal import Decimal

import pytest
from django.utils import timezone

from core.models import AuditEvent
from distribution import services
from distribution.models import (
    Allocation,
    ReceiptException,
    ReturnReview,
    ShipmentEvent,
    ShipmentItem,
    ShipmentReceipt,
    SpecimenReturn,
)
from specimens import services as specimen_services
from specimens.models import CustodyEvent, Hold, QuantityEntry

from .helpers import (
    ago,
    allocate,
    approved_request,
    csv_file,
    declaration,
    dispatch,
    dispatched,
    fast_password_hashing,  # noqa: F401
    fresh,
    gate,
    picked_request,
    refused,
    shipment_for,
    timedelta,
)

pytestmark = pytest.mark.django_db


def draft_shipment(people, request, allocations=()):
    request.refresh_from_db()
    shipment = services.create_shipment(
        actor=people["technician"],
        request_id=request.pk,
        expected_revision=request.revision,
        values={},
    )
    if allocations:
        shipment = set_contents(people, shipment, allocations)
    return shipment


def set_contents(people, shipment, allocations):
    shipment.refresh_from_db()
    return services.set_shipment_contents(
        actor=people["technician"],
        shipment_id=shipment.pk,
        expected_revision=shipment.revision,
        allocation_ids=tuple(a.pk for a in allocations),
    )


def pack(people, shipment, checker="manager", actor="technician", when=None, **declared):
    shipment.refresh_from_db()
    return services.pack_shipment(
        actor=people[actor],
        shipment_id=shipment.pk,
        expected_revision=shipment.revision,
        declaration=declaration(**declared),
        checked_by_id=people[checker].pk if checker else None,
        occurred_at=when or timezone.now(),
        reason="Packed on dry ice",
    )


def unpack(people, shipment):
    shipment.refresh_from_db()
    return services.unpack_shipment(
        actor=people["technician"],
        shipment_id=shipment.pk,
        expected_revision=shipment.revision,
        reason="Repacking",
    )


def receipt_values(shipment, **overrides):
    values = {
        "received_at": timezone.now(),
        "received_by_name": "Example Receiver",
        "unpacked_by_name": "Example Unpacker",
        "condition": "as_expected",
        "refrigerant_on_arrival": "adequate",
        "temperature_note": "",
        "discrepancies": "",
        "received_via": "email",
        "exceptions": (),
    }
    values.update(overrides)
    return services.ReceiptValues(**values)


def receive(people, shipment, actor="technician", upload=None, **overrides):
    shipment.refresh_from_db()
    return services.record_receipt(
        actor=people[actor],
        shipment_id=shipment.pk,
        expected_revision=shipment.revision,
        values=receipt_values(shipment, **overrides),
        upload=upload,
        reason="Recipient's email of today",
    )


def give_back(people, item, quantity="1.5", thaw_count=None, actor="technician"):
    specimen = fresh(item.specimen)
    return services.record_return(
        actor=people[actor],
        item_id=item.pk,
        expected_revision=specimen.revision,
        quantity=Decimal(quantity),
        supplied_text=quantity,
        thaw_count=thaw_count,
        condition="Unopened",
        recipient_id=people[actor].pk,
        occurred_at=timezone.now(),
        reason="Returned unused",
    )


def review(people, returned, decision="release"):
    return services.review_return(
        actor=people["manager"],
        return_id=returned.pk,
        expected_revision=fresh(returned.specimen).revision,
        decision=decision,
        occurred_at=timezone.now(),
        reason="Seal intact" if decision == "release" else "Needs a second look",
    )


# ------------------------------------------------------------------ create, edit, contents


def test_a_shipment_copies_the_ship_to_and_belongs_to_an_approved_request(people, dist, world):
    request = approved_request(people, dist, world)
    shipment = draft_shipment(people, request)
    assert shipment.state == "draft" and shipment.number.startswith("SH-")
    assert shipment.ship_to_name == "Example Investigator"
    assert shipment.ship_to_institution == "Example University"
    assert shipment.ship_to_address == "100 Example Avenue"
    assert "investigator@example.invalid" in shipment.ship_to_contact
    assert list(shipment.events.values_list("kind", flat=True)) == ["created"]
    services.cancel_request(
        actor=people["manager"],
        request_id=request.pk,
        expected_revision=fresh(request).revision,
        reason="No longer needed",
    )
    request.refresh_from_db()
    refused(lambda: draft_shipment(people, request), "invalid_transition")


def test_edit_shipment_is_limited_once_packed(people, dist, world):
    request, allocations, shipment = shipment_for(people, dist, world, count=1)
    shipment.refresh_from_db()
    edited = services.edit_shipment(
        actor=people["technician"],
        shipment_id=shipment.pk,
        expected_revision=shipment.revision,
        values={"courier": "Example Courier", "tracking": "EX1"},
        reason="Booked the courier",
    )
    assert edited.courier == "Example Courier" and edited.state == "packed"
    refused(
        lambda: services.edit_shipment(
            actor=people["technician"],
            shipment_id=shipment.pk,
            expected_revision=fresh(shipment).revision,
            values={"ship_to_address": "Elsewhere"},
            reason="Change the address",
        ),
        "invalid",
    )
    event = ShipmentEvent.objects.filter(shipment=shipment, kind="edited").get()
    assert "courier" in event.changes


def test_contents_are_picked_allocations_of_the_request_not_in_another_shipment(
    people, dist, world
):
    request, allocations = picked_request(people, dist, world, count=2)
    shipment = draft_shipment(people, request, allocations[:1])
    other = draft_shipment(people, request)
    refused(lambda: set_contents(people, other, allocations[:1]), "invalid")
    shipment = set_contents(people, shipment, allocations)
    assert Allocation.objects.filter(shipment=shipment).count() == 2
    shipment = set_contents(people, shipment, allocations[1:])
    assert Allocation.objects.filter(shipment=shipment).count() == 1
    event = ShipmentEvent.objects.filter(shipment=shipment, kind="contents").latest("recorded_at")
    assert len(event.changes["removed"]) == 1


def test_only_picked_specimens_can_join_a_shipment(people, dist, world):
    request = approved_request(people, dist, world)
    allocations = allocate(people, request, dist.specimens[:1])
    shipment = draft_shipment(people, request)
    refused(lambda: set_contents(people, shipment, allocations), "invalid")


# ------------------------------------------------------------------ pack and unpack


def test_pack_stores_the_declarations_and_each_specimens_revision(people, dist, world):
    request, allocations = picked_request(people, dist, world, count=2)
    shipment = draft_shipment(people, request, allocations)
    packed = pack(people, shipment)
    assert packed.state == "packed" and packed.packed_by_id == people["technician"].pk
    assert packed.checked_by_id == people["manager"].pk and packed.packed_at is not None
    assert packed.dry_ice_kg == Decimal("5.00") and packed.training_confirmed is True
    for allocation in allocations:
        allocation.refresh_from_db()
        assert allocation.packed_revision == fresh(allocation.specimen).revision
    assert ShipmentEvent.objects.filter(shipment=shipment, kind="packed").exists()


@pytest.mark.parametrize(
    "overrides,code",
    [
        ({"refrigerant": "dry_ice", "dry_ice_kg": None}, "required"),
        ({"refrigerant": "dry_ice", "dry_ice_kg": Decimal("0")}, "invalid"),
        ({"refrigerant": "dry_ice", "dry_ice_kg": Decimal("5.001")}, "invalid"),
        ({"refrigerant": "gel_ice", "dry_ice_kg": Decimal("5.00")}, "invalid"),
        ({"refrigerant": "none", "dry_ice_kg": "1"}, "invalid"),
        ({"training_confirmed": None}, "required"),
        ({"emergency_contact": "  "}, "required"),
        ({"package_count": 0}, "invalid"),
        ({"temperature_class": "tepid"}, "invalid"),
        ({"classification": "unknown"}, "invalid"),
    ],
)
def test_pack_needs_complete_declarations(people, dist, world, overrides, code):
    request, allocations = picked_request(people, dist, world, count=1)
    shipment = draft_shipment(people, request, allocations)
    refused(lambda: pack(people, shipment, **overrides), code)
    shipment.refresh_from_db()
    assert shipment.state == "draft"


def test_a_shipment_without_dry_ice_has_no_mass(people, dist, world):
    request, allocations = picked_request(people, dist, world, count=1)
    shipment = draft_shipment(people, request, allocations)
    packed = pack(people, shipment, refrigerant="gel_ice", dry_ice_kg=None)
    assert packed.dry_ice_kg is None and packed.refrigerant == "gel_ice"


def test_training_confirmed_false_is_recorded_not_refused(people, dist, world):
    request, allocations = picked_request(people, dist, world, count=1)
    shipment = draft_shipment(people, request, allocations)
    assert pack(people, shipment, training_confirmed=False).training_confirmed is False


def test_the_checker_is_another_person_than_the_packer(people, dist, world):
    request, allocations = picked_request(people, dist, world, count=1)
    shipment = draft_shipment(people, request, allocations)
    refused(lambda: pack(people, shipment, checker="technician"), "self_approval")
    shipment.refresh_from_db()
    assert shipment.state == "draft"
    assert pack(people, shipment, checker="technician2").checked_by_id == people["technician2"].pk


def test_the_checker_must_be_active_operating_staff(people, dist, world):
    request, allocations = picked_request(people, dist, world, count=1)
    shipment = draft_shipment(people, request, allocations)
    refused(lambda: pack(people, shipment, checker="viewer"), "invalid")
    refused(lambda: pack(people, shipment, checker="nobody"), "invalid")


def test_pack_needs_picked_contents(people, dist, world):
    request = approved_request(people, dist, world)
    shipment = draft_shipment(people, request)
    refused(lambda: pack(people, shipment), "invalid_transition")


def test_only_a_draft_shipment_can_be_packed_twice_never(people, dist, world):
    request, allocations, shipment = shipment_for(people, dist, world, count=1)
    refused(lambda: pack(people, shipment), "invalid_transition")


def test_unpack_clears_the_packed_revisions_and_returns_to_draft(people, dist, world):
    request, allocations, shipment = shipment_for(people, dist, world, count=2)
    assert all(fresh(a).packed_revision for a in allocations)
    draft = unpack(people, shipment)
    assert draft.state == "draft" and draft.packed_by_id is None and draft.packed_at is None
    assert draft.checked_by_id is None
    assert all(fresh(a).packed_revision is None for a in allocations)
    assert {fresh(a).shipment_id for a in allocations} == {shipment.pk}  # contents kept
    refused(lambda: unpack(people, draft), "invalid_transition")
    assert pack(people, draft).state == "packed"


# ------------------------------------------------------------------ dispatch


def test_dispatch_writes_the_snapshot_and_moves_the_specimens_out_of_custody(people, dist, world):
    request, allocations, shipment = shipment_for(people, dist, world, count=2)
    before = {s.pk: fresh(s) for s in dist.specimens[:2]}
    dispatch(people, shipment)
    shipment.refresh_from_db()
    assert shipment.state == "dispatched" and shipment.dispatched_by_id == people["technician"].pk
    assert shipment.courier == "Example Courier" and shipment.tracking == "EX123456789"
    items = list(ShipmentItem.objects.filter(shipment=shipment).order_by("line"))
    assert [i.line for i in items] == [1, 2]
    for item in items:
        specimen = fresh(item.specimen)
        assert item.accession == specimen.accession
        assert item.specimen_type == str(specimen.specimen_type)
        assert item.container == str(specimen.container_type)
        assert item.quantity == before[specimen.pk].quantity_on_hand == Decimal("2")
        assert item.unit == "mL" and item.study_code == "DEMO-STUDY"
        assert item.participant_code == ""  # not requested
        assert specimen.status == "distributed" and specimen.custodian_id is None
        assert specimen.open_checkout_id is None and specimen.quantity_on_hand == 0
        assert item.custody_event.kind == "dispatch" and item.custody_event.to_custodian_id is None
        assert item.custody_event.destination.startswith(f"{shipment.number} → ")
        assert specimen.ledger.filter(kind=QuantityEntry.Kind.DISTRIBUTE).exists()
        assert Hold.objects.filter(specimen=specimen, release__isnull=True).count() == 0
    assert {fresh(a).state for a in allocations} == {"shipped"}
    assert {a.shipment_id for a in Allocation.objects.filter(request=request)} == {shipment.pk}
    assert ShipmentEvent.objects.filter(shipment=shipment, kind="dispatched").exists()
    assert AuditEvent.objects.filter(action="distribution.shipment_dispatched").exists()
    assert all(fresh(a).packed_revision is None for a in allocations)


def test_participant_codes_only_when_requested_with_a_coded_or_limited_agreement(
    people, dist, world
):
    request, allocations, shipment = shipment_for(
        people, dist, world, count=1, include_participant_codes=True
    )
    dispatch(people, shipment)
    assert ShipmentItem.objects.get(shipment=shipment).participant_code == "P-000123"


def test_dispatch_cannot_precede_packing(people, dist, world):
    request, allocations, shipment = shipment_for(people, dist, world, count=1)
    shipment.refresh_from_db()
    refused(
        lambda: services.dispatch_shipment(
            actor=people["technician"],
            shipment_id=shipment.pk,
            expected_revision=shipment.revision,
            courier="Example Courier",
            tracking="EX1",
            handed_to="Courier driver",
            occurred_at=shipment.packed_at - timedelta(minutes=5),
            reason="Backdated",
        ),
        "invalid_time",
    )
    shipment.refresh_from_db()
    assert shipment.state == "packed"
    assert not ShipmentItem.objects.exists()


def test_dispatch_needs_a_packed_shipment_and_is_not_repeatable(people, dist, world):
    request, allocations = picked_request(people, dist, world, count=1)
    draft = draft_shipment(people, request, allocations)
    refused(lambda: dispatch(people, draft), "invalid_transition")
    pack(people, draft)
    dispatch(people, draft)
    refused(lambda: dispatch(people, draft), "invalid_transition")


def test_a_failed_dispatch_writes_nothing(people, dist, world, monkeypatch):
    """All checks come first, but a failure in the middle (here the second specimen) rolls the
    first specimen's dispatch back too."""
    request, allocations, shipment = shipment_for(people, dist, world, count=2)
    real = specimen_services.dispatch_specimen
    calls = []

    def flaky(**kwargs):
        calls.append(kwargs["specimen_id"])
        if len(calls) == 2:
            raise RuntimeError("storage fault")
        return real(**kwargs)

    monkeypatch.setattr(specimen_services, "dispatch_specimen", flaky)
    with pytest.raises(RuntimeError):
        dispatch(people, shipment)
    shipment.refresh_from_db()
    assert shipment.state == "packed" and not ShipmentItem.objects.exists()
    assert {fresh(s).status for s in dist.specimens[:2]} == {"available"}
    assert {fresh(a).state for a in allocations} == {"picked"}
    assert not CustodyEvent.objects.filter(kind="dispatch").exists()


# ------------------------------------------------------------------ the manifest is a snapshot


def test_corrections_after_dispatch_do_not_change_the_shipment_items(people, dist, world):
    request, allocations, shipment = dispatched(people, dist, world, count=1)
    snapshot = list(ShipmentItem.objects.filter(shipment=shipment).values())
    plasma = world.plasma
    plasma.refresh_from_db()
    specimen_services.save_specimen_catalogue(
        actor=people["manager"],
        kind="specimen_type",
        object_id=plasma.pk,
        expected_revision=plasma.revision,
        values={"name": "Plasma (renamed)"},
        reason="Clearer name",
    )
    specimen = fresh(dist.specimens[0])
    specimen_services.correct_specimen(
        actor=people["manager"],
        specimen_id=specimen.pk,
        expected_revision=specimen.revision,
        values=specimen_services.CorrectionValues(
            fields=frozenset({"collected_at"}), collected_at=ago(600)
        ),
        reason="Transcription error",
    )
    assert "renamed" in str(fresh(specimen).specimen_type)
    assert list(ShipmentItem.objects.filter(shipment=shipment).values()) == snapshot
    assert "renamed" not in snapshot[0]["specimen_type"]


# ------------------------------------------------------------------ receipt


def test_receipt_is_recorded_once_and_closes_the_shipment(people, dist, world):
    request, allocations, shipment = dispatched(people, dist, world, count=2)
    item = ShipmentItem.objects.filter(shipment=shipment).first()
    receipt = receive(
        people,
        shipment,
        condition="discrepancy",
        discrepancies="One vial thawed",
        exceptions=((item.pk, "thawed", "Arrived thawed"),),
    )
    shipment.refresh_from_db()
    assert shipment.state == "received" and receipt.condition == "discrepancy"
    assert ReceiptException.objects.get().item_id == item.pk
    refused(lambda: receive(people, shipment), "invalid_transition")
    assert ShipmentReceipt.objects.count() == 1
    # No specimen changes: the material is outside custody.
    assert {fresh(s).status for s in dist.specimens[:2]} == {"distributed"}


def test_receipt_needs_a_dispatched_shipment_and_a_time_after_dispatch(people, dist, world):
    request, allocations, shipment = shipment_for(people, dist, world, count=1)
    refused(lambda: receive(people, shipment), "invalid_transition")  # packed, not dispatched
    dispatch(people, shipment)
    shipment.refresh_from_db()
    refused(
        lambda: receive(people, shipment, received_at=shipment.dispatched_at - timedelta(hours=1)),
        "invalid_time",
    )
    refused(
        lambda: receive(people, shipment, received_at=timezone.now() + timedelta(hours=1)),
        "invalid_time",
    )
    refused(lambda: receive(people, shipment, condition="damaged"), "required")  # needs a note
    assert not ShipmentReceipt.objects.exists()


def test_exceptions_must_name_items_of_this_shipment_once_each(people, dist, world):
    request, allocations, shipment = dispatched(people, dist, world, count=1)
    item = ShipmentItem.objects.get(shipment=shipment)
    refused(
        lambda: receive(
            people, shipment, exceptions=((item.pk, "thawed", ""), (item.pk, "missing", ""))
        ),
        "invalid",
    )
    refused(lambda: receive(people, shipment, exceptions=((item.pk, "melted", ""),)), "invalid")


def test_correct_receipt_appends_a_chain_and_keeps_the_attachment(people, dist, world):
    request, allocations, shipment = dispatched(people, dist, world, count=1)
    first = receive(people, shipment, upload=csv_file("note,ok\n1,2\n", "receipt.csv"))
    assert first.attachment_id
    shipment.refresh_from_db()

    def correct(receipt, **overrides):
        shipment.refresh_from_db()
        return services.correct_receipt(
            actor=people["manager"],
            receipt_id=receipt.pk,
            expected_revision=shipment.revision,
            values=receipt_values(shipment, **overrides),
            reason="Recipient corrected the condition",
        )

    second = correct(first, condition="damaged", discrepancies="Box crushed")
    assert second.corrects_id == first.pk and second.attachment_id == first.attachment_id
    assert services.effective_receipt(shipment).pk == second.pk
    refused(lambda: correct(first), "invalid_transition")  # only the latest can be corrected
    third = correct(second, condition="as_expected")
    assert third.corrects_id == second.pk and third.attachment_id == first.attachment_id
    assert ShipmentReceipt.objects.count() == 3
    assert ShipmentReceipt.objects.get(pk=first.pk).condition == "as_expected"  # original untouched
    shipment.refresh_from_db()
    assert shipment.state == "received"


# ------------------------------------------------------------------ returns


def test_a_return_brings_the_specimen_back_on_a_use_hold(people, dist, world):
    request, allocations, shipment = dispatched(people, dist, world, count=1)
    item = ShipmentItem.objects.get(shipment=shipment)
    returned = give_back(people, item, "1.5", thaw_count=1)
    specimen = fresh(dist.specimens[0])
    assert specimen.status == "available" and specimen.quantity_on_hand == Decimal("1.5")
    assert specimen.open_checkout_id == returned.custody_event_id
    assert specimen.custodian_id == people["technician"].pk
    hold = returned.hold
    assert hold.scope == "use" and hold.reference == f"Returned in {shipment.number}"
    assert Hold.objects.filter(pk=hold.pk, release__isnull=True).exists()
    assert returned.quantity == Decimal("1.5") and returned.unit == world.ml
    assert ShipmentEvent.objects.filter(shipment=shipment, kind="returned").exists()
    # A returned specimen is not distributable until reviewed.
    assert "held" in gate(people, approved_request(people, dist, world), specimen)


def test_a_return_of_nothing_leaves_the_specimen_exhausted(people, dist, world):
    request, allocations, shipment = dispatched(people, dist, world, count=1)
    item = ShipmentItem.objects.get(shipment=shipment)
    give_back(people, item, "0")
    assert fresh(dist.specimens[0]).status == "exhausted"


def test_the_same_item_cannot_be_returned_twice(people, dist, world):
    request, allocations, shipment = dispatched(people, dist, world, count=2)
    items = list(ShipmentItem.objects.filter(shipment=shipment))
    give_back(people, items[0])
    refused(lambda: give_back(people, items[0]), "invalid_transition")
    assert SpecimenReturn.objects.count() == 1
    give_back(people, items[1])  # a different item is fine
    assert SpecimenReturn.objects.count() == 2


def test_a_return_works_after_receipt_too(people, dist, world):
    request, allocations, shipment = dispatched(people, dist, world, count=1)
    receive(people, shipment)
    item = ShipmentItem.objects.get(shipment=shipment)
    assert give_back(people, item).shipment_id == shipment.pk


def test_review_release_releases_the_hold_and_keep_keeps_it(people, dist, world):
    request, allocations, shipment = dispatched(people, dist, world, count=2)
    first, second = ShipmentItem.objects.filter(shipment=shipment).order_by("line")
    kept = give_back(people, first)
    released = give_back(people, second)
    review(people, kept, "keep")
    assert Hold.objects.filter(pk=kept.hold_id, release__isnull=True).exists()
    review(people, kept, "release")  # a kept return can still be released later
    assert Hold.objects.get(pk=kept.hold_id).release is not None
    review(people, released, "release")
    assert Hold.objects.get(pk=released.hold_id).release is not None
    refused(lambda: review(people, released, "release"), "invalid_transition")
    assert ReturnReview.objects.count() == 3
    assert list(
        ReturnReview.objects.order_by("recorded_at").values_list("decision", flat=True)
    ) == [
        "keep",
        "release",
        "release",
    ]


# ------------------------------------------------------------------ cancel, notify, notes


def test_cancel_shipment_keeps_the_allocations_picked(people, dist, world):
    request, allocations, shipment = shipment_for(people, dist, world, count=2)
    shipment.refresh_from_db()
    cancelled = services.cancel_shipment(
        actor=people["manager"],
        shipment_id=shipment.pk,
        expected_revision=shipment.revision,
        reason="Courier unavailable",
    )
    assert cancelled.state == "cancelled"
    for allocation in allocations:
        allocation.refresh_from_db()
        assert allocation.state == "picked" and allocation.shipment_id is None
        assert allocation.packed_revision is None
    assert {fresh(s).status for s in dist.specimens[:2]} == {"available"}
    # The picked specimens can go into a new shipment.
    again = draft_shipment(people, request, allocations)
    assert pack(people, again).state == "packed"
    refused(
        lambda: services.cancel_shipment(
            actor=people["manager"],
            shipment_id=shipment.pk,
            expected_revision=fresh(shipment).revision,
            reason="Again",
        ),
        "invalid_transition",
    )


def test_a_dispatched_shipment_cannot_be_cancelled(people, dist, world):
    request, allocations, shipment = dispatched(people, dist, world, count=1)
    refused(
        lambda: services.cancel_shipment(
            actor=people["manager"],
            shipment_id=shipment.pk,
            expected_revision=shipment.revision,
            reason="Too late",
        ),
        "invalid_transition",
    )


def test_notifications_and_notes_are_timeline_events(people, dist, world):
    request, allocations, shipment = dispatched(people, dist, world, count=1)
    for kind in ("notified", "acknowledged"):
        shipment.refresh_from_db()
        event = services.record_notification(
            actor=people["technician"],
            shipment_id=shipment.pk,
            expected_revision=shipment.revision,
            kind=kind,
            occurred_at=timezone.now(),
            note="Told by email (synthetic)",
        )
        assert event.kind == kind
    shipment.refresh_from_db()
    refused(
        lambda: services.record_notification(
            actor=people["technician"],
            shipment_id=shipment.pk,
            expected_revision=shipment.revision,
            kind="shouted",
            occurred_at=timezone.now(),
            note="",
        ),
        "invalid",
    )
    note = services.add_shipment_note(
        actor=people["technician2"],
        shipment_id=shipment.pk,
        note="Waybill attached",
        occurred_at=timezone.now(),
        upload=csv_file("waybill,1\n", "waybill.csv"),
    )
    assert note.kind == "note" and note.attachment_id
    refused(
        lambda: services.add_shipment_note(
            actor=people["technician"],
            shipment_id=shipment.pk,
            note="",
            occurred_at=timezone.now(),
        ),
        "required",
    )
    receive(people, shipment)
    shipment.refresh_from_db()
    refused(
        lambda: services.record_notification(
            actor=people["technician"],
            shipment_id=shipment.pk,
            expected_revision=shipment.revision,
            kind="notified",
            occurred_at=timezone.now(),
            note="",
        ),
        "invalid_transition",
    )
