"""The distributability gate: one test per blocker code, plus the refusals it produces in the
allocate, pick, pack and dispatch services. All data is synthetic."""

from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied
from django.utils import timezone

from distribution import queries, services
from distribution.models import Allocation, ConsentScopeCategory
from specimens import services as specimen_services
from specimens.models import Specimen
from specimens.tests.conftest import ask_disposal, decide, execute

from .helpers import (
    allocate,
    approved_request,
    blocked,
    category,
    collection,
    consent_for,
    declaration,
    dispatch,
    exhausted_specimen,
    fast_password_hashing,  # noqa: F401
    fresh,
    gate,
    govern,
    hold_manually,
    link,
    make_agreement,
    make_form,
    make_specimen,
    make_study,
    packed_shipment,
    participant,
    pick,
    release_manually,
    request_with,
    shipment_for,
)

pytestmark = pytest.mark.django_db


def test_a_clean_specimen_has_no_blockers_at_any_stage(people, dist, world):
    request = approved_request(people, dist, world)
    assert gate(people, request, dist.specimens[0]) == set()
    allocations = allocate(people, request, dist.specimens[:1])
    assert gate(people, request, dist.specimens[0], stage="pick") == set()
    pick(people, request, allocations)
    assert gate(people, request, dist.specimens[0], stage="pack") == set()


def test_viewer_may_read_the_gate_and_nobody_may_not(people, dist, world):
    request = approved_request(people, dist, world)
    found = queries.blockers(
        actor=people["viewer"], request_id=request.pk, specimen_ids=[dist.specimens[0].pk]
    )
    assert found[dist.specimens[0].pk] == ()
    with pytest.raises(PermissionDenied):
        queries.blockers(
            actor=people["nobody"], request_id=request.pk, specimen_ids=[dist.specimens[0].pk]
        )


def test_every_blocker_names_the_recorded_fact(people, dist, world):
    request = approved_request(people, dist, world, categories=("COMMERCIAL",))
    found = queries.blockers(
        actor=people["viewer"], request_id=request.pk, specimen_ids=[dist.specimens[0].pk]
    )
    (blocker,) = found[dist.specimens[0].pk]
    assert blocker.code == "scope_not_permitted"
    assert "P-000123" in blocker.message and "COMMERCIAL" in blocker.message
    assert "DEMO-BROAD" in blocker.message


# ------------------------------------------------------------------ not_available


@pytest.mark.parametrize("how", ["exhausted", "disposed", "distributed"])
def test_not_available_for_exhausted_disposed_and_distributed(people, dist, world, how):
    request = approved_request(people, dist, world)
    tech = people["technician"]
    if how == "exhausted":
        specimen = exhausted_specimen(people, world, "A1")
    elif how == "disposed":
        specimen = make_specimen(people, world, "A1")
        disposal = ask_disposal(tech, specimen)
        decide(people["approver"], disposal)
        execute(tech, disposal)
    else:
        _, _, shipment = shipment_for(people, dist, world, count=1)
        dispatch(people, shipment)
        specimen = dist.specimens[0]
    assert (
        fresh(specimen).status
        == {
            "exhausted": "exhausted",
            "disposed": "disposed",
            "distributed": "distributed",
        }[how]
    )
    other = approved_request(people, dist, world) if how == "distributed" else request
    assert "not_available" in gate(people, other, specimen)


def test_allocate_refuses_an_unavailable_specimen_with_the_coded_blocker(people, dist, world):
    request = approved_request(people, dist, world)
    specimen = exhausted_specimen(people, world, "A1")
    found = blocked(
        lambda: allocate(people, request, [specimen]),
    )
    assert found == {specimen.accession: ["not_available"]}
    assert not Allocation.objects.exists()


# ------------------------------------------------------------------ held


@pytest.mark.parametrize("scope", ["use", "all_use"])
def test_held_by_a_manual_use_or_all_use_hold(people, dist, world, scope):
    request = approved_request(people, dist, world)
    specimen = dist.specimens[0]
    hold_manually(people["technician"], specimen, scope)
    assert "held" in gate(people, request, specimen)
    found = blocked(lambda: allocate(people, request, [specimen]))
    assert found[specimen.accession] == ["held"]


