"""Specimens support for the distribution app: linked holds, dispatch, return, lineage context.

The distribution app calls these inside its own storage operation holding the gate; the tests do
the same with `storage.operation()` + `storage.lock_gate()`. All data is synthetic.
"""

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.utils import timezone

from core.models import AuditEvent
from specimens import queries, services
from specimens.models import (
    Correction,
    CustodyEvent,
    Hold,
    HoldRelease,
    QuantityEntry,
    Specimen,
    ThawEvent,
)
from storage import services as storage

from .conftest import (
    ago,
    ask_disposal,
    catalogue,
    check_in,
    check_out,
    child,
    codes,
    consume,
    decide,
    derive,
    execute,
    fresh,
    hold,
    occ_rev,
    place,
    registration,
    release,
    stored,
)

pytestmark = pytest.mark.django_db

D = Decimal


def gated(fn, **kwargs):
    """Call an internal service the way distribution does: inside an operation holding the gate."""
    with storage.operation():
        storage.lock_gate()
        return fn(**kwargs)


def linked_hold(actor, specimen, scope="disposal", reference="Allocated to RQ-000012", at=None):
    return gated(
        services.place_linked_hold,
        actor=actor,
        specimen_id=specimen.pk,
        scope=scope,
        reference=reference,
        occurred_at=at or ago(150),
        reason="Allocated to an approved request",
    )


def dispatch(actor, specimen, at=None):
    return gated(
        services.dispatch_specimen,
        actor=actor,
        specimen_id=specimen.pk,
        expected_revision=fresh(specimen).revision,
        destination="SH-000003 → Example University",
        occurred_at=at or ago(120),
        reason="Shipment dispatched",
    )


def give_back(actor, specimen, quantity="1.5", thaw_count=None, at=None):
    return gated(
        services.record_return,
        actor=actor,
        specimen_id=specimen.pk,
        expected_revision=fresh(specimen).revision,
        quantity=D(quantity),
        supplied_text=quantity,
        recipient_id=actor.pk,
        thaw_count=thaw_count,
        source="Example University (SH-000003)",
        occurred_at=at or ago(60),
        reason="Returned unused",
    )


def test_internal_services_refuse_to_run_without_the_callers_gate(people, world):
    specimen = stored(people["technician"], world)
    with pytest.raises(RuntimeError):
        services.place_linked_hold(
            actor=people["technician"],
            specimen_id=specimen.pk,
            scope="disposal",
            reference="Allocated to RQ-000012",
            occurred_at=ago(150),
            reason="No gate",
        )
    assert not Hold.objects.exists()


def test_dispatch_and_return_round_trip(people, world):
    tech = people["technician"]
    specimen = stored(tech, world)
    check_out(tech, specimen, tech, occurred_at=ago(200))
    event = dispatch(tech, specimen, at=ago(120))
    specimen = fresh(specimen)
    assert event.kind == CustodyEvent.Kind.DISPATCH and event.to_custodian_id is None
    assert specimen.status == "distributed" and specimen.quantity_on_hand == 0
    assert specimen.custodian_id is None and specimen.open_checkout_id is None
    assert specimen.ledger.filter(kind=QuantityEntry.Kind.DISTRIBUTE, delta=D("-2")).exists()
    with pytest.raises(ValidationError) as exc:
        check_out(tech, specimen, tech, occurred_at=ago(110))
    assert "invalid_transition" in codes(exc.value)

    returned = give_back(tech, specimen, quantity="1.5", thaw_count=None, at=ago(60))
    specimen = fresh(specimen)
    assert specimen.status == "available" and specimen.quantity_on_hand == D("1.5")
    assert specimen.open_checkout_id == returned.pk and specimen.custodian_id == tech.pk
    external = ThawEvent.objects.get(specimen=specimen, kind=ThawEvent.Kind.EXTERNAL)
    assert external.count is None and external.custody_event_id == returned.pk
    summary = queries.thaw_summary(specimen)
    assert not summary.known and "unknown while away" in str(summary)

    check_in(tech, specimen, world.position("A2"), occurred_at=ago(30))
    out = queries.time_out_of_storage(specimen, now=timezone.now())
    assert [i.closed_by for i in out.intervals] == ["dispatch", "check_in"]
    assert len(out.away) == 1 and out.away[0].ended == returned.occurred_at
    # Pick to dispatch (80 min) plus return to check-in (30 min); the time away is not counted.
    assert abs(out.total - timedelta(minutes=110)) < timedelta(seconds=5)


