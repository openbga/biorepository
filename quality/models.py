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
