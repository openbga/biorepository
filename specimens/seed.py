"""Synthetic demo specimens, created through the services and idempotent by demo codes.

`seed_specimens` runs after `storage.seed.seed_storage` and walks the product's first journey
(docs/product.md) with obviously fake data: participant P-000123 in collection DEMO gives a
2 mL plasma specimen that is registered and stored in BOX-001, checked out, split into two
0.5 mL aliquots stored beside it, checked back in with one observed thaw; BOX-001 then moves to
tower T-02 with its contents; one aliquot is disposed (requested by the technician, approved by
the manager, recorded by the technician). A second specimen has a hold placed and released, one
specimen waits unstored, a disposal request waits for approval, an import with errors stays in
preview and a valid import is committed and its labels generated. Accessions are whatever the
sequence returns; nothing here assumes a number. Nothing is created twice: once the DEMO
collection has specimens, the journey is left as it is.

`seed_full_catalogues` adds the vocabulary of the larger demonstration (`seed_demo --full`):
more specimen and container types, three collections and about seventy coded participants. The
specimens themselves are created by `core/management/commands/_demo_full.py`.
"""

from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from storage.models import Box, Position
from storage.seed import move_demo_box

from . import importing, services
from .models import (
    Collection,
    Participant,
    QuantityUnit,
    Specimen,
    SpecimenContainerType,
    SpecimenType,
)
from .services import ChildSpec, Registration

UNITS = (
    ("mL", "millilitre", "volume", True),
    ("uL", "microlitre", "volume", True),
    ("count", "count", "count", False),
)
TYPES = (("PLASMA", "Plasma"), ("SERUM", "Serum"), ("BUFFY-COAT", "Buffy coat"))
PARTICIPANTS = ("P-000123", "P-000124", "P-000125")
INVALID_IMPORT = (
    "row_key,specimen_type,container_type,quantity,unit,collected_at,processing_started_at,"
    "processing_completed_at,participant_code,collection_code,box_code,position_label,"
    "baseline_thaws\n"
    "BAD-1,PLASMA,CRYOVIAL-2ML,0.1234567,mL,2026-01-05T09:00:00-05:00,,,P-999999,DEMO,,,\n"
    "BAD-2,UNKNOWN-TYPE,CRYOVIAL-2ML,1,ML,2026-01-05T09:00:00-05:00,,,,,BOX-003,,\n"
)


def _get_or_create(actor, kind, model, code, **values):
    existing = model.objects.filter(code=code).first()
    if existing is not None:
        return existing
    return services.save_specimen_catalogue(
        actor=actor,
        kind=kind,
        object_id=None,
        expected_revision=None,
        values={"code": code, **values},
        reason="",
    )


def seed_catalogues(*, actor):
    units = {
        code: _get_or_create(
            actor, "unit", QuantityUnit, code, name=name, dimension=dim, fractional=frac
        )
        for code, name, dim, frac in UNITS
    }
    types = {
        code: _get_or_create(actor, "specimen_type", SpecimenType, code, name=name)
        for code, name in TYPES
    }
    vial = _get_or_create(
        actor,
        "container_type",
        SpecimenContainerType,
        "CRYOVIAL-2ML",
        name="Cryovial, 2 mL, external thread",
        nominal_capacity="2",
        capacity_unit=units["mL"].pk,
    )
    participants = {
        code: _get_or_create(actor, "participant", Participant, code) for code in PARTICIPANTS
    }
    collection = _get_or_create(
        actor, "collection", Collection, "DEMO", name="Demonstration collection (synthetic)"
    )
    return units, types, vial, participants, collection


def _position(box, label):
    return Position.objects.get(box__code=box, label=label)


def _fresh(specimen):
    specimen.refresh_from_db()
    return specimen, specimen.occupancy.revision


