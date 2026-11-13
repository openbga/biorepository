"""Ledger, consumption, adjustment, reversal and derivation (aliquots and derivatives)."""

from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Sum

from core.models import AuditEvent
from specimens import queries, services
from specimens.models import Derivation, QuantityEntry, Specimen
from storage.models import Occupancy

from .conftest import (
    ago,
    check_in,
    check_out,
    child,
    codes,
    consume,
    derive,
    fresh,
    place,
    register,
    stored,
)

pytestmark = pytest.mark.django_db

D = Decimal


def refused(code, fn, *args, **kwargs):
    with pytest.raises(ValidationError) as exc:
        fn(*args, **kwargs)
    assert code in codes(exc.value)


def refused_child(index, fn, *args, **kwargs):
    """A bad child row is reported under its own key, `children-<index>`."""
    with pytest.raises(ValidationError) as exc:
        fn(*args, **kwargs)
    assert set(exc.value.message_dict) == {f"children-{index}"}


def assert_ledger(specimen):
    """quantity_on_hand is the sum of the ledger and no balance ever went negative."""
    specimen = fresh(specimen)
    entries = list(specimen.ledger.order_by("recorded_at", "id"))
    assert specimen.quantity_on_hand == sum((e.delta for e in entries), D(0))
    assert specimen.quantity_on_hand == (entries[-1].balance_after if entries else 0)
    running = D(0)
    for entry in entries:
        running += entry.delta
        assert entry.balance_after == running >= 0
    assert specimen.ledger.aggregate(total=Sum("delta"))["total"] == specimen.quantity_on_hand


def out(world, actor, **kwargs):
    """A placed specimen that is now checked out (ready to be consumed or derived)."""
    specimen = stored(actor, world, **kwargs)
    check_out(actor, specimen, actor)
    return fresh(specimen)


# --- derivation --------------------------------------------------------------------------------


def test_aliquots_debit_the_parent_and_must_add_up(people, world):
    t = people["technician"]
    parent = out(world, t, quantity="2")
    kids = derive(t, parent, [child(world, "0.5"), child(world, "0.75")], "1.25")
    parent = fresh(parent)
    assert parent.quantity_on_hand == D("0.75") and parent.status == "available"
    assert sorted(k.quantity_on_hand for k in kids) == [D("0.5"), D("0.75")]
    assert all(k.derivation_kind == "aliquot" and k.parent_id == parent.pk for k in kids)
    assert len({k.accession for k in kids} | {parent.accession}) == 3
    derivation = Derivation.objects.get(parent=parent)
    assert derivation.consumed == D("1.25") and derivation.children.count() == 2
    for specimen in (parent, *kids):
        assert_ledger(specimen)
    assert parent.ledger.get(kind="derive").delta == D("-1.25")
    assert AuditEvent.objects.filter(action="specimens.specimen_derived").count() == 1


def test_aliquot_children_must_sum_to_the_amount_consumed(people, world):
    t = people["technician"]
    parent = out(world, t, quantity="2")
    refused("invalid", derive, t, parent, [child(world, "0.5"), child(world, "0.5")], "1.5")
    refused("invalid", derive, t, parent, [child(world, "0.5")], "0.4")
    assert fresh(parent).quantity_on_hand == D("2") and not Derivation.objects.exists()


def test_aliquots_keep_the_parent_type_and_unit_but_derivatives_may_change_them(people, world):
    t = people["technician"]
    parent = out(world, t, quantity="2")
    other_type = [child(world, "1", specimen_type_id=world.serum.pk)]
    refused_child(0, derive, t, parent, other_type, "1")
    refused_child(0, derive, t, parent, [child(world, "1000", unit_id=world.ul.pk)], "1")
    assert not Specimen.objects.filter(parent=parent).exists()
    kids = derive(
        t,
        parent,
        [child(world, "900", specimen_type_id=world.serum.pk, unit_id=world.ul.pk)],
        "1",
        kind="derivative",
    )
    assert (kids[0].specimen_type_id, kids[0].unit_id) == (world.serum.pk, world.ul.pk)
    assert kids[0].derivation_kind == "derivative" and fresh(parent).quantity_on_hand == D("1")
    assert_ledger(kids[0])


def test_insufficient_quantity_is_refused(people, world):
    t = people["technician"]
    parent = out(world, t, quantity="1")
    refused("insufficient_quantity", derive, t, parent, [child(world, "1.5")], "1.5")
    refused("insufficient_quantity", consume, t, parent, "1.5")
    assert fresh(parent).quantity_on_hand == D("1")