def test_linked_hold_only_released_through_its_workflow(people, world):
    tech, manager = people["technician"], people["manager"]
    specimen = stored(tech, world)
    hold = linked_hold(tech, specimen)
    with pytest.raises(ValidationError) as exc:
        services.release_hold(
            actor=manager,
            hold_id=hold.pk,
            expected_revision=fresh(specimen).revision,
            occurred_at=ago(140),
            reason="Generic release",
        )
    assert "invalid_transition" in codes(exc.value)
    release = gated(
        services.release_linked_hold,
        actor=tech,
        hold_id=hold.pk,
        occurred_at=ago(140),
        reason="Allocation released",
    )
    assert release.hold_id == hold.pk


def test_lineage_context_fills_a_missing_collection_for_the_family(people, world):
    manager = people["manager"]
    specimen = services.register_specimen(
        actor=people["technician"],
        data=replace(registration(world), collection_id=None),
        occurred_at=ago(270),
        reason="Received without a requisition",
    )
    assert specimen.collection_id is None
    family = services.assign_lineage_context(
        actor=manager,
        specimen_id=specimen.pk,
        expected_revision=specimen.revision,
        participant_id=None,
        collection_id=world.collection.pk,
        reason="Collection recorded on the requisition (synthetic)",
    )
    assert [s.collection_id for s in family] == [world.collection.pk]


# --- shared scaffolding for the tests below ---------------------------------------------------


def refused(code, fn, *args, **kwargs):
    with pytest.raises(ValidationError) as exc:
        fn(*args, **kwargs)
    assert code in codes(exc.value)
    return exc.value


def return_kwargs(actor, specimen, quantity="1.5", thaw_count=None, at=None, **over):
    return {
        "actor": actor,
        "specimen_id": specimen.pk,
        "expected_revision": fresh(specimen).revision,
        "quantity": D(quantity),
        "supplied_text": quantity,
        "recipient_id": actor.pk,
        "thaw_count": thaw_count,
        "source": "Example University (SH-000003)",
        "occurred_at": at or ago(60),
        "reason": "Returned unused",
        **over,
    }


def picked(people, world, **kwargs):
    """A stored specimen checked out (picked) by the technician."""
    tech = people["technician"]
    specimen = stored(tech, world, **kwargs)
    check_out(tech, specimen, tech, occurred_at=ago(200))
    return fresh(specimen)


def shipped(people, world, **kwargs):
    """Picked, one consume of 0.5 mL, then dispatched."""
    tech = people["technician"]
    specimen = picked(people, world, **kwargs)
    entry = consume(tech, specimen, "0.5", occurred_at=ago(190))
    event = dispatch(tech, specimen, at=ago(120))
    return SimpleNamespace(specimen=fresh(specimen), entry=entry, event=event)


def written(specimen):
    """Everything an internal service may write, as comparable counts."""
    return (
        Hold.objects.count(),
        HoldRelease.objects.count(),
        CustodyEvent.objects.count(),
        QuantityEntry.objects.count(),
        ThawEvent.objects.count(),
        AuditEvent.objects.count(),
        fresh(specimen).revision,
    )


INTERNAL = (
    "place_linked_hold",
    "release_linked_hold",
    "dispatch_specimen",
    "record_return",
)


def scenario(name, people, world):
    """(specimen, build) where build(actor, **over) gives valid kwargs for the service `name`."""
    tech = people["technician"]
    specimen = stored(tech, world)
    if name in ("dispatch_specimen", "record_return"):
        check_out(tech, specimen, tech, occurred_at=ago(200))
    if name == "record_return":
        dispatch(tech, specimen, at=ago(120))
    held = linked_hold(tech, specimen) if name == "release_linked_hold" else None

    def build(actor, **over):
        base = {
            "place_linked_hold": lambda: {
                "specimen_id": specimen.pk,
                "scope": "disposal",
                "reference": "Allocated to RQ-000012",
                "occurred_at": ago(150),
                "reason": "Allocated",
            },
            "release_linked_hold": lambda: {
                "hold_id": held.pk,
                "occurred_at": ago(140),
                "reason": "Released",
            },
            "dispatch_specimen": lambda: {
                "specimen_id": specimen.pk,
                "expected_revision": fresh(specimen).revision,
                "destination": "Example University",
                "occurred_at": ago(120),
                "reason": "Shipped",
            },
            "record_return": lambda: {
                k: v for k, v in return_kwargs(actor, specimen).items() if k not in ("actor",)
            },
        }[name]()
        return {"actor": actor, **base, **over}

    return specimen, build


# --- 1-3. internal services: gate, roles, chronology, stale revision ---------------------------


@pytest.mark.parametrize("name", INTERNAL)
def test_internal_services_need_the_callers_gate_and_write_nothing(name, people, world):
    specimen, build = scenario(name, people, world)
    before = written(specimen)
    with pytest.raises(RuntimeError):
        getattr(services, name)(**build(people["technician"]))
    assert written(specimen) == before