def test_a_disposal_hold_does_not_block_distribution(people, dist, world):
    request = approved_request(people, dist, world)
    specimen = dist.specimens[0]
    hold_manually(people["technician"], specimen, "disposal")
    assert gate(people, request, specimen) == set()
    assert len(allocate(people, request, [specimen])) == 1


def test_a_released_hold_no_longer_blocks(people, dist, world):
    request = approved_request(people, dist, world)
    specimen = dist.specimens[0]
    held = hold_manually(people["technician"], specimen, "use")
    release_manually(people["manager"], held)
    assert gate(people, request, specimen) == set()


# ------------------------------------------------------------------ allocated_elsewhere


def test_allocated_elsewhere_only_at_allocate(people, dist, world):
    first = approved_request(people, dist, world)
    second = approved_request(people, dist, world)
    specimen = dist.specimens[0]
    allocate(people, first, [specimen])
    assert gate(people, second, specimen) == {"allocated_elsewhere"}
    found = blocked(lambda: allocate(people, second, [specimen]))
    assert found == {specimen.accession: ["allocated_elsewhere"]}
    assert "allocated_elsewhere" not in gate(people, second, specimen, stage="pick")
    # The allocating request itself sees its own allocation too (a duplicate is refused).
    assert gate(people, first, specimen) == {"allocated_elsewhere"}


# ------------------------------------------------------------------ no_study


def test_no_study_without_a_collection(people, dist, world):
    request = approved_request(people, dist, world)
    specimen = make_specimen(people, world, "A1", collection=None)
    assert gate(people, request, specimen) == {"no_study"}


def test_no_study_when_the_collection_is_not_linked(people, dist, world):
    request = approved_request(people, dist, world)
    specimen = make_specimen(people, world, "A1", collection=collection(people, "PILOT"))
    assert gate(people, request, specimen) == {"no_study"}
    found = blocked(lambda: allocate(people, request, [specimen]))
    assert found == {specimen.accession: ["no_study"]}


def test_no_study_when_the_link_has_ended(people, dist, world):
    request = approved_request(people, dist, world)
    spare = collection(people, "SPARE")
    link = govern(people, dist.study, spare)
    specimen = make_specimen(people, world, "A1", collection=spare)
    assert gate(people, request, specimen) == set()
    dist.study.refresh_from_db()
    services.end_collection_link(
        actor=people["manager"],
        link_id=link.pk,
        expected_revision=dist.study.revision,
        reason="Collection moved out of the study",
    )
    assert gate(people, request, specimen) == {"no_study"}


# ------------------------------------------------------------------ study_inactive


@pytest.mark.parametrize("status", ["suspended", "closed"])
def test_study_inactive(people, dist, world, status):
    request = approved_request(people, dist, world)
    dist.study.refresh_from_db()
    services.save_study(
        actor=people["manager"],
        study_id=dist.study.pk,
        expected_revision=dist.study.revision,
        values={"status": status},
        reason="Study paused by its sponsor",
    )
    assert gate(people, request, dist.specimens[0]) == {"study_inactive"}


# ------------------------------------------------------------------ outside_request


def other_study(people, world, code="OTHER-STUDY"):
    study = make_study(people, code)
    other = collection(people, f"COLL-{code}")
    govern(people, study, other)
    return study, other


def test_outside_request_when_the_request_names_other_studies(people, dist, world):
    study, _ = other_study(people, world)
    request = request_with(people, dist, world, studies=[study])
    assert gate(people, request, dist.specimens[0]) == {"outside_request"}
    named = request_with(people, dist, world, studies=[dist.study])
    assert gate(people, named, dist.specimens[0]) == set()


