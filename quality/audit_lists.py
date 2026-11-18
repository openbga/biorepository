"""Selection shared by the box inventory audit register and its download."""

from django.db.models import Q

from core.grids import GridRequest

from .forms import FilterForm
from .services import audits

COLUMNS = {"number": "pk", "box": "box_code", "created": "created_at"}


def audit_selection(*, actor, params):
    # Both consumers need the frozen box code and outcome, not the position snapshots.
    rows = (
        audits(actor=actor)
        .select_related(None)
        .select_related("created_by", "finalization")
        .only(
            "box_code",
            "reason",
            "created_at",
            "created_by__first_name",
            "created_by__last_name",
            "created_by__username",
            "finalization__outcome",
        )
    )
    grid = GridRequest.parse(params, COLUMNS, "-created")
    if "\x00" in grid.query:
        raise ValueError("Search contains an invalid character. Enter the search again.")
    form = FilterForm(params)
    if not form.is_valid():
        return grid, form, rows.none()
    status = form.cleaned_data["status"]
    if status == "open":
        rows = rows.filter(finalization__isnull=True)
    elif status:
        rows = rows.filter(finalization__outcome=status)
    if grid.query:
        query = Q(box_code__icontains=grid.query) | Q(reason__icontains=grid.query)
        number = grid.query.upper().removeprefix("AUD-")
        if number.isdecimal() and len(number) <= 15:
            query |= Q(pk=int(number))
        rows = rows.filter(query)
    return grid, form, rows


def applied_filters(grid, form):
    return {key: value for key, value in {"q": grid.query, **form.cleaned_data}.items() if value}