@pytest.mark.parametrize("name", INTERNAL)
@pytest.mark.parametrize("who", ["viewer", "nobody"])
def test_internal_services_refuse_users_without_an_operating_role(name, who, people, world):
    specimen, build = scenario(name, people, world)
    before = written(specimen)
    with pytest.raises(PermissionDenied):
        gated(getattr(services, name), **build(people[who]))
    assert written(specimen) == before


@pytest.mark.parametrize("name", INTERNAL)
def test_internal_services_work_for_a_technician(name, people, world):
    specimen, build = scenario(name, people, world)
    before = written(specimen)
    gated(getattr(services, name), **build(people["technician"]))
    assert written(specimen)[-1] == before[-1] + 1


@pytest.mark.parametrize("name", INTERNAL)
@pytest.mark.parametrize("when", ["before", "future", "naive"])
def test_internal_services_check_the_event_time(name, when, people, world):
    specimen, build = scenario(name, people, world)
    at = {
        "before": ago(400),
        "future": timezone.now() + timedelta(hours=1),
        "naive": datetime.now(),  # noqa: DTZ005 - the point of the test
    }[when]
    before = written(specimen)
    refused(
        "invalid_time",
        gated,
        getattr(services, name),
        **build(people["technician"], occurred_at=at),
    )
    assert written(specimen) == before


@pytest.mark.parametrize("name", ["dispatch_specimen", "record_return"])
def test_dispatch_and_return_refuse_a_stale_revision(name, people, world):
    specimen, build = scenario(name, people, world)
    stale = fresh(specimen).revision - 1
    before = written(specimen)
    refused(
        "stale_revision",
        gated,
        getattr(services, name),
        **build(people["technician"], expected_revision=stale),
    )
    assert written(specimen) == before


# --- 4. dispatch ----------------------------------------------------------------------------


def test_dispatch_needs_a_pick(people, world):
    specimen = stored(people["technician"], world)
    refused("invalid_transition", dispatch, people["technician"], specimen)
    assert fresh(specimen).status == "available"


def test_dispatch_refuses_an_exhausted_specimen(people, world):
    tech = people["technician"]
    specimen = picked(people, world)
    consume(tech, specimen, "2", occurred_at=ago(190))
    assert fresh(specimen).status == "exhausted"
    refused("invalid_transition", dispatch, tech, specimen)


def test_dispatch_refuses_a_disposed_specimen(people, world):
    tech = people["technician"]
    specimen = stored(tech, world)
    request = ask_disposal(tech, specimen)
    decide(people["approver"], request)
    execute(tech, request, occurred_at=timezone.now())
    assert fresh(specimen).status == "disposed"
    refused("invalid_transition", dispatch, tech, specimen)


def test_dispatch_twice_is_refused(people, world):
    tech = people["technician"]
    specimen = picked(people, world)
    dispatch(tech, specimen)
    refused("invalid_transition", dispatch, tech, specimen, at=ago(100))
    assert specimen.custody.filter(kind=CustodyEvent.Kind.DISPATCH).count() == 1


@pytest.mark.parametrize("scope", ["use", "all_use"])
@pytest.mark.parametrize("linked", [False, True])
def test_a_use_hold_blocks_dispatch(scope, linked, people, world):
    tech = people["technician"]
    specimen = picked(people, world)
    if linked:
        linked_hold(tech, specimen, scope=scope)
    else:
        hold(tech, specimen, scope)
    error = refused("held", dispatch, tech, specimen)
    assert fresh(specimen).status == "available"
    if linked:
        assert "RQ-000012" in str(error)


def test_a_disposal_hold_does_not_block_dispatch(people, world):
    tech = people["technician"]
    specimen = picked(people, world)
    linked_hold(tech, specimen, scope="disposal")
    dispatch(tech, specimen)
    assert fresh(specimen).status == "distributed"


def test_dispatch_records_custody_ledger_projection_and_audit(people, world):
    tech = people["technician"]
    specimen = picked(people, world)
    before_revision = specimen.revision
    occupancy_before = specimen.occupancy
    occupancy_revision = occ_rev(specimen)
    at = ago(120)
    event = dispatch(tech, specimen, at=at)
    specimen = fresh(specimen)
    assert event.kind == CustodyEvent.Kind.DISPATCH
    assert event.from_custodian_id == tech.pk and event.to_custodian_id is None
    assert event.destination == "SH-000003 → Example University"
    entry = specimen.ledger.get(kind=QuantityEntry.Kind.DISTRIBUTE)
    assert entry.delta == -D("2") and entry.balance_after == 0
    assert specimen.status == "distributed" and specimen.quantity_on_hand == 0
    assert specimen.custodian_id is None and specimen.open_checkout_id is None
    assert specimen.revision == before_revision + 1
    occupancy = type(occupancy_before).objects.get(pk=occupancy_before.pk)
    assert occupancy.revision > occupancy_revision and occupancy.last_occurred_at == at
    audit = AuditEvent.objects.get(action="specimens.specimen_dispatched")
    assert audit.object_id == str(specimen.pk) and audit.actor_id == tech.pk