def test_outside_request_for_the_wrong_item_type(people, dist, world):
    request = request_with(
        people,
        dist,
        world,
        items=(services.ItemSpec(line=1, specimen_type_id=world.serum.pk, count=1),),
    )
    item = request.items.get()
    assert gate(people, request, dist.specimens[0]) == set()  # no item given: no line check
    assert gate(people, request, dist.specimens[0], item_id=item.pk) == {"outside_request"}
    found = blocked(lambda: allocate(people, request, dist.specimens[:1]))
    assert found == {dist.specimens[0].accession: ["outside_request"]}


def test_outside_request_for_the_wrong_item_study(people, dist, world):
    study, _ = other_study(people, world)
    request = request_with(
        people,
        dist,
        world,
        items=(
            services.ItemSpec(line=1, specimen_type_id=world.plasma.pk, count=1, study_id=study.pk),
        ),
    )
    item = request.items.get()
    assert gate(people, request, dist.specimens[0], item_id=item.pk) == {"outside_request"}


# ------------------------------------------------------------------ consent


def fresh_world(people, world, code="GATE-STUDY", basis="records", reference="", asked=()):
    """A study with its own collection and a form asking GENERAL plus `asked` categories."""
    study = make_study(people, code, basis=basis, reference=reference)
    coll = collection(people, f"COLL-{code}")
    govern(people, study, coll)
    general = ConsentScopeCategory.objects.get(code="GENERAL")
    form = make_form(
        people,
        study,
        [general, *[ConsentScopeCategory.objects.get(code=c) for c in asked]],
        code=f"F-{code}",
    )
    return study, coll, form


def test_no_consent_when_the_study_needs_records_and_none_exist(people, dist, world):
    request = approved_request(people, dist, world)
    study, coll, _ = fresh_world(people, world)
    specimen = make_specimen(
        people, world, "A1", collection=coll, participant=participant(people, "P-000200")
    )
    assert gate(people, request, specimen) == {"no_consent"}


def test_no_consent_without_a_participant(people, dist, world):
    request = approved_request(people, dist, world)
    specimen = make_specimen(people, world, "A1", participant=None)
    assert gate(people, request, specimen) == {"no_consent"}


@pytest.mark.parametrize("basis", ["records", "determination"])
def test_consent_refused_under_either_basis(people, dist, world, basis):
    request = approved_request(people, dist, world)
    study, coll, form = fresh_world(
        people, world, basis=basis, reference="EX-IRB-DET-0001 (synthetic)"
    )
    person = participant(people, "P-000201")
    consent_for(people, study, form, person, decision="refused")
    specimen = make_specimen(people, world, "A1", collection=coll, participant=person)
    assert gate(people, request, specimen) == {"consent_refused"}


def test_consent_withdrawn(people, dist, world):
    request = approved_request(people, dist, world)
    study, coll, form = fresh_world(people, world)
    person = participant(people, "P-000202")
    record = consent_for(people, study, form, person, answers={"GENERAL": "permitted"})
    specimen = make_specimen(people, world, "A1", collection=coll, participant=person)
    assert gate(people, request, specimen) == set()
    consent = record.consent
    consent.refresh_from_db()
    services.withdraw_consent(
        actor=people["manager"],
        consent_id=consent.pk,
        expected_revision=consent.revision,
        kind="no_use",
        effective_on=timezone.localdate(),
        received_via="Letter (synthetic)",
        occurred_at=timezone.now(),
        reason="Participant asked to stop use",
    )
    assert gate(people, request, specimen) == {"consent_withdrawn", "held"}


def test_scope_not_permitted_when_the_category_is_refused(people, dist, world):
    request = approved_request(people, dist, world, categories=("COMMERCIAL",))
    assert gate(people, request, dist.specimens[0]) == {"scope_not_permitted"}
    found = blocked(lambda: allocate(people, request, dist.specimens[:1]))
    assert found == {dist.specimens[0].accession: ["scope_not_permitted"]}


