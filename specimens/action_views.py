"""Specimen action forms and the derivation entry grid.

Each view is an Action form (docs/ui-standards.md section 2) rendered in the shared form dialog
with a full-page fallback. Views handle transport only: role check at entry, revisions captured
when the form opened, calling specimens.services and showing its errors under the fields they
concern. The services repeat every rule under lock; a form shown as unavailable here is a
courtesy, not the gate.
"""

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse as url
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from accounts.roles import can_approve_disposal, has_role, staff_role_required
from storage.models import Occupancy
from storage.services import position_path

from . import services
from .forms import (
    CHILD_FIELDS,
    DECISION_LABELS,
    AdjustForm,
    CheckInForm,
    CheckOutForm,
    ChildFormSet,
    ConsumeForm,
    CorrectForm,
    CustodyCorrectForm,
    DecideForm,
    DeriveForm,
    DisposalRequestForm,
    HoldForm,
    PlaceForm,
    ReasonForm,
    RegisterForm,
    RelocateForm,
    ThawCorrectForm,
    TimedReasonForm,
    TransferForm,
    apply_errors,
    box_choices,
    catalogue,
    suggestion_text,
)
from .models import (
    CustodyEvent,
    Derivation,
    DisposalRequest,
    Hold,
    QuantityEntry,
    QuantityUnit,
    Specimen,
    SpecimenContainerType,
    SpecimenType,
    ThawEvent,
)
from .services import MANAGE_ROLES as MANAGE
from .services import OPERATE_ROLES as OPERATE
from .templatetags.specimens import amount

ACTION_METHODS = require_http_methods(["GET", "POST"])
FORM_TEMPLATE = "specimens/actions/form.html"
DERIVE_TEMPLATE = "specimens/actions/derive.html"


# --- shared ---------------------------------------------------------------------------------


def _specimen(pk):
    return get_object_or_404(
        Specimen.objects.select_related("unit", "custodian", "open_checkout__to_custodian"), pk=pk
    )


def _occupancy(specimen):
    return Occupancy.objects.select_related("position").get(pk=specimen.occupancy_id)


def _detail(specimen):
    return url("specimens:specimen-detail", args=[specimen.pk])


def _context(specimen, occupancy):
    """The compact block at the top of every action dialog (actions/_context.html).

    `checkout` is the open period in hand: a check-out, or the return that brought the specimen
    back (`returned`). A distributed specimen has neither a position nor a custodian.
    """
    checkout = specimen.open_checkout
    return {
        "specimen": specimen,
        "location": position_path(occupancy.position) if occupancy.position_id else None,
        "checkout": checkout,
        "returned": _returned(specimen),
        "holds": list(services.active_holds(specimen)),
    }


def _returned(specimen):
    """True while the open period in hand is a return from outside (not a check-out)."""
    checkout = specimen.open_checkout
    return checkout is not None and checkout.kind == CustodyEvent.Kind.RETURN


def _crumbs(specimen, title):
    return [
        ("Specimens", url("specimens:specimen-list")),
        (specimen.accession, _detail(specimen)),
        (title, None),
    ]


def _who(user):
    return user.get_full_name() or user.get_username() if user else ""


def _when(value):
    return timezone.localtime(value).strftime("%Y-%m-%d %H:%M") if value else ""


def _status(request, form, blocked, formset=None):
    if request.method != "POST":
        return 200
    if blocked:
        return 409
    if form.errors or (formset is not None and formset.total_error_count()):
        return 422
    return 200


def _page(
    request,
    form,
    *,
    title,
    submit,
    back_url,
    crumbs,
    specimen=None,
    occupancy=None,
    blocked="",
    notes=(),
    facts=(),
    destructive=False,
    template=FORM_TEMPLATE,
    extra=None,
):
    context = {
        "page_title": title,
        "form": form,
        "submit_label": submit,
        "back_url": back_url,
        "crumbs": crumbs,
        "blocked": blocked,
        "notes": notes,
        "facts": facts,
        "destructive": destructive,
        **(_context(specimen, occupancy) if specimen is not None else {}),
        **(extra or {}),
    }
    status = _status(request, form, blocked, (extra or {}).get("formset"))
    return render(request, template, context, status=status)


