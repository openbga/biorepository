"""Application navigation: which sidebar item each page marks, and the section tab sets.

This module is the one declaration of the route-to-item mapping (docs/ui-standards.md section 3,
rule 4). Every page beneath a list marks that list's item, so each item names the routes it owns
besides its own list. A route appears under one item at most; routes reached from the user menu
(account pages) mark none. tests/test_navigation.py checks that every HTML route is declared here
or in its own exemption lists, and that each item's label equals its list page's title.

Templates use:
  {% load navigation %}
  {% nav_item "dashboard" %}          one sidebar item (includes/nav_link.html)
  {% section_tabs "storage" %}         a declared tab set (includes/section_tabs.html)

Which items a user sees is declared here too (NavItem.roles, NavItem.superuser) and read through
visible(): the menus and What's new's "Go to" links both use it, so neither offers a page the user
cannot open. Views enforce access anyway.
"""

from dataclasses import dataclass

from django import template
from django.core.exceptions import ImproperlyConfigured
from django.urls import NoReverseMatch, reverse

from accounts.roles import ROLES, effective_roles

register = template.Library()


@dataclass(frozen=True)
class NavItem:
    key: str
    label: str
    url_name: str  # the list or overview the item opens; it marks the item too
    icon: str = ""
    routes: tuple[str, ...] = ()  # further view names (record, form, action pages) it marks
    roles: tuple[str, ...] = ROLES  # effective roles (accounts.roles) that see the item
    superuser: bool = False  # shown to superusers only (Django admin), whatever their roles


@dataclass(frozen=True)
class Tab:
    label: str
    url_name: str


@dataclass(frozen=True)
class TabSet:
    label: str  # accessible name of the <nav>
    tabs: tuple[Tab, ...]


