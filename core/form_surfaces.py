"""Which pages open in the shared form dialog: presentation, never authorization.

Every form opens in the dialog (docs/ui-standards.md section 7): static/js/form-dialog.js opens a
link to one of these routes in #vr-form-dialog over the page it was clicked on, fetching the page
with the X-VR-Form header; the view, its permission checks, CSRF and validation are the same as
for the full page. Opened by its address, the form opens over the page it belongs to (its Cancel
destination: the record or the list), as when opened from there; a form without a Cancel, or one
already showing errors, stays its own page. Without JavaScript each stays an ordinary page.

tests/test_form_dialog.py walks every route: one whose view takes a form is listed in
FORM_SURFACES or in NOT_FORM_PAGES with the reason its page is not a form page. A route whose
view accepts only POST is a button or panel form on another page and needs neither.
"""

import re
from dataclasses import dataclass
from functools import cache

from django.urls import URLResolver, get_resolver

# view name -> dialog size: lg for most forms; xl for wide forms, entry grids and review tables.
FORM_SURFACES = {
    "account_change_password": "lg",
    "account_set_password": "lg",
    "mfa_activate_totp": "lg",
    "mfa_deactivate_totp": "lg",
    "mfa_generate_recovery_codes": "lg",
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


_SIGN_IN = "a sign-in step: there is no session or workspace page to open it over"
_CONFIRM = (
    "confirms the signed-in person before a two-factor change (sign-in layout); the change it "
    "returns to opens in the dialog"
)
# Routes whose view takes a form but whose page is not a form page, with the reason.
NOT_FORM_PAGES = {
    "account_login": _SIGN_IN,
    "account_signup": _SIGN_IN + "; sign-up is closed",
    "account_request_login_code": _SIGN_IN,
    "account_confirm_login_code": _SIGN_IN,
    "account_reset_password": _SIGN_IN,
    "account_reset_password_from_key": "opened from an emailed link, usually signed out; a "
    "sign-in step",
    "account_confirm_email": "opened from an emailed link, often signed out; a sign-in step",
    "mfa_authenticate": _SIGN_IN,
    "account_reauthenticate": _CONFIRM,
    "account_request_reauthentication_code": _CONFIRM,
    "account_confirm_reauthentication_code": _CONFIRM,
    "mfa_reauthenticate": _CONFIRM,
    "account_email": "the account's email addresses, a state list; Make Primary, Re-send "
    "Verification and Remove act on the address chosen in the list itself, and Add Email opens "
    "in the dialog from the page (form_page local=)",
    "account_logout": "the sign-out confirmation; the user menu's Sign out posts directly",
    "equipment:import-detail": "a record (a validated reading import); Commit import opens in the "
    "dialog from the page (form_page local=)",
}

FORM_HEADER = "X-VR-Form"


def is_dialog_request(request):
    return request.headers.get(FORM_HEADER) == "1"


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
