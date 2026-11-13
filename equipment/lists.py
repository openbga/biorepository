"""One definition per equipment list: filters, query, sorts, search and CSV columns.

The list pages (equipment.views) and the CSV exports (equipment.exports) both read these specs, so
a download always matches what the list shows, and the dashboard queues link to these filters
with the queries they count (equipment.dashboard). An invalid filter returns no rows, never all.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from django import forms
from django.db.models import Case, F, Q, Value, When, Window
from django.db.models.functions import FirstValue
from django.utils import timezone

from accounts.roles import has_role
from specimens.queries import normalize_accession
from storage.models import StorageUnit

from . import queries
from .models import (
    AffectedSpecimen,
    Equipment,
    Excursion,
    LN2Entry,
    ReadingImport,
    ServiceEvent,
    TemperatureReading,
)
from .services import MANAGE_ROLES

PAGE_SIZES = [(size, f"{size} rows") for size in (25, 50, 100)]


def person(user):
    return (user.get_full_name() or user.get_username()) if user else ""


def local(value):
    return timezone.localtime(value).strftime("%Y-%m-%d %H:%M:%S") if value else ""


def _bounds(cleaned):
    """Aware [start, end) of the date range in the repository's time zone."""
    zone = timezone.get_current_timezone()
    start = end = None
    if cleaned.get("date_from"):
        start = datetime.combine(cleaned["date_from"], time.min, tzinfo=zone)
    if cleaned.get("date_to"):
        end = datetime.combine(cleaned["date_to"] + timedelta(days=1), time.min, tzinfo=zone)
    return start, end


# --- filter forms ---------------------------------------------------------------------------


class ListFilter(forms.Form):
    q = forms.CharField(
        label="Search",
        required=False,
        max_length=200,
        widget=forms.SearchInput(attrs={"placeholder": "Asset ID or name"}),
    )
    per_page = forms.TypedChoiceField(
        label="Rows per page", choices=PAGE_SIZES, coerce=int, required=False
    )
    sort = forms.CharField(required=False, widget=forms.HiddenInput())

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

    def clean(self):
        data = super().clean()
        start, end = data.get("date_from"), data.get("date_to")
        if start and end and start > end:
            self.add_error("date_to", "The end date is before the start date.")
        return data


def _equipment(label="Equipment", **filters):
    queryset = Equipment.objects.filter(**filters).order_by("code")
    return forms.ModelChoiceField(
        label=label, queryset=queryset, required=False, empty_label="Any equipment"
    )


def _dates():
    return (
        forms.DateField(
            label="From", required=False, widget=forms.DateInput(attrs={"type": "date"})
        ),
        forms.DateField(label="To", required=False, widget=forms.DateInput(attrs={"type": "date"})),
    )


def _choice(label, choices, any_label):
    return forms.ChoiceField(label=label, choices=[("", any_label), *choices], required=False)


EQUIPMENT_STATUS = [
    ("", "In use"),
    *Equipment.Status.choices,
    ("all", "Any status"),
]