def _action(
    request,
    specimen,
    form_class,
    *,
    title,
    submit,
    run,
    success,
    blocked="",
    occupancy=True,
    form_kwargs=None,
    back_url=None,
    crumbs=None,
    done_url=None,
    notes=(),
    facts=(),
    destructive=False,
):
    """GET shows the form; POST validates, calls `run(cleaned_data)` and redirects on success."""
    occ = _occupancy(specimen)
    form = form_class(
        request.POST if request.method == "POST" else None,
        actor=request.user,
        specimen=specimen,
        occupancy=occ if occupancy else None,
        **{"floor": occ.last_occurred_at, **(form_kwargs or {})},
    )
    if blocked:
        form.disable()
    elif request.method == "POST" and form.is_valid():
        try:
            result = run(form.cleaned_data)
        except ValidationError as exc:
            apply_errors(form, exc)
        else:
            messages.success(request, success(result) if callable(success) else success)
            return redirect(done_url or _detail(specimen))
    return _page(
        request,
        form,
        title=title,
        submit=submit,
        back_url=back_url or _detail(specimen),
        crumbs=crumbs or _crumbs(specimen, title),
        specimen=specimen,
        occupancy=occ,
        blocked=blocked,
        notes=notes,
        facts=facts,
        destructive=destructive,
    )


def _revisions(data):
    return {
        "expected_revision": data["expected_revision"],
        "expected_occupancy_revision": data["expected_occupancy_revision"],
    }


def _position_id(data):
    position = data.get("position")
    return position.pk if position else None


def refusal(check, *args):
    """The message a services check raises for the current state, or "" when it passes.

    Reuses the service's own rule and wording for the courtesy `blocked` notice; the service
    repeats the check under lock when the form is submitted.
    """
    try:
        check(*args)
    except ValidationError as exc:
        return " ".join(exc.messages)
    return ""


def _disposed(specimen):
    return refusal(services.require_not_disposed, specimen)


def _away(specimen):
    """Physical steps need the material here: refused when disposed or distributed."""
    return refusal(services.require_in_custody, specimen)


def _use_hold(specimen, doing):
    """A use or all-use hold blocks check-out, splitting, use and hand-over."""
    return refusal(services.require_no_use_hold, specimen, doing)


def _disposal_hold(specimen):
    """A disposal or all-use hold blocks disposal; a use hold does not."""
    return refusal(services.require_no_disposal_hold, specimen)


def _not_in_hand(specimen, occupancy, doing):
    if specimen.open_checkout_id or (
        occupancy.position_id is None and services.never_stored(specimen)
    ):
        return ""
    if occupancy.position_id is None:
        return f"Specimen {specimen.accession} is not in hand. It must be checked out first."
    return f"Specimen {specimen.accession} is in storage. Record a check-out before it is {doing}."


def _first(*reasons):
    return next((reason for reason in reasons if reason), "")


# --- registration ---------------------------------------------------------------------------


@staff_role_required(*OPERATE)
@ACTION_METHODS
def register(request):
    title = "Register specimen"
    form = RegisterForm(request.POST if request.method == "POST" else None, actor=request.user)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        registration = services.Registration(
            specimen_type_id=data["specimen_type"].pk,
            container_type_id=data["container_type"].pk,
            quantity=data["quantity"],
            supplied_text=data["quantity"],
            unit_id=data["unit"].pk,
            collected_at=data["collected_at"],
            processing_started_at=data.get("processing_started_at"),
            processing_completed_at=data.get("processing_completed_at"),
            participant_id=data["participant"].pk if data.get("participant") else None,
            collection_id=data["collection"].pk if data.get("collection") else None,
            baseline_thaws=data.get("baseline_thaws"),
            custodian_id=data["custodian"].pk,
            position_id=_position_id(data),
        )
        try:
            specimen = services.register_specimen(
                actor=request.user,
                data=registration,
                occurred_at=data["occurred_at"],
                reason=data["reason"],
            )
        except ValidationError as exc:
            apply_errors(form, exc)
        else:
            messages.success(request, f"Registered {specimen.accession}.")
            return redirect(_detail(specimen))
    list_url = url("specimens:specimen-list")
    return _page(
        request,
        form,
        title=title,
        submit=title,
        back_url=list_url,
        crumbs=[("Specimens", list_url), (title, None)],
        notes=(
            "The system assigns the specimen identifier. It carries no location, participant "
            "or study meaning.",
        ),
    )


# --- placement and custody ------------------------------------------------------------------


