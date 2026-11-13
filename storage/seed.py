"""Synthetic demo storage, created through the services and idempotent by demo codes.

`seed_storage` builds the structure: site EXAMPLE, room CRYO, four storage classes, the 9x9 and
8x12 grid types, "Tank VP-01" (vapor phase, -160.00 °C nominal) with towers T-01 and T-02 and
their shelves, boxes BOX-001 and BOX-002 plus the empty BOX-003 (one blocked position), freezer
MF-01 (-80.00 °C) with rack R-01, and a template of VP-01's layout. Existing records are left as
they are. `move_demo_box` records the demo's box move (BOX-001 to the first shelf of T-02); the
demo seed calls it after the specimens are stored, so the move carries them.

`seed_full_storage` adds the rest of a working repository for `seed_demo --full`: a second site
and room, tanks VP-02 (from the TANK-2T template) and VP-03 (liquid phase, canisters), more
freezers, a refrigerator, racks and about thirty boxes of several grid types, a few blocked
positions and one retired box. It creates what is missing, by code, and changes nothing else.
"""

from decimal import Decimal

from . import recipes, services
from .models import (
    Box,
    ContainerType,
    LayoutNode,
    Position,
    Room,
    Site,
    StorageClass,
    StorageGate,
    StorageTemplate,
    StorageUnit,
)

CLASSES = (
    ("CRYOGENIC", "Cryogenic", "LN2 liquid or vapor phase storage."),
    ("ULTRALOW", "Ultra-low", "Mechanical ultra-low freezers, around -80 °C."),
    ("REFRIGERATED", "Refrigerated", "Refrigerators, around 4 °C."),
    ("AMBIENT", "Ambient", "Room temperature storage."),
)
GRIDS = (
    ("GRID-9X9", "81-place cryobox, A1 across rows", 9, 9, "alpha_numeric", "row_major"),
    ("GRID-8X12", "96-place rack, numbered down columns", 8, 12, "numeric", "column_major"),
)
TOWERS = ("T-01", "T-02")
SHELVES_PER_TOWER = 3
BOXES = (
    # code, shelf, slot, grid
    ("BOX-001", "T-01-S1", 1, "GRID-9X9"),
    ("BOX-002", "T-01-S1", 2, "GRID-8X12"),
    ("BOX-003", "T-01-S2", 1, "GRID-9X9"),
)
DEMO_MOVE = ("BOX-001", "T-02-S1", 1)


