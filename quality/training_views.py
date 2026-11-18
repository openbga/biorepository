"""Authorized staff training pages and reasoned, revision-checked form submissions."""

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
from core.grids import GridRequest, active_filters

from . import training_services as services
from .models import TrainingRecord
from .training_exports import EXPORT_CAP, export_training
from .training_forms import FilterForm, TrainingForm, apply_errors
from .training_lists import COLUMNS, DEFAULT_SORT, applied_filters, filter_training


@staff_role_required(*services.READ_ROLES)
@require_GET
def training_list(request):
    try:
        grid = GridRequest.parse(request.GET, COLUMNS, DEFAULT_SORT)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    form = FilterForm(request.GET)
    rows = filter_training(services.training_records(actor=request.user), form, grid.query)
    page = grid.page(rows, COLUMNS)
    export_url = None
    if form.is_valid() and page.paginator.count <= EXPORT_CAP:
        selection = {**applied_filters(form, grid.query), "sort": grid.sort}
        export_url = reverse("quality:training-export", query=selection)
    return render(
        request,
        "quality/training/list.html",
        {
            "page_title": "Training records",
            "page_obj": page,
            "export_url": export_url,
            "export_over_limit": page.paginator.count > EXPORT_CAP,
            "filter_form": form,
            "inline": [form["status"], form["due"]],
            "q": grid.query,
            "sort": grid.sort,
            "active_filters": active_filters(request, form) if form.is_valid() else [],
            "filtered": bool(grid.query or request.GET.get("status") or request.GET.get("due")),
            "can_manage": has_role(request.user, *services.MANAGE_ROLES),
            "noun": "training records",
        },
    )


