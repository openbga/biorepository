from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("accounts", "0001_initial")]

    operations = [
        migrations.AlterModelOptions(
            name="user",
            options={
                "permissions": [
                    ("approve_disposal", "Can approve specimen disposal"),
                    ("approve_distribution", "Can record specimen request decisions"),
                ],
                "verbose_name": "user",
                "verbose_name_plural": "users",
            },
        ),
    ]