def seed_storage(*, actor):
    """Create the demo storage that does not exist yet; return the main records by code."""
    site = Site.objects.filter(code="EXAMPLE").first() or services.create_site(
        actor=actor, code="EXAMPLE", name="Example Biorepository site", timezone="America/New_York"
    )
    room = Room.objects.filter(site=site, code="CRYO").first() or services.create_room(
        actor=actor, site_id=site.pk, code="CRYO", name="Cryogenic storage room"
    )
    classes = {}
    for code, name, description in CLASSES:
        classes[code] = StorageClass.objects.filter(code=code).first() or (
            services.save_storage_catalogue(
                actor=actor,
                kind="class",
                object_id=None,
                expected_revision=None,
                values={"code": code, "name": name, "description": description},
                reason="",
            )
        )
    grids = {}
    for code, name, rows, columns, scheme, order in GRIDS:
        grids[code] = ContainerType.objects.filter(code=code).order_by("version").first() or (
            services.save_storage_catalogue(
                actor=actor,
                kind="container_type",
                object_id=None,
                expected_revision=None,
                values={
                    "code": code,
                    "name": name,
                    "rows": rows,
                    "columns": columns,
                    "label_scheme": scheme,
                    "fill_order": order,
                },
                reason="",
            )
        )
    tank = _unit(actor, room, classes["CRYOGENIC"], "VP-01", "Tank VP-01", "ln2_vapor", "-160.00")
    freezer = _unit(
        actor, room, classes["ULTRALOW"], "MF-01", "Freezer MF-01", "mechanical", "-80.00"
    )
    for number, tower_code in enumerate(TOWERS, start=1):
        tower = _node(
            actor, tank, None, tower_code, f"Tower {number}", "tower", number, SHELVES_PER_TOWER, 0
        )
        for shelf in range(1, SHELVES_PER_TOWER + 1):
            _node(
                actor, tank, tower, f"{tower_code}-S{shelf}", f"Shelf {shelf}", "shelf", shelf, 0, 2
            )
    _node(actor, freezer, None, "R-01", "Rack 1", "rack", 1, 0, 4)
    for code, shelf_code, slot, grid in BOXES:
        if not Box.objects.filter(code=code).exists():
            services.create_box(
                actor=actor,
                node_id=LayoutNode.objects.get(code=shelf_code).pk,
                code=code,
                name=f"Demo box {code[-3:]}",
                slot=slot,
                container_type_id=grids[grid].pk,
            )
    blocked = Position.objects.get(box__code="BOX-003", label="I9")
    if not blocked.blocked and not hasattr(blocked, "occupancy"):
        services.set_position_blocked(
            actor=actor,
            position_id=blocked.pk,
            expected_revision=blocked.revision,
            blocked=True,
            reason="Cracked well found at inspection (synthetic demo)",
        )
    if not StorageTemplate.objects.filter(code="TANK-2T").exists():
        services.save_storage_catalogue(
            actor=actor,
            kind="template",
            object_id=None,
            expected_revision=None,
            values={
                "code": "TANK-2T",
                "name": "Vapor tank with two towers",
                "definition": recipes.recipe_from_unit(tank),
            },
            reason="",
        )
    return {"site": site, "room": room, "tank": tank, "freezer": freezer, "grids": grids}


def _unit(actor, room, storage_class, code, name, technology, temperature):
    return StorageUnit.objects.filter(code=code).first() or services.create_unit(
        actor=actor,
        room_id=room.pk,
        code=code,
        name=name,
        storage_class_id=storage_class.pk,
        technology=technology,
        nominal_temperature=Decimal(temperature),
    )


def _node(actor, unit, parent, code, name, kind, slot, child_slots, box_slots):
    return LayoutNode.objects.filter(code=code).first() or services.create_node(
        actor=actor,
        unit_id=unit.pk,
        parent_id=parent.pk if parent else None,
        code=code,
        name=name,
        kind=kind,
        slot=slot,
        child_slots=child_slots,
        box_slots=box_slots,
    )


def move_demo_box(*, actor, occurred_at, reason="Consolidating tower T-01 (synthetic demo)"):
    """Move BOX-001 with its contents to T-02-S1 slot 1, unless it is already there."""
    code, shelf_code, slot = DEMO_MOVE
    box = Box.objects.get(code=code)
    target = LayoutNode.objects.get(code=shelf_code)
    if box.node_id == target.pk:
        return None
    return services.move_container(
        actor=actor,
        kind="box",
        object_id=box.pk,
        expected_revision=box.revision,
        expected_gate_revision=StorageGate.objects.filter(pk=1)
        .values_list("revision", flat=True)
        .first()
        or 1,
        target_kind="node",
        target_id=target.pk,
        target_slot=slot,
        occurred_at=occurred_at,
        reason=reason,
    )


# --- the full demonstration's additional storage -------------------------------------------------