def seed_specimens(*, manager, technician):
    """Demo catalogues and the journey; `manager` holds the explicit disposal approval."""
    units, types, vial, participants, collection = seed_catalogues(actor=manager)
    if Specimen.objects.filter(collection=collection).exists():
        return None
    ml = units["mL"]
    now = timezone.now()
    start = now - timedelta(days=3)

    def at(hours):
        return start + timedelta(hours=hours)

    def registration(participant, quantity, *, position=None, baseline=0, hours=0):
        return Registration(
            specimen_type_id=types["PLASMA"].pk,
            container_type_id=vial.pk,
            quantity=Decimal(quantity),
            supplied_text=quantity,
            unit_id=ml.pk,
            collected_at=at(hours),
            processing_started_at=at(hours + 0.3),
            processing_completed_at=at(hours + 0.8),
            participant_id=participants[participant].pk,
            collection_id=collection.pk,
            baseline_thaws=baseline,
            position_id=position.pk if position else None,
        )

    tech = technician
    # 1. Register the parent and store it in BOX-001 A1.
    parent = services.register_specimen(
        actor=tech,
        data=registration("P-000123", "2.000000", baseline=0),
        occurred_at=at(1),
        reason="Received from the collection site (synthetic demo)",
    )
    parent, occupancy_revision = _fresh(parent)
    services.place_specimen(
        actor=tech,
        specimen_id=parent.pk,
        expected_revision=parent.revision,
        expected_occupancy_revision=occupancy_revision,
        position_id=_position("BOX-001", "A1").pk,
        occurred_at=at(1.1),
        reason="Stored after processing",
    )
    # 2. Check it out, split two aliquots stored beside it, check it back in (thaw observed).
    parent, occupancy_revision = _fresh(parent)
    services.check_out(
        actor=tech,
        specimen_id=parent.pk,
        expected_revision=parent.revision,
        expected_occupancy_revision=occupancy_revision,
        recipient_id=tech.pk,
        destination="Processing bench 2",
        occurred_at=at(24),
        reason="Aliquoting for the demonstration study",
    )
    parent, occupancy_revision = _fresh(parent)
    aliquots = services.derive_specimens(
        actor=tech,
        parent_id=parent.pk,
        expected_revision=parent.revision,
        expected_occupancy_revision=occupancy_revision,
        kind="aliquot",
        consumed=Decimal("1.000000"),
        children=tuple(
            ChildSpec(
                types["PLASMA"].pk,
                vial.pk,
                Decimal("0.500000"),
                "0.5",
                ml.pk,
                _position("BOX-001", label).pk,
            )
            for label in ("A2", "A3")
        ),
        procedure="Split into two 0.5 mL aliquots",
        occurred_at=at(24.2),
        reason="Aliquoting for the demonstration study",
    )
    parent, occupancy_revision = _fresh(parent)
    services.check_in(
        actor=tech,
        specimen_id=parent.pk,
        expected_revision=parent.revision,
        expected_occupancy_revision=occupancy_revision,
        position_id=_position("BOX-001", "A1").pk,
        recipient_id=tech.pk,
        thaw_observed=True,
        occurred_at=at(24.6),
        reason="Returned after aliquoting; the vial had thawed on the bench",
    )
    # 3. BOX-001 moves to tower T-02 with its three specimens in one event.
    move_demo_box(actor=tech, occurred_at=at(26))
    # 4. A second specimen: hold placed and released.
    second = services.register_specimen(
        actor=tech,
        data=registration(
            "P-000124", "1.800000", position=_position("BOX-002", "1"), baseline=None, hours=2
        ),
        occurred_at=at(3),
        reason="Received from the collection site (synthetic demo)",
    )
    second.refresh_from_db()
    hold = services.place_hold(
        actor=tech,
        specimen_id=second.pk,
        expected_revision=second.revision,
        scope="disposal",
        occurred_at=at(30),
        reason="Possible re-test requested (synthetic demo)",
    )
    second.refresh_from_db()
    services.release_hold(
        actor=manager,
        hold_id=hold.pk,
        expected_revision=second.revision,
        occurred_at=at(48),
        reason="Re-test not needed",
    )
    # 5. A specimen registered but not yet stored (dashboard queue).
    services.register_specimen(
        actor=tech,
        data=registration("P-000125", "1.000000", hours=60),
        occurred_at=at(62),
        reason="Received; waiting for a free box (synthetic demo)",
    )
    # 6. Dispose of one aliquot: technician requests, manager approves, technician records it.
    aliquot = aliquots[1]
    aliquot.refresh_from_db()
    request = services.request_disposal(
        actor=tech,
        specimen_id=aliquot.pk,
        expected_revision=aliquot.revision,
        method="Autoclave, then clinical waste",
        destination="Biohazard waste stream",
        reason="Aliquot used for a method check; remainder not needed (synthetic demo)",
    )
    aliquot.refresh_from_db()
    services.decide_disposal(
        actor=manager,
        request_id=request.pk,
        expected_revision=aliquot.revision,
        decision="approve",
        reason="Checked against the demonstration plan",
    )
    aliquot, occupancy_revision = _fresh(aliquot)
    services.execute_disposal(
        actor=tech,
        request_id=request.pk,
        expected_revision=aliquot.revision,
        expected_occupancy_revision=occupancy_revision,
        occurred_at=timezone.now(),
        reason="Autoclaved and discarded",
    )
    # 7. Imports: one with errors left in preview, one valid and committed, then its labels.
    importing.validate_import(
        actor=tech, csv_bytes=INVALID_IMPORT.encode(), filename="demo-with-errors.csv"
    )
    third_box = Box.objects.get(code="BOX-003")
    valid = (
        ",".join(importing.COLUMNS)
        + "\n"
        + "".join(
            f"DEMO-{n},PLASMA,CRYOVIAL-2ML,0.750000,mL,{at(5).isoformat()},,,P-000123,DEMO,"
            f"{third_box.code},{label},0\n"
            for n, label in ((1, "A1"), (2, "A2"))
        )
    )
    batch = importing.validate_import(
        actor=tech, csv_bytes=valid.encode(), filename="demo-shipment.csv"
    )
    commit = importing.commit_import(
        actor=tech,
        batch_id=batch.pk,
        expected_sha256=batch.sha256,
        reason="Demonstration shipment received",
    )
    try:
        from .labels import render_labels

        render_labels(actor=tech, specimen_ids=tuple(commit.specimen_ids))
    except OSError:  # WeasyPrint's native libraries are missing on this machine
        pass
    # 8. A disposal request waiting for approval (dashboard queue for the approver).
    imported = Specimen.objects.get(pk=commit.specimen_ids[1])
    services.request_disposal(
        actor=tech,
        specimen_id=imported.pk,
        expected_revision=imported.revision,
        method="Autoclave, then clinical waste",
        destination="Biohazard waste stream",
        reason="Duplicate of DEMO-1 (synthetic demo)",
    )
    return {"parent": parent, "aliquots": aliquots, "second": second}


