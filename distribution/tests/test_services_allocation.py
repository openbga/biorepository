"""Allocation, release, pick and put-back. All data is synthetic."""

from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from distribution import services
from distribution.models import Allocation, AllocationEvent
from specimens.models import CustodyEvent, Hold
from specimens.tests.conftest import ask_disposal, occ_rev, release

from .helpers import (
    allocate,
    approved_request,
    blocked,
    exhausted_specimen,
    fast_password_hashing,  # noqa: F401
    fresh,
    hold_manually,
    make_specimen,
    pick,
    picked_request,
    refused,
    request_with,
    shipment_for,
)

pytestmark = pytest.mark.django_db


def allocate_to(people, request, specimens, item=None, below_minimum_ok=False, actor="manager"):
    request.refresh_from_db()
    item = item or request.items.get(line=1)
    return services.allocate(
        actor=people[actor],
        request_id=request.pk,
        expected_revision=request.revision,
        item_id=item.pk,
        choices=tuple(services.Choice(s.pk, fresh(s).revision) for s in specimens),
        below_minimum_ok=below_minimum_ok,
        occurred_at=timezone.now(),
        reason="Allocated for the approved request",
    )


def release_allocations(people, request, allocations, actor="manager"):
    request.refresh_from_db()
    return services.release_allocations(
        actor=people[actor],
        request_id=request.pk,
        expected_revision=request.revision,
        allocation_ids=tuple(a.pk for a in allocations),
        occurred_at=timezone.now(),
        reason="Not needed after all",
    )


def test_allocate_reserves_the_specimen_with_a_linked_disposal_hold(people, dist, world):
    request = approved_request(people, dist, world)
    revision = request.revision
    rows = allocate(people, request, dist.specimens[:2])
    assert len(rows) == 2 and {r.state for r in rows} == {"allocated"}
    assert request.revision == revision + 1
    for row in rows:
        hold = row.hold
        assert hold.scope == "disposal" and hold.reference == f"Allocated to {request.number}"
        assert not Hold.objects.filter(pk=hold.pk, release__isnull=False).exists()
        assert row.events.get().kind == "allocated"
    # The specimens stay where they are, in custody and available.
    for specimen in dist.specimens[:2]:
        assert fresh(specimen).status == "available" and fresh(specimen).open_checkout_id is None


