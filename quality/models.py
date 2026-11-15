"""Manual audit evidence. Both tables are append-only, including their JSON snapshots."""

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

MAX_AUDIT_POSITIONS = 400


class InventoryAudit(models.Model):
    box = models.ForeignKey(
        "storage.Box", on_delete=models.PROTECT, related_name="inventory_audits"
    )
    box_code = models.CharField(max_length=40)
    box_revision = models.PositiveIntegerField()
    location = models.JSONField()
    positions = models.JSONField()
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    reason = models.TextField()
    supersedes = models.ForeignKey(
        "self", on_delete=models.PROTECT, related_name="follow_up_audits", null=True, blank=True
    )

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [models.CheckConstraint(condition=~Q(reason=""), name="quality_audit_reason")]

    @property
    def number(self):
        return f"AUD-{self.pk:06d}"

    @property
    def status(self):
        try:
            return self.finalization.outcome
        except AuditFinalization.DoesNotExist:
            return "open"

    def __str__(self):
        return self.number


class AuditFinalization(models.Model):
    class Outcome(models.TextChoices):
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    audit = models.OneToOneField(
        InventoryAudit, on_delete=models.PROTECT, related_name="finalization"
    )
    outcome = models.CharField(max_length=12, choices=Outcome.choices)
    recorded_at = models.DateTimeField(default=timezone.now, editable=False)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    reason = models.TextField()
    observations = models.JSONField(default=list)
    counts = models.JSONField(default=dict)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=~Q(reason=""), name="quality_finalization_reason"),
            models.CheckConstraint(
                condition=Q(outcome__in=["completed", "cancelled"]),
                name="quality_finalization_outcome",
            ),
            models.CheckConstraint(
                condition=Q(outcome="completed") | Q(observations=[], counts={}),
                name="quality_cancellation_no_results",
            ),
        ]


class Deviation(models.Model):
    """A retained quality report; all changes have an immutable revision event."""

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        AWAITING_REVIEW = "awaiting_review", "Awaiting review"
        CLOSED = "closed", "Closed"

    title = models.CharField(max_length=160)
    occurred_at = models.DateTimeField()
    description = models.TextField(max_length=4000)
    immediate_action = models.TextField(max_length=4000, blank=True)
    investigation = models.TextField(max_length=4000, blank=True)
    corrective_action = models.TextField(max_length=4000, blank=True)
    effectiveness = models.TextField(max_length=4000, blank=True)
    excursion = models.ForeignKey(
        "equipment.Excursion",
        on_delete=models.PROTECT,
        related_name="deviations",
        null=True,
        blank=True,
    )
    inventory_audit = models.ForeignKey(
        InventoryAudit,
        on_delete=models.PROTECT,
        related_name="deviations",
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    updated_at = models.DateTimeField(default=timezone.now, editable=False)
    revision = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=15, choices=Status.choices, default=Status.OPEN)
    submitted_at = models.DateTimeField(null=True, blank=True, editable=False)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", null=True, blank=True
    )
    closed_at = models.DateTimeField(null=True, blank=True, editable=False)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", null=True, blank=True
    )

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.CheckConstraint(condition=Q(title__regex=r"\S"), name="quality_dev_title"),
            models.CheckConstraint(
                condition=Q(description__regex=r"\S"), name="quality_dev_description"
            ),
            models.CheckConstraint(condition=Q(revision__gte=1), name="quality_dev_revision"),
            models.CheckConstraint(
                condition=Q(status__in=["open", "awaiting_review", "closed"]),
                name="quality_dev_status",
            ),
            models.CheckConstraint(
                condition=Q(submitted_at__isnull=True, submitted_by__isnull=True, status="open")
                | Q(submitted_at__isnull=False, submitted_by__isnull=False),
                name="quality_dev_submission",
            ),
            models.CheckConstraint(
                condition=Q(status="closed", closed_at__isnull=False, closed_by__isnull=False)
                | (~Q(status="closed") & Q(closed_at__isnull=True, closed_by__isnull=True)),
                name="quality_dev_closure",
            ),
            models.CheckConstraint(
                condition=~Q(status="closed")
                | (~Q(closed_by=models.F("created_by")) & ~Q(closed_by=models.F("submitted_by"))),
                name="quality_dev_second_person",
            ),
            models.CheckConstraint(
                condition=Q(status="open")
                | (
                    Q(
                        investigation__regex=r"\S",
                        corrective_action__regex=r"\S",
                        effectiveness__regex=r"\S",
                    )
                ),
                name="quality_dev_review_ready",
            ),
            models.CheckConstraint(
                condition=Q(occurred_at__lte=models.F("updated_at"))
                & Q(created_at__lte=models.F("updated_at")),
                name="quality_dev_chronology",
            ),
            models.CheckConstraint(
                condition=Q(submitted_at__isnull=True)
                | (
                    Q(submitted_at__gte=models.F("created_at"))
                    & Q(submitted_at__lte=models.F("updated_at"))
                ),
                name="quality_dev_submit_time",
            ),
            models.CheckConstraint(
                condition=Q(closed_at__isnull=True)
                | (
                    Q(closed_at__gte=models.F("submitted_at"))
                    & Q(closed_at__lte=models.F("updated_at"))
                ),
                name="quality_dev_close_time",
            ),
        ]

    @property
    def number(self):
        return f"DEV-{self.pk:06d}"

    def __str__(self):
        return f"{self.number} · {self.title}"


