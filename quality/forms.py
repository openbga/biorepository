"""Bounded manual observations; nothing is prefilled from expected accessions."""

from django import forms

from accounts.roles import require_role
from equipment.forms import GroupedForm
from storage.forms import apply_errors  # noqa: F401 -- shared view error adapter

from . import services
from .models import InventoryAudit


def reason_field(label="Reason"):
    return forms.CharField(
        label=label,
        max_length=services.TEXT_LIMIT,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Kept in history. Do not enter identifying information.",
    )


class AuditCreateForm(GroupedForm):
    box = forms.ModelChoiceField(queryset=None, label="Box")
    reason = reason_field("Reason for the audit")
    supersedes = forms.ModelChoiceField(
        queryset=InventoryAudit.objects.none(),
        required=False,
        label="Earlier audit",
        help_text="Optional: a finalized audit of this same box.",
    )
    groups = (("Audit", ["box", "reason", "supersedes"]),)

    def __init__(self, *args, actor, **kwargs):
        require_role(actor, *services.OPERATE_ROLES)
        super().__init__(*args, **kwargs)
        self.fields["box"].queryset = services.available_boxes(actor=actor)
        self.fields["supersedes"].queryset = services.audits(actor=actor).filter(
            finalization__isnull=False
        )


class CompleteForm(GroupedForm):
    reason = reason_field("Completion reason")
    discrepancy_explanation = forms.CharField(
        label="Discrepancy explanation",
        required=False,
        max_length=services.TEXT_LIMIT,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=(
            "Required if any position differs. Evidence only; corrections use ordinary workflows."
        ),
    )
    groups = (("Completion", ["reason", "discrepancy_explanation"]),)

    def __init__(self, *args, audit, **kwargs):
        super().__init__(*args, **kwargs)
        self.audit = audit
        for index, position in enumerate(audit.positions):
            self.fields[f"state_{index}"] = forms.ChoiceField(
                label=f"{position['label']}: observed contents",
                choices=[("", "Not yet observed"), ("empty", "Empty"), ("accession", "Accession")],
            )
            self.fields[f"accession_{index}"] = forms.CharField(
                label=f"{position['label']}: observed accession",
                required=False,
                max_length=services.ACCESSION_LIMIT,
                widget=forms.TextInput(attrs={"autocomplete": "off", "spellcheck": "false"}),
            )

    def clean(self):
        cleaned = super().clean()
        observed, seen = {}, set()
        for index, position in enumerate(self.audit.positions):
            state = cleaned.get(f"state_{index}")
            value = cleaned.get(f"accession_{index}", "")
            if state == "accession" and not value:
                self.add_error(f"accession_{index}", "Enter the observed accession.")
            if state == "empty" and value:
                self.add_error(
                    f"accession_{index}", "Clear the accession when marking the position empty."
                )
            if state == "accession" and value:
                if value.casefold() in seen:
                    self.add_error(
                        f"accession_{index}",
                        "This accession is already recorded in another position.",
                    )
                seen.add(value.casefold())
            observed[position["id"]] = (
                value if state == "accession" else "" if state == "empty" else None
            )
        cleaned["observations"] = observed
        return cleaned

    def position_rows(self):
        return [
            (position, self[f"state_{i}"], self[f"accession_{i}"])
            for i, position in enumerate(self.audit.positions)
        ]


class CancelForm(GroupedForm):
    reason = reason_field("Reason for cancellation")
    groups = (("Cancellation", ["reason"]),)


class FilterForm(forms.Form):
    status = forms.ChoiceField(
        required=False,
        choices=[
            ("", "All statuses"),
            ("open", "Open"),
            ("completed", "Completed"),
            ("cancelled", "Cancelled"),
        ],
    )
