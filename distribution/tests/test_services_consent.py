"""Consent records, withdrawal effects, holds, reinstatement and follow-up. All data is
synthetic (coded participants, example.invalid addresses)."""

from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied
from django.utils import timezone

from distribution import queries, services
from distribution.models import (
    Allocation,
    Consent,
    ConsentAnswer,
    ConsentHold,
    ConsentRecord,
    ConsentReinstatement,
    ConsentWithdrawal,
    WithdrawalFollowUp,
)
from specimens import services as specimen_services
from specimens.models import DisposalRequest, Hold, Specimen
from specimens.tests.conftest import ask_disposal, decide

from .helpers import (
    allocate,
    approved_request,
    blocked,
    consent_for,
    csv_file,
    declaration,
    dispatch,
    exhausted_specimen,
    fast_password_hashing,  # noqa: F401
    fresh,
    gate,
    grant_disposal_approval,
    make_form,
    make_specimen,
    make_study,
    make_user,
    packed_shipment,
    participant,
    pick,
    refused,
)

pytestmark = pytest.mark.django_db

REFERENCE = "Consent withdrawn: P-000123, DEMO-STUDY"


def withdraw(people, consent, kind="no_use", actor="manager"):
    consent.refresh_from_db()
    return services.withdraw_consent(
        actor=people[actor],
        consent_id=consent.pk,
        expected_revision=consent.revision,
        kind=kind,
        effective_on=timezone.localdate(),
        received_via="Letter (synthetic)",
        occurred_at=timezone.now(),
        reason="Participant asked to stop use",
    )


def reinstate(people, consent, record=None, actor="admin"):
    consent.refresh_from_db()
    return services.reinstate_consent(
        actor=people[actor],
        consent_id=consent.pk,
        expected_revision=consent.revision,
        record_id=record.pk if record else None,
        occurred_at=timezone.now(),
        reason="Participant confirmed consent again",
    )


def hold_missing(people, consent, actor="manager"):
    consent.refresh_from_db()
    return services.hold_withdrawn_specimens(
        actor=people[actor],
        consent_id=consent.pk,
        expected_revision=consent.revision,
        reason="Registered after the withdrawal",
    )


def reconsent(people, dist, actor="manager2"):
    return consent_for(
        people,
        dist.study,
        dist.form,
        dist.consent.participant,
        answers={
            "GENERAL": "permitted",
            "GENETIC": "permitted",
            "COMMERCIAL": "refused",
            "RECONTACT": "permitted",
        },
        actor=people[actor],
    )


def use_holds(specimen, reference=REFERENCE):
    return Hold.objects.filter(
        specimen=specimen, scope="use", reference=reference, release__isnull=True
    )


# ------------------------------------------------------------------ records


def test_the_fixture_records_a_consent_with_its_answers(people, dist, world):
    consent = Consent.objects.get()
    assert consent.status == "consented" and consent.current_record.form_id == dist.form.pk
    answers = {a.category.code: a.answer for a in consent.current_record.answers.all()}
    assert answers == {
        "GENERAL": "permitted",
        "GENETIC": "permitted",
        "COMMERCIAL": "refused",
        "RECONTACT": "permitted",
    }


def test_re_consent_appends_a_record_and_the_newest_governs(people, dist, world):
    first = dist.consent.current_record
    record = consent_for(
        people,
        dist.study,
        dist.form,
        dist.consent.participant,
        answers={
            "GENERAL": "permitted",
            "GENETIC": "refused",
            "COMMERCIAL": "refused",
            "RECONTACT": "permitted",
        },
    )
    consent = record.consent
    consent.refresh_from_db()
    assert ConsentRecord.objects.filter(consent=consent).count() == 2
    assert consent.current_record_id == record.pk and consent.status == "consented"
    assert ConsentRecord.objects.get(pk=first.pk).answers.count() == 4  # the old record is kept
    request = approved_request(people, dist, world, categories=("GENETIC",))
    assert gate(people, request, dist.specimens[0]) == {"scope_not_permitted"}


