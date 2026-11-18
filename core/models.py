import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class Repository(models.Model):
    """The one repository organization this deployment serves (id fixed to 1)."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    name = models.CharField(max_length=160)
    timezone = models.CharField(max_length=64, default="America/New_York")
    record_retention_statement = models.TextField(
        blank=True,
        max_length=8000,
        help_text="Repository-entered electronic record-retention policy; no automatic deletion.",
    )
    record_retention_revision = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(id=1), name="single_repository")]


class AuditEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    occurred_at = models.DateTimeField(auto_now_add=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    action = models.CharField(max_length=80)
    object_type = models.CharField(max_length=100)
    object_id = models.CharField(max_length=64)
    before = models.JSONField(default=dict)
    after = models.JSONField(default=dict)
    reason = models.TextField(blank=True)

    class Meta:
        ordering = ["occurred_at", "id"]
        indexes = [models.Index(fields=["object_type", "object_id", "occurred_at"])]


class Announcement(models.Model):
    """A notice the repository posts on the sign-in page or for its staff.

    core.announcements holds who may post one, who sees it and when it counts as showing.
    """

    class Tone(models.TextChoices):
        INFO = "info", "Notice"
        WARNING = "warning", "Important"

    title = models.CharField(max_length=120)
    body = models.TextField(max_length=2000)
    tone = models.CharField(max_length=10, choices=Tone.choices, default=Tone.INFO)
    show_on_sign_in = models.BooleanField(default=False)
    show_to_staff = models.BooleanField(default=False)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField(null=True, blank=True)
    withdrawn_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-starts_at", "-pk"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(show_on_sign_in=True) | models.Q(show_to_staff=True),
                name="announcement_has_audience",
                violation_error_message="Choose at least one place to show this announcement.",
            ),
            models.CheckConstraint(
                condition=models.Q(ends_at__isnull=True)
                | models.Q(ends_at__gt=models.F("starts_at")),
                name="announcement_ends_after_start",
                violation_error_message="Show until must be later than Show from.",
            ),
        ]

    def __str__(self):
        return self.title


class AttachmentManager(models.Manager):
    """Default queries never load the bytes; use `with_data()` to download."""

    def get_queryset(self):
        return super().get_queryset().defer("data")

    def with_data(self):
        return super().get_queryset()


class Attachment(models.Model):
    """An uploaded file stored once: immutable bytes with their checksum, size, name and type.

    Records refer to it (`ForeignKey(Attachment, on_delete=PROTECT, related_name="+")`); a
    correction that keeps the file refers to the same row, so history never copies the bytes. Rows
    are append-only (trigger `attachment_immutable`). Authorization belongs to the referring
    record: there is no route that serves an Attachment by its own key. Validation, storage budget
    and download response: `core.attachments`.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100)
    size = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64)
    data = models.BinaryField()
    created_at = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )

    objects = AttachmentManager()

    class Meta:
        db_table = "attachment"
        # Following a reference (record.attachment) uses the base manager: keep the bytes deferred.
        base_manager_name = "objects"
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(name="") & ~models.Q(content_type="") & models.Q(size__gte=1),
                name="attachment_described",
            ),
            models.CheckConstraint(
                condition=models.Q(sha256__regex=r"^[0-9a-f]{64}$"), name="attachment_sha256"
            ),
        ]
        indexes = [models.Index(fields=["sha256"], name="attachment_sha256_idx")]

    def __str__(self):
        return self.name


class UserPreference(models.Model):
    """One presentation setting of one user (core.preferences): a grid's columns, its page size
    and sort. Kept on the server, because repository workstations are shared."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="preferences"
    )
    key = models.CharField(max_length=120)
    value = models.JSONField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "key"], name="unique_user_preference")
        ]

    def __str__(self):
        return f"{self.user} {self.key}"
