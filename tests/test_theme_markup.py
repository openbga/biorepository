"""Server-rendered parts of the visual language (docs/ui-standards.md section 12).

The dateline and the record foot line print dates in the repository's time zone (TIME_ZONE), never
the browser's; the record foot line also marks a record page (numbered panels); history strikes
through the prior value of a change.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth.models import Group
from django.template.loader import render_to_string
from django.test import override_settings
from django.urls import reverse

# Fourteen hours ahead of and eleven behind UTC: the local dates always differ.
FAR_EAST, FAR_WEST = "Pacific/Kiritimati", "Pacific/Pago_Pago"


def today(zone):
    return datetime.now(ZoneInfo(zone)).strftime("%Y-%m-%d")


@pytest.mark.parametrize("zone", [FAR_EAST, FAR_WEST])
def test_dateline_and_record_foot_use_the_repository_time_zone(zone, django_user_model):
    viewer = django_user_model(username="theme-viewer")
    with override_settings(TIME_ZONE=zone):
        dateline = render_to_string("includes/dateline.html")
        foot = render_to_string(
            "includes/record_foot.html", {"identifier": "VP-000001", "user": viewer}
        )
    assert f'<time datetime="{today(zone)}">' in dateline
    assert "Week " in dateline
    assert 'class="vr-record-foot"' in foot
    assert "VP-000001" in foot
    assert f'Viewed <time datetime="{today(zone)}T' in foot
    assert "by theme-viewer" in foot


def test_history_strikes_through_the_prior_value():
    html = render_to_string(
        "includes/history_item.html",
        {
            "title": "Corrected",
            "changes": [("Container", "Cryovial 2 mL", "Cryovial 5 mL"), ("Note", "", "x")],
        },
    )
    assert '<del class="vr-prior">Cryovial 2 mL</del>' in html
    assert '<del class="vr-prior">blank</del>' in html
    assert "changed to</span> Cryovial 5 mL" in html


@pytest.mark.django_db
def test_dashboard_has_dateline_and_no_record_foot(client, django_user_model, settings):
    # The dashboard carries no record foot line (record pages have their own).
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
    technician = django_user_model.objects.create_user(
        "theme-technician", "theme-technician@example.invalid", "test-password-729!"
    )
    technician.groups.add(Group.objects.get_or_create(name="technician")[0])
    client.force_login(technician)

    dashboard = client.get(reverse("dashboard")).content.decode()
    assert 'class="vr-dateline"' in dashboard
    assert 'class="vr-record-foot"' not in dashboard
