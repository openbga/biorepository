"""Display helpers for specimen pages: status tones, amounts, durations and thaw counts.

The mapping from a specimen or disposal-request state to one of the seven status tones lives
here and nowhere else (docs/ui-standards.md section 8). Templates:
  {% load specimens %}
  {% specimen_status specimen %}                 record header stamp
  {% specimen_status specimen plain=True %}      table cell
  {% request_status request.state plain=True %}  disposal request state
  {{ specimen.quantity_on_hand|amount:specimen.unit }}   "0.5 mL" (exact, trailing zeros dropped)
  {{ delta|duration }}                            "1 d 2 h 5 min"
"""

from decimal import Decimal

from django import template

from ..models import Hold
from ..queries import format_duration

register = template.Library()

SPECIMEN_STATES = {
    "available": ("Available", "success"),
    "exhausted": ("Exhausted", "warning"),
    "disposed": ("Disposed", "neutral"),
    "distributed": ("Distributed", "info"),
}
REQUEST_STATES = {
    "pending": ("Awaiting approval", "progress"),
    "approved": ("Approved, not yet disposed", "info"),
    "stale": ("Out of date", "warning"),
    "rejected": ("Rejected", "danger"),
    "cancelled": ("Cancelled", "neutral"),
    "executed": ("Disposed", "neutral"),
}
IMPORT_STATES = {
    "invalid": ("Has errors", "danger"),
    "valid": ("Ready to commit", "progress"),
    "expired": ("Expired", "neutral"),
    "committed": ("Committed", "success"),
}


@register.inclusion_tag("includes/status.html")
def specimen_status(specimen, plain=False, held=False, size=None):
    """held=True folds an active hold into the label ("Available, on hold", hold tone): the
    record header's one status. The view sets specimen.held; lists show it as a second label."""
    label, tone = SPECIMEN_STATES[specimen.status]
    if held and getattr(specimen, "held", False):
        label, tone = f"{label}, on hold", "hold"
    return {"label": label, "tone": tone, "plain": plain, "size": size}


@register.inclusion_tag("includes/status.html")
def request_status(state, plain=False):
    label, tone = REQUEST_STATES[state]
    return {"label": label, "tone": tone, "plain": plain}


@register.inclusion_tag("includes/status.html")
def import_status(state, plain=False):
    label, tone = IMPORT_STATES[state]
    return {"label": label, "tone": tone, "plain": plain}


@register.inclusion_tag("includes/status.html")
def hold_status(scope, plain=False):
    label = Hold.Scope(scope).label if scope in Hold.Scope.values else str(scope)
    return {"label": label, "tone": "hold", "plain": plain}


@register.simple_tag
def specimen_status_parts(kind, value):
    """{label, tone} of a status, for components that open with it ({% notice %}):
    {% specimen_status_parts "request" state as st %}{% notice tone=st.tone label=st.label %}…"""
    if kind == "specimen":
        label, tone = SPECIMEN_STATES[value.status]
    elif kind == "request":
        label, tone = REQUEST_STATES[value]
    elif kind == "import":
        label, tone = IMPORT_STATES[value]
    elif kind == "hold":
        label = Hold.Scope(value).label if value in Hold.Scope.values else str(value)
        tone = "hold"
    else:
        raise ValueError(f"Unknown status kind {kind!r}.")
    return {"label": label, "tone": tone}


@register.filter
def amount(value, unit=None):
    """Exact decimal without trailing zeros, with the unit code: 0.500000 -> '0.5 mL'."""
    if value is None or value == "":
        return ""
    number = value if isinstance(value, Decimal) else Decimal(str(value))
    text = format(number.normalize(), "f") if number else "0"
    code = getattr(unit, "code", unit)
    return f"{text} {code}" if code else text


@register.filter
def duration(value):
    return format_duration(value)


@register.filter
def thaw_short(summary):
    """The thaw count alone, for the record header: "3", "At least 0" or "Unknown"."""
    if summary.baseline is None:
        return "Unknown"
    if summary.unknown_away:
        return f"At least {summary.baseline + summary.recorded}"
    return str(summary.total)


@register.filter
def thaw_text(summary):
    return str(summary)
