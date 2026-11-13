"""Distribution work queues for the staff dashboard (core.views).

Each queue's label links to the list with the filter its count is computed with: request-list
`status=submitted` and `status=approved&to_pick=1`, shipment-list `state=packed` and
`state=dispatched`, return-list `review=awaiting` (the request pages own those filters),
withdrawal-list `attention=1` and agreement-list `horizon=1` (governance_lists). Request
decisions count only requests the user may decide (not created or submitted by them), so that
count can be lower than the list, which shows every request awaiting a decision. The 60-day
agreement horizon is a display choice, not a rule. Empty tables give zero counts.
"""

from django.db.models import Exists, Min, OuterRef
from django.urls import reverse

from accounts.roles import can_decide_requests, has_role

from . import governance_lists
from .models import (
    Allocation,
    RequestSubmission,
    ReturnReview,
    Shipment,
    SpecimenRequest,
    SpecimenReturn,
)
from .services import MANAGE_ROLES, OPERATE_ROLES, READ_ROLES


def _url(name, params):
    return reverse(name) + "?" + "&".join(f"{k}={v}" for k, v in params.items())


def awaiting_decision(user):
    """Submitted requests this user may decide: not created by them, latest submission not
    theirs (the service repeats both checks under lock)."""
    mine = RequestSubmission.objects.filter(
        request=OuterRef("pk"), submitted_by=user, number=OuterRef("latest_submission")
    )
    return (
        SpecimenRequest.objects.filter(status=SpecimenRequest.Status.SUBMITTED)
        .exclude(created_by=user)
        .exclude(Exists(mine))
    )


def to_pick():
    """Approved requests with allocations still to pick."""
    allocated = Allocation.objects.filter(request=OuterRef("pk"), state=Allocation.State.ALLOCATED)
    return SpecimenRequest.objects.filter(status=SpecimenRequest.Status.APPROVED).filter(
        Exists(allocated)
    )


def returns_awaiting_review():
    """Returns without a review that released them or kept them on hold."""
    return SpecimenReturn.objects.exclude(
        Exists(ReturnReview.objects.filter(specimen_return=OuterRef("pk")))
    )


AREA = "Distribution"  # the dashboard row group (templates/core/dashboard.html)


def queues(user):
    """[{area, label, count, url, oldest_at, note, tone}] for the user's roles; [] without a
    read role."""
    if not has_role(user, *READ_ROLES):
        return []
    result = []
    if can_decide_requests(user) and has_role(user, *OPERATE_ROLES):
        awaiting = awaiting_decision(user)
        result.append(
            {
                "area": AREA,
                "label": "Requests awaiting my decision",
                "count": awaiting.count(),
                "url": _url("distribution:request-list", {"status": "submitted"}),
                "oldest_at": RequestSubmission.objects.filter(
                    request__in=awaiting.values("pk")
                ).aggregate(at=Min("submitted_at"))["at"],
                "note": "Submitted by other people; you cannot decide a request you created or "
                "submitted.",
                "tone": "progress",
            }
        )
    if has_role(user, *OPERATE_ROLES):
        picking = to_pick()
        result.append(
            {
                "area": AREA,
                "label": "Approved requests with allocations to pick",
                "count": picking.count(),
                "url": _url("distribution:request-list", {"status": "approved", "to_pick": "1"}),
                "oldest_at": Allocation.objects.filter(
                    request__in=picking.values("pk"), state=Allocation.State.ALLOCATED
                ).aggregate(at=Min("allocated_at"))["at"],
                "note": "",
                "tone": "info",
            }
        )
        packed = Shipment.objects.filter(state=Shipment.State.PACKED)
        result.append(
            {
                "area": AREA,
                "label": "Shipments packed, awaiting dispatch",
                "count": packed.count(),
                "url": _url("distribution:shipment-list", {"state": "packed"}),
                "oldest_at": packed.aggregate(at=Min("packed_at"))["at"],
                "note": "",
                "tone": "progress",
            }
        )
        dispatched = Shipment.objects.filter(state=Shipment.State.DISPATCHED)
        result.append(
            {
                "area": AREA,
                "label": "Dispatched shipments without a recorded receipt",
                "count": dispatched.count(),
                "url": _url("distribution:shipment-list", {"state": "dispatched"}),
                "oldest_at": dispatched.aggregate(at=Min("dispatched_at"))["at"],
                "note": "Record the recipient's confirmation when it arrives.",
                "tone": "info",
            }
        )
    if has_role(user, *MANAGE_ROLES):
        returns = returns_awaiting_review()
        result.append(
            {
                "area": AREA,
                "label": "Returns awaiting review",
                "count": returns.count(),
                "url": _url("distribution:return-list", {"review": "awaiting"}),
                "oldest_at": returns.aggregate(at=Min("occurred_at"))["at"],
                "note": "Returned specimens stay on a use hold until a manager reviews them.",
                "tone": "hold",
            }
        )
        attention = governance_lists.needing_attention()
        result.append(
            {
                "area": AREA,
                "label": "Withdrawals needing follow-up",
                "count": attention.count(),
                "url": _url("distribution:withdrawal-list", {"attention": "1"}),
                "oldest_at": attention.aggregate(at=Min("recorded_at"))["at"],
                "note": "Specimens in custody without a withdrawal hold, or distributed material "
                "without recorded follow-up.",
                "tone": "danger",
            }
        )
        horizon = governance_lists.within_horizon()
        result.append(
            {
                "area": AREA,
                "label": "Agreements expiring or with reporting due within "
                f"{governance_lists.HORIZON_DAYS} days",
                "count": horizon.count(),
                "url": _url("distribution:agreement-list", {"horizon": "1"}),
                "oldest_at": None,
                "note": "Dates as recorded on the agreements; nothing expires or is enforced "
                "automatically except validity at dispatch.",
                "tone": "warning",
            }
        )
    return result
