"""Regression tests for request, consent, agreement and shipment rules. Synthetic data only."""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from distribution import documents, queries, services
from distribution.models import (
    Allocation,
    Consent,
    ConsentReinstatement,
    Shipment,
    SpecimenRequest,
    StudyCollection,
)
from distribution.tests.conftest import (
    allocate,
    approved_request,
    declaration,
    dispatch,
    fresh,
    occ_rev,
    packed_shipment,
    pick,
)
from distribution.tests.helpers import (
    active_holds,
    blocked,
    consent_for,
    gate,
    link,
    make_agreement,
    make_specimen,
    make_study,
    refused,
)
from specimens import services as specimen_services
from specimens.models import CustodyEvent, Hold

pytestmark = pytest.mark.django_db

ALL = {
    "GENERAL": "permitted",
    "GENETIC": "permitted",
    "COMMERCIAL": "permitted",
    "RECONTACT": "permitted",
}


def _withdraw(people, consent, actor="manager"):
    consent.refresh_from_db()
    return services.withdraw_consent(
        actor=people[actor],
        consent_id=consent.pk,
        expected_revision=consent.revision,
        kind="no_use",
        effective_on=timezone.localdate(),
        received_via="Letter (synthetic)",
        occurred_at=timezone.now(),
        reason="Participant letter (synthetic)",
    )


def _relink(people, dist, world, basis="determination"):
    """End DEMO's link to DEMO-STUDY and link it to a new study."""
    link_row = StudyCollection.objects.get(collection=world.collection, ended_at__isnull=True)
    dist.study.refresh_from_db()
    services.end_collection_link(
        actor=people["manager"],
        link_id=link_row.pk,
        expected_revision=dist.study.revision,
        reason="Moved to another study",
    )
    other = make_study(people, "STUDY-B", basis=basis, reference="Committee letter 7 (synthetic)")
    services.link_collection(
        actor=people["manager"],
        study_id=other.pk,
        expected_revision=other.revision,
        collection_id=world.collection.pk,
        reason="Now governed under STUDY-B",
    )
    return other


# ---- rejecting a resubmitted request releases its reservations


def test_rejecting_a_resubmitted_request_releases_what_it_reserved(people, dist, world):
    req = approved_request(people, dist, world)
    rows = allocate(people, req, dist.specimens[:2])
    draft = services.create_shipment(
        actor=people["technician"], request_id=req.pk, expected_revision=req.revision, values={}
    )
    req.refresh_from_db()
    services.reopen_request(
        actor=people["manager"],
        request_id=req.pk,
        expected_revision=req.revision,
        reason="Purpose changes",
    )
    req.refresh_from_db()
    services.submit_request(
        actor=people["technician"], request_id=req.pk, expected_revision=req.revision
    )
    req.refresh_from_db()
    services.decide_request(
        actor=people["decider_manager"],
        request_id=req.pk,
        expected_revision=req.revision,
        submission_number=req.latest_submission,
        decision="reject",
        conditions="",
        committee_reference="Minute 2 (synthetic)",
        conflicts_note="",
        reason="Out of scope",
    )
    req.refresh_from_db()
    assert req.status == SpecimenRequest.Status.REJECTED
    assert set(
        Allocation.objects.filter(pk__in=[r.pk for r in rows]).values_list("state", flat=True)
    ) == {"released"}
    assert not Hold.objects.filter(pk__in=[r.hold_id for r in rows], release__isnull=True).exists()
    assert Shipment.objects.get(pk=draft.pk).state == Shipment.State.CANCELLED
    other = approved_request(people, dist, world)
    assert "allocated_elsewhere" not in gate(people, other, dist.specimens[0])


# ---- consent decisions survive a collection being relinked


def test_a_refusal_keeps_blocking_after_the_collection_is_relinked(people, dist, world):
    req = approved_request(people, dist, world)
    consent_for(people, dist.study, dist.form, world.participant, decision="refused", answers={})
    specimen = dist.specimens[0]
    assert "consent_refused" in gate(people, req, specimen)
    _relink(people, dist, world)
    assert "consent_refused" in gate(people, req, specimen)
    refused(lambda: allocate(people, req, [specimen]), "blocked")


