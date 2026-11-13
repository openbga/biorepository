"""Custody, the time-out clock, thaw counts, chronology, stale forms and holds."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from specimens import queries, services
from specimens.models import CustodyEvent, ThawEvent
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
    hold,
    move_box,
    occ_rev,
    place,
    register,
    release,
    stored,
)

pytestmark = pytest.mark.django_db


def refused(code, fn, *args, **kwargs):
    with pytest.raises(ValidationError) as exc:
        fn(*args, **kwargs)
    assert code in codes(exc.value)


# --- state and custodian projection ------------------------------------------------------------


def test_place_check_out_check_in_and_transfer_project_state_and_custodian(people, world):
    t, m, t2 = people["technician"], people["manager"], people["technician2"]
    specimen = register(t, world)
    assert specimen.custodian_id == t.pk and specimen.open_checkout_id is None
    place(t, specimen, world.position("A1"))
    check_out(t, specimen, t2)
    fresh(specimen)
    assert specimen.custodian_id == t2.pk and specimen.open_checkout_id
    specimen.occupancy.refresh_from_db()
    assert specimen.occupancy.position_id is None
    check_in(t2, specimen, world.position("A2"), recipient=m)
    fresh(specimen)
    assert specimen.custodian_id == m.pk and specimen.open_checkout_id is None
    assert specimen.occupancy.position_id == world.position("A2").pk
    services.transfer_custody(
        actor=m,
        specimen_id=specimen.pk,
        expected_revision=specimen.revision,
        expected_occupancy_revision=occ_rev(specimen),
        recipient_id=t.pk,
        destination="Bench 4",
        occurred_at=ago(90),
        reason="Handover",
    )
    fresh(specimen)
    assert specimen.custodian_id == t.pk and specimen.occupancy.position_id
    assert list(specimen.custody.order_by("recorded_at", "id").values_list("kind", flat=True)) == [
        "register",
        "place",
        "check_out",
        "check_in",
        "transfer",
    ]
    checkin = specimen.custody.get(kind="check_in")
    assert checkin.checkout_id == specimen.custody.get(kind="check_out").pk


def test_check_out_of_an_unplaced_specimen_is_refused(people, world):
    specimen = register(people["technician"], world)
    refused("invalid_transition", check_out, people["technician"], specimen, people["technician"])


def test_check_in_needs_an_open_check_out_and_a_free_position(people, world):
    t = people["technician"]
    specimen = stored(t, world)
    other = stored(t, world, "A2")
    refused("invalid_transition", check_in, t, specimen, world.position("B1"))
    check_out(t, specimen, t)
    refused("occupied", check_in, t, specimen, world.position("A2"))
    assert fresh(other).occupancy.position_id == world.position("A2").pk


def test_double_check_out_and_placing_a_checked_out_specimen_are_refused(people, world):
    t = people["technician"]
    specimen = stored(t, world)
    check_out(t, specimen, t)
    refused("invalid_transition", check_out, t, specimen, t)
    refused("invalid_transition", place, t, specimen, world.position("B1"), ago(150))


def test_recipients_must_be_active_staff(people, world):
    t = people["technician"]
    specimen = stored(t, world)
    refused("invalid", check_out, t, specimen, people["nobody"])
    people["technician2"].is_active = False
    people["technician2"].save()
    refused("invalid", check_out, t, specimen, people["technician2"])


def test_transfer_to_the_current_custodian_is_refused(people, world):
    t = people["technician"]
    specimen = register(t, world)
    with pytest.raises(ValidationError) as exc:
        services.transfer_custody(
            actor=t,
            specimen_id=specimen.pk,
            expected_revision=specimen.revision,
            expected_occupancy_revision=occ_rev(specimen),
            recipient_id=t.pk,
            destination="",
            occurred_at=ago(200),
            reason="Same person",
        )
    assert codes(exc.value) == {"invalid"}


@pytest.mark.parametrize("who", ["viewer", "nobody"])
def test_custody_actions_need_an_operating_role(people, world, who):
    t = people["technician"]
    specimen = stored(t, world)
    with pytest.raises(PermissionDenied):
        check_out(people[who], specimen, t)
    assert fresh(specimen).open_checkout_id is None


# --- time out of storage -----------------------------------------------------------------------


def test_time_out_is_the_sum_of_closed_intervals_plus_the_open_one(people, world):
    t = people["technician"]
    base = timezone.now()

    def at(minutes):
        return base - timedelta(minutes=minutes)

    specimen = stored(t, world)
    check_out(t, specimen, t, occurred_at=at(200))
    check_in(t, specimen, world.position("A1"), occurred_at=at(100))
    check_out(t, specimen, t, occurred_at=at(80))
    check_in(t, specimen, world.position("A1"), occurred_at=at(50))
    closed = queries.time_out_of_storage(specimen, now=base)
    assert closed.total == timedelta(minutes=130) and not closed.open
    assert len(closed.intervals) == 2
    check_out(t, specimen, t, occurred_at=at(30))
    running = queries.time_out_of_storage(specimen, now=base)
    assert running.open and running.total == timedelta(minutes=160)
    assert running.intervals[-1].ended is None


# --- thaw --------------------------------------------------------------------------------------


def checked_round_trip(actor, specimen, world, thaw):
    check_out(actor, specimen, actor)
    check_in(actor, specimen, world.position("A1"), thaw=thaw)


@pytest.mark.parametrize(("thaw", "added"), [(True, 1), (False, 0), (None, 0)])
def test_only_an_observed_thaw_adds_a_thaw(people, world, thaw, added):
    t = people["technician"]
    specimen = stored(t, world, baseline=2)
    checked_round_trip(t, specimen, world, thaw)
    assert ThawEvent.objects.filter(specimen=specimen, kind="thaw").count() == added
    assert queries.thaw_summary(specimen).total == 2 + added
    assert specimen.custody.get(kind="check_in").thaw_observed is thaw


def test_check_in_requires_a_clear_thaw_answer(people, world):
    t = people["technician"]
    specimen = stored(t, world)
    check_out(t, specimen, t)
    refused("invalid", check_in, t, specimen, world.position("A1"), thaw="yes")
    assert fresh(specimen).open_checkout_id  # still out


def test_unknown_baseline_reads_unknown_with_the_recorded_count(people, world):
    t = people["technician"]
    specimen = stored(t, world, baseline=None)
    summary = queries.thaw_summary(specimen)
    assert summary.total is None and str(summary) == "Unknown before registration; 0 recorded"
    checked_round_trip(t, specimen, world, True)
    summary = queries.thaw_summary(specimen)
    assert summary.total is None and str(summary) == "Unknown before registration; 1 recorded"


def test_record_thaw_adds_one_observed_thaw(people, world):
    t = people["technician"]
    specimen = stored(t, world, baseline=0)
    services.record_thaw(
        actor=t,
        specimen_id=specimen.pk,
        expected_revision=specimen.revision,
        occurred_at=ago(100),
        reason="Thawed on the bench before aliquoting",
    )
    assert queries.thaw_summary(specimen).total == 1
    assert fresh(specimen).revision > 1


def thaw_row(specimen, kind):
    return ThawEvent.objects.filter(specimen=specimen, kind=kind).order_by("recorded_at")


def correct(actor, event, count, specimen):
    return services.correct_thaw(
        actor=actor,
        event_id=event.pk,
        expected_revision=fresh(specimen).revision,
        count=count,
        reason="Logged against the wrong vial",
    )


def test_correct_thaw_appends_a_replacement_and_never_rewrites_the_row(people, world):
    t, m = people["technician"], people["manager"]
    specimen = stored(t, world, baseline=2)
    checked_round_trip(t, specimen, world, True)
    thaw = thaw_row(specimen, "thaw").get()
    assert queries.thaw_summary(specimen).total == 3
    first = correct(m, thaw, 0, specimen)
    assert first.kind == "correction" and first.corrects_id == thaw.pk
    thaw.refresh_from_db()
    assert thaw.count == 1  # the original row is untouched
    assert queries.thaw_summary(specimen).total == 2
    # Only the latest version can be corrected.
    refused("invalid_transition", correct, m, thaw, 1, specimen)
    correct(m, first, 1, specimen)
    assert queries.thaw_summary(specimen).total == 3
    assert ThawEvent.objects.filter(specimen=specimen).count() == 4  # baseline, thaw, 2 corrections


def test_correct_thaw_baseline_and_bounds(people, world):
    t, m = people["technician"], people["manager"]
    specimen = stored(t, world, baseline=None)
    baseline = thaw_row(specimen, "baseline").get()
    correct(m, baseline, 4, specimen)
    assert queries.thaw_summary(specimen).total == 4
    thaw = services.record_thaw(
        actor=t,
        specimen_id=specimen.pk,
        expected_revision=fresh(specimen).revision,
        occurred_at=timezone.now(),  # the correction above moved the watermark to now
        reason="Observed",
    )
    refused("invalid", correct, m, thaw, 2, specimen)  # a thaw counts 0 or 1
    with pytest.raises(PermissionDenied):
        correct(t, thaw, 0, specimen)  # technicians do not correct


# --- chronology and stale forms ----------------------------------------------------------------


def test_an_event_earlier_than_the_latest_event_is_refused(people, world):
    t = people["technician"]
    specimen = stored(t, world)  # placed at ago(260)
    check_out(t, specimen, t, occurred_at=ago(200))
    refused("invalid_time", check_in, t, specimen, world.position("A1"), occurred_at=ago(210))
    refused("invalid_time", check_in, t, specimen, world.position("A1"), occurred_at=ago(-5))
    assert fresh(specimen).open_checkout_id  # nothing changed
    check_in(t, specimen, world.position("A1"), occurred_at=ago(200))  # equal time is fine


def test_a_later_box_move_makes_an_earlier_specimen_event_invalid(people, world):
    t = people["technician"]
    specimen = stored(t, world)
    move_box(t, world.box, world.shelf2, 1, occurred_at=ago(100))
    refused("invalid_time", check_out, t, specimen, t, occurred_at=ago(150))
    check_out(t, specimen, t, occurred_at=ago(90))


def test_a_box_move_earlier_than_a_specimen_event_is_refused(people, world):
    t = people["technician"]
    specimen = stored(t, world)  # placed at ago(260)
    refused("invalid_time", move_box, t, world.box, world.shelf2, 1, ago(300))
    assert fresh(specimen).occupancy.position.box_id == world.box.pk


def test_stale_specimen_and_occupancy_revisions_are_refused(people, world):
    t = people["technician"]
    specimen = stored(t, world)
    revision, occupancy_revision = fresh(specimen).revision, occ_rev(specimen)

    def out(rev, occ):
        return services.check_out(
            actor=t,
            specimen_id=specimen.pk,
            expected_revision=rev,
            expected_occupancy_revision=occ,
            recipient_id=t.pk,
            destination="Bench",
            occurred_at=ago(200),
            reason="Aliquoting",
        )

    refused("stale_revision", out, revision - 1, occupancy_revision)
    refused("stale_revision", out, None, occupancy_revision)
    refused("stale_revision", out, revision, occupancy_revision - 1)
    move_box(t, world.box, world.shelf2, 1, occurred_at=ago(150))
    refused("stale_revision", out, revision, occupancy_revision)  # the box moved: stale
    assert Occupancy.objects.get(pk=specimen.occupancy_id).revision > occupancy_revision
    assert fresh(specimen).open_checkout_id is None


# --- holds -------------------------------------------------------------------------------------


def test_an_all_use_hold_blocks_use_but_not_protective_relocation_or_check_in(people, world):
    t, m = people["technician"], people["manager"]
    specimen = stored(t, world)
    held = hold(t, specimen, "all_use", occurred_at=ago(250))
    refused("held", check_out, t, specimen, t)
    refused("held", consume, t, specimen, "0.1")
    refused("held", derive, t, specimen, [child(world, "1")], Decimal("1"))
    refused(
        "held",
        services.transfer_custody,
        actor=t,
        specimen_id=specimen.pk,
        expected_revision=fresh(specimen).revision,
        expected_occupancy_revision=occ_rev(specimen),
        recipient_id=m.pk,
        destination="",
        occurred_at=ago(200),
        reason="Handover",
    )
    # Relocating a held specimen is allowed (protective move).
    services.relocate_specimen(
        actor=t,
        specimen_id=specimen.pk,
        expected_revision=fresh(specimen).revision,
        expected_occupancy_revision=occ_rev(specimen),
        position_id=world.position("C3").pk,
        occurred_at=ago(190),
        reason="Moved away from the door",
    )
    assert fresh(specimen).occupancy.position.label == "C3"
    # Release, check out, hold again: check-in still works for a held specimen.
    release(m, held, occurred_at=ago(180))
    check_out(t, specimen, t, occurred_at=ago(170))
    hold(t, specimen, "all_use", occurred_at=ago(160))
    check_in(t, specimen, world.position("A1"), occurred_at=ago(150))
    assert fresh(specimen).open_checkout_id is None


def test_a_disposal_hold_blocks_none_of_those_actions(people, world):
    t, m = people["technician"], people["manager"]
    specimen = stored(t, world)
    hold(t, specimen, "disposal", occurred_at=ago(250))
    check_out(t, specimen, t, occurred_at=ago(240))
    consume(t, specimen, "0.25", occurred_at=ago(230))
    derive(t, specimen, [child(world, "0.75")], "0.75", occurred_at=ago(220))
    services.transfer_custody(
        actor=t,
        specimen_id=specimen.pk,
        expected_revision=fresh(specimen).revision,
        expected_occupancy_revision=occ_rev(specimen),
        recipient_id=m.pk,
        destination="",
        occurred_at=ago(210),
        reason="Handover",
    )
    check_in(t, specimen, world.position("A1"), occurred_at=ago(200), recipient=m)
    services.relocate_specimen(
        actor=t,
        specimen_id=specimen.pk,
        expected_revision=fresh(specimen).revision,
        expected_occupancy_revision=occ_rev(specimen),
        position_id=world.position("B1").pk,
        occurred_at=ago(190),
        reason="Tidy",
    )
    assert fresh(specimen).quantity_on_hand == 1


def test_only_managers_release_holds(people, world):
    t = people["technician"]
    specimen = stored(t, world)
    held = hold(t, specimen)
    with pytest.raises(PermissionDenied):
        release(t, held)
    release(people["manager"], held)
    refused("invalid_transition", release, people["manager"], held)


# --- custody correction ------------------------------------------------------------------------


def fix(actor, event, specimen, **values):
    return services.correct_custody(
        actor=actor,
        event_id=event.pk,
        expected_revision=fresh(specimen).revision,
        values=values,
        reason="Wrong person recorded",
    )


def test_custody_correction_changes_the_custodian_only_for_the_latest_event(people, world):
    t, m, a = people["technician"], people["manager"], people["admin"]
    specimen = register(t, world)
    services.transfer_custody(
        actor=t,
        specimen_id=specimen.pk,
        expected_revision=specimen.revision,
        expected_occupancy_revision=occ_rev(specimen),
        recipient_id=m.pk,
        destination="Bench 1",
        occurred_at=ago(200),
        reason="Handover",
    )
    registered = specimen.custody.get(kind="register")
    transfer = specimen.custody.get(kind="transfer")
    # An older event: history is corrected, the current custodian is not.
    fix(m, registered, specimen, to_custodian_id=people["technician2"].pk)
    assert fresh(specimen).custodian_id == m.pk
    # The latest event: the current custodian follows.
    fix(m, transfer, specimen, to_custodian_id=a.pk, destination="Bench 2")
    assert fresh(specimen).custodian_id == a.pk
    # The original rows are never rewritten; corrections are appended.
    transfer.refresh_from_db()
    assert transfer.to_custodian_id == m.pk and transfer.destination == "Bench 1"
    assert CustodyEvent.objects.filter(specimen=specimen, kind="correction").count() == 2
    refused("invalid_transition", fix, m, transfer, specimen, destination="Bench 3")


def test_custody_correction_never_changes_times_or_links(people, world):
    t, m = people["technician"], people["manager"]
    specimen = stored(t, world)
    event = specimen.custody.get(kind="place")
    refused("invalid", fix, m, event, specimen, occurred_at=ago(5))
    refused("invalid", fix, m, event, specimen, kind="relocate")
    with pytest.raises(PermissionDenied):
        fix(t, event, specimen, destination="Elsewhere")
