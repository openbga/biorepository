"""Storage forms. Transport and input shape only; storage.services repeats every rule."""

import json
import zoneinfo

from django import forms
from django.db.models import Count, Q
from django.utils import timezone

from .models import (
    ContainerType,
    LayoutNode,
    Room,
    Site,
    StorageClass,
    StorageUnit,
)

DATETIME = forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M")
TIMEZONES = sorted(zoneinfo.available_timezones())


def apply_errors(form, error):
    """Show a service ValidationError on the form, under the field it names where it can."""
    if hasattr(error, "error_dict"):
        for name, errors in error.error_dict.items():
            for item in errors:
                target = FIELD_ALIASES.get(name, name)
                form.add_error(target if target in form.fields else None, item)
    else:
        for item in error.error_list:
            form.add_error(None, item)


FIELD_ALIASES = {"position_id": None, "corrects": None}


class GroupedForm:
    groups = ()

    def grouped(self):
        return [
            (legend, [self[name] for name in names if name in self.fields])
            for legend, names in self.groups
            if any(name in self.fields for name in names)
        ]


def reason_field(label="Reason for the change", help_text="Kept in the history with the change."):
    return forms.CharField(
        label=label,
        max_length=2000,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=help_text,
    )


def revision_field(value):
    return forms.IntegerField(widget=forms.HiddenInput, initial=value)


class CodeField(forms.CharField):
    def __init__(self, **kwargs):
        kwargs.setdefault("max_length", 40)
        kwargs.setdefault(
            "help_text",
            "Up to 40 letters, digits, '.', '_' or '-'. Stored in capitals; it can never be "
            "changed or reused.",
        )
        super().__init__(**kwargs)
        self.widget.attrs.update({"autocapitalize": "characters", "spellcheck": "false"})


class NodeChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, node):
        holds = (
            f"{node.box_slots} box slots" if node.box_slots else f"{node.child_slots} node slots"
        )
        where = node.path.replace("/", " / ")
        return f"{node.unit.code} / {where} ({node.get_kind_display()}, {holds})"


