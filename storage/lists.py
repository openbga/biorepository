"""One definition per storage list: queryset, filters, sorts, search and CSV columns.

The list pages (storage.views) and the CSV exports (storage.exports) both read these specs, so a
download always matches what the list shows. Filters are validated by a Django form; an invalid
filter returns no rows, never all rows.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from django import forms
from django.db.models import (
    Count,
    ExpressionWrapper,
    F,
    IntegerField,
    OuterRef,
    Q,
    Subquery,
    Value,
)
from django.db.models.functions import Concat

from .models import (
    Box,
    ContainerType,
    LayoutNode,
    Position,
    Room,
    Site,
    StorageClass,
    StorageTemplate,
    StorageUnit,
)
from .queries import FREE_ORDER

PAGE_SIZES = [(size, f"{size} rows") for size in (25, 50, 100)]
ACTIVE_POSITIONS = Q(
    box__is_active=True, box__node__is_active=True, box__node__unit__is_active=True
)


class SubqueryCount(Subquery):
    """COUNT(*) of a correlated subquery: each row's positions counted once."""

    template = "(SELECT count(*) FROM (%(subquery)s) _count)"
    output_field = IntegerField()


def with_capacity(queryset, scope):
    """Annotate installed/blocked/usable/occupied/free from positions matching `scope`."""
    positions = Position.objects.filter(ACTIVE_POSITIONS & scope)
    return queryset.annotate(
        installed=SubqueryCount(positions.values("pk")),
        blocked_count=SubqueryCount(positions.filter(blocked=True).values("pk")),
        occupied=SubqueryCount(positions.filter(occupancy__isnull=False).values("pk")),
    ).annotate(
        usable=ExpressionWrapper(F("installed") - F("blocked_count"), IntegerField()),
        free=ExpressionWrapper(F("installed") - F("blocked_count") - F("occupied"), IntegerField()),
    )


UNIT_SCOPE = Q(box__node__unit=OuterRef("pk"))
NODE_SCOPE = Q(box__node=OuterRef("pk")) | Q(
    box__node__path__startswith=Concat(OuterRef("path"), Value("/"))
)
BOX_SCOPE = Q(box=OuterRef("pk"))
ROOM_SCOPE = Q(box__node__unit__room=OuterRef("pk"))


def units():
    return with_capacity(
        StorageUnit.objects.select_related("room__site", "storage_class"), UNIT_SCOPE
    )


def nodes():
    return with_capacity(
        LayoutNode.objects.select_related("unit", "parent").annotate(
            box_count=Count("boxes", filter=Q(boxes__is_active=True), distinct=True),
            child_count=Count("children", filter=Q(children__is_active=True), distinct=True),
        ),
        NODE_SCOPE,
    )


def boxes():
    return with_capacity(Box.objects.select_related("node__unit", "container_type"), BOX_SCOPE)


def rooms():
    return with_capacity(
        Room.objects.select_related("site").annotate(
            unit_count=Count("units", filter=Q(units__is_active=True))
        ),
        ROOM_SCOPE,
    )


def sites():
    return Site.objects.annotate(
        room_count=Count("rooms", distinct=True),
        unit_count=Count("rooms__units", filter=Q(rooms__units__is_active=True), distinct=True),
    )


def free_positions():
    return Position.objects.filter(
        ACTIVE_POSITIONS, blocked=False, occupancy__isnull=True
    ).select_related("box__node__unit", "box__container_type")


def classes():
    return StorageClass.objects.annotate(unit_count=Count("units", filter=Q(units__is_active=True)))


def container_types():
    return ContainerType.objects.annotate(box_count=Count("boxes", filter=Q(boxes__is_active=True)))


def templates():
    return StorageTemplate.objects.annotate(unit_count=Count("units"))


# --- filter forms ---------------------------------------------------------------------------

STATUS_CHOICES = [("", "Active"), ("retired", "Retired"), ("all", "Active and retired")]
FREE_CHOICES = [("", "Any"), ("yes", "Has free positions"), ("no", "No free positions")]


