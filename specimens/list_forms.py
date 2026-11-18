"""Specimen list filters, catalogue edit form, import upload/commit forms and label selection.

Transport and input shape only: specimens.queries and specimens.services repeat every rule. A
filter form that does not validate yields no rows (never all rows).
"""

import uuid

from django import forms
from django.core.exceptions import ValidationError

from accounts.roles import can_approve_disposal
from core.grids import GridRequest
from storage.forms import GroupedForm
from storage.models import Box

from .importing import MAX_BYTES
from .labels import MAX_LABELS
from .models import (
    NO_IDENTIFYING_TEXT,
    Collection,
    DisposalRequest,
    Participant,
    QuantityUnit,
    Specimen,
    SpecimenContainerType,
    SpecimenType,
)
from .queries import DISPOSAL_STATES, disposal_queryset

PAGE_SIZES = [(size, f"{size} rows") for size in (25, 50, 100)]
YES_NO = [("", "Any"), ("yes", "Yes"), ("no", "No")]
DISPOSAL_STATE_LABELS = {
    "pending": "Awaiting approval",
    "approved": "Approved, not yet disposed",
    "stale": "Out of date",
    "rejected": "Rejected",
    "cancelled": "Cancelled",
    "executed": "Disposed",
}


class ListFilter(forms.Form):
    q = forms.CharField(label="Search", required=False, max_length=200)
    per_page = forms.TypedChoiceField(
        label="Rows per page", choices=PAGE_SIZES, coerce=int, required=False
    )
    sort = forms.CharField(required=False, widget=forms.HiddenInput())


class CodeChoiceField(forms.CharField):
    """A code typed by the user, resolved to one catalogue record (exact, case-insensitive)."""

    def __init__(self, model, noun, **kwargs):
        self.model, self.noun = model, noun
        kwargs.setdefault("required", False)
        kwargs.setdefault("max_length", 40)
        super().__init__(**kwargs)
        self.widget.attrs.update({"autocapitalize": "characters", "spellcheck": "false"})

    def clean(self, value):
        code = super().clean(value).strip().upper()
        if not code:
            return None
        obj = self.model.objects.filter(code=code).first()
        if obj is None:
            raise ValidationError(f"No {self.noun} has code {code}.", code="invalid")
        return obj


class SpecimenFilter(ListFilter):
    """Query keys equal specimens.queries.SPECIMEN_FILTERS, so cleaned data is passed as is."""

    status = forms.ChoiceField(
        label="Status", choices=[("", "Any status"), *Specimen.Status.choices], required=False
    )
    type = forms.ModelChoiceField(
        label="Specimen type",
        queryset=SpecimenType.objects.order_by("code"),
        required=False,
        empty_label="Any type",
    )
    held = forms.ChoiceField(
        label="On hold",
        choices=YES_NO,
        required=False,
        help_text="Any active hold: disposal, use or all-use.",
    )
    container = forms.ModelChoiceField(
        label="Specimen container",
        queryset=SpecimenContainerType.objects.order_by("code"),
        required=False,
        empty_label="Any container",
    )
    participant = CodeChoiceField(Participant, "participant", label="Participant code")
    collection = forms.ModelChoiceField(
        label="Collection",
        queryset=Collection.objects.order_by("code"),
        required=False,
        empty_label="Any collection",
    )
    unit = forms.ModelChoiceField(
        label="Unit",
        queryset=QuantityUnit.objects.order_by("code"),
        required=False,
        empty_label="Any unit",
    )
    box = CodeChoiceField(Box, "box", label="Box code")
    checked_out = forms.ChoiceField(label="Checked out", choices=YES_NO, required=False)
    placed = forms.ChoiceField(label="In storage", choices=YES_NO, required=False)

    INLINE = ("status", "type", "held")
    MORE = ("container", "participant", "collection", "unit", "box", "checked_out", "placed")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["q"].widget = forms.TextInput(
            attrs={"placeholder": "Specimen ID, participant or collection code"}
        )

    def filters(self):
        """The cleaned filters for specimens.queries.specimen_queryset."""
        data = self.cleaned_data
        return {
            name: data[name]
            for name in ("q", *self.INLINE, *self.MORE)
            if data.get(name) not in (None, "")
        }


