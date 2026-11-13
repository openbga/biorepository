"""Specimen work queues and recently stored specimens for the staff dashboard (core.views).

Each queue's count is computed with the same query its linked list runs, so the number on the
dashboard equals the number of rows the link shows.
"""

from django.db.models import Min
from django.urls import reverse

from accounts.roles import can_approve_disposal, has_role

from .models import CustodyEvent, Hold
from .queries import disposal_queryset, specimen_queryset
from .services import OPERATE_ROLES, READ_ROLES

UNSTORED = {"status": "available", "placed": "no", "checked_out": "no"}


def _url(name, params):
    return reverse(name) + "?" + "&".join(f"{k}={v}" for k, v in params.items())


def queues(user):
    """[{label, count, url, oldest_at, note, tone}] for the user's roles; [] without a read role."""
    if not has_role(user, *READ_ROLES):
        return []
    result = []
    checked_out = specimen_queryset({"checked_out": "yes"})
    result.append(
        {
            "label": "Checked out",
            "count": checked_out.count(),
            "url": _url("specimens:specimen-list", {"checked_out": "yes"}),
            "oldest_at": checked_out.aggregate(at=Min("open_checkout__occurred_at"))["at"],
            "note": "Out of storage now; time out of storage is running.",
            "tone": "warning",
        }
    )
    unstored = specimen_queryset(UNSTORED)
    result.append(
        {
            "label": "Registered, not stored",
            "count": unstored.count(),
            "url": _url("specimens:specimen-list", UNSTORED),
            "oldest_at": unstored.aggregate(at=Min("registered_at"))["at"],
            "note": "Available specimens without a storage position.",
            "tone": "progress",
        }
    )
    held = specimen_queryset({"held": "yes"})
    result.append(
        {
            "label": "On hold",
            "count": held.count(),
            "url": _url("specimens:specimen-list", {"held": "yes"}),
            "oldest_at": Hold.objects.filter(
                release__isnull=True, specimen__in=held.values("pk")
            ).aggregate(at=Min("occurred_at"))["at"],
            "note": "",
            "tone": "hold",
        }
    )
    if can_approve_disposal(user):
        awaiting = disposal_queryset({"awaiting_me": True, "actor": user})
        result.append(
            {
                "label": "Disposal requests awaiting my approval",
                "count": awaiting.count(),
                "url": _url("specimens:disposal-list", {"awaiting_me": "on"}),
                "oldest_at": awaiting.aggregate(at=Min("recorded_at"))["at"],
                "note": "Requests by other people; you cannot approve your own.",
                "tone": "progress",
            }
        )
    if has_role(user, *OPERATE_ROLES):
        approved = disposal_queryset({"state": "approved"})
        result.append(
            {
                "label": "Approved disposals to record",
                "count": approved.count(),
                "url": _url("specimens:disposal-list", {"state": "approved"}),
                "oldest_at": approved.aggregate(at=Min("decision__recorded_at"))["at"],
                "note": "Dispose of the material, then record the disposal.",
                "tone": "info",
            }
        )
    return result


def recently_stored(user, limit=10):
    """The newest placements and check-ins, with the path recorded at the time."""
    if not has_role(user, *READ_ROLES):
        return []
    return list(
        CustodyEvent.objects.filter(kind__in=(CustodyEvent.Kind.PLACE, CustodyEvent.Kind.CHECK_IN))
        .select_related("specimen", "actor", "move_event")
        .order_by("-occurred_at", "-recorded_at")[:limit]
    )
