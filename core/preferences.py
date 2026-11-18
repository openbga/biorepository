"""Per-user presentation preferences, stored on the server (repository workstations are shared).

Keys and the values they accept:
  grid:<grid>:v<version>  {"order": [column id, …], "hidden": [column id, …]}, static/js/grid.js
  page:<view name>        {"per_page": 25 | 50 | 100, "sort": "<-column>"}, the middleware below

Every value is checked when written and again when read: a value that no longer fits (an old
format, a column that was removed) is ignored and the page falls back to its defaults.
Presentation only: a preference never widens what a user may see.
"""

import json
import re

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseBadRequest
from django.views.decorators.http import require_POST

from core.grids import ACCEPTED, PAGE_SIZES
from core.models import UserPreference

MAX_PREFERENCES = 300  # per user: grids and pages are a bounded set
MAX_BODY = 8_000  # bytes of one request
GRID_KEY = re.compile(r"^grid:[a-z0-9_.-]{1,80}:v\d{1,3}$")
PAGE_KEY = re.compile(r"^page:[a-z0-9_:-]{1,100}$")
COLUMN_ID = re.compile(r"^[a-z0-9_-]{1,40}$")
SORT = re.compile(r"^-?[a-z0-9_]{1,40}$")
CLIENT_KEYS = re.compile(r"^grid:")  # what the page hands to its scripts


def _columns(value):
    return (
        isinstance(value, list)
        and len(value) <= 60
        and all(isinstance(item, str) and COLUMN_ID.match(item) for item in value)
    )


def clean(key, value):
    """The value if it is valid for the key, else None."""
    if GRID_KEY.match(key):
        if isinstance(value, dict) and set(value) == {"order", "hidden"}:
            if _columns(value["order"]) and _columns(value["hidden"]):
                return value
        return None
    if PAGE_KEY.match(key):
        if not isinstance(value, dict) or not set(value) <= {"per_page", "sort"}:
            return None
        cleaned = {}
        if value.get("per_page") in PAGE_SIZES and not isinstance(value["per_page"], bool):
            cleaned["per_page"] = value["per_page"]
        if isinstance(value.get("sort"), str) and SORT.match(value["sort"]):
            cleaned["sort"] = value["sort"]
        return cleaned or None
    return None


SESSION_KEY = "vr_preferences"


def for_request(request):
    """All valid preferences of the signed-in user.

    Read from the database once per sign-in and kept in the session, which every request loads
    anyway, so pages make no extra query. save() updates both. Another session of the same user
    sees a change made elsewhere from its next sign-in.
    """
    if hasattr(request, "_vr_preferences"):
        return request._vr_preferences
    prefs = {}
    user = getattr(request, "user", None)
    session = getattr(request, "session", None)
    if user is not None and user.is_authenticated:
        cached = session.get(SESSION_KEY) if session is not None else None
        if isinstance(cached, dict) and cached.get("user") == user.pk:
            stored = cached.get("prefs", {}).items()
        else:
            stored = UserPreference.objects.filter(user=user).values_list("key", "value")
        for key, value in stored:
            if (valid := clean(key, value)) is not None:
                prefs[key] = valid
        if session is not None and cached != {"user": user.pk, "prefs": prefs}:
            session[SESSION_KEY] = {"user": user.pk, "prefs": prefs}
    request._vr_preferences = prefs
    return prefs


def _remember(request, prefs):
    if getattr(request, "session", None) is not None:
        request.session[SESSION_KEY] = {"user": request.user.pk, "prefs": dict(prefs)}


def save(request, key, value):
    """Store (or, for None, remove) one preference of the signed-in user. Raises ValueError."""
    prefs = for_request(request)
    if value is None:
        UserPreference.objects.filter(user=request.user, key=key).delete()
        prefs.pop(key, None)
        _remember(request, prefs)
        return
    if clean(key, value) != value:
        raise ValueError(f"Not a valid value for {key!r}.")
    if prefs.get(key) == value:
        return
    exists = UserPreference.objects.filter(user=request.user, key=key).exists()
    if not exists and UserPreference.objects.filter(user=request.user).count() >= MAX_PREFERENCES:
        raise ValueError("Too many saved preferences.")
    UserPreference.objects.update_or_create(user=request.user, key=key, defaults={"value": value})
    prefs[key] = value
    _remember(request, prefs)


@login_required
@require_POST
def preference(request):
    """POST {"key": …, "value": …} (value null removes it): the signed-in user's own setting."""
    if len(request.body) > MAX_BODY:
        return HttpResponseBadRequest("Too large.")
    try:
        data = json.loads(request.body)
        key, value = data["key"], data["value"]
        if not isinstance(key, str) or not GRID_KEY.match(key):
            raise ValueError("Unknown preference.")
        save(request, key, value)
    except (ValueError, KeyError, TypeError) as error:
        return HttpResponseBadRequest(str(error))
    return HttpResponse(status=204)


class GridPreferenceMiddleware:
    """Remember each user's page size and sort per list, and apply them when the URL has none.

    Lists follow the shared grid contract (core.grids): `per_page` and `sort` in the query string.
    A value is saved for this page only when the view accepted it (core.grids.GridRequest.parse
    reports what it applied) and responded 200, never straight from the query string; a list
    without GridRequest saves nothing. Without a value in the URL, the saved one is added to the
    request, so the view, its pagination and its links all see it. The view still validates both.
    """

    PARAMS = ("per_page", "sort")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        accepted = {}
        token = ACCEPTED.set(accepted)
        try:
            response = self.get_response(request)
        finally:
            ACCEPTED.reset(token)
        key = getattr(request, "_vr_page_key", None)
        if key is not None and response.status_code == 200 and accepted:
            self._remember(request, key, accepted)
        return response

    @staticmethod
    def _remember(request, key, accepted):
        stored = for_request(request).get(key, {})
        value = dict(stored)
        for name, applied in accepted.items():
            if applied is not None and clean(key, {name: applied}):
                value[name] = applied
            elif name in getattr(request, "vr_saved_params", ()):
                value.pop(name)  # a saved value the view no longer accepts (a removed column)
        if value != stored:
            try:
                save(request, key, value or None)
            except ValueError:
                pass  # over the per-user limit, or not storable: the request still applied it

    def process_view(self, request, view_func, view_args, view_kwargs):
        user = getattr(request, "user", None)
        match = request.resolver_match
        if request.method != "GET" or user is None or not user.is_authenticated or match is None:
            return None
        key = f"page:{match.view_name}"
        if not PAGE_KEY.match(key):
            return None
        request._vr_page_key = key
        stored = for_request(request).get(key, {})
        missing = {
            name: stored[name] for name in self.PARAMS if name in stored and name not in request.GET
        }
        if missing:
            params = request.GET.copy()
            for name, value in missing.items():
                params[name] = str(value)
            request.GET = params
            request.vr_saved_params = set(missing)  # links may leave these out (core.grids)
        return None