def test_scope_not_permitted_when_the_form_did_not_ask_the_category(people, dist, world):
    category(people, "EXTERNAL")
    request = approved_request(people, dist, world, categories=("EXTERNAL",))
    assert gate(people, request, dist.specimens[0]) == {"scope_not_permitted"}
    message = queries.blockers(
        actor=people["viewer"], request_id=request.pk, specimen_ids=[dist.specimens[0].pk]
    )[dist.specimens[0].pk][0].message
    assert "did not address" in message


def test_every_requested_category_must_be_permitted(people, dist, world):
    request = approved_request(people, dist, world, categories=("GENERAL", "COMMERCIAL"))
    assert gate(people, request, dist.specimens[0]) == {"scope_not_permitted"}
    both = approved_request(people, dist, world, categories=("GENERAL", "GENETIC"))
    assert gate(people, both, dist.specimens[0]) == set()


def test_determination_basis_blocks_only_explicit_refusals(people, dist, world):
    category(people, "EXTERNAL")
    study, coll, form = fresh_world(
        people,
        world,
        code="DET-STUDY",
        basis="determination",
        reference="EX-IRB-DET-0001 (synthetic)",
        asked=("COMMERCIAL",),
    )
    ids = {}
    for code, answer in (("P-000210", "refused"), ("P-000211", "permitted")):
        person = participant(people, code)
        consent_for(
            people,
            study,
            form,
            person,
            answers={"GENERAL": "permitted", "COMMERCIAL": answer},
        )
        ids[code] = make_specimen(
            people, world, "A1" if code.endswith("0") else "A2", collection=coll, participant=person
        )
    unconsented = make_specimen(
        people, world, "A3", collection=coll, participant=participant(people, "P-000212")
    )
    commercial = approved_request(people, dist, world, categories=("COMMERCIAL",))
    assert gate(people, commercial, ids["P-000210"]) == {"scope_not_permitted"}
    assert gate(people, commercial, ids["P-000211"]) == set()
    assert gate(people, commercial, unconsented) == set()  # no consent needed on a determination
    # A category the form never asked is "not addressed", which a determination does not refuse.
    external = approved_request(people, dist, world, categories=("EXTERNAL",))
    assert gate(people, external, ids["P-000211"]) == set()


def test_consent_expired(people, dist, world):
    request = approved_request(people, dist, world)
    study, coll, form = fresh_world(people, world)
    person = participant(people, "P-000203")
    today = timezone.localdate()
    consent_for(
        people,
        study,
        form,
        person,
        answers={"GENERAL": "permitted"},
        expires_on=today + timedelta(days=5),
    )
    specimen = make_specimen(people, world, "A1", collection=coll, participant=person)
    assert gate(people, request, specimen) == set()
    assert gate(people, request, specimen, on=today + timedelta(days=5)) == set()
    assert gate(people, request, specimen, on=today + timedelta(days=6)) == {"consent_expired"}


# ------------------------------------------------------------------ request_not_approved


def test_request_not_approved_after_reopen_keeps_allocations_but_refuses_pick(people, dist, world):
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
    assert {fresh(a).state for a in allocations} == {"allocated"}
    assert gate(people, request, dist.specimens[0], stage="pick") == {"request_not_approved"}
    found = blocked(lambda: pick(people, request, allocations))
    assert set(found) == {s.accession for s in dist.specimens[:2]}
    assert all(v == ["request_not_approved"] for v in found.values())
    assert {fresh(a).state for a in allocations} == {"allocated"}


def test_allocate_needs_an_approved_request(people, dist, world):
    request = approved_request(people, dist, world)
    services.reopen_request(
        actor=people["manager"],
        request_id=request.pk,
        expected_revision=request.revision,
        reason="Reopened for changes",
    )
    request.refresh_from_db()
    found = blocked(lambda: allocate(people, request, dist.specimens[:1]))
    assert found == {dist.specimens[0].accession: ["request_not_approved"]}


# ------------------------------------------------------------------ agreement (dispatch only)


def picked_and_packed(people, dist, world, agreement=True):
    request = approved_request(people, dist, world, agreement=agreement)
    allocations = allocate(people, request, dist.specimens[:1])
    pick(people, request, allocations)
    shipment = packed_shipment(people, request, allocations)
    return request, allocations, shipment


