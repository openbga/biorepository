"""CSV downloads of the storage lists, with the same filters, sorts and authorization.

Cells are UTF-8 CSV; text that a spreadsheet would read as a formula is prefixed with an
apostrophe; numbers are written as exact decimals with the unit in the column header. An export
over EXPORT_CAP rows is refused rather than truncated. Each export records who downloaded which
selection and how many rows, never the file itself.
"""

import csv
import io
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import QueryDict

from accounts.roles import require_role
from core.models import AuditEvent

from .lists import CATALOGUE_SPECS, SPECS, filtered, ordered
from .services import READ_ROLES

EXPORT_CAP = 10000
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, Decimal)):
        return str(value)
    text = str(value)
    return "'" + text if text.startswith(FORMULA_START) else text


def _data(filters):
    if isinstance(filters, QueryDict):
        return filters
    if not isinstance(filters, dict):
        raise ValidationError("Filters must be a mapping.", code="invalid")
    data = QueryDict(mutable=True)
    for key, value in filters.items():
        data[key] = "" if value is None else str(getattr(value, "pk", value))
    return data


def _export(*, actor, spec, filters, sort, label):
    require_role(actor, *READ_ROLES)
    data = _data(filters)
    allowed = set(spec.form_class().fields) - {"per_page", "sort"}
    unknown = {k for k in data if k not in allowed and k not in ("page", "per_page", "sort")}
    if unknown:
        raise ValidationError("Unknown filter: " + ", ".join(sorted(unknown)), code="invalid")
    form, queryset = filtered(spec, data)
    if form.errors:
        raise ValidationError("These filters cannot be applied.", code="invalid")
    queryset = ordered(spec, queryset, sort)
    total = queryset.count()
    if total > EXPORT_CAP:
        raise ValidationError(
            f"This selection has {total} rows; exports are limited to {EXPORT_CAP}. "
            "Narrow the filters and export again.",
            code="invalid",
        )
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow([header for header, _ in spec.export_columns])
    for row in queryset[:EXPORT_CAP]:
        writer.writerow([safe_cell(getter(row)) for _, getter in spec.export_columns])
    selection = {k: data.get(k) for k in sorted(data) if data.get(k) and k != "page"}
    with transaction.atomic():
        AuditEvent.objects.create(
            actor=actor,
            action="storage.exported",
            object_type="storage.export",
            object_id=label,
            after={"kind": label, "filters": selection, "sort": sort, "rows": total},
        )
    return buffer.getvalue().encode("utf-8")


def export_storage(*, actor, kind, filters, sort="code"):
    """CSV of units, nodes, boxes, free positions ("position"), sites or rooms."""
    if kind not in SPECS:
        raise ValidationError("Choose unit, node, box, position, site or room.", code="invalid")
    return _export(actor=actor, spec=SPECS[kind], filters=filters, sort=sort, label=kind)


def export_catalogue(*, actor, kind, filters, sort="code"):
    if kind not in CATALOGUE_SPECS:
        raise ValidationError("Unknown catalogue.", code="invalid")
    return _export(actor=actor, spec=CATALOGUE_SPECS[kind], filters=filters, sort=sort, label=kind)
