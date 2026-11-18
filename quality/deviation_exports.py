"""CSV of the six deviation register columns, with an audit of the selection."""

import csv
import io

from django.core.exceptions import ValidationError
from django.utils import timezone

from core.models import AuditEvent
from storage.exports import EXPORT_CAP, safe_cell

from .deviation_lists import COLUMNS, applied_filters, deviation_selection


def export_deviations(*, actor, params):
    try:
        grid, form, rows = deviation_selection(actor=actor, params=params)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    if params.keys() - (form.fields.keys() | {"q", "sort", "page", "per_page"}):
        raise ValidationError(
            "Unknown deviation filter. Return to the register and apply your filters."
        )
    if not form.is_valid() or params.get("sort", grid.sort) != grid.sort:
        raise ValidationError("These deviation filters or sort order cannot be applied.")

    rows = (
        grid.ordered(rows, COLUMNS)
        .select_related(None)
        .select_related("created_by")
        .only(
            "status",
            "title",
            "occurred_at",
            "updated_at",
            "created_by__first_name",
            "created_by__last_name",
            "created_by__username",
        )
    )
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(("Deviation", "Status", "Title", "Occurred at", "Updated", "Created by"))
    count = 0
    for deviation in rows[: EXPORT_CAP + 1].iterator():
        if count == EXPORT_CAP:
            raise ValidationError(f"Narrow the filters to {EXPORT_CAP:,} deviations or fewer.")
        writer.writerow(
            safe_cell(value)
            for value in (
                deviation.number,
                deviation.get_status_display(),
                deviation.title,
                timezone.localtime(deviation.occurred_at).isoformat(),
                timezone.localtime(deviation.updated_at).isoformat(),
                deviation.created_by.get_full_name() or deviation.created_by.get_username(),
            )
        )
        count += 1
    AuditEvent.objects.create(
        actor=actor,
        action="quality.deviations_exported",
        object_type="quality.deviation_export",
        object_id="deviations",
        after={"filters": applied_filters(grid, form), "sort": grid.sort, "rows": count},
    )
    return buffer.getvalue().encode("utf-8")
