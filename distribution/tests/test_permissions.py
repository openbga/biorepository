"""Role checks of every distribution service (contract "Permissions" table).

Each service is called with dummy keys. The role check comes first, so a refused role gets
PermissionDenied before any record is read, while an allowed role gets some other outcome (the
dummy key does not exist, or the values are incomplete) and never PermissionDenied. The
journey test at the end shows the technician doing the work the table allows. Synthetic data.
"""

import uuid
from decimal import Decimal

import pytest
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.utils import timezone

from distribution import queries, services
from specimens.tests.conftest import occ_rev

from .helpers import (
    allocate,
    approved_request,
    declaration,
    fast_password_hashing,  # noqa: F401
    fresh,
    make_user,
    pick,
    picked_request,
    refused,
    shipment_for,
)

pytestmark = pytest.mark.django_db

KEY = uuid.uuid4()
NUMBER = 987654


def now():
    return timezone.now()


def manager_only():
    """name -> callable(actor) for the services restricted to admin and manager."""
    return {
        "save_distribution_catalogue": lambda a: services.save_distribution_catalogue(
            actor=a, kind="category", object_id=KEY, expected_revision=1, values={}, reason="r"
        ),
        "save_study": lambda a: services.save_study(
            actor=a, study_id=KEY, expected_revision=1, values={}, reason="r"
        ),
        "link_collection": lambda a: services.link_collection(
            actor=a, study_id=KEY, expected_revision=1, collection_id=KEY, reason="r"
        ),
        "end_collection_link": lambda a: services.end_collection_link(
            actor=a, link_id=KEY, expected_revision=1, reason="r"
        ),
        "record_consent": lambda a: services.record_consent(
            actor=a,
            participant_id=KEY,
            study_id=KEY,
            form_id=KEY,
            decision="consented",
            answers={},
            consented_on=timezone.localdate(),
            signer="participant",
            documentation="signed",
            source_reference="",
            restrictions="",
            expires_on=None,
            expected_revision=None,
            reason="r",
        ),
        "correct_consent_record": lambda a: services.correct_consent_record(
            actor=a, record_id=KEY, expected_revision=1, values={}, answers=None, reason="r"
        ),
        "withdraw_consent": lambda a: services.withdraw_consent(
            actor=a,
            consent_id=KEY,
            expected_revision=1,
            kind="no_use",
            effective_on=timezone.localdate(),
            received_via="Letter",
            occurred_at=now(),
            reason="r",
        ),
        "hold_withdrawn_specimens": lambda a: services.hold_withdrawn_specimens(
            actor=a, consent_id=KEY, expected_revision=1, occurred_at=now(), reason="r"
        ),
        "request_withdrawal_disposals": lambda a: services.request_withdrawal_disposals(
            actor=a,
            consent_id=KEY,
            expected_revision=1,
            method="Autoclave",
            destination="Waste",
            reason="r",
        ),
        "reinstate_consent": lambda a: services.reinstate_consent(
            actor=a,
            consent_id=KEY,
            expected_revision=1,
            record_id=None,
            occurred_at=now(),
            reason="r",
        ),
        "record_follow_up": lambda a: services.record_follow_up(
            actor=a,
            withdrawal_id=KEY,
            shipment_id=NUMBER,
            action="note",
            note="n",
            occurred_at=now(),
        ),
        "reopen_request": lambda a: services.reopen_request(
            actor=a, request_id=NUMBER, expected_revision=1, reason="r"
        ),
        "cancel_request": lambda a: services.cancel_request(
            actor=a, request_id=NUMBER, expected_revision=1, reason="r"
        ),
        "close_request": lambda a: services.close_request(
            actor=a, request_id=NUMBER, expected_revision=1, reason="r"
        ),
        "link_agreement": lambda a: services.link_agreement(
            actor=a, request_id=NUMBER, expected_revision=1, agreement_id=None, reason="r"
        ),
        "allocate": lambda a: services.allocate(
            actor=a,
            request_id=NUMBER,
            expected_revision=1,
            item_id=KEY,
            choices=(services.Choice(KEY, 1),),
            below_minimum_ok=False,
            occurred_at=now(),
            reason="r",
        ),
        "release_allocations": lambda a: services.release_allocations(
            actor=a,
            request_id=NUMBER,
            expected_revision=1,
            allocation_ids=(KEY,),
            occurred_at=now(),
            reason="r",
        ),
        "put_back_with_release": lambda a: services.put_back(
            actor=a,
            request_id=NUMBER,
            expected_revision=1,
            lines=(services.PutBackLine(KEY, 1, 1, KEY),),
            recipient_id=a.pk,
            release=True,
            occurred_at=now(),
            reason="r",
        ),
        "cancel_shipment": lambda a: services.cancel_shipment(
            actor=a, shipment_id=NUMBER, expected_revision=1, reason="r"
        ),
        "correct_receipt": lambda a: services.correct_receipt(
            actor=a,
            receipt_id=KEY,
            expected_revision=1,
            values=receipt(),
            reason="r",
        ),
        "review_return": lambda a: services.review_return(
            actor=a,
            return_id=KEY,
            expected_revision=1,
            decision="release",
            occurred_at=now(),
            reason="r",
        ),
        "save_agreement": lambda a: services.save_agreement(
            actor=a, agreement_id=NUMBER, expected_revision=1, values={}, reason="r"
        ),
        "execute_agreement": lambda a: services.execute_agreement(
            actor=a,
            agreement_id=NUMBER,
            expected_revision=1,
            executed_on=timezone.localdate(),
            effective_on=timezone.localdate(),
            expires_on=None,
            document_reference="Cabinet",
            reason="r",
        ),
        "terminate_agreement": lambda a: services.terminate_agreement(
            actor=a,
            agreement_id=NUMBER,
            expected_revision=1,
            terminated_on=timezone.localdate(),
            reason="r",
        ),
        "add_agreement_document": lambda a: services.add_agreement_document(
            actor=a, agreement_id=NUMBER, kind="other", title="t", note="", upload=None
        ),
    }


