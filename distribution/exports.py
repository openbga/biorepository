"""CSV downloads of the request, shipment and return lists, with the same filters, sorts and
authorization as the pages (distribution.request_lists).

Same rules as the specimen exports (specimens.exports): UTF-8, formula-safe text cells, exact
decimals with the unit in its own column, times in the repository's time zone, a selection over
MAX_ROWS rows refused rather than truncated, and an OutputRecord plus audit event naming who
downloaded which selection and how many rows (never the file itself).
"""

import csv
import io

from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import QueryDict

from accounts.roles import require_role
from core.audit import record_event
from specimens.exports import safe_cell

from .models import OutputRecord
from .request_lists import EXPORT_KINDS, SPECS, filtered, ordered
from .services import READ_ROLES

MAX_ROWS = 10000
PAGING = ("page", "per_page", "sort")


def _data(filters):
    if isinstance(filters, QueryDict):
        return filters
    data = QueryDict(mutable=True)
    for key, value in (filters or {}).items():
        data[key] = "" if value is None else str(getattr(value, "pk", value))
    return data


def _cell(value):
    if isinstance(value, bool):
        return "yes" if value else "no"
    return safe_cell(value)


def register_csv(queryset, columns, *, limit):
    """Return CSV bytes and their data-row count, refusing any selection over the limit."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow([header for header, _ in columns])
    total = 0
    for total, row in enumerate(queryset[: limit + 1].iterator(chunk_size=500), start=1):
        if total > limit:
            raise ValidationError(
                f"Exports are limited to {limit:,} rows. Narrow the filters and export again.",
                code="invalid",
            )
        writer.writerow([_cell(getter(row)) for _, getter in columns])
    return buffer.getvalue().encode("utf-8"), total


def export_list(*, actor, kind, filters, sort=""):
    """CSV bytes of one list: `requests`, `shipments` or `returns`."""
    require_role(actor, *READ_ROLES)
    if kind not in EXPORT_KINDS:
        raise ValidationError("Unknown list.", code="invalid")
    spec = SPECS[EXPORT_KINDS[kind]]
    data = _data(filters)
    allowed = set(spec.form_class(user=actor).fields) | set(PAGING)
    unknown = {key for key in data if key not in allowed}
    if unknown:
        raise ValidationError("Unknown filter: " + ", ".join(sorted(unknown)), code="invalid")
    form, queryset = filtered(spec, data, actor)
    if form.errors:
        raise ValidationError("These filters cannot be applied.", code="invalid")
    queryset = ordered(spec, queryset, sort)
    content, total = register_csv(queryset, spec.export_columns, limit=MAX_ROWS)
    selection = {k: data.get(k) for k in sorted(data) if data.get(k) and k not in PAGING}
    with transaction.atomic():
        record = OutputRecord.objects.create(
            kind=OutputRecord.Kind.EXPORT,
            actor=actor,
            count=total,
            details={"list": kind, "filters": selection, "sort": sort},
        )
        record_event(
            actor=actor,
            action="distribution.exported",
            obj=record,
            after={"list": kind, "filters": selection, "sort": sort, "rows": total},
            reason="Download",
        )
    return content
