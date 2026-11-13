"""Requests: save, submit, decide (four-eyes), return and resubmit, reopen, cancel, close and
agreement linking. All data is synthetic."""

import pytest
from django.core.exceptions import PermissionDenied
from django.utils import timezone

from core.models import AuditEvent
from distribution import services
from distribution.models import (
    RequestDecision,
    RequestEvent,
    RequestSubmission,
    ShipmentEvent,
    SpecimenRequest,
)
from specimens.models import Hold

from .helpers import (
    ConsentScopeCategory,
    allocate,
    approve,
    approved_request,
    dispatch,
    fast_password_hashing,  # noqa: F401
    fresh,
    link,
    make_agreement,
    new_request,
    pick,
    refused,
    shipment_for,
)

pytestmark = pytest.mark.django_db


def item_specs(world, count=3):
    return (services.ItemSpec(line=1, specimen_type_id=world.plasma.pk, count=count),)


def values_for(dist, **extra):
    return {
        "title": "Plasma for assay development",
        "investigator": dist.investigator.pk,
        "purpose": "Method validation (synthetic)",
        "received_via": "email",
        "use_categories": [ConsentScopeCategory.objects.get(code="GENERAL").pk],
        **extra,
    }


def edit(people, request, world, dist, actor="technician", **values):
    request.refresh_from_db()
    return services.save_request(
        actor=people[actor],
        request_id=request.pk,
        expected_revision=request.revision,
        values=values,
        items=item_specs(world),
        reason="Clarified the title",
    )


def submit(people, request, actor="technician"):
    request.refresh_from_db()
    return services.submit_request(
        actor=people[actor], request_id=request.pk, expected_revision=request.revision
    )


def decide(people, request, decision="approve", actor="decider_manager", number=None):
    request.refresh_from_db()
    return services.decide_request(
        actor=people[actor],
        request_id=request.pk,
        expected_revision=request.revision,
        submission_number=number if number is not None else request.latest_submission,
        decision=decision,
        conditions="",
        committee_reference="Committee minute (synthetic)",
        conflicts_note="None declared",
        reason="Checked against the consented scope",
    )


# ------------------------------------------------------------------ save and submit


def test_a_new_request_is_a_draft_with_its_lines_and_an_event(people, dist, world):
    request = new_request(people["technician"], dist, world)
    assert request.status == "draft" and request.number.startswith("RQ-")
    assert request.institution_id == dist.institution.pk and request.revision == 1
    assert request.items.get().count == 3
    assert [e.kind for e in request.events.all()] == ["created"]
    assert AuditEvent.objects.filter(action="distribution.request_saved").exists()


def test_a_draft_can_be_edited_with_a_reason_and_keeps_a_history(people, dist, world):
    request = new_request(people["technician"], dist, world)
    edited = edit(people, request, world, dist, title="Plasma for a revised assay")
    assert edited.title == "Plasma for a revised assay" and edited.revision == 2
    event = RequestEvent.objects.filter(request=request, kind="edited").get()
    assert event.reason == "Clarified the title" and "title" in event.changes
    request.refresh_from_db()
    refused(
        lambda: services.save_request(
            actor=people["technician"],
            request_id=request.pk,
            expected_revision=request.revision,
            values={"title": "x"},
            items=item_specs(world),
            reason="  ",
        ),
        "required",
    )


@pytest.mark.parametrize("status", ["submitted", "approved", "rejected", "cancelled", "closed"])
def test_only_a_draft_or_returned_request_can_be_edited(people, dist, world, status):
    request = new_request(people["technician"], dist, world)
    SpecimenRequest.objects.filter(pk=request.pk).update(status=status)
    refused(lambda: edit(people, request, world, dist, title="Changed"), "invalid_transition")


def test_a_returned_request_can_be_edited(people, dist, world):
    request = new_request(people["technician"], dist, world)
    submit(people, request)
    decide(people, request, "return")
    request.refresh_from_db()
    assert request.status == "returned"
    assert edit(people, request, world, dist, title="After feedback").title == "After feedback"