class DefinitionForm(GroupedForm, forms.Form):
    """Create form; with an instance it becomes the reasoned edit form (create_only removed)."""

    create_only = ()

    def __init__(self, *args, instance=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance = instance
        if instance is not None:
            for name in self.create_only:
                self.fields.pop(name, None)
            self.fields["reason"] = reason_field()
            self.fields["expected_revision"] = revision_field(instance.revision)
            for name in self.fields:
                if hasattr(instance, name) and name not in ("reason", "expected_revision"):
                    self.initial.setdefault(name, getattr(instance, name))


class SiteForm(DefinitionForm):
    code = CodeField(label="Code")
    name = forms.CharField(label="Name", max_length=160)
    timezone = forms.ChoiceField(
        label="Time zone",
        choices=[(name, name) for name in TIMEZONES],
        initial="America/New_York",
        help_text="IANA time zone of the site, recorded as a setup fact. Times in forms use the "
        "repository's time zone.",
    )
    create_only = ("code",)
    groups = (("Site", ["code", "name", "timezone"]), ("Change", ["reason"]))


class RoomForm(DefinitionForm):
    site = forms.ModelChoiceField(label="Site", queryset=Site.objects.filter(is_active=True))
    code = CodeField(label="Code", help_text="Unique within the site; never changed or reused.")
    name = forms.CharField(label="Name", max_length=160)
    create_only = ("site", "code")
    groups = (("Room", ["site", "code", "name"]), ("Change", ["reason"]))


class UnitForm(DefinitionForm):
    room = forms.ModelChoiceField(
        label="Room",
        queryset=Room.objects.filter(is_active=True).select_related("site"),
    )
    code = CodeField(label="Code")
    name = forms.CharField(label="Name", max_length=160)
    storage_class = forms.ModelChoiceField(
        label="Storage class", queryset=StorageClass.objects.filter(is_active=True)
    )
    technology = forms.ChoiceField(label="Technology", choices=StorageUnit.Technology.choices)
    nominal_temperature = forms.DecimalField(
        label="Nominal temperature (°C)",
        max_digits=6,
        decimal_places=2,
        min_value=-273.15,
        help_text="The unit's nominal set point as stated, e.g. -160.00. A definition, not a "
        "measurement; it never blocks storing an item.",
    )
    create_only = ("room", "code", "technology")
    groups = (
        ("Storage unit", ["room", "code", "name", "technology"]),
        ("Conditions", ["storage_class", "nominal_temperature"]),
        ("Change", ["reason"]),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "room" in self.fields:
            self.fields["room"].label_from_instance = lambda r: (
                f"{r.site.code} / {r.code} ({r.name})"
            )
        if self.instance is not None:
            self.initial["storage_class"] = self.instance.storage_class_id
            # A class deactivated since stays selectable for this unit, so a rename still works.
            self.fields["storage_class"].queryset = StorageClass.objects.filter(
                Q(is_active=True) | Q(pk=self.instance.storage_class_id)
            )


class NodeForm(DefinitionForm):
    unit = forms.ModelChoiceField(
        label="Storage unit", queryset=StorageUnit.objects.filter(is_active=True)
    )
    parent = NodeChoiceField(
        label="Inside",
        queryset=LayoutNode.objects.filter(is_active=True, child_slots__gt=0).select_related(
            "unit"
        ),
        required=False,
        empty_label="Top level of the unit",
        help_text="A shelf holds racks; a rack, tower or canister holds shelves.",
    )
    kind = forms.ChoiceField(label="Kind", choices=LayoutNode.Kind.choices)
    code = CodeField(label="Code")
    name = forms.CharField(label="Name", max_length=160)
    slot = forms.IntegerField(
        label="Slot", min_value=1, help_text="Its numbered place in the unit or in its parent."
    )
    child_slots = forms.IntegerField(
        label="Node slots",
        min_value=0,
        max_value=100,
        initial=0,
        help_text="How many shelves or racks it holds. Give node slots or box slots, not both.",
    )
    box_slots = forms.IntegerField(
        label="Box slots",
        min_value=0,
        max_value=100,
        initial=0,
        help_text="How many boxes it holds directly.",
    )
    create_only = ("unit", "parent", "kind", "code", "slot", "child_slots", "box_slots")
    groups = (
        ("Where", ["unit", "parent", "slot"]),
        ("Rack or shelf", ["kind", "code", "name"]),
        ("Holds", ["child_slots", "box_slots"]),
        ("Change", ["reason"]),
    )

    def clean(self):
        data = super().clean()
        unit, parent = data.get("unit"), data.get("parent")
        if unit and parent and parent.unit_id != unit.pk:
            self.add_error("parent", "Choose a rack or shelf in the same storage unit.")
        if "child_slots" in self.fields:
            nodes, boxes = data.get("child_slots") or 0, data.get("box_slots") or 0
            if (nodes > 0) == (boxes > 0):
                self.add_error("box_slots", "Give node slots or box slots: one of them, not both.")
        return data


class BoxForm(DefinitionForm):
    node = NodeChoiceField(
        label="Rack or shelf",
        queryset=LayoutNode.objects.filter(is_active=True, box_slots__gt=0).select_related("unit"),
    )
    slot = forms.IntegerField(label="Box slot", min_value=1)
    container_type = forms.ModelChoiceField(
        label="Container type",
        queryset=ContainerType.objects.filter(is_active=True),
        help_text="The box grid. Every position is created with the box; the grid never changes.",
    )
    code = CodeField(label="Code")
    name = forms.CharField(
        label="Name", max_length=160, required=False, help_text="Optional; the code by default."
    )
    create_only = ("node", "slot", "container_type", "code")
    groups = (
        ("Where", ["node", "slot"]),
        ("Box", ["container_type", "code", "name"]),
        ("Change", ["reason"]),
    )


class ClassForm(DefinitionForm):
    code = CodeField(label="Code")
    name = forms.CharField(label="Name", max_length=160)
    description = forms.CharField(
        label="Description", max_length=500, required=False, widget=forms.Textarea({"rows": 3})
    )
    is_active = forms.BooleanField(
        label="Active",
        required=False,
        help_text="Inactive classes stay on existing units but cannot be chosen for new ones.",
    )
    create_only = ("code",)
    groups = (
        ("Storage class", ["code", "name", "description", "is_active"]),
        ("Change", ["reason"]),
    )

    def __init__(self, *args, instance=None, **kwargs):
        super().__init__(*args, instance=instance, **kwargs)
        if instance is None:
            self.fields.pop("is_active")


class ContainerTypeForm(DefinitionForm):
    code = CodeField(
        label="Code",
        help_text="Up to 40 letters, digits, '.', '_' or '-'. An existing code adds a new version.",
    )
    name = forms.CharField(label="Name", max_length=160)
    rows = forms.IntegerField(label="Rows", min_value=1, max_value=100)
    columns = forms.IntegerField(label="Columns", min_value=1, max_value=100)
    label_scheme = forms.ChoiceField(
        label="Position labels", choices=ContainerType.LabelScheme.choices
    )
    fill_order = forms.ChoiceField(
        label="Numbering order",
        choices=ContainerType.FillOrder.choices,
        help_text="How positions are numbered from the labelled top-left corner (A1 or 1).",
    )
    is_active = forms.BooleanField(
        label="Active",
        required=False,
        help_text="Inactive types stay on existing boxes but cannot be chosen for new boxes.",
    )
    create_only = ("code",)
    groups = (
        ("Container type", ["code", "name", "is_active"]),
        ("Grid", ["rows", "columns", "label_scheme", "fill_order"]),
        ("Change", ["reason"]),
    )

    def __init__(self, *args, instance=None, used=False, **kwargs):
        super().__init__(*args, instance=instance, **kwargs)
        if instance is None:
            self.fields.pop("is_active")
        elif used:
            for name in ("rows", "columns", "label_scheme", "fill_order"):
                self.fields.pop(name)


class JSONTextField(forms.CharField):
    widget = forms.Textarea

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.widget.attrs.update({"rows": 18, "spellcheck": "false", "class": "font-monospace"})

    def prepare_value(self, value):
        if isinstance(value, (dict, list)):
            return json.dumps(value, indent=2, ensure_ascii=False)
        return value

    def to_python(self, value):
        value = super().to_python(value)
        if not value:
            return value
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            message = f"Not valid JSON: {exc.msg} (line {exc.lineno})."
            raise forms.ValidationError(message) from None


class TemplateForm(DefinitionForm):
    code = CodeField(
        label="Code",
        help_text="Up to 40 letters, digits, '.', '_' or '-'. An existing code adds a new version.",
    )
    name = forms.CharField(label="Name", max_length=160)
    definition = JSONTextField(
        label="Layout recipe",
        help_text="JSON: storage class code, technology, nominal temperature as a quoted decimal "
        "and the tree of racks, shelves and boxes (see the page help).",
    )
    is_active = forms.BooleanField(
        label="Active", required=False, help_text="Inactive templates cannot be applied."
    )
    create_only = ("code",)
    groups = (
        ("Template", ["code", "name", "is_active"]),
        ("Layout", ["definition"]),
        ("Change", ["reason"]),
    )

    def __init__(self, *args, instance=None, used=False, **kwargs):
        super().__init__(*args, instance=instance, **kwargs)
        if instance is None:
            self.fields.pop("is_active")
        elif used:
            self.fields.pop("definition")


class MoveForm(GroupedForm, forms.Form):
    target = forms.ChoiceField(label="Move to")
    target_slot = forms.IntegerField(
        label="Slot",
        min_value=1,
        help_text="A free numbered slot in the destination (its page lists empty slots).",
    )
    occurred_at = forms.DateTimeField(
        label="Moved at",
        widget=DATETIME,
        help_text="Repository local time. Not before the last recorded movement of anything "
        "inside it.",
    )
    reason = reason_field(
        "Reason for the move", "Kept in the history of the container and of every item inside it."
    )
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    expected_gate_revision = forms.IntegerField(widget=forms.HiddenInput)
    groups = (("Destination", ["target", "target_slot"]), ("Record", ["occurred_at", "reason"]))

    def __init__(self, *args, kind, obj, gate_revision, **kwargs):
        super().__init__(*args, **kwargs)
        self.kind = kind
        self.initial.setdefault(
            "occurred_at", timezone.localtime().replace(second=0, microsecond=0)
        )
        self.initial.setdefault("expected_revision", obj.revision)
        self.initial.setdefault("expected_gate_revision", gate_revision)
        choices = []
        if kind == "box":
            for node in (
                LayoutNode.objects.filter(is_active=True, box_slots__gt=0, unit__is_active=True)
                .annotate(used=Count("boxes", filter=Q(boxes__is_active=True)))
                .select_related("unit")
                .order_by("unit__code", "path")
            ):
                free = node.box_slots - node.used
                choices.append(
                    (
                        f"node:{node.pk}",
                        f"{node.unit.code} / {node.path.replace('/', ' / ')} "
                        f"({free} of {node.box_slots} box slots free)",
                    )
                )
        else:
            for unit in StorageUnit.objects.filter(is_active=True).order_by("code"):
                choices.append((f"unit:{unit.pk}", f"{unit.code}: top level"))
            for node in (
                LayoutNode.objects.filter(is_active=True, child_slots__gt=0, unit__is_active=True)
                .exclude(pk=obj.pk)
                .exclude(path__startswith=obj.path + "/")
                .annotate(used=Count("children", filter=Q(children__is_active=True)))
                .select_related("unit")
                .order_by("unit__code", "path")
            ):
                free = node.child_slots - node.used
                choices.append(
                    (
                        f"node:{node.pk}",
                        f"{node.unit.code} / {node.path.replace('/', ' / ')} "
                        f"({node.get_kind_display()}, {free} of {node.child_slots} node slots "
                        "free)",
                    )
                )
        self.fields["target"].choices = [("", "Choose…"), *choices]

    def clean_target(self):
        kind, _, pk = self.cleaned_data["target"].partition(":")
        return kind, pk


class BlockForm(GroupedForm, forms.Form):
    reason = reason_field()
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    groups = (("", ["reason"]),)


class RetireForm(GroupedForm, forms.Form):
    reason = reason_field(
        "Reason for retirement", "Kept in the history. The record and its codes stay."
    )
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    groups = (("", ["reason"]),)


class TemplateApplyForm(GroupedForm, forms.Form):
    room = forms.ModelChoiceField(
        label="Room", queryset=Room.objects.filter(is_active=True).select_related("site")
    )
    unit_code = CodeField(
        label="Unit code",
        help_text="Prefix of every generated code: rack T01 in unit VP-02 becomes VP-02-T01.",
    )
    unit_name = forms.CharField(label="Unit name", max_length=160)
    groups = (("New storage unit", ["room", "unit_code", "unit_name"]),)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["room"].label_from_instance = lambda r: f"{r.site.code} / {r.code} ({r.name})"
