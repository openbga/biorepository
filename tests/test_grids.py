"""List filter chips (core.grids.active_filters)."""

from django import forms
from django.test import RequestFactory

from core.grids import active_filters


class Filters(forms.Form):
    mine = forms.BooleanField(label="Requested by me", required=False)
    status = forms.ChoiceField(choices=[("", "Any"), ("open", "Open")], required=False)


def chips(query):
    request = RequestFactory().get("/list/", query)
    form = Filters(request.GET)
    assert form.is_valid()
    return [(chip["label"], chip["value"]) for chip in active_filters(request, form)]


def test_an_unchecked_box_shows_no_chip():
    assert chips({"status": "open"}) == [("Status", "Open")]
    assert chips({"mine": "on"}) == [("Requested by me", "True")]
    assert chips({}) == []
