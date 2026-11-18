"""Forms of the request, allocation, picking, shipment, receipt and return pages.

Transport and input shape only: distribution.services repeats every rule under lock. Every
free-text field and every upload says not to enter participant-identifying information. Times
are entered in the repository's time zone to the second; amounts are text parsed exactly by the
services (never floats).
"""

from django import forms
from django.db.models import Q
from django.utils import timezone

from accounts.models import User
from accounts.roles import has_role
from core.attachments import AttachmentField
from specimens.forms import THAW_CHOICES, THAW_VALUES, add_position_fields, resolve_position
from specimens.models import QuantityUnit, SpecimenType

from .models import (
    NO_PARTICIPANT_TEXT,
    UPLOAD_TEXT,
    Agreement,
    ConsentScopeCategory,
    Investigator,
    ReceiptException,
    ReturnReview,
    Shipment,
    ShipmentEvent,
    ShipmentReceipt,
    SpecimenRequest,
    Study,
)
from .services import MAX_ITEMS, OPERATE_ROLES, TEXT_LIMIT

DATETIME = forms.DateTimeInput(
    attrs={"type": "datetime-local", "step": "1"}, format="%Y-%m-%dT%H:%M:%S"
)
DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")
WHEN_HELP = "Repository local time; not in the future."

# Service error keys that differ from the form field showing them.
FIELD_ALIASES = {
    "recipient_id": "recipient",
    "checked_by": "checked_by",
    "choices": "choices",
    "allocations": "allocations",
    "lines": None,
    "items": None,
    "agreement": "agreement",
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


def text_field(label, *, required=False, max_length=TEXT_LIMIT, rows=3, help_text=""):
    help_text = f"{help_text} {NO_PARTICIPANT_TEXT}".strip()
    return forms.CharField(
        label=label,
        required=required,
        max_length=max_length,
        widget=forms.Textarea(attrs={"rows": rows}),
        help_text=help_text,
    )


def line_field(label, *, required=False, max_length=160, help_text=""):
    help_text = f"{help_text} {NO_PARTICIPANT_TEXT}".strip()
    return forms.CharField(
        label=label, required=required, max_length=max_length, help_text=help_text
    )


def reason_field(label="Reason", help_text="Kept in the history with the change."):
    return text_field(label, required=True, help_text=help_text)


def when_field(label, help_text=WHEN_HELP, required=True):
    return forms.DateTimeField(label=label, widget=DATETIME, help_text=help_text, required=required)


def revision_field():
    return forms.IntegerField(widget=forms.HiddenInput)


def attachment_field(label="Attachment", help_text=""):
    field = AttachmentField(label=label)
    field.help_text = f"{field.help_text} {help_text} {UPLOAD_TEXT}".replace("  ", " ")
    return field


def operating_staff(*, exclude=None):
    """Active users with an operating role (admin, manager, technician)."""
    queryset = User.objects.filter(is_active=True, groups__name__in=OPERATE_ROLES)
    if exclude is not None:
        queryset = queryset.exclude(pk=exclude.pk)
    return queryset.distinct().order_by("first_name", "last_name", "username")


class StaffChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, user):
        return user.get_full_name() or user.get_username()


class CodeChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return str(obj)


class CodeMultipleChoiceField(forms.ModelMultipleChoiceField):
    def label_from_instance(self, obj):
        return str(obj)


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


def _keep(queryset, model, current):
    """Active entries plus the current value (so an inactive choice already made stays)."""
    if current:
        return model.objects.filter(Q(pk__in=queryset.values("pk")) | Q(pk__in=current))
    return queryset


# --- requests -------------------------------------------------------------------------------

REQUEST_VALUES = (
    "title",
    "investigator",
    "purpose",
    "ethics_reference",
    "ethics_approved_until",
    "use_categories",
    "studies",
    "data_requested",
    "needed_by",
    "received_via",
)


