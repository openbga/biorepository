"""Storage mutations: definitions, box grids, position blocking, retirement, moves and claims.

Every mutation runs in `operation()` (a transaction plus a per-connection operation scope), checks
the actor's role, takes the StorageGate row lock first (`lock_gate`), then locks the rows it
changes in the contract order (unit -> node -> box -> positions by id -> occupancies by id),
revalidates against the locked state, writes, appends events and records the audit event in the
same transaction. Nested calls join the caller's operation: a changed object's revision, and the
gate revision, increase once per top-level operation.

The specimens app calls the internal helpers `new_occupancy`, `change_occupancy` and
`touch_occupancy` from its own operation, after `lock_gate()`:

    from storage import services as storage

    @storage.operation()
    def register_specimen(*, actor, ...):
        storage.lock_gate()
        occupancy = storage.new_occupancy(actor=actor, occurred_at=occurred_at)
        ...

Validation failures raise django ValidationError with one of the contract codes (stale_revision,
occupied, inactive_location, invalid_transition, invalid_time) or a field code (required,
invalid, unique); a role failure raises PermissionDenied before any record is read.
"""

import json
import re
import uuid
import zoneinfo
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import IntegrityError, connection, transaction
from django.db.models import F, Max, Q
from django.utils import timezone as tz

from accounts.roles import require_role
from core.audit import record_event
from core.models import AuditEvent

from . import recipes
from .models import (
    ABSOLUTE_ZERO,
    CODE_PATTERN,
    MAX_DEPTH,
    TEMPERATURE_UNIT,
    Box,
    ContainerType,
    LayoutNode,
    MoveEvent,
    MoveMember,
    Occupancy,
    Position,
    Room,
    Site,
    StorageClass,
    StorageGate,
    StorageTemplate,
    StorageUnit,
)

MANAGE_ROLES = ("admin", "manager")
OPERATE_ROLES = ("admin", "manager", "technician")
READ_ROLES = ("admin", "manager", "technician", "viewer")
INITIAL_REASON = "Initial definition"
MAX_SLOTS = recipes.MAX_SLOTS
CHILD_KINDS = recipes.CHILD_KINDS
ITEM_MOVE_KINDS = ("place", "relocate", "check_out", "check_in", "dispose", "correction")

_OPERATION = "_vapor_storage_operation"


# --- operation scope and gate -------------------------------------------------------------


class _Operation:
    def __init__(self):
        self.id = uuid.uuid4()
        self.depth = 0
        self.gate = None
        self.gate_bumped = False
        self.bumped = set()  # (label, pk) whose revision this operation already increased


@contextmanager
def operation():
    """Transaction plus operation scope; usable as `with operation():` or `@operation()`."""
    with transaction.atomic():
        current = getattr(connection, _OPERATION, None)
        if current is None:
            current = _Operation()
            setattr(connection, _OPERATION, current)
        current.depth += 1
        try:
            yield current
        finally:
            current.depth -= 1
            if current.depth == 0:
                setattr(connection, _OPERATION, None)


def _current():
    current = getattr(connection, _OPERATION, None)
    if current is None or not connection.in_atomic_block:
        raise RuntimeError("Storage mutations must run inside storage.services.operation().")
    return current


def lock_gate():
    """Lock StorageGate(pk=1) for the current operation; always the first lock taken."""
    current = _current()
    if current.gate is None:
        # Migration 0002 inserts the row and a trigger refuses DELETE; a test flush (TRUNCATE)
        # can still empty the table, so restore it without racing a concurrent creator.
        gate = StorageGate.objects.select_for_update().filter(pk=1).first()
        if gate is None:
            StorageGate.objects.bulk_create([StorageGate(pk=1)], ignore_conflicts=True)
            gate = StorageGate.objects.select_for_update().get(pk=1)
        current.gate = gate
    return current.gate


def _require_gate():
    current = _current()
    if current.gate is None:
        raise RuntimeError("Call storage.services.lock_gate() before changing storage claims.")
    return current


def require_gate():
    """Public guard for other apps' internal services: raise RuntimeError unless called inside
    `operation()` after `lock_gate()`; return the locked StorageGate row. Takes no lock itself."""
    return _require_gate().gate


def _id(value, field=None):
    """A UUID from a UUID or its string form (form posts and direct callers alike)."""
    if value is None or isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise _error("Choose an existing record.", "invalid", field) from None


def _bump_gate():
    """Record a topology, position or claim change: the gate revision rises once per operation."""
    current = _require_gate()
    if not current.gate_bumped:
        StorageGate.objects.filter(pk=1).update(revision=F("revision") + 1)
        current.gate.revision += 1
        current.gate_bumped = True


def _bump(obj):
    """Increase obj.revision once per operation (in memory; the caller saves)."""
    current = _current()
    key = (obj._meta.label_lower, obj.pk)
    if key not in current.bumped:
        obj.revision += 1
        current.bumped.add(key)


def _check_revision(obj, expected, noun):
    """Stale unless `expected` is the revision the caller read before this operation began."""
    current = _current()
    already = (obj._meta.label_lower, obj.pk) in current.bumped
    if expected is None or not (
        obj.revision == expected or (already and obj.revision == expected + 1)
    ):
        raise ValidationError(
            f"This {noun} changed since you opened it. Reload it and try again.",
            code="stale_revision",
        )


# --- small validation helpers -------------------------------------------------------------


def _error(message, code, field=None):
    error = ValidationError(message, code=code)
    return ValidationError({field: error}) if field else error


def need_reason(reason, field="reason"):
    reason = (reason or "").strip() if isinstance(reason, str) else ""
    if not reason:
        raise _error("Give the reason. It is kept in the history.", "required", field)
    return reason


def normalize_code(value, field="code"):
    """Trimmed upper-case ASCII code: letters, digits, '.', '_' and '-', at most 40."""
    code = value.strip().upper() if isinstance(value, str) else ""
    if not code:
        raise _error("Enter a code.", "required", field)
    if len(code) > 40 or not code.isascii() or not re.match(CODE_PATTERN, code):
        raise _error(
            "Use at most 40 letters, digits, '.', '_' or '-', starting with a letter or digit.",
            "invalid",
            field,
        )
    return code


def _text(value, field, limit, *, required=True, label="name"):
    text = value.strip() if isinstance(value, str) else ""
    if required and not text:
        raise _error(f"Enter a {label}.", "required", field)
    if len(text) > limit:
        raise _error(f"Use at most {limit} characters.", "invalid", field)
    return text


def _timezone(value, field="timezone"):
    name = value.strip() if isinstance(value, str) else ""
    if name not in zoneinfo.available_timezones():
        raise _error("Choose an IANA time zone, e.g. America/New_York.", "invalid", field)
    return name


def _temperature(value, field="nominal_temperature"):
    if isinstance(value, float) or isinstance(value, bool):
        raise _error("Give the temperature as an exact decimal.", "invalid", field)
    try:
        number = Decimal(str(value).strip()) if not isinstance(value, Decimal) else value
    except InvalidOperation:
        raise _error("Enter a number such as -160.00.", "invalid", field) from None
    if not number.is_finite() or number.as_tuple().exponent < -2:
        raise _error("Use at most two decimal places.", "invalid", field)
    if number < ABSOLUTE_ZERO or abs(number) >= 10000:
        raise _error("Enter a temperature from -273.15 to 9999.99 °C.", "invalid", field)
    return number.quantize(Decimal("0.01"))


