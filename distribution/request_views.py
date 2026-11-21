"""Request, allocation, picking, shipment, receipt and return pages.

Lists, record pages, action forms and entry grids in the shared form dialog (with a full page as
the no-JavaScript fallback), the pick list and manifest document pages and their PDFs, CSV
exports and attachment downloads.

Views handle transport only: the role check at entry, revisions captured when a form opened,
calling one distribution.services function and showing its errors under the fields they concern.
Every rule is enforced again by the service under lock; a control hidden or disabled here is a
courtesy, not the gate. The software records decisions and declarations people make; it never
decides consent, approves on a committee's behalf or classifies a shipment.
"""

import uuid

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import Http404, HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods

from accounts.roles import can_decide_requests, has_role, staff_role_required
from core.attachments import download_response
from core.grids import GridRequest, export_error_response
from core.models import Attachment, AuditEvent
from specimens.forms import box_choices, suggestion_text
from specimens.models import Specimen
from storage import queries as storage_queries
from storage.models import Occupancy

from . import documents, exports, queries, request_lists, services
from .export_views import export_action
from .models import (
    Allocation,
    AllocationEvent,
    RequestDecision,
    RequestItem,
    Shipment,
    ShipmentEvent,
    ShipmentItem,
    ShipmentReceipt,
    SpecimenRequest,
    SpecimenReturn,
    Study,
)
from .request_forms import (
    ITEM_FIELDS,
    AgreementForm,
    AllocateForm,
    ContentsForm,
    DecideForm,
    DispatchForm,
    ItemFormSet,
    ItemsForm,
    NotifyForm,
    PackForm,
    PickForm,
    PickFormSet,
    PutBackForm,
    PutBackFormSet,
    ReasonForm,
    ReceiptForm,
    ReleaseForm,
    RequestForm,
    RequestNoteForm,
    ReturnForm,
    ReturnReviewForm,
    ShipmentForm,
    ShipmentNoteForm,
    SubmitForm,
    apply_errors,
)

READ = services.READ_ROLES
OPERATE = services.OPERATE_ROLES
MANAGE = services.MANAGE_ROLES
ACTION_METHODS = require_http_methods(["GET", "POST"])
FORM_TEMPLATE = "distribution/requests/form.html"
CANDIDATE_PAGE_SIZES = (25, 50, 100)
SHOWN = 200  # rows of a related table on a record page before "first N shown"

ATTACHMENT_KINDS = ("shipment-event", "receipt", "return")
EXPORT_KINDS = tuple(request_lists.EXPORT_KINDS)
ATTACHMENT_MODELS = {
    "shipment-event": ShipmentEvent,
    "receipt": ShipmentReceipt,
    "return": SpecimenReturn,
}


# --- shared ---------------------------------------------------------------------------------


def flags(user):
    """Which controls to render. Services and view decorators still enforce every action."""
    can_operate = has_role(user, *OPERATE)
    return {
        "can_manage": has_role(user, *MANAGE),
        "can_operate": can_operate,
        "can_decide": can_operate and can_decide_requests(user),
    }


def _who(user):
    return (user.get_full_name() or user.get_username()) if user else ""


def _when(value):
    return timezone.localtime(value).strftime("%Y-%m-%d %H:%M") if value else ""


def _uuid(value):
    try:
        return uuid.UUID(str(value))
    except TypeError, ValueError:
        return None


def _post(request):
    return request.POST if request.method == "POST" else None


def _files(request):
    return request.FILES if request.method == "POST" else None


def _errors(exc):
    if hasattr(exc, "error_dict"):
        return [e for errors in exc.error_dict.values() for e in errors]
    return exc.error_list


def _stale(form, exc, revision):
    """After a stale-revision error, the next submit carries the current revision."""
    if "expected_revision" in form.fields and any(e.code == "stale_revision" for e in _errors(exc)):
        form.data = form.data.copy()
        form.data["expected_revision"] = revision()


def blocker_rows(exc):
    """[(accession, [label, ...])] from a `blocked` ValidationError, for per-specimen display."""
    rows = []
    for error in _errors(exc):
        if error.code == "blocked" and error.params:
            for accession, codes in sorted(error.params.get("blockers", {}).items()):
                rows.append((accession, [queries.CODES.get(c, c) for c in codes]))
    return rows


def _status(request, form, blocked, formset=None):
    if request.method != "POST":
        return 200
    if blocked:
        return 409
    if form.errors or (formset is not None and formset.total_error_count()):
        return 422
    return 200


def _action(
    request,
    form,
    *,
    title,
    submit,
    back_url,
    crumbs,
    run=None,
    success="",
    done_url=None,
    revision=None,
    blocked="",
    notes=(),
    facts=(),
    destructive=False,
    extra=None,
):
    """GET shows the form; POST validates, calls `run(cleaned_data)` and redirects on success."""
    context_extra = dict(extra or {})
    if blocked:
        form.disable()
    elif request.method == "POST" and form.is_valid():
        try:
            result = run(form.cleaned_data)
        except ValidationError as exc:
            apply_errors(form, exc)
            context_extra["blockers"] = blocker_rows(exc)
            if revision is not None:
                _stale(form, exc, revision)
        else:
            message = success(result) if callable(success) else success
            messages.success(request, message)
            target = done_url(result) if callable(done_url) else done_url
            return redirect(target or back_url)
    return render(
        request,
        FORM_TEMPLATE,
        {
            "page_title": title,
            "form": form,
            "submit_label": submit,
            "back_url": back_url,
            "crumbs": crumbs,
            "blocked": blocked,
            "notes": notes,
            "facts": facts,
            "destructive": destructive,
            **context_extra,
        },
        status=_status(request, form, blocked),
    )


def history(obj, limit=50):
    """Audit events of one record, newest first (the template shows them newest last)."""
    return list(
        AuditEvent.objects.filter(object_type=obj._meta.label_lower, object_id=str(obj.pk))
        .select_related("actor")
        .order_by("-occurred_at", "-id")[:limit]
    )


# --- lists ----------------------------------------------------------------------------------


def chips(request, form):
    """Removable filter chips from the validated filters (defaults are not active filters)."""
    result = []
    for name, value in form.cleaned_data.items():
        if name in {"sort", "page", "per_page", "q"} or value in (None, "", False):
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


def grid_response(request, key, template, *, export_kind, extra=None):
    spec = request_lists.SPECS[key]
    try:
        grid = GridRequest.parse(request.GET, spec.columns, spec.default_sort)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    data = request.GET.copy()
    data["q"] = grid.query
    form, queryset = request_lists.filtered(spec, data, request.user)
    page_obj = Paginator(request_lists.ordered(spec, queryset, grid.sort), grid.per_page).get_page(
        grid.page_number
    )
    active = chips(request, form) if form.is_valid() else []
    cleaned = getattr(form, "cleaned_data", {})
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
            "more": [form[name] for name in spec.more if name in form.fields],
            "more_open": any(cleaned.get(name) for name in spec.more),
            "noun": spec.noun,
            **export_action(
                form,
                page_obj,
                grid.sort,
                route="distribution:export",
                kind=export_kind,
                limit=exports.MAX_ROWS,
            ),
            **flags(request.user),
            **(extra or {}),
        },
    )


@staff_role_required(*READ)
def request_list(request):
    return grid_response(
        request, "request", "distribution/requests/request_list.html", export_kind="requests"
    )


@staff_role_required(*READ)
def shipment_list(request):
    return grid_response(
        request, "shipment", "distribution/shipments/shipment_list.html", export_kind="shipments"
    )


@staff_role_required(*READ)
def return_list(request):
    return grid_response(
        request, "return", "distribution/shipments/return_list.html", export_kind="returns"
    )


# --- CSV and attachments --------------------------------------------------------------------


