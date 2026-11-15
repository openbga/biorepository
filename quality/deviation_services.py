"""Deviation reports and reviews, locked and audited in one transaction."""

from datetime import datetime

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from accounts.roles import require_role
from core.audit import record_event

from .models import Deviation, DeviationEvent

READ_ROLES = ("admin", "manager", "technician", "viewer")
OPERATE_ROLES = ("admin", "manager", "technician")
MANAGE_ROLES = ("admin", "manager")
TEXT_LIMIT = 4000
NARRATIVES = (
    "description",
    "immediate_action",
    "investigation",
    "corrective_action",
    "effectiveness",
)
EDIT_FIELDS = ("title", "occurred_at", *NARRATIVES, "excursion_id", "inventory_audit_id")
SNAPSHOT_FIELDS = (
    *EDIT_FIELDS,
    "created_at",
    "created_by_id",
    "updated_at",
    "revision",
    "status",
    "submitted_at",
    "submitted_by_id",
    "closed_at",
    "closed_by_id",
)


def deviations(*, actor):
    require_role(actor, *READ_ROLES)
    return Deviation.objects.select_related(
        "created_by",
        "submitted_by",
        "closed_by",
        "excursion",
        "excursion__equipment",
        "inventory_audit",
    )


def get_deviation(*, actor, deviation_id):
    return deviations(actor=actor).get(pk=deviation_id)


def _text(value, field, *, required=True, limit=TEXT_LIMIT):
    if not isinstance(value, str):
        raise ValidationError({field: "Enter text."})
    value = value.strip()
    if required and not value:
        raise ValidationError({field: "This field is required."})
    if len(value) > limit:
        raise ValidationError({field: f"Enter at most {limit} characters."})
    return value


def _snapshot(deviation):
    # ISO strings preserve full stored microsecond precision in both retained histories.
    result = {}
    for field in SNAPSHOT_FIELDS:
        value = getattr(deviation, field)
        result[field] = value.isoformat() if isinstance(value, datetime) else value
    return result


def _apply(deviation, values, now):
    if not isinstance(values, dict) or set(values) - set(EDIT_FIELDS):
        raise ValidationError("Choose only report fields to change.")
    for field, value in values.items():
        if field in NARRATIVES or field == "title":
            value = _text(
                value,
                field,
                required=field in {"title", "description"},
                limit=160 if field == "title" else TEXT_LIMIT,
            )
        setattr(deviation, field, value)
    occurred = deviation.occurred_at
    if not isinstance(occurred, datetime) or timezone.is_naive(occurred):
        raise ValidationError({"occurred_at": "Enter an observed date and time with a time zone."})
    if occurred > now:
        raise ValidationError({"occurred_at": "The observed time cannot be in the future."})
    deviation.updated_at = now
    deviation.full_clean()


def _locked(deviation_id, expected_revision):
    deviation = Deviation.objects.select_for_update().get(pk=deviation_id)
    if (
        not isinstance(expected_revision, int)
        or isinstance(expected_revision, bool)
        or deviation.revision != expected_revision
    ):
        raise ValidationError(
            "This deviation changed while you were working. Your input is retained here. "
            "Reload the record and review its latest values before starting again.",
            code="stale_revision",
        )
    return deviation


def _state(deviation, expected):
    if deviation.status != expected:
        raise ValidationError(
            "This action requires status " + dict(Deviation.Status.choices)[expected] + ".",
            code="invalid_transition",
        )


def _record(deviation, actor, kind, reason, before):
    after = _snapshot(deviation)
    changed = {
        field: [before.get(field), value]
        for field, value in after.items()
        if field not in before or before[field] != value
    }
    DeviationEvent.objects.create(
        deviation=deviation,
        actor=actor,
        kind=kind,
        revision=deviation.revision,
        recorded_at=deviation.updated_at,
        reason=reason,
        changes=changed,
    )
    record_event(
        actor=actor,
        action=f"quality.deviation_{kind}",
        obj=deviation,
        before={field: pair[0] for field, pair in changed.items()} if before else {},
        after={field: pair[1] for field, pair in changed.items()},
        reason=reason,
    )