def test_a_refused_decision_has_no_answers_and_withdraws_nothing(people, dist, world):
    person = participant(people, "P-000300")
    record = consent_for(people, dist.study, dist.form, person, decision="refused")
    assert (
        record.consent.status == "refused"
        and not ConsentAnswer.objects.filter(record=record).exists()
    )
    refused(
        lambda: consent_for(
            people,
            dist.study,
            dist.form,
            participant(people, "P-000301"),
            decision="refused",
            answers={"GENERAL": "permitted"},
        ),
        "invalid",
    )


def test_a_consented_record_answers_exactly_the_forms_categories(people, dist, world):
    person = participant(people, "P-000302")
    refused(
        lambda: consent_for(
            people, dist.study, dist.form, person, answers={"GENERAL": "permitted"}
        ),
        "required",
    )
    assert not Consent.objects.filter(participant=person).exists()


def test_a_form_of_another_study_is_refused(people, dist, world):
    other = make_study(people, "OTHER-STUDY")
    form = make_form(people, other, list(dist.categories.values())[:1], code="OTHER-FORM")
    refused(
        lambda: consent_for(
            people,
            dist.study,
            form,
            participant(people, "P-000303"),
            answers={"GENERAL": "permitted"},
        ),
        "invalid",
    )


def test_the_decision_date_cannot_be_in_the_future(people, dist, world):
    refused(
        lambda: consent_for(
            people,
            dist.study,
            dist.form,
            participant(people, "P-000304"),
            decision="refused",
            consented_on=timezone.localdate() + timedelta(days=1),
        ),
        "invalid_time",
    )


def test_correct_consent_record_appends_a_correction_of_the_current_record(people, dist, world):
    current = dist.consent.current_record
    consent = dist.consent
    consent.refresh_from_db()
    corrected = services.correct_consent_record(
        actor=people["manager2"],
        record_id=current.pk,
        expected_revision=consent.revision,
        values={"restrictions": "No use outside the repository's own studies"},
        answers=None,
        reason="Restriction was missed when transcribing",
    )
    consent.refresh_from_db()
    assert corrected.corrects_id == current.pk and consent.current_record_id == corrected.pk
    assert corrected.answers.count() == 4
    assert ConsentRecord.objects.get(pk=current.pk).restrictions == ""
    refused(
        lambda: services.correct_consent_record(
            actor=people["manager"],
            record_id=current.pk,  # no longer current
            expected_revision=consent.revision,
            values={"restrictions": "x"},
            answers=None,
            reason="Again",
        ),
        "invalid_transition",
    )


def test_recording_consent_while_withdrawn_keeps_the_status_withdrawn(people, dist, world):
    withdraw(people, dist.consent)
    record = reconsent(people, dist)
    consent = record.consent
    consent.refresh_from_db()
    assert consent.status == "withdrawn" and consent.current_record_id == record.pk


# ------------------------------------------------------------------ withdrawal effects


@pytest.fixture
def estate(people, dist, world):
    """Five specimens of the participant: allocated, picked, free, exhausted and distributed
    (the dist fixture's three plus two more)."""
    allocated, picked, free = dist.specimens
    shipped = make_specimen(people, world, "A1")
    exhausted = exhausted_specimen(people, world, "A2")
    request = approved_request(people, dist, world, count=3)
    rows = {a.specimen_id: a for a in allocate(people, request, [allocated, picked, shipped])}
    pick(people, request, [rows[picked.pk], rows[shipped.pk]])
    shipment = packed_shipment(people, request, [rows[shipped.pk]])
    dispatch(people, shipment)
    request.refresh_from_db()
    return {
        "request": request,
        "shipment": shipment,
        "allocated": allocated,
        "picked": picked,
        "free": free,
        "shipped": shipped,
        "exhausted": exhausted,
        "rows": rows,
    }