# --- 5. what a distributed specimen may and may not do --------------------------------------


def _blocked_actions(people, world, away):
    tech, manager = people["technician"], people["manager"]
    s = away.specimen

    def rev():
        return fresh(s).revision

    return {
        "place": lambda: place(tech, s, world.position("A2"), occurred_at=ago(100)),
        "relocate": lambda: services.relocate_specimen(
            actor=tech,
            specimen_id=s.pk,
            expected_revision=rev(),
            expected_occupancy_revision=occ_rev(s),
            position_id=world.position("A2").pk,
            occurred_at=ago(100),
            reason="Move",
        ),
        "check_out": lambda: check_out(tech, s, tech, occurred_at=ago(100)),
        "check_in": lambda: check_in(tech, s, world.position("A2"), occurred_at=ago(100)),
        "transfer": lambda: services.transfer_custody(
            actor=tech,
            specimen_id=s.pk,
            expected_revision=rev(),
            expected_occupancy_revision=occ_rev(s),
            recipient_id=people["technician2"].pk,
            destination="Bench",
            occurred_at=ago(100),
            reason="Hand over",
        ),
        "derive": lambda: derive(tech, s, [child(world, "0.1")], "0.1", occurred_at=ago(100)),
        "consume": lambda: consume(tech, s, "0.1", occurred_at=ago(100)),
        "adjust": lambda: services.adjust_quantity(
            actor=manager,
            specimen_id=s.pk,
            expected_revision=rev(),
            delta=D("1"),
            supplied_text="1",
            occurred_at=ago(100),
            reason="Measured",
        ),
        "reverse": lambda: services.reverse_quantity(
            actor=manager, entry_id=away.entry.pk, expected_revision=rev(), reason="Mistake"
        ),
        "thaw": lambda: services.record_thaw(
            actor=tech,
            specimen_id=s.pk,
            expected_revision=rev(),
            occurred_at=ago(100),
            reason="Seen thawing",
        ),
        "request_disposal": lambda: ask_disposal(tech, s),
    }


@pytest.mark.parametrize(
    "action",
    [
        "place",
        "relocate",
        "check_out",
        "check_in",
        "transfer",
        "derive",
        "consume",
        "adjust",
        "reverse",
        "thaw",
        "request_disposal",
    ],
)
def test_physical_services_refuse_a_distributed_specimen(action, people, world):
    away = shipped(people, world)
    before = written(away.specimen)
    refused("invalid_transition", _blocked_actions(people, world, away)[action])
    assert written(away.specimen) == before


def test_approving_a_disposal_is_refused_while_distributed(people, world):
    tech = people["technician"]
    specimen = picked(people, world)
    request = ask_disposal(tech, specimen)
    dispatch(tech, specimen, at=timezone.now())
    refused("invalid_transition", decide, people["approver"], request)


def test_holds_corrections_and_linked_holds_are_allowed_while_distributed(people, world):
    tech, manager = people["technician"], people["manager"]
    away = shipped(people, world, baseline=0)
    s = away.specimen
    manual = hold(tech, s, "use", occurred_at=ago(110))
    release(manager, manual, occurred_at=ago(105))
    hold(tech, s, "disposal", occurred_at=ago(100))
    linked_hold(tech, s, scope="use", at=ago(90))

    bigger = catalogue(
        manager,
        "container_type",
        code="CRYOVIAL-5ML",
        name="Cryovial 5 mL",
        nominal_capacity="5",
        capacity_unit=world.ml.pk,
    )
    services.correct_specimen(
        actor=manager,
        specimen_id=s.pk,
        expected_revision=fresh(s).revision,
        values=services.CorrectionValues(
            fields=frozenset({"container_type"}), container_type_id=bigger.pk
        ),
        reason="Wrong vial type entered",
    )
    services.correct_custody(
        actor=manager,
        event_id=away.event.pk,
        expected_revision=fresh(s).revision,
        values={"destination": "Example University, Building 2"},
        reason="Destination typo",
    )
    baseline = ThawEvent.objects.get(specimen=s, kind=ThawEvent.Kind.BASELINE)
    services.correct_thaw(
        actor=manager,
        event_id=baseline.pk,
        expected_revision=fresh(s).revision,
        count=2,
        reason="Count confirmed",
    )
    assert fresh(s).status == "distributed"


# --- 6. record_return -----------------------------------------------------------------------


def test_return_of_a_specimen_that_is_not_distributed_is_refused(people, world):
    tech = people["technician"]
    specimen = picked(people, world)
    refused("invalid_transition", gated, services.record_return, **return_kwargs(tech, specimen))
    assert not ThawEvent.objects.filter(kind=ThawEvent.Kind.EXTERNAL).exists()


