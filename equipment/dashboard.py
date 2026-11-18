"""Equipment work queues for the staff dashboard (core.views).

Each queue counts the rows its linked list shows: the count runs the list's own filter
(`lists.filtered`) with the same parameters as the link, so the two cannot drift. The 30-day
horizon is a display choice for the queue, not a maintenance policy.
"""

from django.db.models import Min
from django.urls import reverse

from accounts.roles import has_role

from . import lists, queries
from .services import MANAGE_ROLES, OPERATE_ROLES, READ_ROLES


def _url(name, params):
    return reverse(name) + "?" + "&".join(f"{k}={v}" for k, v in params.items())


def _rows(key, params, user):
    """The rows the list at `key` shows for `params` (no search, any sort)."""
    return lists.filtered(lists.SPECS[key], params, user)[1]


def queues(user):
    """[{label, count, url, oldest_at, note, tone}] for the user's roles; [] without a read role."""
    if not has_role(user, *READ_ROLES):
        return []
    result = []
    if has_role(user, *OPERATE_ROLES):
        uncovered_params = {"outside": "uncovered"}
        uncovered = _rows("reading", uncovered_params, user)
        result.append(
            {
                "label": "Readings outside limits without an excursion",
                "count": uncovered.count(),
                "url": _url("equipment:reading-list", uncovered_params),
                "oldest_at": uncovered.aggregate(at=Min("measured_at"))["at"],
                "note": "Outside the limits your repository set; record an excursion or correct "
                "the reading.",
                "tone": "danger",
            }
        )
    active_params = {"status": "active"}
    active = _rows("excursion", active_params, user)
    result.append(
        {
            "label": "Open temperature excursions",
            "count": active.count(),
            "url": _url("equipment:excursion-list", active_params),
            "oldest_at": active.aggregate(at=Min("started_at"))["at"],
            "note": "Open or awaiting review.",
            "tone": "warning",
        }
    )
    if has_role(user, *MANAGE_ROLES):
        review_params = {"awaiting_me": "on"}
        review = _rows("excursion", review_params, user)
        result.append(
            {
                "label": "Excursions awaiting my review",
                "count": review.count(),
                "url": _url("equipment:excursion-list", review_params),
                "oldest_at": review.aggregate(at=Min("submitted_at"))["at"],
                "note": "Submitted by other people; you cannot close your own.",
                "tone": "progress",
            }
        )
    low_params = {"low_level": "on"}
    low = _rows("equipment", low_params, user)
    result.append(
        {
            "label": "LN2 vessels below their low level",
            "count": low.count(),
            "url": _url("equipment:equipment-list", low_params),
            "oldest_at": None,
            "note": "Latest recorded level is below the level your repository set.",
            "tone": "warning",
        }
    )
    overdue_params = {"state": "overdue"}
    overdue = _rows("due", overdue_params, user)
    result.append(
        {
            "label": "Maintenance and calibration overdue",
            "count": overdue.count(),
            "url": _url("equipment:due-list", overdue_params),
            "oldest_at": None,
            "note": "Past the next due date recorded on the latest event.",
            "tone": "danger",
        }
    )
    upcoming_params = {"state": "upcoming", "within": queries.DASHBOARD_HORIZON}
    upcoming = _rows("due", upcoming_params, user)
    result.append(
        {
            "label": f"Maintenance and calibration due in {queries.DASHBOARD_HORIZON} days",
            "count": upcoming.count(),
            "url": _url("equipment:due-list", upcoming_params),
            "oldest_at": None,
            "note": "",
            "tone": "info",
        }
    )
    return result
