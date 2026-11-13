"""Account administration: the supported way to manage staff accounts.

Boundaries enforced here, because the Django admin is otherwise a path around the domain
rules:
- Only superusers (the deployment owner) can open user or group admin. A superuser has no
  repository role and no disposal approval authority by that status alone (see accounts.roles).
- Users are deactivated, never deleted; the last active superuser cannot be removed.
- The built-in role groups keep their names. Every account and group change, and every
  password change, writes an attributed audit event; password values are never recorded.
- The identity library's own admin pages are replaced. Authenticator secrets and recovery
  codes are never shown or filterable; a superuser may only remove a lost authenticator, which
  is audited.
  Authentication email addresses are read only here.
"""

from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from django import forms
from django.contrib import admin
from django.contrib.admin.utils import unquote
from django.contrib.auth.admin import GroupAdmin, UserAdmin
from django.contrib.auth.forms import AdminUserCreationForm, UserChangeForm
from django.contrib.auth.models import Group, Permission
from django.db import transaction
from django.db.models import Count, Prefetch, Q
from django.db.models.constants import LOOKUP_SEP

from core.audit import record_event

from .models import User
from .roles import ROLES
from .setup import account_snapshot, group_snapshot


class SuperuserOnly:
    """Account and group administration is deployment-owner work."""

    def _allowed(self, request):
        return request.user.is_active and request.user.is_superuser

    def has_module_permission(self, request):
        return self._allowed(request)

    def has_view_permission(self, request, obj=None):
        return self._allowed(request)

    def has_add_permission(self, request):
        return self._allowed(request)

    def has_change_permission(self, request, obj=None):
        return self._allowed(request)

    def has_delete_permission(self, request, obj=None):
        return False


APPROVAL = {"codename": "approve_disposal", "content_type__app_label": "accounts"}


REQUEST_DECISIONS = {"codename": "approve_distribution", "content_type__app_label": "accounts"}


def holds_approval(user, codename="approve_disposal"):
    """Disposal approval (or another explicit grant) from prefetched permissions; same rule as
    accounts.roles."""
    if not user.is_active:
        return False
    direct = (p.codename for p in user.user_permissions.all())
    grouped = (p.codename for g in user.groups.all() for p in g.permissions.all())
    return codename in {*direct, *grouped}


def approval_holders(permission=APPROVAL):
    return User.objects.filter(is_active=True).filter(
        Q(user_permissions__in=Permission.objects.filter(**permission))
        | Q(groups__permissions__in=Permission.objects.filter(**permission))
    )


class ApprovalFilter(admin.SimpleListFilter):
    title = "disposal approval"
    parameter_name = "approval"

    def lookups(self, request, model_admin):
        return [("yes", "Can approve disposal"), ("no", "Cannot approve disposal")]

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(pk__in=approval_holders().values("pk"))
        if self.value() == "no":
            return queryset.exclude(pk__in=approval_holders().values("pk"))
        return queryset


class RequestDecisionFilter(admin.SimpleListFilter):
    title = "request decisions"
    parameter_name = "request_decisions"

    def lookups(self, request, model_admin):
        return [("yes", "Can decide requests"), ("no", "Cannot decide requests")]

    def queryset(self, request, queryset):
        holders = approval_holders(REQUEST_DECISIONS).values("pk")
        if self.value() == "yes":
            return queryset.filter(pk__in=holders)
        if self.value() == "no":
            return queryset.exclude(pk__in=holders)
        return queryset


class AccessRules:
    """Shared validation for the add and change forms."""

    def clean(self):
        data = super().clean()
        if self.instance.pk and self.instance.is_superuser and self.instance.is_active:
            leaving = not data.get("is_superuser", True) or not data.get("is_active", True)
            others = User.objects.filter(is_superuser=True, is_active=True).exclude(
                pk=self.instance.pk
            )
            if leaving and not others.exists():
                raise forms.ValidationError("Keep at least one active superuser.")
        return data


class AccountChangeForm(AccessRules, UserChangeForm):
    pass


class AccountCreationForm(AccessRules, AdminUserCreationForm):
    pass


