"""Equipment forms. Transport and input shape only; equipment.services repeats every rule.

Temperatures, levels and amounts are DecimalFields (never floats); times are entered in the
repository's time zone to the second, as in the specimen forms.
"""

from django import forms
from django.core import signing
from django.core.validators import MaxLengthValidator
from django.db.models import Q
from django.utils import timezone

from accounts.models import User
from core.attachments import AttachmentField
from specimens.models import Hold
from specimens.services import TEXT_LIMIT as HOLD_REASON_LIMIT
from storage.models import StorageUnit

from .models import (
    AMOUNT,
    LEVEL,
    TEMPERATURE,
    AmountUnit,
    Equipment,
    Excursion,
    LevelUnit,
    LN2Entry,
    ServiceEvent,
)
from .services import EXCURSION_HOLD_LIMIT, READ_ROLES, TEXT_LIMIT

DATETIME = forms.DateTimeInput(
    attrs={"type": "datetime-local", "step": "1"}, format="%Y-%m-%dT%H:%M:%S"
)
DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")
NOT_IDENTIFYING = "Do not enter donor or participant names or other identifying details."

# Service error keys that differ from the form field showing them.
FIELD_ALIASES = {"equipment_id": "equipment", "temperature_c": "value"}


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


def local(value):
    return timezone.localtime(value) if value else None


def text_field(label, *, required=False, max_length=TEXT_LIMIT, rows=3, help_text=""):
    return forms.CharField(
        label=label,
        required=required,
        max_length=max_length,
        widget=forms.Textarea(attrs={"rows": rows}),
        help_text=help_text,
    )


def reason_field(label="Reason", help_text="Kept in the history with the change."):
    return text_field(label, required=True, help_text=f"{help_text} {NOT_IDENTIFYING}")


def when_field(label, help_text="Repository local time; not in the future.", required=True):
    return forms.DateTimeField(label=label, widget=DATETIME, help_text=help_text, required=required)


def decimal_field(label, spec, *, suffix="", required=False, help_text="", min_value=None):
    field = forms.DecimalField(
        label=label,
        required=required,
        max_digits=spec["max_digits"],
        decimal_places=spec["decimal_places"],
        min_value=min_value,
        help_text=help_text,
    )
    field.suffix = suffix
    return field


def temperature_field(label, **kwargs):
    return decimal_field(label, TEMPERATURE, suffix="°C", **kwargs)


def revision_field():
    return forms.IntegerField(widget=forms.HiddenInput)


class EquipmentChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return f"{obj.code} · {obj.name}"


def equipment_choices(*, kinds=None, exclude_kinds=None, keep=None):
    """Equipment in use (not retired) of the given kinds, plus `keep` (the current value)."""
    queryset = Equipment.objects.exclude(status=Equipment.Status.RETIRED)
    if kinds:
        queryset = queryset.filter(kind__in=kinds)
    if exclude_kinds:
        queryset = queryset.exclude(kind__in=exclude_kinds)
    if keep:
        queryset = Equipment.objects.filter(
            pk__in=queryset.values("pk")
        ) | Equipment.objects.filter(pk=getattr(keep, "pk", keep))
    return queryset.order_by("code")


def staff_choices(keep=None):
    """Active staff with a read role, plus `keep` (the person a corrected event names)."""
    staff = User.objects.filter(is_active=True, groups__name__in=READ_ROLES).values("pk")
    condition = Q(pk__in=staff) | Q(pk=keep) if keep else Q(pk__in=staff)
    return User.objects.filter(condition).order_by("first_name", "last_name", "username")


class StaffChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, user):
        return user.get_full_name() or user.get_username()


class GroupedForm(forms.Form):
    """`groups` is ((legend, names, note), ...); a tuple in names renders side by side."""

    groups = ()

    def grouped(self):
        sections = []
        for group in self.groups:
            legend, names, note = (*group, "")[:3]
            rows = []
            for item in names:
                names_in_row = item if isinstance(item, tuple) else (item,)
                row = [self[n] for n in names_in_row if n in self.fields]
                row = [bound for bound in row if not bound.is_hidden]
                if row:
                    rows.append(row)
            if rows:
                sections.append((legend, rows, note))
        return sections

    @property
    def has_times(self):
        return any(isinstance(f, forms.DateTimeField) for f in self.fields.values())

    def disable(self):
        for field in self.fields.values():
            field.disabled = True


