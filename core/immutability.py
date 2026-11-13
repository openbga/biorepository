"""Database triggers that make history tables append-only.

core migration 0002 creates one trigger function, prevent_record_mutation(), and attaches it to
core_auditevent. Every app with append-only event tables attaches the same function to its own
tables in its own migration, depending on ("core", "0002_audit_immutable"):

    from core.immutability import protect_history

    class Migration(migrations.Migration):
        dependencies = [("core", "0002_audit_immutable"), ("storage", "0001_initial")]
        operations = [protect_history("storage_moveevent")]

UPDATE and DELETE of a protected row raise an error, so corrections must add a row. The trigger
does not stop TRUNCATE, DROP or a database owner disabling it (docs/operations.md).
"""

import re

from django.db import migrations

FUNCTION = "prevent_record_mutation"
_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def _identifier(value):
    if not _IDENTIFIER.match(value):
        raise ValueError(f"Not a plain lower-case SQL identifier: {value!r}")
    return value


def protect_history(table, trigger=None):
    """A RunSQL operation attaching prevent_record_mutation() to `table` (reversible)."""
    table = _identifier(table)
    trigger = _identifier(trigger or f"{table}_immutable")
    return migrations.RunSQL(
        f"CREATE TRIGGER {trigger} BEFORE UPDATE OR DELETE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION {FUNCTION}();",
        f"DROP TRIGGER {trigger} ON {table};",
    )
