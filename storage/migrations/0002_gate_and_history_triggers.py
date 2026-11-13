from django.db import migrations

from core.immutability import FUNCTION, protect_history


def delete_guard(table):
    """Refuse DELETE only: the row's projection (claim, revision) still changes."""
    trigger = f"{table}_undeletable"
    return migrations.RunSQL(
        f"CREATE TRIGGER {trigger} BEFORE DELETE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION {FUNCTION}();",
        f"DROP TRIGGER {trigger} ON {table};",
    )


def create_gate(apps, schema_editor):
    apps.get_model("storage", "StorageGate").objects.get_or_create(pk=1)


class Migration(migrations.Migration):
    dependencies = [("core", "0002_audit_immutable"), ("storage", "0001_initial")]
    operations = [
        migrations.RunPython(create_gate, migrations.RunPython.noop),
        protect_history("storage_moveevent"),
        protect_history("storage_movemember"),
        delete_guard("storage_occupancy"),
        delete_guard("storage_storagegate"),
    ]