def test_no_use_holds_every_available_or_exhausted_specimen_and_releases_allocations(
    people, dist, world, estate
):
    withdrawal = withdraw(people, dist.consent, "no_use")
    consent = dist.consent
    consent.refresh_from_db()
    assert consent.status == "withdrawn" and consent.no_contact is True
    for key in ("allocated", "picked", "free", "exhausted"):
        holds = use_holds(estate[key])
        assert holds.count() == 1, key
        assert holds.get().actor_id == people["manager"].pk
    assert not use_holds(estate["shipped"]).exists()  # distributed material is not in custody
    assert fresh(estate["shipped"]).status == "distributed"
    assert ConsentHold.objects.filter(withdrawal=withdrawal).count() == 4
    # Allocated allocations are released with their holds; picked ones stay picked.
    rows = estate["rows"]
    assert fresh(rows[estate["allocated"].pk]).state == "released"
    assert Hold.objects.get(pk=rows[estate["allocated"].pk].hold_id).release is not None
    assert fresh(rows[estate["picked"].pk]).state == "picked"
    assert fresh(rows[estate["shipped"].pk]).state == "shipped"
    # The counts are a snapshot stored on the append-only row.
    withdrawal.refresh_from_db()
    assert (withdrawal.held_count, withdrawal.released_count, withdrawal.distributed_count) == (
        4,
        1,
        1,
    )
    stored = ConsentWithdrawal.objects.get(pk=withdrawal.pk)
    assert stored.held_count == 4 and stored.kind == "no_use"
    assert not Specimen.objects.exclude(status__in=("available", "exhausted", "distributed"))


def test_a_picked_specimen_of_a_withdrawn_consent_cannot_be_packed_or_dispatched(
    people, dist, world, estate
):
    withdraw(people, dist.consent)
    request = estate["request"]
    request.refresh_from_db()
    shipment = services.create_shipment(
        actor=people["technician"],
        request_id=request.pk,
        expected_revision=request.revision,
        values={},
    )
    shipment = services.set_shipment_contents(
        actor=people["technician"],
        shipment_id=shipment.pk,
        expected_revision=shipment.revision,
        allocation_ids=(estate["rows"][estate["picked"].pk].pk,),
    )
    found = blocked(
        lambda: services.pack_shipment(
            actor=people["technician"],
            shipment_id=shipment.pk,
            expected_revision=shipment.revision,
            declaration=declaration(),
            checked_by_id=people["manager"].pk,
            occurred_at=timezone.now(),
            reason="Packed",
        )
    )
    assert set(found[estate["picked"].accession]) == {"consent_withdrawn", "held"}
    # Staff put it back; the allocation is released with the manager's put-back.
    assert gate(people, request, estate["picked"], stage="pick") >= {"held"}


def test_a_withdrawal_after_packing_blocks_dispatch(people, dist, world):
    request = approved_request(people, dist, world)
    rows = allocate(people, request, dist.specimens[:1])
    pick(people, request, rows)
    shipment = packed_shipment(people, request, rows)
    withdraw(people, dist.consent)
    found = blocked(lambda: dispatch(people, shipment))
    assert set(found[dist.specimens[0].accession]) == {
        "consent_withdrawn",
        "held",
        "changed_since_packing",
    }
    shipment.refresh_from_db()
    assert shipment.state == "packed" and fresh(dist.specimens[0]).status == "available"


def test_the_withdrawal_names_the_participant_and_study_in_the_hold_reference(
    people, dist, world, estate
):
    withdraw(people, dist.consent)
    hold = use_holds(estate["free"]).get()
    assert hold.reference == "Consent withdrawn: P-000123, DEMO-STUDY"
    # A use hold carries its reference to the generic release, which refuses it.
    refused(
        lambda: specimen_services.release_hold(
            actor=people["manager"],
            hold_id=hold.pk,
            expected_revision=fresh(estate["free"]).revision,
            occurred_at=timezone.now(),
            reason="Try the generic release",
        ),
        "invalid_transition",
    )


def test_no_contact_only_sets_the_flag(people, dist, world, estate):
    holds_before = Hold.objects.filter(release__isnull=True).count()
    withdrawal = withdraw(people, dist.consent, "no_contact")
    consent = dist.consent
    consent.refresh_from_db()
    assert consent.status == "consented" and consent.no_contact is True
    assert (withdrawal.held_count, withdrawal.released_count) == (0, 0)
    assert Hold.objects.filter(release__isnull=True).count() == holds_before
    assert not ConsentHold.objects.exists()
    assert fresh(estate["rows"][estate["allocated"].pk]).state == "allocated"
    request = approved_request(people, dist, world)
    assert gate(people, request, estate["free"]) == set()  # still distributable


def test_withdrawing_twice_is_refused(people, dist, world, estate):
    withdraw(people, dist.consent, "no_use")
    refused(lambda: withdraw(people, dist.consent, "destroy"), "invalid_transition")
    refused(lambda: withdraw(people, dist.consent, "no_use"), "invalid_transition")
    assert ConsentWithdrawal.objects.count() == 1
    assert use_holds(estate["free"]).count() == 1