def test_a_request_needs_use_categories_and_lines_to_submit(people, dist, world):
    bare = services.save_request(
        actor=people["technician"],
        request_id=None,
        expected_revision=None,
        values=values_for(dist, use_categories=[]),
        items=item_specs(world),
    )
    refused(lambda: submit(people, bare), "required")
    empty = services.save_request(
        actor=people["technician"],
        request_id=None,
        expected_revision=None,
        values=values_for(dist),
        items=(),
    )
    refused(lambda: submit(people, empty), "invalid_transition")


def test_submit_snapshots_the_request_and_counts_submissions(people, dist, world):
    request = new_request(people["technician"], dist, world)
    submission = submit(people, request)
    request.refresh_from_db()
    assert submission.number == 1 and request.latest_submission == 1
    assert request.status == "submitted"
    data = submission.data
    assert data["number"] == request.number and data["title"] == "Plasma for assay development"
    assert data["use_categories"] == ["GENERAL"] and data["investigator"]["code"] == "INV-001"
    assert data["items"][0]["specimen_type"] == "PLASMA" and data["items"][0]["count"] == 3
    assert submission.submitted_by_id == people["technician"].pk
    refused(lambda: submit(people, request), "invalid_transition")  # already submitted


def test_a_submission_is_frozen_when_the_request_changes_later(people, dist, world):
    request = new_request(people["technician"], dist, world)
    submit(people, request)
    decide(people, request, "return")
    edit(people, request, world, dist, title="Second version")
    second = submit(people, request)
    first = RequestSubmission.objects.get(request=request, number=1)
    assert first.data["title"] == "Plasma for assay development"
    assert second.number == 2 and second.data["title"] == "Second version"


# ------------------------------------------------------------------ decisions


def test_the_creator_and_the_submitter_cannot_decide_even_with_the_grant(people, dist, world):
    people["technician"].user_permissions.add(people["decider"].user_permissions.first())
    request = new_request(people["technician"], dist, world)
    submit(people, request)
    refused(lambda: decide(people, request, actor="technician"), "self_approval")
    # A grant holder who only submitted (the creator is another person) is refused too.
    other = new_request(people["decider_manager"], dist, world)
    submit(people, other, actor="decider")
    refused(lambda: decide(people, other, actor="decider"), "self_approval")
    refused(lambda: decide(people, other, actor="decider_manager"), "self_approval")
    assert not RequestDecision.objects.exists()
    request.refresh_from_db()
    assert request.status == "submitted"


def test_the_creator_is_refused_even_when_someone_else_submitted(people, dist, world):
    request = new_request(people["decider"], dist, world)
    submit(people, request, actor="technician")
    refused(lambda: decide(people, request, actor="decider"), "self_approval")
    assert decide(people, request, actor="decider_manager").decision == "approve"


def test_a_superuser_with_the_admin_role_still_needs_the_grant(people, dist, world):
    request = new_request(people["technician"], dist, world)
    submit(people, request)
    assert people["superuser"].is_superuser
    with pytest.raises(PermissionDenied):
        decide(people, request, actor="superuser")
    with pytest.raises(PermissionDenied):
        decide(people, request, actor="admin")
    with pytest.raises(PermissionDenied):
        decide(people, request, actor="manager")
    assert not RequestDecision.objects.exists()


def test_a_superuser_who_created_and_holds_the_grant_is_still_refused(people, dist, world):
    superuser = people["superuser"]
    superuser.user_permissions.add(people["decider"].user_permissions.first())
    request = new_request(superuser, dist, world)
    submit(people, request, actor="technician")
    refused(lambda: decide(people, request, actor="superuser"), "self_approval")


def test_another_person_with_the_grant_and_an_operating_role_decides(people, dist, world):
    for actor in ("decider", "decider_manager"):
        request = new_request(people["technician"], dist, world)
        submit(people, request)
        row = decide(people, request, actor=actor)
        request.refresh_from_db()
        assert row.actor_id == people[actor].pk and request.status == "approved"
        assert row.submission.number == 1 and row.committee_reference.startswith("Committee")