class DeviationEvent(models.Model):
    class Kind(models.TextChoices):
        CREATED = "created", "Created"
        UPDATED = "updated", "Updated"
        SUBMITTED = "submitted", "Submitted for review"
        RETURNED = "returned", "Returned for revision"
        CLOSED = "closed", "Closed"
        REOPENED = "reopened", "Reopened"

    deviation = models.ForeignKey(Deviation, on_delete=models.PROTECT, related_name="events")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    revision = models.PositiveIntegerField()
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    recorded_at = models.DateTimeField(default=timezone.now, editable=False)
    reason = models.TextField(max_length=4000)
    changes = models.JSONField(default=dict)

    class Meta:
        ordering = ["revision", "pk"]
        constraints = [
            models.UniqueConstraint(fields=["deviation", "revision"], name="quality_dev_event_rev"),
            models.CheckConstraint(condition=Q(revision__gte=1), name="quality_dev_event_positive"),
            models.CheckConstraint(
                condition=Q(reason__regex=r"\S"), name="quality_dev_event_reason"
            ),
            models.CheckConstraint(
                condition=Q(
                    kind__in=["created", "updated", "submitted", "returned", "closed", "reopened"]
                ),
                name="quality_dev_event_kind",
            ),
        ]


class TrainingRecord(models.Model):
    """Retained staff training evidence; it confers no permission or certification."""

    class Status(models.TextChoices):
        ASSIGNED = "assigned", "Assigned"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    trainee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="training_records"
    )
    topic = models.CharField(max_length=160)
    procedure_reference = models.CharField(max_length=240, blank=True)
    due_on = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ASSIGNED)
    completed_on = models.DateField(null=True, blank=True)
    trainer_provider = models.CharField(max_length=240, blank=True)
    completion_evidence = models.TextField(max_length=4000, blank=True)
    competency_assessment = models.TextField(max_length=4000, blank=True)
    renewal_due = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    updated_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    revision = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["-updated_at", "-pk"]
        constraints = [
            models.CheckConstraint(condition=Q(topic__regex=r"\S"), name="quality_training_topic"),
            models.CheckConstraint(condition=Q(revision__gte=1), name="quality_training_revision"),
            models.CheckConstraint(
                condition=Q(status__in=["assigned", "completed", "cancelled"]),
                name="quality_training_status",
            ),
            models.CheckConstraint(
                condition=Q(created_at__lte=models.F("updated_at")),
                name="quality_training_chronology",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        completed_on__isnull=True,
                        trainer_provider="",
                        completion_evidence="",
                        competency_assessment="",
                        renewal_due__isnull=True,
                    )
                    | Q(
                        completed_on__isnull=False,
                        trainer_provider__regex=r"\S",
                        completion_evidence__regex=r"\S",
                    )
                ),
                name="quality_training_evidence",
            ),
            models.CheckConstraint(
                condition=~Q(status="completed") | Q(completed_on__isnull=False),
                name="quality_training_completed",
            ),
            models.CheckConstraint(
                condition=~Q(status="assigned") | Q(completed_on__isnull=True),
                name="quality_training_assigned",
            ),
            models.CheckConstraint(
                condition=Q(renewal_due__isnull=True)
                | Q(completed_on__isnull=False, renewal_due__gte=models.F("completed_on")),
                name="quality_training_renewal",
            ),
        ]

    @property
    def number(self):
        return f"TRN-{self.pk:06d}"

    def __str__(self):
        return f"{self.number} · {self.topic}"


class TrainingEvent(models.Model):
    class Kind(models.TextChoices):
        CREATED = "created", "Assigned"
        UPDATED = "updated", "Assignment updated"
        COMPLETED = "completed", "Completion recorded"
        CORRECTED = "corrected", "Completion corrected"
        CANCELLED = "cancelled", "Cancelled"

    training = models.ForeignKey(TrainingRecord, on_delete=models.PROTECT, related_name="events")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    revision = models.PositiveIntegerField()
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    recorded_at = models.DateTimeField(default=timezone.now, editable=False)
    reason = models.TextField(max_length=4000)
    changes = models.JSONField(default=dict)

    class Meta:
        ordering = ["revision", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["training", "revision"], name="quality_training_event_rev"
            ),
            models.CheckConstraint(
                condition=Q(revision__gte=1), name="quality_training_event_positive"
            ),
            models.CheckConstraint(
                condition=Q(reason__regex=r"\S"), name="quality_training_event_reason"
            ),
            models.CheckConstraint(
                condition=Q(kind__in=["created", "updated", "completed", "corrected", "cancelled"]),
                name="quality_training_event_kind",
            ),
        ]