def _whole(value, field, *, low, high, label):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise _error(f"{label} must be a whole number from {low} to {high}.", "invalid", field)
    return value


def _occurred(occurred_at, *, now, floor=None, field="occurred_at"):
    if occurred_at is None or tz.is_naive(occurred_at):
        raise _error("Enter when it happened.", "invalid_time", field)
    if occurred_at > now:
        raise _error("The time cannot be in the future.", "invalid_time", field)
    if floor is not None and occurred_at < floor:
        local = tz.localtime(floor).strftime("%Y-%m-%d %H:%M")
        raise _error(
            f"The time is earlier than the last recorded movement here ({local}). "
            "Events are recorded in the order they happened.",
            "invalid_time",
            field,
        )
    return occurred_at


def state(obj, fields):
    """JSON-safe snapshot of the named fields (foreign keys as their key) for audit events."""
    data = {}
    for name in fields:
        field = obj._meta.get_field(name)
        data[name] = getattr(obj, field.attname)
    return json.loads(json.dumps(data, cls=DjangoJSONEncoder))


UNIQUE_CONSTRAINTS = {
    "storage_site_code_key": ("code", "This code is already in use. Codes are never reused."),
    "storage_storageclass_code_key": ("code", "This code is already in use."),
    "storage_storageunit_code_key": (
        "code",
        "This code is already in use. Codes are never reused.",
    ),
    "storage_layoutnode_code_key": ("code", "This code is already in use. Codes are never reused."),
    "storage_box_code_key": ("code", "This code is already in use. Codes are never reused."),
    "storage_room_site_code": ("code", "This site already has a room with this code."),
    "storage_containertype_ver": ("code", "This version already exists."),
    "storage_template_ver": ("code", "This version already exists."),
    "storage_layoutnode_unit_slot": ("slot", "Another node already uses this slot."),
    "storage_layoutnode_parent_slot": ("slot", "Another node already uses this slot."),
    "storage_box_node_slot": ("slot", "Another box already uses this slot."),
}


def _save(obj, **kwargs):
    """Save inside a savepoint; map a known unique conflict to a field error."""
    try:
        with transaction.atomic():
            obj.save(**kwargs)
    except IntegrityError as exc:
        name = getattr(getattr(exc.__cause__, "diag", None), "constraint_name", None)
        if name in UNIQUE_CONSTRAINTS:
            field, message = UNIQUE_CONSTRAINTS[name]
            code = "occupied" if field == "slot" else "unique"
            raise _error(message, code, field) from exc
        raise


def _audit(actor, action, obj, *, before=None, after=None, reason=INITIAL_REASON):
    return record_event(
        actor=actor, action=action, obj=obj, before=before, after=after, reason=reason
    )


def _audit_many(actor, action, rows, reason):
    """Bulk version of record_event for container moves: rows are (obj, before, after)."""
    if not connection.in_atomic_block:
        raise RuntimeError("Audit events must share the domain transaction.")
    AuditEvent.objects.bulk_create(
        [
            AuditEvent(
                actor=actor,
                action=action,
                object_type=obj._meta.label_lower,
                object_id=str(obj.pk),
                before=before,
                after=after,
                reason=reason,
            )
            for obj, before, after in rows
        ],
        batch_size=500,
    )


SITE_FIELDS = ["code", "name", "timezone", "is_active"]
ROOM_FIELDS = ["site", "code", "name", "is_active"]
CLASS_FIELDS = ["code", "name", "description", "is_active"]
UNIT_FIELDS = [
    "code",
    "name",
    "room",
    "storage_class",
    "technology",
    "nominal_temperature",
    "temperature_unit",
    "template",
    "is_active",
]
NODE_FIELDS = [
    "code",
    "name",
    "kind",
    "unit",
    "parent",
    "slot",
    "child_slots",
    "box_slots",
    "depth",
    "path",
    "is_active",
]
BOX_FIELDS = ["code", "name", "node", "slot", "container_type", "is_active"]
GRID_FIELDS = ["code", "version", "name", "rows", "columns", "label_scheme", "fill_order", "origin"]
TEMPLATE_FIELDS = ["code", "version", "name", "definition", "is_active"]
RETIREMENT_FIELDS = ["is_active", "retired_at", "retired_by", "retirement_reason"]
POSITION_FIELDS = ["box", "row", "column", "ordinal", "label", "blocked", "block_reason"]


# --- paths (immutable snapshots recorded on move events) ----------------------------------


def _unit_part(unit):
    room = unit.room
    return {
        "site": {"id": str(room.site_id), "code": room.site.code},
        "room": {"id": str(room.pk), "code": room.code},
        "unit": {
            "id": str(unit.pk),
            "code": unit.code,
            "name": unit.name,
            "technology": unit.technology,
            "nominal_temperature": str(unit.nominal_temperature),
            "temperature_unit": unit.temperature_unit,
        },
    }


def node_chain(node, known=None):
    """Nodes from the top-level node down to `node`, preferring in-memory `known` rows."""
    known = known or {}
    chain = [node]
    while chain[0].parent_id:
        parent_id = chain[0].parent_id
        parent = known.get(parent_id) or LayoutNode.objects.get(pk=parent_id)
        chain.insert(0, parent)
    return chain


def build_path(*, unit, nodes, box=None, position=None, slot=None):
    path = _unit_part(unit)
    path["nodes"] = [
        {"id": str(n.pk), "code": n.code, "kind": n.kind, "slot": n.slot} for n in nodes
    ]
    codes = [unit.code, *(n.code for n in nodes)]
    if box is not None:
        path["box"] = {
            "id": str(box.pk),
            "code": box.code,
            "slot": box.slot,
            "container_type": str(box.container_type),
        }
        codes.append(box.code)
    if position is not None:
        path["position"] = {
            "id": str(position.pk),
            "label": position.label,
            "row": position.row,
            "column": position.column,
            "ordinal": position.ordinal,
        }
        codes.append(position.label)
    if slot is not None:
        path["slot"] = slot
    path["display"] = " / ".join(codes)
    return path


def position_path(position):
    """The full recorded path of one position, as stored on move events."""
    box = Box.objects.select_related("container_type", "node").get(pk=position.box_id)
    nodes = node_chain(box.node)
    unit = StorageUnit.objects.select_related("room__site").get(pk=box.node.unit_id)
    return build_path(unit=unit, nodes=nodes, box=box, position=position)


def _unit(pk):
    return StorageUnit.objects.select_related("room__site").get(pk=pk)


def container_location(obj):
    """Current recorded-style path of a box or node, with its slot."""
    if isinstance(obj, Box):
        box = Box.objects.select_related("container_type").get(pk=obj.pk)
        chain = node_chain(LayoutNode.objects.get(pk=box.node_id))
        return build_path(unit=_unit(chain[0].unit_id), nodes=chain, box=box, slot=box.slot)
    chain = node_chain(obj)
    return build_path(unit=_unit(obj.unit_id), nodes=chain, slot=obj.slot)


