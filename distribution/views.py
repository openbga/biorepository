"""Downloads shared by the request and governance pages: each kind is authorized and served by
the module that owns the record (request_views or governance_views)."""

from django.http import Http404

from . import governance_views, request_views


def attachment(request, kind, pk):
    for module in (request_views, governance_views):
        if kind in getattr(module, "ATTACHMENT_KINDS", ()):
            return module.attachment(request, kind, pk)
    raise Http404


def export(request, kind):
    for module in (request_views, governance_views):
        if kind in getattr(module, "EXPORT_KINDS", ()):
            return module.export(request, kind)
    raise Http404
