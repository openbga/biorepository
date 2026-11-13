"""Specimen action forms and the derivation entry grid. Transport and input shape only.

specimens.services repeats every rule under lock; these forms only collect values, resolve a
box and position label to a Position, and show service errors under the field they concern.
"""

from django import forms
from django.db.models import Count, Q
from django.utils import timezone

from accounts.models import User
from accounts.roles import ROLES, has_role
from storage.models import Box, MoveEvent, Position
from storage.queries import free_positions

from .models import (
    NO_IDENTIFYING_TEXT,
    Collection,
    CustodyEvent,
    Derivation,
    Hold,
    Participant,
    QuantityUnit,
    SpecimenContainerType,
    SpecimenType,
)
from .services import MAX_BASELINE_THAWS, MAX_CHILDREN, TEXT_LIMIT

# Seconds are kept so the default "now" is never earlier than an event recorded a moment ago.
DATETIME = forms.DateTimeInput(
    attrs={"type": "datetime-local", "step": "1"}, format="%Y-%m-%dT%H:%M:%S"
)
WHEN_HELP = (
    "Repository local time; not in the future and not before the specimen's last recorded event."
)
SUGGESTIONS = 12

# Service error keys that differ from the form field showing them.
FIELD_ALIASES = {
    "position_id": "position_label",
    "recipient_id": "recipient",
    "from_custodian_id": "from_custodian",
    "to_custodian_id": "to_custodian",
}


def apply_errors(form, error):
    """Show a service ValidationError on the form, under the field it names where it can."""
    if hasattr(error, "error_dict"):
        for name, errors in error.error_dict.items():
            target = FIELD_ALIASES.get(name, name)
            for item in errors:
                form.add_error(target if target in form.fields else None, item)
    else:
        for item in error.error_list:
            form.add_error(None, item)


def now_to_the_second():
    return timezone.localtime().replace(microsecond=0)


def reason_field(label="Reason", help_text="Kept in the history with the change.", initial=None):
    return forms.CharField(
        label=label,
        max_length=TEXT_LIMIT,
        initial=initial,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=f"{help_text} {NO_IDENTIFYING_TEXT}",
    )


def when_field(label="When it happened", help_text=WHEN_HELP, required=True):
    return forms.DateTimeField(label=label, widget=DATETIME, help_text=help_text, required=required)


def amount_field(label, help_text=""):
    # Text, not a float field: the service parses it as an exact decimal and keeps the text.
    return forms.CharField(
        label=label,
        max_length=40,
        help_text=help_text,
        widget=forms.TextInput(attrs={"inputmode": "decimal", "autocomplete": "off"}),
    )


def staff_queryset(*keep):
    """Active users in any role group, plus any already-recorded people being corrected."""
    query = Q(is_active=True, groups__name__in=ROLES)
    keep = [pk for pk in keep if pk]
    if keep:
        query |= Q(pk__in=keep)
    return User.objects.filter(query).distinct().order_by("first_name", "last_name", "username")


class StaffChoiceField(forms.ModelChoiceField):
    def __init__(self, **kwargs):
        kwargs.setdefault("queryset", staff_queryset())
        super().__init__(**kwargs)

    def label_from_instance(self, user):
        return user.get_full_name() or user.get_username()


def catalogue(model, current=None):
    """Active catalogue entries; an inactive current value stays selectable for corrections."""
    query = Q(is_active=True)
    if current:
        query |= Q(pk=current)
    return model.objects.filter(query).order_by("code")


def box_choices():
    """(pk, "BOX-001 · VP-01 / T-01 / T-01-S1 (5 free)") for every active box, in path order."""
    boxes = (
        Box.objects.filter(is_active=True, node__is_active=True, node__unit__is_active=True)
        .select_related("node__unit")
        .annotate(
            free=Count(
                "positions",
                filter=Q(positions__blocked=False, positions__occupancy__isnull=True),
            )
        )
        .order_by("node__unit__code", "node__path", "code")
    )
    return [
        (
            str(box.pk),
            f"{box.code} · {box.node.unit.code} / {box.node.path.replace('/', ' / ')} "
            f"({box.free} free)",
        )
        for box in boxes
    ]


