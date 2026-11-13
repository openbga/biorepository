import pytest
from django.contrib.auth.models import AnonymousUser, Group
from django.core.management import call_command
from django.core.management.base import CommandError

from accounts.models import User
from accounts.roles import ROLES, can_approve_disposal, can_decide_requests, effective_roles
from core.announcements import announcements_for, withdraw_announcement
from core.models import Announcement, AuditEvent, Repository

SHARED = "synthetic-test-password-only"
ADMIN_ONLY = "separate-admin-password-only"


@pytest.mark.django_db
@pytest.mark.parametrize("existing_manager_group", [False, True])
def test_demo_seed_creates_role_accounts_and_preserves_later_changes(
    monkeypatch, existing_manager_group
):
    if existing_manager_group:
        Group.objects.create(name="manager")
    monkeypatch.setenv("DEMO_PASSWORD", SHARED)
    monkeypatch.delenv("DEMO_ADMIN_PASSWORD", raising=False)
    call_command("seed_demo", verbosity=0)
    assert list(Repository.objects.values_list("name", flat=True)) == ["Example Biorepository"]
    assert set(User.objects.values_list("username", flat=True)) == {
        f"demo_{role}" for role in ROLES
    }
    for role in ROLES:
        assert role in effective_roles(User.objects.get(username=f"demo_{role}"))
    assert not Group.objects.filter(name="manager").get().permissions.exists()
    user = User.objects.get(username="demo_manager")
    password_hash = user.password
    assert can_approve_disposal(user)
    assert user.user_permissions.filter(codename="approve_disposal").exists()
    # approval is explicit: not the admin role, no other demo account, and no group carries it
    assert all(not can_approve_disposal(other) for other in User.objects.exclude(pk=user.pk))
    assert not any(group.permissions.exists() for group in Group.objects.all())
    # request decisions are explicit in the same way: the manager, nobody else
    assert can_decide_requests(user)
    assert all(not can_decide_requests(other) for other in User.objects.exclude(pk=user.pk))

    user.user_permissions.clear()
    monkeypatch.setenv("DEMO_PASSWORD", "different-synthetic-test-password")
    call_command("seed_demo", verbosity=0)
    user.refresh_from_db()
    assert user.password == password_hash
    assert not can_approve_disposal(user)  # a later decision is not undone by a rerun
    assert not can_decide_requests(user)
    assert User.objects.count() == len(ROLES) and Repository.objects.count() == 1


@pytest.mark.django_db
def test_seed_refuses_a_non_demo_repository(monkeypatch):
    monkeypatch.setenv("DEMO_PASSWORD", SHARED)
    Repository.objects.create(pk=1, name="Real Repository Name")
    with pytest.raises(CommandError, match="non-demo"):
        call_command("seed_demo", verbosity=0)
    assert not User.objects.exists()


@pytest.mark.django_db
def test_fresh_seed_announces_the_demo_accounts_without_the_admin(monkeypatch):
    """The nightly-reset path: an empty database, one command, the notice is back."""
    monkeypatch.setenv("DEMO_PASSWORD", SHARED)
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", ADMIN_ONLY)
    monkeypatch.setenv("DEMO_RESET_NOTE", "Erased every day at 08:00 UTC.")
    call_command("seed_demo", announce_accounts=True, verbosity=0)
    admin = User.objects.get(username="demo_admin")
    assert admin.check_password(ADMIN_ONLY) and not admin.check_password(SHARED)
    notice = Announcement.objects.get()
    assert [a.pk for a in announcements_for(AnonymousUser())] == [notice.pk]
    assert (notice.show_on_sign_in, notice.show_to_staff) == (True, False)
    assert f"Password for each: {SHARED}" in notice.body
    assert notice.body.endswith("Anyone can use these accounts. Erased every day at 08:00 UTC.")
    assert "demo_admin" not in notice.body and ADMIN_ONLY not in notice.body
    listed = {line.split(":")[0] for line in notice.body.splitlines() if line.startswith("demo_")}
    assert listed == {"demo_manager", "demo_technician", "demo_viewer"}
    assert all(User.objects.get(username=name).check_password(SHARED) for name in listed)
    assert AuditEvent.objects.get(action="announcement.posted").actor == admin
    # Published accounts cannot change their own credentials; the administrator's can.
    assert set(User.objects.filter(credentials_locked=True).values_list("username", flat=True)) == (
        listed
    )

    # A rerun leaves the administrator's later decision alone.
    withdraw_announcement(actor=admin, pk=notice.pk)
    call_command("seed_demo", announce_accounts=True, verbosity=0)
    assert Announcement.objects.get().withdrawn_at


@pytest.mark.django_db
def test_seed_never_publishes_a_password_that_opens_the_admin_account(monkeypatch):
    monkeypatch.setenv("DEMO_PASSWORD", SHARED)
    monkeypatch.delenv("DEMO_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("DEMO_RESET_NOTE", raising=False)
    call_command("seed_demo", verbosity=0)
    assert not Announcement.objects.exists()  # publishing is opt-in
    with pytest.raises(CommandError, match="also opens demo_admin"):
        call_command("seed_demo", announce_accounts=True, verbosity=0)
    assert not Announcement.objects.exists()

    admin = User.objects.get(username="demo_admin")
    admin.set_password(ADMIN_ONLY)
    admin.save()
    technician = User.objects.get(username="demo_technician")
    technician.set_password("changed-by-someone-else")
    technician.save()
    call_command("seed_demo", announce_accounts=True, verbosity=0)
    body = Announcement.objects.get().body
    assert "demo_manager" in body and "demo_viewer" in body and SHARED in body
    assert "demo_admin" not in body and "demo_technician" not in body  # only accounts it opens
    assert body.endswith("The demonstration can be reset at any time.")

    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", "too-short")
    with pytest.raises(CommandError, match="DEMO_ADMIN_PASSWORD"):
        call_command("seed_demo", verbosity=0)
