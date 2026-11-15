"""Training form transport; services enforce all mutation rules again."""

from django import forms

from accounts.roles import require_role
from equipment.forms import DATE, GroupedForm, apply_errors  # noqa: F401

from . import training_services as services
from .models import TrainingRecord

NOT_IDENTIFYING = "Do not enter donor or participant names or identifying details."


def narrative(label, *, required=False, help_text=""):
    return forms.CharField(
        label=label,
        required=required,
        max_length=4000,
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text=f"{help_text} {NOT_IDENTIFYING}".strip(),
    )


def reason_field():
    return narrative("Reason", required=True, help_text="Kept with this change in history.")


class TrainingForm(GroupedForm):
    revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    trainee = forms.ModelChoiceField(
        queryset=None, help_text="An active account with an application role."
    )
    topic = forms.CharField(max_length=160, help_text=NOT_IDENTIFYING)
    procedure_reference = forms.CharField(
        label="Procedure / version reference",
        required=False,
        max_length=240,
        help_text="Optional reference text; does not link to or control a procedure.",
    )
    due_on = forms.DateField(label="Assignment due", required=False, widget=DATE)
    completed_on = forms.DateField(label="Completed on", widget=DATE)
    trainer_provider = forms.CharField(label="Trainer / provider", max_length=240)
    completion_evidence = narrative("Completion evidence", required=True)
    competency_assessment = narrative(
        "Competency assessment",
        help_text="Optional human assessment; the software does not certify competence.",
    )
    renewal_due = forms.DateField(
        label="Renewal due",
        required=False,
        widget=DATE,
        help_text=(
            "Optional repository-chosen date, on or after completion. No fixed interval is applied."
        ),
    )
    reason = reason_field()
    groups = (
        ("Assignment", ["trainee", "topic", "procedure_reference", "due_on"]),
        (
            "Completion",
            [
                "completed_on",
                "trainer_provider",
                "completion_evidence",
                "competency_assessment",
                "renewal_due",
            ],
        ),
        ("History", ["reason"]),
    )

    def __init__(self, *args, actor, training=None, action="create", **kwargs):
        require_role(actor, *services.MANAGE_ROLES)
        initial = kwargs.pop("initial", {}).copy()
        if training:
            initial.update({field: getattr(training, field) for field in services.EDIT_FIELDS})
            initial["revision"] = training.revision
        super().__init__(*args, initial=initial, **kwargs)
        self.action = action
        if action == "create":
            self.fields["trainee"].queryset = services.eligible_trainees()
            del self.fields["revision"]
        else:
            del self.fields["trainee"]
        allowed = {
            "create": services.ASSIGNMENT_FIELDS,
            "edit": services.ASSIGNMENT_FIELDS,
            "complete": services.COMPLETION_FIELDS,
            "correct": services.EDIT_FIELDS,
            "cancel": (),
        }[action]
        self.value_fields = allowed
        for field in services.EDIT_FIELDS:
            if field not in allowed:
                del self.fields[field]

    def record_values(self):
        return {field: self.cleaned_data[field] for field in self.value_fields}


class FilterForm(forms.Form):
    status = forms.ChoiceField(
        required=False, choices=[("", "All statuses"), *TrainingRecord.Status.choices]
    )
    due = forms.ChoiceField(
        required=False,
        choices=[
            ("", "All dates"),
            ("assignment_overdue", "Assignment overdue"),
            ("renewal_overdue", "Renewal overdue"),
            ("next_30_days", "Due in next 30 days"),
        ],
    )
