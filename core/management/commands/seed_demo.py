import logging
import os

from django.contrib.auth.models import Group, Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.models import User
from accounts.roles import ROLES
from core.announcements import save_announcement
from core.models import Announcement, Repository
from distribution.seed import seed_distribution, seed_full_distribution
from equipment.seed import seed_equipment, seed_full_equipment
from specimens.seed import seed_specimens
from storage.seed import seed_storage

DEMO_REPOSITORY_NAME = "Example Biorepository"
ACCOUNTS_ANNOUNCEMENT = "Demo accounts"
# Listed on the sign-in page by --announce-accounts. demo_admin manages announcements and
# accounts, so it is never listed and must not share the published password.
ANNOUNCED_ACCOUNTS = (
    (
        "demo_manager",
        "repository manager: storage setup, corrections, and disposal and request decisions",
    ),
    (
        "demo_technician",
        "technician: registration, storage, check-out, and disposal and specimen requests",
    ),
    ("demo_viewer", "viewer: read-only access"),
)
# Accounts that receive the explicit approval permissions (accounts.roles): disposal approval
# and specimen request decisions. Both are granted per person, never by role.
APPROVERS = ("manager",)
APPROVAL_PERMISSIONS = ("approve_disposal", "approve_distribution")


class Command(BaseCommand):
    help = "Create synthetic demo records once; never reset existing data or passwords."

    def add_arguments(self, parser):
        parser.add_argument(
            "--full",
            action="store_true",
            help="Also create a synthetic working repository: about 350 specimens from coded "
            "participants in tanks, freezers and a refrigerator, aliquot families, check-outs "
            "(some for weeks), holds, disposal requests in every state, bulk imports and "
            "reasoned corrections, the equipment records for every storage unit (service "
            "events, LN2 level log, temperature readings, excursions) and distribution: "
            "studies, consent records, a withdrawal, agreements and shipments.",
        )
        parser.add_argument(
            "--announce-accounts",
            action="store_true",
            help="For a public demonstration: also post a sign-in page announcement that lists "
            "the demo accounts and DEMO_PASSWORD, where anyone can read them, and stop those "
            "accounts changing their own credentials. demo_admin is left out and needs its own "
            "password: set DEMO_ADMIN_PASSWORD before the first seed. DEMO_RESET_NOTE, if set, "
            "ends the announcement (for example when a scheduled reset_demo runs).",
        )

    def handle(self, *args, **options):
        # The pick lists and manifests the seed generates log every PDF layout step at INFO.
        logger = logging.getLogger("weasyprint")
        level = logger.level
        logger.setLevel(logging.WARNING)
        try:
            self._seed(**options)
        finally:
            logger.setLevel(level)

    @transaction.atomic
    def _seed(self, **options):
        password = os.environ.get("DEMO_PASSWORD")
        if not password or len(password) < 12:
            raise CommandError("Set DEMO_PASSWORD to at least 12 characters; it is never printed.")
        admin_password = os.environ.get("DEMO_ADMIN_PASSWORD") or password
        if len(admin_password) < 12:
            raise CommandError(
                "DEMO_ADMIN_PASSWORD needs at least 12 characters, or leave it unset."
            )
        repository, created = Repository.objects.get_or_create(
            pk=1, defaults={"name": DEMO_REPOSITORY_NAME}
        )
        if not created and repository.name != DEMO_REPOSITORY_NAME:
            raise CommandError("Refusing to seed a non-demo repository.")
        approvals = list(
            Permission.objects.filter(
                content_type__app_label="accounts", codename__in=APPROVAL_PERMISSIONS
            )
        )
        if len(approvals) != len(APPROVAL_PERMISSIONS):
            raise CommandError("Run migrate first: the approval permissions are missing.")
        for role in ROLES:
            group, _ = Group.objects.get_or_create(name=role)
            user, new = User.objects.get_or_create(
                username=f"demo_{role}",
                defaults={
                    "email": f"{role}@example.invalid",
                    "first_name": "Demo",
                    "last_name": role,
                    "is_staff": role == "admin",
                },
            )
            if new:
                user.set_password(admin_password if role == "admin" else password)
                user.save()
                user.groups.add(group)
                if role in APPROVERS:
                    user.user_permissions.add(*approvals)
        if options.get("announce_accounts"):
            posted = self._announce_accounts(password)
            self.stdout.write(
                "Demo accounts announced on the sign-in page."
                if posted
                else "Demo accounts announcement already present; left as it is."
            )
        # Storage, then specimens and their events.
        seed_storage(actor=User.objects.get(username="demo_manager"))
        # The first journey, through the specimen services; idempotent by the DEMO collection.
        seed_specimens(
            manager=User.objects.get(username="demo_manager"),
            technician=User.objects.get(username="demo_technician"),
        )
        # Equipment records of the first tank and freezer; idempotent by asset ID.
        manager = User.objects.get(username="demo_manager")
        technician = User.objects.get(username="demo_technician")
        seed_equipment(manager=manager, technician=technician)
        # Requests, an agreement, consent records and one shipment through its receipt and return.
        seed_distribution(manager=manager, technician=technician)
        if options.get("full"):
            from ._demo_full import seed_full_demo  # loaded only when asked

            created = seed_full_demo()
            self.stdout.write(
                "Synthetic working repository created."
                if created
                else "Synthetic working repository already present; nothing added."
            )
            # After the specimens: an excursion captures the specimens that were in the unit.
            seed_full_equipment(
                manager=manager,
                technician=technician,
                admin=User.objects.get(username="demo_admin"),
            )
            # Last: governance over the larger cohorts, a withdrawal with its holds, a packed
            # shipment. It needs the specimens of the full story.
            seed_full_distribution(
                manager=manager,
                technician=technician,
                admin=User.objects.get(username="demo_admin"),
            )
        self.stdout.write(
            self.style.SUCCESS("Synthetic demo ready; existing records/passwords retained.")
        )

    def _announce_accounts(self, password):
        """Lock the accounts DEMO_PASSWORD opens and post them on the sign-in page, once.

        Never demo_admin: it manages announcements and accounts.
        """
        admin = User.objects.get(username="demo_admin")
        if admin.check_password(password):
            raise CommandError(
                "Refusing to publish DEMO_PASSWORD: it also opens demo_admin, which manages "
                "announcements and accounts. Give demo_admin its own password (set "
                "DEMO_ADMIN_PASSWORD before the first seed, or change its password) and run "
                "this again. Nothing was saved."
            )
        users = User.objects.in_bulk(
            [name for name, _ in ANNOUNCED_ACCOUNTS], field_name="username"
        )
        shared = [
            (name, work)
            for name, work in ANNOUNCED_ACCOUNTS
            if name in users and users[name].check_password(password)
        ]
        if not shared:
            raise CommandError(
                "No demo account uses DEMO_PASSWORD, so there is nothing to announce. "
                "Nothing was saved."
            )
        # Anyone can sign in to these accounts, so none of them may change its own credentials.
        User.objects.filter(username__in=[name for name, _ in shared]).update(
            credentials_locked=True
        )
        if Announcement.objects.filter(title=ACCOUNTS_ANNOUNCEMENT).exists():
            return False  # posted before; an administrator may have changed or withdrawn it
        lines = [f"{name}: {work}" for name, work in shared]
        # A deployment with a scheduled reset_demo says when (render.demo.yaml sets this).
        reset_note = (
            os.environ.get("DEMO_RESET_NOTE", "").strip()
            or "The demonstration can be reset at any time."
        )
        body = "\n".join(
            [
                "Public demonstration with synthetic data. Sign in as:",
                "",
                *lines,
                "",
                f"Password for each: {password}",
                "",
                "Do not enter real data. Anyone can use these accounts. " + reset_note,
            ]
        )
        try:
            save_announcement(
                actor=admin,
                instance=Announcement(title=ACCOUNTS_ANNOUNCEMENT, body=body, show_on_sign_in=True),
            )
        except PermissionDenied as exc:
            raise CommandError(
                "demo_admin no longer holds the admin role, so it cannot post the announcement. "
                "Nothing was saved."
            ) from exc
        except ValidationError as exc:
            raise CommandError(
                f"The demo accounts announcement was refused: {' '.join(exc.messages)} "
                "Nothing was saved."
            ) from exc
        return True