@staff_role_required(*OPERATE)
@ACTION_METHODS
def place(request, pk):
    specimen = _specimen(pk)
    occupancy = _occupancy(specimen)
    blocked = _first(
        _away(specimen),
        specimen.open_checkout_id
        and (
            "This specimen is in hand after its return. Record a check-in to store it."
            if _returned(specimen)
            else "This specimen is checked out. Record a check-in to return it to storage."
        ),
        occupancy.position_id
        and "This specimen is already in storage. Use Relocate specimen to move it.",
        specimen.status != Specimen.Status.AVAILABLE
        and "Only material with an amount on hand is placed.",
    )
    return _action(
        request,
        specimen,
        PlaceForm,
        title="Place in storage",
        submit="Place in storage",
        blocked=blocked,
        run=lambda d: services.place_specimen(
            actor=request.user,
            specimen_id=specimen.pk,
            position_id=_position_id(d),
            occurred_at=d["occurred_at"],
            reason=d["reason"],
            **_revisions(d),
        ),
        success=f"Placed {specimen.accession} in storage.",
    )


@staff_role_required(*OPERATE)
@ACTION_METHODS
def relocate(request, pk):
    specimen = _specimen(pk)
    occupancy = _occupancy(specimen)
    blocked = _first(
        _away(specimen),
        not occupancy.position_id
        and "This specimen is not in a storage position, so it cannot be relocated.",
    )
    return _action(
        request,
        specimen,
        RelocateForm,
        title="Relocate specimen",
        submit="Relocate specimen",
        blocked=blocked,
        run=lambda d: services.relocate_specimen(
            actor=request.user,
            specimen_id=specimen.pk,
            position_id=_position_id(d),
            occurred_at=d["occurred_at"],
            reason=d["reason"],
            corrects_id=d.get("corrects") or None,
            **_revisions(d),
        ),
        success=f"Relocated {specimen.accession}.",
    )


@staff_role_required(*OPERATE)
@ACTION_METHODS
def check_out(request, pk):
    specimen = _specimen(pk)
    occupancy = _occupancy(specimen)
    blocked = _first(
        _away(specimen),
        specimen.open_checkout_id
        and (
            "This specimen is already in hand after its return. Record a check-in to store it."
            if _returned(specimen)
            else "This specimen is already checked out."
        ),
        _use_hold(specimen, "checked out"),
        not occupancy.position_id
        and "This specimen is not in storage. Record a custody transfer to hand it over.",
    )
    return _action(
        request,
        specimen,
        CheckOutForm,
        title="Record check-out",
        submit="Record check-out",
        blocked=blocked,
        notes=("Checking out frees the position and starts the time-out-of-storage clock.",),
        run=lambda d: services.check_out(
            actor=request.user,
            specimen_id=specimen.pk,
            recipient_id=d["recipient"].pk,
            destination=d["destination"],
            occurred_at=d["occurred_at"],
            reason=d["reason"],
            **_revisions(d),
        ),
        success=f"Recorded check-out of {specimen.accession}.",
    )


@staff_role_required(*OPERATE)
@ACTION_METHODS
def check_in(request, pk):
    specimen = _specimen(pk)
    blocked = _first(
        _away(specimen),
        not specimen.open_checkout_id
        and "This specimen is not checked out, so there is nothing to check in.",
    )
    notes = ()
    if _returned(specimen):
        source = specimen.open_checkout.destination
        notes = (
            "This stores the specimen returned to the repository"
            + (f" from {source}" if source else "")
            + ". Time since its return counts as time out of storage; thaws reported for the "
            "time away are already recorded.",
        )
    return _action(
        request,
        specimen,
        CheckInForm,
        title="Record check-in",
        submit="Record check-in",
        blocked=blocked,
        notes=notes,
        run=lambda d: services.check_in(
            actor=request.user,
            specimen_id=specimen.pk,
            position_id=_position_id(d),
            recipient_id=d["recipient"].pk,
            thaw_observed=d["thaw_observed"],
            occurred_at=d["occurred_at"],
            reason=d["reason"],
            **_revisions(d),
        ),
        success=f"Recorded check-in of {specimen.accession}.",
    )


@staff_role_required(*OPERATE)
@ACTION_METHODS
def transfer(request, pk):
    specimen = _specimen(pk)
    blocked = _first(_away(specimen), _use_hold(specimen, "handed over for use"))
    return _action(
        request,
        specimen,
        TransferForm,
        title="Record custody transfer",
        submit="Record custody transfer",
        blocked=blocked,
        notes=("The specimen stays where it is; only the person responsible for it changes.",),
        run=lambda d: services.transfer_custody(
            actor=request.user,
            specimen_id=specimen.pk,
            recipient_id=d["recipient"].pk,
            destination=d["destination"],
            occurred_at=d["occurred_at"],
            reason=d["reason"],
            **_revisions(d),
        ),
        success=f"Recorded custody transfer of {specimen.accession}.",
    )


# --- quantity -------------------------------------------------------------------------------


