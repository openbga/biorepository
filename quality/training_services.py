"""Training record transitions, revision checks and evidence history commit together."""

from datetime import date, datetime

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from accounts.models import User
from accounts.roles import ROLES, require_role
from core.audit import record_event

from .models import TrainingEvent, TrainingRecord

READ_ROLES = ROLES
MANAGE_ROLES = ("admin", "manager")
TEXT_LIMIT = 4000
ASSIGNMENT_FIELDS = ("topic", "procedure_reference", "due_on")
COMPLETION_FIELDS = (
    "completed_on",
    "trainer_provider",
    "completion_evidence",
    "competency_assessment",
    "renewal_due",
)
EDIT_FIELDS = (*ASSIGNMENT_FIELDS, *COMPLETION_FIELDS)
SNAPSHOT_FIELDS = (
    "trainee_id",
    *EDIT_FIELDS,
    "status",
    "created_at",
    "created_by_id",
    "updated_at",
    "updated_by_id",
    "revision",
)


def eligible_trainees():
    # Application role membership, deliberately independent of Django is_staff.
    return (
        User.objects.filter(is_active=True, groups__name__in=ROLES).distinct().order_by("username")
    )


def training_records(*, actor):
    require_role(actor, *READ_ROLES)
    return TrainingRecord.objects.select_related("trainee", "created_by", "updated_by")


def get_training(*, actor, training_id):
    return training_records(actor=actor).get(pk=training_id)


def _text(value, field, *, required=True, limit=TEXT_LIMIT):
    if not isinstance(value, str):
        raise ValidationError({field: "Enter text."})
    value = value.strip()
    if required and not value:
        raise ValidationError({field: "This field is required."})
    if len(value) > limit:
        raise ValidationError({field: f"Enter at most {limit} characters."})
    return value


def _snapshot(training):
    return {
        field: value.isoformat() if isinstance(value := getattr(training, field), date) else value
        for field in SNAPSHOT_FIELDS
    }


def _apply(training, values, allowed):
    if not isinstance(values, dict) or set(values) - set(allowed):
        raise ValidationError(
            "Choose only fields available for this action. The trainee cannot change."
        )
    for field, value in values.items():
        if field in {
            "topic",
            "procedure_reference",
            "trainer_provider",
            "completion_evidence",
            "competency_assessment",
        }:
            value = _text(
                value,
                field,
                required=field == "topic"
                or (
                    training.status == TrainingRecord.Status.COMPLETED
                    and field in {"trainer_provider", "completion_evidence"}
                ),
                limit=160
                if field == "topic"
                else 240
                if field in {"procedure_reference", "trainer_provider"}
                else TEXT_LIMIT,
            )
        elif value is not None and (not isinstance(value, date) or isinstance(value, datetime)):
            raise ValidationError({field: "Enter a date."})
        setattr(training, field, value)
    if training.status == TrainingRecord.Status.COMPLETED:
        if not training.completed_on:
            raise ValidationError({"completed_on": "Enter the completion date."})
        if training.completed_on > timezone.localdate():
            raise ValidationError({"completed_on": "The completion date cannot be in the future."})
        for field in ("trainer_provider", "completion_evidence"):
            _text(getattr(training, field), field)
    if training.renewal_due and (
        not training.completed_on or training.renewal_due < training.completed_on
    ):
        raise ValidationError({"renewal_due": "Renewal due must be on or after completion."})
    training.full_clean()


def _locked(training_id, expected_revision):
    training = TrainingRecord.objects.select_for_update().get(pk=training_id)
    if type(expected_revision) is not int or training.revision != expected_revision:
        raise ValidationError(
            "This training record changed while you were working. Your input is retained here. "
            "Reload the record and review its latest values before starting again.",
            code="stale_revision",
        )
    return training


def _state(training, expected):
    if training.status not in expected:
        raise ValidationError(
            "This action is unavailable in the record's current status.", code="invalid_transition"
        )


