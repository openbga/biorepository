"""Registration and catalogues: opaque accessions, exact amounts, one transaction, role gates."""

import dataclasses
import re
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from core.models import AuditEvent
from specimens import services
from specimens.models import CustodyEvent, QuantityEntry, Specimen, ThawEvent
from storage.models import Occupancy

from .conftest import ago, catalogue, codes, fresh, register, registration

pytestmark = pytest.mark.django_db


def register_data(actor, data, occurred_at=None):
    return services.register_specimen(
        actor=actor, data=data, occurred_at=occurred_at or ago(270), reason="Received"
    )


def refused(world, actor, code, **changes):
    data = dataclasses.replace(registration(world), **changes)
    before = (Specimen.objects.count(), Occupancy.objects.count(), AuditEvent.objects.count())
    with pytest.raises(ValidationError) as exc:
        register_data(actor, data)
    assert code in codes(exc.value)
    # Nothing was created, not even the storage key or an audit event.
    assert before == (
        Specimen.objects.count(),
        Occupancy.objects.count(),
        AuditEvent.objects.count(),
    )


# --- accession ---------------------------------------------------------------------------------


def test_accessions_come_from_the_sequence_and_carry_no_meaning(people, world):
    first = register(people["technician"], world)
    second = register(people["technician"], world, quantity="1")
    assert re.fullmatch(r"VP-\d{6,}", first.accession)
    assert re.fullmatch(r"VP-\d{6,}", second.accession)
    assert int(second.accession[3:]) > int(first.accession[3:])
    # No participant, type, collection or location text in the identifier.
    for text in (world.participant.code, world.plasma.code, world.collection.code, "A1"):
        assert text not in first.accession
    assert first.accession != second.accession


# --- amounts and times -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "quantity",
    [0.5, Decimal("1.1234567"), Decimal("NaN"), Decimal("Infinity"), Decimal("0"), Decimal("-1")],
    ids=["float", "seven-places", "nan", "infinity", "zero", "negative"],
)
def test_inexact_or_invalid_quantities_are_refused_not_rounded(people, world, quantity):
    refused(world, people["technician"], "invalid", quantity=quantity)


def test_a_count_unit_refuses_fractions(people, world):
    refused(world, people["technician"], "invalid", quantity=Decimal("1.5"), unit_id=world.count.pk)
    specimen = register(people["technician"], world, quantity="3", unit_id=world.count.pk)
    assert specimen.quantity_on_hand == Decimal("3") and specimen.unit.code == "count"


def test_six_decimal_places_are_kept_exactly(people, world):
    specimen = register(people["technician"], world, quantity="0.000001")
    assert specimen.quantity_on_hand == Decimal("0.000001")


def test_supplied_text_is_kept_on_the_initial_entry(people, world):
    data = dataclasses.replace(
        registration(world, quantity="1.500000"), supplied_text="1.5 (pipetted)"
    )
    specimen = register_data(people["technician"], data)
    assert specimen.ledger.get(kind="initial").supplied_text == "1.5 (pipetted)"


def test_future_and_misordered_times_are_refused(people, world):
    t = people["technician"]
    refused(world, t, "invalid_time", collected_at=ago(-5))
    refused(world, t, "invalid_time", processing_completed_at=ago(-5))
    refused(world, t, "invalid_time", processing_started_at=ago(310))  # before collection
    refused(
        world, t, "invalid_time", processing_started_at=ago(280), processing_completed_at=ago(290)
    )
    refused(world, t, "invalid_time", processing_started_at=None)  # completion without a start
    refused(world, t, "invalid_time", collected_at=ago(300).replace(tzinfo=None))  # naive


def test_registration_cannot_precede_collection_or_the_end_of_processing(people, world):
    t = people["technician"]
    with pytest.raises(ValidationError) as exc:
        register(t, world, occurred_at=ago(285))  # processing completed at ago(280)
    assert codes(exc.value) == {"invalid_time"}
    with pytest.raises(ValidationError) as exc:
        register(t, world, occurred_at=ago(-1))
    assert codes(exc.value) == {"invalid_time"}
    assert not Specimen.objects.exists()


