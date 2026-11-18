"""Specimen reads: lists, lookup, history, thaw count and time out of storage.

Reads take no gate. Every function checks the reader's role first. Filters and sorts are fixed
allow-lists; an unknown key is an error, never silently ignored.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.core.exceptions import ValidationError
from django.db.models import Exists, F, OuterRef, Q
from django.utils import timezone as tz

from accounts.roles import require_role
from storage import queries as storage_queries
from storage.models import LayoutNode

from .models import (
    ACCESSION_PATTERN,
    Collection,
    Correction,
    CustodyEvent,
    Derivation,
    DisposalRequest,
    Hold,
    Participant,
    QuantityEntry,
    QuantityUnit,
    Specimen,
    SpecimenContainerType,
    SpecimenType,
    ThawEvent,
)
from .services import CATALOGUES, READ_ROLES, request_state

SPECIMEN_FILTERS = {
    "q",
    "accession",
    "type",
    "container",
    "status",
    "participant",
    "collection",
    "unit",
    "node",
    "box",
    "held",
    "checked_out",
    "placed",
}
SPECIMEN_SORTS = {
    "accession": ("accession",),
    "registered_at": ("registered_at",),
    "quantity": ("quantity_on_hand",),
    "status": ("status",),
    "type": ("specimen_type__code",),
    "collected_at": ("collected_at",),
}


def normalize_accession(value):
    """Scanner input: outer whitespace trimmed, upper-cased; inner text is never guessed."""
    return value.strip().upper() if isinstance(value, str) else ""


def with_flags(queryset):
    """Annotate `held` (any active hold), `use_held` (a use or all-use hold: no check-out,
    splitting, use or hand-over) and `disposal_held` (a disposal or all-use hold: no disposal)."""
    active = Hold.objects.filter(specimen=OuterRef("pk"), release__isnull=True)
    return queryset.annotate(
        held=Exists(active),
        use_held=Exists(active.filter(scope__in=[Hold.Scope.USE, Hold.Scope.ALL_USE])),
        disposal_held=Exists(active.filter(scope__in=[Hold.Scope.DISPOSAL, Hold.Scope.ALL_USE])),
    )


def _flag(value, name):
    if value in (True, "true", "yes", "1"):
        return True
    if value in (False, "false", "no", "0"):
        return False
    raise ValidationError(f"{name} must be yes or no.", code="invalid")


def specimen_queryset(filters, sort="accession"):
    """The authorized-list queryset for the allow-listed filters and sort (no pagination)."""
    if not isinstance(filters, dict) or set(filters) - SPECIMEN_FILTERS:
        raise ValidationError("Unknown filter.", code="invalid")
    descending = isinstance(sort, str) and sort.startswith("-")
    key = sort.removeprefix("-") if isinstance(sort, str) else None
    if key not in SPECIMEN_SORTS:
        raise ValidationError("Unknown sort.", code="invalid")
    queryset = with_flags(
        Specimen.objects.select_related(
            "specimen_type",
            "container_type",
            "unit",
            "participant",
            "collection",
            "custodian",
            "occupancy__position__box",
        )
    )
    values = {k: v for k, v in filters.items() if v not in (None, "", [], ())}
    if text := values.get("q"):
        text = str(text).strip()
        queryset = queryset.filter(
            Q(accession__icontains=text)
            | Q(participant__code__icontains=text)
            | Q(collection__code__icontains=text)
        )
    if accession := values.get("accession"):
        queryset = queryset.filter(accession=normalize_accession(accession))
    for name, lookup in (
        ("type", "specimen_type"),
        ("container", "container_type"),
        ("participant", "participant"),
        ("collection", "collection"),
        ("unit", "unit"),
    ):
        if name in values:
            queryset = queryset.filter(**{lookup: values[name]})
    if "status" in values:
        if values["status"] not in Specimen.Status.values:
            raise ValidationError("Unknown status.", code="invalid")
        queryset = queryset.filter(status=values["status"])
    if "box" in values:
        queryset = queryset.filter(occupancy__position__box=values["box"])
    if "node" in values:
        node = values["node"]
        node = node if isinstance(node, LayoutNode) else LayoutNode.objects.get(pk=node)
        queryset = queryset.filter(
            Q(occupancy__position__box__node=node)
            | Q(occupancy__position__box__node__path__startswith=node.path + "/")
        )
    if "held" in values:
        queryset = queryset.filter(held=_flag(values["held"], "On hold"))
    if "checked_out" in values:
        queryset = queryset.filter(
            open_checkout__isnull=not _flag(values["checked_out"], "Checked out")
        )
    if "placed" in values:
        queryset = queryset.filter(
            occupancy__position__isnull=not _flag(values["placed"], "In storage")
        )
    order = [("-" if descending else "") + f for f in SPECIMEN_SORTS[key]]
    return queryset.order_by(*order, "pk")


def lookup_specimen(*, actor, accession):
    """Exact match on the normalized accession; raises Specimen.DoesNotExist otherwise."""
    require_role(actor, *READ_ROLES)
    code = normalize_accession(accession)
    if not re.match(ACCESSION_PATTERN, code):
        raise Specimen.DoesNotExist(code)
    return Specimen.objects.get(accession=code)


# --- clocks and counts ---------------------------------------------------------------------


@dataclass(frozen=True)
class ThawSummary:
    baseline: int | None  # effective count before registration; None = unknown
    inherited: int  # observed thaws recorded on ancestors while the baseline was unknown
    observed: int  # effective observed thaws on this specimen
    external: int = 0  # thaws reported by recipients for times away (effective counts)
    unknown_away: tuple = ()  # (dispatched_at or None, returned_at) for each unreported time away

    @property
    def recorded(self):
        return self.inherited + self.observed + self.external

    @property
    def known(self):
        return self.baseline is not None and not self.unknown_away

    @property
    def total(self):
        return self.baseline + self.recorded if self.known else None

    def away_text(self):
        def day(value):
            return tz.localtime(value).strftime("%Y-%m-%d") if value else "?"

        periods = ", ".join(f"{day(start)} to {day(end)}" for start, end in self.unknown_away)
        return f"unknown while away ({periods})"

    def __str__(self):
        if self.baseline is None:
            text = f"Unknown before registration; {self.recorded} recorded"
        elif self.unknown_away:
            text = f"At least {self.baseline + self.recorded}"
        else:
            return str(self.total)
        return f"{text}; {self.away_text()}" if self.unknown_away else text


def _effective(events):
    """Map each root event to the last version of its correction chain."""
    by_target = {e.corrects_id: e for e in events if e.corrects_id}
    roots = [e for e in events if not e.corrects_id]
    result = []
    for root in roots:
        latest = root
        while latest.pk in by_target:
            latest = by_target[latest.pk]
        result.append((root, latest))
    return result


def _dispatched_before(specimen, moment):
    return (
        CustodyEvent.objects.filter(
            specimen=specimen, kind=CustodyEvent.Kind.DISPATCH, occurred_at__lte=moment
        )
        .order_by("-occurred_at", "-recorded_at")
        .values_list("occurred_at", flat=True)
        .first()
    )


def thaw_summary(specimen):
    events = list(
        ThawEvent.objects.filter(specimen=specimen)
        .select_related("custody_event")
        .order_by("recorded_at", "id")
    )
    baseline, inherited, observed, external, unknown = None, 0, 0, 0, []
    for root, latest in _effective(events):
        if root.kind == ThawEvent.Kind.BASELINE:
            baseline, inherited = latest.count, latest.inherited_recorded_count
        elif root.kind == ThawEvent.Kind.THAW:
            observed += latest.count or 0
        elif root.kind == ThawEvent.Kind.EXTERNAL:
            if latest.count is None:
                returned = root.custody_event.occurred_at
                unknown.append((_dispatched_before(specimen, returned), returned))
            else:
                external += latest.count
    return ThawSummary(
        baseline=baseline,
        inherited=inherited,
        observed=observed,
        external=external,
        unknown_away=tuple(sorted(unknown, key=lambda p: p[1])),
    )


@dataclass(frozen=True)
class OutInterval:
    checkout: CustodyEvent  # the check-out, or the return that brought it back in hand
    started: datetime
    ended: datetime | None  # None while still out
    closed_by: str  # "check_in", "dispose", "dispatch" or "" (open)

    @property
    def duration(self):
        return (self.ended or tz.now()) - self.started


@dataclass(frozen=True)
class AwayInterval:
    dispatch: CustodyEvent
    started: datetime
    ended: datetime | None  # the return; None while still away
    destination: str

    @property
    def duration(self):
        return (self.ended or tz.now()) - self.started


@dataclass(frozen=True)
class TimeOut:
    intervals: tuple
    total: timedelta
    open: bool
    unplaced_since_registration: timedelta | None  # registration until first placement
    away: tuple = ()  # AwayInterval: dispatched until returned, not counted in `total`


OPENS = (CustodyEvent.Kind.CHECK_OUT, CustodyEvent.Kind.RETURN)
CLOSES = (CustodyEvent.Kind.CHECK_IN, CustodyEvent.Kind.DISPOSE, CustodyEvent.Kind.DISPATCH)


def time_out_of_storage(specimen, now=None):
    """Sum of the periods in hand outside storage: computed from events, never typed.

    A check-out or a return opens a period; a check-in, a dispatch or the disposal ends it.
    Time away (dispatch until return) is listed separately and not counted: the material was
    outside the repository's custody.
    """
    now = now or tz.now()
    events = list(
        CustodyEvent.objects.filter(specimen=specimen)
        .exclude(kind=CustodyEvent.Kind.CORRECTION)
        .order_by("occurred_at", "recorded_at", "id")
    )
    intervals, away = [], []
    opened = None
    dispatched = None
    for event in events:
        if event.kind in OPENS:
            if event.kind == CustodyEvent.Kind.RETURN and dispatched is not None:
                away.append(
                    AwayInterval(
                        dispatched,
                        dispatched.occurred_at,
                        event.occurred_at,
                        dispatched.destination,
                    )
                )
                dispatched = None
            opened = event
        elif event.kind in CLOSES and opened is not None:
            intervals.append(OutInterval(opened, opened.occurred_at, event.occurred_at, event.kind))
            opened = None
        if event.kind == CustodyEvent.Kind.DISPATCH:
            dispatched = event
    if opened is not None:
        intervals.append(OutInterval(opened, opened.occurred_at, None, ""))
    if dispatched is not None:
        away.append(AwayInterval(dispatched, dispatched.occurred_at, None, dispatched.destination))
    total = sum(((i.ended or now) - i.started for i in intervals), timedelta())
    register = next((e for e in events if e.kind == CustodyEvent.Kind.REGISTER), None)
    disposal = next((e for e in events if e.kind == CustodyEvent.Kind.DISPOSE), None)
    first_place = next(
        (e for e in events if e.kind in (CustodyEvent.Kind.PLACE, CustodyEvent.Kind.CHECK_IN)),
        None,
    )
    unplaced = None
    if register is not None:
        end = (
            first_place.occurred_at if first_place else (disposal.occurred_at if disposal else now)
        )
        unplaced = end - register.occurred_at
    return TimeOut(
        intervals=tuple(intervals),
        total=total,
        open=any(i.ended is None for i in intervals),
        unplaced_since_registration=unplaced,
        away=tuple(away),
    )


def format_duration(delta):
    if delta is None:
        return ""
    minutes = int(delta.total_seconds() // 60)
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    parts = []
    if days:
        parts.append(f"{days} d")
    if hours or days:
        parts.append(f"{hours} h")
    parts.append(f"{minutes} min")
    return " ".join(parts)


# --- history -------------------------------------------------------------------------------


@dataclass
class HistoryItem:
    at: datetime  # when it happened (or was recorded, for records without an occurrence)
    recorded_at: datetime
    kind: str
    title: str
    actor: object = None
    reason: str = ""
    detail: str = ""
    changes: list = field(default_factory=list)
    link: str = ""  # record id this item can act on (custody/thaw/ledger correction)
    sort_id: str = ""


def _q(value, unit):
    return f"{format(value.normalize(), 'f')} {unit.code}"


def _name(user):
    if user is None:
        return "blank"
    return user.get_full_name() or user.get_username()


def specimen_history(*, actor, specimen_id):
    """Every event of one specimen, oldest first, with storage moves merged in once."""
    require_role(actor, *READ_ROLES)
    specimen = Specimen.objects.select_related("unit").get(pk=specimen_id)
    items = []
    custody = list(
        CustodyEvent.objects.filter(specimen=specimen).select_related(
            "actor", "from_custodian", "to_custodian", "move_event", "corrects"
        )
    )
    linked_moves = {e.move_event_id for e in custody if e.move_event_id}
    for event in custody:
        if event.kind == CustodyEvent.Kind.CORRECTION:
            target = event.corrects
            changes = []
            for label, old, new in (
                ("Destination", target.destination, event.destination),
                ("From", _name(target.from_custodian), _name(event.from_custodian)),
                ("To", _name(target.to_custodian), _name(event.to_custodian)),
            ):
                if old != new:
                    changes.append((label, old, new))
            items.append(
                HistoryItem(
                    event.occurred_at,
                    event.recorded_at,
                    "custody_correction",
                    f"Custody correction of “{target.get_kind_display()}” "
                    f"({tz.localtime(target.occurred_at):%Y-%m-%d %H:%M})",
                    event.actor,
                    event.reason,
                    changes=changes,
                    link=str(event.pk),
                    sort_id=str(event.pk),
                )
            )
            continue
        detail = []
        if event.kind == CustodyEvent.Kind.DISPATCH:
            detail.append(f"From {_name(event.from_custodian)} to outside the repository")
        elif event.kind == CustodyEvent.Kind.RETURN:
            detail.append(f"Received by {_name(event.to_custodian)}")
        elif event.from_custodian_id and event.from_custodian_id != event.to_custodian_id:
            detail.append(f"From {_name(event.from_custodian)} to {_name(event.to_custodian)}")
        elif event.kind == CustodyEvent.Kind.REGISTER:
            detail.append(f"Custodian {_name(event.to_custodian)}")
        if event.destination:
            label = "Returned from" if event.kind == CustodyEvent.Kind.RETURN else "Destination"
            detail.append(f"{label}: {event.destination}")
        if event.move_event_id:
            move = event.move_event
            if move.from_path.get("display"):
                detail.append(f"From {move.from_path['display']}")
            if move.to_path.get("display"):
                detail.append(f"To {move.to_path['display']}")
            if move.corrects_id:
                detail.append("Recorded as a correction of an earlier movement")
        if event.kind == CustodyEvent.Kind.CHECK_IN:
            detail.append(
                {True: "Thaw observed", False: "No thaw observed", None: "Thaw not known"}[
                    event.thaw_observed
                ]
            )
        items.append(
            HistoryItem(
                event.occurred_at,
                event.recorded_at,
                f"custody_{event.kind}",
                event.get_kind_display(),
                event.actor,
                event.reason,
                " · ".join(detail),
                link=str(event.pk),
                sort_id=str(event.pk),
            )
        )
    for member in storage_queries.movement_history(actor=actor, occupancy_id=specimen.occupancy_id):
        move = member.event
        if move.pk in linked_moves:
            continue
        title = "Container moved" if move.is_container_move else move.get_kind_display()
        items.append(
            HistoryItem(
                move.occurred_at,
                move.recorded_at,
                "storage_move",
                title,
                move.actor,
                move.reason,
                f"From {member.from_path.get('display') or 'not in storage'} to "
                f"{member.to_path.get('display') or 'not in storage'}",
                sort_id=str(move.pk),
            )
        )
    for entry in QuantityEntry.objects.filter(specimen=specimen).select_related("actor", "unit"):
        title = entry.get_kind_display()
        sign = "+" if entry.delta > 0 else "−"
        items.append(
            HistoryItem(
                entry.occurred_at,
                entry.recorded_at,
                f"quantity_{entry.kind}",
                f"Amount: {title}",
                entry.actor,
                entry.reason,
                f"{sign}{_q(abs(entry.delta), entry.unit)}, balance {
                    _q(entry.balance_after, entry.unit)
                }"
                + (f" (entered as “{entry.supplied_text}”)" if entry.supplied_text else ""),
                link=str(entry.pk),
                sort_id=str(entry.pk),
            )
        )
    for derivation in (
        Derivation.objects.filter(parent=specimen)
        .prefetch_related("children__child")
        .select_related("actor", "unit")
    ):
        names = ", ".join(c.child.accession for c in derivation.children.all())
        items.append(
            HistoryItem(
                derivation.occurred_at,
                derivation.recorded_at,
                "derivation",
                f"{derivation.get_kind_display()} made",
                derivation.actor,
                derivation.reason,
                f"Used {_q(derivation.consumed, derivation.unit)} for {names}"
                + (f" · Procedure: {derivation.procedure}" if derivation.procedure else ""),
                sort_id=str(derivation.pk),
            )
        )
    for event in ThawEvent.objects.filter(specimen=specimen).select_related("actor", "corrects"):
        if event.kind == ThawEvent.Kind.BASELINE:
            detail = "Unknown" if event.count is None else f"{event.count} prior thaw(s)"
            if event.inherited_recorded_count:
                detail += f"; {event.inherited_recorded_count} recorded on the parent"
        elif event.kind == ThawEvent.Kind.CORRECTION:
            old = "unknown" if event.corrects.count is None else str(event.corrects.count)
            new = "unknown" if event.count is None else str(event.count)
            detail = f"Count {old} → {new}"
        elif event.kind == ThawEvent.Kind.EXTERNAL:
            detail = (
                "Not reported: unknown while away"
                if event.count is None
                else f"{event.count} thaw(s) reported by the recipient"
            )
        else:
            detail = "One observed thaw"
        items.append(
            HistoryItem(
                event.occurred_at,
                event.recorded_at,
                f"thaw_{event.kind}",
                event.get_kind_display(),
                event.actor,
                event.reason,
                detail,
                link=str(event.pk),
                sort_id=str(event.pk),
            )
        )
    for hold in Hold.objects.filter(specimen=specimen).select_related("actor", "release__actor"):
        items.append(
            HistoryItem(
                hold.occurred_at,
                hold.recorded_at,
                "hold",
                f"{hold.get_scope_display()} placed",
                hold.actor,
                hold.reason,
                f"For: {hold.reference}" if hold.reference else "",
                link=str(hold.pk),
                sort_id=str(hold.pk),
            )
        )
        if hasattr(hold, "release"):
            release = hold.release
            items.append(
                HistoryItem(
                    release.occurred_at,
                    release.recorded_at,
                    "hold_release",
                    f"{hold.get_scope_display()} released",
                    release.actor,
                    release.reason,
                    f"For: {hold.reference}" if hold.reference else "",
                    sort_id=str(release.pk),
                )
            )
    for request in DisposalRequest.objects.filter(specimen=specimen).select_related(
        "requested_by", "decision__actor", "execution"
    ):
        items.append(
            HistoryItem(
                request.recorded_at,
                request.recorded_at,
                "disposal_request",
                "Disposal requested",
                request.requested_by,
                request.reason,
                f"Method: {request.method} · Destination: {request.destination}",
                link=str(request.pk),
                sort_id=str(request.pk),
            )
        )
        if hasattr(request, "decision"):
            decision = request.decision
            items.append(
                HistoryItem(
                    decision.recorded_at,
                    decision.recorded_at,
                    f"disposal_{decision.decision}",
                    f"Disposal {decision.get_decision_display().lower()}",
                    decision.actor,
                    decision.reason,
                    link=str(request.pk),
                    sort_id=str(decision.pk),
                )
            )
    for correction in Correction.objects.filter(specimen=specimen).select_related("actor"):
        changes = [
            (
                name.replace("_", " ").capitalize(),
                correction.before.get(name),
                correction.after.get(name),
            )
            for name in correction.after
        ]
        items.append(
            HistoryItem(
                correction.recorded_at,
                correction.recorded_at,
                "correction",
                "Annotation after disposal (record not changed)"
                if correction.annotation
                else "Specimen details corrected",
                correction.actor,
                correction.reason,
                changes=changes,
                sort_id=str(correction.pk),
            )
        )
    items.sort(key=lambda item: (item.at, item.recorded_at, item.sort_id))
    return tuple(items)


# --- related records -----------------------------------------------------------------------


def disposal_requests(specimen):
    occupancy = specimen.occupancy
    rows = list(
        DisposalRequest.objects.filter(specimen=specimen)
        .select_related("requested_by", "decision__actor", "execution__actor")
        .order_by("-recorded_at")
    )
    for row in rows:
        row.state = request_state(row, specimen, occupancy)
    return rows


DISPOSAL_FILTERS = {"q", "state", "mine", "actor", "awaiting_me"}
DISPOSAL_STATES = ("pending", "approved", "stale", "rejected", "cancelled", "executed")
_CURRENT_REQUEST = Q(
    specimen_revision=F("specimen__revision"),
    occupancy_revision=F("specimen__occupancy__revision"),
)
_CURRENT_APPROVAL = Q(
    decision__approved_revision=F("specimen__revision"),
    decision__approved_occupancy_revision=F("specimen__occupancy__revision"),
)
DISPOSAL_STATE_FILTERS = {
    # Same meaning as services.request_state, expressed in SQL for lists and queues.
    "pending": Q(decision__isnull=True, execution__isnull=True) & _CURRENT_REQUEST,
    "approved": Q(decision__decision="approve", execution__isnull=True) & _CURRENT_APPROVAL,
    "stale": (Q(decision__isnull=True, execution__isnull=True) & ~_CURRENT_REQUEST)
    | (Q(decision__decision="approve", execution__isnull=True) & ~_CURRENT_APPROVAL),
    "rejected": Q(decision__decision="reject"),
    "cancelled": Q(decision__decision="cancel"),
    "executed": Q(execution__isnull=False),
}


def disposal_queryset(filters):
    """Disposal requests for the list and queues.

    filters: q (accession text), state (one of DISPOSAL_STATES), mine (requested by `actor`),
    awaiting_me (pending requests `actor` may approve: not requested by them), actor (a User,
    required by mine/awaiting_me). Each row gets `.state` from services.request_state when read
    through `with_states`.
    """
    if set(filters) - DISPOSAL_FILTERS:
        raise ValidationError("Unknown filter.", code="invalid")
    queryset = DisposalRequest.objects.select_related(
        "specimen__occupancy", "requested_by", "decision__actor", "execution"
    ).only(
        "recorded_at",
        "method",
        "destination",
        "specimen_revision",
        "occupancy_revision",
        "specimen__accession",
        "specimen__revision",
        "specimen__occupancy__revision",
        "requested_by__username",
        "requested_by__first_name",
        "requested_by__last_name",
        "decision__decision",
        "decision__approved_revision",
        "decision__approved_occupancy_revision",
        "decision__actor__username",
        "decision__actor__first_name",
        "decision__actor__last_name",
        "execution__id",
    )
    if text := (filters.get("q") or "").strip():
        queryset = queryset.filter(specimen__accession__icontains=text)
    if state := filters.get("state"):
        if state not in DISPOSAL_STATE_FILTERS:
            raise ValidationError("Unknown state.", code="invalid")
        queryset = queryset.filter(DISPOSAL_STATE_FILTERS[state])
    actor = filters.get("actor")
    if filters.get("mine"):
        queryset = queryset.filter(requested_by=actor)
    if filters.get("awaiting_me"):
        queryset = queryset.filter(DISPOSAL_STATE_FILTERS["pending"]).exclude(requested_by=actor)
    return queryset


def with_states(requests):
    """Attach `.state` to each DisposalRequest (an iterable already fetched)."""
    rows = list(requests)
    for row in rows:
        row.state = request_state(row, row.specimen, row.specimen.occupancy)
    return rows


# --- catalogues ----------------------------------------------------------------------------

CATALOGUE_FILTERS = {"active", "code", "name", "q"}
CATALOGUE_SORTS = {"code": ("code",), "name": ("name",)}
CATALOGUE_MODELS = {
    "participant": Participant,
    "collection": Collection,
    "specimen_type": SpecimenType,
    "container_type": SpecimenContainerType,
    "unit": QuantityUnit,
}
assert set(CATALOGUE_MODELS) == set(CATALOGUES)


def catalogue_queryset(kind, filters, sort="code"):
    if kind not in CATALOGUE_MODELS:
        raise ValidationError("Unknown catalogue.", code="invalid")
    if not isinstance(filters, dict) or set(filters) - CATALOGUE_FILTERS:
        raise ValidationError("Unknown filter.", code="invalid")
    descending = isinstance(sort, str) and sort.startswith("-")
    key = sort.removeprefix("-") if isinstance(sort, str) else None
    if key not in CATALOGUE_SORTS or (key == "name" and kind == "participant"):
        raise ValidationError("Unknown sort.", code="invalid")
    queryset = CATALOGUE_MODELS[kind].objects.all()
    active = filters.get("active")
    if active not in (None, ""):
        if active not in (True, False, "true", "false"):
            raise ValidationError("Active must be true or false.", code="invalid")
        queryset = queryset.filter(is_active=active in (True, "true"))
    if text := str(filters.get("q") or "").strip():
        condition = Q(code__icontains=text)
        if kind != "participant":
            condition |= Q(name__icontains=text)
        queryset = queryset.filter(condition)
    if filters.get("code"):
        queryset = queryset.filter(code__icontains=str(filters["code"]).strip())
    if filters.get("name") and kind != "participant":
        queryset = queryset.filter(name__icontains=str(filters["name"]).strip())
    if kind == "container_type":
        queryset = queryset.select_related("capacity_unit")
    order = [("-" if descending else "") + f for f in CATALOGUE_SORTS[key]]
    return queryset.order_by(*order, "pk")


def locations_for(*, actor, specimens):
    """{specimen.pk: path dict or None} via storage's opaque keys."""
    keys = [s.occupancy_id for s in specimens]
    paths = storage_queries.locations(actor=actor, occupancy_ids=keys)
    return {s.pk: paths.get(s.occupancy_id) for s in specimens}