def test_derivation_input_is_exact(people, world):
    t = people["technician"]
    parent = out(world, t, quantity="2")
    refused("invalid", derive, t, parent, [child(world, "1")], 1.0)
    refused("invalid", derive, t, parent, [child(world, "1")], D("1.0000001"))
    refused("invalid", derive, t, parent, [], "1")
    refused("invalid", derive, t, parent, [child(world, "0")], "0")


def test_a_stored_parent_must_be_checked_out_but_a_never_stored_one_may_be_split(people, world):
    t = people["technician"]
    in_box = stored(t, world, quantity="2")
    refused("invalid_transition", derive, t, in_box, [child(world, "1")], "1")
    refused("invalid_transition", consume, t, in_box, "1")
    loose = register(t, world, quantity="2")  # registered, never stored
    kids = derive(t, loose, [child(world, "1"), child(world, "1")], "2", occurred_at=ago(200))
    assert len(kids) == 2 and fresh(loose).status == "exhausted"


def test_children_inherit_context_custodian_and_a_known_thaw_history(people, world):
    t, t2 = people["technician"], people["technician2"]
    parent = stored(t, world, quantity="2", baseline=2)
    check_out(t, parent, t2)
    check_in(t, parent, world.position("A1"), thaw=True, recipient=t2, occurred_at=ago(195))
    check_out(t2, parent, t2, occurred_at=ago(192))
    kids = derive(t2, parent, [child(world, "1")], "1", occurred_at=ago(190))
    kid = kids[0]
    assert kid.participant_id == parent.participant_id == world.participant.pk
    assert kid.collection_id == world.collection.pk and kid.collected_at == parent.collected_at
    assert kid.custodian_id == t2.pk
    summary = queries.thaw_summary(kid)
    assert (summary.baseline, summary.inherited, summary.total) == (3, 0, 3)  # 2 known + 1 observed
    assert kid.occupancy.position_id is None  # children start unplaced


def test_children_of_an_unknown_baseline_keep_the_parents_recorded_thaws(people, world):
    t = people["technician"]
    parent = register(t, world, quantity="2", baseline=None)
    services.record_thaw(
        actor=t,
        specimen_id=parent.pk,
        expected_revision=fresh(parent).revision,
        occurred_at=ago(200),
        reason="Thawed on receipt",
    )
    kid = derive(t, parent, [child(world, "1")], "1", occurred_at=ago(190))[0]
    summary = queries.thaw_summary(kid)
    assert summary.baseline is None and summary.inherited == 1 and summary.total is None
    assert str(summary) == "Unknown before registration; 1 recorded"


def test_children_can_be_placed_in_the_same_transaction(people, world):
    t = people["technician"]
    parent = out(world, t, quantity="2")
    kids = derive(
        t,
        parent,
        [child(world, "1", world.position("C1")), child(world, "1", world.position("C2"))],
        "2",
    )
    assert {fresh(k).occupancy.position.label for k in kids} == {"C1", "C2"}


def test_derivation_is_all_or_nothing(people, world):
    t = people["technician"]
    parent = out(world, t, quantity="2")
    stored(t, world, "C3")  # occupied target
    snapshot = (
        Specimen.objects.count(),
        Occupancy.objects.count(),
        QuantityEntry.objects.count(),
        AuditEvent.objects.count(),
    )

    def unchanged():
        assert snapshot == (
            Specimen.objects.count(),
            Occupancy.objects.count(),
            QuantityEntry.objects.count(),
            AuditEvent.objects.count(),
        )
        assert fresh(parent).quantity_on_hand == D("2") and not Derivation.objects.exists()

    # One invalid child row (fractional count unit) among valid ones.
    refused_child(
        1,
        derive,
        t,
        parent,
        [child(world, "1"), child(world, "1.5", unit_id=world.count.pk)],
        "2",
        kind="derivative",
    )
    unchanged()
    # Two children aimed at the same position.
    same = world.position("C1")
    refused("occupied", derive, t, parent, [child(world, "1", same), child(world, "1", same)], "2")
    unchanged()
    # The second child's target is occupied: the first child is rolled back as well.
    refused(
        "occupied",
        derive,
        t,
        parent,
        [child(world, "1", world.position("C1")), child(world, "1", world.position("C3"))],
        "2",
    )
    unchanged()


# --- consume, adjust, reverse ------------------------------------------------------------------


def test_consuming_to_zero_exhausts_the_vessel_without_freeing_its_position(people, world):
    t = people["technician"]
    specimen = out(world, t, quantity="1")
    entry = consume(t, specimen, "1")
    assert entry.delta == D("-1") and entry.balance_after == D(0)
    specimen = fresh(specimen)
    assert specimen.status == "exhausted" and specimen.quantity_on_hand == 0
    # The empty vessel may still be returned to storage, and it keeps its position.
    check_in(t, specimen, world.position("A1"), occurred_at=ago(150))
    specimen = fresh(specimen)
    assert specimen.status == "exhausted"
    assert specimen.occupancy.position_id == world.position("A1").pk
    refused("invalid_transition", consume, t, specimen, "0.1")
    assert_ledger(specimen)


