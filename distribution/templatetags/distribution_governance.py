"""Small display helpers for the governance pages. Record states use the app's one tone mapping
(distribution.templatetags.distribution); these cover withdrawals, answers and people."""

from django import template

from distribution.models import ConsentWithdrawal

register = template.Library()

ANSWER_TONES = {"Permitted": "success", "Refused": "danger"}


@register.filter
def person(user):
    """A staff member's full name, else the username; empty for None."""
    if not user:
        return ""
    return user.get_full_name() or user.get_username()


@register.inclusion_tag("includes/status.html")
def withdrawal_status(withdrawal, plain=True):
    """In effect (use stopped), Contact only, or Reinstated. Expects `is_reinstated` or
    `reinstated` on the row."""
    reinstated = getattr(withdrawal, "is_reinstated", None)
    if reinstated is None:
        reinstated = getattr(withdrawal, "reinstated", None) is not None
    if reinstated:
        label, tone = "Reinstated", "neutral"
    elif withdrawal.kind == ConsentWithdrawal.Kind.NO_CONTACT:
        label, tone = "Do not contact", "info"
    else:
        label, tone = "In effect", "hold"
    return {"label": label, "tone": tone, "plain": plain}


@register.inclusion_tag("includes/status.html")
def answer_status(label, plain=True):
    """Permitted, Refused or Not addressed for one scope category of a consent record."""
    return {"label": label, "tone": ANSWER_TONES.get(label, "neutral"), "plain": plain}
