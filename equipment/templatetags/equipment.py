"""The one mapping from equipment states to status tones, and small display helpers."""

from django import template

from equipment import queries
from equipment.models import Equipment, Excursion, ReadingImport

register = template.Library()

EQUIPMENT_TONES = {
    Equipment.Status.IN_SERVICE: "success",
    Equipment.Status.OUT_OF_SERVICE: "warning",
    Equipment.Status.RETIRED: "neutral",
}
EXCURSION_TONES = {
    Excursion.Status.OPEN: "danger",
    Excursion.Status.REVIEW: "progress",
    Excursion.Status.CLOSED: "success",
}
IMPORT_TONES = {
    ReadingImport.State.VALIDATED: "info",
    ReadingImport.State.REJECTED: "danger",
    ReadingImport.State.COMMITTED: "success",
    ReadingImport.State.EXPIRED: "neutral",
}


def _status(label, tone, plain):
    return {"label": label, "tone": tone, "plain": plain}


@register.inclusion_tag("includes/status.html")
def equipment_status(status, plain=False):
    return _status(Equipment.Status(status).label, EQUIPMENT_TONES[status], plain)


@register.inclusion_tag("includes/status.html")
def excursion_status(status, plain=False):
    return _status(Excursion.Status(status).label, EXCURSION_TONES[status], plain)


@register.inclusion_tag("includes/status.html")
def equipment_import_status(state, plain=False):
    return _status(ReadingImport.State(state).label, IMPORT_TONES[state], plain)


@register.simple_tag
def equipment_status_parts(kind, value):
    """{% equipment_status_parts "excursion" excursion.status as st %}: the label and tone the
    matching status tag shows, for a {% notice tone=st.tone label=st.label %} opening with the
    record's status."""
    if kind == "equipment":
        return {"label": Equipment.Status(value).label, "tone": EQUIPMENT_TONES[value]}
    if kind == "excursion":
        return {"label": Excursion.Status(value).label, "tone": EXCURSION_TONES[value]}
    return {"label": ReadingImport.State(value).label, "tone": IMPORT_TONES[value]}


@register.inclusion_tag("includes/status.html")
def due_status(event, plain=True):
    label, tone, _ = queries.due_state(event)
    return _status(label, tone, plain)


@register.inclusion_tag("includes/status.html")
def reading_limit_status(reading, plain=True):
    assessment = reading.limit_assessment
    return _status(assessment.label, "danger" if assessment.is_outside else "neutral", plain)


@register.inclusion_tag("includes/status.html")
def entry_status(entry, plain=True):
    """Effective, corrected (superseded) or withdrawn, for log entries kept with corrections."""
    if entry.voided:
        return _status("Withdrawal", "neutral", plain)
    if getattr(entry, "is_superseded", False):
        return _status("Corrected", "neutral", plain)
    if entry.corrects_id:
        return _status("Correction", "info", plain)
    return _status("Effective", "success", plain)


@register.filter
def due_days(event):
    return queries.due_state(event)[2]


@register.filter
def level_change(entry):
    return queries.level_change(entry)


@register.filter
def signed(number):
    """+1.50 / -2.00 / 0.00 for a decimal change."""
    if number is None:
        return ""
    return f"+{number}" if number > 0 else str(number)
