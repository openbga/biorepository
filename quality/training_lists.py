"""Full-queryset training search and due-date filters in repository local time."""

from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

COLUMNS = {
    "number": "pk",
    "trainee": "trainee__username",
    "topic": "topic",
    "status": "status",
    "due": "due_on",
    "completed": "completed_on",
    "renewal": "renewal_due",
    "updated": "updated_at",
}


def filter_training(rows, form, query):
    if not form.is_valid():
        return rows.none()
    if status := form.cleaned_data["status"]:
        rows = rows.filter(status=status)
    today = timezone.localdate()
    match form.cleaned_data["due"]:
        case "assignment_overdue":
            rows = rows.filter(status="assigned", due_on__lt=today)
        case "renewal_overdue":
            rows = rows.filter(status="completed", renewal_due__lt=today)
        case "next_30_days":
            until = today + timedelta(days=30)
            rows = rows.filter(
                Q(status="assigned", due_on__range=(today, until))
                | Q(status="completed", renewal_due__range=(today, until))
            )
    if query:
        predicate = (
            Q(topic__icontains=query)
            | Q(procedure_reference__icontains=query)
            | Q(trainee__username__icontains=query)
            | Q(trainee__first_name__icontains=query)
            | Q(trainee__last_name__icontains=query)
        )
        number = query.upper().removeprefix("TRN-")
        if number.isdecimal() and len(number) <= 15:
            predicate |= Q(pk=int(number))
        rows = rows.filter(predicate)
    return rows