def test_inactive_catalogue_entries_are_refused(people, world):
    m = people["manager"]
    services.save_specimen_catalogue(
        actor=m,
        kind="specimen_type",
        object_id=world.serum.pk,
        expected_revision=world.serum.revision,
        values={"is_active": False},
        reason="Retired locally",
    )
    refused(world, people["technician"], "invalid", specimen_type_id=world.serum.pk)
    inactive_participant = catalogue(m, "participant", code="P-000999", is_active=False)
    refused(world, people["technician"], "invalid", participant_id=inactive_participant.pk)


def test_baseline_thaws_must_be_a_whole_number_or_unknown(people, world):
    refused(world, people["technician"], "invalid", baseline_thaws=-1)
    refused(world, people["technician"], "invalid", baseline_thaws=True)
    refused(world, people["technician"], "invalid", baseline_thaws=1.5)


# --- what registration writes ------------------------------------------------------------------


def test_registration_writes_ledger_custody_baseline_and_audit_together(people, world):
    t = people["technician"]
    specimen = register(t, world, quantity="2.000000", baseline=3)
    assert specimen.status == "available" and specimen.custodian_id == t.pk
    assert specimen.registered_by_id == t.pk and specimen.revision >= 1
    entries = list(QuantityEntry.objects.filter(specimen=specimen))
    assert len(entries) == 1
    assert (entries[0].kind, entries[0].delta, entries[0].balance_after) == (
        "initial",
        Decimal("2"),
        Decimal("2"),
    )
    custody = CustodyEvent.objects.get(specimen=specimen)
    assert custody.kind == "register" and custody.to_custodian_id == t.pk
    baseline = ThawEvent.objects.get(specimen=specimen)
    assert baseline.kind == "baseline" and baseline.count == 3
    event = AuditEvent.objects.get(action="specimens.specimen_registered")
    assert event.object_id == str(specimen.pk) and event.actor_id == t.pk
    assert event.after["accession"] == specimen.accession
    assert specimen.occupancy.position_id is None


def test_unknown_baseline_is_stored_as_null(people, world):
    specimen = register(people["technician"], world, baseline=None)
    assert ThawEvent.objects.get(specimen=specimen, kind="baseline").count is None


def test_registration_with_a_position_places_it(people, world):
    position = world.position("B2")
    specimen = register(people["technician"], world, position=position)
    fresh(specimen)
    specimen.occupancy.refresh_from_db()
    assert specimen.occupancy.position_id == position.pk
    assert list(specimen.custody.order_by("recorded_at").values_list("kind", flat=True)) == [
        "register",
        "place",
    ]


def test_registration_into_an_occupied_position_registers_nothing(people, world):
    t = people["technician"]
    register(t, world, position=world.position("B2"))
    before = (Specimen.objects.count(), Occupancy.objects.count(), QuantityEntry.objects.count())
    with pytest.raises(ValidationError) as exc:
        register(t, world, position=world.position("B2"))
    assert codes(exc.value) == {"occupied"}
    assert before == (
        Specimen.objects.count(),
        Occupancy.objects.count(),
        QuantityEntry.objects.count(),
    )


def test_custodian_can_be_another_active_staff_member(people, world):
    specimen = register(people["technician"], world, custodian_id=people["manager"].pk)
    assert specimen.custodian_id == people["manager"].pk
    refused(world, people["technician"], "invalid", custodian_id=people["nobody"].pk)


# --- roles -------------------------------------------------------------------------------------


@pytest.mark.parametrize("who", ["viewer", "nobody"])
def test_registration_needs_an_operating_role_before_anything_is_created(people, world, who):
    before = (Specimen.objects.count(), Occupancy.objects.count(), AuditEvent.objects.count())
    with pytest.raises(PermissionDenied):
        register(people[who], world)
    assert before == (
        Specimen.objects.count(),
        Occupancy.objects.count(),
        AuditEvent.objects.count(),
    )


