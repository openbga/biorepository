"""The specimen record's Distribution rows (specimens.record_links): presentation only.

The requests that allocated the specimen and the participant's consents, each with its status,
for a user with distribution read access; nothing for anyone else. Two queries per record.
"""

from django.urls import reverse

from accounts.roles import has_role

from .models import Allocation, Consent
from .services import READ_ROLES
from .templatetags.distribution import MODELS, TONES


def specimen_rows(user, specimen):
    if not has_role(user, *READ_ROLES):
        return []
    rows, seen = [], set()
    allocations = (
        Allocation.objects.filter(specimen_id=specimen.pk)
        .select_related("request")
        .order_by("allocated_at", "pk")
    )
    for allocation in allocations:
        request = allocation.request
        if request.pk in seen:
            continue
        seen.add(request.pk)
        rows.append(
            {
                "label": request.number,
                "url": reverse("distribution:request-detail", args=[request.pk]),
                "status_label": MODELS["request"](request.status).label,
                "status_tone": TONES["request"][request.status],
            }
        )
    if specimen.participant_id:
        consents = (
            Consent.objects.filter(participant_id=specimen.participant_id)
            .select_related("participant", "study")
            .order_by("study__code")
        )
        for consent in consents:
            rows.append(
                {
                    "label": f"{consent.participant.code} · {consent.study.code}",
                    "url": reverse("distribution:consent-detail", args=[consent.pk]),
                    "status_label": MODELS["consent"](consent.status).label,
                    "status_tone": TONES["consent"][consent.status],
                }
            )
    return rows
