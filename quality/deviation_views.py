from datetime import datetime

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods

from accounts.models import User
from accounts.roles import has_role, staff_role_required
from core.grids import active_filters

from . import deviation_exports
from . import deviation_services as services
from .deviation_forms import ActionForm, DeviationForm, ReviewForm, apply_errors
from .deviation_lists import COLUMNS, applied_filters, deviation_selection
from .models import Deviation


@staff_role_required(*services.READ_ROLES)
@require_GET
def deviation_list(request):
    try:
        grid, form, rows = deviation_selection(actor=request.user, params=request.GET)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    page = grid.page(rows, COLUMNS)
    over_limit = page.paginator.count > deviation_exports.EXPORT_CAP
    export_url = None
    if form.is_valid() and not over_limit:
        selection = {**applied_filters(grid, form), "sort": grid.sort}
        export_url = reverse("quality:deviation-export", query=selection)
    return render(
        request,
        "quality/deviation_list.html",
        {
            "page_title": "Deviations",
            "page_obj": page,
            "export_url": export_url,
            "exceeded_export_limit": f"{deviation_exports.EXPORT_CAP:,}" if over_limit else "",
            "filter_form": form,
            "inline": [form["status"]],
            "q": grid.query,
            "sort": grid.sort,
            "active_filters": active_filters(request, form) if form.is_valid() else [],
            "filtered": bool(grid.query or request.GET.get("status")),
            "can_operate": has_role(request.user, *services.OPERATE_ROLES),
            "noun": "deviations",
        },
    )