@staff_role_required(*OPERATE)
@ACTION_METHODS
def consume(request, pk):
    specimen = _specimen(pk)
    occupancy = _occupancy(specimen)
    blocked = _first(
        _away(specimen),
        specimen.status != Specimen.Status.AVAILABLE
        and f"Specimen {specimen.accession} has nothing left on hand.",
        _use_hold(specimen, "used"),
        _not_in_hand(specimen, occupancy, "used"),
    )
    return _action(
        request,
        specimen,
        ConsumeForm,
        title="Record consumption",
        submit="Record consumption",
        blocked=blocked,
        occupancy=False,
        run=lambda d: services.consume_quantity(
            actor=request.user,
            specimen_id=specimen.pk,
            expected_revision=d["expected_revision"],
            quantity=d["quantity"],
            supplied_text=d["quantity"],
            occurred_at=d["occurred_at"],
            reason=d["reason"],
        ),
        success=f"Recorded consumption from {specimen.accession}.",
    )


@staff_role_required(*MANAGE)
@ACTION_METHODS
def adjust(request, pk):
    specimen = _specimen(pk)
    return _action(
        request,
        specimen,
        AdjustForm,
        title="Adjust amount",
        submit="Adjust amount",
        blocked=_away(specimen),
        occupancy=False,
        notes=("A measured correction of the amount on hand. The earlier entries stay.",),
        run=lambda d: services.adjust_quantity(
            actor=request.user,
            specimen_id=specimen.pk,
            expected_revision=d["expected_revision"],
            delta=d["delta"],
            supplied_text=d["delta"],
            occurred_at=d["occurred_at"],
            reason=d["reason"],
        ),
        success=f"Adjusted the amount of {specimen.accession}.",
    )


@staff_role_required(*MANAGE)
@ACTION_METHODS
def reverse(request, pk):
    entry = get_object_or_404(QuantityEntry.objects.select_related("actor", "unit"), pk=pk)
    specimen = _specimen(entry.specimen_id)
    blocked = _first(
        _away(specimen),
        entry.kind not in (QuantityEntry.Kind.CONSUME, QuantityEntry.Kind.ADJUST)
        and "Only a consumed or adjusted amount can be reversed.",
        QuantityEntry.objects.filter(reverses=entry).exists()
        and "This entry was already reversed.",
    )
    return _action(
        request,
        specimen,
        ReasonForm,
        title="Reverse entry",
        submit="Reverse entry",
        blocked=blocked,
        occupancy=False,
        facts=(
            ("Entry", entry.get_kind_display()),
            ("Change", amount(entry.delta, entry.unit)),
            ("Recorded", f"{_when(entry.occurred_at)} by {_who(entry.actor)}"),
            ("Reason", entry.reason),
        ),
        notes=("The reversal is a new entry at the current time; the original stays.",),
        run=lambda d: services.reverse_quantity(
            actor=request.user,
            entry_id=entry.pk,
            expected_revision=d["expected_revision"],
            reason=d["reason"],
        ),
        success=f"Reversed an entry of {specimen.accession}.",
    )


# --- holds and thaw -------------------------------------------------------------------------


@staff_role_required(*OPERATE)
@ACTION_METHODS
def hold_add(request, pk):
    specimen = _specimen(pk)
    return _action(
        request,
        specimen,
        HoldForm,
        title="Place hold",
        submit="Place hold",
        blocked=_disposed(specimen),
        occupancy=False,
        run=lambda d: services.place_hold(
            actor=request.user,
            specimen_id=specimen.pk,
            expected_revision=d["expected_revision"],
            scope=d["scope"],
            occurred_at=d["occurred_at"],
            reason=d["reason"],
        ),
        success=f"Placed a hold on {specimen.accession}.",
    )


@staff_role_required(*MANAGE)
@ACTION_METHODS
def hold_release(request, pk):
    hold = get_object_or_404(Hold.objects.select_related("actor"), pk=pk)
    specimen = _specimen(hold.specimen_id)
    occupancy = _occupancy(specimen)
    released = hasattr(hold, "release")
    blocked = _first(
        released and "This hold was already released.",
        hold.reference
        and f"This hold belongs to “{hold.reference}” and is released through that workflow.",
    )
    return _action(
        request,
        specimen,
        TimedReasonForm,
        title="Release hold",
        submit="Release hold",
        blocked=blocked,
        occupancy=False,
        form_kwargs={"floor": max(occupancy.last_occurred_at, hold.occurred_at)},
        facts=(
            ("Hold", hold.get_scope_display()),
            *((("For", hold.reference),) if hold.reference else ()),
            ("Placed", f"{_when(hold.occurred_at)} by {_who(hold.actor)}"),
            ("Reason", hold.reason),
        ),
        run=lambda d: services.release_hold(
            actor=request.user,
            hold_id=hold.pk,
            expected_revision=d["expected_revision"],
            occurred_at=d["occurred_at"],
            reason=d["reason"],
        ),
        success=f"Released a hold on {specimen.accession}.",
    )