def test_a_withdrawal_follows_material_registered_after_a_relink(people, dist, world):
    req = approved_request(people, dist, world)
    withdrawal = _withdraw(people, dist.consent)
    assert withdrawal.held_count == len(dist.specimens)
    _relink(people, dist, world)
    later = make_specimen(people, world, "A1")
    assert later.collection_id == world.collection.pk
    assert later.participant_id == world.participant.pk
    assert "consent_withdrawn" in gate(people, req, later)

    impact = queries.consent_impact(actor=people["viewer"], consent_id=dist.consent.pk)
    assert later.pk in {s.pk for s in impact.unheld}
    consent = Consent.objects.get(pk=dist.consent.pk)
    services.hold_withdrawn_specimens(
        actor=people["manager2"],
        consent_id=consent.pk,
        expected_revision=consent.revision,
        reason="Material registered since the withdrawal",
    )
    assert any(h.scope == "use" for h in active_holds(fresh(later)))


# ---- dispatch and step times are checked against the agreement and the previous step


def test_dispatch_is_judged_on_the_recording_day_as_well(people, dist, world, monkeypatch):
    req = approved_request(people, dist, world)
    rows = allocate(people, req, dist.specimens[:1])
    pick(people, req, rows)
    shipment = packed_shipment(people, req, rows)
    agreement = dist.agreement
    agreement.refresh_from_db()
    tomorrow = timezone.localdate() + timedelta(days=1)
    services.terminate_agreement(
        actor=people["manager"],
        agreement_id=agreement.pk,
        expected_revision=agreement.revision,
        terminated_on=tomorrow,
        reason="Ends tomorrow",
    )
    # The dispatch is recorded "tomorrow" (agreement ended) for a time today (still valid).
    real = timezone.localdate

    def recorded_tomorrow(value=None, timezone=None):
        return real(value, timezone) if value is not None else tomorrow

    monkeypatch.setattr("django.utils.timezone.localdate", recorded_tomorrow)
    found = blocked(lambda: dispatch(people, shipment))
    assert set().union(*map(set, found.values())) == {"agreement_invalid"}


def test_allocation_pick_and_pack_cannot_be_backdated_before_the_previous_step(people, dist, world):
    req = approved_request(people, dist, world)
    earlier = timezone.now() - timedelta(hours=1)
    refused(lambda: allocate(people, req, dist.specimens[:1], occurred_at=earlier), "invalid_time")
    rows = allocate(people, req, dist.specimens[:1])
    req.refresh_from_db()
    s = fresh(dist.specimens[0])
    refused(
        lambda: services.pick(
            actor=people["technician"],
            request_id=req.pk,
            expected_revision=req.revision,
            lines=(services.PickLine(rows[0].pk, s.revision, occ_rev(s)),),
            recipient_id=people["technician"].pk,
            occurred_at=rows[0].allocated_at - timedelta(minutes=1),
            reason="Retrieved",
        ),
        "invalid_time",
    )
    pick(people, req, rows)
    req.refresh_from_db()
    shipment = services.create_shipment(
        actor=people["technician"], request_id=req.pk, expected_revision=req.revision, values={}
    )
    shipment = services.set_shipment_contents(
        actor=people["technician"],
        shipment_id=shipment.pk,
        expected_revision=shipment.revision,
        allocation_ids=(rows[0].pk,),
    )
    refused(
        lambda: services.pack_shipment(
            actor=people["technician"],
            shipment_id=shipment.pk,
            expected_revision=shipment.revision,
            declaration=declaration(),
            checked_by_id=people["manager"].pk,
            occurred_at=rows[0].allocated_at - timedelta(minutes=1),
            reason="Packed",
        ),
        "invalid_time",
    )


# ---- put-back of a specimen already checked in elsewhere