@staff_role_required(*services.READ_ROLES)
@require_GET
def deviation_export(request):
    try:
        content = deviation_exports.export_deviations(actor=request.user, params=request.GET)
    except ValidationError as exc:
        return HttpResponseBadRequest(
            " ".join(exc.messages), content_type="text/plain; charset=utf-8"
        )
    response = HttpResponse(content, content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="deviations.csv"'
    response["Cache-Control"] = "no-store"
    return response


def _deviation(request, pk):
    try:
        return services.get_deviation(actor=request.user, deviation_id=pk)
    except Deviation.DoesNotExist:
        raise Http404 from None


def _crumbs(deviation=None, current=None):
    result = [("Deviations", reverse("quality:deviation-list"))]
    if deviation:
        result.append((deviation.number, reverse("quality:deviation-detail", args=[deviation.pk])))
    if current:
        result.append((current, ""))
    return result


@staff_role_required(*services.READ_ROLES)
@require_GET
def deviation_detail(request, pk):
    deviation = _deviation(request, pk)
    can_manage = has_role(request.user, *services.MANAGE_ROLES)
    second_person = request.user.pk not in {deviation.created_by_id, deviation.submitted_by_id}
    history = []
    labels = {
        field.attname: str(field.verbose_name).capitalize() for field in Deviation._meta.fields
    }
    labels.update(occurred_at="Occurred at", effectiveness="Effectiveness")
    events = list(deviation.events.select_related("actor"))
    actor_fields = {"created_by_id", "submitted_by_id", "closed_by_id"}
    user_ids = {
        value
        for event in events
        for field, pair in event.changes.items()
        if field in actor_fields
        for value in pair
        if value is not None
    }
    users = {
        user.pk: user.get_full_name() or user.get_username()
        for user in User.objects.filter(pk__in=user_ids)
    }

    def display(field, value):
        if value is None or value == "":
            return ""
        if field in actor_fields:
            return users.get(value, str(value))
        if field == "excursion_id":
            return f"EX-{value:06d}"
        if field == "inventory_audit_id":
            return f"AUD-{value:06d}"
        if field == "status":
            return dict(Deviation.Status.choices).get(value, value)
        if field.endswith("_at"):
            return timezone.localtime(datetime.fromisoformat(value)).strftime(
                "%Y-%m-%d %H:%M:%S %Z"
            )
        return str(value)

    for event in events:
        changes = [
            (labels.get(field, field), display(field, pair[0]), display(field, pair[1]))
            for field, pair in event.changes.items()
        ]
        history.append((event, changes))
    return render(
        request,
        "quality/deviation_detail.html",
        {
            "page_title": deviation.number,
            "deviation": deviation,
            "can_operate": has_role(request.user, *services.OPERATE_ROLES),
            "can_manage": can_manage,
            "can_review": can_manage and second_person,
            "history": history,
            "toc": [
                ("report-heading", "Report"),
                ("followup-heading", "Investigation and follow-up"),
                ("review-heading", "Review facts"),
                ("history-heading", "History"),
            ],
        },
    )


def _disable(form):
    # Preserve bound POST values; Field.disabled would substitute current initial values.
    for field in form.fields.values():
        field.widget.attrs["disabled"] = True


def _blocked(request, deviation, status):
    if request.method == "POST" and request.POST.get("revision") != str(deviation.revision):
        return (
            "This deviation changed while you were working. Your input is retained here. "
            "Reload the record and review its latest values before starting again."
        )
    if deviation.status != status:
        return f"This action requires status {dict(Deviation.Status.choices)[status]}."
    return ""


def _form_page(request, *, form, title, label, deviation=None, blocked="", notes=()):
    if blocked:
        _disable(form)
    return render(
        request,
        "quality/form.html",
        {
            "page_title": title,
            "form": form,
            "submit_label": label,
            "blocked": blocked,
            "back_url": reverse("quality:deviation-detail", args=[deviation.pk])
            if deviation
            else reverse("quality:deviation-list"),
            "crumbs": _crumbs(deviation, title),
            "notes": notes,
        },
    )


@staff_role_required(*services.OPERATE_ROLES)
@require_http_methods(["GET", "POST"])
def deviation_create(request):
    form = DeviationForm(request.POST if request.method == "POST" else None, actor=request.user)
    if request.method == "POST" and form.is_valid():
        try:
            deviation = services.create_deviation(
                actor=request.user, values=form.report_values(), reason=form.cleaned_data["reason"]
            )
        except ValidationError as exc:
            apply_errors(form, exc)
        else:
            messages.success(
                request, "Deviation recorded. Complete the investigation before submitting."
            )
            return redirect("quality:deviation-detail", pk=deviation.pk)
    return _form_page(request, form=form, title="New deviation", label="Create deviation")


@staff_role_required(*services.OPERATE_ROLES)
@require_http_methods(["GET", "POST"])
def deviation_edit(request, pk):
    deviation = _deviation(request, pk)
    form = DeviationForm(
        request.POST if request.method == "POST" else None, actor=request.user, deviation=deviation
    )
    blocked = _blocked(request, deviation, Deviation.Status.OPEN)
    if not blocked and request.method == "POST" and form.is_valid():
        try:
            services.update_deviation(
                actor=request.user,
                deviation_id=pk,
                expected_revision=form.cleaned_data["revision"],
                values=form.report_values(),
                reason=form.cleaned_data["reason"],
            )
        except ValidationError as exc:
            apply_errors(form, exc)
            if getattr(exc, "code", None) in {"stale_revision", "invalid_transition"}:
                blocked = " ".join(exc.messages)
        else:
            messages.success(request, "Deviation updated; earlier values remain in history.")
            return redirect("quality:deviation-detail", pk=pk)
    return _form_page(
        request,
        form=form,
        title=f"Edit {deviation.number}",
        label="Save changes",
        deviation=deviation,
        blocked=blocked,
    )


def _action(request, pk, action):
    deviation = _deviation(request, pk)
    form_class = ReviewForm if action == "review" else ActionForm
    form = form_class(
        request.POST if request.method == "POST" else None, initial={"revision": deviation.revision}
    )
    expected = {"submit": "open", "review": "awaiting_review", "reopen": "closed"}[action]
    blocked = _blocked(request, deviation, expected)
    if action == "review" and request.user.pk in {
        deviation.created_by_id,
        deviation.submitted_by_id,
    }:
        blocked = (
            "A reviewer must differ from the creator and latest submitter. "
            "Ask another manager to review."
        )
    labels = {
        "submit": "Submit for review",
        "review": "Record review",
        "reopen": "Reopen deviation",
    }
    notes = {
        "submit": [
            "Submitting freezes the report for a different manager or administrator to review. "
            "Investigation, corrective action and effectiveness evidence must be complete; "
            "explain when none or unknown."
        ],
        "review": [
            "Close accepts this report. Return for revision opens it for editing. "
            "Both decisions require a reason retained in history."
        ],
        "reopen": [
            "Reopening allows operators to edit and submit again. "
            "The previous closure remains in history."
        ],
    }
    if not blocked and request.method == "POST" and form.is_valid():
        kwargs = {
            "actor": request.user,
            "deviation_id": pk,
            "expected_revision": form.cleaned_data["revision"],
            "reason": form.cleaned_data["reason"],
        }
        if action == "review":
            kwargs["decision"] = form.cleaned_data["decision"]
        operation = {
            "submit": services.submit_deviation,
            "review": services.review_deviation,
            "reopen": services.reopen_deviation,
        }[action]
        try:
            operation(**kwargs)
        except ValidationError as exc:
            apply_errors(form, exc)
            if getattr(exc, "code", None) in {"stale_revision", "invalid_transition"}:
                blocked = " ".join(exc.messages)
        else:
            messages.success(request, "Deviation action recorded in history.")
            return redirect("quality:deviation-detail", pk=pk)
    return _form_page(
        request,
        form=form,
        title=f"{labels[action]}: {deviation.number}",
        label=labels[action],
        deviation=deviation,
        blocked=blocked,
        notes=notes[action],
    )


@staff_role_required(*services.OPERATE_ROLES)
@require_http_methods(["GET", "POST"])
def deviation_submit(request, pk):
    return _action(request, pk, "submit")


@staff_role_required(*services.MANAGE_ROLES)
@require_http_methods(["GET", "POST"])
def deviation_review(request, pk):
    return _action(request, pk, "review")


@staff_role_required(*services.MANAGE_ROLES)
@require_http_methods(["GET", "POST"])
def deviation_reopen(request, pk):
    return _action(request, pk, "reopen")
