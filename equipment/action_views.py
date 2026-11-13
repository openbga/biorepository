"""Equipment forms and actions (docs/ui-standards.md: Form and Action form), rendered in the
shared form dialog with a full page as the no-JavaScript fallback.

Each view checks the role at entry, binds the form, calls one equipment.services function and
shows its errors under the fields they concern. Revisions are captured when the form opened. The
services repeat every rule under lock; a form shown as unavailable here is a courtesy.
"""

import uuid
from urllib.parse import urlencode

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from accounts.roles import staff_role_required

from . import services
from .forms import (
    EXCURSION_VALUES,
    LN2_VALUES,
    READING_VALUES,
    SERVICE_VALUES,
    EquipmentForm,
    ExcursionForm,
    ImportUploadForm,
    LN2Form,
    NoteForm,
    ReadingForm,
    ReopenForm,
    ReviewForm,
    ServiceForm,
    StatusForm,
    SubmitForm,
    apply_errors,
    values,
)
from .models import Equipment, Excursion, LN2Entry, ServiceEvent, TemperatureReading
from .views import equipment_crumbs, excursion_crumbs, import_crumbs, may_correct

OPERATE = services.OPERATE_ROLES
MANAGE = services.MANAGE_ROLES
ACTION_METHODS = require_http_methods(["GET", "POST"])
TEMPLATE = "equipment/form.html"
RETIRED = "This equipment is retired and takes no new entries. Change its status to record more."


# --- shared ---------------------------------------------------------------------------------


def _who(user):
    return (user.get_full_name() or user.get_username()) if user else ""


def _when(value, fmt="%Y-%m-%d %H:%M"):
    return timezone.localtime(value).strftime(fmt) if value else ""


def _detail(equipment):
    return reverse("equipment:equipment-detail", args=[equipment.pk])


def _uuid(value):
    try:
        return uuid.UUID(str(value))
    except TypeError, ValueError:
        return None


def _preselect(request, queryset_field):
    """The `?equipment=` value when it names an offered choice, else None."""
    pk = _uuid(request.GET.get("equipment"))
    if pk and queryset_field.queryset.filter(pk=pk).exists():
        return pk
    return None


def _errors(exc):
    if hasattr(exc, "error_dict"):
        return [e for errors in exc.error_dict.values() for e in errors]
    return exc.error_list


def _stale(form, exc, revision):
    """After a stale-revision error, the next submit carries the current revision."""
    if "expected_revision" in form.fields and any(e.code == "stale_revision" for e in _errors(exc)):
        form.data = form.data.copy()
        form.data["expected_revision"] = revision()


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
    tabs=(),
    destructive=False,
):
    """GET shows the form; POST validates, calls `run(cleaned_data)` and redirects on success."""
    if blocked:
        form.disable()
    elif request.method == "POST" and form.is_valid():
        try:
            result = run(form.cleaned_data)
        except ValidationError as exc:
            apply_errors(form, exc)
            if revision is not None:
                _stale(form, exc, revision)
        else:
            message = success(result) if callable(success) else success
            if isinstance(message, tuple):  # (level, text), e.g. a file check that found errors
                messages.add_message(request, *message)
            else:
                messages.success(request, message)
            target = done_url(result) if callable(done_url) else done_url
            return redirect(target or back_url)
    status = 200
    if request.method == "POST":
        status = 409 if blocked else 422 if form.errors else 200
    return render(
        request,
        TEMPLATE,
        {
            "page_title": title,
            "form": form,
            "submit_label": submit,
            "back_url": back_url,
            "crumbs": crumbs,
            "blocked": blocked,
            "notes": notes,
            "facts": facts,
            "tabs": tabs,
            "destructive": destructive,
        },
        status=status,
    )


def _post(request):
    return request.POST if request.method == "POST" else None


def _files(request):
    return request.FILES if request.method == "POST" else None


def _equipment_facts(equipment):
    return (
        ("Equipment", f"{equipment.code} · {equipment.name}"),
        ("Kind", equipment.get_kind_display()),
        ("Status", equipment.get_status_display()),
    )


