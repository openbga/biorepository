"""Storage reads: capacity, free positions, occupants, movement history, catalogue pages.

Reads take no gate. Each rollup is one SQL statement, so its counts are consistent with each
other. Results are advisory: a free position is only claimed when a specimens operation locks it.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db.models import Count, Q
from django.utils import timezone

from accounts.roles import require_role

from .models import (
    MAX_DEPTH,
    Box,
    ContainerType,
    LayoutNode,
    MoveMember,
    Occupancy,
    Position,
    StorageClass,
    StorageTemplate,
    StorageUnit,
)
from .services import READ_ROLES, _error, build_path

MAX_PAGE = 200


@dataclass(frozen=True)
class Capacity:
    installed: int  # positions in active boxes under active ancestors
    blocked: int
    usable: int  # installed - blocked
    occupied: int
    free: int  # usable - occupied


@dataclass(frozen=True)
class Occupant:
    occupancy_id: UUID
    position_id: UUID
    path: dict  # same shape as MoveMember paths; path["display"] is "VP-01 / T-01 / BOX-001 / A1"
    revision: int


@dataclass(frozen=True)
class Presence:
    occupancy_id: UUID
    intervals: tuple  # ((from, until), ...) clipped to [start, end], in time order
    path: dict  # recorded path that put it in the unit for its first interval (MoveMember shape)


def _bounded(offset, limit):
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValidationError("Offset must be zero or more.", code="invalid")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PAGE:
        raise ValidationError(f"Limit must be from 1 to {MAX_PAGE}.", code="invalid")


def scope_positions(kind, object_id):
    """Positions in active boxes within a unit, node (and everything under it) or box.

    Raises the model's DoesNotExist for an unknown object (views map it to 404).
    """
    active = Q(box__is_active=True, box__node__is_active=True, box__node__unit__is_active=True)
    if kind == "unit":
        unit = StorageUnit.objects.get(pk=object_id)
        scope = Q(box__node__unit=unit)
    elif kind == "node":
        node = LayoutNode.objects.get(pk=object_id)
        scope = Q(box__node=node) | Q(box__node__path__startswith=node.path + "/")
    elif kind == "box":
        box = Box.objects.get(pk=object_id)
        scope = Q(box=box)
    else:
        raise ValidationError("Choose a unit, node or box.", code="invalid")
    return Position.objects.filter(active & scope)


CAPACITY_AGGREGATES = {
    "installed": Count("id"),
    "blocked": Count("id", filter=Q(blocked=True)),
    "occupied": Count("id", filter=Q(occupancy__isnull=False)),
}


def capacity_of(queryset):
    counts = queryset.aggregate(**CAPACITY_AGGREGATES)
    usable = counts["installed"] - counts["blocked"]
    return Capacity(
        installed=counts["installed"],
        blocked=counts["blocked"],
        usable=usable,
        occupied=counts["occupied"],
        free=usable - counts["occupied"],
    )


def capacity(*, actor, kind, object_id):
    require_role(actor, *READ_ROLES)
    return capacity_of(scope_positions(kind, object_id))


def free_positions(*, actor, unit_id=None, node_id=None, box_id=None, offset=0, limit=50):
    """Free positions in path-code then ordinal order; no reservation is made."""
    require_role(actor, *READ_ROLES)
    _bounded(offset, limit)
    chosen = [(k, v) for k, v in (("unit", unit_id), ("node", node_id), ("box", box_id)) if v]
    if len(chosen) > 1:
        raise ValidationError("Give at most one of unit, node or box.", code="invalid")
    if chosen:
        queryset = scope_positions(*chosen[0])
    else:
        queryset = Position.objects.filter(
            box__is_active=True, box__node__is_active=True, box__node__unit__is_active=True
        )
    return tuple(
        free_queryset(queryset).select_related("box__node__unit", "box__container_type")[
            offset : offset + limit
        ]
    )


FREE_ORDER = ("box__node__unit__code", "box__node__path", "box__code", "ordinal", "id")


def free_queryset(queryset):
    return queryset.filter(blocked=False, occupancy__isnull=True).order_by(*FREE_ORDER)


# Box -> its node -> up to MAX_DEPTH - 1 ancestors, with the unit, room and site, in one join.
_CHAIN = "node" + "__parent" * (MAX_DEPTH - 1)
_PATH_RELATED = ("container_type", "node__unit__room__site", _CHAIN)


def _paths(positions):
    """Recorded-path dicts for many positions.

    One SELECT joins each box to its whole node chain and unit, so the paths come from one
    consistent snapshot even while a rack or box move commits (READ COMMITTED gives each
    statement, not each function, its own snapshot).
    """
    boxes = {
        b.pk: b
        for b in Box.objects.filter(pk__in={p.box_id for p in positions}).select_related(
            *_PATH_RELATED
        )
    }
    result = {}
    for position in positions:
        box = boxes[position.box_id]
        chain = [box.node]
        while chain[0].parent_id:
            chain.insert(0, chain[0].parent)
        result[position.pk] = build_path(
            unit=box.node.unit, nodes=chain, box=box, position=position
        )
    return result


def occupants(*, actor, kind, object_id):
    """Occupied positions in a unit, node or box: opaque keys only, in path order."""
    require_role(actor, *READ_ROLES)
    if kind == "unit":
        StorageUnit.objects.get(pk=object_id)
        scope = Q(position__box__node__unit_id=object_id)
    elif kind == "node":
        node = LayoutNode.objects.get(pk=object_id)
        scope = Q(position__box__node=node) | Q(
            position__box__node__path__startswith=node.path + "/"
        )
    elif kind == "box":
        Box.objects.get(pk=object_id)
        scope = Q(position__box_id=object_id)
    else:
        raise ValidationError("Choose a unit, node or box.", code="invalid")
    claims = list(
        Occupancy.objects.filter(scope)
        .select_related("position")
        .order_by(
            "position__box__node__unit__code",
            "position__box__node__path",
            "position__box__code",
            "position__ordinal",
        )
    )
    paths = _paths([c.position for c in claims])
    return tuple(
        Occupant(
            occupancy_id=c.pk,
            position_id=c.position_id,
            path=paths[c.position_id],
            revision=c.revision,
        )
        for c in claims
    )


def locations(*, actor, occupancy_ids):
    """Current recorded path (or None when unplaced) for each given storage key."""
    require_role(actor, *READ_ROLES)
    claims = list(Occupancy.objects.filter(pk__in=list(occupancy_ids)).select_related("position"))
    paths = _paths([c.position for c in claims if c.position_id])
    return {c.pk: (paths[c.position_id] if c.position_id else None) for c in claims}


def movement_history(*, actor, occupancy_id):
    """Every recorded movement of one storage key, oldest first, with the paths as recorded."""
    require_role(actor, *READ_ROLES)
    Occupancy.objects.get(pk=occupancy_id)
    return tuple(
        MoveMember.objects.filter(occupancy_id=occupancy_id)
        .select_related("event", "event__actor", "event__corrects")
        .order_by("event__occurred_at", "event__recorded_at", "event__id")
    )


def occupants_during(*, actor, unit_id, start, end):
    """Every storage key that was in `unit_id` at any time in [start, end], from the recorded
    movements (item movements and the per-item members of box and rack moves).

    An item is in the unit at time t when its latest movement at or before t (ordered
    occurred_at, recorded_at, id) put it there; check-out and disposal put it nowhere. Intervals
    are clipped to [start, end]; an item moved in and out at the same instant gets a zero-length
    interval (it was there, briefly). Corrections count from their own time: a wrong record
    stays part of the history until the correction. One SELECT, so one consistent snapshot.
    Ordered by the first interval's path display, then storage key.
    """
    require_role(actor, *READ_ROLES)
    for value, field in ((start, "start"), (end, "end")):
        if not isinstance(value, datetime) or timezone.is_naive(value):
            raise _error("Give a date and time with a time zone.", "invalid", field)
    if end < start:
        raise _error("The end is before the start.", "invalid_time", "end")
    unit_key = str(StorageUnit.objects.values_list("pk", flat=True).get(pk=unit_id))
    candidates = MoveMember.objects.filter(
        to_path__unit__id=unit_key, event__occurred_at__lte=end
    ).values("occupancy_id")
    rows = (
        MoveMember.objects.filter(occupancy_id__in=candidates, event__occurred_at__lte=end)
        .order_by("occupancy_id", "event__occurred_at", "event__recorded_at", "event_id")
        .values_list("occupancy_id", "event__occurred_at", "to_path")
    )
    presences = []
    current, inside, since, first_path, intervals = None, False, None, None, []

    def finish():
        if inside:
            intervals.append((since, end))
        if intervals:
            presences.append(Presence(current, tuple(intervals), first_path))

    for occupancy_id, occurred_at, to_path in rows.iterator(chunk_size=2000):
        if occupancy_id != current:
            if current is not None:
                finish()
            current, inside, since, first_path, intervals = occupancy_id, False, None, None, []
        here = ((to_path or {}).get("unit") or {}).get("id") == unit_key
        if occurred_at <= start:  # where it was when the window opened
            inside, since = here, (start if here else None)
            first_path = to_path if here else None
        elif here and not inside:
            inside, since = True, occurred_at
            if first_path is None:
                first_path = to_path
        elif not here and inside:
            intervals.append((since, occurred_at))
            inside, since = False, None
    if current is not None:
        finish()
    presences.sort(key=lambda p: (p.path.get("display", ""), str(p.occupancy_id)))
    return tuple(presences)


CATALOGUE_MODELS = {
    "class": StorageClass,
    "container_type": ContainerType,
    "template": StorageTemplate,
}
CATALOGUE_FILTERS = {"active", "code", "name"}
CATALOGUE_SORTS = {"code": ("code",), "name": ("name",)}


def catalogue_queryset(kind, filters, sort="code"):
    if kind not in CATALOGUE_MODELS:
        raise ValidationError("Unknown catalogue.", code="invalid")
    if not isinstance(filters, dict) or set(filters) - CATALOGUE_FILTERS:
        raise ValidationError("Unknown filter.", code="invalid")
    descending = isinstance(sort, str) and sort.startswith("-")
    key = sort.removeprefix("-") if isinstance(sort, str) else None
    if key not in CATALOGUE_SORTS:
        raise ValidationError("Unknown sort.", code="invalid")
    queryset = CATALOGUE_MODELS[kind].objects.all()
    if filters.get("active") is not None and filters.get("active") != "":
        active = filters["active"]
        if active not in (True, False, "true", "false"):
            raise ValidationError("Active must be true or false.", code="invalid")
        queryset = queryset.filter(is_active=active in (True, "true"))
    if filters.get("code"):
        queryset = queryset.filter(code__icontains=str(filters["code"]).strip())
    if filters.get("name"):
        queryset = queryset.filter(name__icontains=str(filters["name"]).strip())
    order = [("-" if descending else "") + f for f in CATALOGUE_SORTS[key]]
    if kind != "class":
        order.append("-version" if descending else "version")
    return queryset.order_by(*order, "pk")