def test_a_zero_return_exhausts_the_specimen_without_a_ledger_row(people, world):
    tech = people["technician"]
    away = shipped(people, world)
    entries = QuantityEntry.objects.count()
    gated(services.record_return, **return_kwargs(tech, away.specimen, quantity="0"))
    specimen = fresh(away.specimen)
    assert specimen.status == "exhausted" and specimen.quantity_on_hand == 0
    assert QuantityEntry.objects.count() == entries
    assert specimen.custodian_id == tech.pk and specimen.open_checkout_id is not None


def test_a_positive_return_credits_the_ledger(people, world):
    tech = people["technician"]
    away = shipped(people, world)
    event = gated(services.record_return, **return_kwargs(tech, away.specimen, quantity="1.25"))
    specimen = fresh(away.specimen)
    entry = specimen.ledger.get(kind=QuantityEntry.Kind.RETURN)
    assert entry.delta == D("1.25") and entry.balance_after == D("1.25")
    assert specimen.status == "available" and specimen.quantity_on_hand == D("1.25")
    assert event.destination == "Example University (SH-000003)"
    assert event.from_custodian_id is None and event.to_custodian_id == tech.pk
    audit = AuditEvent.objects.get(action="specimens.specimen_returned")
    assert audit.object_id == str(specimen.pk)


def test_an_unreported_thaw_count_leaves_the_total_unknown(people, world):
    away = shipped(people, world, baseline=3)
    give_back(people["technician"], away.specimen, thaw_count=None)
    summary = queries.thaw_summary(fresh(away.specimen))
    assert not summary.known and summary.total is None
    assert "unknown while away" in str(summary)
    assert len(summary.unknown_away) == 1


def test_a_reported_thaw_count_is_added_to_the_total(people, world):
    away = shipped(people, world, baseline=3)
    give_back(people["technician"], away.specimen, thaw_count=2)
    summary = queries.thaw_summary(fresh(away.specimen))
    assert summary.known and summary.external == 2
    assert summary.total == summary.baseline + summary.recorded == 5


@pytest.mark.parametrize("count", [-1, 1001, True, "2"])
def test_an_invalid_reported_thaw_count_is_refused(count, people, world):
    away = shipped(people, world)
    before = written(away.specimen)
    error = refused(
        "invalid",
        gated,
        services.record_return,
        **return_kwargs(people["technician"], away.specimen, thaw_count=count),
    )
    assert "thaw_count" in error.error_dict
    assert written(away.specimen) == before


def test_the_return_recipient_must_be_active_staff(people, world):
    tech = people["technician"]
    away = shipped(people, world)
    people["technician2"].is_active = False
    people["technician2"].save()
    for who in ("technician2", "nobody"):
        refused(
            "invalid",
            gated,
            services.record_return,
            **return_kwargs(tech, away.specimen, recipient_id=people[who].pk),
        )
    assert fresh(away.specimen).status == "distributed"


def test_the_return_is_the_open_checkout_and_check_in_closes_it(people, world):
    tech = people["technician"]
    away = shipped(people, world)
    returned = give_back(tech, away.specimen)
    assert fresh(away.specimen).open_checkout_id == returned.pk
    check_in(tech, away.specimen, world.position("A2"), occurred_at=ago(30))
    closing = away.specimen.custody.get(kind=CustodyEvent.Kind.CHECK_IN)
    assert closing.checkout == returned
    assert fresh(away.specimen).open_checkout_id is None


def test_time_away_is_listed_but_not_counted(people, world):
    tech = people["technician"]
    away = shipped(people, world)
    returned = give_back(tech, away.specimen, at=ago(60))
    check_in(tech, away.specimen, world.position("A2"), occurred_at=ago(30))
    out = queries.time_out_of_storage(fresh(away.specimen), now=timezone.now())
    assert [i.closed_by for i in out.intervals] == ["dispatch", "check_in"]
    assert out.intervals[1].checkout == returned
    (period,) = out.away
    assert period.dispatch == away.event and period.started == away.event.occurred_at
    assert (
        period.ended == returned.occurred_at
        and "Example University" in period.destination
        or period.destination
    )
    assert abs(out.total - timedelta(minutes=110)) < timedelta(seconds=5)


def test_a_second_dispatch_and_return_gives_two_away_periods(people, world):
    tech = people["technician"]
    away = shipped(people, world)
    give_back(tech, away.specimen, at=ago(60))
    dispatch(tech, away.specimen, at=ago(40))
    give_back(tech, away.specimen, at=ago(20), thaw_count=0)
    out = queries.time_out_of_storage(fresh(away.specimen), now=timezone.now())
    assert len(out.away) == 2
    assert all(period.ended is not None for period in out.away)
    assert [i.closed_by for i in out.intervals] == ["dispatch", "dispatch", ""]