def _limits(lower, upper):
    if lower is None and upper is None:
        return "No limits set"
    parts = []
    if lower is not None:
        parts.append(f"lower {lower} °C")
    if upper is not None:
        parts.append(f"upper {upper} °C")
    return ", ".join(parts)


def _correctable(user, entry, noun):
    """(blocked reason or "") for correcting an entry; PermissionDenied for the wrong person."""
    if not may_correct(user, entry):
        raise PermissionDenied(
            f"Only the person who recorded this {noun} or a manager may correct it."
        )
    if entry.voided:
        return f"This {noun} is a withdrawal. Record a new one instead."
    if type(entry).objects.filter(corrects=entry).exists():
        return f"This {noun} was already corrected. Correct the newer version."
    return ""


# --- register -------------------------------------------------------------------------------


@staff_role_required(*MANAGE)
@ACTION_METHODS
def equipment_edit(request, pk=None):
    equipment = get_object_or_404(Equipment, pk=pk) if pk else None
    form = EquipmentForm(_post(request), instance=equipment)
    names = services.EQUIPMENT_FIELDS

    def run(data):
        if equipment is None:
            return services.save_equipment(actor=request.user, **values(form, names))
        return services.save_equipment(
            actor=request.user,
            equipment_id=equipment.pk,
            expected_revision=data["expected_revision"],
            reason=data["reason"],
            **values(form, names),
        )

    title = f"Edit {equipment.code}" if equipment else "New equipment"
    return _action(
        request,
        form,
        title=title,
        submit="Save changes" if equipment else "Create equipment",
        back_url=_detail(equipment) if equipment else reverse("equipment:equipment-list"),
        crumbs=equipment_crumbs(equipment, (title, None)),
        run=run,
        success=lambda obj: f"Saved {obj.code}." if equipment else f"Registered {obj.code}.",
        done_url=_detail,
        revision=lambda: Equipment.objects.get(pk=equipment.pk).revision if equipment else None,
        notes=(
            "Status changes have their own action with an effective time and reason."
            if equipment
            else "New equipment starts in service.",
        ),
    )


@staff_role_required(*MANAGE)
@ACTION_METHODS
def status_change(request, pk):
    equipment = get_object_or_404(Equipment, pk=pk)
    form = StatusForm(_post(request), equipment=equipment)
    return _action(
        request,
        form,
        title="Change status",
        submit="Change status",
        back_url=_detail(equipment),
        crumbs=equipment_crumbs(equipment, ("Change status", None)),
        run=lambda d: services.change_status(
            actor=request.user,
            equipment_id=equipment.pk,
            expected_revision=d["expected_revision"],
            status=d["status"],
            occurred_at=d["occurred_at"],
            reason=d["reason"],
        ),
        success=lambda obj: f"{obj.code} is now {obj.get_status_display().lower()}.",
        revision=lambda: Equipment.objects.get(pk=equipment.pk).revision,
        facts=_equipment_facts(equipment),
        notes=(
            "Out of service and retired equipment stays in the register with its history. "
            "Retired equipment takes no new entries; retiring needs its excursions closed.",
        ),
    )


# --- service events -------------------------------------------------------------------------


@staff_role_required(*OPERATE)
@ACTION_METHODS
def service_create(request, pk):
    equipment = get_object_or_404(Equipment, pk=pk)
    form = ServiceForm(_post(request), _files(request), actor=request.user)
    kind = request.GET.get("kind")
    if kind in dict(ServiceEvent.Kind.choices):
        form.initial["kind"] = kind

    def run(data):
        return services.record_service_event(
            actor=request.user,
            equipment_id=equipment.pk,
            attachment=data.get("attachment"),
            kind=data["kind"],
            **values(form, SERVICE_VALUES),
        )

    return _action(
        request,
        form,
        title="Record service event",
        submit="Record service event",
        back_url=_detail(equipment),
        crumbs=equipment_crumbs(equipment, ("Record service event", None)),
        run=run,
        success=lambda e: f"Recorded {e.get_kind_display().lower()} of {equipment.code}.",
        blocked=RETIRED if equipment.is_retired else "",
        facts=_equipment_facts(equipment),
        notes=(
            "The next due date is your repository's own date; nothing is computed from an "
            "interval.",
        ),
    )


