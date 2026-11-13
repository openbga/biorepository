from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied

# Group names. The list is a working default (docs/product.md, working assumptions).
ROLES = ("admin", "manager", "technician", "viewer")
OPERATIONAL_ROLES = frozenset({"manager", "technician", "viewer"})


def effective_roles(user):
    """Work roles, including admin's operational access; no membership changes.

    Disposal approval is an explicit permission and is not implied by admin.
    Superuser status alone grants no application role.
    """
    if not user.is_authenticated or not user.is_active:
        return set()
    roles = set(user.groups.values_list("name", flat=True))
    if "admin" in roles:
        roles.update(OPERATIONAL_ROLES)
    return roles


def has_role(user, *roles):
    return bool(effective_roles(user).intersection(roles))


def require_role(user, *roles):
    if not has_role(user, *roles):
        raise PermissionDenied("Your role does not permit this action.")


def require_staff(user):
    require_role(user, *ROLES)


def staff_role_required(*roles):
    """View decorator: sign-in first, then one of the named roles (403 otherwise)."""

    def decorator(view):
        @login_required
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            require_role(request.user, *roles)
            return view(request, *args, **kwargs)

        return wrapper

    return decorator


def can_approve_disposal(user):
    """Explicit repository capability; Django superuser status alone is insufficient.

    Holding it never lets anyone approve a disposal they requested; the disposal service checks
    that separately.
    """
    if not user.is_authenticated or not user.is_active:
        return False
    permission = {"codename": "approve_disposal", "content_type__app_label": "accounts"}
    return (
        user.user_permissions.filter(**permission).exists()
        or user.groups.filter(
            permissions__codename="approve_disposal",
            permissions__content_type__app_label="accounts",
        ).exists()
    )


def require_disposal_authority(user):
    if not can_approve_disposal(user):
        raise PermissionDenied("Explicit disposal approval permission is required.")


def can_decide_requests(user):
    """Explicit repository capability to record specimen request decisions (approve, reject,
    return); granted per person like disposal approval. Superuser status alone is insufficient,
    and nobody decides a request they created or submitted (the distribution service checks)."""
    if not user.is_authenticated or not user.is_active:
        return False
    permission = {"codename": "approve_distribution", "content_type__app_label": "accounts"}
    return (
        user.user_permissions.filter(**permission).exists()
        or user.groups.filter(
            permissions__codename="approve_distribution",
            permissions__content_type__app_label="accounts",
        ).exists()
    )


def require_request_authority(user):
    if not can_decide_requests(user):
        raise PermissionDenied("Explicit request decision permission is required.")
