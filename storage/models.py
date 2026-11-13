"""Storage tree, box grids, position claims and the append-only movement record.

Integrity that one row can prove is a database constraint here. Rules that span rows (parent
kinds, same-unit ancestry, slot bounds, active ancestors, chronology) are checked by
`storage.services` while holding the StorageGate and the affected rows. Nothing here is deleted
through the application; event tables refuse UPDATE and DELETE by trigger, and Occupancy and the
gate refuse DELETE.
"""

import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

CODE_PATTERN = r"^[A-Z0-9][A-Z0-9._-]*$"
TEMPERATURE_UNIT = "°C"
ABSOLUTE_ZERO = Decimal("-273.15")
MAX_DEPTH = 4
MAX_GRID_SIDE = 100


def _code_check(name):
    return models.CheckConstraint(condition=Q(code__regex=CODE_PATTERN), name=name)


class Definition(models.Model):
    """Attribution and optimistic revision shared by every editable storage definition."""

    revision = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", editable=False
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        abstract = True


class Retirable(Definition):
    """A physical definition that can be retired (never deleted) with a reason."""

    retired_at = models.DateTimeField(null=True, blank=True, editable=False)
    retired_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="+",
        null=True,
        blank=True,
        editable=False,
    )
    retirement_reason = models.TextField(blank=True, editable=False)

    class Meta:
        abstract = True
        constraints = [
            models.CheckConstraint(
                condition=Q(
                    is_active=True,
                    retired_at__isnull=True,
                    retired_by__isnull=True,
                    retirement_reason="",
                )
                | (
                    Q(is_active=False, retired_at__isnull=False, retired_by__isnull=False)
                    & ~Q(retirement_reason="")
                ),
                name="%(app_label)s_%(class)s_retirement",
            )
        ]

    @property
    def status(self):
        return "active" if self.is_active else "retired"


class Site(Definition):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=160)
    timezone = models.CharField(max_length=64, help_text="IANA time zone, e.g. America/New_York")

    class Meta:
        ordering = ["code"]
        constraints = [_code_check("storage_site_code_format")]

    def __str__(self):
        return self.code


class Room(Definition):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    site = models.ForeignKey(Site, on_delete=models.PROTECT, related_name="rooms")
    code = models.CharField(max_length=40)
    name = models.CharField(max_length=160)

    class Meta:
        ordering = ["site__code", "code"]
        constraints = [
            models.UniqueConstraint(fields=["site", "code"], name="storage_room_site_code"),
            _code_check("storage_room_code_format"),
        ]

    def __str__(self):
        return f"{self.site.code} / {self.code}"


class StorageClass(Definition):
    """Catalogue of storage conditions (cryogenic, ultra-low, ...). Data, not code."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=160)
    description = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["code"]
        verbose_name_plural = "storage classes"
        constraints = [_code_check("storage_storageclass_code_format")]

    def __str__(self):
        return self.name


class ContainerType(Definition):
    """Box/grid geometry. Immutable after first use: a change is a new version."""

    class LabelScheme(models.TextChoices):
        ALPHA_NUMERIC = "alpha_numeric", "Letter rows, numbered columns (A1)"
        NUMERIC = "numeric", "Numbered positions (1, 2, 3 ...)"

    class FillOrder(models.TextChoices):
        ROW_MAJOR = "row_major", "Across rows (row-major)"
        COLUMN_MAJOR = "column_major", "Down columns (column-major)"

    class Origin(models.TextChoices):
        TOP_LEFT = "top_left", "Top left"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=40)
    version = models.PositiveIntegerField(default=1)
    name = models.CharField(max_length=160)
    rows = models.PositiveSmallIntegerField()
    columns = models.PositiveSmallIntegerField()
    label_scheme = models.CharField(max_length=16, choices=LabelScheme.choices)
    fill_order = models.CharField(max_length=12, choices=FillOrder.choices)
    origin = models.CharField(max_length=12, choices=Origin.choices, default=Origin.TOP_LEFT)

    class Meta:
        ordering = ["code", "version"]
        constraints = [
            models.UniqueConstraint(fields=["code", "version"], name="storage_containertype_ver"),
            _code_check("storage_containertype_code_format"),
            models.CheckConstraint(
                condition=Q(
                    rows__gte=1,
                    rows__lte=MAX_GRID_SIDE,
                    columns__gte=1,
                    columns__lte=MAX_GRID_SIDE,
                    version__gte=1,
                ),
                name="storage_containertype_bounds",
            ),
            models.CheckConstraint(
                condition=Q(label_scheme__in=["alpha_numeric", "numeric"]),
                name="storage_containertype_label_scheme",
            ),
            models.CheckConstraint(
                condition=Q(fill_order__in=["row_major", "column_major"]),
                name="storage_containertype_fill_order",
            ),
            models.CheckConstraint(
                condition=Q(origin="top_left"), name="storage_containertype_origin"
            ),
        ]

    def __str__(self):
        return f"{self.code} v{self.version}"

    @property
    def capacity(self):
        return self.rows * self.columns


class StorageTemplate(Definition):
    """A versioned data recipe for a unit and its layout; see storage.templates."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=40)
    version = models.PositiveIntegerField(default=1)
    name = models.CharField(max_length=160)
    definition = models.JSONField()

    class Meta:
        ordering = ["code", "version"]
        constraints = [
            models.UniqueConstraint(fields=["code", "version"], name="storage_template_ver"),
            _code_check("storage_template_code_format"),
            models.CheckConstraint(condition=Q(version__gte=1), name="storage_template_version"),
        ]

    def __str__(self):
        return f"{self.code} v{self.version}"


