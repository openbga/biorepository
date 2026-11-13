"""Equipment pages: lists, the equipment and excursion records, the readings import page, CSV
exports and attachment downloads. Forms and actions are in equipment.action_views.

Views handle transport: role check at entry, filters through equipment.lists, calling
equipment.services and showing its errors. Every rule is enforced again by the service; a control
hidden here is a courtesy, not the gate.
"""

import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Exists, OuterRef, Subquery
from django.http import Http404, HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods

from accounts.roles import has_role, require_role, staff_role_required
from core.attachments import download_response
from core.grids import GridRequest
from core.models import Attachment, AuditEvent

from . import exports, lists, queries, services
from .forms import ImportCommitForm, apply_errors
from .models import (
    Equipment,
    Excursion,
    ExcursionEvent,
    LN2Entry,
    ReadingImport,
    ReadingImportCommit,
    ServiceEvent,
)

READ = services.READ_ROLES
OPERATE = services.OPERATE_ROLES
MANAGE = services.MANAGE_ROLES
RECENT = 20  # rows of a log shown on the equipment record; the full log is its list
SHOWN = 200  # rows of a related table shown on a record before "and N more"


def flags(user):
    """Which controls to render. Services and view decorators still enforce every action."""
    return {"can_manage": has_role(user, *MANAGE), "can_operate": has_role(user, *OPERATE)}


def may_correct(user, entry):
    """Managers correct any entry; operators the entries they recorded (as the service rules)."""
    if has_role(user, *MANAGE):
        return True
    return has_role(user, *OPERATE) and entry.recorded_by_id == user.pk


def history(obj, limit=50):
    """Audit events of one record, newest first (the template shows them newest last)."""
    return list(
        AuditEvent.objects.filter(object_type=obj._meta.label_lower, object_id=str(obj.pk))
        .select_related("actor")
        .order_by("-occurred_at", "-id")[:limit]
    )


def superseded(model):
    """Annotations for correctable entries: whether a newer row corrects it, and which."""
    newer = model.objects.filter(corrects=OuterRef("pk"))
    return {"is_superseded": Exists(newer), "superseded_by": Subquery(newer.values("pk")[:1])}


# --- lists ----------------------------------------------------------------------------------


def chips(request, form):
    """Removable filter chips from the validated filters (defaults are not active filters)."""
    result = []
    for name, value in form.cleaned_data.items():
        if name in {"sort", "page", "per_page", "q"} or value in (None, "", "all", False):
            continue
        field = form.fields[name]
        if value is True:
            display = "Yes"
        elif hasattr(field, "queryset"):
            display = str(value)
        elif hasattr(field, "choices"):
            display = dict((str(k), v) for k, v in field.choices).get(str(value), value)
        else:
            display = value
        params = request.GET.copy()
        params.pop(name, None)
        params.pop("page", None)
        result.append(
            {"label": field.label, "value": str(display), "remove_url": "?" + params.urlencode()}
        )
    return result


def grid_response(request, spec, template, *, export=True, extra=None, more=None):
    try:
        grid = GridRequest.parse(request.GET, spec.columns, spec.default_sort)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    data = request.GET.copy()
    data["q"] = grid.query
    form, queryset = lists.filtered(spec, data, request.user)
    page_obj = Paginator(lists.ordered(spec, queryset, grid.sort), grid.per_page).get_page(
        grid.page_number
    )
    active = chips(request, form) if form.is_valid() else []
    cleaned = getattr(form, "cleaned_data", {})
    more = spec.more if more is None else more
    query = request.GET.copy()
    query.pop("page", None)
    export_url = reverse("equipment:export", args=[spec.key]) if export else ""
    return render(
        request,
        template,
        {
            "page_title": spec.title,
            "page_obj": page_obj,
            "filter_form": form,
            "q": grid.query,
            "sort": grid.sort,
            "active_filters": active,
            "filtered": bool(active or grid.query),
            "inline": [form[name] for name in spec.inline if name in form.fields],
            "more": [form[name] for name in more if name in form.fields],
            "more_open": any(cleaned.get(name) for name in more),
            "noun": spec.noun,
            "export_url": f"{export_url}?{query.urlencode()}" if export_url else "",
            **flags(request.user),
            **(extra or {}),
        },
    )


