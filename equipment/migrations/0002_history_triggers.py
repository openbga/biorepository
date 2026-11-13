from django.db import migrations

from core.immutability import FUNCTION, protect_history

# Append-only: UPDATE and DELETE refused; corrections add a row.
HISTORY_TABLES = (
    "equipment_statuschange",
    "equipment_serviceevent",
    "equipment_ln2entry",
    "equipment_temperaturereading",
    "equipment_readingimport",
    "equipment_readingimportcommit",
    "equipment_excursionevent",
    "equipment_affectedspecimen",
)
# Records whose details and state change through the services but which are never deleted.
UNDELETABLE_TABLES = ("equipment_equipment", "equipment_excursion")


def delete_guard(table):
    trigger = f"{table}_undeletable"
    return migrations.RunSQL(
        f"CREATE TRIGGER {trigger} BEFORE DELETE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION {FUNCTION}();",
        f"DROP TRIGGER {trigger} ON {table};",
    )


class Migration(migrations.Migration):
    dependencies = [("core", "0002_audit_immutable"), ("equipment", "0001_initial")]
    operations = [
        *(protect_history(table) for table in HISTORY_TABLES),
        *(delete_guard(table) for table in UNDELETABLE_TABLES),
    ]