class StorageUnit(Retirable):
    class Technology(models.TextChoices):
        LN2_VAPOR = "ln2_vapor", "LN2 vapor phase"
        LN2_LIQUID = "ln2_liquid", "LN2 liquid phase"
        MECHANICAL = "mechanical", "Mechanical freezer or refrigerator"
        AMBIENT = "ambient", "Ambient"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=160)
    room = models.ForeignKey(Room, on_delete=models.PROTECT, related_name="units")
    storage_class = models.ForeignKey(StorageClass, on_delete=models.PROTECT, related_name="units")
    technology = models.CharField(max_length=16, choices=Technology.choices)
    nominal_temperature = models.DecimalField(max_digits=6, decimal_places=2)
    temperature_unit = models.CharField(max_length=2, default=TEMPERATURE_UNIT, editable=False)
    template = models.ForeignKey(
        StorageTemplate,
        on_delete=models.PROTECT,
        related_name="units",
        null=True,
        blank=True,
        editable=False,
    )

    class Meta:
        ordering = ["code"]
        constraints = [
            *Retirable.Meta.constraints,
            _code_check("storage_storageunit_code_format"),
            models.CheckConstraint(
                condition=Q(nominal_temperature__gte=ABSOLUTE_ZERO),
                name="storage_storageunit_temperature_floor",
            ),
            models.CheckConstraint(
                condition=Q(temperature_unit=TEMPERATURE_UNIT),
                name="storage_storageunit_temperature_unit",
            ),
            models.CheckConstraint(
                condition=Q(technology__in=["ln2_vapor", "ln2_liquid", "mechanical", "ambient"]),
                name="storage_storageunit_technology",
            ),
        ]
        indexes = [models.Index(fields=["room", "is_active"], name="storage_unit_room_active")]

    def __str__(self):
        return self.code