def test_a_no_contact_withdrawal_can_be_followed_by_a_stop_of_use(people, dist, world, estate):
    withdraw(people, dist.consent, "no_contact")
    withdraw(people, dist.consent, "no_use")
    assert dist.consent.__class__.objects.get(pk=dist.consent.pk).status == "withdrawn"
    assert ConsentWithdrawal.objects.count() == 2


def test_a_failure_part_way_rolls_the_whole_withdrawal_back(
    people, dist, world, estate, monkeypatch
):
    real = specimen_services.place_linked_hold
    calls = []

    def flaky(**kwargs):
        calls.append(kwargs["specimen_id"])
        if len(calls) == 2:
            raise RuntimeError("storage fault")
        return real(**kwargs)

    monkeypatch.setattr(specimen_services, "place_linked_hold", flaky)
    consent = dist.consent
    consent.refresh_from_db()
    revision = consent.revision
    holds_before = Hold.objects.count()
    with pytest.raises(RuntimeError):
        withdraw(people, consent)
    assert len(calls) == 2
    consent.refresh_from_db()
    assert consent.status == "consented" and consent.revision == revision
    assert consent.no_contact is False
    assert not ConsentWithdrawal.objects.exists() and not ConsentHold.objects.exists()
    assert Hold.objects.count() == holds_before
    assert not Hold.objects.filter(reference=REFERENCE).exists()
    assert fresh(estate["rows"][estate["allocated"].pk]).state == "allocated"


def test_a_withdrawal_needs_a_reason_a_channel_and_a_real_date(people, dist, world):
    consent = dist.consent
    consent.refresh_from_db()

    def call(**overrides):
        values = {
            "actor": people["manager"],
            "consent_id": consent.pk,
            "expected_revision": consent.revision,
            "kind": "no_use",
            "effective_on": timezone.localdate(),
            "received_via": "Letter (synthetic)",
            "occurred_at": timezone.now(),
            "reason": "Participant asked to stop use",
        }
        values.update(overrides)
        return services.withdraw_consent(**values)

    refused(lambda: call(reason=" "), "required")
    refused(lambda: call(received_via=""), "required")
    refused(lambda: call(kind="forget"), "invalid")
    refused(lambda: call(effective_on=timezone.localdate() + timedelta(days=1)), "invalid_time")
    refused(lambda: call(occurred_at=timezone.now() + timedelta(hours=1)), "invalid_time")
    assert not ConsentWithdrawal.objects.exists()


# ------------------------------------------------------------------ holding late specimens


def test_hold_withdrawn_specimens_holds_later_registrations_and_is_idempotent(
    people, dist, world, estate
):
    withdraw(people, dist.consent)
    later = make_specimen(people, world, "B1")
    assert gate(people, estate["request"], later) >= {"consent_withdrawn"}
    assert not use_holds(later).exists()
    rows = hold_missing(people, dist.consent)
    assert len(rows) == 1 and rows[0].specimen_id == later.pk
    assert use_holds(later).count() == 1
    assert hold_missing(people, dist.consent) == ()
    assert use_holds(later).count() == 1
    assert use_holds(estate["free"]).count() == 1  # earlier holds are not doubled
    assert "held" in gate(people, estate["request"], later)


def test_hold_withdrawn_specimens_needs_an_open_withdrawal(people, dist, world):
    consent = dist.consent
    consent.refresh_from_db()
    refused(lambda: hold_missing(people, consent), "invalid_transition")


def test_the_consent_impact_lists_unheld_specimens_in_custody(people, dist, world, estate):
    withdraw(people, dist.consent)
    later = make_specimen(people, world, "B1")
    impact = queries.consent_impact(actor=people["viewer"], consent_id=dist.consent.pk)
    assert {s.pk for s in impact.unheld} == {later.pk}
    assert {s.pk for s in impact.held} == {
        estate[k].pk for k in ("allocated", "picked", "free", "exhausted")
    }
    assert [i.specimen_id for i in impact.distributed] == [estate["shipped"].pk]
    assert [a.specimen_id for a in impact.open_allocations] == [estate["picked"].pk]
    hold_missing(people, dist.consent)
    impact = queries.consent_impact(actor=people["viewer"], consent_id=dist.consent.pk)
    assert impact.unheld == ()