def test_a_specimen_has_at_most_one_open_allocation_in_the_database(people, dist, world):
    first = approved_request(people, dist, world)
    second = approved_request(people, dist, world)
    row = allocate(people, first, dist.specimens[:1])[0]
    extra = Hold.objects.create(
        specimen=row.specimen,
        scope="disposal",
        reference="Allocated to RQ-999999",
        occurred_at=timezone.now(),
        actor=people["manager"],
        reason="Bypassing the services",
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        Allocation.objects.create(
            request=second,
            item=second.items.get(),
            specimen=row.specimen,
            hold=extra,
            allocated_by=people["manager"],
        )
    # A released allocation frees the specimen for the index.
    release_allocations(people, first, [row])
    with transaction.atomic():
        Allocation.objects.create(
            request=second,
            item=second.items.get(),
            specimen=row.specimen,
            hold=extra,
            allocated_by=people["manager"],
        )


def test_the_count_per_line_is_enforced(people, dist, world):
    request = request_with(people, dist, world, count=2)
    refused(lambda: allocate_to(people, request, dist.specimens[:3]), "invalid")
    assert not Allocation.objects.exists()
    allocate_to(people, request, dist.specimens[:1])
    allocate_to(people, request, dist.specimens[1:2])
    refused(lambda: allocate_to(people, request, dist.specimens[2:3]), "invalid")
    assert Allocation.objects.count() == 2


def test_a_released_allocation_does_not_count_against_the_line(people, dist, world):
    request = request_with(people, dist, world, count=1)
    rows = allocate_to(people, request, dist.specimens[:1])
    release_allocations(people, request, rows)
    assert len(allocate_to(people, request, dist.specimens[1:2])) == 1


def test_the_same_specimen_cannot_be_chosen_twice_or_unknown(people, dist, world):
    request = approved_request(people, dist, world)
    item = request.items.get()
    spec = services.Choice(dist.specimens[0].pk, fresh(dist.specimens[0]).revision)
    refused(
        lambda: services.allocate(
            actor=people["manager"],
            request_id=request.pk,
            expected_revision=request.revision,
            item_id=item.pk,
            choices=(spec, spec),
            below_minimum_ok=False,
            occurred_at=timezone.now(),
            reason="Twice",
        ),
        "invalid",
    )


def test_minimum_quantity_needs_below_minimum_ok(people, dist, world):
    request = request_with(
        people,
        dist,
        world,
        items=(
            services.ItemSpec(
                line=1,
                specimen_type_id=world.plasma.pk,
                count=2,
                minimum_quantity=Decimal("5"),
                unit_id=world.ml.pk,
            ),
        ),
    )
    refused(lambda: allocate_to(people, request, dist.specimens[:1]), "insufficient_quantity")
    assert not Allocation.objects.exists()
    rows = allocate_to(people, request, dist.specimens[:1], below_minimum_ok=True)
    assert len(rows) == 1
    big = make_specimen(people, world, "A1", quantity="6.000000")
    assert len(allocate_to(people, request, [big])) == 1  # at or above the minimum: no flag


def test_the_linked_hold_cannot_be_released_through_the_generic_release(people, dist, world):
    request = approved_request(people, dist, world)
    row = allocate(people, request, dist.specimens[:1])[0]
    refused(lambda: release(people["manager"], row.hold), "invalid_transition")
    assert not Hold.objects.filter(pk=row.hold_id, release__isnull=False).exists()


def test_disposal_is_refused_for_an_allocated_specimen(people, dist, world):
    request = approved_request(people, dist, world)
    allocate(people, request, dist.specimens[:1])
    refused(lambda: ask_disposal(people["technician"], dist.specimens[0]), "held")


def test_release_allocations_releases_the_hold_and_frees_the_specimen(people, dist, world):
    request = approved_request(people, dist, world)
    rows = allocate(people, request, dist.specimens[:2])
    by = {r.specimen_id: r for r in rows}  # allocate returns rows in specimen id order
    first, second = by[dist.specimens[0].pk], by[dist.specimens[1].pk]
    released = release_allocations(people, request, [first])
    assert [r.state for r in released] == ["released"]
    assert fresh(second).state == "allocated"
    assert Hold.objects.get(pk=first.hold_id).release is not None
    assert Hold.objects.filter(pk=second.hold_id, release__isnull=True).exists()
    assert list(first.events.values_list("kind", flat=True)) == ["allocated", "released"]
    other = approved_request(people, dist, world)
    assert len(allocate(people, other, dist.specimens[:1])) == 1
    # disposal can be requested again once the allocation hold is gone and nothing else holds it
    refused(lambda: ask_disposal(people["technician"], dist.specimens[1]), "held")


def test_release_allocations_refuses_picked_and_foreign_allocations(people, dist, world):
    request, allocations = picked_request(people, dist, world, count=1)
    refused(lambda: release_allocations(people, request, allocations), "invalid_transition")
    other = approved_request(people, dist, world)
    refused(lambda: release_allocations(people, other, allocations), "invalid")


def test_allocation_is_all_or_none(people, dist, world):
    request = approved_request(people, dist, world)
    bad = exhausted_specimen(people, world, "A1")
    holds_before = Hold.objects.count()
    found = blocked(lambda: allocate_to(people, request, [dist.specimens[0], bad]))
    assert found == {bad.accession: ["not_available"]}
    assert not Allocation.objects.exists() and Hold.objects.count() == holds_before
    assert not AllocationEvent.objects.exists()
    fresh(dist.specimens[0])
    assert len(allocate_to(people, request, dist.specimens[:1])) == 1


def test_allocation_refuses_a_stale_specimen_revision(people, dist, world):
    request = approved_request(people, dist, world)
    item = request.items.get()
    stale = fresh(dist.specimens[0]).revision
    hold_manually(people["technician"], dist.specimens[0], "disposal")  # bumps the revision
    refused(
        lambda: services.allocate(
            actor=people["manager"],
            request_id=request.pk,
            expected_revision=request.revision,
            item_id=item.pk,
            choices=(services.Choice(dist.specimens[0].pk, stale),),
            below_minimum_ok=False,
            occurred_at=timezone.now(),
            reason="Stale page",
        ),
        "stale_revision",
    )
    assert not Allocation.objects.exists()


def test_allocating_to_a_line_of_another_request_is_refused(people, dist, world):
    request = approved_request(people, dist, world)
    other = approved_request(people, dist, world)
    request.refresh_from_db()
    refused(
        lambda: services.allocate(
            actor=people["manager"],
            request_id=request.pk,
            expected_revision=request.revision,
            item_id=other.items.get().pk,
            choices=(services.Choice(dist.specimens[0].pk, fresh(dist.specimens[0]).revision),),
            below_minimum_ok=False,
            occurred_at=timezone.now(),
            reason="Wrong line",
        ),
        "invalid",
    )


# ------------------------------------------------------------------ pick


def test_pick_checks_the_specimens_out_and_frees_the_positions(people, dist, world):
    request = approved_request(people, dist, world)
    allocations = allocate(people, request, dist.specimens[:2])
    positions = [fresh(s).occupancy.position_id for s in dist.specimens[:2]]
    assert all(positions)
    rows = pick(people, request, allocations)
    assert {r.state for r in rows} == {"picked"}
    for specimen in dist.specimens[:2]:
        specimen = fresh(specimen)
        assert specimen.open_checkout_id and specimen.custodian_id == people["technician"].pk
        assert specimen.occupancy.position_id is None
    by = {a.specimen_id: a for a in allocations}
    event = CustodyEvent.objects.get(pk=fresh(dist.specimens[0]).open_checkout_id)
    assert event.kind == "check_out" and event.destination == f"Request {request.number}"
    picked_event = AllocationEvent.objects.get(allocation=by[dist.specimens[0].pk], kind="picked")
    assert picked_event.custody_event_id == event.pk


def test_a_missing_line_records_not_found_and_moves_nothing(people, dist, world):
    request = approved_request(people, dist, world)
    by = {a.specimen_id: a for a in allocate(people, request, dist.specimens[:2])}
    found, missing = by[dist.specimens[0].pk], by[dist.specimens[1].pk]
    pick(people, request, [found, missing], missing={missing.pk})
    assert fresh(found).state == "picked"
    assert fresh(missing).state == "allocated"
    assert AllocationEvent.objects.filter(allocation=missing, kind="not_found").exists()
    assert fresh(dist.specimens[1]).open_checkout_id is None
    assert fresh(dist.specimens[1]).occupancy.position_id is not None


def test_only_allocated_specimens_can_be_picked(people, dist, world):
    request, allocations = picked_request(people, dist, world, count=1)
    refused(lambda: pick(people, request, allocations), "invalid_transition")


def test_pick_refuses_a_stale_specimen_revision_and_rolls_everything_back(people, dist, world):
    request = approved_request(people, dist, world)
    allocations = allocate(people, request, dist.specimens[:2])
    request.refresh_from_db()
    lines = [
        services.PickLine(
            allocation_id=a.pk,
            expected_revision=fresh(a.specimen).revision + (5 if i else 0),
            expected_occupancy_revision=occ_rev(a.specimen),
        )
        for i, a in enumerate(allocations)
    ]
    refused(
        lambda: services.pick(
            actor=people["technician"],
            request_id=request.pk,
            expected_revision=request.revision,
            lines=tuple(lines),
            recipient_id=people["technician"].pk,
            occurred_at=timezone.now(),
            reason="Retrieved",
        ),
        "stale_revision",
    )
    assert {fresh(a).state for a in allocations} == {"allocated"}
    assert not CustodyEvent.objects.filter(kind="check_out").exists()


# ------------------------------------------------------------------ put back


def put_back(people, world, request, allocations, *, release=False, actor="technician"):
    request.refresh_from_db()
    lines = []
    for i, allocation in enumerate(allocations):
        specimen = fresh(allocation.specimen)
        label = ["C1", "C2", "C3"][i]
        lines.append(
            services.PutBackLine(
                allocation_id=allocation.pk,
                expected_revision=specimen.revision,
                expected_occupancy_revision=occ_rev(specimen),
                position_id=world.position(label).pk,
            )
        )
    return services.put_back(
        actor=people[actor],
        request_id=request.pk,
        expected_revision=request.revision,
        lines=tuple(lines),
        recipient_id=people[actor].pk,
        release=release,
        occurred_at=timezone.now(),
        reason="Returned to storage",
    )


def test_put_back_returns_the_specimen_to_storage_and_keeps_it_allocated(people, dist, world):
    request, allocations = picked_request(people, dist, world, count=2)
    rows = put_back(people, world, request, allocations)
    assert {r.state for r in rows} == {"allocated"}
    for specimen in dist.specimens[:2]:
        specimen = fresh(specimen)
        assert specimen.open_checkout_id is None and specimen.occupancy.position_id is not None
    assert AllocationEvent.objects.filter(kind="put_back", allocation=allocations[0]).get()
    assert not Hold.objects.filter(
        pk=allocations[0].hold_id, release__isnull=False
    ).exists()  # the allocation (and its hold) stays
    assert len(pick(people, request, allocations)) == 2  # and can be picked again


def test_put_back_with_release_releases_the_allocation_and_its_hold(people, dist, world):
    request, allocations = picked_request(people, dist, world, count=1)
    rows = put_back(people, world, request, allocations, release=True, actor="manager")
    assert [r.state for r in rows] == ["released"]
    assert Hold.objects.get(pk=allocations[0].hold_id).release is not None
    assert list(allocations[0].events.values_list("kind", flat=True)) == [
        "allocated",
        "picked",
        "put_back",
        "released",
    ]


def test_put_back_is_refused_in_a_packed_shipment_until_unpacked(people, dist, world):
    request, allocations, shipment = shipment_for(people, dist, world, count=1)
    refused(lambda: put_back(people, world, request, allocations), "invalid_transition")
    assert fresh(allocations[0]).state == "picked"
    shipment.refresh_from_db()
    services.unpack_shipment(
        actor=people["technician"],
        shipment_id=shipment.pk,
        expected_revision=shipment.revision,
        reason="Taking it out again",
    )
    rows = put_back(people, world, request, allocations)
    assert rows[0].state == "allocated"
    allocations[0].refresh_from_db()
    assert allocations[0].shipment_id is None


def test_only_picked_specimens_can_be_put_back(people, dist, world):
    request = approved_request(people, dist, world)
    allocations = allocate(people, request, dist.specimens[:1])
    refused(lambda: put_back(people, world, request, allocations), "invalid_transition")