# --- definitions ----------------------------------------------------------------------------


@operation()
def create_site(*, actor, code, name, timezone):
    require_role(actor, *MANAGE_ROLES)
    lock_gate()
    site = Site(
        code=normalize_code(code),
        name=_text(name, "name", 160),
        timezone=_timezone(timezone),
        created_by=actor,
    )
    _save(site)
    _bump_gate()
    _audit(actor, "storage.site_created", site, after=state(site, SITE_FIELDS))
    return site


@operation()
def create_room(*, actor, site_id, code, name):
    require_role(actor, *MANAGE_ROLES)
    lock_gate()
    site = Site.objects.select_for_update().get(pk=site_id)
    if not site.is_active:
        raise _error("This site is not active.", "inactive_location", "site")
    room = Room(
        site=site, code=normalize_code(code), name=_text(name, "name", 160), created_by=actor
    )
    _save(room)
    _bump_gate()
    _audit(actor, "storage.room_created", room, after=state(room, ROOM_FIELDS))
    return room


def _create_unit(
    *, actor, room_id, code, name, storage_class_id, technology, nominal_temperature, template=None
):
    room = Room.objects.select_for_update().select_related("site").get(pk=room_id)
    storage_class = StorageClass.objects.select_for_update().get(pk=storage_class_id)
    if not room.is_active:
        raise _error("This room is not active.", "inactive_location", "room")
    if not storage_class.is_active:
        raise _error(
            "This storage class is deactivated; choose an active one.",
            "inactive_location",
            "storage_class",
        )
    if technology not in StorageUnit.Technology.values:
        raise _error("Choose a storage technology.", "invalid", "technology")
    unit = StorageUnit(
        code=normalize_code(code),
        name=_text(name, "name", 160),
        room=room,
        storage_class=storage_class,
        technology=technology,
        nominal_temperature=_temperature(nominal_temperature),
        temperature_unit=TEMPERATURE_UNIT,
        template=template,
        created_by=actor,
    )
    _save(unit)
    _bump_gate()
    _audit(actor, "storage.unit_created", unit, after=state(unit, UNIT_FIELDS))
    return unit


@operation()
def create_unit(*, actor, room_id, code, name, storage_class_id, technology, nominal_temperature):
    require_role(actor, *MANAGE_ROLES)
    lock_gate()
    return _create_unit(
        actor=actor,
        room_id=room_id,
        code=code,
        name=name,
        storage_class_id=storage_class_id,
        technology=technology,
        nominal_temperature=nominal_temperature,
    )


def _active_chain(nodes, unit, field):
    if not unit.is_active:
        raise _error(f"Storage unit {unit.code} is retired.", "inactive_location", field)
    for node in nodes:
        if not node.is_active:
            raise _error(f"{node.code} is retired.", "inactive_location", field)


@operation()
def create_node(*, actor, unit_id, parent_id, code, name, kind, slot, child_slots, box_slots):
    require_role(actor, *MANAGE_ROLES)
    lock_gate()
    unit = StorageUnit.objects.select_for_update().get(pk=unit_id)
    parent = None
    if parent_id is not None:
        parent = LayoutNode.objects.select_for_update().get(pk=parent_id)
    if kind not in CHILD_KINDS:
        raise _error("Choose shelf, rack, tower or canister.", "invalid", "kind")
    child_slots = _whole(child_slots, "child_slots", low=0, high=MAX_SLOTS, label="Node slots")
    box_slots = _whole(box_slots, "box_slots", low=0, high=MAX_SLOTS, label="Box slots")
    if (child_slots > 0) == (box_slots > 0):
        raise _error(
            "A node holds either other nodes or boxes: give one of node slots or box slots.",
            "invalid",
            "box_slots",
        )
    if parent is None:
        _active_chain([], unit, "unit")
        slot = _whole(slot, "slot", low=1, high=recipes.MAX_TOP_LEVEL_SLOT, label="Slot")
        depth = 1
        taken = LayoutNode.objects.filter(unit=unit, parent__isnull=True, slot=slot, is_active=True)
    else:
        if parent.unit_id != unit.pk:
            raise _error("The parent belongs to another storage unit.", "invalid", "parent")
        _active_chain(node_chain(parent), unit, "parent")
        if not parent.child_slots:
            raise _error(f"{parent.code} holds boxes, not other nodes.", "invalid", "parent")
        if kind not in CHILD_KINDS[parent.kind]:
            raise _error(f"A {parent.kind} cannot hold a {kind}.", "invalid", "kind")
        if parent.depth + 1 > MAX_DEPTH:
            raise _error(f"Layouts are at most {MAX_DEPTH} levels deep.", "invalid", "parent")
        slot = _whole(slot, "slot", low=1, high=parent.child_slots, label="Slot")
        depth = parent.depth + 1
        taken = LayoutNode.objects.filter(parent=parent, slot=slot, is_active=True)
    if taken.exists():
        raise _error("Another node already uses this slot.", "occupied", "slot")
    code = normalize_code(code)
    node = LayoutNode(
        code=code,
        name=_text(name, "name", 160),
        kind=kind,
        unit=unit,
        parent=parent,
        slot=slot,
        child_slots=child_slots,
        box_slots=box_slots,
        depth=depth,
        path=f"{parent.path}/{code}" if parent else code,
        created_by=actor,
    )
    _save(node)
    _bump_gate()
    _audit(actor, "storage.node_created", node, after=state(node, NODE_FIELDS))
    return node


def grid_cells(container_type):
    """(row, column, ordinal, label) for every cell; 1-based from the labelled top-left corner."""
    rows, columns = container_type.rows, container_type.columns
    column_major = container_type.fill_order == ContainerType.FillOrder.COLUMN_MAJOR
    cells = []
    for row in range(1, rows + 1):
        for column in range(1, columns + 1):
            ordinal = (column - 1) * rows + row if column_major else (row - 1) * columns + column
            if container_type.label_scheme == ContainerType.LabelScheme.NUMERIC:
                label = str(ordinal)
            else:
                label = f"{row_letters(row)}{column}"
            cells.append((row, column, ordinal, label))
    return cells


def row_letters(number):
    """1 -> A, 26 -> Z, 27 -> AA (spreadsheet-style row letters)."""
    letters = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


@operation()
def create_box(*, actor, node_id, code, name, slot, container_type_id):
    require_role(actor, *MANAGE_ROLES)
    lock_gate()
    return _create_box(
        actor=actor,
        node_id=node_id,
        code=code,
        name=name,
        slot=slot,
        container_type_id=container_type_id,
    )


