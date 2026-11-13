"""The account admin must not be a way around roles or the audit trail."""

import pytest
from django.contrib.auth.models import Group, Permission
from django.urls import reverse

from accounts.models import User
from accounts.roles import can_approve_disposal
from core.models import AuditEvent

pytestmark = pytest.mark.django_db
PASSWORD = "test-password-729!"


@pytest.fixture
def owner(make_user):
    return make_user("owner", "admin", is_staff=True, is_superuser=True)


def add_user(http, **fields):
    data = {
        "username": "newuser",
        "email": "newuser@example.invalid",
        "usable_password": "true",
        "password1": "an-unrelated-long-secret-77",
        "password2": "an-unrelated-long-secret-77",
    }
    return http.post(reverse("admin:accounts_user_add"), {**data, **fields})


def test_only_superusers_open_account_or_group_admin(make_user, client):
    staff = make_user("clerk", "admin", is_staff=True)
    permissions = Permission.objects.filter(content_type__app_label__in=["accounts", "auth"])
    staff.user_permissions.set(permissions)  # even explicit Django permissions are not enough
    client.force_login(staff)
    for name in ("admin:accounts_user_changelist", "admin:auth_group_changelist"):
        assert client.get(reverse(name)).status_code == 403
    assert client.post(reverse("admin:accounts_user_add"), {}).status_code == 403


def test_account_creation_is_audited_without_the_password(owner, client):
    client.force_login(owner)
    assert add_user(client).status_code == 302
    created = User.objects.get(username="newuser")
    assert not created.is_staff and not created.is_superuser and not created.groups.exists()
    event = AuditEvent.objects.get(action="account.created")
    assert event.actor == owner and event.object_id == str(created.pk)
    assert "password" not in event.after and event.reason == "Django admin"


def test_staff_creation_grants_roles_without_disposal_approval(owner, client):
    client.force_login(owner)
    group = Group.objects.get_or_create(name="manager")[0]
    assert add_user(client, groups=[group.pk]).status_code == 302
    created = User.objects.get(username="newuser")
    assert [g.name for g in created.groups.all()] == ["manager"]
    assert not can_approve_disposal(created)
    assert AuditEvent.objects.get(action="account.created").after["groups"] == ["manager"]


def test_group_permission_changes_are_audited_and_role_names_are_fixed(owner, client):
    client.force_login(owner)
    group = Group.objects.get_or_create(name="manager")[0]
    approval = Permission.objects.get(codename="approve_disposal")
    url = reverse("admin:auth_group_change", args=[group.pk])
    response = client.post(url, {"name": "renamed", "permissions": [approval.pk]})
    assert response.status_code == 302
    group.refresh_from_db()
    assert group.name == "manager"  # role names are read-only
    event = AuditEvent.objects.get(action="group.updated")
    assert event.before["permissions"] == []
    assert event.after["permissions"] == ["accounts.approve_disposal"]
    assert event.actor == owner


def test_password_change_is_audited_without_the_value(owner, make_user, client):
    client.force_login(owner)
    target = make_user("target", "technician")
    url = reverse("admin:auth_user_password_change", args=[target.pk])
    secret = "yet-another-long-secret-31"
    response = client.post(
        url, {"usable_password": "true", "password1": secret, "password2": secret}
    )
    assert response.status_code == 302
    event = AuditEvent.objects.get(action="account.password_changed")
    assert event.actor == owner and event.object_id == str(target.pk)
    assert secret not in str(event.__dict__)


def test_users_cannot_be_deleted_and_last_superuser_cannot_be_removed(owner, client):
    client.force_login(owner)
    url = reverse("admin:accounts_user_delete", args=[owner.pk])
    assert client.post(url, {"post": "yes"}).status_code == 403
    assert User.objects.filter(pk=owner.pk).exists()
    from accounts.admin import AccountChangeForm

    form = AccountChangeForm(
        {"username": "owner", "email": owner.email, "is_active": "", "is_superuser": "on"},
        instance=owner,
    )
    form.is_valid()
    assert any("at least one active superuser" in e for e in form.non_field_errors())


def test_user_list_shows_roles_and_disposal_approval(owner, make_user, client):
    approver = make_user("approver1", "manager")
    group = approver.groups.get()
    group.permissions.add(Permission.objects.get(codename="approve_disposal"))
    make_user("manager1", "manager", is_superuser=False)  # shares the group's approval
    direct = make_user("direct1", "viewer")
    direct.user_permissions.add(Permission.objects.get(codename="approve_disposal"))
    plain = make_user("plain1", "technician")
    client.force_login(owner)
    url = reverse("admin:accounts_user_changelist")
    rows = {u.username: u for u in client.get(url).context["cl"].result_list}
    admin_view = client.get(url).context["cl"].model_admin
    assert admin_view.can_approve(rows["approver1"]) is True
    assert admin_view.can_approve(rows["direct1"]) is True
    assert admin_view.can_approve(rows["plain1"]) is False
    assert admin_view.roles(rows["approver1"]) == "manager"
    approvers = {u.username for u in client.get(url, {"approval": "yes"}).context["cl"].result_list}
    assert approvers == {"approver1", "manager1", "direct1"}
    others = {u.username for u in client.get(url, {"approval": "no"}).context["cl"].result_list}
    assert "plain1" in others and not others & approvers
    assert plain.pk


def test_group_list_shows_which_groups_grant_approval(owner, client):
    group = Group.objects.get_or_create(name="manager")[0]
    Group.objects.get_or_create(name="viewer")
    group.permissions.add(Permission.objects.get(codename="approve_disposal"))
    client.force_login(owner)
    response = client.get(reverse("admin:auth_group_changelist"))
    view = response.context["cl"].model_admin
    groups = {g.name: g for g in response.context["cl"].result_list}
    assert view.grants_approval(groups["manager"]) is True
    assert view.grants_approval(groups["viewer"]) is False
