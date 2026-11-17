"""Editing surface metadata for progressive enhancement, never authorization policy."""

import re
from dataclasses import dataclass
from functools import cache

from django.urls import URLResolver, get_resolver

# Dedicated editing pages only. Record pages with inline actions remain record pages.
FORM_SURFACES = {
    "quality:training-create": "lg",
    "quality:training-edit": "lg",
    "quality:training-complete": "lg",
    "quality:training-correct": "lg",
    "quality:training-cancel": "lg",
    "quality:deviation-create": "lg",
    "quality:deviation-edit": "lg",
    "quality:deviation-submit": "lg",
    "quality:deviation-review": "lg",
    "quality:deviation-reopen": "lg",
    "quality:audit-create": "lg",
    "quality:audit-complete": "xl",
    "quality:audit-cancel": "lg",
    "announcement-create": "lg",
    "announcement-edit": "lg",
    "storage:site-create": "lg",
    "storage:site-edit": "lg",
    "storage:room-create": "lg",
    "storage:room-edit": "lg",
    "storage:unit-create": "lg",
    "storage:unit-edit": "lg",
    "storage:node-create": "lg",
    "storage:node-edit": "lg",
    "storage:box-create": "lg",
    "storage:box-edit": "lg",
    "storage:class-create": "lg",
    "storage:class-edit": "lg",
    "storage:container-type-create": "lg",
    "storage:container-type-edit": "lg",
    "storage:template-create": "xl",
    "storage:template-edit": "xl",
    "storage:template-apply": "lg",
    "storage:container-move": "lg",
    "storage:position-block": "lg",
    "storage:retire": "lg",
    "specimens:specimen-create": "lg",
    "specimens:place": "lg",
    "specimens:relocate": "lg",
    "specimens:check-out": "lg",
    "specimens:check-in": "lg",
    "specimens:transfer": "lg",
    "specimens:consume": "lg",
    "specimens:adjust": "lg",
    "specimens:reverse": "lg",
    "specimens:hold-add": "lg",
    "specimens:hold-release": "lg",
    "specimens:thaw-record": "lg",
    "specimens:thaw-correct": "lg",
    "specimens:custody-correct": "lg",
    "specimens:correct": "lg",
    "specimens:disposal-request": "lg",
    "specimens:disposal-decide": "lg",
    "specimens:disposal-execute": "lg",
    "specimens:import-upload": "lg",
    "specimens:derive": "xl",
    "specimens:participant-create": "lg",
    "specimens:participant-edit": "lg",
    "specimens:collection-create": "lg",
    "specimens:collection-edit": "lg",
    "specimens:specimen-type-create": "lg",
    "specimens:specimen-type-edit": "lg",
    "specimens:container-type-create": "lg",
    "specimens:container-type-edit": "lg",
    "specimens:unit-create": "lg",
    "specimens:unit-edit": "lg",
    "equipment:equipment-create": "lg",
    "equipment:equipment-edit": "lg",
    "equipment:status-change": "lg",
    "equipment:service-create": "lg",
    "equipment:service-correct": "lg",
    "equipment:ln2-create": "lg",
    "equipment:ln2-correct": "lg",
    "equipment:reading-create": "lg",
    "equipment:reading-correct": "lg",
    "equipment:import-upload": "lg",
    "equipment:excursion-create": "lg",
    "equipment:excursion-edit": "lg",
    "equipment:excursion-submit": "lg",
    "equipment:excursion-review": "lg",
    "equipment:excursion-reopen": "lg",
    "equipment:excursion-note": "lg",
    "equipment:excursion-holds": "xl",
    "distribution:request-create": "lg",
    "distribution:request-edit": "lg",
    "distribution:request-submit": "lg",
    "distribution:request-decide": "lg",
    "distribution:request-reopen": "lg",
    "distribution:request-cancel": "lg",
    "distribution:request-close": "lg",
    "distribution:request-agreement": "lg",
    "distribution:request-note": "lg",
    "distribution:allocation-release": "lg",
    "distribution:shipment-create": "lg",
    "distribution:shipment-edit": "lg",
    "distribution:shipment-pack": "lg",
    "distribution:shipment-unpack": "lg",
    "distribution:shipment-notify": "lg",
    "distribution:shipment-dispatch": "lg",
    "distribution:shipment-receipt": "lg",
    "distribution:receipt-correct": "lg",
    "distribution:shipment-cancel": "lg",
    "distribution:shipment-note": "lg",
    "distribution:return-record": "lg",
    "distribution:return-review": "lg",
    "distribution:study-create": "lg",
    "distribution:study-edit": "lg",
    "distribution:study-collection-link": "lg",
    "distribution:study-collection-end": "lg",
    "distribution:consent-create": "lg",
    "distribution:consent-correct": "lg",
    "distribution:consent-withdraw": "lg",
    "distribution:consent-escalate": "lg",
    "distribution:consent-hold": "lg",
    "distribution:consent-disposals": "lg",
    "distribution:consent-reinstate": "lg",
    "distribution:withdrawal-follow-up": "lg",
    "distribution:agreement-create": "lg",
    "distribution:agreement-edit": "lg",
    "distribution:agreement-execute": "lg",
    "distribution:agreement-terminate": "lg",
    "distribution:agreement-document": "lg",
    "distribution:category-create": "lg",
    "distribution:category-edit": "lg",
    "distribution:consent-form-create": "lg",
    "distribution:consent-form-edit": "lg",
    "distribution:institution-create": "lg",
    "distribution:institution-edit": "lg",
    "distribution:investigator-create": "lg",
    "distribution:investigator-edit": "lg",
    "distribution:request-items": "xl",
    "distribution:allocate": "xl",
    "distribution:pick": "xl",
    "distribution:put-back": "xl",
    "distribution:shipment-contents": "xl",
}


@dataclass(frozen=True)
class FormSurface:
    size: str


def surface_for_request(request):
    """Return presentation metadata for an already resolved request."""
    match = getattr(request, "resolver_match", None)
    size = FORM_SURFACES.get(match.view_name) if match is not None else None
    return FormSurface(size) if size else None


@cache
def form_route_patterns():
    """Return JavaScript-compatible path regexes from Django's canonical routes.

    The current application's converters use regex syntax shared by Python and JS.
    Remove Python's named-group spelling and end anchor before exposing the patterns.
    Callers match URL.pathname, without its query string or fragment.
    """
    result = []

    def visit(resolver, prefix="", namespaces=()):
        for route in resolver.url_patterns:
            regex = route.pattern.regex.pattern.removeprefix("^")
            if isinstance(route, URLResolver):
                child_namespaces = namespaces + ((route.namespace,) if route.namespace else ())
                visit(route, prefix + regex, child_namespaces)
                continue
            name = ":".join((*namespaces, route.name)) if route.name else ""
            if name not in FORM_SURFACES:
                continue
            pattern = re.sub(r"\(\?P<[^>]+>", "(?:", prefix + regex)
            pattern = pattern.replace(r"\Z", "$")
            if not pattern.endswith("$"):
                pattern += "$"
            result.append({"pattern": "^/" + pattern, "size": FORM_SURFACES[name]})

    visit(get_resolver())
    return result