def _create_box(*, actor, node_id, code, name, slot, container_type_id):
    unit_id = LayoutNode.objects.values_list("unit_id", flat=True).get(pk=node_id)
    unit = StorageUnit.objects.select_for_update().get(pk=unit_id)
    node = LayoutNode.objects.select_for_update().get(pk=node_id)
    grid = ContainerType.objects.select_for_update().get(pk=container_type_id)
    _active_chain(node_chain(node), unit, "node")
    if not node.box_slots:
        raise _error(f"{node.code} holds other nodes, not boxes.", "invalid", "node")
    if not grid.is_active:
        raise _error(
            "This container type is deactivated; choose an active one.",
            "inactive_location",
            "container_type",
        )
    slot = _whole(slot, "slot", low=1, high=node.box_slots, label="Slot")
    if Box.objects.filter(node=node, slot=slot, is_active=True).exists():
        raise _error("Another box already uses this slot.", "occupied", "slot")
    box = Box(
        code=normalize_code(code),
        name=_text(name, "name", 160, required=False) or normalize_code(code),
        node=node,
        slot=slot,
        container_type=grid,
        created_by=actor,
    )
    _save(box)
    now = tz.now()
    Position.objects.bulk_create(
        [
            Position(
                box=box,
                row=row,
                column=column,
                ordinal=ordinal,
                label=label,
                created_by=actor,
                created_at=now,
            )
            for row, column, ordinal, label in grid_cells(grid)
        ],
        batch_size=1000,
    )
    _bump_gate()
    _audit(
        actor,
        "storage.box_created",
        box,
        after={
            **state(box, BOX_FIELDS),
            "grid": state(grid, GRID_FIELDS),
            "positions": grid.capacity,
        },
    )
    return box


# --- catalogues -----------------------------------------------------------------------------

CATALOGUES = {
    "class": StorageClass,
    "container_type": ContainerType,
    "template": StorageTemplate,
}
CATALOGUE_CREATE_FIELDS = {
    "class": {"code", "name", "description"},
    "container_type": {"code", "name", "rows", "columns", "label_scheme", "fill_order"},
    "template": {"code", "name", "definition"},
}
CATALOGUE_EDIT_FIELDS = {
    "class": {"name", "description", "is_active"},
    "container_type": {"name", "is_active", "rows", "columns", "label_scheme", "fill_order"},
    "template": {"name", "is_active", "definition"},
}
CATALOGUE_AUDIT_FIELDS = {
    "class": CLASS_FIELDS,
    "container_type": [*GRID_FIELDS, "is_active"],
    "template": TEMPLATE_FIELDS,
}
GEOMETRY = ("rows", "columns", "label_scheme", "fill_order")


def _unknown_keys(values, allowed):
    if not isinstance(values, dict):
        raise _error("Values must be a mapping of field names.", "invalid")
    unknown = set(values) - allowed
    if unknown:
        raise _error(
            "These fields cannot be set here: " + ", ".join(sorted(unknown)) + ".", "invalid"
        )


def _catalogue_values(kind, values, obj):
    """Validate and apply catalogue values to `obj` (in memory)."""
    if "name" in values:
        obj.name = _text(values["name"], "name", 160)
    if kind == "class" and "description" in values:
        obj.description = _text(values["description"] or "", "description", 500, required=False)
    if "is_active" in values:
        if not isinstance(values["is_active"], bool):
            raise _error("Choose active or not.", "invalid", "is_active")
        obj.is_active = values["is_active"]
    if kind == "container_type":
        for side in ("rows", "columns"):
            if side in values:
                setattr(obj, side, _whole(values[side], side, low=1, high=100, label=side.title()))
        if "label_scheme" in values:
            if values["label_scheme"] not in ContainerType.LabelScheme.values:
                raise _error("Choose a labelling scheme.", "invalid", "label_scheme")
            obj.label_scheme = values["label_scheme"]
        if "fill_order" in values:
            if values["fill_order"] not in ContainerType.FillOrder.values:
                raise _error("Choose a fill order.", "invalid", "fill_order")
            obj.fill_order = values["fill_order"]
    if kind == "template" and "definition" in values:
        obj.definition = recipes.validate_definition(values["definition"])


@operation()
def save_storage_catalogue(*, actor, kind, object_id, expected_revision, values, reason):
    """Create (object_id None) or edit a storage class, container type or template.

    Creating a container type or template with an existing code adds the next version. A used
    version (referenced by a box or a unit) keeps its geometry or recipe; add a version instead.
    """
    require_role(actor, *MANAGE_ROLES)
    if kind not in CATALOGUES:
        raise _error("Unknown catalogue.", "invalid")
    model = CATALOGUES[kind]
    lock_gate()
    if object_id is None:
        _unknown_keys(values, CATALOGUE_CREATE_FIELDS[kind])
        reason = (reason or "").strip() if isinstance(reason, str) else ""
        reason = reason or INITIAL_REASON
        code = normalize_code(values.get("code"))
        obj = model(code=code, created_by=actor)
        if kind == "container_type":
            for name in ("rows", "columns", "label_scheme", "fill_order", "name"):
                if name not in values:
                    raise _error("This field is required.", "required", name)
        if kind == "template" and "definition" not in values:
            raise _error("This field is required.", "required", "definition")
        if "name" not in values:
            raise _error("Enter a name.", "required", "name")
        _catalogue_values(kind, values, obj)
        if kind in ("container_type", "template"):
            latest = model.objects.filter(code=code).aggregate(v=Max("version"))["v"]
            obj.version = (latest or 0) + 1
        _save(obj)
        _audit(
            actor,
            f"storage.{kind}_created",
            obj,
            after=state(obj, CATALOGUE_AUDIT_FIELDS[kind]),
            reason=reason,
        )
        return obj
    _unknown_keys(values, CATALOGUE_EDIT_FIELDS[kind])
    reason = need_reason(reason)
    obj = model.objects.select_for_update().get(pk=object_id)
    _check_revision(obj, expected_revision, "catalogue entry")
    before = state(obj, CATALOGUE_AUDIT_FIELDS[kind])
    _catalogue_values(kind, values, obj)
    after = state(obj, CATALOGUE_AUDIT_FIELDS[kind])
    if after == before:
        raise _error("Nothing to save: the values are unchanged.", "invalid_transition")
    if kind == "container_type" and any(before[g] != after[g] for g in GEOMETRY):
        if obj.boxes.exists():
            raise _error(
                "Boxes use this container type, so its grid cannot change. "
                "Add a new version instead.",
                "invalid_transition",
            )
    if kind == "template" and before["definition"] != after["definition"]:
        if obj.units.exists():
            raise _error(
                "Units were created from this template version, so its layout cannot change. "
                "Add a new version instead.",
                "invalid_transition",
            )
    _bump(obj)
    _save(obj)
    _audit(actor, f"storage.{kind}_updated", obj, before=before, after=after, reason=reason)
    return obj


# --- reasoned edits of physical definitions ---------------------------------------------------

DEFINITIONS = {
    "site": (Site, SITE_FIELDS, {"name", "timezone"}),
    "room": (Room, ROOM_FIELDS, {"name"}),
    "unit": (StorageUnit, UNIT_FIELDS, {"name", "storage_class_id", "nominal_temperature"}),
    "node": (LayoutNode, NODE_FIELDS, {"name"}),
    "box": (Box, BOX_FIELDS, {"name"}),
}