def receipt():
    return services.ReceiptValues(
        received_at=now(),
        received_by_name="Example Receiver",
        unpacked_by_name="",
        condition="as_expected",
        refrigerant_on_arrival="adequate",
        temperature_note="",
        discrepancies="",
        received_via="email",
    )


def operators():
    """name -> callable(actor) for the services open to admin, manager and technician."""
    return {
        "save_request": lambda a: services.save_request(
            actor=a, request_id=None, expected_revision=None, values={}, items=()
        ),
        "submit_request": lambda a: services.submit_request(
            actor=a, request_id=NUMBER, expected_revision=1
        ),
        "add_request_note": lambda a: services.add_request_note(
            actor=a, request_id=NUMBER, note="n"
        ),
        "pick": lambda a: services.pick(
            actor=a,
            request_id=NUMBER,
            expected_revision=1,
            lines=(services.PickLine(KEY, 1, 1),),
            recipient_id=a.pk,
            occurred_at=now(),
            reason="r",
        ),
        "put_back": lambda a: services.put_back(
            actor=a,
            request_id=NUMBER,
            expected_revision=1,
            lines=(services.PutBackLine(KEY, 1, 1, KEY),),
            recipient_id=a.pk,
            release=False,
            occurred_at=now(),
            reason="r",
        ),
        "create_shipment": lambda a: services.create_shipment(
            actor=a, request_id=NUMBER, expected_revision=1, values={}
        ),
        "edit_shipment": lambda a: services.edit_shipment(
            actor=a, shipment_id=NUMBER, expected_revision=1, values={}, reason="r"
        ),
        "set_shipment_contents": lambda a: services.set_shipment_contents(
            actor=a, shipment_id=NUMBER, expected_revision=1, allocation_ids=()
        ),
        "pack_shipment": lambda a: services.pack_shipment(
            actor=a,
            shipment_id=NUMBER,
            expected_revision=1,
            declaration=declaration(),
            checked_by_id=None,
            occurred_at=now(),
            reason="r",
        ),
        "unpack_shipment": lambda a: services.unpack_shipment(
            actor=a, shipment_id=NUMBER, expected_revision=1, reason="r"
        ),
        "record_notification": lambda a: services.record_notification(
            actor=a,
            shipment_id=NUMBER,
            expected_revision=1,
            kind="notified",
            occurred_at=now(),
            note="n",
        ),
        "dispatch_shipment": lambda a: services.dispatch_shipment(
            actor=a,
            shipment_id=NUMBER,
            expected_revision=1,
            courier="c",
            tracking="t",
            handed_to="h",
            occurred_at=now(),
            reason="r",
        ),
        "record_receipt": lambda a: services.record_receipt(
            actor=a, shipment_id=NUMBER, expected_revision=1, values=receipt(), reason="r"
        ),
        "add_shipment_note": lambda a: services.add_shipment_note(
            actor=a, shipment_id=NUMBER, note="n", occurred_at=now()
        ),
        "record_return": lambda a: services.record_return(
            actor=a,
            item_id=KEY,
            expected_revision=1,
            quantity=Decimal("1"),
            supplied_text="1",
            thaw_count=None,
            condition="c",
            recipient_id=a.pk,
            occurred_at=now(),
            reason="r",
        ),
        "decide_request": lambda a: services.decide_request(
            actor=a,
            request_id=NUMBER,
            expected_revision=1,
            submission_number=1,
            decision="approve",
            conditions="",
            committee_reference="",
            conflicts_note="",
            reason="r",
        ),
    }