@staff_role_required(*services.READ_ROLES)
@require_GET
def training_export(request):
    try:
        content = export_training(actor=request.user, params=request.GET)
    except ValidationError as exc:
        return HttpResponseBadRequest(
            " ".join(exc.messages), content_type="text/plain; charset=utf-8"
        )
    response = HttpResponse(content, content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="training-records.csv"'
    response["Cache-Control"] = "no-store"
    return response


def _training(request, pk):
    try:
        return services.get_training(actor=request.user, training_id=pk)
    except TrainingRecord.DoesNotExist:
        raise Http404 from None


def _history(training):
    events = list(training.events.select_related("actor"))
    actor_fields = {"trainee_id", "created_by_id", "updated_by_id"}
    ids = {
        value
        for event in events
        for field, pair in event.changes.items()
        if field in actor_fields
        for value in pair
        if value is not None
    }
    users = {
        user.pk: user.get_full_name() or user.username for user in User.objects.filter(pk__in=ids)
    }
    labels = {
        field.attname: str(field.verbose_name).capitalize() for field in TrainingRecord._meta.fields
    }
    labels.update(
        due_on="Assignment due",
        procedure_reference="Procedure / version reference",
        trainer_provider="Trainer / provider",
        trainee_id="Trainee",
    )

    def display(field, value):
        if value is None or value == "":
            return ""
        if field in actor_fields:
            return users.get(value, str(value))
        if field == "status":
            return dict(TrainingRecord.Status.choices).get(value, value)
        if field.endswith("_at"):
            return timezone.localtime(datetime.fromisoformat(value)).strftime(
                "%Y-%m-%d %H:%M:%S %Z"
            )
        return str(value)

    return [
        (
            event,
            [
                (labels.get(field, field), display(field, pair[0]), display(field, pair[1]))
                for field, pair in event.changes.items()
                if pair[0] not in (None, "") or pair[1] not in (None, "")
            ],
        )
        for event in events
    ]


@staff_role_required(*services.READ_ROLES)
@require_GET
def training_detail(request, pk):
    training = _training(request, pk)
    return render(
        request,
        "quality/training/detail.html",
        {
            "page_title": training.number,
            "training": training,
            "can_manage": has_role(request.user, *services.MANAGE_ROLES),
            "history": _history(training),
            "toc": [
                ("assignment-heading", "Assignment"),
                ("completion-heading", "Completion"),
                ("record-heading", "Record facts"),
                ("history-heading", "History"),
            ],
        },
    )


def _form_page(request, *, form, title, label, training=None, blocked="", action="create"):
    if blocked:
        # Widget-only disabling retains posted input instead of replacing it with initial values.
        for field in form.fields.values():
            field.widget.attrs["disabled"] = True
    crumbs = [("Training records", reverse("quality:training-list"))]
    if training:
        crumbs.append((training.number, reverse("quality:training-detail", args=[training.pk])))
    crumbs.append((title, ""))
    return render(
        request,
        "quality/training/form.html",
        {
            "page_title": title,
            "form": form,
            "submit_label": label,
            "blocked": blocked,
            "back_url": reverse("quality:training-detail", args=[training.pk])
            if training
            else reverse("quality:training-list"),
            "crumbs": crumbs,
            "destructive": action == "cancel",
            "facts": [
                ("Trainee", training.trainee.get_full_name() or training.trainee.username),
                ("Topic", training.topic),
            ]
            if training
            else [],
            "notes": [
                "Training records retain human evidence and assessment. "
                "They do not certify competence, change permissions or block specimen work. "
                "The trainee cannot change; cancel an incorrect "
                "assignment and create a new record."
            ],
        },
    )


@staff_role_required(*services.MANAGE_ROLES)
@require_http_methods(["GET", "POST"])
def training_create(request):
    form = TrainingForm(request.POST if request.method == "POST" else None, actor=request.user)
    if request.method == "POST" and form.is_valid():
        try:
            training = services.create_training(
                actor=request.user,
                trainee_id=form.cleaned_data["trainee"].pk,
                values=form.record_values(),
                reason=form.cleaned_data["reason"],
            )
        except ValidationError as exc:
            apply_errors(form, exc)
        else:
            messages.success(request, "Training assignment recorded.")
            return redirect("quality:training-detail", pk=training.pk)
    return _form_page(
        request, form=form, title="New training record", label="Create training record"
    )


def _change(request, pk, action):
    training = _training(request, pk)
    form = TrainingForm(
        request.POST if request.method == "POST" else None,
        actor=request.user,
        training=training,
        action=action,
    )
    expected = {
        "edit": {"assigned"},
        "complete": {"assigned"},
        "correct": {"completed"},
        "cancel": {"assigned", "completed"},
    }[action]
    blocked = ""
    if request.method == "POST" and request.POST.get("revision") != str(training.revision):
        blocked = (
            "This training record changed while you were working. Your input is retained here. "
            "Reload the record and review its latest values before starting again."
        )
    elif training.status not in expected:
        blocked = (
            "This action is unavailable in the record's current status. "
            "Cancelled records are terminal."
        )
    labels = {
        "edit": "Save changes",
        "complete": "Record completion",
        "correct": "Correct training record",
        "cancel": "Cancel training record",
    }
    if not blocked and request.method == "POST" and form.is_valid():
        kwargs = dict(
            actor=request.user,
            training_id=pk,
            expected_revision=form.cleaned_data["revision"],
            reason=form.cleaned_data["reason"],
        )
        if action != "cancel":
            kwargs["values"] = form.record_values()
        operation = {
            "edit": services.update_training,
            "complete": services.complete_training,
            "correct": services.correct_training,
            "cancel": services.cancel_training,
        }[action]
        try:
            operation(**kwargs)
        except ValidationError as exc:
            apply_errors(form, exc)
            if getattr(exc, "code", None) in {"stale_revision", "invalid_transition"}:
                blocked = " ".join(exc.messages)
        else:
            messages.success(request, "Training record saved; earlier values remain in history.")
            return redirect("quality:training-detail", pk=pk)
    title = (
        f"Edit {training.number}" if action == "edit" else f"{labels[action]}: {training.number}"
    )
    return _form_page(
        request,
        form=form,
        title=title,
        label=labels[action],
        training=training,
        blocked=blocked,
        action=action,
    )


@staff_role_required(*services.MANAGE_ROLES)
@require_http_methods(["GET", "POST"])
def training_edit(request, pk):
    return _change(request, pk, "edit")


@staff_role_required(*services.MANAGE_ROLES)
@require_http_methods(["GET", "POST"])
def training_complete(request, pk):
    return _change(request, pk, "complete")


@staff_role_required(*services.MANAGE_ROLES)
@require_http_methods(["GET", "POST"])
def training_correct(request, pk):
    return _change(request, pk, "correct")


@staff_role_required(*services.MANAGE_ROLES)
@require_http_methods(["GET", "POST"])
def training_cancel(request, pk):
    return _change(request, pk, "cancel")
