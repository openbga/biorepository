from unittest.mock import patch

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse

from accounts.setup import setup_repository
from core.models import AuditEvent, Repository
from core.retention import set_record_retention

pytestmark = pytest.mark.django_db


def owner(staff):
    user = staff("policy_owner", "admin")
    user.is_staff = user.is_superuser = True
    user.save()
    return user


def test_statement_is_optional_audited_and_clearing_preserves_prior_policy(staff):
    actor = owner(staff)
    repository = Repository.objects.create(name="Synthetic policy repository")
    assert repository.record_retention_statement == "" and repository.record_retention_revision == 1
    repository = set_record_retention(
        actor=actor,
        repository_id=repository.pk,
        expected_revision=1,
        statement="Follow the repository's approved record schedule and legal holds.",
        reason="Owner recorded the approved policy reference",
    )
    prior = repository.record_retention_statement
    setup_repository(
        repository_name="Synthetic renamed repository",
        update_repository=True,
        username=actor.username,
    )
    repository.refresh_from_db()
    assert (
        repository.record_retention_statement == prior and repository.record_retention_revision == 2
    )
    set_record_retention(
        actor=actor,
        repository_id=repository.pk,
        expected_revision=2,
        statement="",
        reason="Policy withdrawn for review",
    )
    repository.refresh_from_db()
    events = list(
        AuditEvent.objects.filter(action="repository.record_retention_changed").order_by(
            "occurred_at"
        )
    )
    assert repository.record_retention_statement == "" and repository.record_retention_revision == 3
    assert events[0].before == {"statement": "", "revision": 1}
    assert events[1].before["statement"] == prior and events[1].after["statement"] == ""
    assert all(event.actor == actor and event.reason for event in events)


def test_policy_role_revision_reason_and_audit_atomicity(staff):
    actor = owner(staff)
    repository = Repository.objects.create(name="Synthetic policy repository")
    values = dict(
        actor=actor,
        repository_id=repository.pk,
        expected_revision=1,
        statement="Repository-approved schedule",
        reason="Record policy",
    )
    wrong = staff("policy_manager", "manager")
    wrong.is_superuser = True
    wrong.save()
    with pytest.raises(PermissionDenied):
        set_record_retention(**(values | {"actor": wrong}))
    for change in ({"reason": ""}, {"expected_revision": 0}, {"statement": ""}):
        with pytest.raises(ValidationError):
            set_record_retention(**(values | change))
    with patch("core.retention.record_event", side_effect=RuntimeError("Audit unavailable")):
        with pytest.raises(RuntimeError):
            set_record_retention(**values)
    repository.refresh_from_db()
    assert repository.record_retention_revision == 1 and repository.record_retention_statement == ""
    assert not AuditEvent.objects.filter(action="repository.record_retention_changed").exists()


def test_staff_reads_statement_and_owner_admin_rejects_stale_changes(client, staff):
    actor = owner(staff)
    repository = Repository.objects.create(name="Synthetic policy repository")
    reader = staff("policy_reader", "viewer")
    client.force_login(reader)
    url = reverse("record-retention")
    page = client.get(url)
    assert page.status_code == 200 and b"Not recorded" in page.content
    assert not page.context["can_manage_policy"]
    client.logout()
    assert client.get(url).status_code == 302  # sign in first
    superuser_only = staff("policy_super", "manager")
    superuser_only.is_staff = superuser_only.is_superuser = True
    superuser_only.save()
    client.force_login(superuser_only)
    assert not client.get(url).context["can_manage_policy"]  # reads, but may not change it
    assert (
        client.get(reverse("admin:core_repository_change", args=[repository.pk])).status_code == 403
    )
    client.force_login(actor)
    edit = reverse("admin:core_repository_change", args=[repository.pk])
    payload = {
        "record_retention_statement": "Repository-approved electronic retention schedule.",
        "expected_revision": 1,
        "reason": "Approved by repository management",
        "_save": "Save",
    }
    assert client.post(edit, payload | {"name": "Unauthorized rename"}).status_code == 302
    page = client.post(edit, payload | {"record_retention_statement": "Stale draft"})
    assert page.status_code == 200 and b"retention statement changed" in page.content
    repository.refresh_from_db()
    assert repository.record_retention_statement == payload["record_retention_statement"]
    assert (
        repository.name == "Synthetic policy repository"
        and repository.record_retention_revision == 2
    )
    assert AuditEvent.objects.filter(action="repository.record_retention_changed").count() == 1
    client.force_login(reader)
    page = client.get(url)
    assert page.context["statement"] == payload["record_retention_statement"]