@staff_role_required(*OPERATE)
@ACTION_METHODS
def service_correct(request, pk):
    event = get_object_or_404(
        ServiceEvent.objects.select_related("equipment", "recorded_by"), pk=pk
    )
    equipment = event.equipment
    blocked = _correctable(request.user, event, "event")
    form = ServiceForm(_post(request), _files(request), actor=request.user, original=event)

    def run(data):
        return services.correct_service_event(
            actor=request.user,
            event_id=event.pk,
            reason=data["reason"],
            void=data["void"],
            attachment=data.get("attachment"),
            **values(form, SERVICE_VALUES),
        )

    return _action(
        request,
        form,
        title="Correct service event",
        submit="Correct service event",
        back_url=_detail(equipment),
        crumbs=equipment_crumbs(equipment, ("Correct service event", None)),
        run=run,
        success=lambda e: (
            f"Withdrew the {event.get_kind_display().lower()} of {event.performed_on:%Y-%m-%d}."
            if e.voided
            else f"Corrected the {event.get_kind_display().lower()} of {equipment.code}."
        ),
        done_url=lambda e: _detail(equipment) + "#service-heading",
        blocked=blocked,
        facts=(
            ("Equipment", f"{equipment.code} · {equipment.name}"),
            ("Event", f"{event.get_kind_display()}, performed {event.performed_on:%Y-%m-%d}"),
            ("Recorded", f"{_when(event.recorded_at)} by {_who(event.recorded_by)}"),
        ),
        notes=(
            "The correction is added to the record; the earlier entry stays, marked corrected.",
        ),
    )


# --- LN2 log --------------------------------------------------------------------------------


def _ln2_tabs(request, kind, extra):
    tabs = []
    for value, label in LN2Entry.Kind.choices:
        query = urlencode({**extra, "kind": value})
        tabs.append({"label": label, "url": f"{request.path}?{query}", "current": value == kind})
    return tabs


def _ln2_list(equipment_id):
    return reverse("equipment:ln2-list") + f"?equipment={equipment_id}"


@staff_role_required(*OPERATE)
@ACTION_METHODS
def ln2_create(request):
    kind = request.POST.get("kind") or request.GET.get("kind")
    if kind not in dict(LN2Entry.Kind.choices):
        kind = LN2Entry.Kind.LEVEL
    form = LN2Form(_post(request), kind=kind)
    preselected = _preselect(request, form.fields["equipment"])
    if preselected:
        form.initial["equipment"] = preselected
        vessel = Equipment.objects.get(pk=preselected)
        if vessel.level_unit:
            form.initial.setdefault("level_unit", vessel.level_unit)

    def run(data):
        return services.record_ln2(
            actor=request.user,
            equipment_id=data["equipment"].pk,
            kind=kind,
            **values(form, LN2_VALUES),
        )

    return _action(
        request,
        form,
        title="Record LN2 entry",
        submit="Record LN2 entry",
        back_url=reverse("equipment:ln2-list"),
        crumbs=[("LN2 log", reverse("equipment:ln2-list")), ("Record LN2 entry", None)],
        run=run,
        success=lambda e: f"Recorded a {e.get_kind_display().lower()} of {e.equipment.code}.",
        done_url=lambda e: _ln2_list(e.equipment_id),
        tabs=_ln2_tabs(request, kind, {"equipment": preselected} if preselected else {}),
        notes=(
            "The change since the previous entry is worked out from the levels; it is not entered.",
        ),
    )


