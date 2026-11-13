from django.db import migrations

from core.immutability import FUNCTION, protect_history

# Append-only: UPDATE and DELETE refused; corrections add a row.
HISTORY_TABLES = (
    "distribution_consentrecord",
    "distribution_consentanswer",
    "distribution_consentwithdrawal",
    "distribution_consenthold",
    "distribution_consentreinstatement",
    "distribution_withdrawalfollowup",
    "distribution_requestsubmission",
    "distribution_requestdecision",
    "distribution_requestevent",
    "distribution_allocationevent",
    "distribution_shipmentitem",
    "distribution_shipmentevent",
    "distribution_shipmentreceipt",
    "distribution_receiptexception",
    "distribution_specimenreturn",
    "distribution_returnreview",
    "distribution_agreementdocument",
    "distribution_agreementevent",
    "distribution_outputrecord",
)
# Records whose details and state change through the services but which are never deleted.
# Draft request lines (distribution_requestitem) stay deletable while nothing refers to them.
UNDELETABLE_TABLES = (
    "distribution_study",
    "distribution_studycollection",
    "distribution_consentscopecategory",
    "distribution_consentform",
    "distribution_institution",
    "distribution_investigator",
    "distribution_consent",
    "distribution_specimenrequest",
    "distribution_allocation",
    "distribution_shipment",
    "distribution_agreement",
)


def delete_guard(table):
    trigger = f"{table}_undeletable"
    return migrations.RunSQL(
        f"CREATE TRIGGER {trigger} BEFORE DELETE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION {FUNCTION}();",
        f"DROP TRIGGER {trigger} ON {table};",
    )


class Migration(migrations.Migration):
    dependencies = [("core", "0002_audit_immutable"), ("distribution", "0001_initial")]
    operations = [
        *(protect_history(table) for table in HISTORY_TABLES),
        *(delete_guard(table) for table in UNDELETABLE_TABLES),
    ]
