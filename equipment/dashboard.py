"""Equipment work queues for the staff dashboard (core.views).

Each count is computed with the query its linked list runs, so the number equals the rows the link
shows. The 30-day horizon is a display choice for the queue, not a maintenance policy.
"""

from django.db.models import Min
from django.urls import reverse

from accounts.roles import has_role

from . import queries
from .services import MANAGE_ROLES, OPERATE_ROLES, READ_ROLES


def _url(name, params):
    return reverse(name) + "?" + "&".join(f"{k}={v}" for k, v in params.items())


def queues(user):
    """[{label, count, url, oldest_at, note, tone}] for the user's roles; [] without a read role."""
    if not has_role(user, *READ_ROLES):
        return []
    result = []
    if has_role(user, *OPERATE_ROLES):
        uncovered = queries.uncovered_readings()
        result.append(
            {
                "label": "Readings outside limits without an excursion",
                "count": uncovered.count(),
                "url": _url("equipment:reading-list", {"outside": "uncovered"}),
                "oldest_at": uncovered.aggregate(at=Min("measured_at"))["at"],
                "note": "Outside the limits your repository set; record an excursion or correct "
                "the reading.",
                "tone": "danger",
            }
        )
    active = queries.active_excursions()
    result.append(
        {
            "label": "Open temperature excursions",
            "count": active.count(),
            "url": _url("equipment:excursion-list", {"status": "active"}),
            "oldest_at": active.aggregate(at=Min("started_at"))["at"],
            "note": "Open or awaiting review.",
            "tone": "warning",
        }
    )
    if has_role(user, *MANAGE_ROLES):
        review = queries.awaiting_review(user)
        result.append(
            {
                "label": "Excursions awaiting my review",
                "count": review.count(),
                "url": _url("equipment:excursion-list", {"awaiting_me": "on"}),
                "oldest_at": review.aggregate(at=Min("submitted_at"))["at"],
                "note": "Submitted by other people; you cannot close your own.",
                "tone": "progress",
            }
        )
    low = queries.low_level_equipment()
    result.append(
        {
            "label": "LN2 vessels below their low level",
            "count": low.count(),
            "url": _url("equipment:equipment-list", {"low_level": "on"}),
            "oldest_at": None,
            "note": "Latest recorded level is below the level your repository set.",
            "tone": "warning",
        }
    )
    overdue = queries.due_queryset(state="overdue")
    result.append(
        {
            "label": "Maintenance and calibration overdue",
            "count": overdue.count(),
            "url": _url("equipment:due-list", {"state": "overdue"}),
            "oldest_at": None,
            "note": "Past the next due date recorded on the latest event.",
            "tone": "danger",
        }
    )
    upcoming = queries.due_queryset(state="upcoming", within=queries.DASHBOARD_HORIZON)
    result.append(
        {
            "label": f"Maintenance and calibration due in {queries.DASHBOARD_HORIZON} days",
            "count": upcoming.count(),
            "url": _url(
                "equipment:due-list", {"state": "upcoming", "within": queries.DASHBOARD_HORIZON}
            ),
            "oldest_at": None,
            "note": "",
            "tone": "info",
        }
    )
    return result