def test_a_viewer_with_the_grant_cannot_decide(people, dist, world):
    people["viewer"].user_permissions.add(people["decider"].user_permissions.first())
    request = new_request(people["technician"], dist, world)
    submit(people, request)
    with pytest.raises(PermissionDenied):
        decide(people, request, actor="viewer")


@pytest.mark.parametrize(
    "decision,status", [("approve", "approved"), ("reject", "rejected"), ("return", "returned")]
)
def test_each_decision_sets_the_status_and_writes_one_row(people, dist, world, decision, status):
    request = new_request(people["technician"], dist, world)
    submit(people, request)
    decide(people, request, decision)
    request.refresh_from_db()
    assert request.status == status and RequestDecision.objects.get().decision == decision


def test_a_decision_needs_a_submitted_request_and_a_known_decision(people, dist, world):
    request = new_request(people["technician"], dist, world)
    refused(lambda: decide(people, request, number=0), "invalid_transition")  # still a draft
    submit(people, request)
    refused(lambda: decide(people, request, "maybe"), "invalid")
    request.refresh_from_db()
    refused(
        lambda: services.decide_request(
            actor=people["decider_manager"],
            request_id=request.pk,
            expected_revision=request.revision,
            submission_number=1,
            decision="approve",
            conditions="",
            committee_reference="",
            conflicts_note="",
            reason="",
        ),
        "required",
    )


def test_only_the_latest_submission_can_be_decided(people, dist, world):
    request = new_request(people["technician"], dist, world)
    submit(people, request)
    decide(people, request, "return")
    edit(people, request, world, dist, title="Second version")
    submit(people, request)
    refused(lambda: decide(people, request, number=1), "stale_revision")
    assert decide(people, request, number=2).submission.number == 2


def test_return_edit_resubmit_decide_again(people, dist, world):
    request = new_request(people["technician"], dist, world)
    submit(people, request)
    decide(people, request, "return")
    edit(people, request, world, dist, title="Answered the committee")
    submit(people, request)
    decide(people, request, "approve", actor="decider")
    request.refresh_from_db()
    assert request.status == "approved" and request.latest_submission == 2
    assert list(
        RequestDecision.objects.order_by("submission__number").values_list(
            "submission__number", "decision"
        )
    ) == [(1, "return"), (2, "approve")]


def test_a_decision_is_audited_with_the_actor(people, dist, world):
    request = new_request(people["technician"], dist, world)
    submit(people, request)
    decide(people, request)
    event = AuditEvent.objects.filter(action="distribution.request_decided").get()
    assert event.actor_id == people["decider_manager"].pk


# ------------------------------------------------------------------ reopen, cancel, close


def test_reopen_returns_an_approved_request_to_draft_and_it_can_be_approved_again(
    people, dist, world
):
    request = approved_request(people, dist, world)
    allocations = allocate(people, request, dist.specimens[:2])
    services.reopen_request(
        actor=people["manager"],
        request_id=request.pk,
        expected_revision=request.revision,
        reason="The investigator changed the plan",
    )
    request.refresh_from_db()
    assert request.status == "draft"
    assert RequestEvent.objects.filter(request=request, kind="reopened").exists()
    assert {fresh(a).state for a in allocations} == {"allocated"}
    approve(people, request, agreement=None)
    request.refresh_from_db()
    assert request.status == "approved" and request.latest_submission == 2
    pick(people, request, allocations)


def test_reopen_is_refused_while_specimens_are_picked_or_the_request_is_not_approved(
    people, dist, world
):
    request = approved_request(people, dist, world)
    allocations = allocate(people, request, dist.specimens[:1])
    pick(people, request, allocations)
    refused(
        lambda: services.reopen_request(
            actor=people["manager"],
            request_id=request.pk,
            expected_revision=request.revision,
            reason="Changed plan",
        ),
        "invalid_transition",
    )
    draft = new_request(people["technician"], dist, world)
    refused(
        lambda: services.reopen_request(
            actor=people["manager"],
            request_id=draft.pk,
            expected_revision=draft.revision,
            reason="Changed plan",
        ),
        "invalid_transition",
    )


