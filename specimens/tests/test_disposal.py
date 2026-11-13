"""Four-eyes disposal: who may approve, what makes an approval stale, and what execution writes."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from core.models import AuditEvent
from specimens import queries, services
from specimens.models import (
    CustodyEvent,
    DisposalDecision,
    DisposalExecution,
    DisposalRequest,
    Specimen,
)
from storage.models import Occupancy

from .conftest import (
    ago,
    ask_disposal,
    check_out,
    child,
    codes,
    consume,
    decide,
    derive,
    execute,
    fresh,
    hold,
    make_user,
    move_box,
    occ_rev,
    register,
    release,
    stored,
)

pytestmark = pytest.mark.django_db

D = Decimal


def refused(code, fn, *args, **kwargs):
    with pytest.raises(ValidationError) as exc:
        fn(*args, **kwargs)
    assert code in codes(exc.value)


def nothing_decided(request):
    assert not DisposalDecision.objects.filter(request=request).exists()


def approved_request(world, people, **kwargs):
    """A stored specimen with an approved disposal request (requested by technician)."""
    specimen = stored(people["technician"], world, **kwargs)
    request = ask_disposal(people["technician"], specimen)
    decide(people["approver"], request)
    return specimen, request


# --- who may approve ---------------------------------------------------------------------------


def test_the_requester_cannot_approve_their_own_request_even_with_the_permission(people, world):
    approver = people["approver"]  # technician holding the explicit permission
    specimen = stored(approver, world)
    request = ask_disposal(approver, specimen)
    refused("self_approval", decide, approver, request)
    nothing_decided(request)


def test_a_superuser_who_also_holds_the_permission_cannot_approve_their_own_request(people, world):
    boss = make_user("sp-boss", "admin", approver=True, superuser=True)
    specimen = stored(boss, world)
    request = ask_disposal(boss, specimen)
    refused("self_approval", decide, boss, request)
    nothing_decided(request)
    # Someone else holding the permission can.
    assert decide(people["approver"], request).decision == "approve"


@pytest.mark.parametrize(
    "who",
    ["superuser", "admin", "manager", "technician", "technician2", "viewer", "nobody"],
)
def test_without_the_explicit_permission_nobody_approves(people, world, who):
    """Superuser and admin status, and management roles, never stand in for the permission."""
    specimen = stored(people["technician"], world)
    request = ask_disposal(people["technician"], specimen)
    with pytest.raises(PermissionDenied):
        decide(people[who], request)
    with pytest.raises(PermissionDenied):
        decide(people[who], request, "reject")
    nothing_decided(request)
    assert fresh(specimen).status == "available"


def test_a_technician_with_the_permission_approves_and_a_decision_is_final(people, world):
    specimen = stored(people["technician"], world)
    request = ask_disposal(people["technician"], specimen)
    decision = decide(people["approver"], request)
    specimen = fresh(specimen)
    assert decision.actor_id == people["approver"].pk
    assert decision.approved_revision == specimen.revision
    assert decision.approved_occupancy_revision == occ_rev(specimen)
    refused("invalid_transition", decide, people["approver"], request, "reject")
    assert AuditEvent.objects.filter(action="specimens.disposal_approved").count() == 1


def test_a_permission_granted_through_a_group_counts(people, world):
    from django.contrib.auth.models import Group, Permission

    group = Group.objects.create(name="disposal-approvers")
    group.permissions.add(Permission.objects.get(codename="approve_disposal"))
    reviewer = make_user("sp-group-reviewer", "technician")
    reviewer.groups.add(group)
    specimen = stored(people["technician"], world)
    request = ask_disposal(people["technician"], specimen)
    assert decide(reviewer, request).decision == "approve"


def test_reject_is_recorded_without_an_approval_snapshot(people, world):
    specimen = stored(people["technician"], world)
    request = ask_disposal(people["technician"], specimen)
    rejected = decide(people["approver"], request, "reject", "Still needed for the study")
    assert (rejected.approved_revision, rejected.approved_occupancy_revision) == (None, None)
    assert AuditEvent.objects.filter(action="specimens.disposal_rejected").count() == 1
    refused("approval_required", execute, people["technician"], request)
    assert fresh(specimen).status == "available"


# --- holds -------------------------------------------------------------------------------------


@pytest.mark.parametrize("scope", ["disposal", "all_use"])
def test_an_active_hold_blocks_request_approval_and_execution(people, world, scope):
    t, m = people["technician"], people["manager"]
    specimen = stored(t, world)
    held = hold(t, specimen, scope, occurred_at=ago(150))
    refused("held", ask_disposal, t, specimen)
    release(m, held, occurred_at=ago(140))
    request = ask_disposal(t, specimen)
    # A hold placed after the request blocks the approval.
    held = hold(t, specimen, scope, occurred_at=timezone.now())
    refused("held", decide, people["approver"], request)
    nothing_decided(request)
    release(m, held)
    # Request again, approve, then hold: execution cannot proceed (stale or held).
    request2 = ask_disposal(t, specimen)
    decide(people["approver"], request2)
    hold(t, specimen, scope, occurred_at=timezone.now())
    with pytest.raises(ValidationError) as exc:
        execute(t, request2)
    assert codes(exc.value) <= {"held", "stale_approval"}
    assert fresh(specimen).status == "available"


# --- stale approval ----------------------------------------------------------------------------


def test_an_approval_goes_stale_after_a_hold_is_placed_and_released(people, world):
    t, m = people["technician"], people["manager"]
    specimen, request = approved_request(world, people)
    held = hold(t, specimen, "disposal", occurred_at=timezone.now())
    release(m, held)
    refused("stale_approval", execute, t, request)
    assert fresh(specimen).status == "available"
    assert services.request_state(request) == "stale"


def test_an_approval_goes_stale_after_any_later_specimen_event(people, world):
    t = people["technician"]
    specimen, request = approved_request(world, people)
    services.record_thaw(
        actor=t,
        specimen_id=specimen.pk,
        expected_revision=fresh(specimen).revision,
        occurred_at=timezone.now(),
        reason="Observed thaw on the bench",
    )
    refused("stale_approval", execute, t, request)


def test_an_approval_goes_stale_when_the_box_moves(people, world):
    t = people["technician"]
    specimen, request = approved_request(world, people)
    revision = fresh(specimen).revision
    move_box(t, world.box, world.shelf2, 1)
    assert fresh(specimen).revision == revision  # only the occupancy revision changed
    refused("stale_approval", execute, t, request)
    assert services.request_state(request) == "stale"
    assert fresh(specimen).status == "available"


def test_a_request_goes_stale_before_approval_and_a_new_one_is_then_allowed(people, world):
    t = people["technician"]
    specimen = stored(t, world)
    request = ask_disposal(t, specimen)
    move_box(t, world.box, world.shelf2, 1)
    refused("stale_approval", decide, people["approver"], request)
    nothing_decided(request)
    assert services.request_state(request) == "stale"
    fresh_request = ask_disposal(t, specimen)  # the stale one no longer blocks
    assert decide(people["approver"], fresh_request).decision == "approve"


def test_after_a_stale_approval_a_new_request_can_be_approved_and_executed(people, world):
    t = people["technician"]
    specimen, old = approved_request(world, people)
    move_box(t, world.box, world.shelf2, 1)
    refused("stale_approval", execute, t, old)
    new = ask_disposal(t, specimen)
    decide(people["approver"], new)
    execution = execute(t, new)
    assert execution.request_id == new.pk and fresh(specimen).status == "disposed"
    assert not DisposalExecution.objects.filter(request=old).exists()


def test_execution_needs_the_approved_state(people, world):
    t = people["technician"]
    specimen = stored(t, world)
    request = ask_disposal(t, specimen)
    refused("approval_required", execute, t, request)
    assert fresh(specimen).status == "available"


def test_execution_stops_if_the_approver_no_longer_holds_the_permission(people, world):
    specimen, request = approved_request(world, people)
    people["approver"].user_permissions.clear()
    refused("approval_required", execute, people["technician"], request)


# --- one unresolved request, cancel ------------------------------------------------------------


def test_only_one_unresolved_request_per_specimen(people, world):
    t = people["technician"]
    specimen = stored(t, world)
    request = ask_disposal(t, specimen)
    refused("invalid_transition", ask_disposal, t, specimen)
    refused("invalid_transition", ask_disposal, people["technician2"], specimen)
    decide(people["approver"], request, "reject", "No")
    second = ask_disposal(t, specimen)  # a rejected request no longer blocks
    decide(people["approver"], second)
    refused("invalid_transition", ask_disposal, t, specimen)  # an approved one still does


def test_cancel_is_for_the_requester_or_a_manager_only(people, world):
    t = people["technician"]
    specimen = stored(t, world)
    request = ask_disposal(t, specimen)
    for outsider in ("technician2", "approver", "viewer", "nobody"):
        with pytest.raises(PermissionDenied):
            decide(people[outsider], request, "cancel", "Not mine")
    nothing_decided(request)
    cancelled = decide(t, request, "cancel", "No longer needed")
    assert cancelled.decision == "cancel" and cancelled.approved_revision is None
    assert AuditEvent.objects.filter(action="specimens.disposal_cancelled").count() == 1
    again = ask_disposal(t, specimen)
    assert decide(people["manager"], again, "cancel", "Manager decision").decision == "cancel"
    third = ask_disposal(t, specimen)
    assert decide(people["admin"], third, "cancel", "Admin decision").decision == "cancel"


def test_a_cancelled_approved_request_cannot_be_executed(people, world):
    t = people["technician"]
    specimen = stored(t, world)
    request = ask_disposal(t, specimen)
    decide(t, request, "cancel", "Changed our minds")
    refused("approval_required", execute, t, request)


def test_an_approved_request_cannot_be_cancelled(people, world):
    """One decision per request (workflows: an approved request is recorded or goes stale)."""
    specimen, request = approved_request(world, people)
    for actor in (people["technician"], people["manager"]):
        refused("invalid_transition", decide, actor, request, "cancel", "Changed our minds")
    assert DisposalDecision.objects.get(request=request).decision == "approve"
    assert services.request_state(DisposalRequest.objects.get(pk=request.pk)) == "approved"


def test_request_inputs_are_required(people, world):
    t = people["technician"]
    specimen = stored(t, world)
    for method, destination in (("", "Waste"), ("Autoclave", " ")):
        refused("required", ask_disposal, t, specimen, method=method, destination=destination)
    with pytest.raises(PermissionDenied):
        ask_disposal(people["viewer"], specimen)
    assert not DisposalRequest.objects.exists()


# --- execution ---------------------------------------------------------------------------------


def test_execution_frees_the_position_debits_the_rest_and_records_custody(people, world):
    t, a = people["technician"], people["approver"]
    specimen = stored(t, world, quantity="2")
    position = world.position("A1")
    request = ask_disposal(t, specimen, "Autoclave", "Biohazard waste bin 7")
    decide(a, request)
    executor = people["technician2"]
    execution = execute(executor, request)
    specimen = fresh(specimen)
    specimen.occupancy.refresh_from_db()
    assert specimen.status == "disposed" and specimen.quantity_on_hand == 0
    assert specimen.occupancy.position_id is None
    assert not Occupancy.objects.filter(position=position).exists()
    dispose = specimen.ledger.get(kind="dispose")
    assert dispose.delta == D("-2") and dispose.balance_after == 0
    assert specimen.ledger.filter(kind="dispose").count() == 1
    custody = CustodyEvent.objects.get(pk=execution.custody_event_id)
    assert custody.kind == "dispose" and custody.destination == "Biohazard waste bin 7"
    assert custody.to_custodian_id == executor.pk and custody.actor_id == executor.pk
    assert specimen.custodian_id == executor.pk
    assert execution.actor_id == executor.pk and execution.decision.actor_id == a.pk
    event = AuditEvent.objects.get(action="specimens.specimen_disposed")
    assert event.after["method"] == "Autoclave" and event.after["approved_by"] == a.pk
    # The freed position can be used at once.
    other = register(t, world, position=position, occurred_at=timezone.now())
    assert fresh(other).occupancy.position_id == position.pk


def test_a_partly_used_specimen_is_debited_only_for_the_remainder(people, world):
    t = people["technician"]
    specimen = stored(t, world, quantity="2")
    check_out(t, specimen, t)
    consume(t, specimen, "0.5", occurred_at=ago(190))
    request = ask_disposal(t, specimen)
    decide(people["approver"], request)
    execute(t, request)
    ledger = list(fresh(specimen).ledger.order_by("recorded_at", "id"))
    assert [e.kind for e in ledger] == ["initial", "consume", "dispose"]
    assert ledger[-1].delta == D("-1.5")
    assert sum(e.delta for e in ledger) == 0


def test_an_exhausted_vessel_is_disposed_without_a_zero_entry(people, world):
    t = people["technician"]
    specimen = stored(t, world, quantity="1")
    check_out(t, specimen, t)
    consume(t, specimen, "1", occurred_at=ago(190))
    request = ask_disposal(t, specimen)
    decide(people["approver"], request)
    execute(t, request)
    assert fresh(specimen).status == "disposed"
    assert not specimen.ledger.filter(kind="dispose").exists()


def test_disposal_closes_an_open_check_out_at_the_disposal_time(people, world):
    t = people["technician"]
    base = timezone.now()
    specimen = stored(t, world)
    check_out(t, specimen, t, occurred_at=base - timedelta(minutes=100))
    request = ask_disposal(t, specimen)
    decide(people["approver"], request)
    disposed_at = timezone.now()
    execute(t, request, occurred_at=disposed_at)
    specimen = fresh(specimen)
    assert specimen.open_checkout_id is None
    out = queries.time_out_of_storage(specimen, now=disposed_at + timedelta(days=5))
    assert not out.open and len(out.intervals) == 1
    assert out.intervals[0].ended == disposed_at and out.intervals[0].closed_by == "dispose"
    assert out.total == disposed_at - (base - timedelta(minutes=100))


def test_execution_cannot_precede_the_approval(people, world):
    specimen, request = approved_request(world, people)
    refused("invalid_time", execute, people["technician"], request, occurred_at=ago(100))
    refused("invalid_time", execute, people["technician"], request, ago(-10))
    assert fresh(specimen).status == "available"


def test_executing_twice_is_refused(people, world):
    specimen, request = approved_request(world, people)
    execute(people["technician"], request)
    refused("invalid_transition", execute, people["technician"], request)
    assert DisposalExecution.objects.filter(specimen=specimen).count() == 1


def test_a_viewer_cannot_execute(people, world):
    specimen, request = approved_request(world, people)
    with pytest.raises(PermissionDenied):
        execute(people["viewer"], request)
    assert fresh(specimen).status == "available"


# --- the disposed record is final --------------------------------------------------------------


def test_a_disposed_record_accepts_no_further_physical_change(people, world):
    t, m = people["technician"], people["manager"]
    specimen, request = approved_request(world, people)
    execute(t, request)
    specimen = fresh(specimen)
    entry = specimen.ledger.get(kind="dispose")
    now = timezone.now()
    base = {"specimen_id": specimen.pk, "expected_revision": specimen.revision}
    occupancy = {"expected_occupancy_revision": occ_rev(specimen)}
    attempts = {
        "place": lambda: services.place_specimen(
            actor=t,
            position_id=world.position("B1").pk,
            occurred_at=now,
            reason="r",
            **base,
            **occupancy,
        ),
        "relocate": lambda: services.relocate_specimen(
            actor=t,
            position_id=world.position("B1").pk,
            occurred_at=now,
            reason="r",
            **base,
            **occupancy,
        ),
        "check_out": lambda: services.check_out(
            actor=t,
            recipient_id=t.pk,
            destination="x",
            occurred_at=now,
            reason="r",
            **base,
            **occupancy,
        ),
        "check_in": lambda: services.check_in(
            actor=t,
            position_id=world.position("B1").pk,
            recipient_id=t.pk,
            thaw_observed=None,
            occurred_at=now,
            reason="r",
            **base,
            **occupancy,
        ),
        "transfer": lambda: services.transfer_custody(
            actor=t,
            recipient_id=m.pk,
            destination="",
            occurred_at=now,
            reason="r",
            **base,
            **occupancy,
        ),
        "derive": lambda: services.derive_specimens(
            actor=t,
            parent_id=specimen.pk,
            kind="aliquot",
            consumed=D("0.1"),
            children=(child(world, "0.1"),),
            procedure="",
            occurred_at=now,
            reason="r",
            expected_revision=specimen.revision,
            **occupancy,
        ),
        "consume": lambda: services.consume_quantity(
            actor=t, quantity=D("0.1"), supplied_text="", occurred_at=now, reason="r", **base
        ),
        "adjust": lambda: services.adjust_quantity(
            actor=m, delta=D("1"), supplied_text="", occurred_at=now, reason="r", **base
        ),
        "hold": lambda: services.place_hold(
            actor=t, scope="all_use", occurred_at=now, reason="r", **base
        ),
        "thaw": lambda: services.record_thaw(actor=t, occurred_at=now, reason="r", **base),
        "request": lambda: services.request_disposal(
            actor=t, method="m", destination="d", reason="r", **base
        ),
        "reverse": lambda: services.reverse_quantity(
            actor=m, entry_id=entry.pk, expected_revision=specimen.revision, reason="r"
        ),
    }
    for name, attempt in attempts.items():
        with pytest.raises(ValidationError) as exc:
            attempt()
        assert codes(exc.value) == {"invalid_transition"}, name
    after = fresh(specimen)
    assert (after.status, after.revision, after.quantity_on_hand) == (
        "disposed",
        specimen.revision,
        0,
    )


def correction(actor, specimen, **values):
    fields = frozenset(k.removesuffix("_id") for k in values)
    return services.correct_specimen(
        actor=actor,
        specimen_id=specimen.pk,
        expected_revision=fresh(specimen).revision,
        values=services.CorrectionValues(fields=fields, **values),
        reason="Wrong type entered at registration",
    )


def test_correcting_a_disposed_specimen_stores_an_annotation_and_changes_nothing(people, world):
    t, m = people["technician"], people["manager"]
    specimen, request = approved_request(world, people)
    execute(t, request)
    revision = fresh(specimen).revision
    with pytest.raises(PermissionDenied):
        correction(t, specimen, specimen_type_id=world.serum.pk)
    note = correction(m, specimen, specimen_type_id=world.serum.pk)
    assert note.annotation is True
    assert note.before == {"specimen_type": "PLASMA"} and note.after == {"specimen_type": "SERUM"}
    after = fresh(specimen)
    assert after.specimen_type_id == world.plasma.pk  # the physical record is unchanged
    assert (after.status, after.quantity_on_hand) == ("disposed", 0)
    assert after.revision == revision + 1
    assert AuditEvent.objects.filter(action="specimens.specimen_annotated").count() == 1


def test_correcting_a_live_specimen_applies_and_is_blocked_once_children_exist(people, world):
    t, m = people["technician"], people["manager"]
    specimen = stored(t, world, quantity="2")
    note = correction(m, specimen, specimen_type_id=world.serum.pk)
    assert note.annotation is False and fresh(specimen).specimen_type_id == world.serum.pk
    check_out(t, specimen, t, occurred_at=timezone.now())
    derive(
        t,
        specimen,
        [child(world, "1", specimen_type_id=world.serum.pk)],
        "1",
        occurred_at=timezone.now(),
    )
    refused("invalid_transition", correction, m, specimen, specimen_type_id=world.plasma.pk)
    assert fresh(specimen).specimen_type_id == world.serum.pk
    kid = Specimen.objects.get(parent=specimen)
    refused("invalid_transition", correction, m, kid, participant_id=None)  # inherited field