def _check_in_from_the_specimen_page(people, world, specimen, label="C3"):
    s = fresh(specimen)
    specimen_services.check_in(
        actor=people["technician"],
        specimen_id=s.pk,
        expected_revision=s.revision,
        expected_occupancy_revision=occ_rev(s),
        position_id=world.position(label).pk,
        recipient_id=people["technician"].pk,
        thaw_observed=False,
        occurred_at=timezone.now(),
        reason="Put away from the specimen page",
    )
    return fresh(s)


def test_a_picked_specimen_checked_in_elsewhere_is_refused_at_pack_and_reconciled(
    people, dist, world
):
    req = approved_request(people, dist, world)
    rows = allocate(people, req, dist.specimens[:1])
    pick(people, req, rows)
    s = _check_in_from_the_specimen_page(people, world, dist.specimens[0])
    assert services.out_of_pick_custody([Allocation.objects.get(pk=rows[0].pk)]) == {rows[0].pk}
    assert "not_in_pick_custody" in gate(people, req, s, stage="pack")
    found = blocked(lambda: packed_shipment(people, req, rows))
    assert "not_in_pick_custody" in set().union(*map(set, found.values()))

    check_ins = CustodyEvent.objects.filter(specimen=s, kind="check_in").count()
    position = s.occupancy.position_id
    req.refresh_from_db()
    services.put_back(
        actor=people["technician"],
        request_id=req.pk,
        expected_revision=req.revision,
        lines=(services.PutBackLine(rows[0].pk, s.revision, occ_rev(s), None),),
        recipient_id=people["technician"].pk,
        release=False,
        occurred_at=timezone.now(),
        reason="Found back in storage",
    )
    allocation = Allocation.objects.get(pk=rows[0].pk)
    assert allocation.state == Allocation.State.ALLOCATED and allocation.shipment_id is None
    s = fresh(s)
    assert CustodyEvent.objects.filter(specimen=s, kind="check_in").count() == check_ins
    assert s.occupancy.position_id == position
    event = allocation.events.order_by("-recorded_at").first()
    assert event.kind == "put_back" and event.custody_event_id is not None
    assert event.reason.startswith("Already checked in from the specimen record")
    req.refresh_from_db()
    services.cancel_request(
        actor=people["manager"],
        request_id=req.pk,
        expected_revision=req.revision,
        reason="No longer needed",
    )


def test_put_back_still_needs_a_position_for_a_specimen_in_hand(people, dist, world):
    req = approved_request(people, dist, world)
    rows = allocate(people, req, dist.specimens[:1])
    pick(people, req, rows)
    s = fresh(dist.specimens[0])
    refused(
        lambda: services.put_back(
            actor=people["technician"],
            request_id=req.pk,
            expected_revision=req.revision,
            lines=(services.PutBackLine(rows[0].pk, s.revision, occ_rev(s), None),),
            recipient_id=people["technician"].pk,
            release=False,
            occurred_at=timezone.now(),
            reason="Back",
        ),
        "required",
    )


def test_put_back_page_records_a_specimen_already_in_storage(people, dist, world, client):
    req = approved_request(people, dist, world)
    rows = allocate(people, req, dist.specimens[:1])
    pick(people, req, rows)
    s = _check_in_from_the_specimen_page(people, world, dist.specimens[0])
    client.force_login(people["technician"])
    url = reverse("distribution:put-back", args=[req.pk])
    page = client.get(url).content.decode()
    assert "Already in storage" in page
    req.refresh_from_db()
    response = client.post(
        url,
        {
            "lines-TOTAL_FORMS": "1",
            "lines-INITIAL_FORMS": "1",
            "lines-MIN_NUM_FORMS": "0",
            "lines-MAX_NUM_FORMS": "200",
            "lines-0-include": "on",
            "lines-0-allocation": str(rows[0].pk),
            "lines-0-revision": str(s.revision),
            "lines-0-occupancy_revision": str(occ_rev(s)),
            "lines-0-in_storage": "True",
            "lines-0-thaw_observed": "unknown",
            "recipient": str(people["technician"].pk),
            "occurred_at": timezone.localtime().strftime("%Y-%m-%dT%H:%M:%S"),
            "reason": "Found back in storage",
            "expected_revision": str(req.revision),
        },
    )
    assert response.status_code in (200, 302), response.content.decode()[:500]
    assert Allocation.objects.get(pk=rows[0].pk).state == Allocation.State.ALLOCATED


