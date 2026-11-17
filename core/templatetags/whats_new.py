"""The top bar's What's new link with its unread indicator.

  {% load whats_new %}{% whats_new_button %}

Shown to every signed-in account. The page carries the recent entries and the current seen
mark (core.changelog.indicator_data); static/js/whats-new.js compares them with what
this account has seen in this browser and shows the count, or, in a browser without a mark for
the account, stores the current one and shows none. Entries ship with the application: no request.
"""

from django import template
from django.urls import reverse

from core import changelog

register = template.Library()


@register.inclusion_tag("includes/whats_new_button.html", takes_context=True)
def whats_new_button(context):
    request = context.get("request")
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {"show": False}
    match = getattr(request, "resolver_match", None)
    return {
        "show": True,
        "url": reverse("whats-new"),
        "indicator": changelog.indicator_data(changelog.entries()),
        "current": match is not None and match.view_name == "whats-new",
    }
