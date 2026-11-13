"""Display helpers for audit history (includes/history_item.html)."""

from django import template

register = template.Library()


@register.filter
def action_label(code):
    """Readable history title for an audit action code: "box.moved" -> "Box moved"."""
    return str(code or "").replace(".", " ").replace("_", " ").capitalize()
