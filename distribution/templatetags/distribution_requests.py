"""Small display helpers for the request, shipment and return pages.

State tones come from `distribution` (the app's one mapping); this module only adds the review
state of a returned specimen (computed from its reviews, not stored) and people's names.
"""

from django import template

register = template.Library()

RETURN_STATES = {
    "awaiting": ("Awaiting review", "warning"),
    "kept": ("Kept on hold", "hold"),
    "released": ("Released for use", "success"),
}


@register.inclusion_tag("includes/status.html")
def return_status(state, plain=True):
    """{% return_status row.review_state %}: awaiting, kept or released."""
    label, tone = RETURN_STATES.get(state, (state, "neutral"))
    return {"label": label, "tone": tone, "plain": plain}
