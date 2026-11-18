"""Display helpers for audit history (includes/history_item.html)."""

from django import template

register = template.Library()


@register.filter
def action_label(code):
    """Readable history title for an audit action code: "box.moved" -> "Box moved"."""
    return str(code or "").replace(".", " ").replace("_", " ").capitalize()


@register.filter
def person(user):
    """A staff member's full name, else the username; empty for nobody."""
    return (user.get_full_name() or user.get_username()) if user else ""
