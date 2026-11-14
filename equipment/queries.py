"""Equipment reads shared by pages, CSV exports and the dashboard, so a queue's count equals the
rows its link shows.

"Effective" entries are those nobody has corrected and that are not themselves a withdrawal. The
current due date of a kind of service for one piece of equipment is the `next_due_on` of its latest
effective event of that kind; an event without a due date clears it. Nothing here proposes a date
or a limit.
"""

from datetime import timedelta

from django.db.models import (
    Count,
    Exists,
    F,
    IntegerField,
    Min,
    OuterRef,
    Q,
    Subquery,
    Window,
)
from django.db.models.functions import Lag
from django.utils import timezone as tz

from accounts.roles import require_role
from specimens.models import Hold, Specimen
from storage.services import OPERATE_ROLES

from .models import (
    AffectedSpecimen,
    Equipment,
    Excursion,
    ExcursionEvent,
    LN2Entry,
    ServiceEvent,
    TemperatureReading,
)
from .occupancy import affected_specimens

EFFECTIVE = Q(corrected_by__isnull=True, voided=False)
OUTSIDE = Q(upper_limit__isnull=False, value__gt=F("upper_limit")) | Q(
    lower_limit__isnull=False, value__lt=F("lower_limit")
)
ACTIVE_EXCURSION = ("open", "review")
DASHBOARD_HORIZON = 30  # display horizon of the dashboard queue, not a policy


# ------------------------------------------------------------------ due dates


def due_queryset(*, state="", within=None, kind="", equipment_kind="", today=None):
    """Latest effective event per (equipment, kind) that carries a next due date, for equipment
    that is not retired. `state`: "" (all), "overdue" (before today) or "upcoming" (today to
    today + `within` days)."""
    today = today or tz.localdate()
    latest = (
        ServiceEvent.objects.filter(EFFECTIVE, kind__in=ServiceEvent.DUE_KINDS)
        .order_by("equipment_id", "kind", "-performed_on", "-recorded_at", "-id")
        .distinct("equipment_id", "kind")
        .values("pk")
    )
    queryset = (
        ServiceEvent.objects.filter(pk__in=Subquery(latest), next_due_on__isnull=False)
        .exclude(equipment__status=Equipment.Status.RETIRED)
        .select_related("equipment", "equipment__storage_unit")
    )
    if kind:
        queryset = queryset.filter(kind=kind)
    if equipment_kind:
        queryset = queryset.filter(equipment__kind=equipment_kind)
    if state == "overdue":
        queryset = queryset.filter(next_due_on__lt=today)
    elif state == "upcoming":
        queryset = queryset.filter(
            next_due_on__gte=today,
            next_due_on__lte=today + timedelta(days=30 if within is None else within),
        )
    return queryset


def due_state(event, today=None):
    """("Overdue" | "Due today" | "Scheduled", tone, days from today) for one due row."""
    today = today or tz.localdate()
    days = (event.next_due_on - today).days
    if days < 0:
        return "Overdue", "danger", days
    if days == 0:
        return "Due today", "warning", days
    return "Scheduled", "info", days


def current_due(equipment):
    """{kind: event} of the current due date per kind for one piece of equipment."""
    return {
        event.kind: event
        for event in due_queryset().filter(equipment=equipment).order_by("next_due_on")
    }


# ------------------------------------------------------------------ readings and levels


def effective_readings():
    return TemperatureReading.objects.filter(EFFECTIVE)


def uncovered_readings():
    """Effective readings outside the limits recorded with them, on equipment that is not retired,
    that no excursion of the same equipment covers (start ≤ measured ≤ end, or still open-ended)."""
    covering = Excursion.objects.filter(
        equipment=OuterRef("equipment"), started_at__lte=OuterRef("measured_at")
    ).filter(Q(ended_at__isnull=True) | Q(ended_at__gte=OuterRef("measured_at")))
    return (
        effective_readings()
        .filter(OUTSIDE)
        .exclude(equipment__status=Equipment.Status.RETIRED)
        .exclude(Exists(covering))
    )


def _latest_level():
    return LN2Entry.objects.filter(
        EFFECTIVE, equipment=OuterRef("pk"), level__isnull=False
    ).order_by("-occurred_at", "-recorded_at", "-id")


def _latest_reading():
    return (
        effective_readings()
        .filter(equipment=OuterRef("pk"))
        .order_by("-measured_at", "-recorded_at", "-id")
    )


def with_latest(queryset):
    """Annotate the latest effective reading and level, active excursions and the next due date."""
    latest_level = _latest_level()
    latest_reading = _latest_reading()
    next_due = (
        due_queryset()
        .filter(equipment=OuterRef("pk"))
        .order_by()
        .values("equipment")
        .annotate(first=Min("next_due_on"))
        .values("first")
    )
    active = (
        Excursion.objects.filter(equipment=OuterRef("pk"), status__in=ACTIVE_EXCURSION)
        .order_by()
        .values("equipment")
        .annotate(n=Count("pk"))
        .values("n")
    )
    return queryset.annotate(
        latest_reading_value=Subquery(latest_reading.values("value")[:1]),
        latest_reading_at=Subquery(latest_reading.values("measured_at")[:1]),
        latest_level=Subquery(latest_level.values("level")[:1]),
        latest_level_unit=Subquery(latest_level.values("level_unit")[:1]),
        latest_level_at=Subquery(latest_level.values("occurred_at")[:1]),
        next_due_on=Subquery(next_due[:1]),
        active_excursions=Subquery(active[:1], output_field=IntegerField()),
    )


