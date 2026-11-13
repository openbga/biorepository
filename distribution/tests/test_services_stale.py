"""Every service that takes an expected revision refuses a stale one with `stale_revision` and
changes nothing. The calls run against one prepared world; a failing service is named in the
assertion message. All data is synthetic."""

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from core.models import AuditEvent
from distribution import services
from specimens.tests.conftest import occ_rev

from .helpers import (
    allocate,
    approved_request,
    codes,
    declaration,
    dispatch,
    fast_password_hashing,  # noqa: F401
    fresh,
    make_agreement,
    new_request,
    packed_shipment,
    pick,
    picked_request,
)

pytestmark = pytest.mark.django_db

STALE = 97


def now():
    return timezone.now()


def stale_failures(calls):
    """Names of the calls that did not raise stale_revision."""
    failures = []
    for name, call in calls.items():
        try:
            call()
        except ValidationError as exc:
            if "stale_revision" not in codes(exc):
                failures.append(f"{name}: {sorted(codes(exc))}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{name}: {type(exc).__name__} {exc}")
        else:
            failures.append(f"{name}: no error")
    return failures


def test_governance_and_consent_services_refuse_a_stale_revision(people, dist, world):
    m = people["manager"]
    consent = dist.consent
    consent.refresh_from_db()
    link = dist.study.collection_links.get()
    calls = {
        "save_distribution_catalogue": lambda: services.save_distribution_catalogue(
            actor=m,
            kind="institution",
            object_id=dist.institution.pk,
            expected_revision=STALE,
            values={"name": "Renamed"},
            reason="r",
        ),
        "save_study": lambda: services.save_study(
            actor=m,
            study_id=dist.study.pk,
            expected_revision=STALE,
            values={"title": "Renamed"},
            reason="r",
        ),
        "link_collection": lambda: services.link_collection(
            actor=m,
            study_id=dist.study.pk,
            expected_revision=STALE,
            collection_id=world.collection.pk,
            reason="r",
        ),
        "end_collection_link": lambda: services.end_collection_link(
            actor=m, link_id=link.pk, expected_revision=STALE, reason="r"
        ),
        "record_consent (re-consent without a revision)": lambda: services.record_consent(
            actor=m,
            participant_id=consent.participant_id,
            study_id=dist.study.pk,
            form_id=dist.form.pk,
            decision="refused",
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
        "record_consent (stale number)": lambda: services.record_consent(
            actor=m,
            participant_id=consent.participant_id,
            study_id=dist.study.pk,
            form_id=dist.form.pk,
            decision="refused",
            answers={},
            consented_on=timezone.localdate(),
            signer="participant",
            documentation="signed",
            source_reference="",
            restrictions="",
            expires_on=None,
            expected_revision=STALE,
            reason="r",
        ),
        "correct_consent_record": lambda: services.correct_consent_record(
            actor=m,
            record_id=consent.current_record_id,
            expected_revision=STALE,
            values={},
            answers=None,
            reason="r",
        ),
        "withdraw_consent": lambda: services.withdraw_consent(
            actor=m,
            consent_id=consent.pk,
            expected_revision=STALE,
            kind="no_use",
            effective_on=timezone.localdate(),
            received_via="Letter",
            occurred_at=now(),
            reason="r",
        ),
        "hold_withdrawn_specimens": lambda: services.hold_withdrawn_specimens(
            actor=m, consent_id=consent.pk, expected_revision=STALE, reason="r"
        ),
        "request_withdrawal_disposals": lambda: services.request_withdrawal_disposals(
            actor=m,
            consent_id=consent.pk,
            expected_revision=STALE,
            method="Autoclave",
            destination="Waste",
            reason="r",
        ),
        "reinstate_consent": lambda: services.reinstate_consent(
            actor=m,
            consent_id=consent.pk,
            expected_revision=STALE,
            record_id=None,
            occurred_at=now(),
            reason="r",
        ),
    }
    assert stale_failures(calls) == []


def test_agreement_services_refuse_a_stale_revision(people, dist, world):
    m = people["manager"]
    draft = make_agreement(people, dist, execute=False)
    executed = make_agreement(people, dist)
    calls = {
        "save_agreement (draft)": lambda: services.save_agreement(
            actor=m,
            agreement_id=draft.pk,
            expected_revision=STALE,
            values={"title": "Renamed"},
            reason="r",
        ),
        "save_agreement (executed)": lambda: services.save_agreement(
            actor=m,
            agreement_id=executed.pk,
            expected_revision=STALE,
            values={"title": "Renamed"},
            reason="r",
        ),
        "execute_agreement": lambda: services.execute_agreement(
            actor=m,
            agreement_id=draft.pk,
            expected_revision=STALE,
            executed_on=timezone.localdate(),
            effective_on=timezone.localdate(),
            expires_on=None,
            document_reference="Cabinet",
            reason="r",
        ),
        "terminate_agreement": lambda: services.terminate_agreement(
            actor=m,
            agreement_id=executed.pk,
            expected_revision=STALE,
            terminated_on=timezone.localdate(),
            reason="r",
        ),
    }
    assert stale_failures(calls) == []


def test_request_and_shipment_services_refuse_a_stale_revision(people, dist, world):
    tech, m = people["technician"], people["manager"]
    draft = new_request(tech, dist, world)
    submitted = new_request(tech, dist, world)
    services.submit_request(
        actor=tech, request_id=submitted.pk, expected_revision=submitted.revision
    )
    submitted.refresh_from_db()
    approved = approved_request(people, dist, world)
    allocation = allocate(people, approved, dist.specimens[:1])[0]
    item = approved.items.get()
    approved.refresh_from_db()
    specimen = fresh(dist.specimens[0])
    request_calls = {
        "save_request": lambda: services.save_request(
            actor=tech,
            request_id=draft.pk,
            expected_revision=STALE,
            values={"title": "Renamed"},
            items=(),
            reason="r",
        ),
        "submit_request": lambda: services.submit_request(
            actor=tech, request_id=draft.pk, expected_revision=STALE
        ),
        "decide_request": lambda: services.decide_request(
            actor=people["decider_manager"],
            request_id=submitted.pk,
            expected_revision=STALE,
            submission_number=1,
            decision="approve",
            conditions="",
            committee_reference="",
            conflicts_note="",
            reason="r",
        ),
        "reopen_request": lambda: services.reopen_request(
            actor=m, request_id=approved.pk, expected_revision=STALE, reason="r"
        ),
        "cancel_request": lambda: services.cancel_request(
            actor=m, request_id=approved.pk, expected_revision=STALE, reason="r"
        ),
        "close_request": lambda: services.close_request(
            actor=m, request_id=approved.pk, expected_revision=STALE, reason="r"
        ),
        "link_agreement": lambda: services.link_agreement(
            actor=m,
            request_id=approved.pk,
            expected_revision=STALE,
            agreement_id=None,
            reason="r",
        ),
        "allocate": lambda: services.allocate(
            actor=m,
            request_id=approved.pk,
            expected_revision=STALE,
            item_id=item.pk,
            choices=(services.Choice(dist.specimens[1].pk, fresh(dist.specimens[1]).revision),),
            below_minimum_ok=False,
            occurred_at=now(),
            reason="r",
        ),
        "release_allocations": lambda: services.release_allocations(
            actor=m,
            request_id=approved.pk,
            expected_revision=STALE,
            allocation_ids=(allocation.pk,),
            occurred_at=now(),
            reason="r",
        ),
        "pick": lambda: services.pick(
            actor=tech,
            request_id=approved.pk,
            expected_revision=STALE,
            lines=(services.PickLine(allocation.pk, specimen.revision, occ_rev(specimen)),),
            recipient_id=tech.pk,
            occurred_at=now(),
            reason="r",
        ),
        "put_back": lambda: services.put_back(
            actor=tech,
            request_id=approved.pk,
            expected_revision=STALE,
            lines=(
                services.PutBackLine(
                    allocation.pk, specimen.revision, occ_rev(specimen), world.position("C1").pk
                ),
            ),
            recipient_id=tech.pk,
            release=False,
            occurred_at=now(),
            reason="r",
        ),
        "create_shipment": lambda: services.create_shipment(
            actor=tech, request_id=approved.pk, expected_revision=STALE, values={}
        ),
    }
    assert stale_failures(request_calls) == []

    request, allocations, shipment = packed_for_shipment_calls(people, dist, world)
    shipment.refresh_from_db()
    revision_before = shipment.revision
    audit_before = AuditEvent.objects.count()
    shipment_calls = {
        "edit_shipment": lambda: services.edit_shipment(
            actor=tech,
            shipment_id=shipment.pk,
            expected_revision=STALE,
            values={"courier": "x"},
            reason="r",
        ),
        "set_shipment_contents": lambda: services.set_shipment_contents(
            actor=tech, shipment_id=shipment.pk, expected_revision=STALE, allocation_ids=()
        ),
        "pack_shipment": lambda: services.pack_shipment(
            actor=tech,
            shipment_id=shipment.pk,
            expected_revision=STALE,
            declaration=declaration(),
            checked_by_id=m.pk,
            occurred_at=now(),
            reason="r",
        ),
        "unpack_shipment": lambda: services.unpack_shipment(
            actor=tech, shipment_id=shipment.pk, expected_revision=STALE, reason="r"
        ),
        "record_notification": lambda: services.record_notification(
            actor=tech,
            shipment_id=shipment.pk,
            expected_revision=STALE,
            kind="notified",
            occurred_at=now(),
            note="n",
        ),
        "dispatch_shipment": lambda: services.dispatch_shipment(
            actor=tech,
            shipment_id=shipment.pk,
            expected_revision=STALE,
            courier="c",
            tracking="t",
            handed_to="h",
            occurred_at=now(),
            reason="r",
        ),
        "record_receipt (before dispatch)": lambda: services.record_receipt(
            actor=tech,
            shipment_id=shipment.pk,
            expected_revision=STALE,
            values=receipt(),
            reason="r",
        ),
        "cancel_shipment": lambda: services.cancel_shipment(
            actor=m, shipment_id=shipment.pk, expected_revision=STALE, reason="r"
        ),
    }
    assert stale_failures(shipment_calls) == []
    shipment.refresh_from_db()
    assert shipment.revision == revision_before and shipment.state == "packed"
    assert AuditEvent.objects.count() == audit_before  # nothing was written

    dispatch(people, shipment)
    shipment.refresh_from_db()
    revision = shipment.revision
    assert (
        stale_failures(
            {
                "record_receipt": lambda: services.record_receipt(
                    actor=tech,
                    shipment_id=shipment.pk,
                    expected_revision=STALE,
                    values=receipt(),
                    reason="r",
                )
            }
        )
        == []
    )
    receipt_row = services.record_receipt(
        actor=tech,
        shipment_id=shipment.pk,
        expected_revision=revision,
        values=receipt(),
        reason="Recipient's email",
    )
    item_row = shipment.items.order_by("line").first()
    returned = services.record_return(
        actor=tech,
        item_id=item_row.pk,
        expected_revision=fresh(item_row.specimen).revision,
        quantity=Decimal("1"),
        supplied_text="1",
        thaw_count=None,
        condition="Unopened",
        recipient_id=tech.pk,
        occurred_at=now(),
        reason="Returned",
    )
    other_item = shipment.items.order_by("line").last()
    after_calls = {
        "correct_receipt": lambda: services.correct_receipt(
            actor=m,
            receipt_id=receipt_row.pk,
            expected_revision=STALE,
            values=receipt(),
            reason="r",
        ),
        "record_return (stale specimen revision)": lambda: services.record_return(
            actor=tech,
            item_id=other_item.pk,
            expected_revision=STALE,
            quantity=Decimal("1"),
            supplied_text="1",
            thaw_count=None,
            condition="c",
            recipient_id=tech.pk,
            occurred_at=now(),
            reason="r",
        ),
        "review_return (stale specimen revision)": lambda: services.review_return(
            actor=m,
            return_id=returned.pk,
            expected_revision=STALE,
            decision="release",
            occurred_at=now(),
            reason="r",
        ),
    }
    assert stale_failures(after_calls) == []


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


def packed_for_shipment_calls(people, dist, world):
    """A second request (the first approved one holds specimen 0) with a packed shipment."""
    request = approved_request(people, dist, world)
    allocations = allocate(people, request, dist.specimens[1:3])
    pick(people, request, allocations)
    shipment = packed_shipment(people, request, allocations)
    return request, allocations, shipment


def test_a_stale_specimen_revision_is_refused_by_the_line_services(people, dist, world):
    request, allocations = picked_request(people, dist, world, count=1)
    specimen = fresh(allocations[0].specimen)
    request.refresh_from_db()
    with pytest.raises(ValidationError) as exc:
        services.put_back(
            actor=people["technician"],
            request_id=request.pk,
            expected_revision=request.revision,
            lines=(
                services.PutBackLine(
                    allocations[0].pk,
                    specimen.revision + 3,
                    occ_rev(specimen),
                    world.position("C1").pk,
                ),
            ),
            recipient_id=people["technician"].pk,
            release=False,
            occurred_at=now(),
            reason="Stale page",
        )
    assert "stale_revision" in codes(exc.value)
    allocations[0].refresh_from_db()
    assert allocations[0].state == "picked"