@operation()
def edit_storage_definition(*, actor, kind, object_id, expected_revision, values, reason):
    """Edit names, a site's time zone, a unit's storage class or nominal temperature.

    Codes, parents, slots and grids never change here: moves have their own service.
    """
    require_role(actor, *MANAGE_ROLES)
    if kind not in DEFINITIONS:
        raise _error("Unknown storage definition.", "invalid")
    model, fields, allowed = DEFINITIONS[kind]
    _unknown_keys(values, allowed)
    reason = need_reason(reason)
    lock_gate()
    obj = model.objects.select_for_update().get(pk=object_id)
    _check_revision(obj, expected_revision, kind)
    if not obj.is_active:
        raise _error("A retired definition cannot change.", "inactive_location")
    before = state(obj, fields)
    if "name" in values:
        obj.name = _text(values["name"], "name", 160)
    if "timezone" in values:
        obj.timezone = _timezone(values["timezone"])
    if "storage_class_id" in values and values["storage_class_id"] != obj.storage_class_id:
        storage_class = StorageClass.objects.select_for_update().get(pk=values["storage_class_id"])
        if not storage_class.is_active:
            raise _error(
                "This storage class is deactivated; choose an active one.",
                "inactive_location",
                "storage_class",
            )
        obj.storage_class = storage_class
    if "nominal_temperature" in values:
        obj.nominal_temperature = _temperature(values["nominal_temperature"])
    after = state(obj, fields)
    if after == before:
        raise _error("Nothing to save: the values are unchanged.", "invalid_transition")
    _bump(obj)
    obj.save()
    _audit(actor, f"storage.{kind}_updated", obj, before=before, after=after, reason=reason)
    return obj


# --- templates ------------------------------------------------------------------------------


@operation()
def instantiate_template(*, actor, template_id, room_id, unit_code, unit_name):
    """Create a unit, its nodes and boxes (with positions) from a template, all or none."""
    require_role(actor, *MANAGE_ROLES)
    lock_gate()
    template = StorageTemplate.objects.select_for_update().get(pk=template_id)
    if not template.is_active:
        raise _error("This template is deactivated.", "inactive_location", "template")
    recipe = recipes.validate_definition(template.definition)
    unit_code = normalize_code(unit_code, "unit_code")
    storage_class = StorageClass.objects.get(code=recipe["storage_class"])
    unit = _create_unit(
        actor=actor,
        room_id=room_id,
        code=unit_code,
        name=unit_name,
        storage_class_id=storage_class.pk,
        technology=recipe["technology"],
        nominal_temperature=Decimal(recipe["nominal_temperature"]),
        template=template,
    )
    grids = {}

    def generated(suffix):
        code = f"{unit_code}-{suffix}"
        if len(code) > 40:
            raise _error(
                f"Generated code {code} is longer than 40 characters; use a shorter unit code.",
                "invalid",
                "unit_code",
            )
        return code

    def build(items, parent):
        for item in items:
            node = create_node(
                actor=actor,
                unit_id=unit.pk,
                parent_id=parent.pk if parent else None,
                code=generated(item["suffix"]),
                name=item["name"] or f"{item['kind'].title()} {item['slot']}",
                kind=item["kind"],
                slot=item["slot"],
                child_slots=item["child_slots"],
                box_slots=item["box_slots"],
            )
            build(item["children"], node)
            for spec in item["boxes"]:
                key = (spec["container_type"]["code"], spec["container_type"]["version"])
                if key not in grids:
                    grids[key] = ContainerType.objects.get(code=key[0], version=key[1])
                _create_box(
                    actor=actor,
                    node_id=node.pk,
                    code=generated(spec["suffix"]),
                    name=spec["name"],
                    slot=spec["slot"],
                    container_type_id=grids[key].pk,
                )

    build(recipe["nodes"], None)
    _audit(
        actor,
        "storage.template_applied",
        unit,
        after={"template": str(template.pk), "code": template.code, "version": template.version},
        reason=f"Created from template {template.code} v{template.version}",
    )
    return unit


# --- positions and retirement ---------------------------------------------------------------


def _lock_box_context(box_id):
    """Lock unit -> nodes (top-down) -> box for a box; return (unit, chain, box)."""
    node_id, unit_id = Box.objects.values_list("node_id", "node__unit_id").get(pk=box_id)
    unit = StorageUnit.objects.select_for_update().select_related("room__site").get(pk=unit_id)
    chain = node_chain(LayoutNode.objects.get(pk=node_id))
    locked = {
        n.pk: n for n in LayoutNode.objects.select_for_update().filter(pk__in=[n.pk for n in chain])
    }
    chain = [locked[n.pk] for n in chain]
    box = Box.objects.select_for_update().select_related("container_type").get(pk=box_id)
    return unit, chain, box


@operation()
def set_position_blocked(*, actor, position_id, expected_revision, blocked, reason):
    require_role(actor, *MANAGE_ROLES)
    reason = need_reason(reason)
    lock_gate()
    box_id = Position.objects.values_list("box_id", flat=True).get(pk=position_id)
    unit, chain, box = _lock_box_context(box_id)
    position = Position.objects.select_for_update().get(pk=position_id)
    _check_revision(position, expected_revision, "position")
    _active_chain(chain, unit, None)
    if not box.is_active:
        raise _error(f"Box {box.code} is retired.", "inactive_location")
    if position.blocked == bool(blocked):
        raise _error(
            "The position is already blocked." if blocked else "The position is not blocked.",
            "invalid_transition",
        )
    if blocked and Occupancy.objects.select_for_update().filter(position=position).exists():
        raise _error("The position is occupied. Move the item out before blocking it.", "occupied")
    before = state(position, POSITION_FIELDS)
    position.blocked = bool(blocked)
    position.block_reason = reason if blocked else ""
    _bump(position)
    position.save(update_fields=["blocked", "block_reason", "revision"])
    _bump_gate()
    _audit(
        actor,
        "storage.position_blocked" if blocked else "storage.position_unblocked",
        position,
        before=before,
        after=state(position, POSITION_FIELDS),
        reason=reason,
    )
    return position


@operation()
def retire_storage(*, actor, kind, object_id, expected_revision, reason):
    """Retire an empty box, or a node or unit with no active children. Records stay."""
    require_role(actor, *MANAGE_ROLES)
    reason = need_reason(reason)
    if kind not in ("unit", "node", "box"):
        raise _error("Only units, nodes and boxes are retired.", "invalid")
    lock_gate()
    if kind == "box":
        _, _, obj = _lock_box_context(object_id)
        fields = BOX_FIELDS
        list(Position.objects.select_for_update().filter(box=obj).order_by("pk").values("pk"))
        occupied = Occupancy.objects.select_for_update().filter(position__box=obj).exists()
        if obj.is_active and occupied:
            raise _error("The box still holds items. Move or dispose of them first.", "occupied")
    elif kind == "node":
        unit_id = LayoutNode.objects.values_list("unit_id", flat=True).get(pk=object_id)
        StorageUnit.objects.select_for_update().get(pk=unit_id)
        obj = LayoutNode.objects.select_for_update().get(pk=object_id)
        fields = NODE_FIELDS
        if obj.is_active and (
            obj.children.filter(is_active=True).exists()
            or obj.boxes.filter(is_active=True).exists()
        ):
            raise _error("Retire or move the nodes and boxes it holds first.", "invalid_transition")
    else:
        obj = StorageUnit.objects.select_for_update().get(pk=object_id)
        fields = UNIT_FIELDS
        if obj.is_active and obj.nodes.filter(is_active=True).exists():
            raise _error(
                "Retire or move the unit's racks, towers and shelves first.",
                "invalid_transition",
            )
    _check_revision(obj, expected_revision, kind)
    if not obj.is_active:
        raise _error("This is already retired.", "invalid_transition")
    before = state(obj, [*fields, *RETIREMENT_FIELDS])
    obj.is_active = False
    obj.retired_at = tz.now()
    obj.retired_by = actor
    obj.retirement_reason = reason
    _bump(obj)
    obj.save()
    _bump_gate()
    _audit(
        actor,
        f"storage.{kind}_retired",
        obj,
        before=before,
        after=state(obj, [*fields, *RETIREMENT_FIELDS]),
        reason=reason,
    )
    return obj