def _record(training, actor, kind, reason, before):
    after = _snapshot(training)
    changes = {
        field: [before.get(field), value]
        for field, value in after.items()
        if field not in before or before[field] != value
    }
    TrainingEvent.objects.create(
        training=training,
        actor=actor,
        kind=kind,
        revision=training.revision,
        recorded_at=training.updated_at,
        reason=reason,
        changes=changes,
    )
    record_event(
        actor=actor,
        action=f"quality.training_{kind}",
        obj=training,
        before={field: pair[0] for field, pair in changes.items()} if before else {},
        after={field: pair[1] for field, pair in changes.items()},
        reason=reason,
    )


@transaction.atomic
def create_training(*, actor, trainee_id, values, reason):
    require_role(actor, *MANAGE_ROLES)
    reason = _text(reason, "reason")
    if type(trainee_id) is not int:
        raise ValidationError({"trainee": "Choose an active staff account."})
    # Lock the account so deactivation cannot race creation on the same row.
    trainee = User.objects.select_for_update().filter(pk=trainee_id).first()
    if not trainee or not eligible_trainees().filter(pk=trainee_id).exists():
        raise ValidationError(
            {"trainee": "Choose an active staff account with an application role."}
        )
    now = timezone.now()
    training = TrainingRecord(
        trainee=trainee,
        created_by=actor,
        updated_by=actor,
        created_at=now,
        updated_at=now,
    )
    _apply(training, values, ASSIGNMENT_FIELDS)
    training.save(force_insert=True)
    _record(training, actor, TrainingEvent.Kind.CREATED, reason, {})
    return training


def _change(*, actor, training_id, expected_revision, values, reason, action):
    require_role(actor, *MANAGE_ROLES)
    reason = _text(reason, "reason")
    training = _locked(training_id, expected_revision)
    expected = {
        "updated": {TrainingRecord.Status.ASSIGNED},
        "completed": {TrainingRecord.Status.ASSIGNED},
        "corrected": {TrainingRecord.Status.COMPLETED},
        "cancelled": {TrainingRecord.Status.ASSIGNED, TrainingRecord.Status.COMPLETED},
    }[action]
    _state(training, expected)
    before = _snapshot(training)
    if action == "completed":
        training.status = TrainingRecord.Status.COMPLETED
    elif action == "cancelled":
        training.status = TrainingRecord.Status.CANCELLED
    allowed = {
        "updated": ASSIGNMENT_FIELDS,
        "completed": COMPLETION_FIELDS,
        "corrected": EDIT_FIELDS,
        "cancelled": (),
    }[action]
    training.updated_at = timezone.now()
    training.updated_by = actor
    _apply(training, values, allowed)
    after = _snapshot(training)
    if action in {"updated", "corrected"} and all(
        before[field] == after[field] for field in allowed
    ):
        raise ValidationError("Change at least one field before saving.")
    training.revision += 1
    training.save()
    _record(training, actor, action, reason, before)
    return training


@transaction.atomic
def update_training(*, actor, training_id, expected_revision, values, reason):
    return _change(
        actor=actor,
        training_id=training_id,
        expected_revision=expected_revision,
        values=values,
        reason=reason,
        action="updated",
    )


@transaction.atomic
def complete_training(*, actor, training_id, expected_revision, values, reason):
    return _change(
        actor=actor,
        training_id=training_id,
        expected_revision=expected_revision,
        values=values,
        reason=reason,
        action="completed",
    )


@transaction.atomic
def correct_training(*, actor, training_id, expected_revision, values, reason):
    return _change(
        actor=actor,
        training_id=training_id,
        expected_revision=expected_revision,
        values=values,
        reason=reason,
        action="corrected",
    )


@transaction.atomic
def cancel_training(*, actor, training_id, expected_revision, reason):
    return _change(
        actor=actor,
        training_id=training_id,
        expected_revision=expected_revision,
        values={},
        reason=reason,
        action="cancelled",
    )
