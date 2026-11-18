"""The one mapping from distribution states to status tones, and small display helpers.

Every page of the app renders a record's state through these tags (docs/ui-standards.md section
8: one partial per app maps domain states to the seven tones).
"""

from django import template

from distribution.models import (
    Agreement,
    Allocation,
    Consent,
    Shipment,
    ShipmentReceipt,
    SpecimenRequest,
    Study,
)
from distribution.queries import CODES

register = template.Library()

TONES = {
    "request": {
        SpecimenRequest.Status.DRAFT: "neutral",
        SpecimenRequest.Status.SUBMITTED: "progress",
        SpecimenRequest.Status.RETURNED: "warning",
        SpecimenRequest.Status.APPROVED: "success",
        SpecimenRequest.Status.REJECTED: "danger",
        SpecimenRequest.Status.CANCELLED: "neutral",
        SpecimenRequest.Status.CLOSED: "neutral",
    },
    "shipment": {
        Shipment.State.DRAFT: "neutral",
        Shipment.State.PACKED: "progress",
        Shipment.State.DISPATCHED: "info",
        Shipment.State.RECEIVED: "success",
        Shipment.State.CANCELLED: "neutral",
    },
    "allocation": {
        Allocation.State.ALLOCATED: "hold",
        Allocation.State.PICKED: "progress",
        Allocation.State.SHIPPED: "info",
        Allocation.State.RELEASED: "neutral",
    },
    "consent": {
        Consent.Status.CONSENTED: "success",
        Consent.Status.REFUSED: "neutral",
        Consent.Status.WITHDRAWN: "danger",
    },
    "study": {
        Study.Status.ACTIVE: "success",
        Study.Status.SUSPENDED: "warning",
        Study.Status.CLOSED: "neutral",
    },
    "agreement": {
        Agreement.Status.DRAFT: "neutral",
        Agreement.Status.EXECUTED: "success",
        Agreement.Status.TERMINATED: "danger",
    },
    "receipt": {
        ShipmentReceipt.Condition.AS_EXPECTED: "success",
        ShipmentReceipt.Condition.DISCREPANCY: "warning",
        ShipmentReceipt.Condition.DAMAGED: "danger",
        ShipmentReceipt.Condition.LOST: "danger",
    },
}
MODELS = {
    "request": SpecimenRequest.Status,
    "shipment": Shipment.State,
    "allocation": Allocation.State,
    "consent": Consent.Status,
    "study": Study.Status,
    "agreement": Agreement.Status,
    "receipt": ShipmentReceipt.Condition,
}


@register.inclusion_tag("includes/status.html")
def distribution_status(kind, value, plain=False):
    """{% distribution_status "request" request.status %}: label, tone and glyph."""
    return {"label": MODELS[kind](value).label, "tone": TONES[kind][value], "plain": plain}


@register.simple_tag
def distribution_status_parts(kind, value):
    """{% distribution_status_parts "request" request.status as st %}: the label and tone
    distribution_status shows, for a {% notice tone=st.tone label=st.label %} that opens with the
    record's status. Named per app: a template loading several libraries keeps the last tag."""
    return {"label": MODELS[kind](value).label, "tone": TONES[kind][value]}


@register.inclusion_tag("includes/status.html")
def blocker_status(blocker, plain=True):
    """A gate blocker as a danger stamp; its message is the accessible description."""
    return {
        "label": CODES.get(blocker.code, blocker.code),
        "tone": "danger",
        "plain": plain,
        "title": blocker.message,
    }


@register.inclusion_tag("includes/status.html")
def agreement_validity(agreement, day, plain=True):
    """Valid, not yet in effect, expired or terminated on `day` (computed, never stored)."""
    if agreement.status == Agreement.Status.DRAFT:
        label, tone = "Not executed", "neutral"
    elif agreement.is_valid_on(day):
        label, tone = "Valid", "success"
    elif agreement.terminated_on is not None and day >= agreement.terminated_on:
        label, tone = "Terminated", "danger"
    elif agreement.effective_on and day < agreement.effective_on:
        label, tone = "Not yet in effect", "warning"
    else:
        label, tone = "Expired", "danger"
    return {"label": label, "tone": tone, "plain": plain}
