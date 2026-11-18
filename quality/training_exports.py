"""Export the applied training register selection and record its provenance."""

import csv
import io

from django.core.exceptions import ValidationError
from django.utils import timezone

from core.grids import GridRequest
from core.models import AuditEvent
from storage.exports import EXPORT_CAP, safe_cell

from .training_forms import FilterForm
from .training_lists import COLUMNS, DEFAULT_SORT, applied_filters, filter_training
from .training_services import training_records


def export_training(*, actor, params):
    rows = training_records(actor=actor)
    form = FilterForm(params)
    if params.keys() - (form.fields.keys() | {"q", "sort", "page", "per_page"}):
        raise ValidationError(
            "Unknown training filter. Return to the register and apply your filters."
        )
    try:
        grid = GridRequest.parse(params, COLUMNS, DEFAULT_SORT)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    if not form.is_valid() or params.get("sort", grid.sort) != grid.sort:
        raise ValidationError("These training filters or sort order cannot be applied.")

    rows = filter_training(rows, form, grid.query)
    # Read the displayed values in a single query, leaving evidence and account details out.
    rows = (
        grid.ordered(rows, COLUMNS)
        .select_related(None)
        .select_related("trainee")
        .only(
            "status",
            "trainee__first_name",
            "trainee__last_name",
            "trainee__username",
            "topic",
            "procedure_reference",
            "due_on",
            "completed_on",
            "renewal_due",
            "updated_at",
        )
    )
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(
        (
            "Training record",
            "Status",
            "Trainee",
            "Topic",
            "Procedure / version reference",
            "Assignment due",
            "Completed on",
            "Renewal due",
            "Updated",
        )
    )
    count = 0
    for row in rows[: EXPORT_CAP + 1].iterator():
        if count == EXPORT_CAP:
            raise ValidationError(
                f"Narrow the filters to {EXPORT_CAP:,} training records or fewer."
            )
        writer.writerow(
            safe_cell(value)
            for value in (
                row.number,
                row.get_status_display(),
                row.trainee.get_full_name() or row.trainee.username,
                row.topic,
                row.procedure_reference,
                row.due_on,
                row.completed_on,
                row.renewal_due,
                timezone.localtime(row.updated_at).isoformat(),
            )
        )
        count += 1
    AuditEvent.objects.create(
        actor=actor,
        action="quality.training_exported",
        object_type="quality.training_export",
        object_id="training",
        after={"filters": applied_filters(form, grid.query), "sort": grid.sort, "rows": count},
    )
    return buffer.getvalue().encode("utf-8")
