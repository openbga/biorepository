"""Equipment's references now point at `core.Attachment` (same `attachment` table); the
equipment model state is dropped. State only: no table, column, constraint or trigger changes."""

import django.db.models.deletion
from django.db import migrations, models


def _reference():
    return models.ForeignKey(
        blank=True,
        null=True,
        on_delete=django.db.models.deletion.PROTECT,
        related_name="+",
        to="core.attachment",
    )


class Migration(migrations.Migration):
    dependencies = [
        ("equipment", "0003_attachment_records"),
        ("core", "0003_attachment"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField("serviceevent", "attachment", _reference()),
                migrations.AlterField("excursionevent", "attachment", _reference()),
                migrations.DeleteModel("Attachment"),
            ],
            database_operations=[],
        ),
    ]