@staff_role_required(*READ)
@require_GET
def export(request, kind):
    if kind not in EXPORT_KINDS:
        raise Http404
    params = request.GET.copy()
    sort = params.pop("sort", [""])[-1]
    try:
        content = exports.export_list(actor=request.user, kind=kind, filters=params, sort=sort)
    except ValidationError as exc:
        prefix = request_lists.EXPORT_KINDS[kind]
        return export_error_response(request, exc, list_route=f"distribution:{prefix}-list")
    response = HttpResponse(content, content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="distribution-{kind}.csv"'
    response["Cache-Control"] = "no-store"
    return response


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


# --- request record -------------------------------------------------------------------------


def _request(pk):
    return get_object_or_404(
        SpecimenRequest.objects.select_related(
            "investigator", "institution", "agreement", "created_by"
        ),
        pk=pk,
    )


def _request_url(obj):
    return reverse("distribution:request-detail", args=[obj.pk])


def request_crumbs(obj=None, *rest):
    trail = [("Requests", reverse("distribution:request-list"))]
    if obj is not None:
        trail.append((obj.number, _request_url(obj)))
    return [*trail, *rest]


def _request_revision(obj):
    return lambda: SpecimenRequest.objects.values_list("revision", flat=True).get(pk=obj.pk)


def _request_facts(obj):
    return (
        ("Request", f"{obj.number} · {obj.title}"),
        ("Status", obj.get_status_display()),
        ("Investigator", f"{obj.investigator.name} ({obj.institution.name})"),
    )


def allocation_counts(obj):
    rows = obj.allocations.values("state").annotate(n=Count("pk"))
    counts = {state: 0 for state in Allocation.State.values}
    counts.update({row["state"]: row["n"] for row in rows})
    counts["unshipped_picked"] = obj.allocations.filter(
        state=Allocation.State.PICKED, shipment__isnull=True
    ).count()
    counts["preparing"] = obj.shipments.filter(
        state__in=(Shipment.State.DRAFT, Shipment.State.PACKED)
    ).count()
    return counts


def _items_with_counts(obj):
    return list(
        obj.items.select_related("specimen_type", "unit", "study")
        .annotate(
            allocated_count=Count("allocations", filter=Q(allocations__state="allocated")),
            picked_count=Count("allocations", filter=Q(allocations__state="picked")),
            shipped_count=Count("allocations", filter=Q(allocations__state="shipped")),
        )
        .order_by("line")
    )


def _action_item(label, url, reason=""):
    return {"label": label, "url": url, "reason": reason}


def request_actions(obj, user, allowed, counts, items):
    """Header actions: destructive, secondary (up to three, rest in More), primary."""
    status = obj.status
    S = SpecimenRequest.Status
    url = lambda name, *args: reverse(f"distribution:{name}", args=[obj.pk, *args])  # noqa: E731
    destructive = primary = None
    secondary = []
    if allowed["can_manage"] and status not in SpecimenRequest.FINAL:
        reason = ""
        if counts["picked"] or counts["shipped"]:
            reason = (
                "Specimens are picked or shipped. Put picked ones back first; a request with "
                "shipped material is closed, not cancelled."
            )
        destructive = _action_item("Cancel request", url("request-cancel"), reason)
    if status in SpecimenRequest.EDITABLE and allowed["can_operate"]:
        secondary.append(_action_item("Edit", url("request-edit")))
        secondary.append(_action_item("Edit lines", url("request-items")))
        primary = _action_item(
            "Submit for decision",
            url("request-submit"),
            "" if items else "Add at least one line first.",
        )
    elif status == S.SUBMITTED:
        if allowed["can_decide"]:
            refusal = services.decision_refusal(user, obj)
            primary = _action_item("Decide request", url("request-decide"), refusal or "")
    elif status == S.APPROVED:
        remaining = next((i for i in items if _remaining(i) > 0), None)
        if counts["allocated"] and allowed["can_operate"]:
            primary = _action_item("Pick specimens", url("pick"))
        elif counts["unshipped_picked"] and allowed["can_operate"]:
            primary = _action_item("New shipment", url("shipment-create"))
        elif remaining is not None and allowed["can_manage"]:
            primary = _action_item("Allocate", url("allocate", remaining.pk))
        elif allowed["can_manage"]:
            primary = _action_item("Close request", url("request-close"), _close_reason(counts))
        if counts["allocated"]:
            secondary.append(_action_item("Pick list", url("pick-list")))
        if counts["picked"] and allowed["can_operate"]:
            secondary.append(_action_item("Put back", url("put-back")))
        if (
            allowed["can_operate"]
            and counts["unshipped_picked"]
            and (primary or {}).get("label") != "New shipment"
        ):
            secondary.append(_action_item("New shipment", url("shipment-create")))
        if allowed["can_manage"]:
            if counts["allocated"]:
                secondary.append(_action_item("Release allocations", url("allocation-release")))
            if (primary or {}).get("label") != "Close request":
                secondary.append(
                    _action_item("Close request", url("request-close"), _close_reason(counts))
                )
            secondary.append(
                _action_item("Reopen request", url("request-reopen"), _reopen_reason(counts))
            )
    if allowed["can_manage"] and status not in SpecimenRequest.FINAL:
        secondary.append(_action_item("Link agreement", url("request-agreement")))
    if allowed["can_operate"]:
        secondary.append(_action_item("Add note", url("request-note")))
    shown = [a for a in secondary if not a["reason"]][:2]
    more = [a for a in secondary if a not in shown]
    return {"destructive": destructive, "secondary": shown, "more": more, "primary": primary}


def _remaining(item):
    taken = item.allocated_count + item.picked_count + item.shipped_count
    return max(item.count - taken, 0)


def _close_reason(counts):
    if counts["allocated"] or counts["picked"]:
        return "Specimens are still allocated or picked. Release or ship them first."
    if counts["preparing"]:
        return "A shipment is still in preparation. Dispatch or cancel it first."
    return ""


def _reopen_reason(counts):
    if counts["picked"]:
        return "Specimens are picked for this request. Put them back first."
    return ""


def _locations(user, specimens):
    occupancy_ids = [s.occupancy_id for s in specimens if s.occupancy_id]
    if not occupancy_ids:
        return {}
    return storage_queries.locations(actor=user, occupancy_ids=occupancy_ids)


def request_timeline(obj):
    """RequestEvent, submissions, decisions and AllocationEvent rows, oldest first."""
    entries = []
    for event in obj.events.select_related("actor"):
        detail = ""
        if event.kind == "agreement_linked" and "agreement" in (event.changes or {}):
            before, after = event.changes["agreement"]
            detail = f"Agreement: {before or 'none'} → {after or 'none'}"
        entries.append(
            {
                "title": event.get_kind_display(),
                "at": event.recorded_at,
                "actor": event.actor,
                "reason": event.reason,
                "detail": detail,
            }
        )
    for submission in obj.submissions.select_related("submitted_by"):
        entries.append(
            {
                "title": f"Submitted for decision (submission {submission.number})",
                "at": submission.submitted_at,
                "actor": submission.submitted_by,
                "reason": "",
                "detail": "",
            }
        )
    for decision in RequestDecision.objects.filter(submission__request=obj).select_related(
        "actor", "submission"
    ):
        entries.append(
            {
                "title": f"Decision: {decision.get_decision_display()} "
                f"(submission {decision.submission.number})",
                "at": decision.recorded_at,
                "actor": decision.actor,
                "reason": decision.reason,
                "detail": decision.committee_reference,
            }
        )
    for event in AllocationEvent.objects.filter(allocation__request=obj).select_related(
        "actor", "allocation__specimen"
    ):
        entries.append(
            {
                "title": f"{event.allocation.specimen.accession}: {event.get_kind_display()}",
                "at": event.occurred_at,
                "actor": event.actor,
                "reason": event.reason,
                "detail": "",
            }
        )
    entries.sort(key=lambda e: e["at"])
    return entries


@staff_role_required(*READ)
def request_detail(request, pk):
    obj = _request(pk)
    user = request.user
    allowed = flags(user)
    items = _items_with_counts(obj)
    for item in items:
        item.remaining = _remaining(item)
    counts = allocation_counts(obj)
    allocations = list(
        obj.allocations.select_related(
            "specimen",
            "specimen__specimen_type",
            "specimen__unit",
            "item",
            "shipment",
        ).order_by("item__line", "specimen__accession")
    )
    specimens = [a.specimen for a in allocations]
    paths = _locations(user, specimens)
    waiting = [a.specimen_id for a in allocations if a.state == Allocation.State.ALLOCATED]
    found = queries.evaluate(obj, waiting, stage="pick") if waiting else {}
    for allocation in allocations:
        allocation.path = paths.get(allocation.specimen.occupancy_id)
        allocation.blockers = found.get(allocation.specimen_id, ())
    submissions = list(obj.submissions.select_related("submitted_by").order_by("number"))
    decisions = {
        d.submission_id: d
        for d in RequestDecision.objects.filter(submission__request=obj).select_related("actor")
    }
    for submission in submissions:
        submission.decision_row = decisions.get(submission.pk)
    shipments = list(
        obj.shipments.annotate(
            planned_count=Count("allocations", distinct=True),
            item_count=Count("items", distinct=True),
        ).order_by("-id")
    )
    refusal = ""
    if obj.status == SpecimenRequest.Status.SUBMITTED and allowed["can_decide"]:
        refusal = services.decision_refusal(user, obj) or ""
    today = timezone.localdate()
    toc = [
        ("progress-heading", "Progress"),
        ("facts-heading", "Request"),
        ("lines-heading", f"Lines ({len(items)})"),
        ("allocations-heading", f"Allocations ({len(allocations)})"),
        ("submissions-heading", f"Submissions and decisions ({len(submissions)})"),
        ("shipments-heading", f"Shipments ({len(shipments)})"),
        ("timeline-heading", "Timeline"),
        ("history-heading", "History"),
    ]
    return render(
        request,
        "distribution/requests/request_detail.html",
        {
            "page_title": obj.number,
            "request_obj": obj,
            "items": items,
            "counts": counts,
            "allocations": allocations,
            "submissions": submissions,
            "shipments": shipments,
            "refusal": refusal,
            "today": today,
            "use_categories": list(obj.use_categories.order_by("code")),
            "studies": list(obj.studies.order_by("code")),
            "actions": request_actions(obj, user, allowed, counts, items),
            "timeline": request_timeline(obj),
            "toc": toc,
            "history": history(obj),
            **allowed,
        },
    )


# --- request forms --------------------------------------------------------------------------


@staff_role_required(*OPERATE)
@ACTION_METHODS
def request_edit(request, pk=None):
    obj = _request(pk) if pk else None
    form = RequestForm(_post(request), instance=obj)

    def run(data):
        if obj is None:
            return services.save_request(
                actor=request.user,
                request_id=None,
                expected_revision=None,
                values=form.values(),
                items=(),
            )
        return services.save_request(
            actor=request.user,
            request_id=obj.pk,
            expected_revision=data["expected_revision"],
            values=form.values(),
            items=_current_items(obj),
            reason=data["reason"],
        )

    title = f"Edit {obj.number}" if obj else "New request"
    blocked = ""
    if obj is not None and not obj.is_editable:
        blocked = (
            "Only a draft or returned request can be edited. A manager reopens an approved one."
        )
    return _action(
        request,
        form,
        title=title,
        submit="Save changes" if obj else "Create request",
        back_url=_request_url(obj) if obj else reverse("distribution:request-list"),
        crumbs=request_crumbs(obj, (title, None)),
        run=run,
        success=lambda r: f"Saved {r.number}." if obj else f"Created {r.number}. Add its lines.",
        done_url=lambda r: (
            _request_url(r) if obj else reverse("distribution:request-items", args=[r.pk])
        ),
        revision=_request_revision(obj) if obj else None,
        blocked=blocked,
        facts=_request_facts(obj) if obj else (),
        notes=(
            "Staff record requests as they were received; investigators do not sign in. The "
            "institution is the investigator's.",
        ),
    )


def _current_items(obj):
    return tuple(
        services.ItemSpec(
            line=i.line,
            specimen_type_id=i.specimen_type_id,
            count=i.count,
            minimum_quantity=i.minimum_quantity,
            unit_id=i.unit_id,
            study_id=i.study_id,
            notes=i.notes,
        )
        for i in obj.items.order_by("line")
    )


def _quantity_text(value):
    return format(value.normalize(), "f") if value is not None else ""


def _item_initial(item):
    return {
        "line": item.line,
        "specimen_type": item.specimen_type_id,
        "count": item.count,
        "minimum_quantity": _quantity_text(item.minimum_quantity),
        "unit": item.unit_id,
        "study": item.study_id,
        "notes": item.notes,
    }


def _posted_items(data, prefix="items"):
    try:
        total = int(data.get(f"{prefix}-TOTAL_FORMS", 0))
    except ValueError:
        total = 0
    total = max(0, min(total, services.MAX_ITEMS))
    return [
        {name: data.get(f"{prefix}-{i}-{name}", "") for name in ITEM_FIELDS} for i in range(total)
    ]


def _next_line(rows):
    used = [int(r["line"]) for r in rows if str(r.get("line") or "").isdigit()]
    return max(used, default=0) + 1


@staff_role_required(*OPERATE)
@ACTION_METHODS
def request_items(request, pk):
    obj = _request(pk)
    referenced = set(
        Allocation.objects.filter(item__request=obj).values_list("item__line", flat=True)
    )
    existing = list(obj.items.order_by("line"))
    blocked = ""
    if not obj.is_editable:
        blocked = (
            "Lines change only while the request is a draft or returned for changes. A manager "
            "reopens an approved request."
        )
    post = _post(request)
    adding = post is not None and "add_row" in post
    removing = post.get("remove_row") if post is not None else None
    top_initial = {
        "expected_revision": obj.revision,
        "reason": "" if existing else "Lines of the request as received",
    }
    if post is not None and (adding or removing is not None) and not blocked:
        # Add or remove a line without JavaScript: re-render the grid, call nothing.
        rows = _posted_items(post)
        if adding and len(rows) < services.MAX_ITEMS:
            rows.append({"line": _next_line(rows), "count": 1})
        if removing is not None and removing.isdigit() and len(rows) > 1:
            index = int(removing)
            if index < len(rows) and _int(rows[index].get("line")) not in referenced:
                rows.pop(index)
        top = {k: v for k, v in post.items() if k in ("reason", "expected_revision")}
        form = ItemsForm(initial={**top_initial, **top})
        formset = ItemFormSet(prefix="items", initial=rows)
        post = None
    else:
        form = ItemsForm(post, initial=top_initial)
        rows = [_item_initial(i) for i in existing] or [{"line": 1, "count": 1}]
        formset = ItemFormSet(post, prefix="items", initial=None if post is not None else rows)
    if blocked:
        form.disable()
        for row in formset.forms:
            for field in row.fields.values():
                field.disabled = True
    elif post is not None and form.is_valid() & formset.is_valid():
        cleaned = [row for row in formset.cleaned_data if row]
        next_line = _next_line(cleaned)
        items = []
        for row in cleaned:
            line = row.get("line")
            if not line:
                line, next_line = next_line, next_line + 1
            items.append(
                services.ItemSpec(
                    line=line,
                    specimen_type_id=row["specimen_type"].pk,
                    count=row["count"],
                    minimum_quantity=row.get("minimum_quantity") or None,
                    unit_id=row["unit"].pk if row.get("unit") else None,
                    study_id=row["study"].pk if row.get("study") else None,
                    notes=row.get("notes") or "",
                )
            )
        try:
            services.save_request(
                actor=request.user,
                request_id=obj.pk,
                expected_revision=form.cleaned_data["expected_revision"],
                values={},
                items=tuple(items),
                reason=form.cleaned_data["reason"],
            )
        except ValidationError as exc:
            apply_errors(form, exc)
            _stale(form, exc, _request_revision(obj))
        else:
            messages.success(request, f"Saved the lines of {obj.number}.")
            return redirect(_request_url(obj))
    for row in formset.forms:
        row.locked = _int(row["line"].value()) in referenced
    title = "Edit lines"
    return render(
        request,
        "distribution/requests/items.html",
        {
            "page_title": title,
            "form": form,
            "formset": formset,
            "request_obj": obj,
            "max_rows": services.MAX_ITEMS,
            "blocked": blocked,
            "submit_label": "Save lines",
            "back_url": _request_url(obj),
            "crumbs": request_crumbs(obj, (title, None)),
            "facts": _request_facts(obj),
        },
        status=_status(request, form, blocked, formset),
    )


def _int(value):
    try:
        return int(value)
    except TypeError, ValueError:
        return None


@staff_role_required(*OPERATE)
@ACTION_METHODS
def request_submit(request, pk):
    obj = _request(pk)
    form = SubmitForm(_post(request), initial={"expected_revision": obj.revision})
    items = _items_with_counts(obj)
    blocked = ""
    if not obj.is_editable:
        blocked = "Only a draft or returned request can be submitted."
    elif not items:
        blocked = "Add at least one line before submitting."
    categories = ", ".join(c.code for c in obj.use_categories.order_by("code"))
    lines = "; ".join(
        f"line {i.line}: {i.count} × {i.specimen_type.code}"
        + (f" ({i.study.code})" if i.study_id else "")
        for i in items
    )
    return _action(
        request,
        form,
        title="Submit for decision",
        submit="Submit for decision",
        back_url=_request_url(obj),
        crumbs=request_crumbs(obj, ("Submit for decision", None)),
        run=lambda d: services.submit_request(
            actor=request.user, request_id=obj.pk, expected_revision=d["expected_revision"]
        ),
        success=lambda s: f"Submitted {obj.number} (submission {s.number}) for a decision.",
        revision=_request_revision(obj),
        blocked=blocked,
        facts=(
            *_request_facts(obj),
            ("Uses requested", categories or "None chosen"),
            ("Lines", lines or "None"),
        ),
        notes=(
            "The request as it is now is kept as a numbered submission. A person with request "
            "decision permission who did not create or submit it records the decision.",
        ),
    )


@staff_role_required(*OPERATE)
@ACTION_METHODS
def request_decide(request, pk):
    obj = _request(pk)
    user = request.user
    if not can_decide_requests(user):
        raise PermissionDenied("Recording request decisions needs the explicit permission.")
    form = DecideForm(
        _post(request),
        initial={"expected_revision": obj.revision, "submission_number": obj.latest_submission},
    )
    choice = request.GET.get("decision")
    if choice in dict(form.fields["decision"].choices):
        form.initial["decision"] = choice
    blocked = ""
    if obj.status != SpecimenRequest.Status.SUBMITTED:
        blocked = "This request is not awaiting a decision."
    else:
        blocked = services.decision_refusal(user, obj) or ""
    submission = (
        obj.submissions.filter(number=obj.latest_submission).select_related("submitted_by").first()
    )
    facts = list(_request_facts(obj))
    if submission is not None:
        facts.append(
            (
                "Submission",
                f"{submission.number}, {_when(submission.submitted_at)} by "
                f"{_who(submission.submitted_by)}",
            )
        )
    facts.append(
        ("Uses requested", ", ".join(c.code for c in obj.use_categories.order_by("code")) or "None")
    )
    return _action(
        request,
        form,
        title="Decide request",
        submit="Record decision",
        back_url=_request_url(obj),
        crumbs=request_crumbs(obj, ("Decide request", None)),
        run=lambda d: services.decide_request(
            actor=user,
            request_id=obj.pk,
            expected_revision=d["expected_revision"],
            submission_number=d["submission_number"],
            decision=d["decision"],
            conditions=d.get("conditions", ""),
            committee_reference=d.get("committee_reference", ""),
            conflicts_note=d.get("conflicts_note", ""),
            reason=d["reason"],
        ),
        success=lambda row: (
            f"Recorded the decision on {obj.number}: {row.get_decision_display().lower()}."
        ),
        revision=_request_revision(obj),
        blocked=blocked,
        facts=facts,
        notes=(
            "Record the decision the access committee or reviewer made. The software records it; "
            "it does not judge the request.",
        ),
    )


def _simple_request_action(request, pk, *, title, run, success, blocked_when, notes=(), **kw):
    obj = _request(pk)
    form = ReasonForm(_post(request), initial={"expected_revision": obj.revision})
    return _action(
        request,
        form,
        title=title,
        submit=title,
        back_url=_request_url(obj),
        crumbs=request_crumbs(obj, (title, None)),
        run=lambda d: run(obj, d),
        success=success(obj),
        revision=_request_revision(obj),
        blocked=blocked_when(obj),
        facts=_request_facts(obj),
        notes=notes,
        **kw,
    )


@staff_role_required(*MANAGE)
@ACTION_METHODS
def request_reopen(request, pk):
    def blocked(obj):
        if obj.status != SpecimenRequest.Status.APPROVED:
            return "Only an approved request can be reopened."
        return _reopen_reason(allocation_counts(obj))

    return _simple_request_action(
        request,
        pk,
        title="Reopen request",
        run=lambda obj, d: services.reopen_request(
            actor=request.user,
            request_id=obj.pk,
            expected_revision=d["expected_revision"],
            reason=d["reason"],
        ),
        success=lambda obj: f"Reopened {obj.number} as a draft.",
        blocked_when=blocked,
        notes=(
            "The request becomes a draft again and needs a new decision. Its allocations stay but "
            "cannot be picked, packed or dispatched until it is approved again.",
        ),
    )


@staff_role_required(*MANAGE)
@ACTION_METHODS
def request_cancel(request, pk):
    def blocked(obj):
        if obj.status in SpecimenRequest.FINAL:
            return "This request is already final."
        counts = allocation_counts(obj)
        if counts["picked"] or counts["shipped"]:
            return (
                "Specimens are picked or shipped for this request. Put picked specimens back "
                "first; a request with shipped material is closed, not cancelled."
            )
        return ""

    return _simple_request_action(
        request,
        pk,
        title="Cancel request",
        run=lambda obj, d: services.cancel_request(
            actor=request.user,
            request_id=obj.pk,
            expected_revision=d["expected_revision"],
            reason=d["reason"],
        ),
        success=lambda obj: f"Cancelled {obj.number}.",
        blocked_when=blocked,
        destructive=True,
        notes=(
            "Allocated specimens are released with their holds and draft shipments are "
            "cancelled. The request and its history stay.",
        ),
    )


@staff_role_required(*MANAGE)
@ACTION_METHODS
def request_close(request, pk):
    def blocked(obj):
        if obj.status != SpecimenRequest.Status.APPROVED:
            return "Only an approved request can be closed."
        return _close_reason(allocation_counts(obj))

    return _simple_request_action(
        request,
        pk,
        title="Close request",
        run=lambda obj, d: services.close_request(
            actor=request.user,
            request_id=obj.pk,
            expected_revision=d["expected_revision"],
            reason=d["reason"],
        ),
        success=lambda obj: f"Closed {obj.number}.",
        blocked_when=blocked,
        notes=("A closed request is final: nothing more is allocated or shipped for it.",),
    )


@staff_role_required(*MANAGE)
@ACTION_METHODS
def request_agreement(request, pk):
    obj = _request(pk)
    form = AgreementForm(_post(request), request_obj=obj)
    current = "None"
    if obj.agreement_id:
        current = f"{obj.agreement.number} ({obj.agreement.get_status_display().lower()})"
    return _action(
        request,
        form,
        title="Link agreement",
        submit="Link agreement",
        back_url=_request_url(obj),
        crumbs=request_crumbs(obj, ("Link agreement", None)),
        run=lambda d: services.link_agreement(
            actor=request.user,
            request_id=obj.pk,
            expected_revision=d["expected_revision"],
            agreement_id=d["agreement"].pk if d.get("agreement") else None,
            reason=d["reason"],
        ),
        success=lambda r: (
            f"Linked {r.agreement.number} to {r.number}."
            if r.agreement_id
            else f"Removed the agreement from {r.number}."
        ),
        revision=_request_revision(obj),
        blocked="This request is final." if obj.status in SpecimenRequest.FINAL else "",
        facts=(*_request_facts(obj), ("Current agreement", current)),
        notes=(
            "Dispatch needs an executed agreement with the request's institution that is valid "
            "on the dispatch date and covers the uses requested.",
        ),
    )


@staff_role_required(*OPERATE)
@ACTION_METHODS
def request_note(request, pk):
    obj = _request(pk)
    form = RequestNoteForm(_post(request))
    return _action(
        request,
        form,
        title="Add note",
        submit="Add note",
        back_url=_request_url(obj),
        crumbs=request_crumbs(obj, ("Add note", None)),
        run=lambda d: services.add_request_note(
            actor=request.user, request_id=obj.pk, note=d["note"]
        ),
        success=f"Added a note to {obj.number}.",
        done_url=_request_url(obj),
        facts=_request_facts(obj),
    )


# --- allocation -----------------------------------------------------------------------------


def _choice_value(specimen):
    return f"{specimen.pk}:{specimen.revision}"


def _parse_choices(values):
    choices = []
    for value in values:
        specimen_id, _, revision = str(value).partition(":")
        pk = _uuid(specimen_id)
        if pk is None or not revision.isdigit():
            raise ValidationError("A chosen specimen could not be read. Reload the page.")
        choices.append(services.Choice(pk, int(revision)))
    return tuple(choices)


def _candidate_filters(request):
    return {
        name: request.GET.get(name, "").strip()[:40]
        for name in ("q", "participant", "collection")
        if request.GET.get(name, "").strip()
    }


@staff_role_required(*MANAGE)
@ACTION_METHODS
def allocate(request, pk, item):
    obj = _request(pk)
    line = get_object_or_404(
        RequestItem.objects.select_related("specimen_type", "unit", "study"), pk=item, request=obj
    )
    taken = line.allocations.exclude(state=Allocation.State.RELEASED).count()
    remaining = max(line.count - taken, 0)
    blocked = ""
    if obj.status != SpecimenRequest.Status.APPROVED:
        blocked = "Specimens are allocated to approved requests only."
    elif remaining == 0:
        blocked = f"Line {line.line} has all {line.count} of its specimens allocated."
    filters = _candidate_filters(request)
    try:
        per_page = int(request.GET.get("per_page", 50))
    except ValueError:
        per_page = 50
    per_page = per_page if per_page in CANDIDATE_PAGE_SIZES else 50
    try:
        number = max(int(request.GET.get("page", 1)), 1)
    except ValueError:
        number = 1
    total = queries.candidate_queryset(obj, line, filters).count()
    page_obj = Paginator(range(total), per_page).get_page(number)
    rows, _ = queries.candidates(
        actor=request.user,
        request_id=obj.pk,
        item_id=line.pk,
        filters=filters,
        offset=(page_obj.number - 1) * per_page,
        limit=per_page,
    )
    gov = queries.governance([c.specimen for c in rows])
    form = AllocateForm(_post(request), initial={"expected_revision": obj.revision})
    chosen = set(request.POST.getlist("specimen")) if request.method == "POST" else set()
    row_blockers = {}
    if blocked:
        form.disable()
    elif request.method == "POST" and form.is_valid():
        try:
            choices = _parse_choices(chosen)
            if not choices:
                raise ValidationError({"choices": "Choose at least one specimen."}, code="required")
            created = services.allocate(
                actor=request.user,
                request_id=obj.pk,
                expected_revision=form.cleaned_data["expected_revision"],
                item_id=line.pk,
                choices=choices,
                below_minimum_ok=form.cleaned_data["below_minimum_ok"],
                occurred_at=form.cleaned_data["occurred_at"],
                reason=form.cleaned_data["reason"],
            )
        except ValidationError as exc:
            apply_errors(form, exc)
            _stale(form, exc, _request_revision(obj))
            row_blockers = dict(blocker_rows(exc))
        else:
            messages.success(
                request,
                f"Allocated {len(created)} specimen{'s' if len(created) != 1 else ''} to line "
                f"{line.line} of {obj.number}.",
            )
            return redirect(_request_url(obj))
    candidates = []
    for candidate in rows:
        specimen = candidate.specimen
        consent = gov[specimen.pk].consent
        below = line.minimum_quantity is not None and (
            specimen.unit_id != line.unit_id or specimen.quantity_on_hand < line.minimum_quantity
        )
        candidates.append(
            {
                "specimen": specimen,
                "value": _choice_value(specimen),
                "blockers": candidate.blockers,
                "path": candidate.path,
                "remaining": candidate.same_type_remaining,
                "restrictions": consent.current_record.restrictions
                if consent and consent.current_record
                else "",
                "study": gov[specimen.pk].study,
                "below_minimum": below,
                "chosen": _choice_value(specimen) in chosen,
                "errors": row_blockers.get(specimen.accession, ()),
            }
        )
    listed = {c["specimen"].accession for c in candidates}
    title = f"Allocate line {line.line}"
    return render(
        request,
        "distribution/requests/allocate.html",
        {
            "page_title": title,
            "form": form,
            "request_obj": obj,
            "line": line,
            "taken": taken,
            "remaining": remaining,
            "candidates": candidates,
            "unlisted_blockers": [
                (accession, labels)
                for accession, labels in row_blockers.items()
                if accession not in listed
            ],
            "page_obj": page_obj,
            "filters": filters,
            "blocked": blocked,
            "submit_label": "Allocate",
            "back_url": _request_url(obj),
            "crumbs": request_crumbs(obj, (title, None)),
            "facts": (
                *_request_facts(obj),
                (
                    "Line",
                    f"{line.line}: {line.count} × {line.specimen_type}"
                    + (f", study {line.study.code}" if line.study_id else "")
                    + (
                        f", at least {_quantity_text(line.minimum_quantity)} {line.unit.code} each"
                        if line.minimum_quantity is not None
                        else ""
                    ),
                ),
                ("Allocated so far", f"{taken} of {line.count}"),
            ),
        },
        status=_status(request, form, blocked),
    )


@staff_role_required(*MANAGE)
@ACTION_METHODS
def allocation_release(request, pk):
    obj = _request(pk)
    allocated = list(
        obj.allocations.filter(state=Allocation.State.ALLOCATED)
        .select_related("specimen", "item")
        .order_by("item__line", "specimen__accession")
    )
    choices = [(str(a.pk), f"{a.specimen.accession} (line {a.item.line})") for a in allocated]
    form = ReleaseForm(_post(request), choices=choices, initial={"expected_revision": obj.revision})
    selected = request.GET.getlist("allocation")
    if selected:
        form.initial["allocations"] = selected
    blocked = "" if allocated else "No specimens are allocated and waiting to be picked."
    return _action(
        request,
        form,
        title="Release allocations",
        submit="Release allocations",
        back_url=_request_url(obj),
        crumbs=request_crumbs(obj, ("Release allocations", None)),
        run=lambda d: services.release_allocations(
            actor=request.user,
            request_id=obj.pk,
            expected_revision=d["expected_revision"],
            allocation_ids=tuple(d["allocations"]),
            occurred_at=d["occurred_at"],
            reason=d["reason"],
        ),
        success=lambda rows: (
            f"Released {len(rows)} specimen{'s' if len(rows) != 1 else ''} from {obj.number}."
        ),
        done_url=_request_url(obj),
        revision=_request_revision(obj),
        blocked=blocked,
        facts=_request_facts(obj),
        notes=(
            "Picked specimens are put back first (Put back, with Release ticked). Released "
            "specimens keep their place in storage.",
        ),
    )


# --- picking and putting back ---------------------------------------------------------------


def _occupancy_revisions(specimens):
    return dict(
        Occupancy.objects.filter(pk__in=[s.occupancy_id for s in specimens]).values_list(
            "pk", "revision"
        )
    )


@staff_role_required(*OPERATE)
@ACTION_METHODS
def pick(request, pk):
    obj = _request(pk)
    rows = queries.pick_list(actor=request.user, request_id=obj.pk)
    by_allocation = {str(r.allocation.pk): r for r in rows}
    blocked = ""
    if obj.status != SpecimenRequest.Status.APPROVED:
        blocked = "Specimens are picked for approved requests only."
    elif not rows:
        blocked = "No specimens are allocated and waiting to be picked."
    revisions = _occupancy_revisions([r.specimen for r in rows])
    initial = [
        {
            "allocation": r.allocation.pk,
            "revision": r.specimen.revision,
            "occupancy_revision": revisions.get(r.specimen.occupancy_id, 0),
            "outcome": "leave" if r.blockers else "picked",
            "note": "",
        }
        for r in rows
    ]
    post = _post(request)
    form = PickForm(post, actor=request.user, initial={"expected_revision": obj.revision})
    formset = PickFormSet(post, prefix="lines", initial=None if post is not None else initial)
    row_blockers = {}
    if blocked:
        form.disable()
        for row in formset.forms:
            for field in row.fields.values():
                field.disabled = True
    elif post is not None and form.is_valid() & formset.is_valid():
        lines = tuple(
            services.PickLine(
                allocation_id=row["allocation"],
                expected_revision=row["revision"],
                expected_occupancy_revision=row["occupancy_revision"],
                missing=row["outcome"] == "missing",
                note=row.get("note") or "",
            )
            for row in formset.cleaned_data
            if row and row["outcome"] in ("picked", "missing")
        )
        try:
            if not lines:
                raise ValidationError("Mark at least one specimen picked or not found.")
            services.pick(
                actor=request.user,
                request_id=obj.pk,
                expected_revision=form.cleaned_data["expected_revision"],
                lines=lines,
                recipient_id=form.cleaned_data["recipient"].pk,
                occurred_at=form.cleaned_data["occurred_at"],
                reason=form.cleaned_data["reason"],
            )
        except ValidationError as exc:
            apply_errors(form, exc)
            _stale(form, exc, _request_revision(obj))
            row_blockers = dict(blocker_rows(exc))
        else:
            picked = sum(1 for line in lines if not line.missing)
            missing = len(lines) - picked
            text = f"Picked {picked} specimen{'s' if picked != 1 else ''} for {obj.number}."
            if missing:
                text += f" Recorded {missing} not found."
            messages.success(request, text)
            return redirect(_request_url(obj))
    for row in formset.forms:
        info = by_allocation.get(str(row["allocation"].value()))
        row.info = info
        row.errors_for_row = (
            row_blockers.get(info.specimen.accession, ()) if info is not None else ()
        )
    title = "Pick specimens"
    return render(
        request,
        "distribution/requests/pick.html",
        {
            "page_title": title,
            "form": form,
            "formset": formset,
            "request_obj": obj,
            "blocked": blocked,
            "submit_label": "Record pick",
            "back_url": _request_url(obj),
            "crumbs": request_crumbs(obj, (title, None)),
            "facts": _request_facts(obj),
        },
        status=_status(request, form, blocked, formset),
    )


@staff_role_required(*OPERATE)
@ACTION_METHODS
def put_back(request, pk):
    obj = _request(pk)
    picked = list(
        obj.allocations.filter(state=Allocation.State.PICKED)
        .select_related(
            "specimen",
            "specimen__specimen_type",
            "specimen__unit",
            "specimen__open_checkout__to_custodian",
            "shipment",
            "item",
        )
        .order_by("specimen__accession")
    )
    packed = [a for a in picked if a.shipment_id and a.shipment.state == Shipment.State.PACKED]
    available = [a for a in picked if a not in packed]
    by_allocation = {str(a.pk): a for a in available}
    in_storage = services.out_of_pick_custody(available)
    for a in available:
        a.in_storage = a.pk in in_storage
    blocked = "" if available else "No picked specimens can be put back now."
    revisions = _occupancy_revisions([a.specimen for a in available])
    initial = [
        {
            "include": False,
            "allocation": a.pk,
            "revision": a.specimen.revision,
            "occupancy_revision": revisions.get(a.specimen.occupancy_id, 0),
            "in_storage": a.in_storage,
            "thaw_observed": "unknown",
        }
        for a in available
    ]
    choices = box_choices()
    post = _post(request)
    form = PutBackForm(post, actor=request.user, initial={"expected_revision": obj.revision})
    formset = PutBackFormSet(
        post,
        prefix="lines",
        initial=None if post is not None else initial,
        form_kwargs={"actor": request.user, "choices": choices},
    )
    if blocked:
        form.disable()
        for row in formset:
            for field in row.fields.values():
                field.disabled = True
    elif post is not None and form.is_valid() & formset.is_valid():
        lines = tuple(
            services.PutBackLine(
                allocation_id=row["allocation"],
                expected_revision=row["revision"],
                expected_occupancy_revision=row["occupancy_revision"],
                position_id=row["position"].pk if row.get("position") else None,
                thaw_observed=row.get("thaw_value"),
            )
            for row in formset.cleaned_data
            if row and row.get("include")
        )
        try:
            if not lines:
                raise ValidationError("Tick at least one specimen to put back.")
            services.put_back(
                actor=request.user,
                request_id=obj.pk,
                expected_revision=form.cleaned_data["expected_revision"],
                lines=lines,
                recipient_id=form.cleaned_data["recipient"].pk,
                release=bool(form.cleaned_data.get("release")),
                occurred_at=form.cleaned_data["occurred_at"],
                reason=form.cleaned_data["reason"],
            )
        except ValidationError as exc:
            apply_errors(form, exc)
            _stale(form, exc, _request_revision(obj))
        else:
            count = len(lines)
            messages.success(
                request,
                f"Put back {count} specimen{'s' if count != 1 else ''} of {obj.number}.",
            )
            return redirect(_request_url(obj))
    for row in formset.forms:
        row.info = by_allocation.get(str(row["allocation"].value()))
    title = "Put back"
    return render(
        request,
        "distribution/requests/put_back.html",
        {
            "page_title": title,
            "form": form,
            "formset": formset,
            "request_obj": obj,
            "packed": packed,
            "suggestions": suggestion_text(request.user),
            "blocked": blocked,
            "submit_label": "Record put back",
            "back_url": _request_url(obj),
            "crumbs": request_crumbs(obj, (title, None)),
            "facts": _request_facts(obj),
        },
        status=_status(request, form, blocked, formset),
    )


# --- pick list (Document) -------------------------------------------------------------------


@staff_role_required(*READ)
@require_GET
def pick_list(request, pk):
    obj = _request(pk)
    context = documents.pick_list_context(actor=request.user, request_id=obj.pk)
    return render(
        request,
        "distribution/requests/pick_list.html",
        {
            **context,
            "page_title": "Pick list",
            "crumbs": request_crumbs(obj, ("Pick list", None)),
            **flags(request.user),
        },
    )


@staff_role_required(*READ)
@require_GET
def pick_list_pdf(request, pk):
    obj = _request(pk)
    back = reverse("distribution:pick-list", args=[obj.pk])
    try:
        pdf = documents.render_pick_list(actor=request.user, request_id=obj.pk)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect(back)
    except OSError:
        messages.error(request, "The pick list PDF cannot be produced on this server right now.")
        return redirect(back)
    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="pick-list-{obj.number}.pdf"'
    response["Cache-Control"] = "no-store"
    return response


# --- shipment record ------------------------------------------------------------------------


def _shipment(pk):
    return get_object_or_404(
        Shipment.objects.select_related(
            "request",
            "request__investigator",
            "request__institution",
            "request__agreement",
            "packed_by",
            "checked_by",
            "dispatched_by",
            "created_by",
        ),
        pk=pk,
    )


def _shipment_url(shipment):
    return reverse("distribution:shipment-detail", args=[shipment.pk])


def shipment_crumbs(shipment=None, *rest):
    trail = [("Shipments", reverse("distribution:shipment-list"))]
    if shipment is not None:
        trail.append((shipment.number, _shipment_url(shipment)))
    return [*trail, *rest]


def _shipment_revision(shipment):
    return lambda: Shipment.objects.values_list("revision", flat=True).get(pk=shipment.pk)


def _shipment_facts(shipment):
    return (
        ("Shipment", shipment.number),
        ("State", shipment.get_state_display()),
        ("Request", f"{shipment.request.number} · {shipment.request.title}"),
        ("Ship to", f"{shipment.ship_to_name}, {shipment.ship_to_institution}"),
    )


def shipment_actions(shipment, allowed):
    S = Shipment.State
    url = lambda name: reverse(f"distribution:{name}", args=[shipment.pk])  # noqa: E731
    destructive = primary = None
    secondary = []
    state = shipment.state
    if state in (S.DRAFT, S.PACKED) and allowed["can_manage"]:
        destructive = _action_item("Cancel shipment", url("shipment-cancel"))
    if allowed["can_operate"]:
        if state == S.DRAFT:
            secondary.append(_action_item("Edit", url("shipment-edit")))
            empty = not shipment.allocations.exists()
            primary = _action_item(
                "Pack", url("shipment-pack"), "Add picked specimens first." if empty else ""
            )
        elif state == S.PACKED:
            secondary.append(_action_item("Unpack", url("shipment-unpack")))
            secondary.append(_action_item("Record notification", url("shipment-notify")))
            secondary.append(_action_item("Edit", url("shipment-edit")))
            primary = _action_item("Dispatch", url("shipment-dispatch"))
        elif state == S.DISPATCHED:
            secondary.append(_action_item("Record notification", url("shipment-notify")))
            primary = _action_item("Record receipt", url("shipment-receipt"))
        secondary.append(_action_item("Add note", url("shipment-note")))
    if state == S.RECEIVED and allowed["can_manage"]:
        receipt = services.effective_receipt(shipment)
        if receipt is not None:
            primary = _action_item(
                "Correct receipt", reverse("distribution:receipt-correct", args=[receipt.pk])
            )
    shown = secondary[:2]
    return {
        "destructive": destructive,
        "secondary": shown,
        "more": secondary[2:],
        "primary": primary,
    }


def shipment_timeline(shipment):
    events = list(
        shipment.events.select_related("actor", "attachment")
        .defer("attachment__data")
        .order_by("occurred_at", "recorded_at", "id")
    )
    for event in events:
        changes = event.changes or {}
        parts = []
        for key, label in (("added", "Added"), ("removed", "Removed")):
            if changes.get(key):
                parts.append(f"{label}: {', '.join(changes[key])}")
        event.detail = "; ".join(parts)
    return events


def receipt_chain(shipment):
    """Every recorded receipt, newest first, with the effective one marked."""
    receipts = list(
        shipment.receipts.select_related("actor", "attachment", "corrects")
        .defer("attachment__data")
        .prefetch_related("exceptions__item")
        .order_by("-recorded_at", "-id")
    )
    corrected = {r.corrects_id for r in receipts if r.corrects_id}
    for receipt in receipts:
        receipt.is_effective = receipt.pk not in corrected
    return receipts


@staff_role_required(*READ)
def shipment_detail(request, pk):
    shipment = _shipment(pk)
    user = request.user
    allowed = flags(user)
    request_obj = shipment.request
    planned = []
    if not shipment.is_sent:
        planned = list(
            shipment.allocations.select_related(
                "specimen", "specimen__specimen_type", "specimen__unit", "item"
            ).order_by("specimen__accession")
        )
    items = list(shipment.items.order_by("line")) if shipment.is_sent else []
    returns = {
        r.item_id: r
        for r in request_lists.with_review_state(
            shipment.returns.select_related("actor", "unit", "attachment").defer("attachment__data")
        )
    }
    # Items keep the study code as sent; the current title is looked up for the screen only.
    titles = dict(
        Study.objects.filter(code__in={i.study_code for i in items}).values_list("code", "title")
    )
    for item in items:
        item.study_title = titles.get(item.study_code, "")
        item.return_row = returns.get(item.pk)
        if item.return_row is not None:
            item.return_row.review_state = request_lists.review_state(item.return_row)
    receipts = receipt_chain(shipment) if shipment.is_sent else []
    effective = next((r for r in receipts if r.is_effective), None)
    blockers = []
    if shipment.state == Shipment.State.PACKED:
        ids = [a.specimen_id for a in planned]
        found = queries.evaluate(request_obj, ids, stage="dispatch") if ids else {}
        accessions = {a.specimen_id: a.specimen.accession for a in planned}
        blockers = [
            (accessions[k], v)
            for k, v in sorted(found.items(), key=lambda kv: accessions[kv[0]])
            if v
        ]
        code = queries.participant_code_blocker(shipment)
        if code is not None:
            blockers.append(("All specimens", (code,)))
    toc = [
        ("progress-heading", "Progress"),
        ("facts-heading", "Shipment"),
        ("contents-heading", "Contents"),
    ]
    if shipment.is_sent:
        toc += [("receipt-heading", "Receipt"), ("returns-heading", "Returns")]
    toc += [("timeline-heading", "Timeline"), ("history-heading", "History")]
    return render(
        request,
        "distribution/shipments/shipment_detail.html",
        {
            "page_title": shipment.number,
            "shipment": shipment,
            "request_obj": request_obj,
            "planned": planned,
            "items": items,
            "returned_count": len(returns),
            "receipts": receipts,
            "effective": effective,
            "dispatch_blockers": blockers,
            "actions": shipment_actions(shipment, allowed),
            "events": shipment_timeline(shipment),
            "toc": toc,
            "history": history(shipment),
            **allowed,
        },
    )


# --- shipment forms -------------------------------------------------------------------------


@staff_role_required(*OPERATE)
@ACTION_METHODS
def shipment_create(request, pk):
    obj = _request(pk)
    form = ShipmentForm(
        _post(request), request_obj=obj, initial={"expected_revision": obj.revision}
    )
    title = "New shipment"
    blocked = ""
    if obj.status != SpecimenRequest.Status.APPROVED:
        blocked = "Shipments are prepared for approved requests only."
    return _action(
        request,
        form,
        title=title,
        submit="Create shipment",
        back_url=_request_url(obj),
        crumbs=[
            ("Shipments", reverse("distribution:shipment-list")),
            (obj.number, _request_url(obj)),
            (title, None),
        ],
        run=lambda d: services.create_shipment(
            actor=request.user,
            request_id=obj.pk,
            expected_revision=d["expected_revision"],
            values=form.values(),
        ),
        success=lambda s: f"Created {s.number}. Choose its contents.",
        done_url=lambda s: reverse("distribution:shipment-contents", args=[s.pk]),
        revision=_request_revision(obj),
        blocked=blocked,
        facts=_request_facts(obj),
        notes=(
            "Ship-to details are copied from the investigator and institution; change them here "
            "if this shipment goes elsewhere.",
        ),
    )


def _shipment_blocked(shipment, states, message):
    return "" if shipment.state in states else message


@staff_role_required(*OPERATE)
@ACTION_METHODS
def shipment_edit(request, pk):
    shipment = _shipment(pk)
    form = ShipmentForm(_post(request), instance=shipment)
    title = f"Edit {shipment.number}"
    notes = ()
    if shipment.state == Shipment.State.PACKED:
        notes = (
            "The shipment is packed: only the planned date and courier details can change. "
            "Unpack it to change anything else.",
        )
    return _action(
        request,
        form,
        title=title,
        submit="Save changes",
        back_url=_shipment_url(shipment),
        crumbs=shipment_crumbs(shipment, (title, None)),
        run=lambda d: services.edit_shipment(
            actor=request.user,
            shipment_id=shipment.pk,
            expected_revision=d["expected_revision"],
            values=form.values(),
            reason=d["reason"],
        ),
        success=f"Saved {shipment.number}.",
        revision=_shipment_revision(shipment),
        blocked=_shipment_blocked(
            shipment,
            (Shipment.State.DRAFT, Shipment.State.PACKED),
            "A dispatched or cancelled shipment cannot be edited.",
        ),
        facts=_shipment_facts(shipment),
        notes=notes,
    )


@staff_role_required(*OPERATE)
@ACTION_METHODS
def shipment_contents(request, pk):
    shipment = _shipment(pk)
    candidates = list(
        Allocation.objects.filter(request=shipment.request_id, state=Allocation.State.PICKED)
        .filter(Q(shipment__isnull=True) | Q(shipment=shipment))
        .select_related(
            "specimen",
            "specimen__specimen_type",
            "specimen__unit",
            "specimen__open_checkout__to_custodian",
            "item",
        )
        .order_by("specimen__accession")
    )
    choices = [(str(a.pk), a.specimen.accession) for a in candidates]
    form = ContentsForm(
        _post(request),
        choices=choices,
        initial={
            "expected_revision": shipment.revision,
            "allocations": [str(a.pk) for a in candidates if a.shipment_id == shipment.pk],
        },
    )
    blocked = _shipment_blocked(
        shipment, (Shipment.State.DRAFT,), "Only a draft shipment's contents can change."
    )
    if not blocked and not candidates:
        blocked = "No picked specimens of the request are free for this shipment. Pick first."
    if blocked:
        form.disable()
    elif request.method == "POST" and form.is_valid():
        try:
            services.set_shipment_contents(
                actor=request.user,
                shipment_id=shipment.pk,
                expected_revision=form.cleaned_data["expected_revision"],
                allocation_ids=tuple(form.cleaned_data["allocations"]),
                reason=form.cleaned_data.get("reason", ""),
            )
        except ValidationError as exc:
            apply_errors(form, exc)
            _stale(form, exc, _shipment_revision(shipment))
        else:
            messages.success(request, f"Saved the contents of {shipment.number}.")
            return redirect(_shipment_url(shipment))
    selected = set(form["allocations"].value() or ())
    rows = [{"allocation": a, "checked": str(a.pk) in selected} for a in candidates]
    title = "Change contents"
    return render(
        request,
        "distribution/shipments/contents.html",
        {
            "page_title": title,
            "form": form,
            "rows": rows,
            "shipment": shipment,
            "blocked": blocked,
            "submit_label": "Save contents",
            "back_url": _shipment_url(shipment),
            "crumbs": shipment_crumbs(shipment, (title, None)),
            "facts": _shipment_facts(shipment),
        },
        status=_status(request, form, blocked),
    )


@staff_role_required(*OPERATE)
@ACTION_METHODS
def shipment_pack(request, pk):
    shipment = _shipment(pk)
    form = PackForm(_post(request), actor=request.user, shipment=shipment)
    count = shipment.allocations.count()
    blocked = _shipment_blocked(
        shipment, (Shipment.State.DRAFT,), "Only a draft shipment can be packed."
    )
    if not blocked and not count:
        blocked = "Add picked specimens before packing."
    agreement = shipment.request.agreement
    level = agreement.get_data_level_display() if agreement else "no agreement linked"

    def run(d):
        declaration = services.PackDeclaration(
            temperature_class=d["temperature_class"],
            refrigerant=d["refrigerant"],
            dry_ice_kg=d.get("dry_ice_kg"),
            classification=d["classification"],
            classification_note=d.get("classification_note", ""),
            package_count=d["package_count"],
            emergency_contact=d["emergency_contact"],
            logger_ids=d.get("logger_ids", ""),
            storage_instructions=d.get("storage_instructions", ""),
            include_participant_codes=bool(d.get("include_participant_codes")),
            training_confirmed=d["training_confirmed"] == "yes",
        )
        checker = d.get("checked_by")
        return services.pack_shipment(
            actor=request.user,
            shipment_id=shipment.pk,
            expected_revision=d["expected_revision"],
            declaration=declaration,
            checked_by_id=checker.pk if checker else None,
            occurred_at=d["occurred_at"],
            reason=d["reason"],
        )

    return _action(
        request,
        form,
        title="Pack",
        submit="Pack",
        back_url=_shipment_url(shipment),
        crumbs=shipment_crumbs(shipment, ("Pack", None)),
        run=run,
        success=f"Packed {shipment.number}.",
        revision=_shipment_revision(shipment),
        blocked=blocked,
        facts=(
            *_shipment_facts(shipment),
            ("Specimens", str(count)),
            ("Agreement data level", level),
        ),
        notes=(
            "Packing checks every specimen again and freezes the contents: a specimen changed "
            "after packing stops the dispatch. The declarations are the shipper's; the software "
            "records them and does not classify the shipment.",
        ),
    )


@staff_role_required(*OPERATE)
@ACTION_METHODS
def shipment_unpack(request, pk):
    shipment = _shipment(pk)
    form = ReasonForm(_post(request), initial={"expected_revision": shipment.revision})
    return _action(
        request,
        form,
        title="Unpack",
        submit="Unpack",
        back_url=_shipment_url(shipment),
        crumbs=shipment_crumbs(shipment, ("Unpack", None)),
        run=lambda d: services.unpack_shipment(
            actor=request.user,
            shipment_id=shipment.pk,
            expected_revision=d["expected_revision"],
            reason=d["reason"],
        ),
        success=f"Unpacked {shipment.number}; it is a draft again.",
        revision=_shipment_revision(shipment),
        blocked=_shipment_blocked(
            shipment, (Shipment.State.PACKED,), "Only a packed shipment can be unpacked."
        ),
        facts=_shipment_facts(shipment),
        notes=("The shipment returns to draft; its specimens stay picked and in it.",),
    )


@staff_role_required(*OPERATE)
@ACTION_METHODS
def shipment_notify(request, pk):
    shipment = _shipment(pk)
    form = NotifyForm(_post(request), initial={"expected_revision": shipment.revision})
    return _action(
        request,
        form,
        title="Record notification",
        submit="Record notification",
        back_url=_shipment_url(shipment),
        crumbs=shipment_crumbs(shipment, ("Record notification", None)),
        run=lambda d: services.record_notification(
            actor=request.user,
            shipment_id=shipment.pk,
            expected_revision=d["expected_revision"],
            kind=d["kind"],
            occurred_at=d["occurred_at"],
            note=d.get("note", ""),
        ),
        success=lambda e: f"Recorded on {shipment.number}: {e.get_kind_display().lower()}.",
        done_url=_shipment_url(shipment),
        revision=_shipment_revision(shipment),
        blocked=""
        if shipment.state not in (Shipment.State.RECEIVED, Shipment.State.CANCELLED)
        else "This shipment is already final.",
        facts=_shipment_facts(shipment),
    )


@staff_role_required(*OPERATE)
@ACTION_METHODS
def shipment_dispatch(request, pk):
    shipment = _shipment(pk)
    form = DispatchForm(_post(request), shipment=shipment)
    blocked = _shipment_blocked(
        shipment, (Shipment.State.PACKED,), "Only a packed shipment can be dispatched."
    )
    precheck = []
    if not blocked:
        planned = list(shipment.allocations.select_related("specimen"))
        found = queries.evaluate(
            shipment.request, [a.specimen_id for a in planned], stage="dispatch"
        )
        accessions = {a.specimen_id: a.specimen.accession for a in planned}
        precheck = [
            (accessions[k], [b.message for b in v])
            for k, v in sorted(found.items(), key=lambda kv: accessions[kv[0]])
            if v
        ]
        code = queries.participant_code_blocker(shipment)
        if code is not None:
            precheck.append(("All specimens", [code.message]))
    notes = [
        "Dispatch checks every specimen again (consent, holds, the agreement on the dispatch "
        "date, no change since packing) before anything is written. Each specimen then leaves "
        "custody and the manifest is fixed from that moment.",
    ]
    return _action(
        request,
        form,
        title="Dispatch",
        submit="Dispatch",
        back_url=_shipment_url(shipment),
        crumbs=shipment_crumbs(shipment, ("Dispatch", None)),
        run=lambda d: services.dispatch_shipment(
            actor=request.user,
            shipment_id=shipment.pk,
            expected_revision=d["expected_revision"],
            courier=d.get("courier", ""),
            tracking=d.get("tracking", ""),
            handed_to=d.get("handed_to", ""),
            occurred_at=d["occurred_at"],
            reason=d["reason"],
        ),
        success=f"Dispatched {shipment.number}.",
        revision=_shipment_revision(shipment),
        blocked=blocked,
        facts=(
            *_shipment_facts(shipment),
            ("Packed", f"{_when(shipment.packed_at)} by {_who(shipment.packed_by)}"),
        ),
        notes=notes,
        extra={"precheck": precheck},
    )


def _receipt_values(form):
    d = form.cleaned_data
    return services.ReceiptValues(
        received_at=d["received_at"],
        received_by_name=d["received_by_name"],
        unpacked_by_name=d.get("unpacked_by_name", ""),
        condition=d["condition"],
        refrigerant_on_arrival=d["refrigerant_on_arrival"],
        temperature_note=d.get("temperature_note", ""),
        discrepancies=d.get("discrepancies", ""),
        received_via=d["received_via"],
        exceptions=form.exceptions(),
    )


@staff_role_required(*OPERATE)
@ACTION_METHODS
def shipment_receipt(request, pk):
    shipment = _shipment(pk)
    items = list(shipment.items.order_by("line"))
    form = ReceiptForm(_post(request), _files(request), shipment=shipment, items=items)
    return _action(
        request,
        form,
        title="Record receipt",
        submit="Record receipt",
        back_url=_shipment_url(shipment),
        crumbs=shipment_crumbs(shipment, ("Record receipt", None)),
        run=lambda d: services.record_receipt(
            actor=request.user,
            shipment_id=shipment.pk,
            expected_revision=d["expected_revision"],
            values=_receipt_values(form),
            upload=d.get("attachment"),
            reason=d["reason"],
        ),
        success=f"Recorded the receipt of {shipment.number}.",
        done_url=_shipment_url(shipment),
        revision=_shipment_revision(shipment),
        blocked=_shipment_blocked(
            shipment,
            (Shipment.State.DISPATCHED,),
            "Receipt is recorded once, for a dispatched shipment. A manager corrects a recorded "
            "receipt.",
        ),
        facts=(
            *_shipment_facts(shipment),
            ("Dispatched", _when(shipment.dispatched_at)),
        ),
        notes=(
            "Transcribe what the recipient reported. Nothing changes on the specimens: they are "
            "outside the repository.",
        ),
    )


@staff_role_required(*MANAGE)
@ACTION_METHODS
def receipt_correct(request, pk):
    receipt = get_object_or_404(ShipmentReceipt.objects.select_related("shipment", "actor"), pk=pk)
    shipment = _shipment(receipt.shipment_id)
    items = list(shipment.items.order_by("line"))
    form = ReceiptForm(
        _post(request), _files(request), shipment=shipment, items=items, original=receipt
    )
    effective = services.effective_receipt(shipment)
    blocked = ""
    if effective is None or effective.pk != receipt.pk:
        blocked = "This receipt was already corrected. Correct the latest one."
    return _action(
        request,
        form,
        title="Correct receipt",
        submit="Correct receipt",
        back_url=_shipment_url(shipment),
        crumbs=shipment_crumbs(shipment, ("Correct receipt", None)),
        run=lambda d: services.correct_receipt(
            actor=request.user,
            receipt_id=receipt.pk,
            expected_revision=d["expected_revision"],
            values=_receipt_values(form),
            upload=d.get("attachment"),
            reason=d["reason"],
        ),
        success=f"Corrected the receipt of {shipment.number}.",
        done_url=_shipment_url(shipment),
        revision=_shipment_revision(shipment),
        blocked=blocked,
        facts=(
            *_shipment_facts(shipment),
            ("Receipt", f"Recorded {_when(receipt.recorded_at)} by {_who(receipt.actor)}"),
        ),
        notes=("The correction is added; the earlier receipt stays in the record, marked.",),
    )


@staff_role_required(*MANAGE)
@ACTION_METHODS
def shipment_cancel(request, pk):
    shipment = _shipment(pk)
    form = ReasonForm(_post(request), initial={"expected_revision": shipment.revision})
    return _action(
        request,
        form,
        title="Cancel shipment",
        submit="Cancel shipment",
        back_url=_shipment_url(shipment),
        crumbs=shipment_crumbs(shipment, ("Cancel shipment", None)),
        run=lambda d: services.cancel_shipment(
            actor=request.user,
            shipment_id=shipment.pk,
            expected_revision=d["expected_revision"],
            reason=d["reason"],
        ),
        success=f"Cancelled {shipment.number}.",
        revision=_shipment_revision(shipment),
        blocked=_shipment_blocked(
            shipment,
            (Shipment.State.DRAFT, Shipment.State.PACKED),
            "Only a draft or packed shipment can be cancelled.",
        ),
        facts=_shipment_facts(shipment),
        destructive=True,
        notes=("Its specimens stay picked: put them back or add them to another shipment.",),
    )


@staff_role_required(*OPERATE)
@ACTION_METHODS
def shipment_note(request, pk):
    shipment = _shipment(pk)
    form = ShipmentNoteForm(_post(request), _files(request))
    return _action(
        request,
        form,
        title="Add note",
        submit="Add note",
        back_url=_shipment_url(shipment),
        crumbs=shipment_crumbs(shipment, ("Add note", None)),
        run=lambda d: services.add_shipment_note(
            actor=request.user,
            shipment_id=shipment.pk,
            note=d.get("note", ""),
            occurred_at=d["occurred_at"],
            upload=d.get("attachment"),
        ),
        success=f"Added a note to {shipment.number}.",
        done_url=_shipment_url(shipment),
        facts=_shipment_facts(shipment),
        notes=("Enter a note, attach a file, or both.",),
    )


# --- manifest (Document) --------------------------------------------------------------------


@staff_role_required(*READ)
@require_GET
def manifest(request, pk):
    shipment = _shipment(pk)
    context = documents.manifest_context(actor=request.user, shipment_id=shipment.pk)
    return render(
        request,
        "distribution/shipments/manifest.html",
        {
            **context,
            "page_title": "Manifest",
            "crumbs": shipment_crumbs(shipment, ("Manifest", None)),
        },
    )


@staff_role_required(*READ)
@require_GET
def manifest_pdf(request, pk):
    shipment = _shipment(pk)
    back = reverse("distribution:manifest", args=[shipment.pk])
    try:
        pdf = documents.render_manifest(actor=request.user, shipment_id=shipment.pk)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect(back)
    except OSError:
        messages.error(request, "The manifest PDF cannot be produced on this server right now.")
        return redirect(back)
    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="manifest-{shipment.number}.pdf"'
    response["Cache-Control"] = "no-store"
    return response


# --- returns --------------------------------------------------------------------------------


@staff_role_required(*OPERATE)
@ACTION_METHODS
def return_record(request, pk):
    item = get_object_or_404(ShipmentItem.objects.select_related("shipment"), pk=pk)
    shipment = _shipment(item.shipment_id)
    specimen = Specimen.objects.select_related("unit").get(pk=item.specimen_id)
    form = ReturnForm(
        _post(request),
        _files(request),
        actor=request.user,
        unit=specimen.unit.code,
        initial={"expected_revision": specimen.revision},
    )
    blocked = ""
    if not shipment.is_sent:
        blocked = "Only material of a dispatched shipment can be returned."
    elif SpecimenReturn.objects.filter(item=item).exists():
        blocked = f"{item.accession} was already returned from this shipment."
    return _action(
        request,
        form,
        title="Record return",
        submit="Record return",
        back_url=_shipment_url(shipment),
        crumbs=shipment_crumbs(shipment, ("Record return", None)),
        run=lambda d: services.record_return(
            actor=request.user,
            item_id=item.pk,
            expected_revision=d["expected_revision"],
            quantity=d["quantity"],
            supplied_text=d["quantity"],
            thaw_count=d.get("thaw_count"),
            condition=d.get("condition", ""),
            recipient_id=d["recipient"].pk,
            occurred_at=d["occurred_at"],
            reason=d["reason"],
            upload=d.get("attachment"),
        ),
        success=f"Recorded the return of {item.accession}. It is on hold until a manager "
        "reviews it.",
        done_url=_shipment_url(shipment),
        revision=lambda: Specimen.objects.values_list("revision", flat=True).get(pk=specimen.pk),
        blocked=blocked,
        facts=(
            ("Specimen", item.accession),
            ("Shipment", f"{shipment.number}, dispatched {_when(shipment.dispatched_at)}"),
            ("Sent", f"{_quantity_text(item.quantity)} {item.unit}"),
        ),
        notes=(
            "The specimen comes back into the custody of the person receiving it, with the "
            "measured amount and the thaws reported, and a use hold until a manager reviews "
            "it. Store it afterwards with the specimen's Check in.",
        ),
    )


@staff_role_required(*MANAGE)
@ACTION_METHODS
def return_review(request, pk):
    row = get_object_or_404(
        SpecimenReturn.objects.select_related("item", "shipment", "unit", "actor"), pk=pk
    )
    shipment = _shipment(row.shipment_id)
    specimen = Specimen.objects.get(pk=row.specimen_id)
    form = ReturnReviewForm(_post(request), initial={"expected_revision": specimen.revision})
    blocked = "This return was already released for use." if services.return_reviewed(row) else ""
    reviews = list(row.reviews.select_related("actor").order_by("recorded_at"))
    facts = [
        ("Specimen", row.item.accession),
        ("Shipment", shipment.number),
        ("Returned", f"{_when(row.occurred_at)}, recorded by {_who(row.actor)}"),
        ("Amount", f"{_quantity_text(row.quantity)} {row.unit.code}"),
        (
            "Thaws reported while away",
            str(row.thaw_count) if row.thaw_count is not None else "Not known",
        ),
        ("Condition", row.condition or "Not described"),
    ]
    for review in reviews:
        facts.append(
            (
                "Earlier review",
                f"{review.get_decision_display()}, {_when(review.recorded_at)} by "
                f"{_who(review.actor)}: {review.reason}",
            )
        )
    return _action(
        request,
        form,
        title="Review return",
        submit="Record review",
        back_url=reverse("distribution:return-list"),
        crumbs=[
            ("Shipments", reverse("distribution:shipment-list")),
            ("Returns", reverse("distribution:return-list")),
            ("Review return", None),
        ],
        run=lambda d: services.review_return(
            actor=request.user,
            return_id=row.pk,
            expected_revision=d["expected_revision"],
            decision=d["decision"],
            occurred_at=d["occurred_at"],
            reason=d["reason"],
        ),
        success=lambda r: (
            f"Recorded the review of {row.item.accession}: {r.get_decision_display().lower()}."
        ),
        revision=lambda: Specimen.objects.values_list("revision", flat=True).get(pk=specimen.pk),
        blocked=blocked,
        facts=facts,
        notes=(
            "Releasing removes the hold placed when it came back, so it can be used again. "
            "Keeping it on hold can be reviewed again later. Disposal is a separate request.",
        ),
    )
