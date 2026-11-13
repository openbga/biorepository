"""Bootstrap: creates a usable repository once and never rewrites what exists."""

from io import StringIO

import pytest
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from accounts.models import User
from accounts.roles import ROLES, can_approve_disposal, has_role
from accounts.setup import setup_repository
from core.models import AuditEvent, Repository

pytestmark = pytest.mark.django_db
PASSWORD = "a-long-unique-passphrase-93"


def first_run(**extra):
    args = dict(
        repository_name="Example Biorepository",
        username="owner",
        email="owner@example.invalid",
        password=PASSWORD,
    )
    return setup_repository(**{**args, **extra})


def test_fresh_database_gets_repository_groups_and_a_non_approving_admin(settings):
    result = first_run()
    repository = Repository.objects.get()
    assert (repository.name, repository.timezone) == ("Example Biorepository", settings.TIME_ZONE)
    assert set(Group.objects.values_list("name", flat=True)) == set(ROLES)
    assert not any(group.permissions.exists() for group in Group.objects.all())
    owner = User.objects.get(username="owner")
    assert owner.check_password(PASSWORD) and owner.is_staff and owner.is_superuser
    assert has_role(owner, "admin") and has_role(owner, "technician")
    assert list(owner.groups.values_list("name", flat=True)) == ["admin"]
    assert not can_approve_disposal(owner)  # approval is never implied by admin or superuser
    assert not result.warnings
    events = AuditEvent.objects.order_by("occurred_at", "id")
    assert {e.actor for e in events} == {owner}
    assert [e.action for e in events][0] == "setup.account_created"
    assert "password" not in str(events[0].after)


def test_rerun_changes_nothing_and_never_resets_a_password():
    first_run()
    owner = User.objects.get(username="owner")
    Group.objects.get(name="manager").permissions.add(
        Permission.objects.get(codename="approve_disposal")
    )  # a repository decision, not removed by a rerun
    count = AuditEvent.objects.count()
    result = first_run(password="a-different-passphrase-55")
    assert not result.changed
    owner.refresh_from_db()
    assert owner.check_password(PASSWORD)
    assert Group.objects.get(name="manager").permissions.filter(codename="approve_disposal")
    assert AuditEvent.objects.count() == count


def test_existing_repository_is_not_renamed_without_the_explicit_flag():
    first_run()
    with pytest.raises(ValidationError, match="--update-repository"):
        first_run(repository_name="Another Repository")
    assert Repository.objects.get().name == "Example Biorepository"
    first_run(repository_name="Another Repository", update_repository=True)
    assert Repository.objects.get().name == "Another Repository"
    assert AuditEvent.objects.filter(action="setup.repository_updated").count() == 1


def test_existing_account_only_gains_explicitly_named_roles(make_user):
    existing = make_user("operator", "technician", is_superuser=True, is_staff=True)
    setup_repository(
        repository_name="Example Biorepository", username="operator", roles=["manager"]
    )
    existing.refresh_from_db()
    assert existing.check_password("test-password-729!")
    assert set(existing.groups.values_list("name", flat=True)) == {"technician", "manager"}
    event = AuditEvent.objects.get(action="setup.account_roles_added")
    assert event.before["groups"] == ["technician"] and event.actor == existing


def test_unknown_role_is_refused_and_nothing_is_written(make_user):
    make_user("operator", "technician", is_superuser=True, is_staff=True)
    with pytest.raises(ValidationError, match="Unknown role"):
        setup_repository(
            repository_name="Example Biorepository", username="operator", roles=["approver"]
        )
    assert not Repository.objects.exists()
    assert set(Group.objects.values_list("name", flat=True)) == {"technician"}


def test_changes_need_an_attributable_administrator_and_a_strong_password():
    with pytest.raises(ValidationError, match="admin-username"):
        setup_repository(repository_name="Example Biorepository")
    with pytest.raises(ValidationError):
        first_run(password="password")
    assert not Repository.objects.exists() and not User.objects.exists()


def test_timezone_is_the_deployment_setting_and_drift_is_reported_then_synced():
    first_run()
    with override_settings(TIME_ZONE="America/Chicago"):
        drifted = setup_repository(username="owner")
        assert "TIME_ZONE" in drifted.warnings[0]
        assert Repository.objects.get().timezone != "America/Chicago"
        setup_repository(username="owner", sync_timezone=True)
    assert Repository.objects.get().timezone == "America/Chicago"


def test_command_reads_password_from_environment_and_is_idempotent(monkeypatch):
    monkeypatch.setenv("SETUP_ADMIN_PASSWORD", PASSWORD)
    argv = [
        "--repository-name",
        "Example Biorepository",
        "--admin-username",
        "owner",
        "--admin-email",
        "o@example.invalid",
    ]
    out = StringIO()
    call_command("setup_repository", *argv, stdout=out)
    assert "Setup complete." in out.getvalue() and PASSWORD not in out.getvalue()
    out = StringIO()
    call_command("setup_repository", *argv, stdout=out)
    assert "Nothing to change" in out.getvalue()
    assert User.objects.count() == 1


def test_command_without_a_password_source_fails_cleanly(monkeypatch):
    monkeypatch.delenv("SETUP_ADMIN_PASSWORD", raising=False)
    with pytest.raises(CommandError, match="SETUP_ADMIN_PASSWORD"):
        call_command(
            "setup_repository",
            "--repository-name",
            "Example Biorepository",
            "--admin-username",
            "owner",
            "--admin-email",
            "o@example.invalid",
        )
    assert not Repository.objects.exists()


def test_disposal_approval_permission_exists_and_setup_grants_it_to_no_group():
    assert Permission.objects.filter(
        content_type__app_label="accounts", codename="approve_disposal"
    ).exists()
    first_run()
    assert not Permission.objects.filter(group__isnull=False, codename="approve_disposal").exists()
