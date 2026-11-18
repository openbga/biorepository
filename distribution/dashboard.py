"""Distribution work queues for the staff dashboard (core.views).

Each queue counts the rows its linked list shows: the count runs the list's own filter
(`request_lists.filtered` / `governance_lists.filtered`) with the same parameters as the link,
so the two cannot drift. Request decisions use the list's "Awaiting my decision" filter
(requests the user neither created nor submitted). The 60-day agreement horizon is a display
choice, not a rule. Empty tables give zero counts.
"""

from django.db.models import Min
from django.urls import reverse

from accounts.roles import can_decide_requests, has_role

from . import governance_lists, request_lists
from .models import Allocation, RequestSubmission
from .request_lists import awaiting_decision  # noqa: F401 (the decision rule, used by tests)
from .services import MANAGE_ROLES, OPERATE_ROLES, READ_ROLES


def _url(name, params):
    return reverse(name) + "?" + "&".join(f"{k}={v}" for k, v in params.items())


def _rows(lists, key, params, user):
    """The rows the list at `key` shows for `params` (no search, any sort)."""
    return lists.filtered(lists.SPECS[key], params, user)[1]


AREA = "Distribution"  # the dashboard row group (templates/core/dashboard.html)


def queues(user):
    """[{area, label, count, url, oldest_at, note, tone}] for the user's roles; [] without a
    read role."""
    if not has_role(user, *READ_ROLES):
        return []
    result = []
    if can_decide_requests(user) and has_role(user, *OPERATE_ROLES):
        params = {"awaiting_me": "on"}
        awaiting = _rows(request_lists, "request", params, user)
        result.append(
            {
                "area": AREA,
                "key": "request_decide",
                "label": "Requests awaiting my decision",
                "count": awaiting.count(),
                "url": _url("distribution:request-list", params),
                "oldest_at": RequestSubmission.objects.filter(
                    request__in=awaiting.values("pk")
                ).aggregate(at=Min("submitted_at"))["at"],
                "note": "Submitted by other people; you cannot decide a request you created or "
                "submitted.",
                "tone": "progress",
            }
        )
    if has_role(user, *OPERATE_ROLES):
        params = {"status": "approved", "to_pick": "1"}
        picking = _rows(request_lists, "request", params, user)
        result.append(
            {
                "area": AREA,
                "key": "request_pick",
                "label": "Approved requests with allocations to pick",
                "count": picking.count(),
                "url": _url("distribution:request-list", params),
                "oldest_at": Allocation.objects.filter(
                    request__in=picking.values("pk"), state=Allocation.State.ALLOCATED
                ).aggregate(at=Min("allocated_at"))["at"],
                "note": "",
                "tone": "info",
            }
        )
        params = {"state": "packed"}
        packed = _rows(request_lists, "shipment", params, user)
        result.append(
            {
                "area": AREA,
                "key": "shipment_packed",
                "label": "Shipments packed, awaiting dispatch",
                "count": packed.count(),
                "url": _url("distribution:shipment-list", params),
                "oldest_at": packed.aggregate(at=Min("packed_at"))["at"],
                "note": "",
                "tone": "progress",
            }
        )
        params = {"state": "dispatched"}
        dispatched = _rows(request_lists, "shipment", params, user)
        result.append(
            {
                "area": AREA,
                "label": "Dispatched shipments without a recorded receipt",
                "count": dispatched.count(),
                "url": _url("distribution:shipment-list", params),
                "oldest_at": dispatched.aggregate(at=Min("dispatched_at"))["at"],
                "note": "Record the recipient's confirmation when it arrives.",
                "tone": "info",
            }
        )
    if has_role(user, *MANAGE_ROLES):
        params = {"review": "awaiting"}
        returns = _rows(request_lists, "return", params, user)
        result.append(
            {
                "area": AREA,
                "key": "returns_review",
                "label": "Returns awaiting review",
                "count": returns.count(),
                "url": _url("distribution:return-list", params),
                "oldest_at": returns.aggregate(at=Min("occurred_at"))["at"],
                "note": "Returned specimens stay on a use hold until a manager reviews them.",
                "tone": "hold",
            }
        )
        params = {"attention": "1"}
        attention = _rows(governance_lists, "withdrawal", params, user)
        result.append(
            {
                "area": AREA,
                "label": "Withdrawals needing follow-up",
                "count": attention.count(),
                "url": _url("distribution:withdrawal-list", params),
                "oldest_at": attention.aggregate(at=Min("recorded_at"))["at"],
                "note": "Specimens in custody without a withdrawal hold, or distributed material "
                "without recorded follow-up.",
                "tone": "danger",
            }
        )
        params = {"horizon": "1"}
        horizon = _rows(governance_lists, "agreement", params, user)
        result.append(
            {
                "area": AREA,
                "label": "Agreements expiring or with reporting due within "
                f"{governance_lists.HORIZON_DAYS} days",
                "count": horizon.count(),
                "url": _url("distribution:agreement-list", params),
                "oldest_at": None,
                "note": "Dates as recorded on the agreements; nothing expires or is enforced "
                "automatically except validity at dispatch.",
                "tone": "warning",
            }
        )
    return result
