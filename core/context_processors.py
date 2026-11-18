from django.contrib.auth.models import AnonymousUser

from accounts.roles import effective_roles

from .form_surfaces import form_route_patterns, is_dialog_request, surface_for_request
from .models import Repository
from .page_docs import documentation_for_request


def form_presentation(request):
    surface = surface_for_request(request)
    form_dialog = bool(surface and is_dialog_request(request))
    return {
        "form_surface": surface,
        "form_route_patterns": form_route_patterns(),
        # A fetched dialog: components that never show in a dialog (history_panel) read it.
        "form_dialog": form_dialog,
        "form_surface_layout": (
            "layouts/form_fragment.html" if form_dialog else "layouts/document.html"
        ),
    }


def workspace(request):
    roles = effective_roles(getattr(request, "user", AnonymousUser()))
    return {
        "repository": Repository.objects.first(),
        "user_roles": roles,
        "page_documentation": documentation_for_request(request),
    }