# ------------------------------------------------------------------ reinstatement


def test_reinstate_releases_the_holds_and_restores_the_status(people, dist, world, estate):
    withdraw(people, dist.consent)  # by the manager
    record = reconsent(people, dist, actor="manager2")
    row = reinstate(people, dist.consent, record, actor="admin")
    consent = dist.consent
    consent.refresh_from_db()
    assert consent.status == "consented" and row.record_id == record.pk
    for key in ("allocated", "picked", "free", "exhausted"):
        assert not use_holds(estate[key]).exists(), key
    assert Hold.objects.filter(reference=REFERENCE, release__isnull=False).count() == 4
    request = approved_request(people, dist, world)
    assert gate(people, request, estate["free"]) == set()
    refused(lambda: reinstate(people, consent, record), "invalid_transition")  # no longer withdrawn


def test_reinstate_without_a_new_record_corrects_a_withdrawal_recorded_in_error(
    people, dist, world, estate
):
    withdraw(people, dist.consent)
    row = reinstate(people, dist.consent, None, actor="manager2")
    consent = dist.consent
    consent.refresh_from_db()
    assert row.record_id is None and consent.status == "consented"
    assert not use_holds(estate["free"]).exists()
    # A new withdrawal can follow a reinstated one.
    withdraw(people, dist.consent)
    assert use_holds(estate["free"]).count() == 1


def test_the_withdrawals_recorder_cannot_reinstate_even_as_a_superuser(people, dist, world, estate):
    withdraw(people, dist.consent, actor="manager")
    refused(lambda: reinstate(people, dist.consent, None, actor="manager"), "self_approval")
    withdraw_row = ConsentWithdrawal.objects.get()
    assert not ConsentReinstatement.objects.exists()
    # The superuser (admin role) recorded the withdrawal of another consent: refused too.
    other = consent_for(
        people,
        dist.study,
        dist.form,
        participant(people, "P-000400"),
        answers={
            "GENERAL": "permitted",
            "GENETIC": "permitted",
            "COMMERCIAL": "permitted",
            "RECONTACT": "permitted",
        },
    ).consent
    withdraw(people, other, actor="superuser")
    refused(lambda: reinstate(people, other, None, actor="superuser"), "self_approval")
    assert withdraw_row.consent_id == dist.consent.pk


def test_the_recorder_of_the_new_consent_cannot_reinstate_even_as_a_superuser(
    people, dist, world, estate
):
    withdraw(people, dist.consent, actor="manager")
    record = reconsent(people, dist, actor="manager2")
    refused(lambda: reinstate(people, dist.consent, record, actor="manager2"), "self_approval")
    fresh_record = reconsent(people, dist, actor="superuser")
    refused(
        lambda: reinstate(people, dist.consent, fresh_record, actor="superuser"), "self_approval"
    )
    assert not ConsentReinstatement.objects.exists()
    assert reinstate(people, dist.consent, fresh_record, actor="admin").record_id == fresh_record.pk


def test_reinstatement_needs_the_record_made_after_the_withdrawal(people, dist, world, estate):
    old = dist.consent.current_record
    withdraw(people, dist.consent)
    refused(lambda: reinstate(people, dist.consent, old, actor="admin"), "invalid")


def test_reinstatement_is_for_managers_only(people, dist, world, estate):
    withdraw(people, dist.consent)
    for actor in ("technician", "decider", "viewer", "nobody"):
        with pytest.raises(PermissionDenied):
            reinstate(people, dist.consent, None, actor=actor)


# ------------------------------------------------------------------ disposal after withdrawal


def test_a_use_hold_does_not_stop_a_disposal_request(people, dist, world, estate):
    withdraw(people, dist.consent, "destroy")
    request = ask_disposal(people["technician"], estate["free"])
    assert request.specimen_id == estate["free"].pk


