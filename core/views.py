from django.contrib.auth.decorators import login_required
from django.db import connection
from django.http import JsonResponse
from django.shortcuts import render
from django.urls import NoReverseMatch, reverse

from accounts.roles import has_role, require_staff

from . import changelog
from .models import AuditEvent, Repository
from .templatetags.navigation import ITEMS, ROUTE_ITEMS


@login_required
def dashboard(request):
    """Staff start page: the user's work queues and the most recently stored specimens."""
    require_staff(request.user)
    from distribution.dashboard import queues as distribution_queues
    from equipment.dashboard import queues as equipment_queues
    from specimens.dashboard import queues, recently_stored

    return render(
        request,
        "core/dashboard.html",
        {
            "page_title": "Dashboard",
            "queues": queues(request.user)
            + equipment_queues(request.user)
            + distribution_queues(request.user),
            "recently_stored": recently_stored(request.user),
        },
    )


def health(request):
    return JsonResponse({"status": "ok"})


def ready(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ready"})


@login_required
def record_retention(request):
    require_staff(request.user)
    repository = Repository.objects.first()
    return render(
        request,
        "core/record_retention.html",
        {
            "page_title": "Record retention",
            "repository": repository,
            "statement": repository.record_retention_statement if repository else "",
            "can_manage_policy": request.user.is_staff
            and request.user.is_superuser
            and has_role(request.user, "admin"),
            "policy_events": AuditEvent.objects.filter(
                object_type="core.repository",
                object_id="1",
                action="repository.record_retention_changed",
            )
            .select_related("actor")
            .order_by("-occurred_at")[:20],
        },
    )


def csrf_failure(request, reason=""):
    """Use the normal error shell without echoing CSRF diagnostics or request values."""
    return render(request, "403.html", {"csrf_failure": True}, status=403)


def _entry_link(entry):
    """The menu destination an entry points to; the page still checks the user's access."""
    key = ROUTE_ITEMS.get(entry.link)
    if not entry.link or key is None:
        return None
    try:
        return {"url": reverse(entry.link), "label": ITEMS[key].label}
    except NoReverseMatch:
        return None


@login_required
def whats_new(request):
    """Every change in changelog.d/, grouped like CHANGELOG.md (core.changelog), for any signed-in
    account, with or without a role. static/js/whats-new.js remembers in this browser which entries
    the account has seen, marks the ones that were new and clears the top bar's indicator; a
    browser without a mark for the account has nothing new."""
    entries = changelog.entries()
    groups = [
        {
            "key": group.start.strftime("%Y-%m"),
            "label": group.label,
            "start": group.start,
            "end": group.end,
            "count": group.count,
            "sections": [
                {
                    "impact": impact,
                    "label": label,
                    "entries": [{"entry": entry, "link": _entry_link(entry)} for entry in items],
                }
                for impact, label, items in group.sections
            ],
        }
        for group in changelog.group_entries(entries)
    ]
    return render(
        request,
        "core/whats_new.html",
        {
            "page_title": "What's new",
            "groups": groups,
            "total": len(entries),
        },
    )
