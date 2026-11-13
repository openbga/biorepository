"""Shared fixtures for specimens tests: people with roles, a small storage tree, catalogues.

All data is synthetic: users at example.invalid, participant codes like P-000123.
"""

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group, Permission
from django.utils import timezone

from accounts.models import User
from specimens import services
from specimens.services import Registration
from storage import services as storage

PASSWORD = "test-password-729!"


def make_user(name, *roles, approver=False, superuser=False):
    user = User.objects.create_user(name, f"{name}@example.invalid", PASSWORD)
    if superuser:
        user.is_superuser = True
        user.is_staff = True
        user.save()
    for role in roles:
        user.groups.add(Group.objects.get_or_create(name=role)[0])
    if approver:
        user.user_permissions.add(
            Permission.objects.get(content_type__app_label="accounts", codename="approve_disposal")
        )
    return user


@pytest.fixture
def people(db):
    return {
        "admin": make_user("sp-admin", "admin"),
        "manager": make_user("sp-manager", "manager"),
        "technician": make_user("sp-technician", "technician"),
        "technician2": make_user("sp-technician-2", "technician"),
        "approver": make_user("sp-approver", "technician", approver=True),
        "superuser": make_user("sp-superuser", "admin", superuser=True),
        "viewer": make_user("sp-viewer", "viewer"),
        "nobody": make_user("sp-nobody"),
    }


def ago(minutes):
    return timezone.now() - timedelta(minutes=minutes)


@dataclass
class World:
    unit: object
    shelf1: object
    shelf2: object
    box: object
    box2: object
    plasma: object
    serum: object
    cryovial: object
    ml: object
    ul: object
    count: object
    participant: object
    collection: object

    def position(self, label, box=None):
        return (box or self.box).positions.get(label=label)


def catalogue(actor, kind, **values):
    return services.save_specimen_catalogue(
        actor=actor, kind=kind, object_id=None, expected_revision=None, values=values, reason=""
    )


@pytest.fixture
def world(people):
    m = people["manager"]
    site = storage.create_site(actor=m, code="EXAMPLE", name="Example site", timezone="UTC")
    room = storage.create_room(actor=m, site_id=site.pk, code="CRYO", name="Cryo room")
    cls = storage.save_storage_catalogue(
        actor=m,
        kind="class",
        object_id=None,
        expected_revision=None,
        values={"code": "CRYOGENIC", "name": "Cryogenic", "description": ""},
        reason="",
    )
    unit = storage.create_unit(
        actor=m,
        room_id=room.pk,
        code="VP-01",
        name="Tank VP-01",
        storage_class_id=cls.pk,
        technology="ln2_vapor",
        nominal_temperature=Decimal("-160.00"),
    )
    tower = storage.create_node(
        actor=m,
        unit_id=unit.pk,
        parent_id=None,
        code="T-01",
        name="Tower 1",
        kind="tower",
        slot=1,
        child_slots=2,
        box_slots=0,
    )
    shelves = [
        storage.create_node(
            actor=m,
            unit_id=unit.pk,
            parent_id=tower.pk,
            code=f"T-01-S{n}",
            name=f"Shelf {n}",
            kind="shelf",
            slot=n,
            child_slots=0,
            box_slots=2,
        )
        for n in (1, 2)
    ]
    grid = storage.save_storage_catalogue(
        actor=m,
        kind="container_type",
        object_id=None,
        expected_revision=None,
        values={
            "code": "GRID-3X3",
            "name": "3 x 3",
            "rows": 3,
            "columns": 3,
            "label_scheme": "alpha_numeric",
            "fill_order": "row_major",
        },
        reason="",
    )
    box = storage.create_box(
        actor=m,
        node_id=shelves[0].pk,
        code="BOX-001",
        name="Box 1",
        slot=1,
        container_type_id=grid.pk,
    )
    box2 = storage.create_box(
        actor=m,
        node_id=shelves[0].pk,
        code="BOX-002",
        name="Box 2",
        slot=2,
        container_type_id=grid.pk,
    )
    ml = catalogue(m, "unit", code="mL", name="millilitre", dimension="volume", fractional=True)
    ul = catalogue(m, "unit", code="uL", name="microlitre", dimension="volume", fractional=True)
    count = catalogue(m, "unit", code="count", name="count", dimension="count", fractional=False)
    return World(
        unit=unit,
        shelf1=shelves[0],
        shelf2=shelves[1],
        box=box,
        box2=box2,
        plasma=catalogue(m, "specimen_type", code="PLASMA", name="Plasma"),
        serum=catalogue(m, "specimen_type", code="SERUM", name="Serum"),
        cryovial=catalogue(
            m,
            "container_type",
            code="CRYOVIAL-2ML",
            name="Cryovial 2 mL",
            nominal_capacity="2",
            capacity_unit=ml.pk,
        ),
        ml=ml,
        ul=ul,
        count=count,
        participant=catalogue(m, "participant", code="P-000123"),
        collection=catalogue(m, "collection", code="DEMO", name="Demonstration collection"),
    )


def registration(world, quantity="2.000000", position=None, baseline=0, **extra):
    return Registration(
        specimen_type_id=extra.pop("specimen_type_id", world.plasma.pk),
        container_type_id=world.cryovial.pk,
        quantity=Decimal(quantity),
        supplied_text=quantity,
        unit_id=extra.pop("unit_id", world.ml.pk),
        collected_at=extra.pop("collected_at", ago(300)),
        processing_started_at=extra.pop("processing_started_at", ago(290)),
        processing_completed_at=extra.pop("processing_completed_at", ago(280)),
        participant_id=world.participant.pk,
        collection_id=world.collection.pk,
        baseline_thaws=baseline,
        position_id=position.pk if position else None,
        **extra,
    )