def values(form, names):
    return {name: form.cleaned_data.get(name) for name in names if name in form.fields}


# --- register -------------------------------------------------------------------------------

LIMITS_NOTE = (
    "Your repository sets these limits for this equipment. There is no default and nothing is "
    "computed: leave a limit empty when none is set. Readings keep the limits in force when they "
    "are recorded."
)


class EquipmentForm(GroupedForm):
    code = forms.CharField(
        label="Asset ID",
        max_length=40,
        help_text="Letters, digits, '.', '_' or '-'; stored in capitals.",
        widget=forms.TextInput(attrs={"autocapitalize": "characters", "spellcheck": "false"}),
    )
    name = forms.CharField(label="Name", max_length=160)
    kind = forms.ChoiceField(label="Kind", choices=Equipment.Kind.choices)
    is_critical = forms.BooleanField(
        label="Critical equipment",
        required=False,
        help_text="On the repository's list of critical equipment.",
    )
    storage_unit = forms.ModelChoiceField(
        label="Storage unit",
        queryset=StorageUnit.objects.none(),
        required=False,
        empty_label="None",
        help_text="The storage unit this tank or freezer encloses. A supply dewar or a monitor "
        "has none.",
    )
    location = forms.CharField(label="Location", max_length=160, required=False)
    manufacturer = forms.CharField(label="Manufacturer", max_length=120, required=False)
    model = forms.CharField(label="Model", max_length=120, required=False)
    serial_number = forms.CharField(label="Serial number", max_length=120, required=False)
    installed_on = forms.DateField(label="Installed on", widget=DATE, required=False)
    service_provider = forms.CharField(label="Service provider", max_length=160, required=False)
    notes = text_field("Notes")
    lower_limit = temperature_field("Lower limit", help_text="Readings below it are outside.")
    upper_limit = temperature_field("Upper limit", help_text="Readings above it are outside.")
    level_unit = forms.ChoiceField(
        label="Level unit",
        choices=[("", "Not set"), *LevelUnit.choices],
        required=False,
        help_text="LN2 tanks and supply dewars: the unit levels are read in.",
    )
    level_low_limit = decimal_field(
        "Low level",
        LEVEL,
        min_value=0,
        help_text="LN2 tanks and supply dewars: the level at which your repository refills.",
    )
    reason = reason_field("Reason for the change")
    expected_revision = revision_field()

    groups = (
        ("Equipment", [("code", "kind"), "name", "is_critical"]),
        ("Where", ["storage_unit", "location"]),
        (
            "Make and service",
            [("manufacturer", "model"), ("serial_number", "installed_on"), "service_provider"],
        ),
        (
            "Limits set by the repository",
            [("lower_limit", "upper_limit"), ("level_unit", "level_low_limit")],
            LIMITS_NOTE,
        ),
        ("Notes", ["notes"]),
        ("Change", ["reason"]),
    )

    def __init__(self, *args, instance=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance = instance
        units = StorageUnit.objects.filter(is_active=True)
        if instance is not None and instance.storage_unit_id:
            units = units | StorageUnit.objects.filter(pk=instance.storage_unit_id)
        self.fields["storage_unit"].queryset = units.order_by("code")
        if instance is None:
            del self.fields["reason"]
            del self.fields["expected_revision"]
            return
        self.initial.setdefault("expected_revision", instance.revision)
        for name in self.fields:
            if name not in ("reason", "expected_revision"):
                attribute = "storage_unit_id" if name == "storage_unit" else name
                self.initial.setdefault(name, getattr(instance, attribute))


class StatusForm(GroupedForm):
    status = forms.ChoiceField(label="New status", widget=forms.RadioSelect)
    occurred_at = when_field(
        "Effective from", "Repository local time; not before the last status change."
    )
    reason = reason_field(
        "Reason", "Kept in the status history, e.g. the fault, the repair or the return date."
    )
    expected_revision = revision_field()
    groups = (("", ["status", "occurred_at", "reason"]),)

    def __init__(self, *args, equipment, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["status"].choices = [
            choice for choice in Equipment.Status.choices if choice[0] != equipment.status
        ]
        self.initial.setdefault("occurred_at", now_to_the_second())
        self.initial.setdefault("expected_revision", equipment.revision)


# --- service events -------------------------------------------------------------------------

SERVICE_VALUES = (
    "performed_on",
    "performed_by",
    "performed_by_external",
    "outcome",
    "reference",
    "description",
    "reference_device",
    "as_found",
    "as_left",
    "next_due_on",
)


class ServiceForm(GroupedForm):
    kind = forms.ChoiceField(label="Kind", choices=ServiceEvent.Kind.choices)
    performed_on = forms.DateField(label="Performed on", widget=DATE)
    outcome = forms.ChoiceField(
        label="Outcome",
        choices=[("", "Select…"), *ServiceEvent.Outcome.choices],
        help_text="Maintenance and repair: completed or not acceptable. Calibration, mapping and "
        "qualification: acceptable or not acceptable.",
    )
    performed_by = StaffChoiceField(
        label="Performed by (staff)",
        queryset=User.objects.none(),
        required=False,
        empty_label="Not a staff member",
    )
    performed_by_external = forms.CharField(
        label="Performed by (outside provider)",
        max_length=160,
        required=False,
        help_text="Name the staff member or the outside provider.",
    )
    reference = forms.CharField(
        label="Reference",
        max_length=160,
        required=False,
        help_text="Certificate, work order or procedure reference.",
    )
    description = text_field("Description", help_text="What was done; for a repair, the cause.")
    reference_device = forms.CharField(
        label="Reference device",
        max_length=160,
        required=False,
        help_text="Calibration: the reference instrument's name and serial number.",
    )
    as_found = forms.CharField(label="As found", max_length=200, required=False)
    as_left = forms.CharField(label="As left", max_length=200, required=False)
    next_due_on = forms.DateField(
        label="Next due",
        widget=DATE,
        required=False,
        help_text="Your repository's date for the next one of this kind; never computed. A repair "
        "has none.",
    )
    attachment = AttachmentField(label="Certificate or report")
    void = forms.BooleanField(
        label="Withdraw this event",
        required=False,
        help_text="It was recorded in error. The values below are ignored and the event no "
        "longer counts.",
    )
    reason = reason_field("Reason for the correction")

    groups = (
        ("Event", [("kind", "performed_on"), "outcome"]),
        ("Performed by", [("performed_by", "performed_by_external")]),
        (
            "Details",
            ["reference", "description", "reference_device", ("as_found", "as_left")],
        ),
        ("Next due", ["next_due_on"]),
        ("Attachment", ["attachment"]),
        ("Correction", ["void", "reason"]),
    )

    def __init__(self, *args, actor, original=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["performed_by"].queryset = staff_choices()
        if original is None:
            for name in ("void", "reason"):
                del self.fields[name]
            self.initial.setdefault("performed_on", timezone.localdate())
            self.initial.setdefault("performed_by", actor.pk)
            return
        del self.fields["kind"]
        self.fields["attachment"].label = "Replace the attachment"
        self.fields["attachment"].help_text += " Leave empty to keep the current file."
        self.fields["performed_by"].queryset = staff_choices(keep=original.performed_by_id)
        for name in SERVICE_VALUES:
            value = getattr(original, f"{name}_id" if name == "performed_by" else name)
            self.initial.setdefault(name, value)
        self.groups = (("Correction", ["void", "reason"]),) + self.groups[:-1]


# --- LN2 log --------------------------------------------------------------------------------

LN2_VALUES = (
    "occurred_at",
    "method",
    "level_before",
    "level",
    "level_unit",
    "amount_added",
    "amount_unit",
    "supply",
    "reference",
    "performed_by_external",
    "note",
)
FILL_ONLY = ("level_before", "amount_added", "amount_unit", "supply", "reference")


class LN2Form(GroupedForm):
    equipment = EquipmentChoiceField(label="Vessel", queryset=Equipment.objects.none())
    kind = forms.ChoiceField(choices=LN2Entry.Kind.choices, widget=forms.HiddenInput)
    occurred_at = when_field("When")
    method = forms.ChoiceField(
        label="How the level was read",
        choices=[("", "Not recorded"), *LN2Entry.Method.choices],
        required=False,
    )
    level_before = decimal_field("Level before the fill", LEVEL, min_value=0)
    level = decimal_field("Level", LEVEL, min_value=0)
    level_unit = forms.ChoiceField(
        label="Level unit",
        choices=[("", "The vessel's unit"), *LevelUnit.choices],
        required=False,
        help_text="Left empty, the unit set on the vessel is used.",
    )
    amount_added = decimal_field("Amount added", AMOUNT)
    amount_unit = forms.ChoiceField(
        label="Amount unit", choices=[("", "Select…"), *AmountUnit.choices], required=False
    )
    supply = EquipmentChoiceField(
        label="Supply dewar",
        queryset=Equipment.objects.none(),
        required=False,
        empty_label="Not from a registered dewar",
    )
    reference = forms.CharField(
        label="Delivery reference",
        max_length=160,
        required=False,
        help_text="Delivery note or supplier reference.",
    )
    performed_by_external = forms.CharField(
        label="Filled by (outside provider)", max_length=160, required=False
    )
    note = text_field("Note", rows=2)
    void = forms.BooleanField(
        label="Withdraw this entry",
        required=False,
        help_text="It was recorded in error. The values below are ignored and the entry no "
        "longer counts.",
    )
    reason = reason_field("Reason for the correction")

    groups = (
        ("Correction", ["void", "reason"]),
        ("Entry", ["equipment", "occurred_at", "method"]),
        ("Level", [("level_before", "level"), "level_unit"]),
        ("Fill", [("amount_added", "amount_unit"), "supply", "reference"]),
        ("Other", ["performed_by_external", "note"]),
    )

    def __init__(self, *args, kind, original=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.kind = kind
        self.initial["kind"] = kind
        keep_supply = original.supply_id if original else None
        self.fields["supply"].queryset = equipment_choices(
            kinds=("supply_dewar",), keep=keep_supply
        )
        if kind == LN2Entry.Kind.LEVEL:
            for name in FILL_ONLY:
                del self.fields[name]
            self.fields["level"].required = True
            self.fields["level"].label = "Level read"
            self.fields["performed_by_external"].label = "Read by (outside provider)"
        else:
            self.fields["level"].label = "Level after the fill"
            self.fields["level"].help_text = "Enter the level after the fill, the amount, or both."
        if original is None:
            for name in ("void", "reason"):
                del self.fields[name]
            self.fields["equipment"].queryset = equipment_choices(kinds=Equipment.LN2_KINDS)
            self.initial.setdefault("occurred_at", now_to_the_second())
            return
        del self.fields["equipment"]
        for name in LN2_VALUES:
            if name in self.fields:
                value = getattr(original, "supply_id" if name == "supply" else name)
                self.initial.setdefault(name, local(value) if name == "occurred_at" else value)


# --- temperature readings -------------------------------------------------------------------

READING_VALUES = ("measured_at", "value", "probe_position", "device", "note")


class ReadingForm(GroupedForm):
    equipment = EquipmentChoiceField(
        label="Equipment",
        queryset=Equipment.objects.none(),
        help_text="The tank or freezer read, not the monitor.",
    )
    measured_at = when_field("Read at")
    value = temperature_field("Temperature", required=True)
    probe_position = forms.CharField(
        label="Probe position",
        max_length=80,
        required=False,
        help_text="Where it was read, as stated: top of rack, display.",
    )
    device = EquipmentChoiceField(
        label="Monitor", queryset=Equipment.objects.none(), required=False, empty_label="None"
    )
    note = text_field("Note", rows=2)
    void = forms.BooleanField(
        label="Withdraw this reading",
        required=False,
        help_text="It was recorded in error. The values below are ignored and the reading no "
        "longer counts.",
    )
    reason = reason_field("Reason for the correction")

    groups = (
        ("Correction", ["void", "reason"]),
        ("Reading", ["equipment", ("measured_at", "value"), ("probe_position", "device"), "note"]),
    )

    def __init__(self, *args, original=None, **kwargs):
        super().__init__(*args, **kwargs)
        keep = original.device_id if original else None
        self.fields["device"].queryset = equipment_choices(kinds=("monitor",), keep=keep)
        if original is None:
            for name in ("void", "reason"):
                del self.fields[name]
            self.fields["equipment"].queryset = equipment_choices(exclude_kinds=("monitor",))
            self.initial.setdefault("measured_at", now_to_the_second())
            return
        del self.fields["equipment"]
        for name in READING_VALUES:
            value = getattr(original, "device_id" if name == "device" else name)
            self.initial.setdefault(name, local(value) if name == "measured_at" else value)


class ImportUploadForm(GroupedForm):
    file = forms.FileField(
        label="CSV file",
        help_text="UTF-8 CSV, up to 5 MiB and 10,000 rows. Nothing is recorded "
        "until you commit a valid file.",
    )
    equipment = EquipmentChoiceField(
        label="Equipment",
        queryset=Equipment.objects.none(),
        required=False,
        empty_label="Named in the file",
        help_text="For every row without an equipment column value.",
    )
    groups = (("", ["file", "equipment"]),)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["equipment"].queryset = equipment_choices(exclude_kinds=("monitor",))


class ImportCommitForm(GroupedForm):
    reason = reason_field("Reason", "Kept with the import, e.g. the logger download it came from.")
    groups = (("", ["reason"]),)


# --- excursions -----------------------------------------------------------------------------

EXCURSION_VALUES = (
    "started_at",
    "ended_at",
    "extreme_value",
    "extreme_at",
    "detection",
    "cause_category",
    "cause",
    "action",
    "impact",
)


class ExcursionForm(GroupedForm):
    equipment = EquipmentChoiceField(
        label="Equipment",
        queryset=Equipment.objects.none(),
        help_text="The tank or freezer; its storage unit and current limits are kept with the "
        "excursion.",
    )
    started_at = when_field("Started")
    ended_at = when_field(
        "Ended", "Repository local time. Leave empty while it is ongoing.", required=False
    )
    extreme_value = temperature_field("Extreme reached")
    extreme_at = when_field("Extreme reached at", "Within the excursion.", required=False)
    detection = forms.ChoiceField(
        label="Detected by", choices=[("", "Select…"), *Excursion.Detection.choices]
    )
    cause_category = forms.ChoiceField(
        label="Cause category", choices=Excursion.Cause.choices, initial=Excursion.Cause.UNKNOWN
    )
    cause = text_field("Cause", help_text="Needed before submitting for review.")
    action = text_field("Action taken", help_text="Needed before submitting for review.")
    impact = text_field(
        "Impact assessment",
        help_text="Effect on the specimens stored in the unit. Needed before submitting.",
    )
    note = text_field("Note", rows=2, help_text=NOT_IDENTIFYING)
    attachment = AttachmentField(label="Attachment")
    expected_revision = revision_field()

    groups = (
        ("Excursion", ["equipment", ("started_at", "ended_at"), ("extreme_value", "extreme_at")]),
        ("Investigation", ["detection", "cause_category", "cause", "action", "impact"]),
        ("First note", ["note", "attachment"]),
    )

    def __init__(self, *args, instance=None, **kwargs):
        super().__init__(*args, **kwargs)
        if instance is None:
            del self.fields["expected_revision"]
            self.fields["equipment"].queryset = equipment_choices(exclude_kinds=("monitor",))
            self.initial.setdefault("started_at", now_to_the_second())
            return
        for name in ("equipment", "note", "attachment"):
            del self.fields[name]
        self.initial.setdefault("expected_revision", instance.revision)
        for name in EXCURSION_VALUES:
            value = getattr(instance, name)
            self.initial.setdefault(name, local(value) if name.endswith("_at") else value)


class SubmitForm(GroupedForm):
    note = text_field("Note", rows=2, help_text=f"Optional, for the reviewer. {NOT_IDENTIFYING}")
    expected_revision = revision_field()
    groups = (("", ["note"]),)


DECISIONS = (("return", "Return for more work"), ("close", "Close the excursion"))


class ReviewForm(GroupedForm):
    decision = forms.ChoiceField(label="Decision", choices=DECISIONS, widget=forms.RadioSelect)
    note = reason_field(
        "Review note", "Kept in the timeline: what is missing, or why it can be closed."
    )
    expected_revision = revision_field()
    groups = (("", ["decision", "note"]),)

    def __init__(self, *args, may_close=True, **kwargs):
        super().__init__(*args, **kwargs)
        if not may_close:
            self.fields["decision"].choices = DECISIONS[:1]
            self.initial["decision"] = "return"


class ReopenForm(GroupedForm):
    reason = reason_field("Reason for reopening")
    expected_revision = revision_field()
    groups = (("", ["reason"]),)


class NoteForm(GroupedForm):
    note = text_field("Note", help_text=NOT_IDENTIFYING)
    attachment = AttachmentField(label="Attachment")
    groups = (("", ["note", "attachment"]),)


HOLD_SNAPSHOT_SALT = "equipment.excursion-manual-holds"
HOLD_SNAPSHOT_AGE = 60 * 60


class ExcursionHoldsForm(GroupedForm):
    specimen_ids = forms.MultipleChoiceField(
        label="Specimens",
        choices=(),
        widget=forms.CheckboxSelectMultiple(
            attrs={"class": "form-check-input", "aria-describedby": "selection-note"}
        ),
        error_messages={
            "required": "Select at least one specimen from this page.",
            "invalid_choice": "A selected specimen is not available on this page. "
            "Reload the candidates and review your selection.",
        },
    )
    scope = forms.ChoiceField(
        label="Hold scope",
        choices=[("", "Select…"), *Hold.Scope.choices],
        help_text="Disposal blocks disposal only. Use blocks use and dispatch, but permits "
        "disposal. All-use blocks both. Existing holds remain separate.",
    )
    reason = reason_field(help_text="The excursion number is added to every hold reason.")
    snapshot = forms.CharField(widget=forms.HiddenInput)
    groups = (("Hold details", ["scope", "reason"]),)

    def __init__(self, *args, actor, excursion, candidates, **kwargs):
        super().__init__(*args, **kwargs)
        self.actor, self.excursion = actor, excursion
        self.fields["specimen_ids"].choices = [
            (str(item["specimen"].pk), item["specimen"].accession) for item in candidates
        ]
        limit = HOLD_REASON_LIMIT - len(f"Excursion {excursion.number}: ")
        self.fields["reason"].max_length = limit
        self.fields["reason"].validators.append(MaxLengthValidator(limit))
        self.fields["reason"].widget.attrs["maxlength"] = limit
        self.fields["reason"].help_text += f" Use at most {limit} characters."
        # Never refresh a bound snapshot: a failed submission must not approve newer state.
        if not self.is_bound:
            self.initial["snapshot"] = signing.dumps(
                {
                    "actor": str(actor.pk),
                    "excursion": str(excursion.pk),
                    "revision": excursion.revision,
                    "specimens": {
                        str(item["specimen"].pk): item["specimen"].revision
                        for item in candidates
                        if item["selectable"]
                    },
                },
                salt=HOLD_SNAPSHOT_SALT,
                compress=True,
            )

    def clean_specimen_ids(self):
        selected = self.cleaned_data["specimen_ids"]
        if len(selected) > EXCURSION_HOLD_LIMIT or len(selected) != len(set(selected)):
            raise forms.ValidationError(
                f"Select at most {EXCURSION_HOLD_LIMIT} distinct specimens."
            )
        return selected

    def clean_snapshot(self):
        try:
            value = signing.loads(
                self.cleaned_data["snapshot"], salt=HOLD_SNAPSHOT_SALT, max_age=HOLD_SNAPSHOT_AGE
            )
            if (
                value["actor"] != str(self.actor.pk)
                or value["excursion"] != str(self.excursion.pk)
                or type(value["revision"]) is not int
                or not isinstance(value["specimens"], dict)
            ):
                raise ValueError
        except signing.BadSignature, KeyError, TypeError, ValueError:
            raise forms.ValidationError(
                "This candidate review expired or is invalid. Reload the candidates and "
                "review your selection before submitting again."
            ) from None
        return value

    def clean(self):
        data = super().clean()
        if "snapshot" in data and "specimen_ids" in data:
            if not set(data["specimen_ids"]).issubset(data["snapshot"]["specimens"]):
                self.add_error("specimen_ids", "Select only specimens from your reviewed page.")
        return data