@staff_role_required(*OPERATE)
@ACTION_METHODS
def ln2_correct(request, pk):
    entry = get_object_or_404(LN2Entry.objects.select_related("equipment", "recorded_by"), pk=pk)
    blocked = _correctable(request.user, entry, "entry")
    form = LN2Form(_post(request), kind=entry.kind, original=entry)
    back = _ln2_list(entry.equipment_id)
    return _action(
        request,
        form,
        title="Correct LN2 entry",
        submit="Correct LN2 entry",
        back_url=back,
        crumbs=[("LN2 log", reverse("equipment:ln2-list")), ("Correct LN2 entry", None)],
        run=lambda d: services.correct_ln2(
            actor=request.user,
            entry_id=entry.pk,
            reason=d["reason"],
            void=d["void"],
            **values(form, LN2_VALUES),
        ),
        success=lambda e: "Withdrew the entry." if e.voided else "Corrected the entry.",
        blocked=blocked,
        facts=(
            ("Vessel", f"{entry.equipment.code} · {entry.equipment.name}"),
            ("Entry", f"{entry.get_kind_display()}, {_when(entry.occurred_at)}"),
            ("Recorded", f"{_when(entry.recorded_at)} by {_who(entry.recorded_by)}"),
        ),
        notes=("The correction is added to the log; the earlier entry stays in the history.",),
    )


# --- temperature readings -------------------------------------------------------------------


def _readings(equipment_id):
    return reverse("equipment:reading-list") + f"?equipment={equipment_id}"


@staff_role_required(*OPERATE)
@ACTION_METHODS
def reading_create(request):
    form = ReadingForm(_post(request))
    preselected = _preselect(request, form.fields["equipment"])
    notes = ["The limits set on the equipment now are kept with the reading."]
    if preselected:
        form.initial["equipment"] = preselected
        chosen = Equipment.objects.get(pk=preselected)
        notes = [
            f"Limits set for {chosen.code}: {_limits(chosen.lower_limit, chosen.upper_limit)}. "
            "They are kept with the reading."
        ]
    return _action(
        request,
        form,
        title="Record reading",
        submit="Record reading",
        back_url=reverse("equipment:reading-list"),
        crumbs=[
            ("Temperature readings", None),
            ("Readings", reverse("equipment:reading-list")),
            ("Record reading", None),
        ],
        run=lambda d: services.record_reading(
            actor=request.user, equipment_id=d["equipment"].pk, **values(form, READING_VALUES)
        ),
        success=lambda r: (
            f"Recorded {r.value} °C on {r.equipment.code}."
            + (" It is outside the limits set." if r.outside_limits else "")
        ),
        done_url=lambda r: _readings(r.equipment_id),
        notes=notes,
    )


@staff_role_required(*OPERATE)
@ACTION_METHODS
def reading_correct(request, pk):
    reading = get_object_or_404(
        TemperatureReading.objects.select_related("equipment", "recorded_by"), pk=pk
    )
    blocked = _correctable(request.user, reading, "reading")
    form = ReadingForm(_post(request), original=reading)
    return _action(
        request,
        form,
        title="Correct reading",
        submit="Correct reading",
        back_url=_readings(reading.equipment_id),
        crumbs=[
            ("Temperature readings", None),
            ("Readings", reverse("equipment:reading-list")),
            ("Correct reading", None),
        ],
        run=lambda d: services.correct_reading(
            actor=request.user,
            reading_id=reading.pk,
            reason=d["reason"],
            void=d["void"],
            **values(form, READING_VALUES),
        ),
        success=lambda r: "Withdrew the reading." if r.voided else "Corrected the reading.",
        blocked=blocked,
        facts=(
            ("Equipment", f"{reading.equipment.code} · {reading.equipment.name}"),
            ("Reading", f"{reading.value} °C at {_when(reading.measured_at)}"),
            ("Limits recorded", _limits(reading.lower_limit, reading.upper_limit)),
            ("Recorded", f"{_when(reading.recorded_at)} by {_who(reading.recorded_by)}"),
        ),
        notes=(
            "The limits recorded with the reading stay as they were. The earlier reading stays "
            "in the history.",
        ),
    )


