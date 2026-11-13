"""Bounded attachments for every app: validation on the way in, authorized download on the way out.

Files are untrusted data. The stored content type comes from a fixed extension map confirmed by
the file's leading bytes, never from the client-supplied header; downloads are always
attachments, never rendered inline. Each accepted upload is stored once as an immutable
`Attachment` row (bytes in a deferred PostgreSQL column; architecture: bounded attachments in
PostgreSQL until volume says otherwise) that records refer to; a correction that keeps the file
refers to the same row instead of copying it. Same validation rules as the sibling product's
evidence files.

The referring app authorizes: it checks that the user may see the record, then returns
`download_response(Attachment.objects.with_data().get(pk=record.attachment_id))`.

`ATTACHMENT_STORAGE_LIMIT_BYTES` (Django setting, read from the environment variable of the same
name; None = no limit) caps the total bytes stored in attachments for the deployment; the check
runs under a transaction-scoped advisory lock so concurrent uploads cannot overshoot it together.
"""

import hashlib
import os

from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import connection
from django.db.models import Sum
from django.http import HttpResponse
from django.utils.http import content_disposition_header

from .models import Attachment

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
ALLOWED = {
    ".pdf": ("application/pdf", (b"%PDF-",)),
    ".png": ("image/png", (b"\x89PNG\r\n\x1a\n",)),
    ".jpg": ("image/jpeg", (b"\xff\xd8\xff",)),
    ".jpeg": ("image/jpeg", (b"\xff\xd8\xff",)),
    ".txt": ("text/plain; charset=utf-8", None),
    ".csv": ("text/csv; charset=utf-8", None),
}
HELP = "PDF, PNG, JPEG, TXT or CSV, up to 10 MiB."


def read_attachment(uploaded):
    """Model field values for an uploaded file, or {} when none. Raises ValueError."""
    if not uploaded:
        return {}
    name = os.path.basename(uploaded.name or "")[:255]
    extension = os.path.splitext(name)[1].lower()
    if extension not in ALLOWED:
        raise ValueError("Attach a PDF, PNG, JPEG, TXT or CSV file.")
    if uploaded.size is not None and uploaded.size > MAX_ATTACHMENT_BYTES:
        raise ValueError("The file is larger than 10 MiB.")
    data = uploaded.read(MAX_ATTACHMENT_BYTES + 1)
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise ValueError("The file is larger than 10 MiB.")
    if not data:
        raise ValueError("The file is empty.")
    content_type, signatures = ALLOWED[extension]
    if signatures and not data.startswith(signatures):
        raise ValueError(f"The file content does not look like a {extension[1:].upper()} file.")
    if signatures is None:
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Text and CSV attachments must be UTF-8.") from exc
    return {
        "name": name,
        "content_type": content_type,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "data": data,
    }


class AttachmentField(forms.FileField):
    """Optional upload that fails validation with the same message the service would give."""

    def __init__(self, **kwargs):
        kwargs.setdefault("required", False)
        kwargs.setdefault("label", "Attachment")
        kwargs.setdefault("help_text", HELP)
        super().__init__(**kwargs)

    def clean(self, data, initial=None):
        uploaded = super().clean(data, initial)
        if uploaded:
            try:
                fields = read_attachment(uploaded)
            except ValueError as exc:
                raise forms.ValidationError(str(exc)) from exc
            uploaded.seek(0)
            uploaded.attachment_fields = fields
        return uploaded


# Key of the advisory lock serializing the storage budget check ("VRATTACH").
BUDGET_LOCK = 0x5652415454414348


def attachment_fields(uploaded, field="attachment"):
    """Validated values for an upload, or {} when none. Read once and remembered on the upload,
    so a service that asks twice does not find the stream consumed. A bad file is a
    ValidationError on `field`, with the message the form shows."""
    if not uploaded:
        return {}
    fields = getattr(uploaded, "attachment_fields", None)
    if fields is None:
        try:
            fields = read_attachment(uploaded)
        except ValueError as exc:
            raise ValidationError({field: str(exc)}) from exc
        uploaded.attachment_fields = fields
    return fields


def store_attachment(uploaded, *, actor, field="attachment"):
    """Store a validated upload once and return its Attachment, or None when there is no file.

    Call inside the caller's transaction, after its authorization checks."""

    fields = attachment_fields(uploaded, field)
    if not fields:
        return None
    limit = getattr(settings, "ATTACHMENT_STORAGE_LIMIT_BYTES", None)
    if limit is not None:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", [BUDGET_LOCK])
        stored = Attachment.objects.aggregate(total=Sum("size"))["total"] or 0
        if stored + fields["size"] > limit:
            raise ValidationError(
                {
                    field: "The repository's storage for attached files is full. Ask an "
                    "administrator; nothing was recorded."
                }
            )
    return Attachment.objects.create(created_by=actor, **fields)


def download_response(attachment):
    """Download response for an Attachment loaded with `Attachment.objects.with_data()`."""
    response = HttpResponse(bytes(attachment.data), content_type=attachment.content_type)
    response["Content-Disposition"] = content_disposition_header(True, attachment.name)
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    return response