# --- the full demonstration's additional catalogue entries ------------------------------------

FULL_TYPES = (("URINE", "Urine"), ("PBMC", "PBMC (cryopreserved)"), ("DNA-EXTRACT", "DNA extract"))
# code, name, nominal capacity (mL)
FULL_CONTAINERS = (
    ("CRYOTUBE-5ML", "Cryotube, 5 mL, external thread", "5"),
    ("MICROTUBE-0.5ML", "Microtube, 0.5 mL, screw cap", "0.5"),
    ("URINE-TUBE-10ML", "Urine tube, 10 mL", "10"),
)
FULL_COLLECTIONS = (
    ("COH-A", "Cohort A (synthetic)"),
    ("COH-B", "Cohort B (synthetic)"),
    ("PILOT", "Pilot draws (synthetic)"),
)
# Coded participants only: no name, contact or record number exists to be stored.
COHORT_A = tuple(f"P-{n:06d}" for n in range(201, 217))
COHORT_B = tuple(f"P-{n:06d}" for n in range(217, 241))
PILOT = tuple(f"P-{n:06d}" for n in range(241, 249))
SHIPMENT = tuple(f"P-{n:06d}" for n in range(301, 321))


def seed_full_catalogues(*, actor):
    """Add the larger demonstration's catalogue entries that are missing; return them by code."""
    units, types, vial, participants, collection = seed_catalogues(actor=actor)
    types = {
        **types,
        **{
            code: _get_or_create(actor, "specimen_type", SpecimenType, code, name=name)
            for code, name in FULL_TYPES
        },
    }
    containers = {"CRYOVIAL-2ML": vial}
    for code, name, capacity in FULL_CONTAINERS:
        containers[code] = _get_or_create(
            actor,
            "container_type",
            SpecimenContainerType,
            code,
            name=name,
            nominal_capacity=capacity,
            capacity_unit=units["mL"].pk,
        )
    collections = {"DEMO": collection}
    for code, name in FULL_COLLECTIONS:
        collections[code] = _get_or_create(actor, "collection", Collection, code, name=name)
    for code in (*COHORT_A, *COHORT_B, *PILOT, *SHIPMENT):
        participants[code] = _get_or_create(actor, "participant", Participant, code)
    return units, types, containers, participants, collections
