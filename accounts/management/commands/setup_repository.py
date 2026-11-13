import os
from getpass import getpass

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from accounts.models import User
from accounts.roles import ROLES
from accounts.setup import setup_repository


class Command(BaseCommand):
    help = (
        "Create the repository record, the role groups and the first administrator on a fresh "
        "database. "
        "Safe to rerun: existing records, passwords and roles are never changed unless you "
        "pass an explicit update flag. The password comes from SETUP_ADMIN_PASSWORD or a "
        "hidden prompt, never from an argument."
    )

    def add_arguments(self, parser):
        parser.add_argument("--repository-name", help="Organization name shown on screens.")
        parser.add_argument("--admin-username", help="Administrator to create or attribute to.")
        parser.add_argument("--admin-email", default="", help="Needed when creating the account.")
        parser.add_argument("--admin-first-name", default="")
        parser.add_argument("--admin-last-name", default="")
        parser.add_argument(
            "--role",
            action="append",
            choices=ROLES,
            help="Role group for the administrator; repeatable. A new account defaults to "
            "admin. For an existing account the named roles are added, never removed. "
            "Disposal approval is a separate permission granted in the admin.",
        )
        parser.add_argument(
            "--update-repository", action="store_true", help="Rename an existing repository."
        )
        parser.add_argument(
            "--sync-timezone",
            action="store_true",
            help="Set the recorded repository time zone to the deployment TIME_ZONE setting.",
        )

    def handle(self, *args, **options):
        username = options["admin_username"]
        password = None
        if username and not User.objects.filter(username=username).exists():
            password = os.environ.get("SETUP_ADMIN_PASSWORD") or self._prompt()
        try:
            result = setup_repository(
                repository_name=options["repository_name"],
                update_repository=options["update_repository"],
                sync_timezone=options["sync_timezone"],
                username=username,
                email=options["admin_email"],
                password=password,
                first_name=options["admin_first_name"],
                last_name=options["admin_last_name"],
                roles=options["role"],
            )
        except ValidationError as exc:
            raise CommandError("; ".join(exc.messages)) from exc
        for message in result.messages:
            self.stdout.write(message)
        for warning in result.warnings:
            self.stderr.write(self.style.WARNING(warning))
        if not result.changed:
            self.stdout.write("Nothing to change; the repository is already set up.")
        else:
            self.stdout.write(self.style.SUCCESS("Setup complete."))

    def _prompt(self):
        if not os.isatty(0):
            raise CommandError(
                "Set SETUP_ADMIN_PASSWORD in the environment or run from a terminal."
            )
        first = getpass("Administrator password: ")
        if first != getpass("Repeat password: "):
            raise CommandError("The passwords did not match.")
        return first
