"""Specimens, their catalogues, quantity ledger, custody, thaw, holds, disposal and imports.

Row-local integrity is a database constraint here. Rules spanning rows (one open check-out, one
unresolved disposal request, chronology, lineage, approval by a different person) are checked by
`specimens.services` under the StorageGate and the specimen row lock. Event tables refuse UPDATE
and DELETE by trigger (migration 0002); Specimen refuses DELETE. The specimen columns
`custodian`, `open_checkout`, `quantity_on_hand` and `status` are projections maintained by the
services in the same transaction as the events they summarise.

No donor identity is stored anywhere: a participant is a coded identifier only.
"""

import uuid

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

CODE_PATTERN = r"^[A-Z0-9][A-Z0-9._-]*$"
UNIT_CODE_PATTERN = r"^[A-Za-z][A-Za-z0-9._/-]*$"
ACCESSION_PATTERN = r"^VP-[0-9]{6,}$"
QUANTITY_DIGITS = 18
QUANTITY_PLACES = 6
NO_IDENTIFYING_TEXT = (
    "Do not enter names, record numbers, birth dates, contact details or any other text that "
    "could identify a donor."
)


def _code_check(name, pattern=CODE_PATTERN):
    return models.CheckConstraint(condition=Q(code__regex=pattern), name=name)


def _reason_check(name, field="reason"):
    return models.CheckConstraint(condition=~Q(**{field: ""}), name=name)


def _quantity(**kwargs):
    return models.DecimalField(max_digits=QUANTITY_DIGITS, decimal_places=QUANTITY_PLACES, **kwargs)


class Definition(models.Model):
    """Attribution and optimistic revision shared by every editable catalogue record."""

    revision = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", editable=False
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        abstract = True

    @property
    def status(self):
        return "active" if self.is_active else "inactive"


# --- catalogues ------------------------------------------------------------------------------


class Participant(Definition):
    """A coded participant identifier, e.g. P-000123. Never a name or record number."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=40, unique=True)

    class Meta:
        ordering = ["code"]
        constraints = [_code_check("specimens_participant_code_format")]

    def __str__(self):
        return self.code


class Collection(Definition):
    """An optional organizational grouping; not a visit, consent or study approval."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=160)

    class Meta:
        ordering = ["code"]
        constraints = [_code_check("specimens_collection_code_format")]

    def __str__(self):
        return self.code


class SpecimenType(Definition):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=160)

    class Meta:
        ordering = ["code"]
        constraints = [_code_check("specimens_specimentype_code_format")]

    def __str__(self):
        return f"{self.code} · {self.name}"


class QuantityUnit(Definition):
    """Amount unit. Codes are case-sensitive (mL, uL, mg, g, count) and never upper-cased."""

    class Dimension(models.TextChoices):
        VOLUME = "volume", "Volume"
        MASS = "mass", "Mass"
        COUNT = "count", "Count"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=16, unique=True)
    name = models.CharField(max_length=80)
    dimension = models.CharField(max_length=12, choices=Dimension.choices)
    fractional = models.BooleanField(default=True)

    class Meta:
        ordering = ["code"]
        constraints = [
            _code_check("specimens_quantityunit_code_format", UNIT_CODE_PATTERN),
            models.CheckConstraint(
                condition=Q(dimension__in=["volume", "mass", "count"]),
                name="specimens_quantityunit_dimension",
            ),
            models.CheckConstraint(
                condition=~Q(dimension="count", fractional=True),
                name="specimens_quantityunit_count_integral",
            ),
        ]

    def __str__(self):
        return self.code