def dispatch_blockers(people, shipment):
    return blocked(lambda: dispatch(people, shipment))


def test_agreement_missing(people, dist, world):
    request, _, shipment = picked_and_packed(people, dist, world, agreement=False)
    specimen = dist.specimens[0]
    assert gate(people, request, specimen, stage="dispatch") == {"agreement_missing"}
    assert gate(people, request, specimen, stage="pack") == set()  # only checked at dispatch
    assert dispatch_blockers(people, shipment) == {specimen.accession: ["agreement_missing"]}
    shipment.refresh_from_db()
    assert shipment.state == "packed"
    assert fresh(specimen).status == "available"


def test_agreement_invalid_for_a_draft_agreement(people, dist, world):
    draft = make_agreement(people, dist, execute=False)
    request, _, shipment = picked_and_packed(people, dist, world, agreement=False)
    link(people, request, draft)
    assert dispatch_blockers(people, shipment) == {
        dist.specimens[0].accession: ["agreement_invalid"]
    }


def test_agreement_invalid_when_not_yet_effective_or_expired(people, dist, world):
    request, _, _ = picked_and_packed(people, dist, world)
    specimen = dist.specimens[0]
    today = timezone.localdate()
    agreement = dist.agreement
    assert gate(people, request, specimen, stage="dispatch", on=today) == set()
    effective = agreement.effective_on
    expires = agreement.expires_on
    on = effective - timedelta(days=1)
    assert gate(people, request, specimen, stage="dispatch", on=on) == {"agreement_invalid"}
    assert gate(people, request, specimen, stage="dispatch", on=effective) == set()
    assert gate(people, request, specimen, stage="dispatch", on=expires) == set()  # inclusive
    on = expires + timedelta(days=1)
    assert gate(people, request, specimen, stage="dispatch", on=on) == {"agreement_invalid"}


def test_agreement_invalid_for_an_agreement_that_ended_yesterday(people, dist, world):
    today = timezone.localdate()
    ended = make_agreement(people, dist, expires_days=-1)
    assert ended.expires_on == today - timedelta(days=1)
    request, _, shipment = picked_and_packed(people, dist, world, agreement=False)
    link(people, request, ended)
    assert dispatch_blockers(people, shipment) == {
        dist.specimens[0].accession: ["agreement_invalid"]
    }


def test_agreement_invalid_on_or_after_the_termination_date(people, dist, world):
    request, _, shipment = picked_and_packed(people, dist, world)
    specimen = dist.specimens[0]
    today = timezone.localdate()
    dist.agreement.refresh_from_db()
    services.terminate_agreement(
        actor=people["manager"],
        agreement_id=dist.agreement.pk,
        expected_revision=dist.agreement.revision,
        terminated_on=today,
        reason="Terminated by the institution",
    )
    assert gate(people, request, specimen, stage="dispatch", on=today - timedelta(days=1)) == set()
    assert gate(people, request, specimen, stage="dispatch", on=today) == {"agreement_invalid"}
    assert dispatch_blockers(people, shipment) == {specimen.accession: ["agreement_invalid"]}


def test_agreement_mismatch_when_categories_are_not_covered(people, dist, world):
    narrow = make_agreement(people, dist, categories=("GENETIC",))
    request, _, shipment = picked_and_packed(people, dist, world, agreement=False)
    link(people, request, narrow)
    specimen = dist.specimens[0]
    assert gate(people, request, specimen, stage="dispatch") == {"agreement_mismatch"}
    assert dispatch_blockers(people, shipment) == {specimen.accession: ["agreement_mismatch"]}
    covering = make_agreement(people, dist, categories=("GENERAL", "GENETIC"))
    link(people, request, covering)
    assert gate(people, request, specimen, stage="dispatch") == set()