MANAGER_ONLY = manager_only()
OPERATORS = operators()


@pytest.mark.parametrize("name", sorted(MANAGER_ONLY))
@pytest.mark.parametrize("who", ["technician", "technician2", "decider", "viewer", "nobody"])
def test_manager_only_services_refuse_everyone_else(people, name, who):
    with pytest.raises(PermissionDenied):
        MANAGER_ONLY[name](people[who])


@pytest.mark.parametrize("name", sorted(MANAGER_ONLY))
@pytest.mark.parametrize("who", ["admin", "manager", "manager2", "decider_manager", "superuser"])
def test_manager_only_services_do_not_refuse_the_role(people, name, who):
    try:
        MANAGER_ONLY[name](people[who])
    except PermissionDenied:
        pytest.fail(f"{who} was refused {name}")
    except ObjectDoesNotExist, ValidationError:
        pass  # the dummy key does not exist or the values are incomplete


@pytest.mark.parametrize("name", sorted(OPERATORS))
@pytest.mark.parametrize("who", ["viewer", "nobody"])
def test_operating_services_refuse_viewer_and_nobody(people, name, who):
    with pytest.raises(PermissionDenied):
        OPERATORS[name](people[who])


@pytest.mark.parametrize("name", sorted(set(OPERATORS) - {"decide_request"}))
@pytest.mark.parametrize(
    "who", ["admin", "manager", "technician", "technician2", "decider", "superuser"]
)
def test_operating_services_do_not_refuse_the_role(people, name, who):
    try:
        OPERATORS[name](people[who])
    except PermissionDenied:
        pytest.fail(f"{who} was refused {name}")
    except ObjectDoesNotExist, ValidationError:
        pass


@pytest.mark.parametrize("who", ["technician", "technician2", "admin", "manager", "superuser"])
def test_deciding_needs_the_explicit_grant(people, who):
    with pytest.raises(PermissionDenied):
        OPERATORS["decide_request"](people[who])


@pytest.mark.parametrize("who", ["decider", "decider_manager"])
def test_the_grant_with_an_operating_role_gets_past_the_permission_check(people, who):
    with pytest.raises(ObjectDoesNotExist):
        OPERATORS["decide_request"](people[who])


def test_the_grant_alone_is_not_an_operating_role(people):
    grantee = make_user("dist-grant-only")
    grantee.user_permissions.add(people["decider"].user_permissions.first())
    with pytest.raises(PermissionDenied):
        OPERATORS["decide_request"](grantee)


# ------------------------------------------------------------------ reads


def test_reading_is_open_to_every_role_and_closed_to_nobody(people, dist, world):
    request = approved_request(people, dist, world)
    item = request.items.get()
    for who in ("admin", "manager", "technician", "viewer", "superuser"):
        actor = people[who]
        queries.pick_list(actor=actor, request_id=request.pk)
        queries.candidates(actor=actor, request_id=request.pk, item_id=item.pk, filters={})
        queries.consent_impact(actor=actor, consent_id=dist.consent.pk)
        queries.blockers(actor=actor, request_id=request.pk, specimen_ids=[dist.specimens[0].pk])
    nobody = people["nobody"]
    for call in (
        lambda: queries.pick_list(actor=nobody, request_id=request.pk),
        lambda: queries.candidates(
            actor=nobody, request_id=request.pk, item_id=item.pk, filters={}
        ),
        lambda: queries.consent_impact(actor=nobody, consent_id=dist.consent.pk),
        lambda: queries.blockers(
            actor=nobody, request_id=request.pk, specimen_ids=[dist.specimens[0].pk]
        ),
    ):
        with pytest.raises(PermissionDenied):
            call()


# ------------------------------------------------------------------ what a technician can do