@staff_role_required(*OPERATE)
@ACTION_METHODS
def import_upload(request):
    form = ImportUploadForm(_post(request), _files(request))

    def run(data):
        chosen = data.get("equipment")
        return services.validate_reading_import(
            actor=request.user, file=data["file"], equipment_id=chosen.pk if chosen else None
        )

    def success(batch):
        if batch.is_valid:
            return f"The file is valid: {batch.row_count} readings ready to record."
        return (
            messages.WARNING,
            f"The file has {len(batch.errors)} errors. Nothing was recorded; fix the file and "
            "upload it again.",
        )

    columns = ", ".join(services.IMPORT_COLUMNS)
    return _action(
        request,
        form,
        title="Upload readings",
        submit="Validate file",
        back_url=reverse("equipment:import-list"),
        crumbs=import_crumbs(None, ("Upload readings", None)),
        run=run,
        success=success,
        done_url=lambda batch: reverse("equipment:import-detail", args=[batch.pk]),
        notes=(
            "Uploading only checks the file; nothing is recorded until you commit it.",
            f"Columns: {columns}. Required: measured_at and temperature_c. Times are ISO 8601; "
            "without an offset they are read in repository time. equipment and device are asset "
            "IDs.",
        ),
    )


# --- excursions -----------------------------------------------------------------------------


def _excursion(pk):
    return get_object_or_404(Excursion.objects.select_related("equipment", "submitted_by"), pk=pk)


def _excursion_detail(excursion):
    return reverse("equipment:excursion-detail", args=[excursion.pk])


def _excursion_facts(excursion):
    return (
        ("Excursion", excursion.number),
        ("Equipment", f"{excursion.equipment.code} · {excursion.equipment.name}"),
        ("Status", excursion.get_status_display()),
        ("Limits in force", _limits(excursion.lower_limit, excursion.upper_limit)),
    )


def _revision(excursion):
    return lambda: Excursion.objects.get(pk=excursion.pk).revision


@staff_role_required(*OPERATE)
@ACTION_METHODS
def excursion_create(request):
    form = ExcursionForm(_post(request), _files(request))
    preselected = _preselect(request, form.fields["equipment"])
    notes = [
        "The equipment's limits and storage unit at the time you save are kept with the excursion."
    ]
    facts = ()
    if preselected:
        form.initial["equipment"] = preselected
        chosen = Equipment.objects.select_related("storage_unit").get(pk=preselected)
        facts = (
            ("Equipment", f"{chosen.code} · {chosen.name}"),
            ("Storage unit", chosen.storage_unit.code if chosen.storage_unit_id else "None"),
            (
                "Limits in force",
                f"{_limits(chosen.lower_limit, chosen.upper_limit)} (set by the repository)",
            ),
        )

    def run(data):
        return services.open_excursion(
            actor=request.user,
            equipment_id=data["equipment"].pk,
            note=data.get("note", ""),
            attachment=data.get("attachment"),
            **values(form, EXCURSION_VALUES),
        )

    return _action(
        request,
        form,
        title="New excursion",
        submit="Create excursion",
        back_url=reverse("equipment:excursion-list"),
        crumbs=excursion_crumbs(None, ("New excursion", None)),
        run=run,
        success=lambda e: f"Recorded excursion {e.number}.",
        done_url=_excursion_detail,
        facts=facts,
        notes=notes,
    )


@staff_role_required(*OPERATE)
@ACTION_METHODS
def excursion_edit(request, pk):
    excursion = _excursion(pk)
    title = f"Edit {excursion.number}"
    form = ExcursionForm(_post(request), instance=excursion)
    return _action(
        request,
        form,
        title=title,
        submit="Save changes",
        back_url=_excursion_detail(excursion),
        crumbs=excursion_crumbs(excursion, (title, None)),
        run=lambda d: services.update_excursion(
            actor=request.user,
            excursion_id=excursion.pk,
            expected_revision=d["expected_revision"],
            **values(form, EXCURSION_VALUES),
        ),
        success=f"Saved {excursion.number}.",
        revision=_revision(excursion),
        blocked=""
        if excursion.status == Excursion.Status.OPEN
        else "Only an open excursion can be edited. A manager returns it for more work first.",
        facts=_excursion_facts(excursion),
        notes=("The timeline keeps each earlier value.",),
    )