class StorageFilter(forms.Form):
    q = forms.CharField(
        label="Search",
        required=False,
        max_length=200,
        widget=forms.SearchInput(attrs={"placeholder": "Code or name"}),
    )
    per_page = forms.TypedChoiceField(
        label="Rows per page", choices=PAGE_SIZES, coerce=int, required=False
    )
    sort = forms.CharField(required=False, widget=forms.HiddenInput())


def _site():
    return forms.ModelChoiceField(
        label="Site", queryset=Site.objects.order_by("code"), required=False, empty_label="Any site"
    )


def _room():
    return forms.ModelChoiceField(
        label="Room",
        queryset=Room.objects.select_related("site").order_by("site__code", "code"),
        required=False,
        empty_label="Any room",
    )


def _unit():
    return forms.ModelChoiceField(
        label="Storage unit",
        queryset=StorageUnit.objects.order_by("code"),
        required=False,
        empty_label="Any unit",
    )


def _node():
    return forms.ModelChoiceField(
        label="Rack or shelf",
        queryset=LayoutNode.objects.filter(is_active=True).order_by("unit__code", "path"),
        required=False,
        empty_label="Anywhere",
    )


def _class():
    return forms.ModelChoiceField(
        label="Storage class",
        queryset=StorageClass.objects.order_by("code"),
        required=False,
        empty_label="Any class",
    )


def _grid():
    return forms.ModelChoiceField(
        label="Container type",
        queryset=ContainerType.objects.order_by("code", "version"),
        required=False,
        empty_label="Any container type",
    )


def _status():
    return forms.ChoiceField(label="Status", choices=STATUS_CHOICES, required=False)


def _free():
    return forms.ChoiceField(label="Free positions", choices=FREE_CHOICES, required=False)