class LayoutNode(Retirable):
    """A shelf, rack, tower or canister. It holds child nodes or boxes, never both."""

    class Kind(models.TextChoices):
        SHELF = "shelf", "Shelf"
        RACK = "rack", "Rack"
        TOWER = "tower", "Tower"
        CANISTER = "canister", "Canister"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=160)
    kind = models.CharField(max_length=12, choices=Kind.choices)
    unit = models.ForeignKey(StorageUnit, on_delete=models.PROTECT, related_name="nodes")
    parent = models.ForeignKey(
        "self", on_delete=models.PROTECT, related_name="children", null=True, blank=True
    )
    slot = models.PositiveIntegerField()
    child_slots = models.PositiveIntegerField(default=0)
    box_slots = models.PositiveIntegerField(default=0)
    # Denormalized under the gate by create/move: 1 for a top-level node, and the "/"-joined
    # codes from the top-level node down to this one (stable sort key and subtree prefix).
    depth = models.PositiveSmallIntegerField(default=1, editable=False)
    path = models.CharField(max_length=200, editable=False)

    class Meta:
        ordering = ["path"]
        constraints = [
            *Retirable.Meta.constraints,
            _code_check("storage_layoutnode_code_format"),
            models.CheckConstraint(
                condition=Q(kind__in=["shelf", "rack", "tower", "canister"]),
                name="storage_layoutnode_kind",
            ),
            models.CheckConstraint(condition=Q(slot__gte=1), name="storage_layoutnode_slot"),
            models.CheckConstraint(
                condition=Q(parent__isnull=True) | ~Q(parent=F("id")),
                name="storage_layoutnode_not_own_parent",
            ),
            models.CheckConstraint(
                condition=Q(child_slots__gt=0, box_slots=0) | Q(child_slots=0, box_slots__gt=0),
                name="storage_layoutnode_children_xor_boxes",
            ),
            models.CheckConstraint(
                condition=Q(parent__isnull=True, depth=1)
                | Q(parent__isnull=False, depth__gte=2, depth__lte=MAX_DEPTH),
                name="storage_layoutnode_depth",
            ),
            models.UniqueConstraint(
                fields=["unit", "slot"],
                condition=Q(parent__isnull=True, is_active=True),
                name="storage_layoutnode_unit_slot",
            ),
            models.UniqueConstraint(
                fields=["parent", "slot"],
                condition=Q(parent__isnull=False, is_active=True),
                name="storage_layoutnode_parent_slot",
            ),
        ]
        indexes = [
            models.Index(fields=["unit", "kind", "is_active"], name="storage_node_unit_kind"),
            models.Index(
                fields=["path"], name="storage_node_path", opclasses=["varchar_pattern_ops"]
            ),
        ]

    def __str__(self):
        return self.code


class Box(Retirable):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=160)
    node = models.ForeignKey(LayoutNode, on_delete=models.PROTECT, related_name="boxes")
    slot = models.PositiveIntegerField()
    container_type = models.ForeignKey(
        ContainerType, on_delete=models.PROTECT, related_name="boxes"
    )

    class Meta:
        ordering = ["code"]
        verbose_name_plural = "boxes"
        constraints = [
            *Retirable.Meta.constraints,
            _code_check("storage_box_code_format"),
            models.CheckConstraint(condition=Q(slot__gte=1), name="storage_box_slot"),
            models.UniqueConstraint(
                fields=["node", "slot"], condition=Q(is_active=True), name="storage_box_node_slot"
            ),
        ]
        indexes = [models.Index(fields=["node", "is_active"], name="storage_box_node_active")]

    def __str__(self):
        return self.code


class Position(models.Model):
    """One cell of a box grid, generated once with the box. Coordinates never change."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    box = models.ForeignKey(Box, on_delete=models.PROTECT, related_name="positions")
    row = models.PositiveSmallIntegerField()
    column = models.PositiveSmallIntegerField()
    ordinal = models.PositiveIntegerField()
    label = models.CharField(max_length=20)
    blocked = models.BooleanField(default=False)
    block_reason = models.TextField(blank=True)
    revision = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", editable=False
    )

    class Meta:
        ordering = ["box", "ordinal"]
        constraints = [
            models.UniqueConstraint(fields=["box", "row", "column"], name="storage_position_cell"),
            models.UniqueConstraint(fields=["box", "ordinal"], name="storage_position_ordinal"),
            models.UniqueConstraint(fields=["box", "label"], name="storage_position_label"),
            models.CheckConstraint(
                condition=Q(row__gte=1, column__gte=1, ordinal__gte=1),
                name="storage_position_coordinates",
            ),
            models.CheckConstraint(
                condition=Q(blocked=False, block_reason="")
                | (Q(blocked=True) & ~Q(block_reason="")),
                name="storage_position_block_reason",
            ),
        ]
        indexes = [
            models.Index(fields=["box", "blocked", "ordinal"], name="storage_position_free"),
        ]

    def __str__(self):
        return f"{self.box.code} {self.label}"


class Occupancy(models.Model):
    """The opaque, permanent storage key of one stored item (a specimen, owned by specimens).

    `position` is the item's current claim; the unique constraint is the one-item-per-position
    rule. Storage never learns what the item is.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    position = models.OneToOneField(
        Position, on_delete=models.PROTECT, related_name="occupancy", null=True, blank=True
    )
    last_occurred_at = models.DateTimeField()
    revision = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", editable=False
    )

    class Meta:
        verbose_name_plural = "occupancies"