FULL_GRIDS = (
    ("GRID-10X10", "100-place cryobox, A1 across rows", 10, 10, "alpha_numeric", "row_major"),
    ("GRID-5X5", "25-place small box, A1 across rows", 5, 5, "alpha_numeric", "row_major"),
)
# site code, room code, room name; the ANNEX site is created here
FULL_ROOMS = (("EXAMPLE", "FRZ", "Freezer room"), ("ANNEX", "STOR", "Annex cold store"))
# unit code, name, site, room, class, technology, nominal temperature
FULL_UNITS = (
    ("VP-03", "Tank VP-03 (liquid phase)", "EXAMPLE", "CRYO", "CRYOGENIC", "ln2_liquid", "-196.00"),
    ("MF-02", "Freezer MF-02", "EXAMPLE", "FRZ", "ULTRALOW", "mechanical", "-80.00"),
    ("MF-03", "Freezer MF-03", "ANNEX", "STOR", "ULTRALOW", "mechanical", "-80.00"),
    ("RF-01", "Refrigerator RF-01", "ANNEX", "STOR", "REFRIGERATED", "mechanical", "4.00"),
)
# unit, parent code or None, code, name, kind, slot, child slots, box slots
FULL_NODES = (
    ("VP-03", None, "VP-03-C1", "Canister 1", "canister", 1, 3, 0),
    ("VP-03", None, "VP-03-C2", "Canister 2", "canister", 2, 3, 0),
    ("VP-03", "VP-03-C1", "VP-03-C1-S1", "Shelf 1", "shelf", 1, 0, 2),
    ("VP-03", "VP-03-C1", "VP-03-C1-S2", "Shelf 2", "shelf", 2, 0, 2),
    ("VP-03", "VP-03-C2", "VP-03-C2-S1", "Shelf 1", "shelf", 1, 0, 2),
    ("MF-01", None, "R-02", "Rack 2", "rack", 2, 0, 4),
    ("MF-01", None, "R-03", "Rack 3", "rack", 3, 0, 4),
    ("MF-02", None, "MF-02-R1", "Rack 1", "rack", 1, 0, 4),
    ("MF-02", None, "MF-02-R2", "Rack 2", "rack", 2, 0, 4),
    ("MF-03", None, "MF-03-R1", "Rack 1", "rack", 1, 0, 4),
    ("RF-01", None, "RF-01-R1", "Rack 1", "rack", 1, 0, 3),
)
# code, node, slot, grid, name
FULL_BOXES = (
    ("BOX-004", "T-01-S1", 1, "GRID-9X9", "Plasma aliquots, cohort A"),
    ("BOX-005", "T-01-S2", 2, "GRID-9X9", "Plasma aliquots, cohort B"),
    ("BOX-006", "T-01-S3", 1, "GRID-10X10", "Serum, cohort A"),
    ("BOX-007", "T-01-S3", 2, "GRID-10X10", "Serum, cohort B"),
    ("BOX-008", "T-02-S1", 2, "GRID-9X9", "Primary plasma tubes"),
    ("BOX-009", "T-02-S2", 1, "GRID-5X5", "Pilot specimens"),
    ("BOX-010", "T-02-S2", 2, "GRID-9X9", "Spare box"),
    ("VP-03-L01", "VP-03-C1-S1", 1, "GRID-9X9", "Serum, earlier collection"),
    ("VP-03-L02", "VP-03-C1-S1", 2, "GRID-5X5", "DNA extracts"),
    ("VP-03-L03", "VP-03-C1-S2", 1, "GRID-9X9", "Plasma, earlier collection"),
    ("VP-03-L04", "VP-03-C2-S1", 1, "GRID-9X9", "Spare box"),
    ("MF01-B1", "R-01", 1, "GRID-8X12", "Serum working stock"),
    ("MF01-B2", "R-01", 2, "GRID-8X12", "Serum working stock, second"),
    ("MF01-B3", "R-02", 1, "GRID-10X10", "Urine, cohort A"),
    ("MF01-B4", "R-02", 2, "GRID-10X10", "Spare box"),
    ("MF02-B1", "MF-02-R1", 1, "GRID-8X12", "Urine, cohort B"),
    ("MF02-B2", "MF-02-R1", 2, "GRID-5X5", "Reference material"),
    ("MF02-B3", "MF-02-R1", 3, "GRID-9X9", "Buffy coat"),
    ("MF02-B4", "MF-02-R2", 1, "GRID-9X9", "Spare box"),
    ("MF03-B1", "MF-03-R1", 1, "GRID-9X9", "Shipment from the partner site"),
    ("MF03-B2", "MF-03-R1", 2, "GRID-9X9", "Retired box"),
    ("RF01-B1", "RF-01-R1", 1, "GRID-5X5", "Urine tubes, short term"),
)
# box code, position label, why (damaged wells found at inspection)
FULL_BLOCKED = (
    ("BOX-004", "I9", "Cracked well found at inspection (synthetic demo)"),
    ("MF01-B1", "96", "Damaged well found at inspection (synthetic demo)"),
    ("BOX-007", "J10", "Damaged well found at inspection (synthetic demo)"),
)
RETIRED_BOX = ("MF03-B2", "Never used; the freezer's second box was removed (synthetic demo)")