def test_cancel_releases_allocations_and_their_holds_and_cancels_draft_shipments(
    people, dist, world
):
    request = approved_request(people, dist, world)
    allocations = allocate(people, request, dist.specimens[:2])
    shipment = services.create_shipment(
        actor=people["technician"],
        request_id=request.pk,
        expected_revision=fresh(request).revision,
        values={},
    )
    request.refresh_from_db()
    services.cancel_request(
        actor=people["manager"],
        request_id=request.pk,
        expected_revision=request.revision,
        reason="Request withdrawn by the investigator",
    )
    request.refresh_from_db()
    shipment.refresh_from_db()
    assert request.status == "cancelled" and shipment.state == "cancelled"
    assert ShipmentEvent.objects.filter(shipment=shipment, kind="cancelled").exists()
    assert {fresh(a).state for a in allocations} == {"released"}
    assert not Hold.objects.filter(
        specimen__in=dist.specimens, release__isnull=True, reference__startswith="Allocated"
    ).exists()
    assert RequestEvent.objects.filter(request=request, kind="cancelled").exists()
    # The specimens can be allocated to another request now.
    other = approved_request(people, dist, world)
    assert len(allocate(people, other, dist.specimens[:2])) == 2


def test_cancel_is_refused_with_picked_material_and_for_final_requests(people, dist, world):
    request = approved_request(people, dist, world)
    allocations = allocate(people, request, dist.specimens[:1])
    pick(people, request, allocations)
    request.refresh_from_db()

    def cancel(r):
        r.refresh_from_db()
        return services.cancel_request(
            actor=people["manager"],
            request_id=r.pk,
            expected_revision=r.revision,
            reason="No longer needed",
        )

    refused(lambda: cancel(request), "invalid_transition")
    done = new_request(people["technician"], dist, world)
    cancel(done)
    refused(lambda: cancel(done), "invalid_transition")


def test_cancel_works_from_draft_submitted_and_returned(people, dist, world):
    for stage in ("draft", "submitted", "returned"):
        request = new_request(people["technician"], dist, world)
        if stage != "draft":
            submit(people, request)
        if stage == "returned":
            decide(people, request, "return")
        request.refresh_from_db()
        services.cancel_request(
            actor=people["manager"],
            request_id=request.pk,
            expected_revision=request.revision,
            reason="No longer needed",
        )
        request.refresh_from_db()
        assert request.status == "cancelled"


def test_close_requires_nothing_open(people, dist, world):
    request = approved_request(people, dist, world)
    allocations = allocate(people, request, dist.specimens[:1])

    def close():
        request.refresh_from_db()
        return services.close_request(
            actor=people["manager"],
            request_id=request.pk,
            expected_revision=request.revision,
            reason="Everything requested was sent",
        )

    refused(close, "invalid_transition")  # allocated
    services.release_allocations(
        actor=people["manager"],
        request_id=request.pk,
        expected_revision=fresh(request).revision,
        allocation_ids=tuple(a.pk for a in allocations),
        occurred_at=timezone.now(),
        reason="Not needed after all",
    )
    request.refresh_from_db()
    draft = services.create_shipment(
        actor=people["technician"],
        request_id=request.pk,
        expected_revision=request.revision,
        values={},
    )
    refused(close, "invalid_transition")  # a draft shipment in preparation
    services.cancel_shipment(
        actor=people["manager"],
        shipment_id=draft.pk,
        expected_revision=draft.revision,
        reason="Not needed",
    )
    assert close().status == "closed"
    assert RequestEvent.objects.filter(request=request, kind="closed").exists()
    refused(close, "invalid_transition")


def test_close_is_possible_after_dispatch(people, dist, world):
    request, allocations, shipment = shipment_for(people, dist, world, count=1)
    dispatch(people, shipment)
    request.refresh_from_db()
    row = services.close_request(
        actor=people["manager"],
        request_id=request.pk,
        expected_revision=request.revision,
        reason="Everything requested was sent",
    )
    assert row.status == "closed"