# ---- participant codes follow the agreement data level


def test_pack_refuses_participant_codes_the_agreement_does_not_allow(people, dist, world):
    agreement = make_agreement(people, dist, data_level="de_identified")
    req = approved_request(people, dist, world, agreement=False)
    link(people, req, agreement, reason="De-identified MTA")
    rows = allocate(people, req, dist.specimens[:1])
    pick(people, req, rows)
    error = refused(
        lambda: packed_shipment(people, req, rows, include_participant_codes=True), "blocked"
    )
    assert "include_participant_codes" in error.message_dict
    assert Shipment.objects.get().state == Shipment.State.DRAFT


def test_planned_manifest_drops_codes_when_the_agreement_changes_after_packing(people, dist, world):
    req = approved_request(people, dist, world)
    rows = allocate(people, req, dist.specimens[:1])
    pick(people, req, rows)
    shipment = packed_shipment(people, req, rows, include_participant_codes=True)
    context = documents.manifest_context(actor=people["viewer"], shipment_id=shipment.pk)
    assert context["show_participants"]
    assert context["lines"][0].participant_code == world.participant.code
    req.refresh_from_db()
    link(people, req, make_agreement(people, dist, data_level="de_identified"), reason="New MTA")
    context = documents.manifest_context(actor=people["viewer"], shipment_id=shipment.pk)
    assert not context["show_participants"]
    assert context["lines"][0].participant_code == ""


# ---- reinstating consent: who may do it and which record it names


def test_reinstating_after_a_new_record_names_it_and_its_recorder_cannot(people, dist, world):
    consent = dist.consent
    _withdraw(people, consent)  # manager
    new = consent_for(
        people, dist.study, dist.form, world.participant, actor=people["manager2"], answers=ALL
    )
    consent.refresh_from_db()

    def reinstate(actor, record_id):
        consent.refresh_from_db()
        return services.reinstate_consent(
            actor=people[actor],
            consent_id=consent.pk,
            expected_revision=consent.revision,
            record_id=record_id,
            occurred_at=timezone.now(),
            reason="Re-consented",
        )

    refused(lambda: reinstate("manager2", None), "self_approval")
    refused(lambda: reinstate("manager2", new.pk), "self_approval")
    error = refused(lambda: reinstate("decider_manager", None), "invalid")
    assert "record" in error.message_dict
    row = reinstate("decider_manager", new.pk)
    assert ConsentReinstatement.objects.get(pk=row.pk).record_id == new.pk
    consent.refresh_from_db()
    assert consent.status == Consent.Status.CONSENTED


def test_recorded_in_error_takes_the_status_from_the_record_before_the_withdrawal(
    people, dist, world, client
):
    consent = dist.consent
    _withdraw(people, consent)
    client.force_login(people["manager2"])
    page = client.get(reverse("distribution:consent-reinstate", args=[consent.pk]))
    assert ">None: the withdrawal was recorded in error</option>" in page.content.decode()
    consent.refresh_from_db()
    row = services.reinstate_consent(
        actor=people["manager2"],
        consent_id=consent.pk,
        expected_revision=consent.revision,
        record_id=None,
        occurred_at=timezone.now(),
        reason="Recorded against the wrong participant",
    )
    assert row.record_id is None
    consent.refresh_from_db()
    assert consent.status == Consent.Status.CONSENTED


def test_the_reinstate_form_offers_no_error_option_after_a_new_record(people, dist, world, client):
    _withdraw(people, dist.consent)
    consent_for(
        people, dist.study, dist.form, world.participant, actor=people["manager2"], answers=ALL
    )
    client.force_login(people["decider_manager"])
    page = client.get(reverse("distribution:consent-reinstate", args=[dist.consent.pk]))
    assert ">None: the withdrawal was recorded in error</option>" not in page.content.decode()
