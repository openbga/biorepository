"""`core.Attachment` takes over the `attachment` table that equipment 0003 created.

State only: the table, its rows, constraints, index and append-only trigger stay exactly as they
are, on a fresh database (equipment 0003 creates them first) and on one where equipment 0003 is
already applied. equipment 0004 then points its references here and drops its own model state.
"""

import uuid

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0002_audit_immutable"),
        ("equipment", "0003_attachment_records"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name="Attachment",
                    fields=[
                        (
                            "id",
                            models.UUIDField(
                                default=uuid.uuid4,
                                editable=False,
                                primary_key=True,
                                serialize=False,
                            ),
                        ),
                        ("name", models.CharField(max_length=255)),
                        ("content_type", models.CharField(max_length=100)),
                        ("size", models.PositiveIntegerField()),
                        ("sha256", models.CharField(max_length=64)),
                        ("data", models.BinaryField()),
                        ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                        (
                            "created_by",
                            models.ForeignKey(
                                on_delete=django.db.models.deletion.PROTECT,
                                related_name="+",
                                to=settings.AUTH_USER_MODEL,
                            ),
                        ),
                    ],
                    options={
                        "db_table": "attachment",
                        "base_manager_name": "objects",
                        "indexes": [models.Index(fields=["sha256"], name="attachment_sha256_idx")],
                        "constraints": [
                            models.CheckConstraint(
                                condition=models.Q(
                                    models.Q(("name", ""), _negated=True),
                                    models.Q(("content_type", ""), _negated=True),
                                    ("size__gte", 1),
                                ),
                                name="attachment_described",
                            ),
                            models.CheckConstraint(
                                condition=models.Q(("sha256__regex", "^[0-9a-f]{64}$")),
                                name="attachment_sha256",
                            ),
                        ],
                    },
                ),
            ],
            database_operations=[],
        ),
    ]