def test_agreement_mismatch_for_participant_codes_at_data_level_de_identified(people, dist, world):
    # Packing refuses codes under a de-identified agreement; relinking one after packing is
    # caught again at dispatch.
    request = approved_request(people, dist, world)
    allocations = allocate(people, request, dist.specimens[:1])
    pick(people, request, allocations)
    shipment = packed_shipment(people, request, allocations, include_participant_codes=True)
    link(people, request, make_agreement(people, dist, data_level="de_identified"))
    assert dispatch_blockers(people, shipment) == {
        dist.specimens[0].accession: ["agreement_mismatch"]
    }
    link(people, request, make_agreement(people, dist, data_level="limited"))
    dispatch(people, shipment)
    item = shipment.items.get()
    assert item.participant_code == "P-000123"


# ------------------------------------------------------------------ changed_since_packing


def test_changed_since_packing_after_a_thaw_record(people, dist, world):
    request, allocations, shipment = picked_and_packed(people, dist, world)
    specimen = dist.specimens[0]
    assert gate(people, request, specimen, stage="dispatch") == set()
    specimen_services.record_thaw(
        actor=people["technician"],
        specimen_id=specimen.pk,
        expected_revision=fresh(specimen).revision,
        occurred_at=timezone.now(),
        reason="Observed a partial thaw while labelling",
    )
    assert gate(people, request, specimen, stage="dispatch") == {"changed_since_packing"}
    assert dispatch_blockers(people, shipment) == {specimen.accession: ["changed_since_packing"]}
    # Unpack, check and pack again: the new revision is the packed one.
    shipment.refresh_from_db()
    shipment = services.unpack_shipment(
        actor=people["technician"],
        shipment_id=shipment.pk,
        expected_revision=shipment.revision,
        reason="Specimen changed after packing",
    )
    shipment = services.pack_shipment(
        actor=people["technician"],
        shipment_id=shipment.pk,
        expected_revision=shipment.revision,
        declaration=declaration(),
        checked_by_id=people["manager"].pk,
        occurred_at=timezone.now(),
        reason="Packed again",
    )
    dispatch(people, shipment)
    shipment.refresh_from_db()
    assert shipment.state == "dispatched"


def test_changed_since_packing_after_a_manual_hold_placed_and_released(people, dist, world):
    request, _, shipment = picked_and_packed(people, dist, world)
    specimen = dist.specimens[0]
    held = hold_manually(people["technician"], specimen, "use")
    assert gate(people, request, specimen, stage="dispatch") == {"held", "changed_since_packing"}
    release_manually(people["manager"], held)
    assert gate(people, request, specimen, stage="dispatch") == {"changed_since_packing"}
    assert dispatch_blockers(people, shipment) == {specimen.accession: ["changed_since_packing"]}


# ------------------------------------------------------------------ pick and pack refusals


def test_pick_and_pack_refuse_what_the_gate_blocks(people, dist, world):
    request = approved_request(people, dist, world)
    allocations = allocate(people, request, dist.specimens[:2])
    hold_manually(people["technician"], dist.specimens[0], "use")
    found = blocked(lambda: pick(people, request, allocations))
    assert found == {dist.specimens[0].accession: ["held"]}
    assert {fresh(a).state for a in allocations} == {"allocated"}  # all or none
    assert not Specimen.objects.filter(open_checkout__isnull=False).exists()


def test_pack_refuses_a_specimen_whose_consent_was_withdrawn_after_picking(people, dist, world):
    request = approved_request(people, dist, world)
    allocations = allocate(people, request, dist.specimens[:1])
    pick(people, request, allocations)
    consent = dist.consent
    consent.refresh_from_db()
    services.withdraw_consent(
        actor=people["manager"],
        consent_id=consent.pk,
        expected_revision=consent.revision,
        kind="no_use",
        effective_on=timezone.localdate(),
        received_via="Letter (synthetic)",
        occurred_at=timezone.now(),
        reason="Participant asked to stop use",
    )
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
        allocation_ids=(allocations[0].pk,),
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
    assert set(found) == {dist.specimens[0].accession}
    assert set(found[dist.specimens[0].accession]) == {"consent_withdrawn", "held"}