@staff_role_required(*READ)
def equipment_list(request):
    return grid_response(request, lists.SPECS["equipment"], "equipment/equipment_list.html")


@staff_role_required(*READ)
def due_list(request):
    return grid_response(request, lists.SPECS["due"], "equipment/due_list.html")


def _create_url(request, name):
    """The create form's URL, preselecting the equipment the list is filtered to."""
    url = reverse(name)
    try:
        return f"{url}?equipment={uuid.UUID(request.GET.get('equipment', ''))}"
    except ValueError:
        return url


@staff_role_required(*READ)
def ln2_list(request):
    return grid_response(
        request,
        lists.SPECS["ln2"],
        "equipment/ln2_list.html",
        extra={"create_url": _create_url(request, "equipment:ln2-create")},
    )


@staff_role_required(*READ)
def reading_list(request):
    return grid_response(
        request,
        lists.SPECS["reading"],
        "equipment/reading_list.html",
        extra={"create_url": _create_url(request, "equipment:reading-create")},
    )


@staff_role_required(*READ)
def excursion_list(request):
    spec = lists.SPECS["excursion"]
    more = (
        spec.more
        if has_role(request.user, *MANAGE)
        else tuple(name for name in spec.more if name != "awaiting_me")
    )
    return grid_response(request, spec, "equipment/excursion_list.html", more=more)


def import_state(batch, now=None):
    if hasattr(batch, "state"):
        return batch.state
    if _committed(batch):
        return "committed"
    if batch.errors or not batch.row_count:
        return "rejected"
    if batch.expires_at < (now or timezone.now()):
        return "expired"
    return "validated"


def _committed(batch):
    try:
        return batch.commit is not None
    except ReadingImport.commit.RelatedObjectDoesNotExist:
        return False


def import_name(batch):
    return batch.filename or f"Import {timezone.localtime(batch.recorded_at):%Y-%m-%d %H:%M}"


@staff_role_required(*READ)
def import_list(request):
    return grid_response(request, lists.SPECS["import"], "equipment/import_list.html", export=False)


# --- CSV and attachments --------------------------------------------------------------------