class SpecimenContainerType(Definition):
    """The vessel holding one specimen (cryovial, straw, bag). Not a box grid."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=160)
    nominal_capacity = _quantity(null=True, blank=True)
    capacity_unit = models.ForeignKey(
        QuantityUnit, on_delete=models.PROTECT, related_name="+", null=True, blank=True
    )

    class Meta:
        ordering = ["code"]
        constraints = [
            _code_check("specimens_containertype_code_format"),
            models.CheckConstraint(
                condition=Q(nominal_capacity__isnull=True, capacity_unit__isnull=True)
                | Q(nominal_capacity__gt=0, capacity_unit__isnull=False),
                name="specimens_containertype_capacity",
            ),
        ]

    def __str__(self):
        return f"{self.code} · {self.name}"


# --- specimen --------------------------------------------------------------------------------


class Specimen(models.Model):
    class Kind(models.TextChoices):
        PRIMARY = "primary", "Primary"
        ALIQUOT = "aliquot", "Aliquot"
        DERIVATIVE = "derivative", "Derivative"

    class Status(models.TextChoices):
        AVAILABLE = "available", "Available"
        EXHAUSTED = "exhausted", "Exhausted"
        DISPOSED = "disposed", "Disposed"
        DISTRIBUTED = "distributed", "Distributed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    accession = models.CharField(max_length=32, unique=True, editable=False)
    occupancy = models.OneToOneField(
        "storage.Occupancy", on_delete=models.PROTECT, related_name="+", editable=False
    )
    specimen_type = models.ForeignKey(SpecimenType, on_delete=models.PROTECT, related_name="+")
    container_type = models.ForeignKey(
        SpecimenContainerType, on_delete=models.PROTECT, related_name="+"
    )
    participant = models.ForeignKey(
        Participant, on_delete=models.PROTECT, related_name="specimens", null=True, blank=True
    )
    collection = models.ForeignKey(
        Collection, on_delete=models.PROTECT, related_name="specimens", null=True, blank=True
    )
    parent = models.ForeignKey(
        "self", on_delete=models.PROTECT, related_name="children", null=True, blank=True
    )
    derivation_kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.PRIMARY)
    collected_at = models.DateTimeField()
    processing_started_at = models.DateTimeField(null=True, blank=True)
    processing_completed_at = models.DateTimeField(null=True, blank=True)
    registered_at = models.DateTimeField(default=timezone.now, editable=False)
    registered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", editable=False
    )
    unit = models.ForeignKey(QuantityUnit, on_delete=models.PROTECT, related_name="+")
    quantity_on_hand = _quantity()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.AVAILABLE)
    # Projections of the custody events, maintained under the specimen lock. No custodian while
    # distributed: the material is outside the repository (dispatch has no staff recipient).
    custodian = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", null=True, blank=True
    )
    open_checkout = models.OneToOneField(
        "CustodyEvent", on_delete=models.PROTECT, related_name="+", null=True, blank=True
    )
    revision = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["accession"]
        constraints = [
            models.CheckConstraint(
                condition=Q(accession__regex=ACCESSION_PATTERN), name="specimens_accession_format"
            ),
            models.CheckConstraint(
                condition=Q(quantity_on_hand__gte=0), name="specimens_specimen_quantity"
            ),
            models.CheckConstraint(
                condition=Q(derivation_kind="primary", parent__isnull=True)
                | Q(derivation_kind__in=["aliquot", "derivative"], parent__isnull=False),
                name="specimens_specimen_lineage",
            ),
            models.CheckConstraint(
                condition=~Q(parent=F("id")), name="specimens_specimen_not_own_parent"
            ),
            models.CheckConstraint(
                condition=Q(processing_completed_at__isnull=True)
                | Q(processing_started_at__isnull=False),
                name="specimens_specimen_processing_started",
            ),
            models.CheckConstraint(
                condition=Q(processing_started_at__isnull=True)
                | Q(processing_started_at__gte=F("collected_at")),
                name="specimens_specimen_processing_after_collection",
            ),
            models.CheckConstraint(
                condition=Q(processing_completed_at__isnull=True)
                | Q(processing_completed_at__gte=F("processing_started_at")),
                name="specimens_specimen_processing_order",
            ),
            models.CheckConstraint(
                condition=Q(status="available", quantity_on_hand__gt=0)
                | Q(status__in=["exhausted", "disposed", "distributed"], quantity_on_hand=0),
                name="specimens_specimen_status_quantity",
            ),
            models.CheckConstraint(
                condition=~Q(status="disposed", open_checkout__isnull=False),
                name="specimens_specimen_disposed_closed",
            ),
            models.CheckConstraint(
                condition=~Q(status="distributed", open_checkout__isnull=False),
                name="specimens_specimen_distributed_closed",
            ),
            models.CheckConstraint(
                condition=Q(status="distributed", custodian__isnull=True)
                | (~Q(status="distributed") & Q(custodian__isnull=False)),
                name="specimens_specimen_custodian",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "registered_at"], name="specimens_status_registered"),
            models.Index(fields=["participant", "collected_at"], name="specimens_spec_participant"),
            models.Index(fields=["collection", "specimen_type"], name="specimens_collection_type"),
        ]

    def __str__(self):
        return self.accession

    @property
    def is_disposed(self):
        return self.status == self.Status.DISPOSED

    @property
    def is_distributed(self):
        return self.status == self.Status.DISTRIBUTED


# --- events (append-only) ----------------------------------------------------------------------


class EventMixin(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    occurred_at = models.DateTimeField()
    recorded_at = models.DateTimeField(default=timezone.now)
    reason = models.TextField()

    class Meta:
        abstract = True


class Derivation(EventMixin):
    class Kind(models.TextChoices):
        ALIQUOT = "aliquot", "Aliquots"
        DERIVATIVE = "derivative", "Derivatives"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    parent = models.ForeignKey(Specimen, on_delete=models.PROTECT, related_name="derivations")
    kind = models.CharField(max_length=12, choices=Kind.choices)
    consumed = _quantity()
    unit = models.ForeignKey(QuantityUnit, on_delete=models.PROTECT, related_name="+")
    procedure = models.CharField(max_length=160, blank=True)

    class Meta:
        ordering = ["occurred_at", "recorded_at", "id"]
        constraints = [
            models.CheckConstraint(condition=Q(consumed__gt=0), name="specimens_derivation_used"),
            models.CheckConstraint(
                condition=Q(kind__in=["aliquot", "derivative"]), name="specimens_derivation_kind"
            ),
            _reason_check("specimens_derivation_reason"),
        ]
        indexes = [models.Index(fields=["parent", "occurred_at"], name="specimens_derivation_par")]


class DerivationChild(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    derivation = models.ForeignKey(Derivation, on_delete=models.PROTECT, related_name="children")
    child = models.OneToOneField(Specimen, on_delete=models.PROTECT, related_name="origin")
    quantity = _quantity()
    unit = models.ForeignKey(QuantityUnit, on_delete=models.PROTECT, related_name="+")

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(quantity__gt=0), name="specimens_derivchild_qty")
        ]


class QuantityEntry(EventMixin):
    class Kind(models.TextChoices):
        INITIAL = "initial", "Initial amount"
        CONSUME = "consume", "Consumed"
        DERIVE = "derive", "Used for aliquots or derivatives"
        ADJUST = "adjust", "Adjusted"
        REVERSAL = "reversal", "Reversal"
        DISPOSE = "dispose", "Disposed"
        DISTRIBUTE = "distribute", "Distributed"
        RETURN = "return", "Returned"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    specimen = models.ForeignKey(Specimen, on_delete=models.PROTECT, related_name="ledger")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    delta = _quantity()
    balance_after = _quantity()
    unit = models.ForeignKey(QuantityUnit, on_delete=models.PROTECT, related_name="+")
    supplied_text = models.CharField(max_length=80, blank=True)
    reverses = models.OneToOneField(
        "self", on_delete=models.PROTECT, related_name="reversed_by", null=True, blank=True
    )
    derivation = models.ForeignKey(
        Derivation, on_delete=models.PROTECT, related_name="entries", null=True, blank=True
    )

    class Meta:
        ordering = ["occurred_at", "recorded_at", "id"]
        verbose_name_plural = "quantity entries"
        constraints = [
            models.CheckConstraint(condition=~Q(delta=0), name="specimens_qty_nonzero"),
            models.CheckConstraint(condition=Q(balance_after__gte=0), name="specimens_qty_balance"),
            models.CheckConstraint(
                condition=Q(
                    kind__in=[
                        "initial",
                        "consume",
                        "derive",
                        "adjust",
                        "reversal",
                        "dispose",
                        "distribute",
                        "return",
                    ]
                ),
                name="specimens_qty_kind",
            ),
            models.CheckConstraint(
                condition=~Q(kind__in=["initial", "return"]) | Q(delta__gt=0),
                name="specimens_qty_initial_sign",
            ),
            models.CheckConstraint(
                condition=~Q(kind__in=["consume", "derive", "dispose", "distribute"])
                | Q(delta__lt=0),
                name="specimens_qty_debit_sign",
            ),
            models.CheckConstraint(
                condition=Q(kind="reversal", reverses__isnull=False)
                | (~Q(kind="reversal") & Q(reverses__isnull=True)),
                name="specimens_qty_reversal",
            ),
            models.CheckConstraint(
                condition=~Q(kind="derive") | Q(derivation__isnull=False),
                name="specimens_qty_derivation",
            ),
            models.UniqueConstraint(
                fields=["specimen"], condition=Q(kind="initial"), name="specimens_qty_one_initial"
            ),
            _reason_check("specimens_qty_reason"),
        ]
        indexes = [
            models.Index(fields=["specimen", "recorded_at", "id"], name="specimens_qty_specimen")
        ]


class CustodyEvent(EventMixin):
    class Kind(models.TextChoices):
        REGISTER = "register", "Registered"
        PLACE = "place", "Placed in storage"
        RELOCATE = "relocate", "Relocated"
        CHECK_OUT = "check_out", "Checked out"
        CHECK_IN = "check_in", "Checked in"
        TRANSFER = "transfer", "Custody transferred"
        DISPOSE = "dispose", "Disposed"
        CORRECTION = "correction", "Custody correction"
        DISPATCH = "dispatch", "Dispatched"
        RETURN = "return", "Returned to repository"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    specimen = models.ForeignKey(Specimen, on_delete=models.PROTECT, related_name="custody")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    from_custodian = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", null=True, blank=True
    )
    # Blank only for a dispatch (the material leaves for an outside recipient) and its corrections.
    to_custodian = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", null=True, blank=True
    )
    destination = models.CharField(max_length=160, blank=True)
    move_event = models.ForeignKey(
        "storage.MoveEvent", on_delete=models.PROTECT, related_name="+", null=True, blank=True
    )
    corrects = models.OneToOneField(
        "self", on_delete=models.PROTECT, related_name="corrected_by", null=True, blank=True
    )
    checkout = models.OneToOneField(
        "self", on_delete=models.PROTECT, related_name="checkin", null=True, blank=True
    )
    thaw_observed = models.BooleanField(null=True, blank=True)

    class Meta:
        ordering = ["occurred_at", "recorded_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(
                    kind__in=[
                        "register",
                        "place",
                        "relocate",
                        "check_out",
                        "check_in",
                        "transfer",
                        "dispose",
                        "correction",
                        "dispatch",
                        "return",
                    ]
                ),
                name="specimens_custody_kind",
            ),
            models.CheckConstraint(
                condition=~Q(kind="dispatch") | Q(to_custodian__isnull=True),
                name="specimens_custody_dispatch_outside",
            ),
            models.CheckConstraint(
                condition=Q(to_custodian__isnull=False) | Q(kind__in=["dispatch", "correction"]),
                name="specimens_custody_to",
            ),
            models.CheckConstraint(
                condition=Q(kind="check_in", checkout__isnull=False)
                | (~Q(kind="check_in") & Q(checkout__isnull=True)),
                name="specimens_custody_checkout_link",
            ),
            models.CheckConstraint(
                condition=Q(kind="correction", corrects__isnull=False)
                | (~Q(kind="correction") & Q(corrects__isnull=True)),
                name="specimens_custody_corrects",
            ),
            models.CheckConstraint(
                condition=Q(kind__in=["register", "correction", "return"])
                | Q(from_custodian__isnull=False),
                name="specimens_custody_from",
            ),
            models.CheckConstraint(
                condition=Q(kind="check_in") | Q(thaw_observed__isnull=True),
                name="specimens_custody_thaw_on_checkin",
            ),
            models.CheckConstraint(
                condition=Q(occurred_at__lte=F("recorded_at")),
                name="specimens_custody_not_future",
            ),
            _reason_check("specimens_custody_reason"),
        ]
        indexes = [
            models.Index(
                fields=["specimen", "occurred_at", "recorded_at"], name="specimens_custody_time"
            )
        ]


class ThawEvent(EventMixin):
    """An observed thaw, the baseline known at registration, or a correction of either.

    A baseline with count NULL means the history before registration is unknown. An external
    entry is what a recipient reported for the time the material was away (dispatch to return),
    linked to the return custody event; NULL means not known.
    """

    class Kind(models.TextChoices):
        BASELINE = "baseline", "Thaw history at registration"
        THAW = "thaw", "Observed thaw"
        CORRECTION = "correction", "Thaw record correction"
        EXTERNAL = "external", "Thaws while away"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    specimen = models.ForeignKey(Specimen, on_delete=models.PROTECT, related_name="thaws")
    kind = models.CharField(max_length=12, choices=Kind.choices)
    count = models.PositiveIntegerField(null=True, blank=True)
    inherited_recorded_count = models.PositiveIntegerField(default=0)
    corrects = models.OneToOneField(
        "self", on_delete=models.PROTECT, related_name="corrected_by", null=True, blank=True
    )
    custody_event = models.ForeignKey(
        CustodyEvent, on_delete=models.PROTECT, related_name="thaws", null=True, blank=True
    )

    class Meta:
        ordering = ["occurred_at", "recorded_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(kind__in=["baseline", "thaw", "correction", "external"]),
                name="specimens_thaw_kind",
            ),
            models.CheckConstraint(
                condition=~Q(kind="external")
                | Q(custody_event__isnull=False, inherited_recorded_count=0),
                name="specimens_thaw_external",
            ),
            models.UniqueConstraint(
                fields=["specimen"], condition=Q(kind="baseline"), name="specimens_thaw_baseline"
            ),
            models.CheckConstraint(
                condition=~Q(kind="thaw") | Q(count=1, inherited_recorded_count=0),
                name="specimens_thaw_single",
            ),
            models.CheckConstraint(
                condition=Q(kind="correction", corrects__isnull=False)
                | (~Q(kind="correction") & Q(corrects__isnull=True)),
                name="specimens_thaw_corrects",
            ),
            _reason_check("specimens_thaw_reason"),
        ]
        indexes = [models.Index(fields=["specimen", "occurred_at"], name="specimens_thaw_time")]


class Hold(EventMixin):
    """A hold. `disposal` blocks disposal; `use` blocks check-out, splitting, use and hand-over
    but not disposal; `all_use` blocks both. A hold with a reference belongs to the workflow that
    placed it (for example "Allocated to RQ-000012") and is released only through it."""

    class Scope(models.TextChoices):
        DISPOSAL = "disposal", "Disposal hold"
        USE = "use", "Use hold"
        ALL_USE = "all_use", "All-use hold"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    specimen = models.ForeignKey(Specimen, on_delete=models.PROTECT, related_name="holds")
    scope = models.CharField(max_length=12, choices=Scope.choices)
    reference = models.CharField(max_length=120, blank=True, default="")

    class Meta:
        ordering = ["occurred_at", "recorded_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(scope__in=["disposal", "use", "all_use"]), name="specimens_hold_scope"
            ),
            _reason_check("specimens_hold_reason"),
        ]
        indexes = [models.Index(fields=["specimen", "scope"], name="specimens_hold_scope_idx")]


class HoldRelease(EventMixin):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    hold = models.OneToOneField(Hold, on_delete=models.PROTECT, related_name="release")

    class Meta:
        constraints = [_reason_check("specimens_holdrelease_reason")]


class DisposalRequest(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    specimen = models.ForeignKey(Specimen, on_delete=models.PROTECT, related_name="disposals")
    method = models.CharField(max_length=160)
    destination = models.CharField(max_length=160)
    reason = models.TextField()
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    recorded_at = models.DateTimeField(default=timezone.now)
    specimen_revision = models.PositiveIntegerField()
    occupancy_revision = models.PositiveIntegerField()

    class Meta:
        ordering = ["recorded_at", "id"]
        constraints = [
            _reason_check("specimens_disposalrequest_reason"),
            _reason_check("specimens_disposalrequest_method", "method"),
            _reason_check("specimens_disposalrequest_destination", "destination"),
        ]
        indexes = [models.Index(fields=["specimen", "recorded_at"], name="specimens_disposal_req")]


class DisposalDecision(models.Model):
    class Decision(models.TextChoices):
        APPROVE = "approve", "Approved"
        REJECT = "reject", "Rejected"
        CANCEL = "cancel", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.OneToOneField(
        DisposalRequest, on_delete=models.PROTECT, related_name="decision"
    )
    decision = models.CharField(max_length=12, choices=Decision.choices)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    recorded_at = models.DateTimeField(default=timezone.now)
    reason = models.TextField()
    approved_revision = models.PositiveIntegerField(null=True, blank=True)
    approved_occupancy_revision = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(decision__in=["approve", "reject", "cancel"]),
                name="specimens_disposaldecision_kind",
            ),
            models.CheckConstraint(
                condition=Q(
                    decision="approve",
                    approved_revision__isnull=False,
                    approved_occupancy_revision__isnull=False,
                )
                | Q(
                    decision__in=["reject", "cancel"],
                    approved_revision__isnull=True,
                    approved_occupancy_revision__isnull=True,
                ),
                name="specimens_disposaldecision_snapshot",
            ),
            _reason_check("specimens_disposaldecision_reason"),
        ]


class DisposalExecution(EventMixin):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.OneToOneField(
        DisposalRequest, on_delete=models.PROTECT, related_name="execution"
    )
    decision = models.OneToOneField(
        DisposalDecision, on_delete=models.PROTECT, related_name="execution"
    )
    specimen = models.OneToOneField(Specimen, on_delete=models.PROTECT, related_name="disposal")
    custody_event = models.OneToOneField(
        CustodyEvent, on_delete=models.PROTECT, related_name="disposal_execution"
    )

    class Meta:
        constraints = [_reason_check("specimens_disposalexecution_reason")]


class Correction(models.Model):
    """A reasoned correction of allow-listed specimen metadata, or an annotation after disposal."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    specimen = models.ForeignKey(Specimen, on_delete=models.PROTECT, related_name="corrections")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    recorded_at = models.DateTimeField(default=timezone.now)
    reason = models.TextField()
    before = models.JSONField(default=dict)
    after = models.JSONField(default=dict)
    annotation = models.BooleanField(default=False)

    class Meta:
        ordering = ["recorded_at", "id"]
        constraints = [_reason_check("specimens_correction_reason")]
        indexes = [
            models.Index(fields=["specimen", "recorded_at"], name="specimens_correction_time")
        ]