def seed_full_storage(*, actor):
    """Add the rest of the demonstration storage; return units, rooms and boxes by code."""
    sites = {"EXAMPLE": Site.objects.get(code="EXAMPLE")}
    sites["ANNEX"] = Site.objects.filter(code="ANNEX").first() or services.create_site(
        actor=actor, code="ANNEX", name="Example Annex (synthetic)", timezone="America/New_York"
    )
    rooms = {("EXAMPLE", "CRYO"): Room.objects.get(site=sites["EXAMPLE"], code="CRYO")}
    for site_code, code, name in FULL_ROOMS:
        site = sites[site_code]
        rooms[(site_code, code)] = Room.objects.filter(site=site, code=code).first() or (
            services.create_room(actor=actor, site_id=site.pk, code=code, name=name)
        )
    classes = {c.code: c for c in StorageClass.objects.all()}
    grids = {}
    for code, name, rows, columns, scheme, order in FULL_GRIDS:
        grids[code] = ContainerType.objects.filter(code=code).order_by("version").first() or (
            services.save_storage_catalogue(
                actor=actor,
                kind="container_type",
                object_id=None,
                expected_revision=None,
                values={
                    "code": code,
                    "name": name,
                    "rows": rows,
                    "columns": columns,
                    "label_scheme": scheme,
                    "fill_order": order,
                },
                reason="",
            )
        )
    for grid in ContainerType.objects.filter(code__in=("GRID-9X9", "GRID-8X12")):
        grids.setdefault(grid.code, grid)
    units = {unit.code: unit for unit in StorageUnit.objects.all()}
    # VP-02 is made from the template: the quickest way to add a tank like VP-01.
    if "VP-02" not in units:
        services.instantiate_template(
            actor=actor,
            template_id=StorageTemplate.objects.get(code="TANK-2T").pk,
            room_id=rooms[("EXAMPLE", "CRYO")].pk,
            unit_code="VP-02",
            unit_name="Tank VP-02",
        )
        units["VP-02"] = StorageUnit.objects.get(code="VP-02")
    for code, name, site, room, storage_class, technology, temperature in FULL_UNITS:
        if code not in units:
            units[code] = _unit(
                actor,
                rooms[(site, room)],
                classes[storage_class],
                code,
                name,
                technology,
                temperature,
            )
    for unit, parent, code, name, kind, slot, child_slots, box_slots in FULL_NODES:
        parent_node = LayoutNode.objects.get(code=parent) if parent else None
        _node(actor, units[unit], parent_node, code, name, kind, slot, child_slots, box_slots)
    for code, node, slot, grid, name in FULL_BOXES:
        if not Box.objects.filter(code=code).exists():
            services.create_box(
                actor=actor,
                node_id=LayoutNode.objects.get(code=node).pk,
                code=code,
                name=name,
                slot=slot,
                container_type_id=grids[grid].pk,
            )
    for code, label, why in FULL_BLOCKED:
        position = Position.objects.get(box__code=code, label=label)
        if not position.blocked and not hasattr(position, "occupancy"):
            services.set_position_blocked(
                actor=actor,
                position_id=position.pk,
                expected_revision=position.revision,
                blocked=True,
                reason=why,
            )
    code, why = RETIRED_BOX
    box = Box.objects.get(code=code)
    if box.is_active:
        services.retire_storage(
            actor=actor, kind="box", object_id=box.pk, expected_revision=box.revision, reason=why
        )
    return {"units": units, "boxes": {b.code: b for b in Box.objects.all()}}