@transaction.atomic
def create_deviation(*, actor, values, reason):
    require_role(actor, *OPERATE_ROLES)
    reason = _text(reason, "reason")
    now = timezone.now()
    deviation = Deviation(created_at=now, updated_at=now, created_by=actor)
    _apply(deviation, values, now)
    deviation.save(force_insert=True)
    _record(deviation, actor, DeviationEvent.Kind.CREATED, reason, {})
    return deviation


@transaction.atomic
def update_deviation(*, actor, deviation_id, expected_revision, values, reason):
    require_role(actor, *OPERATE_ROLES)
    reason = _text(reason, "reason")
    deviation = _locked(deviation_id, expected_revision)
    _state(deviation, Deviation.Status.OPEN)
    before = _snapshot(deviation)
    _apply(deviation, values, timezone.now())
    if all(before[field] == _snapshot(deviation)[field] for field in EDIT_FIELDS):
        raise ValidationError("Change at least one report field before saving.")
    deviation.revision += 1
    deviation.save()
    _record(deviation, actor, DeviationEvent.Kind.UPDATED, reason, before)
    return deviation


@transaction.atomic
def submit_deviation(*, actor, deviation_id, expected_revision, reason):
    require_role(actor, *OPERATE_ROLES)
    reason = _text(reason, "reason")
    deviation = _locked(deviation_id, expected_revision)
    _state(deviation, Deviation.Status.OPEN)
    missing = {
        field: f"Complete {field.replace('_', ' ')} before submitting; explain if none or unknown."
        for field in ("investigation", "corrective_action", "effectiveness")
        if not getattr(deviation, field).strip()
    }
    if missing:
        raise ValidationError(missing)
    before = _snapshot(deviation)
    deviation.status = Deviation.Status.AWAITING_REVIEW
    deviation.updated_at = deviation.submitted_at = timezone.now()
    deviation.submitted_by = actor
    deviation.revision += 1
    deviation.full_clean()
    deviation.save()
    _record(deviation, actor, DeviationEvent.Kind.SUBMITTED, reason, before)
    return deviation


@transaction.atomic
def review_deviation(*, actor, deviation_id, expected_revision, decision, reason):
    require_role(actor, *MANAGE_ROLES)
    reason = _text(reason, "reason")
    deviation = _locked(deviation_id, expected_revision)
    _state(deviation, Deviation.Status.AWAITING_REVIEW)
    if actor.pk in {deviation.created_by_id, deviation.submitted_by_id}:
        raise PermissionDenied("A reviewer must differ from the creator and latest submitter.")
    if decision not in {"close", "return"}:
        raise ValidationError({"decision": "Choose Close or Return for revision."})
    before = _snapshot(deviation)
    deviation.updated_at = timezone.now()
    if decision == "close":
        deviation.status = Deviation.Status.CLOSED
        deviation.closed_at = deviation.updated_at
        deviation.closed_by = actor
        kind = DeviationEvent.Kind.CLOSED
    else:
        deviation.status = Deviation.Status.OPEN
        kind = DeviationEvent.Kind.RETURNED
    deviation.revision += 1
    deviation.full_clean()
    deviation.save()
    _record(deviation, actor, kind, reason, before)
    return deviation


@transaction.atomic
def reopen_deviation(*, actor, deviation_id, expected_revision, reason):
    require_role(actor, *MANAGE_ROLES)
    reason = _text(reason, "reason")
    deviation = _locked(deviation_id, expected_revision)
    _state(deviation, Deviation.Status.CLOSED)
    before = _snapshot(deviation)
    deviation.status = Deviation.Status.OPEN
    deviation.closed_at = deviation.closed_by = None
    deviation.updated_at = timezone.now()
    deviation.revision += 1
    deviation.full_clean()
    deviation.save()
    _record(deviation, actor, DeviationEvent.Kind.REOPENED, reason, before)
    return deviation