class UnitFilter(StorageFilter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["site"] = _site()
        self.fields["room"] = _room()
        self.fields["storage_class"] = _class()
        self.fields["technology"] = forms.ChoiceField(
            label="Technology",
            choices=[("", "Any technology"), *StorageUnit.Technology.choices],
            required=False,
        )
        self.fields["status"] = _status()
        self.fields["free"] = _free()


class NodeFilter(StorageFilter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["unit"] = _unit()
        self.fields["kind"] = forms.ChoiceField(
            label="Kind", choices=[("", "Any kind"), *LayoutNode.Kind.choices], required=False
        )
        self.fields["site"] = _site()
        self.fields["room"] = _room()
        self.fields["status"] = _status()
        self.fields["free"] = _free()


class BoxFilter(StorageFilter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["unit"] = _unit()
        self.fields["node"] = _node()
        self.fields["free"] = _free()
        self.fields["container_type"] = _grid()
        self.fields["site"] = _site()
        self.fields["room"] = _room()
        self.fields["status"] = _status()


class FreePositionFilter(StorageFilter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["q"].widget.attrs["placeholder"] = "Box code"
        self.fields["unit"] = _unit()
        self.fields["node"] = _node()
        self.fields["container_type"] = _grid()
        self.fields["box"] = forms.ModelChoiceField(
            label="Box",
            queryset=Box.objects.filter(is_active=True).order_by("code"),
            required=False,
            empty_label="Any box",
        )
        self.fields["site"] = _site()
        self.fields["room"] = _room()


class SiteFilter(StorageFilter):
    pass


class RoomFilter(StorageFilter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["site"] = _site()


class CatalogueFilter(StorageFilter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["active"] = forms.ChoiceField(
            label="Status",
            choices=[("", "Active and inactive"), ("true", "Active"), ("false", "Inactive")],
            required=False,
        )


# --- refinement -----------------------------------------------------------------------------


def _status_filter(queryset, cleaned):
    status = cleaned.get("status")
    if status == "retired":
        return queryset.filter(is_active=False)
    if status == "all":
        return queryset
    return queryset.filter(is_active=True)


def _free_filter(queryset, cleaned):
    if cleaned.get("free") == "yes":
        return queryset.filter(free__gt=0)
    if cleaned.get("free") == "no":
        return queryset.filter(free__lte=0)
    return queryset


def refine_units(queryset, cleaned):
    if cleaned.get("site"):
        queryset = queryset.filter(room__site=cleaned["site"])
    if cleaned.get("room"):
        queryset = queryset.filter(room=cleaned["room"])
    if cleaned.get("storage_class"):
        queryset = queryset.filter(storage_class=cleaned["storage_class"])
    if cleaned.get("technology"):
        queryset = queryset.filter(technology=cleaned["technology"])
    return _free_filter(_status_filter(queryset, cleaned), cleaned)


def refine_nodes(queryset, cleaned):
    if cleaned.get("unit"):
        queryset = queryset.filter(unit=cleaned["unit"])
    if cleaned.get("kind"):
        queryset = queryset.filter(kind=cleaned["kind"])
    if cleaned.get("site"):
        queryset = queryset.filter(unit__room__site=cleaned["site"])
    if cleaned.get("room"):
        queryset = queryset.filter(unit__room=cleaned["room"])
    return _free_filter(_status_filter(queryset, cleaned), cleaned)


def _within_node(prefix, node):
    return Q(**{f"{prefix}node": node}) | Q(**{f"{prefix}node__path__startswith": node.path + "/"})


def refine_boxes(queryset, cleaned):
    if cleaned.get("unit"):
        queryset = queryset.filter(node__unit=cleaned["unit"])
    if cleaned.get("node"):
        queryset = queryset.filter(_within_node("", cleaned["node"]))
    if cleaned.get("container_type"):
        queryset = queryset.filter(container_type=cleaned["container_type"])
    if cleaned.get("site"):
        queryset = queryset.filter(node__unit__room__site=cleaned["site"])
    if cleaned.get("room"):
        queryset = queryset.filter(node__unit__room=cleaned["room"])
    return _free_filter(_status_filter(queryset, cleaned), cleaned)


def refine_free(queryset, cleaned):
    if cleaned.get("unit"):
        queryset = queryset.filter(box__node__unit=cleaned["unit"])
    if cleaned.get("node"):
        queryset = queryset.filter(_within_node("box__", cleaned["node"]))
    if cleaned.get("box"):
        queryset = queryset.filter(box=cleaned["box"])
    if cleaned.get("container_type"):
        queryset = queryset.filter(box__container_type=cleaned["container_type"])
    if cleaned.get("site"):
        queryset = queryset.filter(box__node__unit__room__site=cleaned["site"])
    if cleaned.get("room"):
        queryset = queryset.filter(box__node__unit__room=cleaned["room"])
    return queryset


def refine_rooms(queryset, cleaned):
    if cleaned.get("site"):
        queryset = queryset.filter(site=cleaned["site"])
    return queryset


def refine_catalogue(queryset, cleaned):
    if cleaned.get("active") == "true":
        return queryset.filter(is_active=True)
    if cleaned.get("active") == "false":
        return queryset.filter(is_active=False)
    return queryset


# --- specs ----------------------------------------------------------------------------------


def _state_label(obj):
    return "Active" if obj.is_active else "Retired"


def _active(obj):
    return "Active" if obj.is_active else "Inactive"


CAPACITY_COLUMNS = (
    ("Installed positions", lambda o: o.installed),
    ("Blocked", lambda o: o.blocked_count),
    ("Occupied", lambda o: o.occupied),
    ("Free", lambda o: o.free),
)


@dataclass(frozen=True)
class ListSpec:
    key: str
    title: str
    noun: str
    queryset: Callable
    columns: dict  # sort key -> ORM ordering field
    default_sort: str
    form_class: type
    search_fields: tuple
    refine: Callable
    inline: tuple
    more: tuple = ()
    export_columns: tuple = field(default=())
    order: tuple = ()  # fixed order instead of sortable columns (free positions)


SPECS = {
    "unit": ListSpec(
        key="unit",
        title="Storage units",
        noun="storage units",
        queryset=units,
        columns={
            "code": "code",
            "name": "name",
            "room": "room__code",
            "class": "storage_class__code",
            "temperature": "nominal_temperature",
            "occupied": "occupied",
            "free": "free",
        },
        default_sort="code",
        form_class=UnitFilter,
        search_fields=("code", "name"),
        refine=refine_units,
        inline=("site", "storage_class", "free"),
        more=("room", "technology", "status"),
        export_columns=(
            ("Code", lambda o: o.code),
            ("Name", lambda o: o.name),
            ("Site", lambda o: o.room.site.code),
            ("Room", lambda o: o.room.code),
            ("Storage class", lambda o: o.storage_class.code),
            ("Technology", lambda o: o.get_technology_display()),
            ("Nominal temperature (°C)", lambda o: o.nominal_temperature),
            *CAPACITY_COLUMNS,
            ("Status", _state_label),
        ),
    ),
    "node": ListSpec(
        key="node",
        title="Racks and shelves",
        noun="racks and shelves",
        queryset=nodes,
        columns={
            "code": "code",
            "name": "name",
            "kind": "kind",
            "unit": "unit__code",
            "path": "path",
            "occupied": "occupied",
            "free": "free",
        },
        default_sort="code",
        form_class=NodeFilter,
        search_fields=("code", "name"),
        refine=refine_nodes,
        inline=("unit", "kind", "free"),
        more=("site", "room", "status"),
        export_columns=(
            ("Code", lambda o: o.code),
            ("Name", lambda o: o.name),
            ("Kind", lambda o: o.get_kind_display()),
            ("Storage unit", lambda o: o.unit.code),
            ("Path", lambda o: o.path),
            ("Slot", lambda o: o.slot),
            ("Node slots", lambda o: o.child_slots),
            ("Box slots", lambda o: o.box_slots),
            *CAPACITY_COLUMNS,
            ("Status", _state_label),
        ),
    ),
    "box": ListSpec(
        key="box",
        title="Boxes",
        noun="boxes",
        queryset=boxes,
        columns={
            "code": "code",
            "name": "name",
            "unit": "node__unit__code",
            "location": "node__path",
            "grid": "container_type",  # ContainerType orders by code, then numeric version.
            "occupied": "occupied",
            "free": "free",
        },
        default_sort="code",
        form_class=BoxFilter,
        search_fields=("code", "name"),
        refine=refine_boxes,
        inline=("unit", "node", "free"),
        more=("container_type", "site", "room", "status"),
        export_columns=(
            ("Code", lambda o: o.code),
            ("Name", lambda o: o.name),
            ("Storage unit", lambda o: o.node.unit.code),
            ("Rack or shelf", lambda o: o.node.path),
            ("Slot", lambda o: o.slot),
            ("Container type", lambda o: str(o.container_type)),
            *CAPACITY_COLUMNS,
            ("Status", _state_label),
        ),
    ),
    "position": ListSpec(
        key="position",
        title="Free positions",
        noun="free positions",
        queryset=free_positions,
        columns={},
        default_sort="",
        form_class=FreePositionFilter,
        search_fields=("box__code",),
        refine=refine_free,
        inline=("unit", "node", "box"),
        more=("container_type", "site", "room"),
        export_columns=(
            ("Storage unit", lambda o: o.box.node.unit.code),
            ("Rack or shelf", lambda o: o.box.node.path),
            ("Box", lambda o: o.box.code),
            ("Position", lambda o: o.label),
            ("Row", lambda o: o.row),
            ("Column", lambda o: o.column),
            ("Ordinal", lambda o: o.ordinal),
            ("Container type", lambda o: str(o.box.container_type)),
        ),
        order=FREE_ORDER,
    ),
    "site": ListSpec(
        key="site",
        title="Sites",
        noun="sites",
        queryset=sites,
        columns={"code": "code", "name": "name", "timezone": "timezone", "units": "unit_count"},
        default_sort="code",
        form_class=SiteFilter,
        search_fields=("code", "name"),
        refine=lambda queryset, cleaned: queryset,
        inline=(),
        export_columns=(
            ("Code", lambda o: o.code),
            ("Name", lambda o: o.name),
            ("Time zone", lambda o: o.timezone),
            ("Rooms", lambda o: o.room_count),
            ("Active storage units", lambda o: o.unit_count),
        ),
    ),
    "room": ListSpec(
        key="room",
        title="Rooms",
        noun="rooms",
        queryset=rooms,
        columns={
            "code": "code",
            "name": "name",
            "site": "site__code",
            "units": "unit_count",
            "occupied": "occupied",
            "free": "free",
        },
        default_sort="code",
        form_class=RoomFilter,
        search_fields=("code", "name"),
        refine=refine_rooms,
        inline=("site",),
        export_columns=(
            ("Site", lambda o: o.site.code),
            ("Code", lambda o: o.code),
            ("Name", lambda o: o.name),
            ("Active storage units", lambda o: o.unit_count),
            *CAPACITY_COLUMNS,
        ),
    ),
}

CATALOGUE_SPECS = {
    "class": ListSpec(
        key="class",
        title="Storage catalogues",
        noun="storage classes",
        queryset=classes,
        columns={"code": "code", "name": "name", "units": "unit_count"},
        default_sort="code",
        form_class=CatalogueFilter,
        search_fields=("code", "name"),
        refine=refine_catalogue,
        inline=("active",),
        export_columns=(
            ("Code", lambda o: o.code),
            ("Name", lambda o: o.name),
            ("Description", lambda o: o.description),
            ("Active storage units", lambda o: o.unit_count),
            ("Status", _active),
        ),
    ),
    "container_type": ListSpec(
        key="container_type",
        title="Storage catalogues",
        noun="container types",
        queryset=container_types,
        columns={"code": "code", "name": "name", "boxes": "box_count"},
        default_sort="code",
        form_class=CatalogueFilter,
        search_fields=("code", "name"),
        refine=refine_catalogue,
        inline=("active",),
        export_columns=(
            ("Code", lambda o: o.code),
            ("Version", lambda o: o.version),
            ("Name", lambda o: o.name),
            ("Rows", lambda o: o.rows),
            ("Columns", lambda o: o.columns),
            ("Positions", lambda o: o.capacity),
            ("Labels", lambda o: o.get_label_scheme_display()),
            ("Fill order", lambda o: o.get_fill_order_display()),
            ("Active boxes", lambda o: o.box_count),
            ("Status", _active),
        ),
    ),
    "template": ListSpec(
        key="template",
        title="Storage catalogues",
        noun="storage templates",
        queryset=templates,
        columns={"code": "code", "name": "name", "units": "unit_count"},
        default_sort="code",
        form_class=CatalogueFilter,
        search_fields=("code", "name"),
        refine=refine_catalogue,
        inline=("active",),
        export_columns=(
            ("Code", lambda o: o.code),
            ("Version", lambda o: o.version),
            ("Name", lambda o: o.name),
            ("Units created", lambda o: o.unit_count),
            ("Status", _active),
        ),
    ),
}


def filtered(spec, data):
    """(form, queryset) for request-style filter data; an invalid form yields no rows."""
    form = spec.form_class(data)
    queryset = spec.queryset()
    if not form.is_valid():
        return form, queryset.none()
    cleaned = form.cleaned_data
    if cleaned.get("q"):
        condition = Q()
        for name in spec.search_fields:
            condition |= Q(**{f"{name}__icontains": cleaned["q"]})
        queryset = queryset.filter(condition)
    return form, spec.refine(queryset, cleaned)


def ordered(spec, queryset, sort):
    """Apply an allowlisted sort with the primary key as the stable tie-break."""
    if spec.order:
        return queryset.order_by(*spec.order)
    key = (sort or spec.default_sort).removeprefix("-")
    if key not in spec.columns:
        sort, key = spec.default_sort, spec.default_sort
    prefix = "-" if sort.startswith("-") else ""
    extra = (
        ("-version" if prefix else "version",) if spec.key in ("container_type", "template") else ()
    )
    return queryset.order_by(prefix + spec.columns[key], *extra, "pk")
