from django.contrib.auth.decorators import login_required
from django.db import connection
from django.http import JsonResponse
from django.shortcuts import render
from django.urls import NoReverseMatch, reverse

from accounts.roles import has_role, require_staff

from . import changelog
from .models import AuditEvent, Repository
from .templatetags.navigation import ITEMS, ROUTE_ITEMS

# The specimen journey on the dashboard: (key, label, icon, unit, queue keys). Each stage lists the
# dashboard's own queue objects, so its numbers are the queues' and open the same rows.
JOURNEY = (
    ("receive", "Receive", "box-arrow-in-down", "not stored", ("unstored",)),
    ("store", "Store", "snow", "in storage", ("held",)),
    ("out", "Check out / in", "stopwatch", "out of storage", ("checked_out", "returns_review")),
    (
        "distribute",
        "Distribute",
        "send",
        "in progress",
        ("request_decide", "request_pick", "shipment_packed"),
    ),
    ("dispose", "Dispose", "trash3", "in progress", ("disposal_awaiting", "disposal_approved")),
)


def journey(queues, stored):
    """[{key, label, icon, count, unit, url, queues}] built from the dashboard queues.

    Receive and Check out / in count their first queue (registered, not stored; checked out) and
    link to it. Store counts the specimens in storage (`stored`, from the specimen list's In
    storage filter it links to). Distribute and Dispose count the sum of their queues and link to
    the first one. A stage whose role sees none of its queues has no link.
    """
    by_key = {queue["key"]: queue for queue in queues if queue.get("key")}
    stages = []
    for key, label, icon, unit, queue_keys in JOURNEY:
        shown = [by_key[k] for k in queue_keys if k in by_key]
        if key == "store":
            count, url = (stored["count"], stored["url"]) if stored else (0, None)
        elif key in ("receive", "out"):
            first = by_key.get(queue_keys[0])
            count, url = (first["count"], first["url"]) if first else (0, None)
        else:
            count = sum(queue["count"] for queue in shown)
            url = shown[0]["url"] if shown else None
        stages.append(
            {
                "key": key,
                "label": label,
                "icon": icon,
                "count": count,
                "unit": unit,
                "url": url,
                "queues": shown,
            }
        )
    return stages


@login_required
def dashboard(request):
    """Staff start page: the specimen journey, the user's work queues and the most recently
    stored specimens."""
    require_staff(request.user)
    from distribution.dashboard import queues as distribution_queues
    from equipment.dashboard import queues as equipment_queues
    from specimens.dashboard import in_storage, queues, recently_stored

    all_queues = (
        queues(request.user) + equipment_queues(request.user) + distribution_queues(request.user)
    )
    return render(
        request,
        "core/dashboard.html",
        {
            "page_title": "Dashboard",
            "queues": all_queues,
            "journey": journey(all_queues, in_storage(request.user)),
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