def test_a_technician_does_the_operating_work_end_to_end(people, dist, world):
    tech = people["technician"]
    # The request is created and submitted by the technician, decided by another person, and
    # allocated by a manager.
    request = approved_request(people, dist, world)
    assert request.created_by_id == tech.pk
    allocations = allocate(people, request, dist.specimens[:2])
    # Technician: pick, put back, shipments (create, edit, contents, pack, unpack, notify,
    # dispatch), receipt, return, notes.
    pick(people, request, allocations, actor=tech)
    request.refresh_from_db()
    shipment = services.create_shipment(
        actor=tech, request_id=request.pk, expected_revision=request.revision, values={}
    )
    shipment = services.edit_shipment(
        actor=tech,
        shipment_id=shipment.pk,
        expected_revision=shipment.revision,
        values={"courier": "Example Courier"},
        reason="Booked",
    )
    shipment = services.set_shipment_contents(
        actor=tech,
        shipment_id=shipment.pk,
        expected_revision=shipment.revision,
        allocation_ids=tuple(a.pk for a in allocations),
    )
    shipment = services.pack_shipment(
        actor=tech,
        shipment_id=shipment.pk,
        expected_revision=shipment.revision,
        declaration=declaration(),
        checked_by_id=people["technician2"].pk,
        occurred_at=now(),
        reason="Packed",
    )
    shipment = services.unpack_shipment(
        actor=tech, shipment_id=shipment.pk, expected_revision=shipment.revision, reason="Recheck"
    )
    shipment = services.pack_shipment(
        actor=tech,
        shipment_id=shipment.pk,
        expected_revision=shipment.revision,
        declaration=declaration(),
        checked_by_id=people["technician2"].pk,
        occurred_at=now(),
        reason="Packed again",
    )
    services.record_notification(
        actor=tech,
        shipment_id=shipment.pk,
        expected_revision=shipment.revision,
        kind="notified",
        occurred_at=now(),
        note="Told by email (synthetic)",
    )
    services.add_shipment_note(
        actor=tech, shipment_id=shipment.pk, note="Label checked", occurred_at=now()
    )
    services.add_request_note(actor=tech, request_id=request.pk, note="Packed and ready")
    shipment.refresh_from_db()
    services.dispatch_shipment(
        actor=tech,
        shipment_id=shipment.pk,
        expected_revision=shipment.revision,
        courier="Example Courier",
        tracking="EX1",
        handed_to="Courier driver",
        occurred_at=now(),
        reason="Collected",
    )
    shipment.refresh_from_db()
    receipt_row = services.record_receipt(
        actor=tech,
        shipment_id=shipment.pk,
        expected_revision=shipment.revision,
        values=receipt(),
        reason="Recipient's email",
    )
    assert receipt_row.actor_id == tech.pk
    item = shipment.items.first()
    returned = services.record_return(
        actor=tech,
        item_id=item.pk,
        expected_revision=fresh(item.specimen).revision,
        quantity=Decimal("1"),
        supplied_text="1",
        thaw_count=None,
        condition="Unopened",
        recipient_id=tech.pk,
        occurred_at=now(),
        reason="Returned",
    )
    assert returned.actor_id == tech.pk


def test_a_technician_can_put_back_but_not_with_release(people, dist, world):
    request, allocations = picked_request(people, dist, world, count=1)
    specimen = allocations[0].specimen
    line = services.PutBackLine(
        allocation_id=allocations[0].pk,
        expected_revision=fresh(specimen).revision,
        expected_occupancy_revision=occ_rev(specimen),
        position_id=world.position("C1").pk,
    )

    def put_back(actor, release):
        request.refresh_from_db()
        return services.put_back(
            actor=actor,
            request_id=request.pk,
            expected_revision=request.revision,
            lines=(line,),
            recipient_id=actor.pk,
            release=release,
            occurred_at=now(),
            reason="Returned to storage",
        )

    with pytest.raises(PermissionDenied):
        put_back(people["technician"], True)
    allocations[0].refresh_from_db()
    assert allocations[0].state == "picked"
    assert put_back(people["technician"], False)[0].state == "allocated"


def test_technicians_cannot_cancel_a_shipment_or_check_their_own_packing(people, dist, world):
    request, allocations, shipment = shipment_for(people, dist, world, count=1)
    shipment.refresh_from_db()
    with pytest.raises(PermissionDenied):
        services.cancel_shipment(
            actor=people["technician"],
            shipment_id=shipment.pk,
            expected_revision=shipment.revision,
            reason="Cancel",
        )
    shipment.refresh_from_db()
    assert shipment.state == "packed"
    refused(
        lambda: services.pack_shipment(
            actor=people["technician"],
            shipment_id=shipment.pk,
            expected_revision=shipment.revision,
            declaration=declaration(),
            checked_by_id=people["technician"].pk,
            occurred_at=now(),
            reason="Self check",
        ),
        "self_approval",
    )