@staff_role_required(*OPERATE)
@ACTION_METHODS
def excursion_submit(request, pk):
    excursion = _excursion(pk)
    form = SubmitForm(_post(request), initial={"expected_revision": excursion.revision})
    return _action(
        request,
        form,
        title="Submit for review",
        submit="Submit for review",
        back_url=_excursion_detail(excursion),
        crumbs=excursion_crumbs(excursion, ("Submit for review", None)),
        run=lambda d: services.submit_excursion(
            actor=request.user,
            excursion_id=excursion.pk,
            expected_revision=d["expected_revision"],
            note=d.get("note", ""),
        ),
        success=f"Submitted {excursion.number} for review.",
        revision=_revision(excursion),
        blocked=""
        if excursion.status == Excursion.Status.OPEN
        else "Only an open excursion can be submitted.",
        facts=_excursion_facts(excursion),
        notes=(
            "It needs its end time, cause, action taken and impact assessment. A manager or "
            "administrator who did not submit it reviews and closes it.",
        ),
    )


@staff_role_required(*MANAGE)
@ACTION_METHODS
def excursion_review(request, pk):
    excursion = _excursion(pk)
    may_close = excursion.submitted_by_id != request.user.pk
    form = ReviewForm(
        _post(request), may_close=may_close, initial={"expected_revision": excursion.revision}
    )
    if request.GET.get("decision") in ("return", "close") and may_close:
        form.initial["decision"] = request.GET["decision"]

    def run(data):
        action = (
            services.close_excursion if data["decision"] == "close" else (services.return_excursion)
        )
        return action(
            actor=request.user,
            excursion_id=excursion.pk,
            expected_revision=data["expected_revision"],
            note=data["note"],
        )

    notes = ["Closing captures the specimens that were in the unit during the excursion."]
    if not may_close:
        notes.insert(
            0,
            "You submitted this excursion. Another manager or administrator reviews and closes "
            "it; you can return it for more work.",
        )
    return _action(
        request,
        form,
        title="Review excursion",
        submit="Record review",
        back_url=_excursion_detail(excursion),
        crumbs=excursion_crumbs(excursion, ("Review excursion", None)),
        run=run,
        success=lambda e: (
            f"Closed {e.number}."
            if e.status == Excursion.Status.CLOSED
            else f"Returned {e.number} for more work."
        ),
        revision=_revision(excursion),
        blocked=""
        if excursion.status == Excursion.Status.REVIEW
        else "Only an excursion awaiting review can be reviewed.",
        facts=(
            *_excursion_facts(excursion),
            ("Submitted", f"{_when(excursion.submitted_at)} by {_who(excursion.submitted_by)}"),
        ),
        notes=notes,
    )


@staff_role_required(*MANAGE)
@ACTION_METHODS
def excursion_reopen(request, pk):
    excursion = _excursion(pk)
    form = ReopenForm(_post(request), initial={"expected_revision": excursion.revision})
    return _action(
        request,
        form,
        title="Reopen excursion",
        submit="Reopen excursion",
        back_url=_excursion_detail(excursion),
        crumbs=excursion_crumbs(excursion, ("Reopen excursion", None)),
        run=lambda d: services.reopen_excursion(
            actor=request.user,
            excursion_id=excursion.pk,
            expected_revision=d["expected_revision"],
            reason=d["reason"],
        ),
        success=f"Reopened {excursion.number}.",
        revision=_revision(excursion),
        blocked=""
        if excursion.status == Excursion.Status.CLOSED
        else "Only a closed excursion can be reopened.",
        facts=_excursion_facts(excursion),
        notes=(
            "The specimen list captured when it was closed stays with the record; closing it "
            "again captures a new list.",
        ),
    )


@staff_role_required(*OPERATE)
@ACTION_METHODS
def excursion_note(request, pk):
    excursion = _excursion(pk)
    form = NoteForm(_post(request), _files(request))
    return _action(
        request,
        form,
        title="Add note",
        submit="Add note",
        back_url=_excursion_detail(excursion),
        crumbs=excursion_crumbs(excursion, ("Add note", None)),
        run=lambda d: services.add_excursion_note(
            actor=request.user,
            excursion_id=excursion.pk,
            note=d.get("note", ""),
            attachment=d.get("attachment"),
        ),
        success=f"Added a note to {excursion.number}.",
        done_url=_excursion_detail(excursion) + "#timeline-heading",
        facts=_excursion_facts(excursion),
        notes=("Enter a note, attach a file, or both.",),
    )
