"""The specimen record's Distribution rows (distribution.record_rows via specimens.record_links)."""

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from accounts.models import User
from distribution.record_rows import specimen_rows
from specimens.record_links import DISTRIBUTION_SOURCES

from .conftest import allocate, approved_request, stored

pytestmark = pytest.mark.django_db


def test_the_record_lists_its_requests_and_consents_with_their_status(client, people, world, dist):
    assert specimen_rows in DISTRIBUTION_SOURCES  # registered by DistributionConfig.ready
    request = approved_request(people, dist, world)
    specimen = stored(people["technician"], world, 1, start=9)[0]
    allocate(people, request, [specimen])
    rows = specimen_rows(people["viewer"], specimen)
    labels = [(row["label"], row["status_label"]) for row in rows]
    assert (request.number, "Approved") in labels
    assert (f"{world.participant.code} · {dist.study.code}", "Consented") in labels
    client.force_login(people["viewer"])
    page = client.get(reverse("specimens:specimen-detail", args=[specimen.pk])).content.decode()
    assert f'href="{reverse("distribution:request-detail", args=[request.pk])}">' in page
    assert request.number in page and f"{world.participant.code} · {dist.study.code}" in page


def test_a_user_without_distribution_read_access_gets_no_rows(people, world, dist):
    specimen = stored(people["technician"], world, 1, start=9)[0]
    nobody = User.objects.create_user("rows-nobody", "rows@example.invalid", "test-pass-729!")
    assert specimen_rows(nobody, specimen) == []
    gone = User.objects.create_user("rows-gone", "gone@example.invalid", "test-pass-729!")
    gone.groups.add(Group.objects.get_or_create(name="viewer")[0])
    gone.is_active = False
    gone.save()
    assert specimen_rows(gone, specimen) == []
    assert specimen_rows(people["viewer"], specimen)  # the consent row, for a reader
