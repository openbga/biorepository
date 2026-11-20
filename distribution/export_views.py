"""CSV actions and error recovery shared by the distribution registers."""

from django.contrib import messages
from django.http import HttpResponseBadRequest
from django.shortcuts import redirect
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


def export_error_response(request, error, *, list_route):
    message = " ".join(error.messages)
    if error.code not in ("invalid_filters", "export_limit"):
        return HttpResponseBadRequest(message, content_type="text/plain; charset=utf-8")
    messages.error(request, message)
    query = request.GET.copy()
    query.pop("page", None)
    query.pop("per_page", None)
    return redirect(reverse(list_route, query=query))