@staff_role_required(*OPERATE)
@ACTION_METHODS
def thaw_record(request, pk):
    specimen = _specimen(pk)
    return _action(
        request,
        specimen,
        TimedReasonForm,
        title="Record observed thaw",
        submit="Record observed thaw",
        blocked=_away(specimen),
        occupancy=False,
        notes=(
            "Record only a thaw episode that was observed and not already recorded at a "
            "check-in. Leaving storage is not a thaw.",
        ),
        run=lambda d: services.record_thaw(
            actor=request.user,
            specimen_id=specimen.pk,
            expected_revision=d["expected_revision"],
            occurred_at=d["occurred_at"],
            reason=d["reason"],
        ),
        success=f"Recorded an observed thaw of {specimen.accession}.",
    )


def _root(event):
    while event.corrects_id:
        event = event.corrects
    return event


@staff_role_required(*MANAGE)
@ACTION_METHODS
def thaw_correct(request, pk):
    event = get_object_or_404(ThawEvent.objects.select_related("actor"), pk=pk)
    specimen = _specimen(event.specimen_id)
    root = _root(event)
    baseline = root.kind == ThawEvent.Kind.BASELINE
    external = root.kind == ThawEvent.Kind.EXTERNAL
    current = event.count
    return _action(
        request,
        specimen,
        ThawCorrectForm,
        title="Correct thaw record",
        submit="Correct thaw record",
        blocked=ThawEvent.objects.filter(corrects=event).exists()
        and "This record was already corrected. Correct the latest version.",
        occupancy=False,
        form_kwargs={"baseline": baseline, "current": current, "external": external},
        facts=(
            ("Record", root.get_kind_display()),
            ("Count", "Unknown" if current is None else str(current)),
            ("Recorded", f"{_when(event.occurred_at)} by {_who(event.actor)}"),
            ("Reason", event.reason),
        ),
        notes=("The correction is added to the history; the earlier record stays.",),
        run=lambda d: services.correct_thaw(
            actor=request.user,
            event_id=event.pk,
            expected_revision=d["expected_revision"],
            count=d["count"],
            reason=d["reason"],
        ),
        success=f"Corrected a thaw record of {specimen.accession}.",
    )


@staff_role_required(*MANAGE)
@ACTION_METHODS
def custody_correct(request, pk):
    event = get_object_or_404(
        CustodyEvent.objects.select_related("from_custodian", "to_custodian", "move_event"),
        pk=pk,
    )
    specimen = _specimen(event.specimen_id)
    root_kind = _root(event).kind
    location = ""
    if event.move_event_id:
        move = event.move_event
        location = move.to_path.get("display") or move.from_path.get("display") or ""
    form_holder = {}

    def run(data):
        values = form_holder["form"].changes()
        if not values:
            raise ValidationError("Nothing to correct: the values are unchanged.")
        return services.correct_custody(
            actor=request.user,
            event_id=event.pk,
            expected_revision=data["expected_revision"],
            values=values,
            reason=data["reason"],
        )

    class BoundCustodyForm(CustodyCorrectForm):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, event=event, root_kind=root_kind, **kwargs)
            form_holder["form"] = self

    return _action(
        request,
        specimen,
        BoundCustodyForm,
        title="Correct custody event",
        submit="Correct custody event",
        blocked=CustodyEvent.objects.filter(corrects=event).exists()
        and "This event was already corrected. Correct the latest version.",
        occupancy=False,
        facts=(
            ("Event", event.get_kind_display()),
            ("Occurred", _when(event.occurred_at)),
            *((("Location", location),) if location else ()),
        ),
        notes=(
            "Only the destination and the people can be corrected. Times, kinds and locations "
            "cannot: a wrong movement is corrected with Relocate specimen.",
            *_custody_limits(root_kind),
        ),
        run=run,
        success=f"Corrected a custody event of {specimen.accession}.",
    )


def _custody_limits(kind):
    if kind == CustodyEvent.Kind.DISPATCH:
        return (
            "A dispatch has no staff recipient: the material left the repository. Correct who "
            "handed it over or where it was sent.",
        )
    if kind == CustodyEvent.Kind.RETURN:
        return (
            "A return comes from outside the repository, so it has no staff member handing it "
            "over. Correct who received it or where it came back from.",
        )
    return ()