STAFF_NAV = (
    NavItem("dashboard", "Dashboard", "dashboard", "house"),
    NavItem(
        "quality-deviations",
        "Deviations",
        "quality:deviation-list",
        "exclamation-octagon",
        (
            "quality:deviation-create",
            "quality:deviation-detail",
            "quality:deviation-edit",
            "quality:deviation-submit",
            "quality:deviation-review",
            "quality:deviation-reopen",
        ),
    ),
    NavItem(
        "quality-audits",
        "Inventory audits",
        "quality:audit-list",
        "clipboard-check",
        (
            "quality:audit-create",
            "quality:audit-detail",
            "quality:audit-complete",
            "quality:audit-cancel",
        ),
    ),
    NavItem(
        "quality-training",
        "Training records",
        "quality:training-list",
        "journal-text",
        (
            "quality:training-create",
            "quality:training-detail",
            "quality:training-edit",
            "quality:training-complete",
            "quality:training-correct",
            "quality:training-cancel",
        ),
    ),
    # Storage
    NavItem("storage-overview", "Storage overview", "storage:overview", "layers"),
    NavItem(
        "storage-units",
        "Storage units",
        "storage:unit-list",
        "archive",
        ("storage:unit-create", "storage:unit-detail", "storage:unit-edit", "storage:retire"),
    ),
    NavItem(
        "storage-nodes",
        "Racks and shelves",
        "storage:node-list",
        "layout-three-columns",
        ("storage:node-create", "storage:node-detail", "storage:node-edit"),
    ),
    NavItem(
        "storage-boxes",
        "Boxes",
        "storage:box-list",
        "box-seam",
        (
            "storage:box-create",
            "storage:box-detail",
            "storage:box-edit",
            "storage:container-move",
            "storage:position-block",
        ),
    ),
    NavItem("storage-free", "Free positions", "storage:free-position-list", "inbox"),
    NavItem(
        "storage-sites",
        "Sites",
        "storage:site-list",
        "building",
        ("storage:site-create", "storage:site-detail", "storage:site-edit"),
    ),
    NavItem(
        "storage-rooms",
        "Rooms",
        "storage:room-list",
        "folder2",
        ("storage:room-create", "storage:room-detail", "storage:room-edit"),
    ),
    # Specimens
    NavItem(
        "specimens",
        "Specimens",
        "specimens:specimen-list",
        "droplet",
        (
            "specimens:specimen-create",
            "specimens:specimen-detail",
            "specimens:label-preview",
            "specimens:place",
            "specimens:relocate",
            "specimens:check-out",
            "specimens:check-in",
            "specimens:transfer",
            "specimens:consume",
            "specimens:adjust",
            "specimens:reverse",
            "specimens:derive",
            "specimens:hold-add",
            "specimens:hold-release",
            "specimens:thaw-record",
            "specimens:thaw-correct",
            "specimens:custody-correct",
            "specimens:correct",
            "specimens:disposal-request",
        ),
    ),
    NavItem(
        "specimen-disposals",
        "Disposal requests",
        "specimens:disposal-list",
        "clipboard-check",
        ("specimens:disposal-detail", "specimens:disposal-decide", "specimens:disposal-execute"),
    ),
    NavItem(
        "specimen-imports",
        "Imports",
        "specimens:import-list",
        "file-earmark-arrow-up",
        ("specimens:import-upload", "specimens:import-detail", "specimens:import-label-preview"),
        roles=("admin", "manager", "technician"),  # specimens OPERATE_ROLES
    ),
    # Equipment
    NavItem(
        "equipment",
        "Equipment",
        "equipment:equipment-list",
        "tools",
        (
            "equipment:equipment-create",
            "equipment:equipment-detail",
            "equipment:equipment-edit",
            "equipment:status-change",
            "equipment:service-create",
            "equipment:service-correct",
        ),
    ),
    NavItem("equipment-due", "Maintenance due", "equipment:due-list", "clock-history"),
    NavItem(
        "equipment-ln2",
        "LN2 log",
        "equipment:ln2-list",
        "clipboard-data",
        ("equipment:ln2-create", "equipment:ln2-correct"),
    ),
    NavItem(
        "equipment-readings",
        "Temperature readings",
        "equipment:reading-list",
        "graph-up",
        (
            "equipment:reading-create",
            "equipment:reading-correct",
            "equipment:import-list",
            "equipment:import-upload",
            "equipment:import-detail",
        ),
    ),
    NavItem(
        "equipment-excursions",
        "Temperature excursions",
        "equipment:excursion-list",
        "exclamation-octagon",
        (
            "equipment:excursion-create",
            "equipment:excursion-detail",
            "equipment:excursion-edit",
            "equipment:excursion-submit",
            "equipment:excursion-review",
            "equipment:excursion-reopen",
            "equipment:excursion-note",
            "equipment:excursion-holds",
        ),
    ),
    # Distribution
    NavItem(
        "distribution-requests",
        "Requests",
        "distribution:request-list",
        "clipboard-data",
        (
            "distribution:request-create",
            "distribution:request-detail",
            "distribution:request-edit",
            "distribution:request-items",
            "distribution:request-submit",
            "distribution:request-decide",
            "distribution:request-reopen",
            "distribution:request-cancel",
            "distribution:request-close",
            "distribution:request-agreement",
            "distribution:request-note",
            "distribution:allocate",
            "distribution:allocation-release",
            "distribution:pick",
            "distribution:put-back",
            "distribution:pick-list",
        ),
    ),
    NavItem(
        "distribution-shipments",
        "Shipments",
        "distribution:shipment-list",
        "truck",
        (
            "distribution:shipment-create",
            "distribution:shipment-detail",
            "distribution:shipment-edit",
            "distribution:shipment-contents",
            "distribution:shipment-pack",
            "distribution:shipment-unpack",
            "distribution:shipment-notify",
            "distribution:shipment-dispatch",
            "distribution:shipment-receipt",
            "distribution:receipt-correct",
            "distribution:shipment-cancel",
            "distribution:shipment-note",
            "distribution:manifest",
            "distribution:return-list",
            "distribution:return-record",
            "distribution:return-review",
        ),
    ),
    NavItem(
        "distribution-agreements",
        "Agreements",
        "distribution:agreement-list",
        "file-earmark-text",
        (
            "distribution:agreement-create",
            "distribution:agreement-detail",
            "distribution:agreement-edit",
            "distribution:agreement-execute",
            "distribution:agreement-terminate",
            "distribution:agreement-document",
        ),
    ),
    NavItem(
        "distribution-studies",
        "Studies",
        "distribution:study-list",
        "journal-text",
        (
            "distribution:study-create",
            "distribution:study-detail",
            "distribution:study-edit",
            "distribution:study-collection-link",
            "distribution:study-collection-end",
        ),
    ),
    NavItem(
        "distribution-consents",
        "Consent",
        "distribution:consent-list",
        "patch-check",
        (
            "distribution:consent-create",
            "distribution:consent-detail",
            "distribution:consent-correct",
            "distribution:consent-withdraw",
            "distribution:consent-escalate",
            "distribution:consent-hold",
            "distribution:consent-disposals",
            "distribution:consent-reinstate",
            "distribution:withdrawal-list",
            "distribution:withdrawal-follow-up",
        ),
    ),
    # Setup
    NavItem("record-retention", "Record retention", "record-retention", "lock"),
    NavItem(
        "announcements",
        "Announcements",
        "announcement-list",
        "megaphone",
        ("announcement-create", "announcement-edit"),
        roles=("admin",),  # core.announcements.ANNOUNCEMENT_ROLES
    ),
    NavItem(
        "storage-catalogues",
        "Storage catalogues",
        "storage:class-list",
        "rulers",
        (
            "storage:class-create",
            "storage:class-detail",
            "storage:class-edit",
            "storage:container-type-list",
            "storage:container-type-create",
            "storage:container-type-detail",
            "storage:container-type-edit",
            "storage:template-list",
            "storage:template-create",
            "storage:template-detail",
            "storage:template-edit",
            "storage:template-apply",
        ),
    ),
    NavItem(
        "specimen-catalogues",
        "Specimen catalogues",
        "specimens:participant-list",
        "tag",
        (
            "specimens:participant-create",
            "specimens:participant-edit",
            "specimens:collection-list",
            "specimens:collection-create",
            "specimens:collection-edit",
            "specimens:specimen-type-list",
            "specimens:specimen-type-create",
            "specimens:specimen-type-edit",
            "specimens:container-type-list",
            "specimens:container-type-create",
            "specimens:container-type-edit",
            "specimens:unit-list",
            "specimens:unit-create",
            "specimens:unit-edit",
        ),
    ),
    NavItem(
        "distribution-catalogues",
        "Distribution catalogues",
        "distribution:category-list",
        "list-check",
        (
            "distribution:category-create",
            "distribution:category-edit",
            "distribution:consent-form-list",
            "distribution:consent-form-create",
            "distribution:consent-form-edit",
            "distribution:institution-list",
            "distribution:institution-create",
            "distribution:institution-edit",
            "distribution:investigator-list",
            "distribution:investigator-create",
            "distribution:investigator-edit",
        ),
    ),
    # Django admin renders its own chrome, so no shell page marks this item.
    NavItem("accounts-roles", "Accounts & roles", "admin:index", "people", superuser=True),
)

