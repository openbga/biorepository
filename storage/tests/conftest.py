from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.utils import timezone

from accounts.models import User
from storage import services
from storage.models import StorageGate

PASSWORD = "test-password-729!"


def make_user(name, *roles):
    user = User.objects.create_user(name, f"{name}@example.invalid", PASSWORD)
    for role in roles:
        user.groups.add(Group.objects.get_or_create(name=role)[0])
    return user


@pytest.fixture
def people(db):
    return {
        "admin": make_user("st-admin", "admin"),
        "manager": make_user("st-manager", "manager"),
        "technician": make_user("st-technician", "technician"),
        "viewer": make_user("st-viewer", "viewer"),
        "nobody": make_user("st-nobody"),
    }


def ago(minutes):
    return timezone.now() - timedelta(minutes=minutes)


def grid(actor, code="GRID-9X9", rows=9, columns=9, scheme="alpha_numeric", order="row_major"):
    return services.save_storage_catalogue(
        actor=actor,
        kind="container_type",
        object_id=None,
        expected_revision=None,
        values={
            "code": code,
            "name": f"{rows} x {columns}",
            "rows": rows,
            "columns": columns,
            "label_scheme": scheme,
            "fill_order": order,
        },
        reason="",
    )


@dataclass
class Tree:
    site: object
    room: object
    cryogenic: object
    unit: object
    tower: object
    shelf1: object
    shelf2: object
    grid: object
    box: object
    other_unit: object
    rack: object


@pytest.fixture
def tree(people):
    """EXAMPLE / CRYO / VP-01 (tower T-01: shelves S1, S2 with 2 box slots) + BOX-001 (9x9) on
    S1 slot 1; freezer MF-01 with a top-level rack R-01 holding 2 box slots."""
    m = people["manager"]
    site = services.create_site(actor=m, code="example", name="Example site", timezone="UTC")
    room = services.create_room(actor=m, site_id=site.pk, code="CRYO", name="Cryo room")
    cryogenic = services.save_storage_catalogue(
        actor=m,
        kind="class",
        object_id=None,
        expected_revision=None,
        values={"code": "CRYOGENIC", "name": "Cryogenic", "description": ""},
        reason="",
    )
    unit = services.create_unit(
        actor=m,
        room_id=room.pk,
        code="VP-01",
        name="Tank VP-01",
        storage_class_id=cryogenic.pk,
        technology="ln2_vapor",
        nominal_temperature=Decimal("-160.00"),
    )
    tower = services.create_node(
        actor=m,
        unit_id=unit.pk,
        parent_id=None,
        code="T-01",
        name="Tower 1",
        kind="tower",
        slot=1,
        child_slots=4,
        box_slots=0,
    )
    shelves = [
        services.create_node(
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
    grid9 = grid(m)
    box = services.create_box(
        actor=m,
        node_id=shelves[0].pk,
        code="BOX-001",
        name="Box 1",
        slot=1,
        container_type_id=grid9.pk,
    )
    other_unit = services.create_unit(
        actor=m,
        room_id=room.pk,
        code="MF-01",
        name="Freezer MF-01",
        storage_class_id=cryogenic.pk,
        technology="mechanical",
        nominal_temperature=Decimal("-80.00"),
    )
    rack = services.create_node(
        actor=m,
        unit_id=other_unit.pk,
        parent_id=None,
        code="R-01",
        name="Rack 1",
        kind="rack",
        slot=1,
        child_slots=0,
        box_slots=2,
    )
    return Tree(
        site, room, cryogenic, unit, tower, shelves[0], shelves[1], grid9, box, other_unit, rack
    )


def gate_revision():
    return StorageGate.objects.get(pk=1).revision


def new_item(actor, occurred_at=None):
    """An unplaced storage key, as the specimens registration creates one."""
    with services.operation():
        services.lock_gate()
        return services.new_occupancy(actor=actor, occurred_at=occurred_at or ago(60))


def place(actor, occupancy, position, occurred_at=None, kind="place", **extra):
    with services.operation():
        services.lock_gate()
        return services.change_occupancy(
            actor=actor,
            occupancy_id=occupancy.pk,
            expected_revision=extra.pop("expected_revision", occupancy.revision),
            position_id=position.pk if position else None,
            kind=kind,
            occurred_at=occurred_at or ago(30),
            reason=extra.pop("reason", "Stored after processing"),
            **extra,
        )
