from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods

from accounts.roles import has_role, staff_role_required
from core.grids import GridRequest, active_filters
from core.models import AuditEvent

from . import services
from .forms import AuditCreateForm, CancelForm, CompleteForm, FilterForm, apply_errors
from .models import InventoryAudit


@staff_role_required(*services.READ_ROLES)
@require_GET
def audit_list(request):
    columns = {"number": "pk", "box": "box_code", "created": "created_at"}
    try:
        grid = GridRequest.parse(request.GET, columns, "-created")
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    form = FilterForm(request.GET)
    rows = services.audits(actor=request.user)
    if form.is_valid():
        status = form.cleaned_data["status"]
        if status == "open":
            rows = rows.filter(finalization__isnull=True)
        elif status:
            rows = rows.filter(finalization__outcome=status)
    else:
        rows = rows.none()
    if grid.query:
        query = Q(box_code__icontains=grid.query) | Q(reason__icontains=grid.query)
        number = grid.query.upper().removeprefix("AUD-")
        if number.isdecimal() and len(number) <= 15:
            query |= Q(pk=int(number))
        rows = rows.filter(query)
    return render(
        request,
        "quality/audit_list.html",
        {
            "page_title": "Inventory audits",
            "page_obj": grid.page(rows, columns),
            "filter_form": form,
            "inline": [form["status"]],
            "q": grid.query,
            "sort": grid.sort,
            "active_filters": active_filters(request, form) if form.is_valid() else [],
            "filtered": bool(grid.query or request.GET.get("status")),
            "can_operate": has_role(request.user, *services.OPERATE_ROLES),
            "noun": "audits",
        },
    )


def _audit(request, pk):
    try:
        return services.get_audit(actor=request.user, audit_id=pk)
    except InventoryAudit.DoesNotExist:
        raise Http404 from None


def _crumbs(audit=None, current=None):
    result = [("Inventory audits", reverse("quality:audit-list"))]
    if audit:
        result.append((audit.number, reverse("quality:audit-detail", args=[audit.pk])))
    if current:
        result.append((current, ""))
    return result


@staff_role_required(*services.READ_ROLES)
@require_GET
def audit_detail(request, pk):
    audit = _audit(request, pk)
    final = getattr(audit, "finalization", None)
    rows = []
    observations = (
        {r["position_id"]: r for r in final.observations["positions"]}
        if final and final.outcome == "completed"
        else {}
    )
    for position in audit.positions:
        rows.append({**position, **observations.get(position["id"], {})})
    return render(
        request,
        "quality/audit_detail.html",
        {
            "page_title": audit.number,
            "audit": audit,
            "final": final,
            "rows": rows,
            "toc": [
                ("snapshot-heading", "Audit snapshot"),
                ("results-heading", "Final result"),
                ("positions-heading", "Positions"),
                ("history-heading", "History"),
            ]
            if final
            else [],
            "stale": audit.status == "open" and services.is_stale(actor=request.user, audit=audit),
            "can_operate": has_role(request.user, *services.OPERATE_ROLES),
            "history": AuditEvent.objects.filter(
                object_type="quality.inventoryaudit", object_id=str(pk)
            )
            .select_related("actor")
            .order_by("occurred_at", "pk"),
        },
    )


@staff_role_required(*services.OPERATE_ROLES)
@require_http_methods(["GET", "POST"])
def audit_create(request):
    form = AuditCreateForm(
        request.POST if request.method == "POST" else None,
        actor=request.user,
        initial={"box": request.GET.get("box"), "supersedes": request.GET.get("supersedes")},
    )
    if request.method == "POST" and form.is_valid():
        try:
            audit = services.create_audit(
                actor=request.user,
                box_id=form.cleaned_data["box"].pk,
                reason=form.cleaned_data["reason"],
                supersedes_id=form.cleaned_data["supersedes"].pk
                if form.cleaned_data["supersedes"]
                else None,
            )
        except ValidationError as exc:
            apply_errors(form, exc)
        else:
            messages.success(request, "Audit created. Check every position before completing it.")
            return redirect("quality:audit-detail", pk=audit.pk)
    return render(
        request,
        "quality/form.html",
        {
            "page_title": "New inventory audit",
            "form": form,
            "submit_label": "Create audit",
            "back_url": reverse("quality:audit-list"),
            "crumbs": _crumbs(current="New inventory audit"),
            "notes": [
                "Snapshots one active box with up to 400 positions. "
                "No storage or specimen records change."
            ],
        },
    )


def _disable_widgets(form):
    # Field.disabled substitutes initial values for submitted data. Keep a rejected POST
    # bound for review while the blocked branch and domain service prevent saving it.
    for field in form.fields.values():
        field.widget.attrs["disabled"] = True


@staff_role_required(*services.OPERATE_ROLES)
@require_http_methods(["GET", "POST"])
def audit_complete(request, pk):
    audit = _audit(request, pk)
    form = CompleteForm(request.POST if request.method == "POST" else None, audit=audit)
    blocked = (
        "This audit is already finalized. Start a new audit to add evidence."
        if audit.status != "open"
        else ""
    )
    if not blocked and services.is_stale(actor=request.user, audit=audit):
        blocked = "Storage changed since this audit began. Cancel this audit and start a new one."
    if blocked:
        _disable_widgets(form)
    elif request.method == "POST" and form.is_valid():
        try:
            services.complete_audit(
                actor=request.user,
                audit_id=pk,
                **{
                    key: form.cleaned_data[key]
                    for key in ("observations", "reason", "discrepancy_explanation")
                },
            )
        except ValidationError as exc:
            apply_errors(form, exc)
        else:
            messages.success(
                request,
                "Audit completed. Results are retained as evidence; inventory is unchanged.",
            )
            return redirect("quality:audit-detail", pk=pk)
    return render(
        request,
        "quality/complete.html",
        {
            "page_title": f"Complete {audit.number}",
            "form": form,
            "audit": audit,
            "blocked": blocked,
            "submit_label": "Complete audit",
            "back_url": reverse("quality:audit-detail", args=[pk]),
            "crumbs": _crumbs(audit, "Complete audit"),
        },
    )


@staff_role_required(*services.OPERATE_ROLES)
@require_http_methods(["GET", "POST"])
def audit_cancel(request, pk):
    audit = _audit(request, pk)
    form = CancelForm(request.POST if request.method == "POST" else None)
    blocked = "This audit is already finalized." if audit.status != "open" else ""
    if blocked:
        _disable_widgets(form)
    elif request.method == "POST" and form.is_valid():
        try:
            services.cancel_audit(
                actor=request.user, audit_id=pk, reason=form.cleaned_data["reason"]
            )
        except ValidationError as exc:
            apply_errors(form, exc)
        else:
            messages.success(request, "Audit cancelled. Its snapshot remains in history.")
            return redirect("quality:audit-detail", pk=pk)
    return render(
        request,
        "quality/form.html",
        {
            "page_title": f"Cancel {audit.number}",
            "form": form,
            "blocked": blocked,
            "submit_label": "Cancel audit",
            "destructive": True,
            "back_url": reverse("quality:audit-detail", args=[pk]),
            "crumbs": _crumbs(audit, "Cancel audit"),
        },
    )
