"""A test-only form route for the browser suite: the project's URLs plus one dialog form whose
page header has an action (no real dialog form has one today). Used through settings.ROOT_URLCONF
in e2e/test_form_dialog.py."""

from django import forms
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.template import engines
from django.urls import path, reverse

from config.urls import urlpatterns as project_urlpatterns

PAGE = engines["django"].from_string(
    "{% extends form_surface_layout %}{% load ui %}"
    "{% block page_actions %}"
    '<a class="btn btn-outline-secondary" href="{{ other_url }}">Open the list</a>'
    "{% endblock %}"
    "{% block content %}"
    '{% form_page form=form cancel_url=other_url submit_label="Save" %}{% endform_page %}'
    "{% endblock %}"
)


class NoteForm(forms.Form):
    note = forms.CharField(label="Note", required=False)


@login_required
def form_with_header_action(request):
    other = reverse("announcement-list")
    context = {"page_title": "Form with a header action", "form": NoteForm(), "other_url": other}
    return HttpResponse(PAGE.render(context, request))


urlpatterns = [
    path("e2e/form-with-header-action/", form_with_header_action, name="e2e-header-action"),
    *project_urlpatterns,
]