class StorageGate(models.Model):
    """The single row every storage-affecting mutation locks first (see storage.services)."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    revision = models.PositiveBigIntegerField(default=1)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(id=1), name="storage_gate_singleton")]


ITEM_KINDS = ("place", "relocate", "check_out", "check_in", "dispose")


class MoveEvent(models.Model):
    """Append-only: one location change of an item, or of a box or node with its contents."""

    class Kind(models.TextChoices):
        PLACE = "place", "Placed"
        RELOCATE = "relocate", "Relocated"
        CHECK_OUT = "check_out", "Checked out"
        CHECK_IN = "check_in", "Checked in"
        DISPOSE = "dispose", "Disposed"
        CONTAINER_MOVE = "container_move", "Container moved"
        CORRECTION = "correction", "Correction"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    occurred_at = models.DateTimeField()
    recorded_at = models.DateTimeField(default=timezone.now)
    reason = models.TextField()
    node = models.ForeignKey(
        LayoutNode, on_delete=models.PROTECT, related_name="moves", null=True, blank=True
    )
    box = models.ForeignKey(
        Box, on_delete=models.PROTECT, related_name="moves", null=True, blank=True
    )
    from_path = models.JSONField(default=dict)
    to_path = models.JSONField(default=dict)
    corrects = models.OneToOneField(
        "self", on_delete=models.PROTECT, related_name="corrected_by", null=True, blank=True
    )

    class Meta:
        ordering = ["occurred_at", "recorded_at", "id"]
        constraints = [
            models.CheckConstraint(condition=~Q(reason=""), name="storage_moveevent_reason"),
            models.CheckConstraint(
                condition=Q(kind__in=[*ITEM_KINDS, "container_move", "correction"]),
                name="storage_moveevent_kind",
            ),
            models.CheckConstraint(
                condition=(
                    Q(kind="container_move")
                    & (
                        Q(node__isnull=False, box__isnull=True)
                        | Q(node__isnull=True, box__isnull=False)
                    )
                )
                | Q(kind__in=ITEM_KINDS, node__isnull=True, box__isnull=True)
                | (Q(kind="correction") & ~Q(node__isnull=False, box__isnull=False)),
                name="storage_moveevent_target_shape",
            ),
            models.CheckConstraint(
                condition=Q(kind="correction", corrects__isnull=False)
                | (~Q(kind="correction") & Q(corrects__isnull=True)),
                name="storage_moveevent_corrects",
            ),
            models.CheckConstraint(
                condition=Q(occurred_at__lte=F("recorded_at")),
                name="storage_moveevent_not_future",
            ),
        ]
        indexes = [models.Index(fields=["occurred_at", "id"], name="storage_moveevent_time")]

    @property
    def is_container_move(self):
        return self.node_id is not None or self.box_id is not None


class MoveMember(models.Model):
    """Append-only: one item's from/to position within a MoveEvent, with recorded paths."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event = models.ForeignKey(MoveEvent, on_delete=models.PROTECT, related_name="members")
    occupancy = models.ForeignKey(Occupancy, on_delete=models.PROTECT, related_name="moves")
    from_position = models.ForeignKey(
        Position, on_delete=models.PROTECT, related_name="+", null=True, blank=True
    )
    to_position = models.ForeignKey(
        Position, on_delete=models.PROTECT, related_name="+", null=True, blank=True
    )
    from_path = models.JSONField(default=dict)
    to_path = models.JSONField(default=dict)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["event", "occupancy"], name="storage_movemember_once"),
        ]
        indexes = [
            models.Index(fields=["occupancy", "event"], name="storage_movemember_item"),
        ]