@pytest.mark.parametrize("who", ["technician", "viewer", "nobody"])
def test_only_managers_edit_catalogues(people, world, who):
    with pytest.raises(PermissionDenied):
        catalogue(people[who], "participant", code="P-000777")


# --- catalogues --------------------------------------------------------------------------------


def test_codes_are_normalized_upper_case_but_unit_codes_keep_their_case(people, world):
    m = people["manager"]
    assert catalogue(m, "participant", code=" p-000500 ").code == "P-000500"
    assert catalogue(m, "specimen_type", code="urine", name="Urine").code == "URINE"
    mg = catalogue(m, "unit", code="mg", name="milligram", dimension="mass", fractional=True)
    assert mg.code == "mg"
    # mL and ML are different units; ML may exist next to mL.
    big = catalogue(m, "unit", code="ML", name="megalitre", dimension="volume", fractional=True)
    assert big.code == "ML" and big.pk != world.ml.pk


def test_duplicate_codes_are_refused(people, world):
    with pytest.raises(ValidationError) as exc:
        catalogue(people["manager"], "specimen_type", code="plasma", name="Again")
    assert "unique" in codes(exc.value)


def test_a_count_unit_cannot_be_fractional(people, world):
    with pytest.raises(ValidationError) as exc:
        catalogue(
            people["manager"], "unit", code="vial", name="vial", dimension="count", fractional=True
        )
    assert codes(exc.value) == {"invalid"}


def edit(actor, obj, kind, values, reason="Correction of a typo", revision=None):
    return services.save_specimen_catalogue(
        actor=actor,
        kind=kind,
        object_id=obj.pk,
        expected_revision=obj.revision if revision is None else revision,
        values=values,
        reason=reason,
    )


def test_code_is_frozen_once_a_specimen_uses_it(people, world):
    register(people["technician"], world)
    m = people["manager"]
    for kind, obj in (
        ("specimen_type", world.plasma),
        ("participant", world.participant),
        ("collection", world.collection),
        ("container_type", world.cryovial),
    ):
        with pytest.raises(ValidationError) as exc:
            edit(m, obj, kind, {"code": obj.code + "-X"})
        assert codes(exc.value) == {"invalid_transition"}
    # An unused entry may still be renamed.
    assert edit(m, world.serum, "specimen_type", {"code": "SERUM-2"}).code == "SERUM-2"


def test_unit_code_dimension_and_fractional_are_frozen_once_used(people, world):
    register(people["technician"], world)
    m = people["manager"]
    for values in ({"code": "mL2"}, {"dimension": "mass"}, {"fractional": False}):
        with pytest.raises(ValidationError) as exc:
            edit(m, world.ml, "unit", values)
        assert codes(exc.value) == {"invalid_transition"}
    # The name is not frozen.
    assert edit(m, world.ml, "unit", {"name": "millilitres"}).name == "millilitres"


def test_catalogue_edits_need_a_reason_and_the_current_revision(people, world):
    m = people["manager"]
    with pytest.raises(ValidationError) as exc:
        edit(m, world.serum, "specimen_type", {"name": "Serum (new)"}, reason=" ")
    assert codes(exc.value) == {"required"}
    updated = edit(m, world.serum, "specimen_type", {"name": "Serum (new)"})
    assert updated.revision == world.serum.revision + 1
    with pytest.raises(ValidationError) as exc:
        edit(m, world.serum, "specimen_type", {"name": "Serum (again)"})  # stale object
    assert codes(exc.value) == {"stale_revision"}
    event = AuditEvent.objects.filter(action="specimens.specimen_type_changed").get()
    assert event.reason == "Correction of a typo" and event.before["name"] == "Serum"


def test_a_no_op_catalogue_edit_is_refused(people, world):
    with pytest.raises(ValidationError) as exc:
        edit(people["manager"], world.serum, "specimen_type", {"name": "Serum"})
    assert codes(exc.value) == {"invalid_transition"}