# --- 7. holds and gates ---------------------------------------------------------------------


def _use_action(action, people, world, specimen):
    tech = people["technician"]
    return {
        "check_out": lambda: check_out(tech, specimen, tech, occurred_at=ago(200)),
        "derive": lambda: derive(tech, specimen, [child(world, "1")], "1", occurred_at=ago(100)),
        "consume": lambda: consume(tech, specimen, "0.5", occurred_at=ago(100)),
        "transfer": lambda: services.transfer_custody(
            actor=tech,
            specimen_id=specimen.pk,
            expected_revision=fresh(specimen).revision,
            expected_occupancy_revision=occ_rev(specimen),
            recipient_id=people["technician2"].pk,
            destination="Bench",
            occurred_at=ago(100),
            reason="Hand over",
        ),
    }[action]


USE_ACTIONS = ["check_out", "derive", "consume", "transfer"]


def _held_for_use(action, scope, people, world):
    tech = people["technician"]
    specimen = stored(tech, world) if action == "check_out" else picked(people, world)
    hold(tech, specimen, scope, occurred_at=ago(250 if action == "check_out" else 150))
    return specimen


@pytest.mark.parametrize("scope", ["use", "all_use"])
@pytest.mark.parametrize("action", USE_ACTIONS)
def test_use_and_all_use_holds_block_using_the_specimen(action, scope, people, world):
    specimen = _held_for_use(action, scope, people, world)
    before = written(specimen)
    refused("held", _use_action(action, people, world, specimen))
    assert written(specimen) == before


@pytest.mark.parametrize("action", USE_ACTIONS)
def test_a_disposal_hold_does_not_block_using_the_specimen(action, people, world):
    specimen = _held_for_use(action, "disposal", people, world)
    _use_action(action, people, world, specimen)()


def test_a_use_hold_does_not_block_the_disposal_steps(people, world):
    tech = people["technician"]
    specimen = stored(tech, world)
    hold(tech, specimen, "use", occurred_at=ago(250))
    request = ask_disposal(tech, specimen)
    with pytest.raises(PermissionDenied):
        decide(tech, request)  # no approval grant
    other = stored(people["approver"], world, position="A2")
    own = ask_disposal(people["approver"], other)
    refused("self_approval", decide, people["approver"], own)  # four-eyes still applies
    decide(people["approver"], request)
    execute(tech, request, occurred_at=timezone.now())
    assert fresh(specimen).status == "disposed"


@pytest.mark.parametrize("scope", ["disposal", "all_use"])
def test_disposal_and_all_use_holds_block_the_disposal_steps(scope, people, world):
    tech = people["technician"]
    first = stored(tech, world)
    hold(tech, first, scope, occurred_at=ago(250))
    refused("held", ask_disposal, tech, first)

    second = stored(tech, world, position="A2")
    request = ask_disposal(tech, second)
    hold(tech, second, scope, occurred_at=timezone.now())
    refused("held", decide, people["approver"], request)


@pytest.mark.parametrize("scope", ["disposal", "use", "all_use"])
def test_a_manual_hold_accepts_every_scope_and_a_manager_releases_it(scope, people, world):
    tech = people["technician"]
    specimen = stored(tech, world)
    placed = hold(tech, specimen, scope, occurred_at=ago(250))
    assert placed.scope == scope and placed.reference == ""
    release(people["manager"], placed)
    assert not services.active_holds(specimen).exists()


def test_generic_release_refuses_a_referenced_hold(people, world):
    tech = people["technician"]
    specimen = stored(tech, world)
    placed = linked_hold(tech, specimen, scope="use")
    error = refused("invalid_transition", release, people["manager"], placed)
    assert "RQ-000012" in str(error)
    assert not HoldRelease.objects.exists()


def test_release_linked_hold_refuses_manual_and_released_holds(people, world):
    tech = people["technician"]
    specimen = stored(tech, world)
    manual = hold(tech, specimen, "use", occurred_at=ago(250))
    refused(
        "invalid_transition",
        gated,
        services.release_linked_hold,
        actor=tech,
        hold_id=manual.pk,
        occurred_at=ago(140),
        reason="Not mine",
    )
    linked = linked_hold(tech, specimen, scope="use", at=ago(150))
    kwargs = {
        "actor": tech,
        "hold_id": linked.pk,
        "occurred_at": ago(140),
        "reason": "Done",
    }
    gated(services.release_linked_hold, **kwargs)
    refused(
        "invalid_transition",
        gated,
        services.release_linked_hold,
        **{**kwargs, "occurred_at": ago(130)},
    )
    assert HoldRelease.objects.filter(hold=linked).count() == 1


# --- 8. derivation after an unreported time away ----------------------------------------------


