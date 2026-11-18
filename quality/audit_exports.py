"""CSV of the filtered inventory audit register."""

import codecs
import csv
from tempfile import SpooledTemporaryFile

from django.core.exceptions import ValidationError
from django.utils import timezone

from core.models import AuditEvent
from storage.exports import EXPORT_CAP, safe_cell

from .audit_lists import COLUMNS, applied_filters, audit_selection

SPOOL_MEMORY_LIMIT = 1024 * 1024


def export_audits(*, actor, params):
    try:
        grid, form, rows = audit_selection(actor=actor, params=params)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    if params.keys() - (form.fields.keys() | {"q", "sort", "page", "per_page"}):
        raise ValidationError(
            "Unknown inventory audit filter. Return to the register and apply your filters."
        )
    if not form.is_valid() or params.get("sort", grid.sort) != grid.sort:
        raise ValidationError("These inventory audit filters or sort order cannot be applied.")

    # A full register can contain 10,000 long creation reasons. Roll larger files
    # onto ephemeral storage and finish validating the selection before serving it.
    output = SpooledTemporaryFile(max_size=SPOOL_MEMORY_LIMIT, mode="w+b")
    try:
        writer = csv.writer(codecs.getwriter("utf-8")(output))
        writer.writerow(("Audit", "Box", "Status", "Created", "Created by", "Reason"))
        count = 0
        for audit in grid.ordered(rows, COLUMNS)[: EXPORT_CAP + 1].iterator(chunk_size=100):
            if count == EXPORT_CAP:
                raise ValidationError(
                    f"Narrow the filters to {EXPORT_CAP:,} inventory audits or fewer."
                )
            writer.writerow(
                safe_cell(value)
                for value in (
                    audit.number,
                    audit.box_code,
                    audit.status.capitalize(),
                    timezone.localtime(audit.created_at).isoformat(),
                    audit.created_by.get_full_name() or audit.created_by.get_username(),
                    audit.reason,
                )
            )
            count += 1
        output.seek(0)
        AuditEvent.objects.create(
            actor=actor,
            action="quality.inventory_audits_exported",
            object_type="quality.inventory_audit_export",
            object_id="inventory_audits",
            after={"filters": applied_filters(grid, form), "sort": grid.sort, "rows": count},
        )
    except BaseException:
        output.close()
        raise
    return output