def test_withdrawal_disposals_file_ordinary_requests_approved_by_someone_else(
    people, dist, world, estate
):
    withdraw(people, dist.consent, "destroy")
    requester = grant_disposal_approval(make_user("dist-requester", "manager"))
    people["requester"] = requester
    consent = dist.consent
    consent.refresh_from_db()
    filed = services.request_withdrawal_disposals(
        actor=requester,
        consent_id=consent.pk,
        expected_revision=consent.revision,
        method="Autoclave",
        destination="Biohazard waste",
        reason="Participant asked for destruction",
    )
    # The picked specimen still has its allocation's disposal hold, so it is skipped; the
    # allocated one was released and is requested like the free and exhausted ones.
    assert {r.specimen_id for r in filed} == {
        estate[k].pk for k in ("allocated", "free", "exhausted")
    }
    assert not DisposalRequest.objects.filter(specimen=estate["picked"]).exists()
    assert all(isinstance(r, DisposalRequest) for r in filed)
    first = filed[0]
    refused(
        lambda: specimen_services.decide_disposal(
            actor=requester,
            request_id=first.pk,
            expected_revision=fresh(first.specimen).revision,
            decision="approve",
            reason="Approving my own request",
        ),
        "self_approval",
    )
    decision = decide(people["approver"], first)
    assert decision.decision == "approve"
    # Filing again skips specimens with an open request.
    consent.refresh_from_db()
    again = services.request_withdrawal_disposals(
        actor=requester,
        consent_id=consent.pk,
        expected_revision=consent.revision,
        method="Autoclave",
        destination="Biohazard waste",
        reason="Participant asked for destruction",
    )
    assert again == ()


def test_withdrawal_disposals_are_offered_only_for_destroy(people, dist, world, estate):
    withdraw(people, dist.consent, "no_use")
    consent = dist.consent
    consent.refresh_from_db()
    refused(
        lambda: services.request_withdrawal_disposals(
            actor=people["manager"],
            consent_id=consent.pk,
            expected_revision=consent.revision,
            method="Autoclave",
            destination="Waste",
            reason="Not asked for",
        ),
        "invalid_transition",
    )


# ------------------------------------------------------------------ follow-up


def test_follow_up_is_recorded_for_shipments_that_held_the_participants_material(
    people, dist, world, estate
):
    row = withdraw(people, dist.consent, "destroy")
    shipment = estate["shipment"]
    followed = services.record_follow_up(
        actor=people["manager"],
        withdrawal_id=row.pk,
        shipment_id=shipment.pk,
        action="recipient_notified",
        note="Recipient told by email (synthetic)",
        occurred_at=timezone.now(),
        upload=csv_file("notice,sent\n", "notice.csv"),
    )
    assert isinstance(followed, WithdrawalFollowUp) and followed.attachment_id
    assert followed.shipment_id == shipment.pk
    refused(
        lambda: services.record_follow_up(
            actor=people["manager"],
            withdrawal_id=row.pk,
            shipment_id=shipment.pk,
            action="telepathy",
            note="x",
            occurred_at=timezone.now(),
        ),
        "invalid",
    )
    refused(
        lambda: services.record_follow_up(
            actor=people["manager"],
            withdrawal_id=row.pk,
            shipment_id=shipment.pk,
            action="note",
            note=" ",
            occurred_at=timezone.now(),
        ),
        "required",
    )


def test_follow_up_for_another_participants_shipment_is_refused(people, dist, world, estate):
    row = withdraw(people, dist.consent, "destroy")
    other_request = approved_request(people, dist, world)
    other_person = participant(people, "P-000500")
    consent_for(
        people,
        dist.study,
        dist.form,
        other_person,
        answers={
            "GENERAL": "permitted",
            "GENETIC": "permitted",
            "COMMERCIAL": "permitted",
            "RECONTACT": "permitted",
        },
    )
    stranger = make_specimen(people, world, "C1", participant=other_person)
    rows = allocate(people, other_request, [stranger])
    pick(people, other_request, rows)
    shipment = packed_shipment(people, other_request, rows)
    dispatch(people, shipment)
    refused(
        lambda: services.record_follow_up(
            actor=people["manager"],
            withdrawal_id=row.pk,
            shipment_id=shipment.pk,
            action="note",
            note="Wrong shipment",
            occurred_at=timezone.now(),
        ),
        "invalid",
    )
    assert not WithdrawalFollowUp.objects.exists()


def test_allocation_rows_are_never_deleted_by_a_withdrawal(people, dist, world, estate):
    before = Allocation.objects.count()
    withdraw(people, dist.consent)
    assert Allocation.objects.count() == before