@staff_role_required(*MANAGE)
@ACTION_METHODS
def correct(request, pk):
    specimen = _specimen(pk)
    form_holder = {}

    class BoundCorrectForm(CorrectForm):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            form_holder["form"] = self

    def run(data):
        names = form_holder["form"].changes()
        if not names:
            raise ValidationError("Nothing to correct: the values are unchanged.")
        values = services.CorrectionValues(
            fields=frozenset(names),
            **{
                (f"{n}_id" if not n.endswith("_at") else n): (
                    getattr(data[n], "pk", None) if not n.endswith("_at") else data[n]
                )
                for n in names
            },
        )
        return services.correct_specimen(
            actor=request.user,
            specimen_id=specimen.pk,
            expected_revision=data["expected_revision"],
            values=values,
            reason=data["reason"],
        )

    notes = ["Only the fields you change are corrected. The earlier values stay in the history."]
    if specimen.is_disposed:
        notes.insert(
            0,
            f"Specimen {specimen.accession} was disposed, so its record is not changed: "
            "the correction is kept as an annotation in its history.",
        )
    return _action(
        request,
        specimen,
        BoundCorrectForm,
        title="Correct specimen",
        submit="Correct specimen",
        occupancy=False,
        notes=notes,
        run=run,
        success=(
            f"Annotated {specimen.accession}."
            if specimen.is_disposed
            else f"Corrected {specimen.accession}."
        ),
    )


# --- disposal -------------------------------------------------------------------------------


def _open_request(specimen, occupancy):
    for item in DisposalRequest.objects.filter(specimen=specimen).select_related(
        "decision", "execution"
    ):
        if services.request_state(item, specimen, occupancy) in ("pending", "approved"):
            return item
    return None


@staff_role_required(*OPERATE)
@ACTION_METHODS
def disposal_request(request, pk):
    specimen = _specimen(pk)
    occupancy = _occupancy(specimen)
    blocked = _first(
        _away(specimen),
        _disposal_hold(specimen),
        _open_request(specimen, occupancy) is not None
        and "This specimen already has an open disposal request. Decide or cancel it first.",
    )
    return _action(
        request,
        specimen,
        DisposalRequestForm,
        title="Request disposal",
        submit="Request disposal",
        blocked=blocked,
        occupancy=False,
        notes=(
            "Another person with disposal approval must approve the request before the "
            "disposal can be recorded.",
        ),
        run=lambda d: services.request_disposal(
            actor=request.user,
            specimen_id=specimen.pk,
            expected_revision=d["expected_revision"],
            method=d["method"],
            destination=d["destination"],
            reason=d["reason"],
        ),
        success=f"Requested disposal of {specimen.accession}.",
    )


def _disposal(pk):
    return get_object_or_404(
        DisposalRequest.objects.select_related(
            "requested_by", "decision__actor", "execution", "specimen"
        ),
        pk=pk,
    )


def _disposal_crumbs(item, title):
    return [
        ("Disposal requests", url("specimens:disposal-list")),
        (f"Disposal request for {item.specimen.accession}", _disposal_url(item)),
        (title, None),
    ]


def _disposal_url(item):
    return url("specimens:disposal-detail", args=[item.pk])


def _request_facts(item):
    facts = [
        ("Method", item.method),
        ("Destination", item.destination),
        ("Requested", f"{_when(item.recorded_at)} by {_who(item.requested_by)}"),
        ("Reason", item.reason),
    ]
    decision = getattr(item, "decision", None)
    if decision is not None:
        facts.append(
            (
                decision.get_decision_display(),
                f"{_when(decision.recorded_at)} by {_who(decision.actor)}",
            )
        )
    return facts


REQUESTER_SENTENCE = "You requested this. Another person with disposal approval must approve it."
STATE_REASONS = {
    "approved": "This request was already approved. Record the disposal instead.",
    "stale": "The specimen changed after this request was made, so the request is out of "
    "date. Ask for a new disposal request.",
    "rejected": "This request was already rejected.",
    "cancelled": "This request was already cancelled.",
    "executed": "This disposal was already recorded.",
}