# --- container moves ------------------------------------------------------------------------


def _latest_container_move(node_ids=(), box_ids=()):
    return MoveEvent.objects.filter(
        Q(node_id__in=list(node_ids)) | Q(box_id__in=list(box_ids))
    ).aggregate(latest=Max("occurred_at"))["latest"]


def _latest_item_movement(box_ids):
    """Latest recorded movement of any item into or out of these boxes, including items that
    have since left (a box cannot be recorded as moved before its last recorded check-out)."""
    box_ids = list(box_ids)
    if not box_ids:
        return None
    return MoveEvent.objects.filter(
        Q(members__from_position__box_id__in=box_ids) | Q(members__to_position__box_id__in=box_ids)
    ).aggregate(latest=Max("occurred_at"))["latest"]


@operation()
def move_container(
    *,
    actor,
    kind,
    object_id,
    expected_revision,
    expected_gate_revision,
    target_kind,
    target_id,
    target_slot,
    occurred_at,
    reason,
    corrects_id=None,
):
    """Move a box to a node's box slot, or a node (with everything under it) to another node
    or to the top level of a unit. One MoveEvent, one member per occupied position."""
    require_role(actor, *(MANAGE_ROLES if corrects_id is not None else OPERATE_ROLES))
    reason = need_reason(reason)
    if kind not in ("box", "node"):
        raise _error("Only boxes and nodes move as containers.", "invalid")
    allowed_targets = {"box": ("node",), "node": ("node", "unit")}[kind]
    if target_kind not in allowed_targets:
        raise _error("Choose where to move it.", "invalid", "target")
    object_id, target_id = _id(object_id), _id(target_id, "target")
    corrects_id = _id(corrects_id, "corrects")
    gate = lock_gate()
    if expected_gate_revision != gate.revision:
        raise _error(
            "Storage changed since you opened this form (another placement or move was "
            "recorded). Reload it and check the contents before moving.",
            "stale_revision",
        )
    now = tz.now()

    # Discover the rows involved (unlocked), then lock them in contract order.
    if kind == "box":
        source_unit_id = Box.objects.values_list("node__unit_id", flat=True).get(pk=object_id)
    else:
        source_unit_id = LayoutNode.objects.values_list("unit_id", flat=True).get(pk=object_id)
    if target_kind == "unit":
        target_unit_id = StorageUnit.objects.values_list("pk", flat=True).get(pk=target_id)
    else:
        target_unit_id = LayoutNode.objects.values_list("unit_id", flat=True).get(pk=target_id)
    units = {
        u.pk: u
        for u in StorageUnit.objects.select_for_update()
        .select_related("room__site")
        .filter(pk__in={source_unit_id, target_unit_id})
        .order_by("pk")
    }
    if kind == "box":
        source_parent = LayoutNode.objects.get(pk=Box.objects.get(pk=object_id).node_id)
        subtree_ids = []
        source_chain_ids = [n.pk for n in node_chain(source_parent)]
    else:
        moved = LayoutNode.objects.get(pk=object_id)
        subtree_ids = list(
            LayoutNode.objects.filter(
                Q(pk=moved.pk) | Q(path__startswith=moved.path + "/")
            ).values_list("pk", flat=True)
        )
        source_chain_ids = [n.pk for n in node_chain(moved)]
    target_chain_ids = []
    if target_kind == "node":
        target_chain_ids = [n.pk for n in node_chain(LayoutNode.objects.get(pk=target_id))]
    nodes = {
        n.pk: n
        for n in LayoutNode.objects.select_for_update()
        .filter(pk__in={*subtree_ids, *source_chain_ids, *target_chain_ids})
        .order_by("pk")
    }
    if kind == "box":
        boxes = list(
            Box.objects.select_for_update().select_related("container_type").filter(pk=object_id)
        )
    else:
        boxes = list(
            Box.objects.select_for_update()
            .select_related("container_type")
            .filter(node_id__in=subtree_ids)
            .order_by("pk")
        )
    box_ids = [b.pk for b in boxes]
    positions = {
        p.pk: p
        for p in Position.objects.select_for_update().filter(box_id__in=box_ids).order_by("pk")
    }
    claims = list(
        Occupancy.objects.select_for_update().filter(position_id__in=positions).order_by("pk")
    )
    obj = boxes[0] if kind == "box" else nodes[object_id]
    _check_revision(obj, expected_revision, kind)
    if not obj.is_active:
        raise _error(f"{obj.code} is retired and cannot move.", "inactive_location")

    # Validate the target.
    target_unit = units[target_unit_id]
    target_node = nodes[target_id] if target_kind == "node" else None
    target_chain = [nodes[pk] for pk in target_chain_ids]
    _active_chain(target_chain, target_unit, "target")
    if kind == "box":
        if not target_node.box_slots:
            raise _error(f"{target_node.code} holds other nodes, not boxes.", "invalid", "target")
        slot = _whole(target_slot, "target_slot", low=1, high=target_node.box_slots, label="Slot")
        if obj.node_id == target_node.pk and obj.slot == slot:
            raise _error("The box is already in this slot.", "invalid_transition", "target_slot")
        if (
            Box.objects.filter(node=target_node, slot=slot, is_active=True)
            .exclude(pk=obj.pk)
            .exists()
        ):
            raise _error("Another box already uses this slot.", "occupied", "target_slot")
    else:
        subtree = [nodes[pk] for pk in subtree_ids]
        if target_node is not None:
            if target_node.pk in nodes and target_node.pk in subtree_ids:
                raise _error("A node cannot move into itself.", "invalid", "target")
            if not target_node.child_slots:
                raise _error(
                    f"{target_node.code} holds boxes, not other nodes.", "invalid", "target"
                )
            if obj.kind not in CHILD_KINDS[target_node.kind]:
                raise _error(f"A {target_node.kind} cannot hold a {obj.kind}.", "invalid", "target")
            slot = _whole(
                target_slot, "target_slot", low=1, high=target_node.child_slots, label="Slot"
            )
            new_depth = target_node.depth + 1
            siblings = LayoutNode.objects.filter(parent=target_node, slot=slot, is_active=True)
        else:
            slot = _whole(
                target_slot, "target_slot", low=1, high=recipes.MAX_TOP_LEVEL_SLOT, label="Slot"
            )
            new_depth = 1
            siblings = LayoutNode.objects.filter(
                unit=target_unit, parent__isnull=True, slot=slot, is_active=True
            )
        if (
            obj.parent_id == (target_node.pk if target_node else None)
            and obj.unit_id == target_unit.pk
            and obj.slot == slot
        ):
            raise _error("The node is already in this slot.", "invalid_transition", "target_slot")
        if siblings.exclude(pk=obj.pk).exists():
            raise _error("Another node already uses this slot.", "occupied", "target_slot")
        deepest = max(n.depth for n in subtree) - obj.depth + new_depth
        if deepest > MAX_DEPTH:
            raise _error(
                f"Layouts are at most {MAX_DEPTH} levels deep; this move would make {deepest}.",
                "invalid",
                "target",
            )

    # Chronology: not before any affected item's or container's latest recorded movement,
    # including movements of items that have since left the moved boxes.
    floors = [c.last_occurred_at for c in claims]
    for latest in (
        _latest_container_move({*source_chain_ids, *subtree_ids, *target_chain_ids}, box_ids),
        _latest_item_movement(box_ids),
    ):
        if latest:
            floors.append(latest)
    _occurred(occurred_at, now=now, floor=max(floors) if floors else None)

    corrected = None
    if corrects_id is not None:
        corrected = MoveEvent.objects.select_for_update().get(pk=corrects_id)
        same_target = (kind == "box" and corrected.box_id == obj.pk) or (
            kind == "node" and corrected.node_id == obj.pk
        )
        if not same_target:
            raise _error(
                "A correction must be a move of the same box or node.", "invalid", "corrects"
            )
        if MoveEvent.objects.filter(corrects=corrected).exists():
            raise _error(
                "That move was already corrected. Correct the newer record.",
                "invalid_transition",
                "corrects",
            )

    # Snapshot "before" paths.
    def box_paths(box_list, known):
        result = {}
        for box in box_list:
            chain = node_chain(known[box.node_id] if box.node_id in known else box.node, known)
            unit = units.get(chain[0].unit_id) or _unit(chain[0].unit_id)
            result[box.pk] = (unit, chain)
        return result

    def container_path(known):
        if kind == "box":
            chain = node_chain(nodes.get(obj.node_id) or obj.node, known)
            unit = units.get(chain[0].unit_id) or _unit(chain[0].unit_id)
            return build_path(unit=unit, nodes=chain, box=obj, slot=obj.slot)
        chain = node_chain(obj, known)
        return build_path(unit=units[obj.unit_id], nodes=chain, slot=obj.slot)

    before_boxes = box_paths(boxes, nodes)
    from_paths = {
        c.pk: build_path(
            unit=before_boxes[positions[c.position_id].box_id][0],
            nodes=before_boxes[positions[c.position_id].box_id][1],
            box=next(b for b in boxes if b.pk == positions[c.position_id].box_id),
            position=positions[c.position_id],
        )
        for c in claims
    }
    from_container = container_path(nodes)
    audit_rows = []
    carried_boxes = []

    # Apply.
    if kind == "box":
        before = state(obj, BOX_FIELDS)
        obj.node = target_node
        obj.slot = slot
        _bump(obj)
        obj.save(update_fields=["node", "slot", "revision"])
        audit_rows.append((obj, before, state(obj, BOX_FIELDS)))
    else:
        old_prefix = obj.path
        before = state(obj, NODE_FIELDS)
        obj.parent = target_node
        obj.unit = target_unit
        obj.slot = slot
        obj.depth = new_depth
        obj.path = f"{target_node.path}/{obj.code}" if target_node else obj.code
        _bump(obj)
        obj.save(update_fields=["parent", "unit", "slot", "depth", "path", "revision"])
        audit_rows.append((obj, before, state(obj, NODE_FIELDS)))
        depth_change = new_depth - before["depth"]
        for node in sorted(subtree, key=lambda n: n.depth):
            if node.pk == obj.pk:
                continue
            node_before = state(node, NODE_FIELDS)
            node.unit = target_unit
            node.depth += depth_change
            node.path = obj.path + node.path[len(old_prefix) :]
            _bump(node)
            node.save(update_fields=["unit", "depth", "path", "revision"])
            audit_rows.append((node, node_before, state(node, NODE_FIELDS)))
        for box in boxes:
            unit, chain = before_boxes[box.pk]
            box_before = {"location": build_path(unit=unit, nodes=chain, box=box)["display"]}
            _bump(box)
            box.save(update_fields=["revision"])
            carried_boxes.append((box, box_before))

    after_boxes = box_paths(boxes, nodes)
    for box, box_before in carried_boxes:
        unit, chain = after_boxes[box.pk]
        location = build_path(unit=unit, nodes=chain, box=box)["display"]
        audit_rows.append((box, box_before, {"location": location}))
    to_paths = {
        c.pk: build_path(
            unit=after_boxes[positions[c.position_id].box_id][0],
            nodes=after_boxes[positions[c.position_id].box_id][1],
            box=next(b for b in boxes if b.pk == positions[c.position_id].box_id),
            position=positions[c.position_id],
        )
        for c in claims
    }
    event = MoveEvent.objects.create(
        kind=MoveEvent.Kind.CORRECTION if corrected else MoveEvent.Kind.CONTAINER_MOVE,
        actor=actor,
        occurred_at=occurred_at,
        recorded_at=now,
        reason=reason,
        node=obj if kind == "node" else None,
        box=obj if kind == "box" else None,
        from_path=from_container,
        to_path=container_path(nodes),
        corrects=corrected,
    )
    MoveMember.objects.bulk_create(
        [
            MoveMember(
                event=event,
                occupancy=claim,
                from_position_id=claim.position_id,
                to_position_id=claim.position_id,
                from_path=from_paths[claim.pk],
                to_path=to_paths[claim.pk],
            )
            for claim in claims
        ],
        batch_size=500,
    )
    occupancy_rows = []
    for claim in claims:
        claim_before = {"revision": claim.revision, "path": from_paths[claim.pk]["display"]}
        claim.last_occurred_at = occurred_at
        _bump(claim)
        occupancy_rows.append(
            (
                claim,
                claim_before,
                {
                    "revision": claim.revision,
                    "path": to_paths[claim.pk]["display"],
                    "event": str(event.pk),
                },
            )
        )
    Occupancy.objects.bulk_update(claims, ["last_occurred_at", "revision"], batch_size=500)
    _bump_gate()
    event_ref = {"event": str(event.pk)}
    moved_obj, moved_before, moved_after = audit_rows[0]
    _audit(
        actor,
        "storage.move_corrected" if corrected else f"storage.{kind}_moved",
        moved_obj,
        before={**moved_before, "location": from_container["display"]},
        after={**moved_after, "location": event.to_path["display"], **event_ref},
        reason=reason,
    )
    _audit_many(
        actor,
        "storage.carried_by_move",
        [(o, b, {**a, **event_ref}) for o, b, a in audit_rows[1:]],
        reason,
    )
    _audit_many(actor, "storage.occupancy_moved_with_container", occupancy_rows, reason)
    return event