# Pages reached from the user menu or the top bar (What's new): they render inside the user's
# shell and mark no item.
ACCOUNT_ROUTES = frozenset(
    {
        "whats-new",
        "account_password",
        "account_change_password",
        "account_set_password",
        "account_email",
        "account_logout",
        "mfa_index",
        "mfa_activate_totp",
        "mfa_deactivate_totp",
        "mfa_view_recovery_codes",
        "mfa_generate_recovery_codes",
    }
)

# Section tabs: one sidebar item whose content is two to six sibling lists (standard section 3,
# rule 2). Every list in a set marks the same sidebar item.
SECTION_TABS = {
    "storage-catalogues": TabSet(
        "Storage catalogues",
        (
            Tab("Storage classes", "storage:class-list"),
            Tab("Container types", "storage:container-type-list"),
            Tab("Storage templates", "storage:template-list"),
        ),
    ),
    "specimen-catalogues": TabSet(
        "Specimen catalogues",
        (
            Tab("Participants", "specimens:participant-list"),
            Tab("Collections", "specimens:collection-list"),
            Tab("Specimen types", "specimens:specimen-type-list"),
            Tab("Specimen containers", "specimens:container-type-list"),
            Tab("Units", "specimens:unit-list"),
        ),
    ),
    "distribution-shipments": TabSet(
        "Shipments",
        (
            Tab("Shipments", "distribution:shipment-list"),
            Tab("Returns", "distribution:return-list"),
        ),
    ),
    "distribution-consents": TabSet(
        "Consent",
        (
            Tab("Consents", "distribution:consent-list"),
            Tab("Withdrawals", "distribution:withdrawal-list"),
        ),
    ),
    "distribution-catalogues": TabSet(
        "Distribution catalogues",
        (
            Tab("Scope categories", "distribution:category-list"),
            Tab("Consent forms", "distribution:consent-form-list"),
            Tab("Institutions", "distribution:institution-list"),
            Tab("Investigators", "distribution:investigator-list"),
        ),
    ),
    "equipment-readings": TabSet(
        "Temperature readings",
        (
            Tab("Readings", "equipment:reading-list"),
            Tab("Imports", "equipment:import-list"),
        ),
    ),
}