@staff_role_required(*OPERATE)
@ACTION_METHODS
def disposal_decide(request, pk):
    item = _disposal(pk)
    specimen = _specimen(item.specimen_id)
    occupancy = _occupancy(specimen)
    user = request.user
    is_requester = item.requested_by_id == user.pk
    approver = can_approve_disposal(user)
    options = []
    if approver and not is_requester:
        options += ["approve", "reject"]
    if is_requester or has_role(user, *MANAGE):
        options.append("cancel")
    fixed = request.GET.get("decision") or request.POST.get("decision")
    fixed = fixed if fixed in DECISION_LABELS else None
    if fixed in ("approve", "reject") and not approver and not is_requester:
        return _forbidden()
    if fixed == "cancel" and "cancel" not in options:
        return _forbidden()
    if not options and not is_requester:
        return _forbidden()
    state = services.request_state(item, specimen, occupancy)
    decided = getattr(item, "decision", None) is not None
    # Approval needs the material here and no disposal or all-use hold; reject/cancel stay open.
    no_approval = _first(_away(specimen), _disposal_hold(specimen))
    if no_approval and fixed is None and "approve" in options and len(options) > 1:
        options.remove("approve")
    if fixed in ("approve", "reject") and is_requester:
        blocked = REQUESTER_SENTENCE
    elif decided:
        blocked = STATE_REASONS["approved" if state == "stale" else state]
    elif state == "stale" and fixed == "approve":
        blocked = STATE_REASONS[
            "stale"
        ]  # an out-of-date request can still be rejected or cancelled
    elif fixed == "approve" and no_approval:
        blocked = no_approval
    else:
        blocked = ""
    title = DECISION_LABELS[fixed] if fixed else "Decide disposal request"
    submit = title if fixed else "Record decision"
    return _action(
        request,
        specimen,
        DecideForm,
        title=title,
        submit=submit,
        blocked=blocked,
        occupancy=False,
        destructive=fixed in ("reject", "cancel"),
        form_kwargs={"options": options or [fixed], "fixed": fixed},
        back_url=_disposal_url(item),
        crumbs=_disposal_crumbs(item, title),
        done_url=_disposal_url(item),
        facts=_request_facts(item),
        run=lambda d: services.decide_disposal(
            actor=user,
            request_id=item.pk,
            expected_revision=d["expected_revision"],
            decision=d["decision"],
            reason=d["reason"],
        ),
        success=lambda row: f"{row.get_decision_display()} the disposal of {specimen.accession}.",
    )


def _forbidden():
    raise PermissionDenied("Your role does not permit this decision.")


@staff_role_required(*OPERATE)
@ACTION_METHODS
def disposal_execute(request, pk):
    item = _disposal(pk)
    specimen = _specimen(item.specimen_id)
    occupancy = _occupancy(specimen)
    state = services.request_state(item, specimen, occupancy)
    blocked = ""
    if state != "approved":
        blocked = {
            "pending": "This request is still awaiting approval by another person with "
            "disposal approval.",
        }.get(state) or STATE_REASONS.get(state, "")
        if state == "stale" and getattr(item, "decision", None) is not None:
            blocked = (
                "The specimen changed after the approval, so the approval is out of date. "
                "Ask for a new disposal request."
            )
    else:
        blocked = _first(_away(specimen), _disposal_hold(specimen))
    floor = occupancy.last_occurred_at
    decision = getattr(item, "decision", None)
    if decision is not None:
        floor = max(floor, decision.recorded_at)
    title = "Record disposal"
    return _action(
        request,
        specimen,
        TimedReasonForm,
        title=title,
        submit=title,
        blocked=blocked,
        form_kwargs={"floor": floor},
        back_url=_disposal_url(item),
        crumbs=_disposal_crumbs(item, title),
        done_url=_disposal_url(item),
        facts=_request_facts(item),
        notes=(
            "Recording the disposal frees the position, debits the remaining amount and keeps "
            "the full history. The approved method and destination cannot be changed here.",
        ),
        run=lambda d: services.execute_disposal(
            actor=request.user,
            request_id=item.pk,
            occurred_at=d["occurred_at"],
            reason=d["reason"],
            **_revisions(d),
        ),
        success=f"Recorded disposal of {specimen.accession}.",
    )


# --- derivation entry grid ------------------------------------------------------------------

PRESET_ROWS = 2


def _row_initial(specimen):
    return {
        "specimen_type": str(specimen.specimen_type_id),
        "container_type": str(specimen.container_type_id),
        "unit": str(specimen.unit_id),
    }


def _posted_rows(data, prefix="children"):
    try:
        total = int(data.get(f"{prefix}-TOTAL_FORMS", 0))
    except ValueError:
        total = 0
    total = max(0, min(total, services.MAX_CHILDREN))
    return [
        {name: data.get(f"{prefix}-{i}-{name}", "") for name in CHILD_FIELDS} for i in range(total)
    ]