# --- internal claim helpers for the specimens app ----------------------------------------------


@operation()
def new_occupancy(*, actor, occurred_at):
    """A new, unplaced storage key. Only inside a specimens operation that holds the gate."""
    _require_gate()
    require_role(actor, *OPERATE_ROLES)
    _occurred(occurred_at, now=tz.now())
    occupancy = Occupancy.objects.create(
        position=None, last_occurred_at=occurred_at, created_by=actor
    )
    _audit(
        actor,
        "storage.occupancy_created",
        occupancy,
        after={"position": None, "revision": occupancy.revision},
    )
    return occupancy


def _lock_target(position_id, field):
    """Lock unit -> chain -> box of a target position; return (box, chain, unit)."""
    try:
        box_id = Position.objects.values_list("box_id", flat=True).get(pk=position_id)
    except Position.DoesNotExist:
        raise _error("Choose an existing position.", "invalid", field) from None
    unit, chain, box = _lock_box_context(box_id)
    return unit, chain, box


@operation()
def change_occupancy(
    *,
    actor,
    occupancy_id,
    expected_revision,
    position_id,
    kind,
    occurred_at,
    reason,
    corrects_id=None,
):
    """Move one item's claim: place, relocate, check out, check in, dispose or correct.

    Internal to a specimens operation (gate already held). The specimens service owns
    status, hold and approval checks; this validates location, claim and chronology only.
    """
    _require_gate()
    # Corrections rewrite the meaning of recorded history: managers and admins only.
    correcting = kind == "correction" or corrects_id is not None
    require_role(actor, *(MANAGE_ROLES if correcting else OPERATE_ROLES))
    reason = need_reason(reason)
    if kind not in ITEM_MOVE_KINDS:
        raise _error("Unknown movement kind.", "invalid_transition")
    if (kind == "correction") != (corrects_id is not None):
        raise _error(
            "A correction names the movement it corrects, and only a correction does.",
            "invalid_transition",
        )
    occupancy_id, position_id = _id(occupancy_id), _id(position_id, "position_id")
    corrects_id = _id(corrects_id, "corrects")
    now = tz.now()
    source_id = Occupancy.objects.values_list("position_id", flat=True).get(pk=occupancy_id)
    # Lock definition rows (target first: it is the one validated), then positions and claims.
    target_context = _lock_target(position_id, "position_id") if position_id else None
    position_ids = sorted({pk for pk in (source_id, position_id) if pk}, key=str)
    positions = {
        p.pk: p
        for p in Position.objects.select_for_update().filter(pk__in=position_ids).order_by("pk")
    }
    claims = {
        c.pk: c
        for c in Occupancy.objects.select_for_update()
        .filter(Q(pk=occupancy_id) | Q(position_id__in=position_ids))
        .order_by("pk")
    }
    occupancy = claims[occupancy_id]
    _check_revision(occupancy, expected_revision, "item's storage record")
    source_id = occupancy.position_id
    target = positions.get(position_id) if position_id else None

    rules = {
        "place": (False, True),
        "check_in": (False, True),
        "relocate": (True, True),
        "check_out": (True, False),
    }
    if kind in rules:
        need_source, need_target = rules[kind]
        if need_source and source_id is None:
            raise _error("The item is not in a storage position.", "invalid_transition")
        if not need_source and source_id is not None:
            raise _error(
                "The item already has a storage position. Record a relocation instead.",
                "invalid_transition",
            )
        if need_target and target is None:
            raise _error("Choose a position.", "required", "position_id")
        if not need_target and position_id is not None:
            raise _error("A check-out leaves the position empty.", "invalid_transition")
    elif kind == "dispose" and position_id is not None:
        raise _error("Disposal frees the position; give no target.", "invalid_transition")
    if position_id is not None and source_id == position_id:
        raise _error("The item is already in this position.", "invalid_transition", "position_id")
    if kind == "correction" and source_id is None and position_id is None:
        raise _error("The correction changes nothing.", "invalid_transition")

    floors = [occupancy.last_occurred_at]
    if target is not None:
        unit, chain, box = target_context
        _active_chain(chain, unit, "position_id")
        if not box.is_active:
            raise _error(f"Box {box.code} is retired.", "inactive_location", "position_id")
        if target.blocked:
            raise _error(
                f"Position {box.code} {target.label} is blocked: {target.block_reason}",
                "inactive_location",
                "position_id",
            )
        if any(c.position_id == target.pk and c.pk != occupancy.pk for c in claims.values()):
            raise _error(
                f"Position {box.code} {target.label} is occupied. Choose a free position.",
                "occupied",
                "position_id",
            )
        latest = _latest_container_move([n.pk for n in chain], [box.pk])
        if latest:
            floors.append(latest)
    _occurred(occurred_at, now=now, floor=max(floors))

    corrected = None
    if corrects_id is not None:
        corrected = MoveEvent.objects.select_for_update().get(pk=corrects_id)
        if (
            corrected.is_container_move
            or not corrected.members.filter(occupancy=occupancy).exists()
        ):
            raise _error(
                "A correction must correct a movement of this item.", "invalid", "corrects"
            )
        if MoveEvent.objects.filter(corrects=corrected).exists():
            raise _error(
                "That movement was already corrected. Correct the newer record.",
                "invalid_transition",
                "corrects",
            )

    from_path = position_path(positions[source_id]) if source_id else {}
    to_path = position_path(target) if target else {}
    event = MoveEvent.objects.create(
        kind=kind,
        actor=actor,
        occurred_at=occurred_at,
        recorded_at=now,
        reason=reason,
        from_path=from_path,
        to_path=to_path,
        corrects=corrected,
    )
    MoveMember.objects.create(
        event=event,
        occupancy=occupancy,
        from_position_id=source_id,
        to_position=target,
        from_path=from_path,
        to_path=to_path,
    )
    before = {
        "position": str(source_id) if source_id else None,
        "path": from_path.get("display", ""),
        "revision": occupancy.revision,
    }
    occupancy.position = target
    occupancy.last_occurred_at = occurred_at
    _bump(occupancy)
    try:
        with transaction.atomic():
            occupancy.save(update_fields=["position", "last_occurred_at", "revision"])
    except IntegrityError as exc:  # the unique claim: a writer bypassed the gate
        raise _error("The position is occupied.", "occupied", "position_id") from exc
    _bump_gate()
    _audit(
        actor,
        f"storage.occupancy_{kind}",
        occupancy,
        before=before,
        after={
            "position": str(target.pk) if target else None,
            "path": to_path.get("display", ""),
            "revision": occupancy.revision,
            "event": str(event.pk),
        },
        reason=reason,
    )
    return event


@operation()
def touch_occupancy(*, actor, occupancy_id, expected_revision, occurred_at, reason):
    """Advance an item's chronology watermark and revision without a location change."""
    _require_gate()
    require_role(actor, *OPERATE_ROLES)
    reason = need_reason(reason)
    occupancy = Occupancy.objects.select_for_update().get(pk=occupancy_id)
    _check_revision(occupancy, expected_revision, "item's storage record")
    _occurred(occurred_at, now=tz.now(), floor=occupancy.last_occurred_at)
    before = {"revision": occupancy.revision, "last_occurred_at": occupancy.last_occurred_at}
    occupancy.last_occurred_at = occurred_at
    _bump(occupancy)
    occupancy.save(update_fields=["last_occurred_at", "revision"])
    _audit(
        actor,
        "storage.occupancy_touched",
        occupancy,
        before=state_times(before),
        after=state_times(
            {"revision": occupancy.revision, "last_occurred_at": occupancy.last_occurred_at}
        ),
        reason=reason,
    )
    return occupancy


def state_times(data):
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in data.items()}