class DisposalFilter(ListFilter):
    state = forms.ChoiceField(
        label="State",
        choices=[("", "Any state"), *((s, DISPOSAL_STATE_LABELS[s]) for s in DISPOSAL_STATES)],
        required=False,
    )
    mine = forms.BooleanField(label="Requested by me", required=False)

    def __init__(self, *args, approver=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["q"].widget = forms.TextInput(attrs={"placeholder": "Specimen ID"})
        if approver:
            self.fields["awaiting_me"] = forms.BooleanField(
                label="Awaiting my approval", required=False
            )

    def clean(self):
        # An unchecked personal filter allows every requester. Omit it from the applied chips.
        return {name: value for name, value in super().clean().items() if value is not False}

    def filters(self):
        data = self.cleaned_data
        filters = {}
        for name in ("q", "state", "mine", "awaiting_me"):
            if data.get(name):
                filters[name] = data[name]
        return filters


DISPOSAL_COLUMNS = {"recorded_at": "recorded_at", "specimen": "specimen__accession"}


def disposal_selection(actor, params):
    """Bind the register and download to the same actor, filters and total ordering."""
    grid = GridRequest.parse(params, DISPOSAL_COLUMNS, "-recorded_at")
    form = DisposalFilter(params, approver=can_approve_disposal(actor))
    if set(params) - (form.fields.keys() | {"page"}):
        form.add_error(None, "This filter is unavailable. Clear the filters and try again.")
    if params.get("sort", grid.sort) != grid.sort:
        form.add_error(None, "Choose Request or Specimen to sort.")
    rows = (
        disposal_queryset({**form.filters(), "actor": actor})
        if form.is_valid()
        else DisposalRequest.objects.none()
    )
    return grid, form, grid.ordered(rows, DISPOSAL_COLUMNS)


class CatalogueFilter(ListFilter):
    active = forms.ChoiceField(
        label="Status",
        choices=[("", "Active and inactive"), ("true", "Active"), ("false", "Inactive")],
        required=False,
    )

    def __init__(self, *args, kind="participant", **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["q"].widget = forms.TextInput(
            attrs={"placeholder": "Code" if kind == "participant" else "Code or name"}
        )

    def filters(self):
        return {k: v for k, v in self.cleaned_data.items() if k in ("q", "active") and v}


# --- catalogue edit form --------------------------------------------------------------------

PARTICIPANT_HELP = (
    "A coded identifier such as P-000123. Never a name, record number, birth date or any other "
    "identifying information."
)
CODE_HELP = "Up to 40 letters, digits, '.', '_' or '-'. Stored in capitals."
UNIT_CODE_HELP = "Up to 16 characters, starting with a letter. Unit codes keep their case (mL, uL)."


class CatalogueForm(GroupedForm, forms.Form):
    """Create or edit one specimen catalogue entry. Fields follow services.CATALOGUE_FIELDS."""

    def __init__(self, *args, kind, instance=None, in_use=False, **kwargs):
        self.kind, self.instance, self.in_use = kind, instance, in_use
        if instance is not None and "initial" not in kwargs:
            kwargs["initial"] = self.initial_for(kind, instance)
        super().__init__(*args, **kwargs)
        fields = {}
        if kind == "unit":
            fields["code"] = forms.CharField(label="Code", max_length=16, help_text=UNIT_CODE_HELP)
        else:
            fields["code"] = forms.CharField(
                label="Code",
                max_length=40,
                help_text=PARTICIPANT_HELP if kind == "participant" else CODE_HELP,
            )
            fields["code"].widget.attrs["autocapitalize"] = "characters"
        fields["code"].widget.attrs["spellcheck"] = "false"
        if kind != "participant":
            fields["name"] = forms.CharField(label="Name", max_length=80 if kind == "unit" else 160)
        if kind == "container_type":
            fields["nominal_capacity"] = forms.CharField(
                label="Nominal capacity",
                max_length=40,
                required=False,
                help_text="The vessel's stated capacity, as an exact decimal. Leave blank if none.",
            )
            fields["nominal_capacity"].widget.attrs["inputmode"] = "decimal"
            fields["capacity_unit"] = forms.ModelChoiceField(
                label="Capacity unit",
                queryset=QuantityUnit.objects.filter(is_active=True).order_by("code"),
                required=False,
                empty_label="No capacity",
            )
        if kind == "unit":
            fields["dimension"] = forms.ChoiceField(
                label="Dimension", choices=[("", "Choose"), *QuantityUnit.Dimension.choices]
            )
            fields["fractional"] = forms.BooleanField(
                label="Fractional amounts allowed",
                required=False,
                initial=True,
                help_text="Clear for whole-number units such as count.",
            )
        if instance is not None:
            fields["is_active"] = forms.BooleanField(
                label="Active",
                required=False,
                help_text="An inactive entry stays on existing specimens but is not offered for "
                "new ones.",
            )
        fields["reason"] = forms.CharField(
            label="Reason for the change" if instance else "Reason",
            max_length=2000,
            required=instance is not None,
            widget=forms.Textarea(attrs={"rows": 3}),
            help_text="Kept in the history with the change. " + NO_IDENTIFYING_TEXT
            if instance
            else "Optional. Kept in the history; blank reads “Initial definition”.",
        )
        fields["expected_revision"] = forms.IntegerField(widget=forms.HiddenInput, required=False)
        self.fields.update(fields)
        if in_use:
            for name in self.frozen_fields():
                self.fields[name].disabled = True

    def frozen_fields(self):
        return ["code", *(("dimension", "fractional") if self.kind == "unit" else ())]

    @staticmethod
    def initial_for(kind, obj):
        data = {"code": obj.code, "is_active": obj.is_active, "expected_revision": obj.revision}
        for name in ("name", "dimension", "fractional"):
            if hasattr(obj, name):
                data[name] = getattr(obj, name)
        if kind == "container_type":
            if obj.nominal_capacity is not None:
                data["nominal_capacity"] = format(obj.nominal_capacity.normalize(), "f")
            data["capacity_unit"] = obj.capacity_unit_id
        return data

    @property
    def groups(self):
        details = ("code", "name", "nominal_capacity", "capacity_unit", "dimension", "fractional")
        return (("Details", (*details, "is_active")), ("Reason", ("reason",)))

    def values(self):
        """The values for services.save_specimen_catalogue (frozen fields left out)."""
        data = self.cleaned_data
        skip = set(self.frozen_fields()) if self.in_use else set()
        values = {}
        for name in ("code", "name", "nominal_capacity", "capacity_unit", "dimension"):
            if name in self.fields and name not in skip:
                value = data.get(name)
                if name == "capacity_unit":
                    value = value.pk if value else None
                elif name == "nominal_capacity":
                    value = (value or "").strip() or None
                values[name] = value
        if "fractional" in self.fields and "fractional" not in skip:
            values["fractional"] = bool(data.get("fractional"))
        if "is_active" in self.fields:
            values["is_active"] = bool(data.get("is_active"))
        return values


# --- imports --------------------------------------------------------------------------------


class ImportUploadForm(GroupedForm, forms.Form):
    groups = (("File", ("file",)),)

    file = forms.FileField(
        label="CSV file",
        help_text="UTF-8 CSV with the template's header row, at most 2 MiB and 1,000 rows.",
        widget=forms.ClearableFileInput(attrs={"accept": ".csv,text/csv"}),
    )

    def clean_file(self):
        upload = self.cleaned_data["file"]
        if upload.size > MAX_BYTES:
            raise ValidationError("The file is larger than 2 MiB. Split it into smaller files.")
        if not upload.name.lower().endswith(".csv"):
            raise ValidationError("Upload a .csv file.")
        return upload


class ImportCommitForm(forms.Form):
    reason = forms.CharField(
        label="Reason",
        max_length=2000,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Kept in each specimen's history, for example the shipment or batch. "
        + NO_IDENTIFYING_TEXT,
    )
    expected_sha256 = forms.CharField(widget=forms.HiddenInput, max_length=64)


# --- labels ---------------------------------------------------------------------------------


def label_ids(values):
    """Validated specimen ids from repeated `id` parameters, in order (repeats are reprints)."""
    if not values:
        raise ValidationError("Choose at least one specimen to print a label for.")
    if len(values) > MAX_LABELS:
        raise ValidationError(f"Print at most {MAX_LABELS} labels at a time.")
    ids = []
    for value in values:
        try:
            ids.append(uuid.UUID(str(value)))
        except ValueError:
            raise ValidationError("A label request names a specimen that does not exist.") from None
    return ids