def _apply_derive_errors(form, formset, exc):
    """children-<i> errors go under row i; an occupied position goes on that row's position."""
    if not hasattr(exc, "error_dict"):
        apply_errors(form, exc)
        return
    rest = {}
    for name, errors in exc.error_dict.items():
        index = name.removeprefix("children-")
        if name.startswith("children-") and index.isdigit() and int(index) < len(formset.forms):
            for error in errors:
                formset.forms[int(index)].add_error(None, error)
        elif name == "position_id":
            for error in errors:
                row = _row_for_position(formset, " ".join(error.messages))
                if row is not None:
                    row.add_error("position_label", error)
                else:
                    form.add_error(None, error)
        else:
            rest[name] = errors
    if rest:
        apply_errors(form, ValidationError(rest))


def _row_for_position(formset, message):
    for row in formset.forms:
        position = row.cleaned_data.get("position") if hasattr(row, "cleaned_data") else None
        if position is not None and f"{position.box.code} {position.label}" in message:
            return row
    return None


@staff_role_required(*OPERATE)
@ACTION_METHODS
def derive(request, pk):
    specimen = _specimen(pk)
    occupancy = _occupancy(specimen)
    title = "Make aliquots or derivatives"
    blocked = _first(
        _away(specimen),
        specimen.status != Specimen.Status.AVAILABLE
        and f"Specimen {specimen.accession} has nothing left on hand.",
        _use_hold(specimen, "split or processed"),
        _not_in_hand(specimen, occupancy, "split"),
    )
    choices = box_choices()
    catalogues = {
        "specimen_type": catalogue(SpecimenType),
        "container_type": catalogue(SpecimenContainerType),
        "unit": catalogue(QuantityUnit),
    }
    row_kwargs = {"actor": request.user, "choices": choices, "catalogues": catalogues}
    post = request.POST if request.method == "POST" else None
    adding = post is not None and "add_row" in post
    removing = post.get("remove_row") if post is not None else None
    if post is not None and (adding or removing is not None):
        # Add or remove a row without JavaScript: re-render the grid, call nothing.
        rows = _posted_rows(post)
        if adding and len(rows) < services.MAX_CHILDREN:
            rows.append(_row_initial(specimen))
        if removing is not None and removing.isdigit() and len(rows) > 1:
            index = int(removing)
            if index < len(rows):
                rows.pop(index)
        top = {k: v for k, v in post.items() if not k.startswith("children-")}
        top.pop("csrfmiddlewaretoken", None)
        form = DeriveForm(actor=request.user, specimen=specimen, occupancy=occupancy, initial=top)
        formset = ChildFormSet(prefix="children", initial=rows, form_kwargs=row_kwargs)
        post = None
    else:
        form = DeriveForm(post, actor=request.user, specimen=specimen, occupancy=occupancy)
        formset = ChildFormSet(
            post,
            prefix="children",
            initial=None if post is not None else [_row_initial(specimen)] * PRESET_ROWS,
            form_kwargs=row_kwargs,
        )
    if blocked:
        form.disable()
        for row in formset.forms:
            for field in row.fields.values():
                field.disabled = True
    elif post is not None and form.is_valid() & formset.is_valid():
        data = form.cleaned_data
        children = tuple(
            services.ChildSpec(
                specimen_type_id=row["specimen_type"].pk,
                container_type_id=row["container_type"].pk,
                quantity=row["quantity"],
                supplied_text=row["quantity"],
                unit_id=row["unit"].pk,
                position_id=row["position"].pk if row.get("position") else None,
            )
            for row in formset.cleaned_data
        )
        try:
            created = services.derive_specimens(
                actor=request.user,
                parent_id=specimen.pk,
                kind=data["kind"],
                consumed=data["consumed"],
                children=children,
                procedure=data["procedure"],
                occurred_at=data["occurred_at"],
                reason=data["reason"],
                **_revisions(data),
            )
        except ValidationError as exc:
            _apply_derive_errors(form, formset, exc)
        else:
            made = ", ".join(child.accession for child in created)
            messages.success(request, f"Made {made} from {specimen.accession}.")
            return redirect(_detail(specimen))
    return _page(
        request,
        form,
        title=title,
        submit="Record split",
        back_url=_detail(specimen),
        crumbs=_crumbs(specimen, title),
        specimen=specimen,
        occupancy=occupancy,
        blocked=blocked,
        template=DERIVE_TEMPLATE,
        extra={
            "formset": formset,
            "suggestions": suggestion_text(request.user),
            "max_rows": services.MAX_CHILDREN,
            "aliquot": Derivation.Kind.ALIQUOT,
        },
    )