@staff_role_required(*READ)
@require_GET
def export(request, kind):
    if kind not in lists.EXPORT_KINDS:
        raise Http404
    params = request.GET.copy()
    sort = params.pop("sort", [""])[-1]
    try:
        content = exports.export_list(actor=request.user, kind=kind, filters=params, sort=sort)
    except ValidationError as exc:
        return HttpResponseBadRequest(" ".join(exc.messages))
    response = HttpResponse(content, content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="equipment-{kind}.csv"'
    response["Cache-Control"] = "no-store"
    return response


ATTACHMENT_MODELS = {"service": ServiceEvent, "excursion-event": ExcursionEvent}


@staff_role_required(*READ)
@require_GET
def attachment(request, kind, pk):
    model = ATTACHMENT_MODELS.get(kind)
    if model is None:
        raise Http404
    # Authorized through the referring record; an Attachment has no route of its own.
    obj = get_object_or_404(model.objects.only("pk", "attachment"), pk=pk)
    if not obj.has_attachment:
        raise Http404
    return download_response(Attachment.objects.with_data().get(pk=obj.attachment_id))


# --- equipment record -----------------------------------------------------------------------


def equipment_crumbs(equipment=None, *rest):
    trail = [("Equipment", reverse("equipment:equipment-list"))]
    if equipment is not None:
        trail.append((equipment.code, reverse("equipment:equipment-detail", args=[equipment.pk])))
    return [*trail, *rest]


@staff_role_required(*READ)
def equipment_detail(request, pk):
    equipment = get_object_or_404(
        queries.with_latest(Equipment.objects.select_related("storage_unit", "created_by")), pk=pk
    )
    user = request.user
    allowed = flags(user)
    due = queries.current_due(equipment)
    due_rows = [
        (label, due.get(kind))
        for kind, label in ServiceEvent.Kind.choices
        if kind in ServiceEvent.DUE_KINDS
    ]
    events = (
        ServiceEvent.objects.filter(equipment=equipment)
        .select_related("performed_by", "recorded_by", "corrects", "attachment")
        .defer("attachment__data")
        .annotate(**superseded(ServiceEvent))
        .order_by("-performed_on", "-recorded_at", "-id")
    )
    event_count = events.count()
    events = list(events[:SHOWN])
    for event in events:
        event.may_correct = (
            not event.is_superseded and not event.voided and may_correct(user, event)
        )
    ln2 = []
    if equipment.keeps_ln2_log:
        ln2 = list(
            queries.ln2_log(LN2Entry.objects.filter(equipment=equipment))
            .select_related("supply", "recorded_by")
            .order_by("-occurred_at", "-recorded_at", "-id")[:RECENT]
        )
    readings = []
    if equipment.takes_readings:
        readings = list(
            queries.effective_readings()
            .filter(equipment=equipment)
            .select_related("device", "recorded_by")
            .order_by("-measured_at", "-recorded_at", "-id")[:RECENT]
        )
    excursions = list(
        equipment.excursions.select_related("storage_unit").order_by("-started_at", "-id")
    )
    status_changes = list(
        equipment.status_changes.select_related("actor").order_by("-occurred_at", "-recorded_at")
    )
    open_for_entries = not equipment.is_retired
    retired_note = "Retired equipment takes no new entries. Change its status to record more."
    toc = [
        ("facts-heading", "Equipment"),
        ("limits-heading", "Limits"),
        ("due-heading", "Due dates"),
        ("service-heading", f"Service events ({event_count})"),
    ]
    if equipment.keeps_ln2_log:
        toc.append(("ln2-heading", "LN2 log"))
    if equipment.takes_readings:
        toc += [("readings-heading", "Temperature readings"), ("excursions-heading", "Excursions")]
    toc += [("status-heading", "Status history"), ("history-heading", "History")]
    return render(
        request,
        "equipment/equipment_detail.html",
        {
            "page_title": equipment.code,
            "equipment": equipment,
            "due_rows": due_rows,
            "events": events,
            "event_count": event_count,
            "ln2": ln2,
            "readings": readings,
            "excursions": excursions,
            "status_changes": status_changes,
            "open_for_entries": open_for_entries,
            "retired_note": retired_note,
            "toc": toc,
            "history": history(equipment),
            "recent": RECENT,
            **allowed,
        },
    )


# --- excursion record -----------------------------------------------------------------------


def excursion_crumbs(excursion=None, *rest):
    trail = [("Temperature excursions", reverse("equipment:excursion-list"))]
    if excursion is not None:
        trail.append((excursion.number, reverse("equipment:excursion-detail", args=[excursion.pk])))
    return [*trail, *rest]


CHANGE_LABELS = {
    "started_at": "Started",
    "ended_at": "Ended",
    "extreme_value": "Extreme (°C)",
    "extreme_at": "Extreme reached at",
    "detection": "Detected by",
    "cause_category": "Cause category",
    "cause": "Cause",
    "action": "Action taken",
    "impact": "Impact assessment",
}
CHANGE_CHOICES = {
    "detection": dict(Excursion.Detection.choices),
    "cause_category": dict(Excursion.Cause.choices),
}


def _display(name, value):
    if value in (None, ""):
        return ""
    if name in CHANGE_CHOICES:
        return CHANGE_CHOICES[name].get(value, value)
    if name.endswith("_at"):
        try:
            return timezone.localtime(datetime.fromisoformat(value)).strftime("%Y-%m-%d %H:%M")
        except TypeError, ValueError:
            return value
    return str(value)


def change_rows(event):
    rows = []
    for name, pair in (event.changes or {}).items():
        if isinstance(pair, list) and len(pair) == 2:
            rows.append(
                (CHANGE_LABELS.get(name, name), _display(name, pair[0]), _display(name, pair[1]))
            )
    return rows


def _per_page(request):
    try:
        size = int(request.GET.get("per_page", 25))
    except TypeError, ValueError:
        return 25
    return size if size in (25, 50, 100) else 25


@staff_role_required(*READ)
def excursion_detail(request, pk):
    excursion = get_object_or_404(
        Excursion.objects.select_related(
            "equipment", "storage_unit", "opened_by", "submitted_by", "closed_by"
        ),
        pk=pk,
    )
    user = request.user
    allowed = flags(user)
    rows, capture = queries.excursion_specimens(excursion, actor=user)
    specimens = Paginator(rows, _per_page(request)).get_page(request.GET.get("page"))
    readings = queries.readings_during(excursion).select_related("recorded_by")
    reading_count = readings.count()
    readings = list(readings[:SHOWN])
    events = list(
        excursion.events.select_related("actor", "attachment")
        .defer("attachment__data")
        .order_by("occurred_at", "id")
    )
    for event in events:
        event.change_rows = change_rows(event)
        if event.kind == ExcursionEvent.Kind.CLOSED and "affected_specimens" in event.changes:
            count = event.changes["affected_specimens"]
            event.detail = f"{count} specimen{'s' if count != 1 else ''} captured as affected."
    status = excursion.status
    own_submission = status == Excursion.Status.REVIEW and excursion.submitted_by_id == user.pk
    toc = [
        ("facts-heading", "Excursion"),
        ("specimens-heading", f"Specimens ({len(rows)})"),
        ("readings-heading", f"Readings ({reading_count})"),
        ("timeline-heading", "Timeline"),
        ("history-heading", "History"),
    ]
    return render(
        request,
        "equipment/excursion_detail.html",
        {
            "page_title": excursion.number,
            "excursion": excursion,
            "specimens": specimens,
            "specimen_count": len(rows),
            "capture": capture,
            "computed_until": excursion.ended_at or timezone.now(),
            "readings": readings,
            "reading_count": reading_count,
            "shown": SHOWN,
            "events": events,
            "own_submission": own_submission,
            "toc": toc,
            "history": history(excursion),
            **allowed,
        },
    )


# --- readings import (Document page) --------------------------------------------------------


def import_crumbs(batch=None, *rest):
    trail = [
        ("Temperature readings", None),
        ("Imports", reverse("equipment:import-list")),
    ]
    if batch is not None:
        trail.append((import_name(batch), reverse("equipment:import-detail", args=[batch.pk])))
    return [*trail, *rest]


def _preview(rows, devices):
    result = []
    for row in rows:
        try:
            measured = timezone.localtime(datetime.fromisoformat(row["measured_at"]))
        except KeyError, TypeError, ValueError:
            measured = None
        try:
            value = Decimal(row.get("value", ""))
        except InvalidOperation:
            value = row.get("value")
        result.append(
            {
                **row,
                "measured": measured,
                "number": value,
                "device_code": devices.get(row.get("device") or "", ""),
            }
        )
    return result


@staff_role_required(*READ)
@require_http_methods(["GET", "POST"])
def import_detail(request, pk):
    batch = get_object_or_404(ReadingImport.objects.select_related("actor", "equipment"), pk=pk)
    form = ImportCommitForm(request.POST if request.method == "POST" else None)
    status = 200
    if request.method == "POST":
        require_role(request.user, *OPERATE)
        if form.is_valid():
            try:
                commit = services.commit_reading_import(
                    actor=request.user, batch_id=batch.pk, reason=form.cleaned_data["reason"]
                )
            except ValidationError as exc:
                apply_errors(form, exc)
            else:
                messages.success(
                    request,
                    f"Recorded {commit.reading_count} reading"
                    f"{'s' if commit.reading_count != 1 else ''} from {import_name(batch)}.",
                )
                return redirect("equipment:import-detail", pk=batch.pk)
        status = 422
    commit = ReadingImportCommit.objects.select_related("actor").filter(batch=batch).first()
    state = "committed" if commit else import_state(batch)
    preview = batch.normalized_rows[:SHOWN]
    device_ids = {row.get("device") for row in preview if row.get("device")}
    devices = {str(e.pk): e.code for e in Equipment.objects.filter(pk__in=device_ids).only("code")}
    return render(
        request,
        "equipment/import_detail.html",
        {
            "page_title": import_name(batch),
            "crumbs": import_crumbs(None, (import_name(batch), None)),
            "batch": batch,
            "state": state,
            "commit": commit,
            "readings_url": reverse("equipment:reading-list"),
            "rows": _preview(preview, devices),
            "row_total": len(batch.normalized_rows),
            "shown": SHOWN,
            "form": form,
            **flags(request.user),
        },
        status=status,
    )