class EquipmentFilter(ListFilter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["kind"] = _choice("Kind", Equipment.Kind.choices, "Any kind")
        self.fields["status"] = forms.ChoiceField(
            label="Status", choices=EQUIPMENT_STATUS, required=False
        )
        self.fields["storage_unit"] = forms.ModelChoiceField(
            label="Storage unit",
            queryset=StorageUnit.objects.order_by("code"),
            required=False,
            empty_label="Any unit",
        )
        self.fields["low_level"] = forms.BooleanField(
            label="Below the low level",
            required=False,
            help_text="LN2 vessels whose latest level is below the low level your repository set.",
        )


DUE_STATES = [("overdue", "Overdue"), ("upcoming", "Due soon")]
WITHIN = [("7", "Next 7 days"), ("30", "Next 30 days"), ("90", "Next 90 days")]


class DueFilter(ListFilter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["state"] = _choice("Due", DUE_STATES, "All due dates")
        self.fields["within"] = forms.TypedChoiceField(
            label="Due soon within",
            choices=[("", "Next 30 days"), *WITHIN],
            coerce=int,
            empty_value=None,
            required=False,
        )
        self.fields["kind"] = _choice(
            "Service",
            [c for c in ServiceEvent.Kind.choices if c[0] in ServiceEvent.DUE_KINDS],
            "Any service",
        )
        self.fields["equipment_kind"] = _choice(
            "Equipment kind", Equipment.Kind.choices, "Any kind"
        )


class LN2Filter(ListFilter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["q"].widget.attrs["placeholder"] = "Asset ID or reference"
        self.fields["equipment"] = _equipment("Vessel", kind__in=Equipment.LN2_KINDS)
        self.fields["kind"] = _choice("Entry", LN2Entry.Kind.choices, "Level checks and fills")
        self.fields["date_from"], self.fields["date_to"] = _dates()


OUTSIDE = [
    ("any", "Outside the recorded limits"),
    ("uncovered", "Outside, without an excursion"),
]


class ReadingFilter(ListFilter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["q"].widget.attrs["placeholder"] = "Asset ID or probe position"
        self.fields["equipment"] = _equipment(
            kind__in=("ln2_tank", "freezer", "supply_dewar", "other")
        )
        self.fields["outside"] = _choice("Limits", OUTSIDE, "All readings")
        self.fields["source"] = _choice(
            "Source", TemperatureReading.Source.choices, "Entered and imported"
        )
        self.fields["date_from"], self.fields["date_to"] = _dates()


EXCURSION_STATUS = [("active", "Open or awaiting review"), *Excursion.Status.choices]


class ExcursionFilter(ListFilter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["q"].widget.attrs["placeholder"] = "Excursion number or asset ID"
        self.fields["status"] = _choice("Status", EXCURSION_STATUS, "Any status")
        self.fields["equipment"] = _equipment(
            kind__in=("ln2_tank", "freezer", "supply_dewar", "other")
        )
        self.fields["cause_category"] = _choice("Cause", Excursion.Cause.choices, "Any cause")
        self.fields["awaiting_me"] = forms.BooleanField(
            label="Awaiting my review",
            required=False,
            help_text="Submitted by somebody else; managers and administrators review.",
        )
        self.fields["specimen"] = forms.CharField(
            label="Affected specimen",
            required=False,
            max_length=40,
            help_text="Specimen ID. Closed excursions only: the affected specimens are kept when "
            "an excursion is closed; an open one computes them on its page.",
        )
        self.fields["date_from"], self.fields["date_to"] = _dates()
        self.fields["date_from"].help_text = "Started on or after."
        self.fields["date_to"].help_text = "Started on or before."

    def clean_specimen(self):
        return normalize_accession(self.cleaned_data.get("specimen") or "")


IMPORT_STATES = [
    ("validated", "Ready to commit"),
    ("rejected", "Rejected"),
    ("committed", "Committed"),
    ("expired", "Expired"),
]


class ImportFilter(ListFilter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["q"].widget.attrs["placeholder"] = "File name"
        self.fields["state"] = _choice("State", IMPORT_STATES, "Any state")


# --- queries --------------------------------------------------------------------------------


def _search(queryset, text, fields):
    condition = Q()
    for name in fields:
        condition |= Q(**{f"{name}__icontains": text})
    return queryset.filter(condition)


def equipment_rows(cleaned, user):
    queryset = queries.with_latest(Equipment.objects.select_related("storage_unit"))
    status = cleaned.get("status")
    if status == "":
        queryset = queryset.exclude(status=Equipment.Status.RETIRED)
    elif status != "all" and status:
        queryset = queryset.filter(status=status)
    if cleaned.get("kind"):
        queryset = queryset.filter(kind=cleaned["kind"])
    if cleaned.get("storage_unit"):
        queryset = queryset.filter(storage_unit=cleaned["storage_unit"])
    if cleaned.get("low_level"):
        queryset = queryset.filter(pk__in=queries.low_level_equipment().values("pk"))
    return queryset


def due_rows(cleaned, user):
    return queries.due_queryset(
        state=cleaned.get("state") or "",
        within=cleaned.get("within") or 30,
        kind=cleaned.get("kind") or "",
        equipment_kind=cleaned.get("equipment_kind") or "",
    )


def _row(name):
    """The row's own value as a window over itself. Filters on it are applied after the LN2 log's
    window (Django wraps the query), so the change since the previous entry is computed over the
    vessel's whole log, not over the filtered selection."""
    return Window(FirstValue(name), partition_by=[F("pk")])


def ln2_rows(cleaned, user):
    queryset = LN2Entry.objects.select_related("equipment", "supply", "recorded_by")
    if cleaned.get("equipment"):  # whole partitions: safe before the window
        queryset = queryset.filter(equipment=cleaned["equipment"])
    queryset = queries.ln2_log(queryset).annotate(row_kind=_row("kind"), row_at=_row("occurred_at"))
    if cleaned.get("kind"):
        queryset = queryset.filter(row_kind=cleaned["kind"])
    start, end = _bounds(cleaned)
    if start:
        queryset = queryset.filter(row_at__gte=start)
    if end:
        queryset = queryset.filter(row_at__lt=end)
    return queryset


def reading_rows(cleaned, user):
    queryset = queries.effective_readings().select_related("equipment", "device", "recorded_by")
    if cleaned.get("equipment"):
        queryset = queryset.filter(equipment=cleaned["equipment"])
    if cleaned.get("outside") == "any":
        queryset = queryset.filter(queries.OUTSIDE)
    elif cleaned.get("outside") == "uncovered":
        queryset = queryset.filter(pk__in=queries.uncovered_readings().values("pk"))
    if cleaned.get("source"):
        queryset = queryset.filter(source=cleaned["source"])
    start, end = _bounds(cleaned)
    if start:
        queryset = queryset.filter(measured_at__gte=start)
    if end:
        queryset = queryset.filter(measured_at__lt=end)
    return queryset


EXCURSION_NUMBER = re.compile(r"^(?:EX-?)?0*(\d{1,12})$", re.I)


def excursion_rows(cleaned, user):
    queryset = Excursion.objects.select_related("equipment", "storage_unit")
    status = cleaned.get("status")
    if status == "active":
        queryset = queryset.filter(status__in=queries.ACTIVE_EXCURSION)
    elif status:
        queryset = queryset.filter(status=status)
    if cleaned.get("equipment"):
        queryset = queryset.filter(equipment=cleaned["equipment"])
    if cleaned.get("cause_category"):
        queryset = queryset.filter(cause_category=cleaned["cause_category"])
    if cleaned.get("awaiting_me"):
        if has_role(user, *MANAGE_ROLES):
            queryset = queryset.filter(pk__in=queries.awaiting_review(user).values("pk"))
        else:
            queryset = queryset.none()
    if cleaned.get("specimen"):
        captured = AffectedSpecimen.objects.filter(specimen__accession=cleaned["specimen"])
        queryset = queryset.filter(pk__in=captured.values("excursion_id"))
    start, end = _bounds(cleaned)
    if start:
        queryset = queryset.filter(started_at__gte=start)
    if end:
        queryset = queryset.filter(started_at__lt=end)
    return queryset


def search_excursions(queryset, text):
    match = EXCURSION_NUMBER.match(text.strip())
    condition = Q(equipment__code__icontains=text) | Q(equipment__name__icontains=text)
    if match:
        condition |= Q(pk=int(match.group(1)))
    return queryset.filter(condition)


def import_rows(cleaned, user):
    queryset = ReadingImport.objects.select_related("actor", "equipment", "commit").annotate(
        state=Case(
            When(commit__isnull=False, then=Value("committed")),
            When(Q(row_count=0) | ~Q(errors=[]), then=Value("rejected")),
            When(expires_at__lt=timezone.now(), then=Value("expired")),
            default=Value("validated"),
        )
    )
    if cleaned.get("state"):
        queryset = queryset.filter(state=cleaned["state"])
    return queryset


# --- CSV columns ----------------------------------------------------------------------------


def _due_state(event):
    return queries.due_state(event)[0]


def _due_days(event):
    return queries.due_state(event)[2]


def _outside(reading):
    return {"above": "Above upper limit", "below": "Below lower limit"}.get(
        reading.outside_limits, ""
    )


EXPORT = {
    "equipment": (
        ("Asset ID", lambda o: o.code),
        ("Name", lambda o: o.name),
        ("Kind", lambda o: o.get_kind_display()),
        ("Status", lambda o: o.get_status_display()),
        ("Storage unit", lambda o: o.storage_unit.code if o.storage_unit_id else ""),
        ("Location", lambda o: o.location),
        ("Critical", lambda o: o.is_critical),
        ("Lower limit (°C)", lambda o: o.lower_limit),
        ("Upper limit (°C)", lambda o: o.upper_limit),
        ("Latest reading (°C)", lambda o: o.latest_reading_value),
        ("Latest reading at", lambda o: local(o.latest_reading_at)),
        ("Latest level", lambda o: o.latest_level),
        ("Latest level unit", lambda o: o.latest_level_unit or ""),
        ("Latest level at", lambda o: local(o.latest_level_at)),
        ("Low level", lambda o: o.level_low_limit),
        ("Level unit", lambda o: o.level_unit),
        ("Next due", lambda o: o.next_due_on),
        ("Active excursions", lambda o: o.active_excursions or 0),
    ),
    "due": (
        ("Asset ID", lambda o: o.equipment.code),
        ("Equipment", lambda o: o.equipment.name),
        ("Equipment kind", lambda o: o.equipment.get_kind_display()),
        ("Service", lambda o: o.get_kind_display()),
        ("Next due", lambda o: o.next_due_on),
        ("State", _due_state),
        ("Days from today", _due_days),
        ("Last performed", lambda o: o.performed_on),
        ("Outcome", lambda o: o.get_outcome_display()),
    ),
    "ln2": (
        ("When", lambda o: local(o.occurred_at)),
        ("Asset ID", lambda o: o.equipment.code),
        ("Entry", lambda o: o.get_kind_display()),
        ("Level before", lambda o: o.level_before),
        ("Level", lambda o: o.level),
        ("Level unit", lambda o: o.level_unit),
        ("Change since previous", queries.level_change),
        ("Amount added", lambda o: o.amount_added),
        ("Amount unit", lambda o: o.amount_unit),
        ("Supply dewar", lambda o: o.supply.code if o.supply_id else ""),
        ("Method", lambda o: o.get_method_display()),
        ("Reference", lambda o: o.reference),
        ("Outside provider", lambda o: o.performed_by_external),
        ("Recorded by", lambda o: person(o.recorded_by)),
        ("Note", lambda o: o.note),
    ),
    "reading": (
        ("Measured at", lambda o: local(o.measured_at)),
        ("Asset ID", lambda o: o.equipment.code),
        ("Temperature (°C)", lambda o: o.value),
        ("Probe position", lambda o: o.probe_position),
        ("Monitor", lambda o: o.device.code if o.device_id else ""),
        ("Lower limit (°C)", lambda o: o.lower_limit),
        ("Upper limit (°C)", lambda o: o.upper_limit),
        ("Outside limits", _outside),
        ("Source", lambda o: o.get_source_display()),
        ("Recorded by", lambda o: person(o.recorded_by)),
        ("Note", lambda o: o.note),
    ),
    "excursion": (
        ("Excursion", lambda o: o.number),
        ("Status", lambda o: o.get_status_display()),
        ("Asset ID", lambda o: o.equipment.code),
        ("Storage unit", lambda o: o.storage_unit.code if o.storage_unit_id else ""),
        ("Started", lambda o: local(o.started_at)),
        ("Ended", lambda o: local(o.ended_at)),
        ("Extreme (°C)", lambda o: o.extreme_value),
        ("Extreme at", lambda o: local(o.extreme_at)),
        ("Lower limit (°C)", lambda o: o.lower_limit),
        ("Upper limit (°C)", lambda o: o.upper_limit),
        ("Detected by", lambda o: o.get_detection_display()),
        ("Cause category", lambda o: o.get_cause_category_display()),
        ("Cause", lambda o: o.cause),
        ("Action taken", lambda o: o.action),
        ("Impact assessment", lambda o: o.impact),
    ),
}


# --- specs ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class ListSpec:
    key: str
    title: str
    noun: str
    build: Callable  # (cleaned, user) -> queryset
    empty: Callable  # () -> an empty queryset of the right model
    columns: dict  # sort key -> ORM ordering field
    default_sort: str
    form_class: type
    search_fields: tuple = ()
    search: Callable | None = None  # (queryset, text) -> queryset, instead of search_fields
    inline: tuple = ()
    more: tuple = ()
    export_columns: tuple = field(default=())


SPECS = {
    "equipment": ListSpec(
        key="equipment",
        title="Equipment",
        noun="equipment",
        build=equipment_rows,
        empty=Equipment.objects.none,
        columns={
            "code": "code",
            "name": "name",
            "kind": "kind",
            "status": "status",
            "unit": "storage_unit__code",
            "reading": "latest_reading_at",
            "due": "next_due_on",
            "excursions": "active_excursions",
        },
        default_sort="code",
        form_class=EquipmentFilter,
        search_fields=("code", "name", "serial_number", "location"),
        inline=("kind", "status", "storage_unit"),
        more=("low_level",),
        export_columns=EXPORT["equipment"],
    ),
    "due": ListSpec(
        key="due",
        title="Maintenance due",
        noun="due dates",
        build=due_rows,
        empty=ServiceEvent.objects.none,
        columns={
            "due": "next_due_on",
            "equipment": "equipment__code",
            "kind": "kind",
            "performed": "performed_on",
        },
        default_sort="due",
        form_class=DueFilter,
        search_fields=("equipment__code", "equipment__name"),
        inline=("state", "within", "kind"),
        more=("equipment_kind",),
        export_columns=EXPORT["due"],
    ),
    "ln2": ListSpec(
        key="ln2",
        title="LN2 log",
        noun="entries",
        build=ln2_rows,
        empty=LN2Entry.objects.none,
        columns={"when": "occurred_at", "equipment": "equipment__code", "kind": "kind"},
        default_sort="-when",
        form_class=LN2Filter,
        search_fields=("equipment__code", "equipment__name", "reference"),
        inline=("equipment", "kind"),
        more=("date_from", "date_to"),
        export_columns=EXPORT["ln2"],
    ),
    "reading": ListSpec(
        key="reading",
        title="Temperature readings",
        noun="readings",
        build=reading_rows,
        empty=TemperatureReading.objects.none,
        columns={
            "measured": "measured_at",
            "equipment": "equipment__code",
            "value": "value",
        },
        default_sort="-measured",
        form_class=ReadingFilter,
        search_fields=("equipment__code", "equipment__name", "probe_position"),
        inline=("equipment", "outside"),
        more=("source", "date_from", "date_to"),
        export_columns=EXPORT["reading"],
    ),
    "excursion": ListSpec(
        key="excursion",
        title="Temperature excursions",
        noun="excursions",
        build=excursion_rows,
        empty=Excursion.objects.none,
        columns={
            "number": "id",
            "status": "status",
            "equipment": "equipment__code",
            "started": "started_at",
            "ended": "ended_at",
            "extreme": "extreme_value",
        },
        default_sort="-started",
        form_class=ExcursionFilter,
        search=search_excursions,
        inline=("status", "equipment", "cause_category"),
        more=("awaiting_me", "specimen", "date_from", "date_to"),
        export_columns=EXPORT["excursion"],
    ),
    "import": ListSpec(
        key="import",
        title="Temperature readings",
        noun="imports",
        build=import_rows,
        empty=ReadingImport.objects.none,
        columns={"file": "filename", "rows": "row_count", "validated": "recorded_at"},
        default_sort="-validated",
        form_class=ImportFilter,
        search_fields=("filename",),
        inline=("state",),
    ),
}

EXPORT_KINDS = {key for key, spec in SPECS.items() if spec.export_columns}


def filtered(spec, data, user):
    """(form, queryset) for request-style filter data; an invalid form yields no rows."""
    form = spec.form_class(data, user=user)
    if not form.is_valid():
        return form, spec.empty()
    cleaned = form.cleaned_data
    queryset = spec.build(cleaned, user)
    if cleaned.get("q"):
        if spec.search:
            queryset = spec.search(queryset, cleaned["q"])
        else:
            queryset = _search(queryset, cleaned["q"], spec.search_fields)
    return form, queryset


def ordered(spec, queryset, sort):
    """Apply an allowlisted sort with the primary key as the stable tie-break."""
    key = (sort or spec.default_sort).removeprefix("-")
    if key not in spec.columns:
        sort = spec.default_sort
        key = sort.removeprefix("-")
    prefix = "-" if sort.startswith("-") else ""
    return queryset.order_by(prefix + spec.columns[key], prefix + "pk")
