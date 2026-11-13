from django.db import migrations

from core.immutability import FUNCTION, protect_history

HISTORY_TABLES = (
    "specimens_derivation",
    "specimens_derivationchild",
    "specimens_quantityentry",
    "specimens_custodyevent",
    "specimens_thawevent",
    "specimens_hold",
    "specimens_holdrelease",
    "specimens_disposalrequest",
    "specimens_disposaldecision",
    "specimens_disposalexecution",
    "specimens_correction",
    "specimens_importbatch",
    "specimens_importcommit",
    "specimens_outputrecord",
)
# Rows whose projections or definitions change but which are never deleted.
UNDELETABLE_TABLES = (
    "specimens_specimen",
    "specimens_participant",
    "specimens_collection",
    "specimens_specimentype",
    "specimens_quantityunit",
    "specimens_specimencontainertype",
)


def delete_guard(table):
    trigger = f"{table}_undeletable"
    return migrations.RunSQL(
        f"CREATE TRIGGER {trigger} BEFORE DELETE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION {FUNCTION}();",
        f"DROP TRIGGER {trigger} ON {table};",
    )


class Migration(migrations.Migration):
    dependencies = [("core", "0002_audit_immutable"), ("specimens", "0001_initial")]
    operations = [
        # Accessions VP-000001...: allocated by nextval, never reset or recycled; gaps expected.
        migrations.RunSQL(
            "CREATE SEQUENCE specimens_accession_seq AS bigint START 1 MINVALUE 1 NO CYCLE;",
            "DROP SEQUENCE specimens_accession_seq;",
        ),
        *(protect_history(table) for table in HISTORY_TABLES),
        *(delete_guard(table) for table in UNDELETABLE_TABLES),
    ]