def suggestion_text(actor, box_id=None):
    """Up to 12 free positions, grouped by box, as help text. Nothing is reserved."""
    try:
        positions = free_positions(actor=actor, box_id=box_id or None, limit=SUGGESTIONS)
    except Exception:  # an unknown box id from a bad request: no suggestions
        positions = ()
    if not positions:
        return "No free positions" + (" in this box." if box_id else ".")
    grouped = {}
    for position in positions:
        grouped.setdefault(position.box.code, []).append(position.label)
    listing = "; ".join(f"{code} {', '.join(labels)}" for code, labels in grouped.items())
    return f"Free now: {listing}. The position is checked again when you save."


def resolve_position(form, data):
    """Turn the chosen box and typed label into data["position"] (None when both are blank)."""
    box_id = data.get("box")
    label = (data.get("position_label") or "").strip()
    data["position"] = None
    if not box_id and not label:
        return
    if not box_id:
        form.add_error("box", "Choose the box for this position.")
        return
    if not label:
        form.add_error("position_label", "Enter the position label, for example A1.")
        return
    box = Box.objects.filter(pk=box_id).first()
    if box is None:
        form.add_error("box", "Choose an existing box.")
        return
    position = Position.objects.filter(box=box, label__iexact=label).first()
    if position is None:
        positions = Position.objects.filter(box=box).order_by("ordinal")
        first, last = positions.first(), positions.last()
        span = f" Its labels run from {first.label} to {last.label}." if first else ""
        form.add_error("position_label", f"{box.code} has no position {label}.{span}")
        return
    data["position"] = position


def add_position_fields(form, *, actor, choices, required, help_text=True):
    form.fields["box"] = forms.ChoiceField(
        label="Box",
        choices=[("", "Choose a box…"), *choices],
        required=required,
        help_text="Active boxes with their storage unit, rack and free positions.",
    )
    form.fields["box"].col = "col-md-7"
    box_id = form.data.get(form.add_prefix("box")) if form.is_bound else None
    form.fields["position_label"] = forms.CharField(
        label="Position",
        max_length=20,
        required=required,
        help_text=suggestion_text(actor, box_id) if help_text else "For example A1.",
        widget=forms.TextInput(attrs={"autocomplete": "off", "spellcheck": "false"}),
    )
    form.fields["position_label"].col = "col-md-5"


# --- base -----------------------------------------------------------------------------------


class ActionForm(forms.Form):
    """Hidden revisions captured when the form opened, event time defaulting to now, groups.

    `groups` is ((legend, [name | (name, name)]), ...); a tuple is one row of short fields.
    """

    groups = ()
    with_position = None  # None, "optional" or "required"

    def __init__(
        self, *args, actor, specimen=None, occupancy=None, floor=None, choices=None, **kwargs
    ):
        super().__init__(*args, **kwargs)
        self.actor = actor
        self.specimen = specimen
        if self.with_position:
            add_position_fields(
                self,
                actor=actor,
                choices=box_choices() if choices is None else choices,
                required=self.with_position == "required",
            )
        if specimen is not None:
            self.fields["expected_revision"] = forms.IntegerField(widget=forms.HiddenInput)
            self.initial.setdefault("expected_revision", specimen.revision)
        if occupancy is not None:
            self.fields["expected_occupancy_revision"] = forms.IntegerField(
                widget=forms.HiddenInput
            )
            self.initial.setdefault("expected_occupancy_revision", occupancy.revision)
            floor = floor or occupancy.last_occurred_at
        self.floor = floor
        if "occurred_at" in self.fields:
            self.initial.setdefault("occurred_at", now_to_the_second())

    def grouped(self):
        sections = []
        for legend, names in self.groups:
            rows = []
            for item in names:
                names_in_row = item if isinstance(item, tuple) else (item,)
                row = [self[n] for n in names_in_row if n in self.fields]
                row = [bound for bound in row if not bound.is_hidden]
                if row:
                    rows.append(row)
            if rows:
                sections.append((legend, rows))
        return sections

    @property
    def has_times(self):
        return any(isinstance(f, forms.DateTimeField) for f in self.fields.values())

    def clean(self):
        data = super().clean()
        if self.with_position:
            resolve_position(self, data)
        # Times are entered to the second. A time in the same second as the latest recorded
        # event means that event's own time, so "now" right after an event stays in order.
        when, floor = data.get("occurred_at"), self.floor
        if when and floor and floor.replace(microsecond=0) <= when < floor:
            data["occurred_at"] = floor
        return data

    def disable(self):
        for field in self.fields.values():
            field.disabled = True