def test_a_child_of_a_returned_specimen_with_unknown_time_away_has_an_unknown_baseline(
    people, world
):
    tech = people["technician"]
    specimen = picked(people, world, baseline=2)
    services.record_thaw(
        actor=tech,
        specimen_id=specimen.pk,
        expected_revision=specimen.revision,
        occurred_at=ago(195),
        reason="Seen thawing",
    )
    dispatch(tech, specimen, at=ago(120))
    give_back(tech, specimen, thaw_count=None, at=ago(60))
    (kid,) = derive(tech, specimen, [child(world, "1")], "1", occurred_at=ago(50))
    baseline = ThawEvent.objects.get(specimen=kid, kind=ThawEvent.Kind.BASELINE)
    assert baseline.count is None
    # The known baseline (2) and the one recorded thaw (1) both travel with the child.
    assert baseline.inherited_recorded_count == 3
    assert not queries.thaw_summary(kid).known


# --- 9. correcting dispatch and return custody -----------------------------------------------


def _correct(manager, specimen, event, **values):
    return services.correct_custody(
        actor=manager,
        event_id=event.pk,
        expected_revision=fresh(specimen).revision,
        values=values,
        reason="Correction",
    )


def test_a_dispatch_recipient_cannot_be_set_but_its_destination_can(people, world):
    manager = people["manager"]
    away = shipped(people, world)
    error = refused(
        "invalid", _correct, manager, away.specimen, away.event, to_custodian_id=manager.pk
    )
    assert "to_custodian_id" in error.error_dict
    fix = _correct(manager, away.specimen, away.event, destination="Example University, Lab 2")
    assert fix.kind == CustodyEvent.Kind.CORRECTION and fix.to_custodian_id is None
    assert fix.destination == "Example University, Lab 2"
    assert fresh(away.specimen).custodian_id is None


def test_a_return_sender_cannot_be_set_but_its_source_can(people, world):
    tech, manager = people["technician"], people["manager"]
    away = shipped(people, world)
    returned = give_back(tech, away.specimen)
    error = refused(
        "invalid", _correct, manager, away.specimen, returned, from_custodian_id=manager.pk
    )
    assert "from_custodian_id" in error.error_dict
    fix = _correct(manager, away.specimen, returned, destination="Example University, Dock")
    assert fix.from_custodian_id is None and fix.to_custodian_id == tech.pk
    assert fix.destination == "Example University, Dock"


# --- 10. assign_lineage_context -------------------------------------------------------------


def lineage(actor, specimen, **over):
    values = {
        "actor": actor,
        "specimen_id": specimen.pk,
        "expected_revision": fresh(specimen).revision,
        "participant_id": None,
        "collection_id": None,
        "reason": "Recorded on the requisition (synthetic)",
        **over,
    }
    return services.assign_lineage_context(**values)


def family_without_collection(people, world):
    primary = services.register_specimen(
        actor=people["technician"],
        data=replace(registration(world), collection_id=None),
        occurred_at=ago(270),
        reason="Received without a requisition",
    )
    kids = derive(
        people["technician"],
        primary,
        [child(world, "1"), child(world, "1")],
        "2",
        occurred_at=ago(190),
    )
    return fresh(primary), kids


def family_state(specimens):
    return (
        [fresh(s).revision for s in specimens],
        Correction.objects.count(),
        AuditEvent.objects.filter(action="specimens.lineage_context_assigned").count(),
    )


@pytest.mark.parametrize("who", ["technician", "viewer"])
def test_lineage_context_needs_a_manager(who, people, world):
    primary, kids = family_without_collection(people, world)
    before = family_state([primary, *kids])
    with pytest.raises(PermissionDenied):
        lineage(people[who], primary, collection_id=world.collection.pk)
    assert family_state([primary, *kids]) == before


def test_lineage_context_refuses_a_stale_revision(people, world):
    primary, kids = family_without_collection(people, world)
    before = family_state([primary, *kids])
    stale = fresh(primary).revision - 1
    refused(
        "stale_revision",
        lineage,
        people["manager"],
        primary,
        collection_id=world.collection.pk,
        expected_revision=stale,
    )
    assert family_state([primary, *kids]) == before


def test_lineage_context_needs_a_value(people, world):
    primary, _ = family_without_collection(people, world)
    refused("required", lineage, people["manager"], primary)


def test_lineage_context_is_assigned_on_the_primary_only(people, world):
    _, kids = family_without_collection(people, world)
    refused(
        "invalid_transition", lineage, people["manager"], kids[0], collection_id=world.collection.pk
    )


def test_lineage_context_never_overwrites_a_recorded_value(people, world):
    primary, kids = family_without_collection(people, world)
    Specimen.objects.filter(pk=kids[1].pk).update(collection=world.collection)
    before = family_state([primary, *kids])
    error = refused(
        "invalid_transition", lineage, people["manager"], primary, collection_id=world.collection.pk
    )
    assert "collection" in error.error_dict
    assert family_state([primary, *kids]) == before
    assert Specimen.objects.get(pk=primary.pk).collection_id is None