def low_level_equipment():
    """LN2 vessels in use whose latest effective level, in the unit of their set low level, is
    below it. Vessels without a low level set by the repository are never listed."""
    latest = _latest_level()
    return (
        Equipment.objects.filter(kind__in=Equipment.LN2_KINDS, level_low_limit__isnull=False)
        .exclude(status=Equipment.Status.RETIRED)
        .annotate(
            low_check_level=Subquery(latest.values("level")[:1]),
            low_check_unit=Subquery(latest.values("level_unit")[:1]),
        )
        .filter(low_check_unit=F("level_unit"), low_check_level__lt=F("level_low_limit"))
    )


def ln2_log(queryset):
    """Effective LN2 entries with the previous effective entry's level, unit and time on the same
    vessel (`previous_level`, `previous_unit`, `previous_at`) for the derived change."""
    order = [F("occurred_at").asc(), F("recorded_at").asc(), F("id").asc()]

    def lag(field):
        return Window(Lag(field), partition_by=[F("equipment_id")], order_by=order)

    return queryset.filter(EFFECTIVE).annotate(
        previous_level=lag("level"),
        previous_unit=lag("level_unit"),
        previous_at=lag("occurred_at"),
    )


def level_change(entry):
    """Change since the previous entry's level (negative = consumed), when both are in one unit:
    a level check against the previous level; a fill's level before against it. None otherwise."""
    previous = getattr(entry, "previous_level", None)
    if previous is None or entry.level_unit != getattr(entry, "previous_unit", None):
        return None
    current = entry.level_before if entry.kind == LN2Entry.Kind.FILL else entry.level
    if current is None:
        return None
    return current - previous


# ------------------------------------------------------------------ excursions


def active_excursions():
    return Excursion.objects.filter(status__in=ACTIVE_EXCURSION)


def awaiting_review(user):
    """Excursions in review that `user` may close: submitted by somebody else."""
    return Excursion.objects.filter(status=Excursion.Status.REVIEW).exclude(submitted_by=user)


def excursion_specimens(excursion, *, actor):
    """(rows, captured) — rows have specimen_id, accession, present_from, present_until, location.

    A closed excursion shows the list captured when it was closed (`captured` is the closing
    entry); otherwise the list is computed now from the recorded movements, up to the end or, while
    it is ongoing, up to now.
    """
    if excursion.status == Excursion.Status.CLOSED:
        capture = (
            ExcursionEvent.objects.filter(excursion=excursion, kind=ExcursionEvent.Kind.CLOSED)
            .order_by("-occurred_at", "-id")
            .first()
        )
        rows = list(
            AffectedSpecimen.objects.filter(capture=capture)
            .select_related("specimen")
            .order_by("location", "specimen__accession")
        )
        for row in rows:
            row.accession = row.specimen.accession
        return rows, capture
    if not excursion.storage_unit_id:
        return [], None
    end = excursion.ended_at or tz.now()
    return (
        affected_specimens(
            actor=actor, unit_id=excursion.storage_unit_id, start=excursion.started_at, end=end
        ),
        None,
    )


def excursion_hold_candidates(excursion, *, actor, query=""):
    """All affected rows, with current status and active holds for an explicit selection page.

    Do not omit disposed specimens: show why they cannot receive a hold. Pagination belongs to
    the view, after filtering the whole affected set.
    """
    require_role(actor, *OPERATE_ROLES)
    rows, capture = excursion_specimens(excursion, actor=actor)
    specimens = {
        specimen.pk: specimen
        for specimen in Specimen.objects.filter(pk__in={row.specimen_id for row in rows})
    }
    active = {}
    for hold in Hold.objects.filter(specimen_id__in=specimens, release__isnull=True):
        active.setdefault(hold.specimen_id, []).append(hold.get_scope_display())
    query = query.strip().casefold()
    return [
        {
            "row": row,
            "specimen": specimens[row.specimen_id],
            "active_holds": active.get(row.specimen_id, []),
            "selectable": specimens[row.specimen_id].status != Specimen.Status.DISPOSED,
        }
        for row in rows
        if row.specimen_id in specimens and (not query or query in row.accession.casefold())
    ], capture


def readings_during(excursion):
    """Effective readings of the excursion's equipment from its start to its end (or now)."""
    end = excursion.ended_at or tz.now()
    return (
        effective_readings()
        .filter(equipment=excursion.equipment, measured_at__gte=excursion.started_at)
        .filter(measured_at__lte=end)
        .select_related("device")
        .order_by("measured_at", "recorded_at")
    )