def test_an_exhausted_unplaced_vessel_cannot_be_placed(people, world):
    t = people["technician"]
    specimen = register(t, world, quantity="1")
    consume(t, specimen, "1", occurred_at=ago(200))
    refused("invalid_transition", place, t, specimen, world.position("A1"), ago(150))


def test_adjustment_restores_availability_and_is_manager_only(people, world):
    t, m = people["technician"], people["manager"]
    specimen = stored(t, world, quantity="1")
    adjust = lambda actor, delta: services.adjust_quantity(  # noqa: E731
        actor=actor,
        specimen_id=specimen.pk,
        expected_revision=fresh(specimen).revision,
        delta=D(delta),
        supplied_text=delta,
        occurred_at=ago(100),
        reason="Re-measured by weight",
    )
    with pytest.raises(PermissionDenied):
        adjust(t, "-0.5")
    adjust(m, "-1")  # the stored vessel is emptied: no check-out needed for a measured correction
    assert fresh(specimen).status == "exhausted"
    assert fresh(specimen).occupancy.position_id == world.position("A1").pk
    adjust(m, "0.4")
    assert fresh(specimen).status == "available" and fresh(specimen).quantity_on_hand == D("0.4")
    refused("insufficient_quantity", adjust, m, "-0.5")
    assert_ledger(specimen)


def reverse(actor, entry, specimen, reason="Entered in error"):
    return services.reverse_quantity(
        actor=actor, entry_id=entry.pk, expected_revision=fresh(specimen).revision, reason=reason
    )


def test_a_consumption_is_reversed_once_by_a_manager(people, world):
    t, m = people["technician"], people["manager"]
    specimen = out(world, t, quantity="1")
    entry = consume(t, specimen, "1")
    assert fresh(specimen).status == "exhausted"
    with pytest.raises(PermissionDenied):
        reverse(t, entry, specimen)
    reversal = reverse(m, entry, specimen)
    assert reversal.kind == "reversal" and reversal.reverses_id == entry.pk
    assert reversal.delta == D("1") and fresh(specimen).status == "available"
    refused("invalid_transition", reverse, m, entry, specimen)  # only once
    refused("invalid_transition", reverse, m, reversal, specimen)  # never a reversal of a reversal
    assert_ledger(specimen)


def test_only_consume_and_adjust_entries_can_be_reversed(people, world):
    t, m = people["technician"], people["manager"]
    specimen = out(world, t, quantity="2")
    initial = specimen.ledger.get(kind="initial")
    refused("invalid_transition", reverse, m, initial, specimen)
    derive(t, specimen, [child(world, "1")], "1")
    refused("invalid_transition", reverse, m, specimen.ledger.get(kind="derive"), specimen)


def test_a_reversal_cannot_drive_the_balance_negative(people, world):
    t, m = people["technician"], people["manager"]
    specimen = out(world, t, quantity="1")
    added = services.adjust_quantity(
        actor=m,
        specimen_id=specimen.pk,
        expected_revision=fresh(specimen).revision,
        delta=D("0.5"),
        supplied_text="0.5",
        occurred_at=ago(150),
        reason="Re-measured",
    )
    consume(t, specimen, "1.5", occurred_at=ago(140))
    refused("insufficient_quantity", reverse, m, added, specimen)
    assert fresh(specimen).quantity_on_hand == 0
    assert_ledger(specimen)


def test_quantity_changes_need_the_current_revision(people, world):
    t = people["technician"]
    specimen = out(world, t, quantity="1")
    stale = fresh(specimen).revision - 1
    refused(
        "stale_revision",
        services.consume_quantity,
        actor=t,
        specimen_id=specimen.pk,
        expected_revision=stale,
        quantity=D("0.1"),
        supplied_text="0.1",
        occurred_at=ago(150),
        reason="Assay",
    )
    assert_ledger(specimen)


def test_the_ledger_stays_consistent_through_a_mixed_history(people, world):
    t, m = people["technician"], people["manager"]
    specimen = out(world, t, quantity="3")
    consume(t, specimen, "0.25", occurred_at=ago(190))
    derive(t, specimen, [child(world, "1"), child(world, "0.5")], "1.5", occurred_at=ago(180))
    entry = consume(t, specimen, "1.25", occurred_at=ago(170))
    reverse(m, entry, specimen)
    assert fresh(specimen).quantity_on_hand == D("1.25")
    assert_ledger(specimen)
    for kid in Specimen.objects.filter(parent=specimen):
        assert_ledger(kid)