# Routes whose page belongs to another item depending on the record it shows. The route is
# declared under its usual item above; its view may name one of the alternatives listed here as
# "nav_item" in the template context. Any other value is ignored.
SHARED_ROUTES = {
    # Moving or retiring a rack marks Racks and shelves; a box, Boxes (storage.views).
    "storage:container-move": frozenset({"storage-nodes"}),
    "storage:retire": frozenset({"storage-boxes", "storage-nodes"}),
}

ITEMS = {item.key: item for item in STAFF_NAV}


def _route_items():
    mapping = {}
    for item in STAFF_NAV:
        for route in (item.url_name, *item.routes):
            if route in mapping:
                raise ImproperlyConfigured(
                    f"Route {route!r} marks both {mapping[route]!r} and {item.key!r}."
                )
            mapping[route] = item.key
    if len(ITEMS) != len(STAFF_NAV):
        raise ImproperlyConfigured("Navigation item keys must be unique.")
    return mapping


ROUTE_ITEMS = _route_items()


def visible(key, user, roles=None):
    """Whether `user` sees item `key`; `roles` saves a query when the effective roles are known."""
    item = ITEMS[key]
    if item.superuser:
        return bool(getattr(user, "is_superuser", False))
    if roles is None:
        roles = effective_roles(user)
    return bool(set(item.roles) & set(roles))


def current_item_key(request, chosen=None):
    """The key of the item the request's page marks, or None (account, error, unmapped).

    `chosen` is the view's "nav_item" context value; it counts only for a declared shared route.
    """
    match = getattr(request, "resolver_match", None)
    if not match:
        return None
    if chosen in SHARED_ROUTES.get(match.view_name, ()):
        return chosen
    return ROUTE_ITEMS.get(match.view_name)


def _url(url_name):
    try:
        return reverse(url_name)
    except NoReverseMatch:  # not mounted: the item renders as "Planned"
        return ""


def _item_context(context, key):
    item = ITEMS[key]
    request = context.get("request")
    return {
        "visible": visible(key, context.get("user"), context.get("user_roles")),
        "url": _url(item.url_name),
        "label": item.label,
        "icon": item.icon,
        "current": request is not None
        and current_item_key(request, context.get("nav_item")) == key,
    }


@register.inclusion_tag("includes/nav_link.html", takes_context=True)
def nav_item(context, key):
    """One staff sidebar item, marked current when the page belongs to it."""
    return _item_context(context, key)


@register.inclusion_tag("includes/section_tabs.html", takes_context=True)
def section_tabs(context, name):
    """A declared tab set rendered through includes/section_tabs.html."""
    tab_set = SECTION_TABS[name]
    match = getattr(context.get("request"), "resolver_match", None)
    view_name = match.view_name if match else None
    return {
        "label": tab_set.label,
        "tabs": [
            {"label": tab.label, "url": _url(tab.url_name), "current": tab.url_name == view_name}
            for tab in tab_set.tabs
        ],
    }