class ImportBatch(models.Model):
    """An immutable validation result: normalized rows and errors; no raw file is kept."""

    class State(models.TextChoices):
        INVALID = "invalid", "Has errors"
        VALID = "valid", "Ready to commit"
        EXPIRED = "expired", "Expired"
        COMMITTED = "committed", "Committed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    recorded_at = models.DateTimeField(default=timezone.now)
    filename = models.CharField(max_length=160, blank=True)
    sha256 = models.CharField(max_length=64)
    schema_version = models.PositiveIntegerField(default=1)
    row_count = models.PositiveIntegerField(default=0)
    normalized_rows = models.JSONField(default=list)
    errors = models.JSONField(default=list)  # the first importing.MAX_ERRORS errors found
    # Every error found. Null only on batches validated before the cap, whose list is complete.
    error_count = models.PositiveIntegerField(null=True, blank=True)
    warnings = models.JSONField(default=list)
    expires_at = models.DateTimeField()

    class Meta:
        ordering = ["-recorded_at", "id"]
        verbose_name_plural = "import batches"
        indexes = [models.Index(fields=["sha256"], name="specimens_import_sha")]

    @property
    def name(self):
        return self.filename or f"Import {timezone.localtime(self.recorded_at):%Y-%m-%d %H:%M}"

    def state_at(self, now, *, error_total):
        if error_total or not self.row_count:
            return self.State.INVALID
        if hasattr(self, "commit"):
            return self.State.COMMITTED
        if self.expires_at < now:
            return self.State.EXPIRED
        return self.State.VALID

    @property
    def total_errors(self):
        return len(self.errors) if self.error_count is None else self.error_count

    @property
    def errors_truncated(self):
        return self.total_errors > len(self.errors)

    @property
    def is_valid(self):
        return not self.total_errors and self.row_count > 0


class ImportCommit(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    batch = models.OneToOneField(ImportBatch, on_delete=models.PROTECT, related_name="commit")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    recorded_at = models.DateTimeField(default=timezone.now)
    reason = models.TextField()
    specimen_ids = models.JSONField(default=list)

    class Meta:
        constraints = [_reason_check("specimens_importcommit_reason")]


class OutputRecord(models.Model):
    """What was printed or exported, by whom and when: identifiers and counts, never file bytes."""

    class Kind(models.TextChoices):
        LABELS = "labels", "Labels printed"
        EXPORT = "export", "Specimens exported"
        CATALOGUE_EXPORT = "catalogue_export", "Catalogue exported"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    recorded_at = models.DateTimeField(default=timezone.now)
    count = models.PositiveIntegerField()
    details = models.JSONField(default=dict)

    class Meta:
        ordering = ["-recorded_at", "id"]