def register(actor, world, *, occurred_at=None, **kwargs):
    return services.register_specimen(
        actor=actor,
        data=registration(world, **kwargs),
        occurred_at=occurred_at or ago(270),
        reason="Received from collection site",
    )


def occ_rev(specimen):
    specimen.refresh_from_db()
    specimen.occupancy.refresh_from_db()
    return specimen.occupancy.revision


def place(actor, specimen, position, occurred_at=None):
    specimen.refresh_from_db()
    return services.place_specimen(
        actor=actor,
        specimen_id=specimen.pk,
        expected_revision=specimen.revision,
        expected_occupancy_revision=occ_rev(specimen),
        position_id=position.pk,
        occurred_at=occurred_at or ago(260),
        reason="Stored after processing",
    )


def check_out(actor, specimen, recipient, occurred_at=None):
    specimen.refresh_from_db()
    return services.check_out(
        actor=actor,
        specimen_id=specimen.pk,
        expected_revision=specimen.revision,
        expected_occupancy_revision=occ_rev(specimen),
        recipient_id=recipient.pk,
        destination="Processing bench 2",
        occurred_at=occurred_at or ago(200),
        reason="Aliquoting",
    )


def check_in(actor, specimen, position, thaw=None, occurred_at=None, recipient=None):
    specimen.refresh_from_db()
    return services.check_in(
        actor=actor,
        specimen_id=specimen.pk,
        expected_revision=specimen.revision,
        expected_occupancy_revision=occ_rev(specimen),
        position_id=position.pk,
        recipient_id=(recipient or actor).pk,
        thaw_observed=thaw,
        occurred_at=occurred_at or ago(100),
        reason="Returned after aliquoting",
    )


# --- helpers shared by the service-layer tests -----------------------------------------------


def codes(exc):
    """Validation error codes of a raised ValidationError (field or non-field)."""
    from django.core.exceptions import ValidationError

    assert isinstance(exc, ValidationError), exc
    if hasattr(exc, "error_dict"):
        return {e.code for errors in exc.error_dict.values() for e in errors}
    return {e.code for e in exc.error_list}


def fresh(specimen):
    specimen.refresh_from_db()
    return specimen


def gate_revision():
    from storage.models import StorageGate

    return StorageGate.objects.get(pk=1).revision


def move_box(actor, box, node, slot=1, occurred_at=None):
    """Move a box with its contents (changes the occupancy revision of everything in it)."""
    box.refresh_from_db()
    return storage.move_container(
        actor=actor,
        kind="box",
        object_id=box.pk,
        expected_revision=box.revision,
        expected_gate_revision=gate_revision(),
        target_kind="node",
        target_id=node.pk,
        target_slot=slot,
        occurred_at=occurred_at or timezone.now(),
        reason="Rebalancing shelves",
    )


def ask_disposal(actor, specimen, method="Autoclave", destination="Biohazard waste"):
    return services.request_disposal(
        actor=actor,
        specimen_id=specimen.pk,
        expected_revision=fresh(specimen).revision,
        method=method,
        destination=destination,
        reason="Study closed",
    )


def decide(actor, request, decision="approve", reason="Checked against the closure notice"):
    return services.decide_disposal(
        actor=actor,
        request_id=request.pk,
        expected_revision=fresh(request.specimen).revision,
        decision=decision,
        reason=reason,
    )


def execute(actor, request, occurred_at=None):
    specimen = fresh(request.specimen)
    return services.execute_disposal(
        actor=actor,
        request_id=request.pk,
        expected_revision=specimen.revision,
        expected_occupancy_revision=occ_rev(specimen),
        occurred_at=occurred_at or timezone.now(),
        reason="Autoclaved",
    )


def hold(actor, specimen, scope="all_use", occurred_at=None):
    return services.place_hold(
        actor=actor,
        specimen_id=specimen.pk,
        expected_revision=fresh(specimen).revision,
        scope=scope,
        occurred_at=occurred_at or ago(150),
        reason="Quality review",
    )


def release(actor, held, occurred_at=None):
    return services.release_hold(
        actor=actor,
        hold_id=held.pk,
        expected_revision=fresh(held.specimen).revision,
        occurred_at=occurred_at or timezone.now(),
        reason="Review complete",
    )


def derive(actor, parent, children, consumed, kind="aliquot", occurred_at=None):
    fresh(parent)
    return services.derive_specimens(
        actor=actor,
        parent_id=parent.pk,
        expected_revision=parent.revision,
        expected_occupancy_revision=occ_rev(parent),
        kind=kind,
        consumed=consumed,
        children=tuple(children),
        procedure="Split",
        occurred_at=occurred_at or ago(190),
        reason="Aliquoting",
    )


def child(world, quantity, position=None, **extra):
    return services.ChildSpec(
        extra.pop("specimen_type_id", world.plasma.pk),
        world.cryovial.pk,
        Decimal(quantity),
        str(quantity),
        extra.pop("unit_id", world.ml.pk),
        position.pk if position else None,
    )


def consume(actor, specimen, quantity, occurred_at=None):
    return services.consume_quantity(
        actor=actor,
        specimen_id=specimen.pk,
        expected_revision=fresh(specimen).revision,
        quantity=Decimal(quantity),
        supplied_text=str(quantity),
        occurred_at=occurred_at or ago(190),
        reason="Assay",
    )


def stored(actor, world, position="A1", **kwargs):
    """A registered specimen placed in the given position of BOX-001."""
    specimen = register(actor, world, **kwargs)
    place(actor, specimen, world.position(position))
    return fresh(specimen)