class RequestForm(GroupedForm):
    title = forms.CharField(label="Title", max_length=160, help_text=NO_PARTICIPANT_TEXT)
    investigator = CodeChoiceField(
        label="Investigator",
        queryset=Investigator.objects.none(),
        help_text="The requesting investigator; the request takes their institution.",
    )
    received_via = forms.ChoiceField(
        label="Received by",
        choices=[("", "Select…"), *SpecimenRequest.ReceivedVia.choices],
        help_text="How the request reached the repository.",
    )
    purpose = text_field(
        "Purpose", required=True, rows=4, help_text="The research use, as the requester stated it."
    )
    use_categories = CodeMultipleChoiceField(
        label="Uses requested",
        queryset=ConsentScopeCategory.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="Scope categories that govern use. Each specimen's consent must permit every "
        "one chosen. At least one is needed to submit.",
    )
    studies = CodeMultipleChoiceField(
        label="Studies",
        queryset=Study.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="Leave all unticked for material of any study.",
    )
    ethics_reference = line_field(
        "Ethics approval reference", max_length=80, help_text="The requester's approval, if any."
    )
    ethics_approved_until = forms.DateField(
        label="Ethics approval valid until", widget=DATE, required=False
    )
    data_requested = text_field("Data requested", help_text="Data asked for with the material.")
    needed_by = forms.DateField(label="Needed by", widget=DATE, required=False)
    reason = reason_field("Reason for the change")
    expected_revision = revision_field()

    groups = (
        ("Request", ["title", ("investigator", "received_via"), "purpose"]),
        ("Scope", ["use_categories", "studies", "data_requested"]),
        ("Approval and timing", [("ethics_reference", "ethics_approved_until"), "needed_by"]),
        ("Change", ["reason"]),
    )

    def __init__(self, *args, instance=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance = instance
        keep = {"investigator": [], "use_categories": [], "studies": []}
        if instance is not None:
            keep["investigator"] = [instance.investigator_id]
            keep["use_categories"] = list(instance.use_categories.values_list("pk", flat=True))
            keep["studies"] = list(instance.studies.values_list("pk", flat=True))
        self.fields["investigator"].queryset = _keep(
            Investigator.objects.filter(is_active=True), Investigator, keep["investigator"]
        ).order_by("code")
        self.fields["use_categories"].queryset = _keep(
            ConsentScopeCategory.objects.filter(is_active=True, gates_use=True),
            ConsentScopeCategory,
            keep["use_categories"],
        ).order_by("code")
        self.fields["studies"].queryset = Study.objects.order_by("code")
        if instance is None:
            del self.fields["reason"]
            del self.fields["expected_revision"]
            return
        self.initial.setdefault("expected_revision", instance.revision)
        for name in REQUEST_VALUES:
            if name in ("use_categories", "studies"):
                self.initial.setdefault(name, keep[name])
            elif name == "investigator":
                self.initial.setdefault(name, instance.investigator_id)
            else:
                self.initial.setdefault(name, getattr(instance, name))

    def values(self):
        data = self.cleaned_data
        result = {}
        for name in REQUEST_VALUES:
            value = data.get(name)
            if name == "investigator":
                value = value.pk if value else None
            elif name in ("use_categories", "studies"):
                value = [obj.pk for obj in value or ()]
            elif value is None:
                value = None if name in ("ethics_approved_until", "needed_by") else ""
            result[name] = value
        return result


class ItemsForm(GroupedForm):
    reason = reason_field("Reason for the change")
    expected_revision = revision_field()
    groups = (("Change", ["reason"]),)

    def __init__(self, *args, needs_reason=True, **kwargs):
        super().__init__(*args, **kwargs)
        if not needs_reason:
            self.fields["reason"].required = False
            self.fields["reason"].help_text = (
                "Optional for a request that was never saved with lines. " + NO_PARTICIPANT_TEXT
            )


class ItemForm(forms.Form):
    """One line of a request: what kind of material and how much."""

    line = forms.IntegerField(widget=forms.HiddenInput, required=False)
    specimen_type = CodeChoiceField(label="Specimen type", queryset=SpecimenType.objects.none())
    count = forms.IntegerField(label="Count", min_value=1, max_value=500)
    minimum_quantity = forms.CharField(
        label="Minimum amount",
        max_length=40,
        required=False,
        widget=forms.TextInput(attrs={"inputmode": "decimal", "autocomplete": "off"}),
    )
    unit = CodeChoiceField(
        label="Unit", queryset=QuantityUnit.objects.none(), required=False, empty_label="—"
    )
    study = CodeChoiceField(
        label="Study", queryset=Study.objects.none(), required=False, empty_label="Any study"
    )
    notes = forms.CharField(label="Notes", max_length=500, required=False)

    def __init__(self, *args, catalogues=None, **kwargs):
        super().__init__(*args, **kwargs)
        catalogues = catalogues or {}
        self.fields["specimen_type"].queryset = catalogues.get(
            "specimen_type", SpecimenType.objects.filter(is_active=True).order_by("code")
        )
        self.fields["unit"].queryset = catalogues.get(
            "unit", QuantityUnit.objects.filter(is_active=True).order_by("code")
        )
        self.fields["study"].queryset = catalogues.get("study", Study.objects.order_by("code"))


ItemFormSet = forms.formset_factory(
    ItemForm, extra=0, min_num=1, validate_min=True, max_num=MAX_ITEMS, validate_max=True
)
ITEM_FIELDS = ("line", "specimen_type", "count", "minimum_quantity", "unit", "study", "notes")


class SubmitForm(GroupedForm):
    expected_revision = revision_field()


DECISION_CHOICES = (
    ("approve", "Approve"),
    ("return", "Return for changes"),
    ("reject", "Reject"),
)


class DecideForm(GroupedForm):
    decision = forms.ChoiceField(
        label="Decision", choices=DECISION_CHOICES, widget=forms.RadioSelect
    )
    committee_reference = line_field(
        "Committee or reviewer reference",
        help_text="Where the decision is minuted, for example an access committee minute.",
    )
    conditions = text_field("Conditions", help_text="Conditions attached to an approval.")
    conflicts_note = text_field(
        "Conflicts of interest", help_text="Declared conflicts and how they were handled."
    )
    reason = reason_field("Reason", "Why the request was approved, returned or rejected.")
    expected_revision = revision_field()
    submission_number = forms.IntegerField(widget=forms.HiddenInput)
    groups = (
        ("Decision", ["decision", "committee_reference", "conditions", "conflicts_note", "reason"]),
    )


class ReasonForm(GroupedForm):
    reason = reason_field()
    expected_revision = revision_field()
    groups = (("", ["reason"]),)


class AgreementForm(GroupedForm):
    agreement = CodeChoiceField(
        label="Agreement",
        queryset=Agreement.objects.none(),
        required=False,
        empty_label="No agreement",
        help_text="Agreements with the request's institution that are not terminated. A draft "
        "can be linked; dispatch needs one that is valid on the dispatch date.",
    )
    reason = reason_field()
    expected_revision = revision_field()
    groups = (("", ["agreement", "reason"]),)

    def __init__(self, *args, request_obj, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["agreement"].queryset = (
            Agreement.objects.filter(institution_id=request_obj.institution_id)
            .exclude(status=Agreement.Status.TERMINATED)
            .order_by("-id")
        )
        self.initial.setdefault("agreement", request_obj.agreement_id)
        self.initial.setdefault("expected_revision", request_obj.revision)


class RequestNoteForm(GroupedForm):
    note = text_field("Note", required=True, rows=4)
    groups = (("", ["note"]),)


# --- allocation and picking -----------------------------------------------------------------


class AllocateForm(GroupedForm):
    below_minimum_ok = forms.BooleanField(
        label="Allocate specimens below the requested minimum amount",
        required=False,
        help_text="Needed when a chosen specimen holds less than the line's minimum amount, or "
        "is measured in another unit.",
    )
    occurred_at = when_field("Allocated at")
    reason = reason_field("Reason", "Kept with each allocation.")
    expected_revision = revision_field()
    groups = (("Allocation", ["below_minimum_ok", "occurred_at", "reason"]),)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial.setdefault("occurred_at", now_to_the_second())
        self.initial.setdefault("reason", "Allocated for the approved request")


class ReleaseForm(GroupedForm):
    allocations = forms.MultipleChoiceField(
        label="Allocated specimens to release",
        widget=forms.CheckboxSelectMultiple,
        help_text="Their allocation holds are released and they can be allocated again.",
    )
    occurred_at = when_field("Released at")
    reason = reason_field()
    expected_revision = revision_field()
    groups = (("", ["allocations", "occurred_at", "reason"]),)

    def __init__(self, *args, choices=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["allocations"].choices = list(choices)
        self.initial.setdefault("occurred_at", now_to_the_second())


PICK_OUTCOMES = (
    ("picked", "Picked"),
    ("missing", "Not found"),
    ("leave", "Not now"),
)


class PickForm(GroupedForm):
    recipient = StaffChoiceField(
        label="Picked by (custodian)",
        queryset=User.objects.none(),
        help_text="The person who holds the specimens once checked out.",
    )
    occurred_at = when_field("Picked at")
    reason = reason_field("Reason", "Kept with each check-out and allocation event.")
    expected_revision = revision_field()
    groups = (("Retrieval", ["recipient", "occurred_at", "reason"]),)

    def __init__(self, *args, actor, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["recipient"].queryset = operating_staff()
        self.initial.setdefault("recipient", actor.pk)
        self.initial.setdefault("occurred_at", now_to_the_second())
        self.initial.setdefault("reason", "Retrieved for the request")


class PickRowForm(forms.Form):
    allocation = forms.UUIDField(widget=forms.HiddenInput)
    revision = forms.IntegerField(widget=forms.HiddenInput)
    occupancy_revision = forms.IntegerField(widget=forms.HiddenInput)
    outcome = forms.ChoiceField(label="Outcome", choices=PICK_OUTCOMES, widget=forms.RadioSelect)
    note = forms.CharField(
        label="Note", max_length=500, required=False, help_text=NO_PARTICIPANT_TEXT
    )


PickFormSet = forms.formset_factory(PickRowForm, extra=0, max_num=200, validate_max=True)


class PutBackForm(GroupedForm):
    recipient = StaffChoiceField(
        label="Custodian after check-in",
        queryset=User.objects.none(),
        help_text="The person recorded as putting the specimens away.",
    )
    release = forms.BooleanField(
        label="Release them from the request",
        required=False,
        help_text="Managers: the allocation and its hold are released and the specimens can be "
        "allocated again. Unticked, they stay allocated to this request.",
    )
    occurred_at = when_field("Put back at")
    reason = reason_field("Reason", "Kept with each check-in and allocation event.")
    expected_revision = revision_field()
    groups = (("Put back", ["recipient", "release", "occurred_at", "reason"]),)

    def __init__(self, *args, actor, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["recipient"].queryset = operating_staff()
        self.initial.setdefault("recipient", actor.pk)
        self.initial.setdefault("occurred_at", now_to_the_second())
        if not has_role(actor, "admin", "manager"):
            del self.fields["release"]


class PutBackRowForm(forms.Form):
    """One picked specimen: tick it, choose its box and type the position label."""

    include = forms.BooleanField(label="Put back", required=False)
    allocation = forms.UUIDField(widget=forms.HiddenInput)
    revision = forms.IntegerField(widget=forms.HiddenInput)
    occupancy_revision = forms.IntegerField(widget=forms.HiddenInput)
    # Already checked in from the specimen record: nothing to place (the service checks it).
    in_storage = forms.BooleanField(required=False, widget=forms.HiddenInput)
    thaw_observed = forms.ChoiceField(
        label="Thaw observed while out", choices=THAW_CHOICES, initial="unknown"
    )

    def __init__(self, *args, actor=None, choices=(), **kwargs):
        super().__init__(*args, **kwargs)
        add_position_fields(self, actor=actor, choices=choices, required=False, help_text=False)
        for name in ("box", "position_label"):
            self.fields[name].help_text = ""

    def clean(self):
        data = super().clean()
        if not data.get("include") or data.get("in_storage"):
            data["position"] = None
            data["thaw_value"] = None
            return data
        resolve_position(self, data)
        if data.get("position") is None and not self.errors:
            self.add_error("box", "Choose the box and position to put it in.")
        data["thaw_value"] = THAW_VALUES.get(data.get("thaw_observed"))
        return data


PutBackFormSet = forms.formset_factory(PutBackRowForm, extra=0, max_num=200, validate_max=True)


# --- shipments ------------------------------------------------------------------------------

SHIPMENT_VALUES = (
    "ship_to_name",
    "ship_to_institution",
    "ship_to_address",
    "ship_to_contact",
    "planned_on",
    "courier",
    "service_level",
    "tracking",
    "storage_instructions",
)
PACKED_VALUES = ("planned_on", "courier", "service_level", "tracking")


class ShipmentForm(GroupedForm):
    ship_to_name = forms.CharField(label="Recipient", max_length=160)
    ship_to_institution = forms.CharField(label="Institution", max_length=160)
    ship_to_address = forms.CharField(
        label="Address", max_length=500, widget=forms.Textarea(attrs={"rows": 3})
    )
    ship_to_contact = forms.CharField(
        label="Contact",
        max_length=160,
        required=False,
        help_text="The recipient's professional email or phone.",
    )
    planned_on = forms.DateField(label="Planned dispatch date", widget=DATE, required=False)
    courier = forms.CharField(label="Courier", max_length=80, required=False)
    service_level = forms.CharField(label="Service", max_length=80, required=False)
    tracking = forms.CharField(label="Tracking number", max_length=80, required=False)
    storage_instructions = line_field(
        "Storage instructions",
        max_length=500,
        help_text="Printed on the manifest, for example the storage temperature on arrival.",
    )
    reason = reason_field("Reason for the change")
    expected_revision = revision_field()
    groups = (
        (
            "Ship to",
            [("ship_to_name", "ship_to_institution"), "ship_to_address", "ship_to_contact"],
        ),
        ("Courier", ["planned_on", ("courier", "service_level"), "tracking"]),
        ("Instructions", ["storage_instructions"]),
        ("Change", ["reason"]),
    )

    def __init__(self, *args, instance=None, request_obj=None, **kwargs):
        super().__init__(*args, **kwargs)
        if instance is None:
            del self.fields["reason"]
            if request_obj is not None:
                # The request's revision: creating a shipment updates the request.
                self.initial.setdefault("expected_revision", request_obj.revision)
                investigator = request_obj.investigator
                contact = " · ".join(v for v in (investigator.email, investigator.phone) if v)
                self.initial.setdefault("ship_to_name", investigator.name)
                self.initial.setdefault("ship_to_institution", request_obj.institution.name)
                self.initial.setdefault("ship_to_address", request_obj.institution.address)
                self.initial.setdefault("ship_to_contact", contact[:160])
            return
        self.initial.setdefault("expected_revision", instance.revision)
        for name in SHIPMENT_VALUES:
            self.initial.setdefault(name, getattr(instance, name))
        if instance.state == Shipment.State.PACKED:
            for name in SHIPMENT_VALUES:
                if name not in PACKED_VALUES:
                    del self.fields[name]

    def values(self):
        result = {}
        for name in SHIPMENT_VALUES:
            if name in self.fields:
                value = self.cleaned_data.get(name)
                result[name] = value if name == "planned_on" else (value or "")
        return result


class ContentsForm(GroupedForm):
    allocations = forms.MultipleChoiceField(
        label="Picked specimens in this shipment",
        widget=forms.CheckboxSelectMultiple,
        required=False,
        help_text="Picked specimens of the request that are not in another shipment.",
    )
    reason = text_field("Note", help_text="Optional, kept in the timeline.")
    expected_revision = revision_field()
    groups = (("", ["reason"]),)  # the allocations are the grid's checkboxes

    def __init__(self, *args, choices=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["allocations"].choices = list(choices)


YES_NO = (("yes", "Yes"), ("no", "No"))


class PackForm(GroupedForm):
    temperature_class = forms.ChoiceField(
        label="Temperature class",
        choices=[("", "Select…"), *Shipment.Temperature.choices],
    )
    refrigerant = forms.ChoiceField(
        label="Refrigerant", choices=[("", "Select…"), *Shipment.Refrigerant.choices]
    )
    dry_ice_kg = forms.DecimalField(
        label="Dry ice",
        max_digits=6,
        decimal_places=2,
        required=False,
        widget=forms.NumberInput(attrs={"unit": "kg"}),
        help_text="Net mass of dry ice, only when the refrigerant is dry ice.",
    )
    classification = forms.ChoiceField(
        label="Classification as declared by the shipper",
        choices=[("", "Select…"), *Shipment.Classification.choices],
        help_text="The software records the shipper's declaration. It does not classify the "
        "shipment or check the declaration.",
    )
    classification_note = line_field(
        "Classification note", help_text="For example the UN number or packing instruction."
    )
    package_count = forms.IntegerField(label="Packages", min_value=1, max_value=99)
    emergency_contact = forms.CharField(
        label="Emergency contact",
        max_length=160,
        help_text="A staff name or duty phone printed on the manifest.",
    )
    logger_ids = forms.CharField(
        label="Temperature loggers", max_length=160, required=False, help_text="Logger IDs."
    )
    storage_instructions = line_field(
        "Storage instructions",
        max_length=500,
        help_text="Printed on the manifest, for example the storage temperature on arrival.",
    )
    include_participant_codes = forms.BooleanField(
        label="Print participant codes on the manifest",
        required=False,
        help_text="Only coded participant identifiers are printed, never collection dates. "
        "Dispatch is refused unless the request's agreement is at data level coded or limited "
        "data set.",
    )
    training_confirmed = forms.ChoiceField(
        label="Packer confirmed current shipping training",
        choices=YES_NO,
        widget=forms.RadioSelect,
        help_text="As stated by the packer; the software does not check training records.",
    )
    checked_by = StaffChoiceField(
        label="Checked by",
        queryset=User.objects.none(),
        required=False,
        empty_label="No second check",
        help_text="Optional second person who checked the packing; never the packer.",
    )
    occurred_at = when_field("Packed at")
    reason = reason_field("Reason", "Kept in the timeline.")
    expected_revision = revision_field()

    groups = (
        (
            "Conditions",
            [("temperature_class", "refrigerant"), "dry_ice_kg", "logger_ids"],
        ),
        (
            "Declaration",
            ["classification", "classification_note", "training_confirmed"],
            "The shipper declares the classification and is responsible for it.",
        ),
        (
            "Manifest",
            [
                "package_count",
                "emergency_contact",
                "storage_instructions",
                "include_participant_codes",
            ],
        ),
        ("Packing", ["checked_by", "occurred_at", "reason"]),
    )

    def __init__(self, *args, actor, shipment, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["checked_by"].queryset = operating_staff(exclude=actor)
        self.initial.setdefault("occurred_at", now_to_the_second())
        self.initial.setdefault("expected_revision", shipment.revision)
        self.initial.setdefault("storage_instructions", shipment.storage_instructions)
        for name in (
            "temperature_class",
            "refrigerant",
            "dry_ice_kg",
            "classification",
            "classification_note",
            "package_count",
            "emergency_contact",
            "logger_ids",
            "include_participant_codes",
        ):
            value = getattr(shipment, name)
            if value not in (None, ""):
                self.initial.setdefault(name, value)

    def clean(self):
        data = super().clean()
        if data.get("refrigerant") == Shipment.Refrigerant.DRY_ICE:
            if data.get("dry_ice_kg") is None and "dry_ice_kg" not in self.errors:
                self.add_error("dry_ice_kg", "Give the net dry ice mass in kg.")
        elif data.get("dry_ice_kg") is not None:
            self.add_error(
                "dry_ice_kg", "Dry ice mass applies only when the refrigerant is dry ice."
            )
        return data


class NotifyForm(GroupedForm):
    kind = forms.ChoiceField(
        label="What happened",
        choices=[
            (ShipmentEvent.Kind.NOTIFIED, "We notified the recipient"),
            (ShipmentEvent.Kind.ACKNOWLEDGED, "The recipient acknowledged"),
        ],
        widget=forms.RadioSelect,
        help_text="Nothing is sent by the software; this records what people did.",
    )
    occurred_at = when_field("When")
    note = text_field("Note", help_text="For example how and to whom.")
    expected_revision = revision_field()
    groups = (("", ["kind", "occurred_at", "note"]),)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial.setdefault("occurred_at", now_to_the_second())


class DispatchForm(GroupedForm):
    courier = forms.CharField(label="Courier", max_length=80, required=False)
    tracking = forms.CharField(label="Tracking number", max_length=80, required=False)
    handed_to = forms.CharField(
        label="Handed to",
        max_length=160,
        required=False,
        help_text="The courier's driver or the person who collected it.",
    )
    occurred_at = when_field("Dispatched at", "Repository local time; not before packing.")
    reason = reason_field("Reason", "Kept in the timeline and each specimen's custody history.")
    expected_revision = revision_field()
    groups = (("Dispatch", [("courier", "tracking"), "handed_to", "occurred_at", "reason"]),)

    def __init__(self, *args, shipment, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial.setdefault("courier", shipment.courier)
        self.initial.setdefault("tracking", shipment.tracking)
        self.initial.setdefault("occurred_at", now_to_the_second())
        self.initial.setdefault("expected_revision", shipment.revision)
        self.initial.setdefault("reason", "Collected by the courier")


RECEIPT_VALUES = (
    "received_at",
    "received_by_name",
    "unpacked_by_name",
    "condition",
    "refrigerant_on_arrival",
    "temperature_note",
    "discrepancies",
    "received_via",
)
NO_EXCEPTION = ("", "No exception")


class ReceiptForm(GroupedForm):
    received_at = when_field(
        "Received at",
        "As the recipient reported it; repository local time. For a lost shipment, when the "
        "loss was confirmed.",
    )
    received_by_name = forms.CharField(
        label="Received by", max_length=160, help_text="The person at the recipient's site."
    )
    unpacked_by_name = forms.CharField(label="Unpacked by", max_length=160, required=False)
    condition = forms.ChoiceField(
        label="Condition",
        choices=[("", "Select…"), *ShipmentReceipt.Condition.choices],
        help_text="As the recipient reported it.",
    )
    refrigerant_on_arrival = forms.ChoiceField(
        label="Refrigerant on arrival",
        choices=[("", "Select…"), *ShipmentReceipt.Refrigerant.choices],
    )
    temperature_note = line_field(
        "Temperature note", help_text="For example the logger reading on arrival."
    )
    discrepancies = text_field(
        "Discrepancies", help_text="Required unless the condition is As expected."
    )
    received_via = forms.ChoiceField(
        label="Reported by", choices=[("", "Select…"), *ShipmentReceipt.Via.choices]
    )
    attachment = attachment_field(help_text="For example the signed receipt or the email.")
    reason = reason_field("Reason", "Kept in the timeline.")
    expected_revision = revision_field()

    def __init__(self, *args, shipment, items, original=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.items = list(items)
        self.initial.setdefault("expected_revision", shipment.revision)
        current = {}
        if original is not None:
            for name in RECEIPT_VALUES:
                value = getattr(original, name)
                if name == "received_at":
                    value = timezone.localtime(value)
                self.initial.setdefault(name, value)
            current = {e.item_id: e for e in original.exceptions.all()}
            self.fields["attachment"].label = "Replace the attachment"
            self.fields["attachment"].help_text += " Leave empty to keep the current file."
            self.fields["reason"].label = "Reason for the correction"
        exception_names = []
        for item in self.items:
            outcome, note = f"outcome_{item.pk}", f"note_{item.pk}"
            self.fields[outcome] = forms.ChoiceField(
                label=f"{item.accession}",
                choices=[NO_EXCEPTION, *ReceiptException.Outcome.choices],
                required=False,
            )
            self.fields[note] = forms.CharField(
                label=f"Note on {item.accession}",
                max_length=500,
                required=False,
                help_text=NO_PARTICIPANT_TEXT,
            )
            if item.pk in current:
                self.initial.setdefault(outcome, current[item.pk].outcome)
                self.initial.setdefault(note, current[item.pk].note)
            exception_names.append((outcome, note))
        self.groups = (
            (
                "Receipt",
                [
                    "received_at",
                    ("received_by_name", "unpacked_by_name"),
                    ("condition", "refrigerant_on_arrival"),
                    "temperature_note",
                    "discrepancies",
                    "received_via",
                ],
            ),
            (
                "Exceptions per item",
                exception_names,
                "One optional outcome per item sent, as the recipient reported it.",
            ),
            ("Attachment", ["attachment"]),
            ("Record", ["reason"]),
        )

    def exceptions(self):
        result = []
        for item in self.items:
            outcome = self.cleaned_data.get(f"outcome_{item.pk}")
            if outcome:
                result.append((item.pk, outcome, self.cleaned_data.get(f"note_{item.pk}") or ""))
        return tuple(result)

    def clean(self):
        data = super().clean()
        for item in self.items:
            if data.get(f"note_{item.pk}") and not data.get(f"outcome_{item.pk}"):
                self.add_error(f"outcome_{item.pk}", "Choose the outcome this note explains.")
        return data


class ShipmentNoteForm(GroupedForm):
    note = text_field("Note", rows=4)
    attachment = attachment_field(help_text="For example a waybill, a photo or logger output.")
    occurred_at = when_field("When")
    groups = (("", ["note", "attachment", "occurred_at"]),)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial.setdefault("occurred_at", now_to_the_second())


# --- returns --------------------------------------------------------------------------------


class ReturnForm(GroupedForm):
    quantity = forms.CharField(
        label="Amount returned",
        max_length=40,
        help_text="Measured on return, in the specimen's unit; 0 when nothing is left. The text "
        "you enter is kept.",
        widget=forms.TextInput(attrs={"inputmode": "decimal", "autocomplete": "off"}),
    )
    thaw_count = forms.IntegerField(
        label="Thaws reported while away",
        min_value=0,
        max_value=999,
        required=False,
        help_text="As the recipient reported it. Leave empty when not known; it is recorded as "
        "not known.",
    )
    condition = text_field("Condition", help_text="As found on return.")
    recipient = StaffChoiceField(
        label="Received by (custodian)",
        queryset=User.objects.none(),
        help_text="The staff member who holds it until it is checked in to storage.",
    )
    occurred_at = when_field("Returned at", "Repository local time; not before dispatch.")
    attachment = attachment_field()
    reason = reason_field("Reason", "Kept in the specimen's history.")
    expected_revision = revision_field()
    groups = (
        ("Return", ["quantity", "thaw_count", "condition", "recipient", "occurred_at"]),
        ("Attachment", ["attachment"]),
        ("Record", ["reason"]),
    )

    def __init__(self, *args, actor, unit, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["quantity"].suffix = unit
        self.fields["recipient"].queryset = operating_staff()
        self.initial.setdefault("recipient", actor.pk)
        self.initial.setdefault("occurred_at", now_to_the_second())


class ReturnReviewForm(GroupedForm):
    decision = forms.ChoiceField(
        label="Decision",
        choices=[
            (ReturnReview.Decision.RELEASE, "Release for use (the hold is released)"),
            (ReturnReview.Decision.KEEP, "Keep on hold"),
        ],
        widget=forms.RadioSelect,
    )
    occurred_at = when_field("Reviewed at")
    reason = reason_field("Reason", "For example the seal and the reported thaws.")
    expected_revision = revision_field()
    groups = (("", ["decision", "occurred_at", "reason"]),)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial.setdefault("occurred_at", now_to_the_second())
