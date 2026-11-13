from django.db import migrations

from core.immutability import protect_history

# The one trigger function every app's history tables use (core.immutability).
FUNCTION = """
CREATE FUNCTION prevent_record_mutation() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'Historical records cannot be updated or deleted';
END;
$$ LANGUAGE plpgsql;
"""


class Migration(migrations.Migration):
    dependencies = [("core", "0001_initial")]
    operations = [
        migrations.RunSQL(FUNCTION, "DROP FUNCTION prevent_record_mutation();"),
        protect_history("core_auditevent", "audit_immutable"),
    ]
