from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import FileResponse, Http404, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods

from accounts.roles import has_role, staff_role_required
from core.grids import active_filters
from core.models import AuditEvent

from . import audit_exports, services
from .audit_lists import COLUMNS, applied_filters, audit_selection
from .forms import (
    AuditCreateForm,
    CancelForm,
    CompleteForm,
    ScannerUploadForm,
    apply_errors,
)
from .models import InventoryAudit


@staff_role_required(*services.READ_ROLES)
@require_GET
def audit_list(request):
    try:
        grid, form, rows = audit_selection(actor=request.user, params=request.GET)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    page = grid.page(rows, COLUMNS)
    over_limit = page.paginator.count > audit_exports.EXPORT_CAP
    export_url = None
    if form.is_valid() and not over_limit:
        selection = {**applied_filters(grid, form), "sort": grid.sort}
        export_url = reverse("quality:audit-export", query=selection)
    return render(
        request,
        "quality/audit_list.html",
        {
            "page_title": "Inventory audits",
            "page_obj": page,
            "export_url": export_url,
            "exceeded_export_limit": f"{audit_exports.EXPORT_CAP:,}" if over_limit else "",
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


@staff_role_required(*services.READ_ROLES)
@require_GET
def audit_export(request):
    try:
        content = audit_exports.export_audits(actor=request.user, params=request.GET)
    except ValidationError as exc:
        return HttpResponseBadRequest(
            " ".join(exc.messages), content_type="text/plain; charset=utf-8"
        )
    response = FileResponse(
        content,
        as_attachment=True,
        filename="inventory-audits.csv",
        content_type="text/csv; charset=utf-8",
    )
    response["Cache-Control"] = "no-store"
    return response


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
    is_post = request.method == "POST"
    action = request.POST.get("action", "complete") if is_post else ""
    if is_post and action not in {"load_scanner", "complete"}:
        return HttpResponseBadRequest("Choose Load observations or Complete audit.")
    form = CompleteForm(request.POST if is_post else None, audit=audit)
    upload_form = ScannerUploadForm(
        request.POST if is_post and action == "load_scanner" else None,
        request.FILES if is_post and action == "load_scanner" else None,
    )
    loaded = False
    blocked = (
        "This audit is already finalized. Start a new audit to add evidence."
        if audit.status != "open"
        else ""
    )
    if not blocked and services.is_stale(actor=request.user, audit=audit):
        blocked = "Storage changed since this audit began. Cancel this audit and start a new one."
    if blocked:
        _disable_widgets(form)
        _disable_widgets(upload_form)
    elif is_post and action == "load_scanner":
        # Preview does not validate completion fields or turn unobserved entries into errors.
        # Keep every posted grid value and note if the upload is rejected.
        initial = {name: request.POST.get(name, "") for name in form.fields}
        form = CompleteForm(audit=audit, initial=initial)
        if upload_form.is_valid():
            try:
                observations = services.load_scanner_observations(
                    actor=request.user, audit_id=pk, upload=upload_form.cleaned_data["scanner_file"]
                )
            except ValidationError as exc:
                upload_form.add_error("scanner_file", exc)
                if exc.code in {"stale_snapshot", "invalid_transition"}:
                    blocked = " ".join(exc.messages)
                    _disable_widgets(form)
                    _disable_widgets(upload_form)
            else:
                for index, position in enumerate(audit.positions):
                    accession = observations[position["id"]]
                    initial[f"state_{index}"] = "accession" if accession else "empty"
                    initial[f"accession_{index}"] = accession
                form = CompleteForm(audit=audit, initial=initial)
                upload_form = ScannerUploadForm()
                loaded = True
    elif is_post and request.FILES:
        upload_form = ScannerUploadForm(request.POST, request.FILES)
        upload_form.is_valid()
        # A selected file must be deliberately loaded and reviewed before finalization.
        upload_form.add_error(
            "scanner_file", "Load observations and review them before completing."
        )
    elif is_post and form.is_valid():
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
            if getattr(exc, "code", None) in {"stale_snapshot", "invalid_transition"}:
                blocked = " ".join(exc.messages)
                _disable_widgets(form)
                _disable_widgets(upload_form)
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
            "upload_form": upload_form,
            "loaded": loaded,
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
