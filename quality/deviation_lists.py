"""The authorized selection behind the deviation register and its download."""

from django.db.models import Q

from core.grids import GridRequest

from .deviation_forms import FilterForm
from .deviation_services import deviations

COLUMNS = {
    "number": "pk",
    "title": "title",
    "status": "status",
    "observed": "occurred_at",
    "updated": "updated_at",
}


def deviation_selection(*, actor, params):
    rows = deviations(actor=actor)
    grid = GridRequest.parse(params, COLUMNS, "-updated")
    if "\x00" in grid.query:
        raise ValueError("Search contains an invalid character. Enter the search again.")
    form = FilterForm(params)
    if not form.is_valid():
        return grid, form, rows.none()

    if status := form.cleaned_data["status"]:
        rows = rows.filter(status=status)
    if grid.query:
        query = Q(title__icontains=grid.query) | Q(description__icontains=grid.query)
        number = grid.query.upper().removeprefix("DEV-")
        if number.isdecimal() and len(number) <= 15:
            query |= Q(pk=int(number))
        rows = rows.filter(query)
    return grid, form, rows


def applied_filters(grid, form):
    return {key: value for key, value in {**form.cleaned_data, "q": grid.query}.items() if value}
