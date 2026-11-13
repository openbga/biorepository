import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.test import RequestFactory
from django.urls import reverse

from accounts.models import User
from accounts.roles import (
    can_approve_disposal,
    effective_roles,
    has_role,
    require_disposal_authority,
    staff_role_required,
)


@pytest.mark.django_db
def test_anonymous_redirect_and_registration_closed(client):
    assert client.get(reverse("dashboard")).status_code == 302
    response = client.post(
        reverse("account_signup"),
        {
            "username": "outsider",
            "email": "outside@example.invalid",
            "password1": "a-long-test-password",
            "password2": "a-long-test-password",
        },
    )
    assert response.status_code in (200, 302, 403)
    assert not User.objects.filter(username="outsider").exists()


@pytest.mark.django_db
def test_real_login_and_logout_require_post(client, staff):
    user = staff("technician", "technician")
    response = client.post(
        reverse("account_login"), {"login": user.username, "password": "test-password-729!"}
    )
    assert response.status_code == 302
    assert str(client.session["_auth_user_id"]) == str(user.pk)
    client.get(reverse("account_logout"))
    assert "_auth_user_id" in client.session
    client.post(reverse("account_logout"))
    assert "_auth_user_id" not in client.session


@pytest.mark.django_db
def test_admin_has_operational_roles_without_membership_or_approval_grants(client, staff):
    admin = staff("admin", "admin")
    assert not admin.is_staff and not admin.is_superuser
    assert effective_roles(admin) == {"admin", "manager", "technician", "viewer"}
    assert not has_role(admin, "unrecognized_role")
    assert not can_approve_disposal(admin)
    client.force_login(admin)
    page = client.get(reverse("dashboard"))
    assert page.status_code == 200
    assert set(admin.groups.values_list("name", flat=True)) == {"admin"}
    admin.groups.clear()
    assert not effective_roles(admin)  # role revocation is not hidden by a persistent cache


@pytest.mark.django_db
@pytest.mark.parametrize("kind", ["anonymous", "inactive_admin", "superuser", "viewer"])
def test_admin_access_does_not_escape_the_active_role_boundary(staff, kind):
    if kind == "anonymous":
        user = AnonymousUser()
    elif kind == "superuser":
        user = User.objects.create_superuser("root", "root@example.invalid", "test-password")
    else:
        user = staff(kind, "viewer" if kind == "viewer" else "admin")
        if kind == "inactive_admin":
            user.is_active = False
            user.save()
    assert not has_role(user, "manager")
    assert not can_approve_disposal(user)
    with pytest.raises(PermissionDenied):
        require_disposal_authority(user)


@pytest.mark.django_db
def test_csrf_required_for_mutations(staff):
    from django.test import Client as Browser

    browser = Browser(enforce_csrf_checks=True)
    browser.force_login(staff("admin", "admin"))
    assert browser.post(reverse("announcement-create"), {}).status_code == 403


@pytest.mark.django_db
def test_disposal_approval_is_an_explicit_per_user_permission(staff):
    from django.contrib.auth.models import Permission

    manager = staff("manager", "manager")
    permission = Permission.objects.get(
        content_type__app_label="accounts", codename="approve_disposal"
    )
    assert not can_approve_disposal(manager)
    manager.user_permissions.add(permission)
    assert can_approve_disposal(manager)
    assert has_role(manager, "manager")
    manager.is_active = False
    assert not can_approve_disposal(manager)
    manager.is_active = True
    manager.user_permissions.remove(permission)
    assert not can_approve_disposal(manager)
    require_disposal_authority_fails = pytest.raises(PermissionDenied)
    with require_disposal_authority_fails:
        require_disposal_authority(manager)
    admin = staff("admin", "admin")
    admin.is_superuser = True
    admin.save()
    assert not can_approve_disposal(admin)
    assert not AnonymousUser().is_authenticated and not can_approve_disposal(AnonymousUser())


@pytest.mark.django_db
def test_staff_role_required_decorator(staff):
    @staff_role_required("manager")
    def view(request):
        return HttpResponse("ok")

    def call(user):
        request = RequestFactory().get("/probe/")
        request.user = user
        return view(request)

    anonymous = call(AnonymousUser())
    assert anonymous.status_code == 302 and "login" in anonymous.url
    with pytest.raises(PermissionDenied):
        call(staff("probe_viewer", "viewer"))
    assert call(staff("probe_manager", "manager")).status_code == 200
    assert call(staff("probe_admin", "admin")).status_code == 200  # admin implies manager
