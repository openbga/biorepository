"""CSV actions shared by the distribution registers."""

from django.urls import reverse


def export_action(form, page, sort, *, route, kind, limit):
    if not form.is_valid():
        return {"export_url": None}
    if page.paginator.count > limit:
        return {"export_url": None, "exceeded_export_limit": f"{limit:,}"}
    # Rebind the same field inputs on download, including model choices and checkboxes.
    query = {
        name: form.data[name]
        for name in form.fields
        if name not in ("sort", "per_page") and form.data.get(name)
    }
    return {"export_url": reverse(route, args=[kind], query={**query, "sort": sort})}
