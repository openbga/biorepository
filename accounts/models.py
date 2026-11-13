from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    email = models.EmailField(unique=True)
    # Set only by `seed_demo --announce-accounts`, for accounts whose password it publishes.
    # db_default keeps the previous release's inserts valid while both releases run.
    credentials_locked = models.BooleanField(
        default=False,
        db_default=False,
        help_text="Shared demonstration account: whoever signs in cannot change its password, "
        "email addresses or two-factor settings.",
    )

    class Meta(AbstractUser.Meta):
        permissions = [
            ("approve_disposal", "Can approve specimen disposal"),
            ("approve_distribution", "Can record specimen request decisions"),
        ]