# ------------------------------------------------------------------ line changes under allocations


def test_lines_with_allocations_keep_their_type_and_study(people, dist, world):
    other = approved_request(people, dist, world)
    allocate(people, other, dist.specimens[:1])
    services.reopen_request(
        actor=people["manager"],
        request_id=other.pk,
        expected_revision=fresh(other).revision,
        reason="Changed plan",
    )
    other.refresh_from_db()
    refused(
        lambda: services.save_request(
            actor=people["technician"],
            request_id=other.pk,
            expected_revision=other.revision,
            values={},
            items=(services.ItemSpec(line=1, specimen_type_id=world.serum.pk, count=3),),
            reason="Different material",
        ),
        "invalid",
    )
    refused(
        lambda: services.save_request(
            actor=people["technician"],
            request_id=other.pk,
            expected_revision=other.revision,
            values={},
            items=(services.ItemSpec(line=1, specimen_type_id=world.plasma.pk, count=0),),
            reason="Fewer",
        ),
        "invalid",
    )


# ------------------------------------------------------------------ link_agreement


def test_link_agreement_needs_the_same_institution_and_a_live_agreement(people, dist, world):
    request = approved_request(people, dist, world, agreement=False)
    elsewhere = services.save_distribution_catalogue(
        actor=people["manager"],
        kind="institution",
        object_id=None,
        expected_revision=None,
        values={"code": "OTH", "name": "Other Institute", "address": "1 Example Road"},
    )
    foreign = make_agreement(people, dist, institution=elsewhere)
    request.refresh_from_db()
    refused(
        lambda: services.link_agreement(
            actor=people["manager"],
            request_id=request.pk,
            expected_revision=request.revision,
            agreement_id=foreign.pk,
            reason="Wrong institution",
        ),
        "invalid",
    )
    link(people, request, dist.agreement)
    assert request.agreement_id == dist.agreement.pk
    assert RequestEvent.objects.filter(request=request, kind="agreement_linked").exists()
    link(people, request, None, reason="Linked in error")
    assert request.agreement_id is None


def test_a_draft_agreement_can_be_linked_but_a_terminated_one_cannot(people, dist, world):
    request = approved_request(people, dist, world, agreement=False)
    draft = make_agreement(people, dist, execute=False)
    link(people, request, draft)
    ended = make_agreement(people, dist)
    ended = services.terminate_agreement(
        actor=people["manager"],
        agreement_id=ended.pk,
        expected_revision=ended.revision,
        terminated_on=timezone.localdate(),
        reason="Terminated by the institution",
    )
    refused(
        lambda: services.link_agreement(
            actor=people["manager"],
            request_id=request.pk,
            expected_revision=fresh(request).revision,
            agreement_id=ended.pk,
            reason="Try the old one",
        ),
        "invalid",
    )


def test_a_final_request_cannot_get_an_agreement(people, dist, world):
    request = new_request(people["technician"], dist, world)
    services.cancel_request(
        actor=people["manager"],
        request_id=request.pk,
        expected_revision=request.revision,
        reason="No longer needed",
    )
    request.refresh_from_db()
    refused(
        lambda: services.link_agreement(
            actor=people["manager"],
            request_id=request.pk,
            expected_revision=request.revision,
            agreement_id=dist.agreement.pk,
            reason="Late link",
        ),
        "invalid_transition",
    )


# ------------------------------------------------------------------ notes


def test_notes_are_events_and_technicians_may_add_them(people, dist, world):
    request = new_request(people["technician"], dist, world)
    services.add_request_note(
        actor=people["technician2"], request_id=request.pk, note="Investigator called"
    )
    event = RequestEvent.objects.filter(request=request, kind="note").get()
    assert event.reason == "Investigator called"
    refused(
        lambda: services.add_request_note(
            actor=people["technician"], request_id=request.pk, note="  "
        ),
        "required",
    )