def test_lineage_context_refuses_an_inactive_collection(people, world):
    primary, kids = family_without_collection(people, world)
    world.collection.is_active = False
    world.collection.save()
    before = family_state([primary, *kids])
    refused("invalid", lineage, people["manager"], primary, collection_id=world.collection.pk)
    assert family_state([primary, *kids]) == before


def test_lineage_context_fills_the_whole_family_with_one_correction_each(people, world):
    primary, kids = family_without_collection(people, world)
    members = [primary, *kids]
    revisions = {s.pk: fresh(s).revision for s in members}
    family = lineage(people["manager"], primary, collection_id=world.collection.pk)
    assert len(family) == 3
    for member in family:
        assert member.collection_id == world.collection.pk
        assert member.revision == revisions[member.pk] + 1
        (correction,) = Correction.objects.filter(specimen=member)
        assert correction.before == {"collection": None}
        assert correction.after == {"collection": world.collection.code}
    assert AuditEvent.objects.filter(action="specimens.lineage_context_assigned").count() == 3


# --- 11. database guards --------------------------------------------------------------------


@pytest.fixture
def traveled(people, world):
    """A specimen with a referenced hold, a dispatch, a distribute entry and an external thaw."""
    tech = people["technician"]
    specimen = picked(people, world)
    held = linked_hold(tech, specimen, scope="disposal")
    event = dispatch(tech, specimen, at=ago(120))
    returned = give_back(tech, specimen, thaw_count=1)
    return SimpleNamespace(
        specimen=fresh(specimen),
        hold=held,
        dispatch=event,
        returned=returned,
        entry=specimen.ledger.get(kind=QuantityEntry.Kind.DISTRIBUTE),
        thaw=ThawEvent.objects.get(specimen=specimen, kind=ThawEvent.Kind.EXTERNAL),
    )


def sql_refused(sql, *params):
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(sql, params)


@pytest.mark.parametrize("row", ["hold", "dispatch", "entry", "thaw"])
def test_the_new_rows_are_append_only(row, traveled):
    obj = getattr(traveled, row)
    table = type(obj)._meta.db_table
    sql_refused(f"UPDATE {table} SET reason = reason || 'x' WHERE id = %s", obj.pk)
    sql_refused(f"DELETE FROM {table} WHERE id = %s", obj.pk)
    assert type(obj).objects.filter(pk=obj.pk).exists()


def violates(constraint, create):
    with pytest.raises(IntegrityError) as exc, transaction.atomic():
        create()
    assert constraint in str(exc.value)


def test_check_constraints_protect_the_new_states(people, world):
    tech = people["technician"]
    specimen = picked(people, world)
    pick = specimen.custody.get(kind=CustodyEvent.Kind.CHECK_OUT)
    base = {"specimen": specimen, "actor": tech, "occurred_at": ago(100), "reason": "Test"}

    violates(
        "specimens_custody_dispatch_outside",
        lambda: CustodyEvent.objects.create(
            kind="dispatch", from_custodian=tech, to_custodian=tech, **base
        ),
    )
    violates(
        "specimens_custody_to",
        lambda: CustodyEvent.objects.create(
            kind="check_in", from_custodian=tech, to_custodian=None, checkout=pick, **base
        ),
    )
    violates(
        "specimens_specimen_custodian",
        lambda: Specimen.objects.filter(pk=specimen.pk).update(
            status="distributed", quantity_on_hand=0, open_checkout=None
        ),
    )
    violates(
        "specimens_thaw_external",
        lambda: ThawEvent.objects.create(kind="external", count=1, **base),
    )
    violates(
        "specimens_qty_debit_sign",
        lambda: QuantityEntry.objects.create(
            kind="distribute",
            delta=D("1"),
            balance_after=D("3"),
            unit=specimen.unit,
            supplied_text="1",
            **base,
        ),
    )


# --- 12. history ----------------------------------------------------------------------------


def test_history_describes_the_distribution_events(people, world):
    tech = people["technician"]
    specimen = picked(people, world)
    linked_hold(tech, specimen)
    dispatch(tech, specimen, at=ago(120))
    give_back(tech, specimen, thaw_count=None, at=ago(60))
    items = queries.specimen_history(actor=people["viewer"], specimen_id=specimen.pk)
    text = "\n".join(f"{i.title} | {i.detail}" for i in items)
    for expected in (
        "Dispatched",
        "Returned to repository",
        "outside the repository",
        "Returned from",
        "For: Allocated to RQ-000012",
        "Thaws while away",
    ):
        assert expected in text, expected
