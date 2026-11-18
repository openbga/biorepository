"""CSV downloads of the equipment lists, with the same filters, sorts and authorization.

Same rules as the storage exports (storage.exports): UTF-8, formula-safe text cells, exact
decimals with the unit in the column header, times in the repository's time zone, a selection
over EXPORT_CAP rows refused rather than truncated, and an audit event naming who downloaded which
selection and how many rows (never the file itself).
"""

import csv
import io

from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import QueryDict

from accounts.roles import require_role
from core.models import AuditEvent
from storage.exports import safe_cell

from .lists import EXPORT_KINDS, SPECS, filtered, ordered
from .services import READ_ROLES

EXPORT_CAP = 10000
PAGING = ("page", "per_page", "sort")


def export_list(*, actor, kind, filters, sort=""):
    """CSV bytes of all rows matching an equipment register selection."""
    require_role(actor, *READ_ROLES)
    if kind not in EXPORT_KINDS:
        raise ValidationError("Unknown list.", code="invalid")
    spec = SPECS[kind]
    data = filters.copy() if isinstance(filters, QueryDict) else QueryDict(mutable=True)
    if not isinstance(filters, QueryDict):
        for key, value in (filters or {}).items():
            data[key] = "" if value is None else str(getattr(value, "pk", value))
    allowed = set(spec.form_class(user=actor).fields) | set(PAGING)
    unknown = {key for key in data if key not in allowed}
    if unknown:
        raise ValidationError("Unknown filter: " + ", ".join(sorted(unknown)), code="invalid")
    sort = sort or data.get("sort") or spec.default_sort
    if sort.removeprefix("-") not in spec.columns:
        sort = spec.default_sort
    data["q"] = data.get("q", "").strip()
    form, queryset = filtered(spec, data, actor)
    if form.errors or data.get("per_page") == "":
        raise ValidationError("These filters cannot be applied.", code="invalid")
    queryset = ordered(spec, queryset, sort)
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow([header for header, _ in spec.export_columns])
    total = 0
    for total, row in enumerate(queryset[: EXPORT_CAP + 1].iterator(), start=1):
        if total > EXPORT_CAP:
            raise ValidationError(
                f"Exports are limited to {EXPORT_CAP:,} rows. Narrow the filters and export again.",
                code="invalid",
            )
        writer.writerow([safe_cell(getter(row)) for _, getter in spec.export_columns])
    selection = {k: data[k] for k in sorted(data) if data[k] and k not in PAGING}
    with transaction.atomic():
        AuditEvent.objects.create(
            actor=actor,
            action="equipment.exported",
            object_type="equipment.export",
            object_id=kind,
            after={"kind": kind, "filters": selection, "sort": sort, "rows": total},
        )
    return buffer.getvalue().encode("utf-8")
