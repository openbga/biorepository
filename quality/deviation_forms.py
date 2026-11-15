"""Report transport; services recheck roles, chronology, revision and review gates."""

from django import forms
from django.utils import timezone

from accounts.roles import require_role
from equipment.forms import DATETIME, GroupedForm
from equipment.models import Excursion
from storage.forms import apply_errors  # noqa: F401 -- shared view error adapter

from . import deviation_services as services
from . import services as audit_services
from .models import Deviation, InventoryAudit

NOT_IDENTIFYING = "Do not enter donor or participant names or other identifying details."


def narrative(label, *, required=False, help_text=""):
    return forms.CharField(
        label=label,
        required=required,
        max_length=services.TEXT_LIMIT,
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text=f"{help_text} {NOT_IDENTIFYING}".strip(),
    )


def reason_field():
    return narrative("Reason", required=True, help_text="Kept with this change in history.")


class DeviationForm(GroupedForm):
    revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    title = forms.CharField(max_length=160, help_text=NOT_IDENTIFYING)
    occurred_at = forms.DateTimeField(
        label="Occurred at",
        widget=DATETIME,
        help_text="When the deviation was observed, in repository local time; not in the future.",
    )
    description = narrative("Description", required=True)
    immediate_action = narrative("Immediate action")
    investigation = narrative(
        "Investigation", help_text="Required for submission; explain if unknown."
    )
    corrective_action = narrative(
        "Corrective action", help_text="Required for submission; explain if no action is needed."
    )
    effectiveness = narrative(
        "Effectiveness", help_text="Required for submission; explain if unknown or not applicable."
    )
    excursion = forms.ModelChoiceField(queryset=None, required=False, label="Excursion")
    inventory_audit = forms.ModelChoiceField(
        queryset=InventoryAudit.objects.none(), required=False, label="Inventory audit"
    )
    reason = reason_field()
    groups = (
        ("Report", ["title", "occurred_at", "description", "immediate_action"]),
        ("Investigation and follow-up", ["investigation", "corrective_action", "effectiveness"]),
        (
            "Related evidence",
            ["excursion", "inventory_audit"],
            "Optional links provide context; they do not change those records or place holds.",
        ),
        ("History", ["reason"]),
    )

    def __init__(self, *args, actor, deviation=None, **kwargs):
        require_role(actor, *services.OPERATE_ROLES)
        initial = kwargs.pop("initial", {}).copy()
        if deviation:
            initial.update({field: getattr(deviation, field) for field in services.EDIT_FIELDS})
            initial.update(
                revision=deviation.revision,
                occurred_at=timezone.localtime(deviation.occurred_at),
                excursion=deviation.excursion_id,
                inventory_audit=deviation.inventory_audit_id,
            )
        else:
            initial.setdefault("occurred_at", timezone.localtime().replace(microsecond=0))
        super().__init__(*args, initial=initial, **kwargs)
        if not deviation:
            del self.fields["revision"]
        self.fields["excursion"].queryset = Excursion.objects.select_related("equipment").order_by(
            "-opened_at", "-pk"
        )
        self.fields["inventory_audit"].queryset = audit_services.audits(actor=actor)

    def report_values(self):
        values = {
            field: self.cleaned_data[field]
            for field in services.EDIT_FIELDS
            if not field.endswith("_id")
        }
        for field in ("excursion", "inventory_audit"):
            source = self.cleaned_data[field]
            values[f"{field}_id"] = source.pk if source else None
        return values


class ActionForm(GroupedForm):
    revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    reason = reason_field()
    groups = (("Reason", ["reason"]),)


class ReviewForm(ActionForm):
    decision = forms.ChoiceField(
        choices=[("", "Choose a decision"), ("close", "Close"), ("return", "Return for revision")]
    )
    groups = (("Review", ["decision", "reason"]),)


class FilterForm(forms.Form):
    status = forms.ChoiceField(
        required=False, choices=[("", "All statuses"), *Deviation.Status.choices]
    )
