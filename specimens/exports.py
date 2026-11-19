"""CSV exports of specimen lists and catalogues: allow-listed selection, safe cells, audited.

Times are UTC ISO 8601; amounts carry their unit in a separate column. Cells that a spreadsheet
would read as a formula are prefixed with an apostrophe. Each export records who exported what
selection and how many rows (never the file) in the same short transaction as its audit event.
"""

import csv
import io
from datetime import UTC

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from accounts.roles import require_role
from core.audit import record_event
from storage import queries as storage_queries

from .list_forms import DISPOSAL_STATE_LABELS, disposal_selection, import_selection
from .models import OutputRecord
from .queries import catalogue_queryset, specimen_queryset
from .services import CATALOGUE_FIELDS, OPERATE_ROLES, READ_ROLES, request_state

MAX_ROWS = 10000
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(value):
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.astimezone(UTC).isoformat()
    text = str(value)
    return "'" + text if text.startswith(FORMULA_START) else text


def _csv(header, rows):
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow([safe_cell(cell) for cell in row])
    return buffer.getvalue().encode("utf-8")


def _bounded_rows(queryset):
    rows = list(queryset[: MAX_ROWS + 1])
    if len(rows) > MAX_ROWS:
        raise ValidationError(
            f"To export {MAX_ROWS:,} rows or fewer, narrow the filters.", code="invalid"
        )
    return rows


def _quantity(value):
    return format(value.normalize(), "f")


def _selection(filters):
    return {key: str(getattr(value, "pk", value)) for key, value in filters.items() if value}


SPECIMEN_HEADER = (
    "accession",
    "status",
    "specimen_type",
    "container_type",
    "quantity_on_hand",
    "unit",
    "participant_code",
    "collection_code",
    "parent_accession",
    "derivation_kind",
    "collected_at_utc",
    "processing_started_at_utc",
    "processing_completed_at_utc",
    "registered_at_utc",
    "custodian",
    "checked_out",
    "on_hold",
    "location",
)


def export_specimens(*, actor, filters, sort="accession"):
    require_role(actor, *READ_ROLES)
    queryset = specimen_queryset(filters, sort).select_related("parent")
    specimens = _bounded_rows(queryset)
    paths = storage_queries.locations(
        actor=actor, occupancy_ids=[s.occupancy_id for s in specimens]
    )
    rows = []
    for s in specimens:
        path = paths.get(s.occupancy_id)
        rows.append(
            (
                s.accession,
                s.status,
                s.specimen_type.code,
                s.container_type.code,
                _quantity(s.quantity_on_hand),
                s.unit.code,
                s.participant.code if s.participant else "",
                s.collection.code if s.collection else "",
                s.parent.accession if s.parent else "",
                s.derivation_kind,
                s.collected_at,
                s.processing_started_at,
                s.processing_completed_at,
                s.registered_at,
                s.custodian.get_username() if s.custodian else "",  # blank while distributed
                "yes" if s.open_checkout_id else "no",
                "yes" if s.held else "no",
                path["display"] if path else "",
            )
        )
    data = _csv(SPECIMEN_HEADER, rows)
    _record(
        actor, OutputRecord.Kind.EXPORT, len(rows), {"filters": _selection(filters), "sort": sort}
    )
    return data


def export_disposals(*, actor, params):
    require_role(actor, *READ_ROLES)
    try:
        grid, form, rows = disposal_selection(actor, params)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    if not form.is_valid():
        raise ValidationError("These disposal request filters or sort order cannot be applied.")

    header = ("Request", "State", "Specimen", "Method", "Destination", "Requested by", "Decided by")
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(header)
    count = 0
    for row in rows[: MAX_ROWS + 1].iterator(chunk_size=100):
        if count == MAX_ROWS:
            raise ValidationError(f"Narrow the filters to {MAX_ROWS:,} disposal requests or fewer.")
        decision = getattr(row, "decision", None)
        state = request_state(row, row.specimen, row.specimen.occupancy)
        values = (
            row.recorded_at,
            DISPOSAL_STATE_LABELS[state],
            row.specimen.accession,
            row.method,
            row.destination,
            row.requested_by.get_full_name() or row.requested_by.get_username(),
            (decision.actor.get_full_name() or decision.actor.get_username()) if decision else "",
        )
        writer.writerow(safe_cell(value) for value in values)
        count += 1
    content = buffer.getvalue().encode("utf-8")
    _record(
        actor,
        OutputRecord.Kind.EXPORT,
        count,
        {"list": "disposal", "filters": form.filters(), "sort": grid.sort},
    )
    return content


def export_imports(*, actor, params):
    require_role(actor, *OPERATE_ROLES)
    try:
        grid, form, rows = import_selection(params)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    if not form.is_valid():
        raise ValidationError("These import filters or sort order cannot be applied.")

    batches = list(rows[: MAX_ROWS + 1])
    if len(batches) > MAX_ROWS:
        raise ValidationError(f"Narrow the filters to {MAX_ROWS:,} imports or fewer.")
    now = timezone.now()
    content = _csv(
        ("Import", "State", "Rows", "Errors", "Validated by", "Validated"),
        (
            (
                batch.name,
                batch.state_at(now, error_total=batch.error_total).label,
                batch.row_count,
                batch.error_total,
                batch.actor.get_full_name() or batch.actor.get_username(),
                batch.recorded_at,
            )
            for batch in batches
        ),
    )
    filters = {"q": grid.query} if grid.query else {}
    _record(
        actor,
        OutputRecord.Kind.EXPORT,
        len(batches),
        {"list": "imports", "filters": filters, "sort": grid.sort},
    )
    return content


def export_catalogue(*, actor, kind, filters, sort="code"):
    require_role(actor, *READ_ROLES)
    queryset = catalogue_queryset(kind, filters, sort)
    fields = CATALOGUE_FIELDS[kind]
    rows = []
    for obj in _bounded_rows(queryset):
        row = []
        for name in fields:
            value = getattr(obj, name)
            if hasattr(value, "code"):
                value = value.code
            elif isinstance(value, bool):
                value = "yes" if value else "no"
            elif value is not None and name == "nominal_capacity":
                value = _quantity(value)
            row.append(value)
        rows.append(row)
    data = _csv(fields, rows)
    _record(
        actor,
        OutputRecord.Kind.CATALOGUE_EXPORT,
        len(rows),
        {"catalogue": kind, "filters": _selection(filters), "sort": sort},
    )
    return data


def _record(actor, kind, count, details):
    with transaction.atomic():
        record = OutputRecord.objects.create(kind=kind, actor=actor, count=count, details=details)
        record_event(
            actor=actor,
            action=f"specimens.{kind}",
            obj=record,
            after={"count": count, **details},
            reason="Download",
        )
