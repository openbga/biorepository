"""Display labels for stored storage codes (templates keep recipes as plain JSON)."""

from django import template
from django.apps import apps

from storage.models import StorageUnit

register = template.Library()


@register.filter
def technology_label(value):
    """ "ln2_vapor" -> "LN2 vapor phase"; unknown values print as stored."""
    return dict(StorageUnit.Technology.choices).get(value, value)


@register.filter
def app_installed(label):
    """{% if "equipment"|app_installed %}: show a link to another app's pages only when that
    app is installed. Storage names other apps by URL and template only, never by import."""
    return apps.is_installed(label)


@register.filter
def count_held(positions):
    """How many of the given positions hold an item another app marked held (claim.specimen.held,
    set by specimens' attach_specimens); 0 when nothing marked them. Storage learns no more."""
    return sum(
        1
        for p in positions
        if getattr(getattr(getattr(p, "claim", None), "specimen", None), "held", False)
    )