@admin.register(User)
class AccountAdmin(SuperuserOnly, UserAdmin):
    form = AccountChangeForm
    add_form = AccountCreationForm
    list_display = (
        "username",
        "full_name",
        "email",
        "roles",
        "can_approve",
        "can_decide_requests",
        "is_active",
        "is_superuser",
        "last_login",
    )
    list_filter = (
        ApprovalFilter,
        RequestDecisionFilter,
        "is_active",
        "is_superuser",
        "groups",
    )
    ordering = ("username",)

    def get_queryset(self, request):
        permissions = Permission.objects.select_related("content_type")
        return (
            super()
            .get_queryset(request)
            .prefetch_related(
                "groups",
                Prefetch("user_permissions", queryset=permissions),
                Prefetch("groups__permissions", queryset=permissions),
            )
        )

    @admin.display(description="Name", ordering="last_name")
    def full_name(self, user):
        return user.get_full_name() or "—"

    @admin.display(description="Roles")
    def roles(self, user):
        return ", ".join(sorted(g.name for g in user.groups.all())) or "None"

    @admin.display(description="Can approve disposal", boolean=True)
    def can_approve(self, user):
        return holds_approval(user)

    @admin.display(description="Can decide requests", boolean=True)
    def can_decide_requests(self, user):
        return holds_approval(user, "approve_distribution")

    actions = None
    fieldsets = (
        (None, {"fields": ("username", "password")}),
        ("Personal info", {"fields": ("first_name", "last_name", "email")}),
        (
            "Repository roles",
            {
                "fields": ("groups",),
                "description": "Staff roles are the groups: admin, manager, technician, viewer. "
                "No role group carries disposal approval or request decisions; grant them "
                "below.",
            },
        ),
        (
            "Account status and technical access",
            {
                "fields": ("is_active", "is_staff", "is_superuser", "user_permissions"),
                "description": "Staff and superuser open this admin console only. They give "
                "no repository role and no disposal approval authority. Approving a disposal "
                "needs the accounts | approve_disposal permission, granted to qualified "
                "people here; recording specimen request decisions needs the accounts | "
                "approve_distribution permission, granted the same way. Untick Active instead "
                "of deleting a user.",
            },
        ),
        ("Important dates", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("username", "email", "usable_password", "password1", "password2"),
            },
        ),
        ("Repository roles", {"fields": ("groups",)}),
        ("Technical access", {"fields": ("is_staff", "is_superuser")}),
    )

    def save_model(self, request, obj, form, change):
        obj._audit_before = account_snapshot(User.objects.get(pk=obj.pk)) if change else {}
        super().save_model(request, obj, form, change)

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        obj = form.instance
        after = account_snapshot(obj)
        before = obj._audit_before
        if after != before:
            record_event(
                actor=request.user,
                action="account.updated" if change else "account.created",
                obj=obj,
                before=before,
                after=after,
                reason="Django admin",
            )

    def user_change_password(self, request, id, form_url=""):
        with transaction.atomic():
            response = super().user_change_password(request, id, form_url)
            if request.method == "POST" and response.status_code == 302:
                user = self.get_object(request, unquote(id))
                record_event(
                    actor=request.user,
                    action="account.password_changed",
                    obj=user,
                    reason="Django admin; password value not recorded",
                )
            return response


admin.site.unregister(Group)


@admin.register(Group)
class RoleGroupAdmin(SuperuserOnly, GroupAdmin):
    """Group permissions can carry disposal approval, so edits are audited and names fixed."""

    actions = None
    list_display = ("name", "members", "grants_approval", "grants_request_decisions")

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .annotate(member_count=Count("user", distinct=True))
            .prefetch_related("permissions__content_type")
        )

    @admin.display(description="Members", ordering="member_count")
    def members(self, group):
        return group.member_count

    @admin.display(description="Grants disposal approval", boolean=True)
    def grants_approval(self, group):
        return any(
            p.codename == "approve_disposal" and p.content_type.app_label == "accounts"
            for p in group.permissions.all()
        )

    @admin.display(description="Grants request decisions", boolean=True)
    def grants_request_decisions(self, group):
        return any(
            p.codename == "approve_distribution" and p.content_type.app_label == "accounts"
            for p in group.permissions.all()
        )

    def get_readonly_fields(self, request, obj=None):
        return ("name",) if obj is not None and obj.name in ROLES else ()

    def save_model(self, request, obj, form, change):
        obj._audit_before = group_snapshot(Group.objects.get(pk=obj.pk)) if change else {}
        super().save_model(request, obj, form, change)

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        obj = form.instance
        after = group_snapshot(obj)
        if after != obj._audit_before:
            record_event(
                actor=request.user,
                action="group.updated" if change else "group.created",
                obj=obj,
                before=obj._audit_before,
                after=after,
                reason="Django admin",
            )


admin.site.unregister(Authenticator)


@admin.register(Authenticator)
class AuthenticatorAdmin(SuperuserOnly, admin.ModelAdmin):
    """Read-only factor records without their secrets; removal is the only change.

    Removing a lost authenticator is the account-recovery path, so a superuser may delete one
    record at a time (no bulk action) and the removal is audited against the account in the
    same transaction. Enrollment stays on the user's own two-factor pages.
    """

    fields = ("user", "type", "created_at", "last_used_at")  # never "data": secrets, codes
    readonly_fields = fields
    list_display = fields
    list_filter = ("type", "created_at", "last_used_at")
    list_select_related = ("user",)
    actions = None

    def lookup_allowed(self, lookup, value, request=None):
        # As UserAdmin does for password: a changelist filter on the stored secret, such as
        # ?data__secret__startswith=<prefix>, would reveal it one character per request.
        if lookup.split(LOOKUP_SEP, 1)[0] == "data":
            return False
        return super().lookup_allowed(lookup, value, request)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return self._allowed(request)

    def delete_model(self, request, obj):
        user = obj.user
        before = {
            "authenticator_id": obj.pk,
            "type": obj.type,
            "created_at": obj.created_at.isoformat(),
            "last_used_at": obj.last_used_at.isoformat() if obj.last_used_at else None,
        }
        super().delete_model(request, obj)
        record_event(
            actor=request.user,
            action="account.authenticator_removed",
            obj=user,
            before=before,
            reason="Django admin; authenticator secret not recorded",
        )


admin.site.unregister(EmailAddress)


@admin.register(EmailAddress)
class EmailAddressAdmin(SuperuserOnly, admin.ModelAdmin):
    """Authentication email addresses, read only. Users manage their own addresses."""

    fields = ("user", "email", "verified", "primary")
    readonly_fields = fields
    list_display = ("email", "user", "primary", "verified")
    list_filter = ("primary", "verified")
    list_select_related = ("user",)
    search_fields = (
        "email",
        "user__username",
        "user__first_name",
        "user__last_name",
        "user__email",
    )
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