POSITION_ROW = ("box", "position_label")


# --- registration ---------------------------------------------------------------------------


class RegisterForm(ActionForm):
    with_position = "optional"
    specimen_type = forms.ModelChoiceField(label="Specimen type", queryset=SpecimenType.objects)
    container_type = forms.ModelChoiceField(
        label="Specimen container", queryset=SpecimenContainerType.objects
    )
    quantity = amount_field("Amount", "Exact amount as measured, for example 1.5.")
    unit = forms.ModelChoiceField(label="Unit", queryset=QuantityUnit.objects)
    participant = forms.ModelChoiceField(
        label="Participant",
        queryset=Participant.objects,
        required=False,
        help_text=f"Coded participant identifier only. {NO_IDENTIFYING_TEXT}",
    )
    collection = forms.ModelChoiceField(
        label="Collection",
        queryset=Collection.objects,
        required=False,
        help_text="Optional grouping of specimens.",
    )
    collected_at = when_field("Collected", "Repository local time; not in the future.")
    processing_started_at = when_field(
        "Processing started", "Optional; not before collection.", required=False
    )
    processing_completed_at = when_field(
        "Processing completed", "Optional; needs the start time.", required=False
    )
    occurred_at = when_field(
        "Registered", "Repository local time; not before collection or the end of processing."
    )
    baseline_thaws = forms.IntegerField(
        label="Prior thaws",
        min_value=0,
        max_value=MAX_BASELINE_THAWS,
        required=False,
        help_text="Thaws before registration, as stated by the sender.",
    )
    baseline_unknown = forms.BooleanField(
        label="Unknown",
        required=False,
        initial=True,
        help_text="Tick when the thaw history before registration is not known.",
    )
    custodian = StaffChoiceField(label="Custodian", help_text="Who has the specimen now.")
    reason = reason_field(initial="Received for storage")
    groups = (
        ("Specimen", ["specimen_type", "container_type", ("quantity", "unit")]),
        ("Origin", ["participant", "collection"]),
        (
            "Times",
            [
                "collected_at",
                ("processing_started_at", "processing_completed_at"),
                "occurred_at",
            ],
        ),
        ("Thaw history", [("baseline_thaws", "baseline_unknown")]),
        ("Custody and storage", ["custodian", POSITION_ROW]),
        ("Reason", ["reason"]),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, model in (
            ("specimen_type", SpecimenType),
            ("container_type", SpecimenContainerType),
            ("unit", QuantityUnit),
            ("participant", Participant),
            ("collection", Collection),
        ):
            self.fields[name].queryset = catalogue(model)
        self.fields["custodian"].queryset = staff_queryset()
        self.initial.setdefault("custodian", self.actor.pk)
        self.fields["position_label"].help_text = (
            "Optional: store it now. " + self.fields["position_label"].help_text
        )

    def clean(self):
        data = super().clean()
        if data.get("baseline_unknown"):
            if data.get("baseline_thaws") is not None:
                self.add_error(
                    "baseline_thaws", "Clear the number, or untick Unknown to give a count."
                )
            data["baseline_thaws"] = None
        elif data.get("baseline_thaws") is None and "baseline_thaws" not in self.errors:
            self.add_error("baseline_thaws", "Enter the number of prior thaws, or tick Unknown.")
        return data


# --- placement and custody ------------------------------------------------------------------


class PlaceForm(ActionForm):
    with_position = "required"
    occurred_at = when_field("Placed at")
    reason = reason_field(initial="Stored after processing")
    groups = (("Position", [POSITION_ROW]), ("Record", ["occurred_at", "reason"]))


class RelocateForm(ActionForm):
    with_position = "required"
    corrects = forms.ChoiceField(
        label="This corrects a wrong movement",
        required=False,
        help_text="Optional. Choose the earlier movement of this specimen that was recorded "
        "wrongly; this move is then recorded as its correction.",
    )
    occurred_at = when_field("Moved at")
    reason = reason_field()
    groups = (
        ("New position", [POSITION_ROW]),
        ("Record", ["corrects", "occurred_at", "reason"]),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not has_role(self.actor, "admin", "manager"):
            # Correcting movement history is for repository managers; others never see it.
            del self.fields["corrects"]
            self.groups = (
                ("New position", [POSITION_ROW]),
                ("Record", ["occurred_at", "reason"]),
            )
            return
        events = (
            MoveEvent.objects.filter(
                members__occupancy_id=self.specimen.occupancy_id,
                node__isnull=True,
                box__isnull=True,
                corrected_by__isnull=True,
            )
            .distinct()
            .order_by("-occurred_at", "-recorded_at")
        )
        choices = []
        for event in events:
            when = timezone.localtime(event.occurred_at).strftime("%Y-%m-%d %H:%M")
            source = event.from_path.get("display") or "not stored"
            target = event.to_path.get("display") or "not stored"
            choices.append(
                (str(event.pk), f"{when} · {event.get_kind_display()}: {source} → {target}")
            )
        self.fields["corrects"].choices = [("", "No, a new movement"), *choices]


class CheckOutForm(ActionForm):
    recipient = StaffChoiceField(label="Taken by", help_text="Who takes custody of it.")
    destination = forms.CharField(
        label="Destination", max_length=160, help_text="Where it is going, e.g. Processing bench 2."
    )
    occurred_at = when_field("Checked out at")
    reason = reason_field()
    groups = (("Check-out", ["recipient", "destination", "occurred_at", "reason"]),)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["recipient"].queryset = staff_queryset()
        self.initial.setdefault("recipient", self.actor.pk)


THAW_CHOICES = (
    ("yes", "Yes, a thaw was observed"),
    ("no", "No thaw was observed"),
    ("unknown", "Not known"),
)
THAW_VALUES = {"yes": True, "no": False, "unknown": None}


class CheckInForm(ActionForm):
    with_position = "required"
    recipient = StaffChoiceField(label="Custodian after check-in")
    thaw_observed = forms.ChoiceField(
        label="Was a thaw observed while it was out?",
        choices=THAW_CHOICES,
        widget=forms.RadioSelect,
        help_text="Leaving storage is not a thaw. Record only what was observed.",
    )
    occurred_at = when_field("Checked in at")
    reason = reason_field()
    groups = (
        ("Position", [POSITION_ROW]),
        ("Check-in", ["recipient", "thaw_observed", "occurred_at", "reason"]),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["recipient"].queryset = staff_queryset()
        self.initial.setdefault("recipient", self.actor.pk)

    def clean_thaw_observed(self):
        return THAW_VALUES[self.cleaned_data["thaw_observed"]]


class TransferForm(ActionForm):
    recipient = StaffChoiceField(label="Hand over to")
    destination = forms.CharField(
        label="Destination", max_length=160, required=False, help_text="Optional."
    )
    occurred_at = when_field("Handed over at")
    reason = reason_field()
    groups = (("Custody transfer", ["recipient", "destination", "occurred_at", "reason"]),)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["recipient"].queryset = staff_queryset()


# --- quantity -------------------------------------------------------------------------------


class ConsumeForm(ActionForm):
    quantity = amount_field(
        "Amount used", "Exact amount taken from the specimen; the text you enter is kept."
    )
    occurred_at = when_field("Used at")
    reason = reason_field()
    groups = (("Consumption", ["quantity", "occurred_at", "reason"]),)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["quantity"].suffix = self.specimen.unit.code


class AdjustForm(ActionForm):
    delta = amount_field(
        "Change in amount",
        "Signed: a positive amount adds, a negative amount removes, for example -0.25.",
    )
    occurred_at = when_field("Measured at")
    reason = reason_field()
    groups = (("Adjustment", ["delta", "occurred_at", "reason"]),)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["delta"].suffix = self.specimen.unit.code


class ReasonForm(ActionForm):
    reason = reason_field()
    groups = (("", ["reason"]),)


class TimedReasonForm(ActionForm):
    occurred_at = when_field()
    reason = reason_field()
    groups = (("", ["occurred_at", "reason"]),)


# --- holds and thaw -------------------------------------------------------------------------


class HoldForm(ActionForm):
    scope = forms.ChoiceField(
        label="Kind of hold",
        choices=(
            (Hold.Scope.DISPOSAL, "Disposal hold — blocks disposal only"),
            (
                Hold.Scope.USE,
                "Use hold — blocks check-out, splitting, use, hand-over and dispatch; not disposal",
            ),
            (Hold.Scope.ALL_USE, "All-use hold — blocks both use and disposal"),
        ),
        widget=forms.RadioSelect,
        help_text="A disposal hold keeps the material from being disposed of; a use hold keeps it "
        "unused but still lets it be disposed of; an all-use hold does both. A repository "
        "manager releases a hold.",
    )
    occurred_at = when_field("Placed at")
    reason = reason_field()
    groups = (("Hold", ["scope", "occurred_at", "reason"]),)


class ThawCorrectForm(ActionForm):
    """An observed thaw counts 0 or 1; the baseline is a whole number or unknown."""

    count = forms.IntegerField(label="Thaws", min_value=0, max_value=MAX_BASELINE_THAWS)
    unknown = forms.BooleanField(label="Unknown", required=False)
    reason = reason_field()
    groups = (("Correction", [("count", "unknown"), "reason"]),)

    def __init__(self, *args, baseline, current, external=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.baseline = baseline or external
        if self.baseline:
            self.fields["count"].required = False
            self.fields["count"].label = (
                "Thaws while away" if external else "Thaws before registration"
            )
            self.fields["count"].help_text = (
                "As reported by the recipient: a whole number, or tick Unknown."
                if external
                else "A whole number, or tick Unknown."
            )
            self.initial.setdefault("count", current)
            self.initial.setdefault("unknown", current is None)
        else:
            self.fields.pop("unknown")
            self.fields["count"] = forms.TypedChoiceField(
                label="Did this thaw happen?",
                choices=(("1", "Yes, it happened (counts 1)"), ("0", "No, it did not (counts 0)")),
                coerce=int,
                widget=forms.RadioSelect,
            )
            self.initial.setdefault("count", "0" if current else "1")

    def clean(self):
        data = super().clean()
        if self.baseline:
            if data.get("unknown"):
                if data.get("count") is not None:
                    self.add_error("count", "Clear the number, or untick Unknown.")
                data["count"] = None
            elif data.get("count") is None and "count" not in self.errors:
                self.add_error("count", "Enter the number of thaws, or tick Unknown.")
        return data


class CustodyCorrectForm(ActionForm):
    """A dispatch has no staff recipient and a return no staff sender: those fields are left out."""

    destination = forms.CharField(label="Destination", max_length=160, required=False)
    from_custodian = StaffChoiceField(label="From", required=False)
    to_custodian = StaffChoiceField(label="To")
    reason = reason_field()
    groups = (("Correction", ["destination", ("from_custodian", "to_custodian"), "reason"]),)

    def __init__(self, *args, event, root_kind=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.event = event
        queryset = staff_queryset(event.from_custodian_id, event.to_custodian_id)
        self.fields["from_custodian"].queryset = queryset
        self.fields["to_custodian"].queryset = queryset
        self.initial.setdefault("destination", event.destination)
        self.initial.setdefault("from_custodian", event.from_custodian_id)
        self.initial.setdefault("to_custodian", event.to_custodian_id)
        if root_kind == CustodyEvent.Kind.DISPATCH:
            del self.fields["to_custodian"]
            self.fields["destination"].help_text = "Where it was sent."
        elif root_kind == CustodyEvent.Kind.RETURN:
            del self.fields["from_custodian"]
            self.fields["destination"].label = "Returned from"
            self.fields["destination"].help_text = "Where it came back from."

    def changes(self):
        """Only the values that differ from the event, keyed as correct_custody expects."""
        data, event = self.cleaned_data, self.event
        values = {}
        if (data.get("destination") or "") != (event.destination or ""):
            values["destination"] = data.get("destination") or ""
        for name in ("from_custodian", "to_custodian"):
            user = data.get(name)
            if name in self.fields and user is not None and user.pk != getattr(event, f"{name}_id"):
                values[f"{name}_id"] = user.pk
        return values


CORRECT_LABELS = {
    "specimen_type": "Specimen type",
    "container_type": "Specimen container",
    "participant": "Participant",
    "collection": "Collection",
    "collected_at": "Collected",
    "processing_started_at": "Processing started",
    "processing_completed_at": "Processing completed",
}
INHERITED = ("participant", "collection", "collected_at")


class CorrectForm(ActionForm):
    specimen_type = forms.ModelChoiceField(label="Specimen type", queryset=SpecimenType.objects)
    container_type = forms.ModelChoiceField(
        label="Specimen container", queryset=SpecimenContainerType.objects
    )
    participant = forms.ModelChoiceField(
        label="Participant",
        queryset=Participant.objects,
        required=False,
        help_text=f"Coded participant identifier only. {NO_IDENTIFYING_TEXT}",
    )
    collection = forms.ModelChoiceField(
        label="Collection", queryset=Collection.objects, required=False
    )
    collected_at = when_field("Collected", "Repository local time.")
    processing_started_at = when_field("Processing started", "Optional.", required=False)
    processing_completed_at = when_field("Processing completed", "Optional.", required=False)
    reason = reason_field(help_text="Kept in the history with the earlier and new values.")
    groups = (
        ("Specimen", ["specimen_type", "container_type"]),
        ("Origin", ["participant", "collection"]),
        ("Times", ["collected_at", ("processing_started_at", "processing_completed_at")]),
        ("Reason", ["reason"]),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        specimen = self.specimen
        for name, model in (
            ("specimen_type", SpecimenType),
            ("container_type", SpecimenContainerType),
            ("participant", Participant),
            ("collection", Collection),
        ):
            current = getattr(specimen, f"{name}_id")
            self.fields[name].queryset = catalogue(model, current)
            self.initial.setdefault(name, current)
        for name in ("collected_at", "processing_started_at", "processing_completed_at"):
            value = getattr(specimen, name)
            self.initial.setdefault(name, timezone.localtime(value) if value else None)
        if specimen.parent_id:
            for name in INHERITED:
                self.fields[name].disabled = True
                self.fields[
                    name
                ].help_text = "Comes from the parent specimen; correct the parent instead."

    def changes(self):
        """Names of the allow-listed fields whose submitted value differs from the record."""
        changed = []
        for name in CORRECT_LABELS:
            if name not in self.cleaned_data or self.fields[name].disabled:
                continue
            new = self.cleaned_data[name]
            old = getattr(self.specimen, name)
            if hasattr(old, "pk"):
                old = old.pk
            if hasattr(new, "pk"):
                new = new.pk
            if name.endswith("_at") and old is not None and new is not None:
                old = old.replace(microsecond=0)
                new = new.replace(microsecond=0)
            if old != new:
                changed.append(name)
        return changed


# --- disposal -------------------------------------------------------------------------------


class DisposalRequestForm(ActionForm):
    method = forms.CharField(
        label="Method", max_length=160, help_text="For example: autoclave, then clinical waste."
    )
    destination = forms.CharField(
        label="Destination", max_length=160, help_text="Where the material goes."
    )
    reason = reason_field(help_text="Why it is disposed. Kept with the request.")
    groups = (("Disposal", ["method", "destination", "reason"]),)


DECISION_LABELS = {
    "approve": "Approve disposal",
    "reject": "Reject disposal request",
    "cancel": "Cancel disposal request",
}


class DecideForm(ActionForm):
    decision = forms.ChoiceField(label="Decision", widget=forms.RadioSelect)
    reason = reason_field(help_text="Kept with the decision.")
    groups = (("Decision", ["decision", "reason"]),)

    def __init__(self, *args, options, fixed=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["decision"].choices = [(o, DECISION_LABELS[o]) for o in options]
        if fixed:
            self.fields["decision"].widget = forms.HiddenInput()
            self.initial["decision"] = fixed
            self.groups = (("Decision", ["reason"]),)


# --- derivation entry grid ------------------------------------------------------------------


class DeriveForm(ActionForm):
    kind = forms.ChoiceField(
        label="What are you making?",
        choices=(
            (Derivation.Kind.ALIQUOT, "Aliquots: same type and unit as the parent"),
            (Derivation.Kind.DERIVATIVE, "Derivatives: a different material"),
        ),
        widget=forms.RadioSelect,
        initial=Derivation.Kind.ALIQUOT,
    )
    consumed = amount_field(
        "Taken from the parent",
        "Aliquot amounts must add up to it. Record process loss separately as consumption.",
    )
    procedure = forms.CharField(
        label="Procedure", max_length=160, required=False, help_text="Optional."
    )
    occurred_at = when_field("Made at")
    reason = reason_field()
    groups = (
        ("Split", ["kind", "consumed", "procedure"]),
        ("Record", ["occurred_at", "reason"]),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["consumed"].suffix = self.specimen.unit.code


class ChildForm(forms.Form):
    """One row of the entry grid: a new aliquot or derivative and its optional position."""

    specimen_type = forms.ModelChoiceField(label="Specimen type", queryset=SpecimenType.objects)
    container_type = forms.ModelChoiceField(
        label="Specimen container", queryset=SpecimenContainerType.objects
    )
    quantity = amount_field("Amount")
    unit = forms.ModelChoiceField(label="Unit", queryset=QuantityUnit.objects)

    def __init__(self, *args, actor=None, choices=(), catalogues=None, **kwargs):
        super().__init__(*args, **kwargs)
        catalogues = catalogues or {}
        for name, model in (
            ("specimen_type", SpecimenType),
            ("container_type", SpecimenContainerType),
            ("unit", QuantityUnit),
        ):
            self.fields[name].queryset = catalogues.get(name, catalogue(model))
        add_position_fields(self, actor=actor, choices=choices, required=False, help_text=False)
        for name in ("box", "position_label"):
            self.fields[name].help_text = ""

    def clean(self):
        data = super().clean()
        resolve_position(self, data)
        return data


ChildFormSet = forms.formset_factory(
    ChildForm,
    extra=0,
    min_num=1,
    validate_min=True,
    max_num=MAX_CHILDREN,
    validate_max=True,
)
CHILD_FIELDS = ("specimen_type", "container_type", "quantity", "unit", "box", "position_label")
