"""Storage equipment records: register, status, service events, LN2 log, temperature readings,
excursions and the specimens they touched.

Equipment and excursions are mutable records with an optimistic revision; every change goes
through `equipment.services` with an attributed audit event in the same transaction, and neither
is ever deleted (trigger). Status changes, service events, LN2 entries, temperature readings,
reading imports, excursion timeline entries and affected-specimen lists are append-only (UPDATE and
DELETE refused by trigger). A mistake is fixed by a correction row that `corrects` the earlier one
with a reason: either a full replacement or `voided` (withdrawn). Effective rows are those nobody
has corrected and that are not themselves a withdrawal.

Limits, refill levels and next-due dates are entered by the repository; nothing here supplies a
default or computes one from an interval (ISBER 5th ed. H12.1, D3.6.2.2, H3.1). Temperatures are
exact decimals in °C; levels and amounts are exact decimals with an explicit unit.
"""

import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

CODE_PATTERN = r"^[A-Z0-9][A-Z0-9._-]*$"
TEMPERATURE_UNIT = "°C"
ABSOLUTE_ZERO = Decimal("-273.15")
TEMPERATURE = {"max_digits": 6, "decimal_places": 2}
LEVEL = {"max_digits": 8, "decimal_places": 2}
AMOUNT = {"max_digits": 10, "decimal_places": 2}


def _actor(related_name="+", **kwargs):
    return models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name=related_name, **kwargs
    )


class LevelUnit(models.TextChoices):
    MM = "mm", "mm"
    CM = "cm", "cm"
    INCH = "in", "in"
    PERCENT = "%", "% of full"


class AmountUnit(models.TextChoices):
    LITRE = "L", "L"
    KILOGRAM = "kg", "kg"


# ------------------------------------------------------------------ attachments


class AttachmentMixin(models.Model):
    """An optional reference to a stored `core.Attachment`; the bytes are never copied here."""

    attachment = models.ForeignKey(
        "core.Attachment", on_delete=models.PROTECT, related_name="+", null=True, blank=True
    )

    class Meta:
        abstract = True

    @property
    def has_attachment(self):
        return self.attachment_id is not None


class Correctable(models.Model):
    """A correction row supersedes `corrects`; `voided` withdraws it with no replacement."""

    corrects = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="corrected_by"
    )
    voided = models.BooleanField(default=False)
    correction_reason = models.TextField(blank=True)

    class Meta:
        abstract = True

    @classmethod
    def correction_constraints(cls, prefix):
        return [
            models.CheckConstraint(
                condition=Q(corrects__isnull=True, correction_reason="", voided=False)
                | (Q(corrects__isnull=False) & ~Q(correction_reason="")),
                name=f"{prefix}_correction",
            ),
        ]


# ------------------------------------------------------------------ register


class Equipment(models.Model):
    """One piece of storage equipment, optionally the enclosure of a storage unit."""

    class Kind(models.TextChoices):
        LN2_TANK = "ln2_tank", "LN2 storage tank"
        FREEZER = "freezer", "Mechanical freezer or refrigerator"
        MONITOR = "monitor", "Temperature monitor or probe"
        SUPPLY_DEWAR = "supply_dewar", "LN2 supply dewar"
        OTHER = "other", "Other equipment"

    class Status(models.TextChoices):
        IN_SERVICE = "in_service", "In service"
        OUT_OF_SERVICE = "out_of_service", "Out of service"
        RETIRED = "retired", "Retired"

    LN2_KINDS = ("ln2_tank", "supply_dewar")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField("Asset ID", max_length=40, unique=True)
    name = models.CharField(max_length=160)
    kind = models.CharField(max_length=14, choices=Kind.choices)
    storage_unit = models.ForeignKey(
        "storage.StorageUnit",
        on_delete=models.PROTECT,
        related_name="equipment",
        null=True,
        blank=True,
    )
    location = models.CharField(max_length=160, blank=True)
    manufacturer = models.CharField(max_length=120, blank=True)
    model = models.CharField(max_length=120, blank=True)
    serial_number = models.CharField(max_length=120, blank=True)
    is_critical = models.BooleanField("Critical equipment", default=False)
    installed_on = models.DateField(null=True, blank=True)
    service_provider = models.CharField(max_length=160, blank=True)
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=14, choices=Status.choices, default=Status.IN_SERVICE)
    lower_limit = models.DecimalField(null=True, blank=True, **TEMPERATURE)
    upper_limit = models.DecimalField(null=True, blank=True, **TEMPERATURE)
    temperature_unit = models.CharField(max_length=2, default=TEMPERATURE_UNIT, editable=False)
    level_unit = models.CharField(max_length=2, choices=LevelUnit.choices, blank=True)
    level_low_limit = models.DecimalField(null=True, blank=True, **LEVEL)
    revision = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    created_by = _actor(editable=False)

    class Meta:
        ordering = ["code"]
        verbose_name_plural = "equipment"
        constraints = [
            models.CheckConstraint(
                condition=Q(code__regex=CODE_PATTERN), name="equipment_equipment_code_format"
            ),
            models.CheckConstraint(
                condition=Q(kind__in=["ln2_tank", "freezer", "monitor", "supply_dewar", "other"]),
                name="equipment_equipment_kind",
            ),
            models.CheckConstraint(
                condition=Q(status__in=["in_service", "out_of_service", "retired"]),
                name="equipment_equipment_status",
            ),
            models.CheckConstraint(
                condition=Q(temperature_unit=TEMPERATURE_UNIT),
                name="equipment_equipment_temperature_unit",
            ),
            models.CheckConstraint(
                condition=(Q(lower_limit__isnull=True) | Q(lower_limit__gte=ABSOLUTE_ZERO))
                & (Q(upper_limit__isnull=True) | Q(upper_limit__gte=ABSOLUTE_ZERO))
                & (
                    Q(lower_limit__isnull=True)
                    | Q(upper_limit__isnull=True)
                    | Q(lower_limit__lt=F("upper_limit"))
                ),
                name="equipment_equipment_limits",
            ),
            models.CheckConstraint(
                condition=Q(level_low_limit__isnull=True)
                | (Q(level_low_limit__gte=0) & ~Q(level_unit="")),
                name="equipment_equipment_level_limit",
            ),
            models.CheckConstraint(
                condition=Q(level_unit__in=["", "mm", "cm", "in", "%"]),
                name="equipment_equipment_level_unit",
            ),
            # One storage enclosure (tank or freezer) in use per storage unit.
            models.UniqueConstraint(
                fields=["storage_unit"],
                condition=Q(kind__in=["ln2_tank", "freezer"], storage_unit__isnull=False)
                & ~Q(status="retired"),
                name="equipment_one_enclosure_per_unit",
            ),
        ]
        indexes = [models.Index(fields=["kind", "status"], name="equipment_kind_status")]

    def __str__(self):
        return self.code

    @property
    def is_retired(self):
        return self.status == self.Status.RETIRED

    @property
    def keeps_ln2_log(self):
        return self.kind in self.LN2_KINDS

    @property
    def takes_readings(self):
        return self.kind != self.Kind.MONITOR


class StatusChange(models.Model):
    """Append-only: when the equipment's status changed, as stated, and why."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    equipment = models.ForeignKey(
        Equipment, on_delete=models.PROTECT, related_name="status_changes"
    )
    from_status = models.CharField(max_length=14, choices=Equipment.Status.choices)
    to_status = models.CharField(max_length=14, choices=Equipment.Status.choices)
    occurred_at = models.DateTimeField()
    recorded_at = models.DateTimeField(default=timezone.now)
    actor = _actor()
    reason = models.TextField()

    class Meta:
        ordering = ["occurred_at", "recorded_at", "id"]
        constraints = [
            models.CheckConstraint(condition=~Q(reason=""), name="equipment_status_reason"),
            models.CheckConstraint(
                condition=~Q(from_status=F("to_status")), name="equipment_status_changes"
            ),
            models.CheckConstraint(
                condition=Q(occurred_at__lte=F("recorded_at")), name="equipment_status_not_future"
            ),
        ]


# ------------------------------------------------------------------ service events


class ServiceEvent(AttachmentMixin, Correctable):
    """Maintenance, calibration, repair, mapping or qualification performed on one day.

    `next_due_on` is the repository's own date for the next one of this kind; never computed.
    """

    class Kind(models.TextChoices):
        MAINTENANCE = "maintenance", "Preventive maintenance"
        CALIBRATION = "calibration", "Calibration"
        REPAIR = "repair", "Repair or service call"
        MAPPING = "mapping", "Temperature mapping"
        QUALIFICATION = "qualification", "Qualification or verification"

    class Outcome(models.TextChoices):
        COMPLETED = "completed", "Completed"
        ACCEPTABLE = "acceptable", "Acceptable"
        NOT_ACCEPTABLE = "not_acceptable", "Not acceptable"

    DUE_KINDS = ("maintenance", "calibration", "mapping", "qualification")
    OUTCOMES = {
        "maintenance": ("completed", "not_acceptable"),
        "repair": ("completed", "not_acceptable"),
        "calibration": ("acceptable", "not_acceptable"),
        "mapping": ("acceptable", "not_acceptable"),
        "qualification": ("acceptable", "not_acceptable"),
    }

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    equipment = models.ForeignKey(
        Equipment, on_delete=models.PROTECT, related_name="service_events"
    )
    kind = models.CharField(max_length=14, choices=Kind.choices)
    performed_on = models.DateField()
    performed_by = _actor(null=True, blank=True)
    performed_by_external = models.CharField(
        "Performed by (outside provider)", max_length=160, blank=True
    )
    outcome = models.CharField(max_length=14, choices=Outcome.choices)
    reference = models.CharField(
        max_length=160, blank=True, help_text="Certificate, work order or procedure reference."
    )
    description = models.TextField(blank=True)
    reference_device = models.CharField(
        max_length=160, blank=True, help_text="Reference instrument name and serial number."
    )
    as_found = models.CharField(max_length=200, blank=True)
    as_left = models.CharField(max_length=200, blank=True)
    next_due_on = models.DateField(null=True, blank=True)
    recorded_by = _actor()
    recorded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-performed_on", "-recorded_at", "-id"]
        indexes = [
            models.Index(fields=["equipment", "kind", "-performed_on"], name="equipment_service")
        ]
        constraints = [
            *Correctable.correction_constraints("equipment_service"),
            models.CheckConstraint(
                condition=Q(
                    kind__in=["maintenance", "calibration", "repair", "mapping", "qualification"]
                ),
                name="equipment_service_kind",
            ),
            models.CheckConstraint(
                condition=Q(
                    kind__in=["maintenance", "repair"], outcome__in=["completed", "not_acceptable"]
                )
                | Q(
                    kind__in=["calibration", "mapping", "qualification"],
                    outcome__in=["acceptable", "not_acceptable"],
                ),
                name="equipment_service_outcome",
            ),
            models.CheckConstraint(
                condition=Q(performed_by__isnull=False) | ~Q(performed_by_external=""),
                name="equipment_service_performer",
            ),
            models.CheckConstraint(
                condition=Q(next_due_on__isnull=True) | Q(next_due_on__gte=F("performed_on")),
                name="equipment_service_due_after",
            ),
            models.CheckConstraint(
                condition=Q(next_due_on__isnull=True) | ~Q(kind="repair"),
                name="equipment_service_due_kinds",
            ),
        ]

    @property
    def performer(self):
        if self.performed_by_id:
            user = self.performed_by
            return user.get_full_name() or user.get_username()
        return self.performed_by_external


# ------------------------------------------------------------------ LN2 log


class LN2Entry(Correctable):
    """A level check or a fill of one LN2 vessel. Consumption is derived from the readings."""

    class Kind(models.TextChoices):
        LEVEL = "level", "Level check"
        FILL = "fill", "Fill"

    class Method(models.TextChoices):
        DIPSTICK = "dipstick", "Dipstick"
        SENSOR = "sensor", "Level sensor or display"
        OTHER = "other", "Other"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    equipment = models.ForeignKey(Equipment, on_delete=models.PROTECT, related_name="ln2_entries")
    kind = models.CharField(max_length=6, choices=Kind.choices)
    occurred_at = models.DateTimeField()
    recorded_at = models.DateTimeField(default=timezone.now)
    recorded_by = _actor()
    method = models.CharField(max_length=10, choices=Method.choices, blank=True)
    level_before = models.DecimalField(null=True, blank=True, **LEVEL)
    level = models.DecimalField(null=True, blank=True, **LEVEL)
    level_unit = models.CharField(max_length=2, choices=LevelUnit.choices, blank=True)
    amount_added = models.DecimalField(null=True, blank=True, **AMOUNT)
    amount_unit = models.CharField(max_length=2, choices=AmountUnit.choices, blank=True)
    supply = models.ForeignKey(
        Equipment, on_delete=models.PROTECT, related_name="supplied_fills", null=True, blank=True
    )
    reference = models.CharField(
        max_length=160, blank=True, help_text="Delivery note or supplier reference."
    )
    performed_by_external = models.CharField(
        "Filled by (outside provider)", max_length=160, blank=True
    )
    note = models.TextField(blank=True)

    class Meta:
        ordering = ["occurred_at", "recorded_at", "id"]
        verbose_name = "LN2 log entry"
        verbose_name_plural = "LN2 log entries"
        indexes = [models.Index(fields=["equipment", "occurred_at"], name="equipment_ln2_time")]
        constraints = [
            *Correctable.correction_constraints("equipment_ln2"),
            models.CheckConstraint(
                condition=Q(kind__in=["level", "fill"]), name="equipment_ln2_kind"
            ),
            models.CheckConstraint(
                condition=Q(occurred_at__lte=F("recorded_at")), name="equipment_ln2_not_future"
            ),
            models.CheckConstraint(
                condition=(Q(level__isnull=True) | Q(level__gte=0))
                & (Q(level_before__isnull=True) | Q(level_before__gte=0))
                & (Q(amount_added__isnull=True) | Q(amount_added__gt=0)),
                name="equipment_ln2_values",
            ),
            models.CheckConstraint(
                condition=~Q(level_unit="%")
                | (
                    (Q(level__isnull=True) | Q(level__lte=100))
                    & (Q(level_before__isnull=True) | Q(level_before__lte=100))
                ),
                name="equipment_ln2_percent",
            ),
            models.CheckConstraint(
                condition=(Q(level__isnull=True, level_before__isnull=True) & Q(level_unit=""))
                | (
                    (Q(level__isnull=False) | Q(level_before__isnull=False))
                    & Q(level_unit__in=["mm", "cm", "in", "%"])
                ),
                name="equipment_ln2_level_unit",
            ),
            models.CheckConstraint(
                condition=Q(amount_added__isnull=True, amount_unit="")
                | Q(amount_added__isnull=False, amount_unit__in=["L", "kg"]),
                name="equipment_ln2_amount_unit",
            ),
            models.CheckConstraint(
                condition=Q(
                    kind="level",
                    level__isnull=False,
                    level_before__isnull=True,
                    amount_added__isnull=True,
                    supply__isnull=True,
                )
                | (Q(kind="fill") & (Q(level__isnull=False) | Q(amount_added__isnull=False))),
                name="equipment_ln2_shape",
            ),
        ]


# ------------------------------------------------------------------ temperature readings


class ReadingImport(models.Model):
    """An immutable validation result for a readings CSV: normalized rows and errors only."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor = _actor()
    recorded_at = models.DateTimeField(default=timezone.now)
    filename = models.CharField(max_length=160, blank=True)
    sha256 = models.CharField(max_length=64)
    equipment = models.ForeignKey(
        Equipment, on_delete=models.PROTECT, related_name="+", null=True, blank=True
    )
    row_count = models.PositiveIntegerField(default=0)
    normalized_rows = models.JSONField(default=list)
    errors = models.JSONField(default=list)
    warnings = models.JSONField(default=list)
    expires_at = models.DateTimeField()

    class Meta:
        ordering = ["-recorded_at", "id"]
        indexes = [models.Index(fields=["sha256"], name="equipment_import_sha")]

    @property
    def is_valid(self):
        return not self.errors and self.row_count > 0


class ReadingImportCommit(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    batch = models.OneToOneField(ReadingImport, on_delete=models.PROTECT, related_name="commit")
    actor = _actor()
    recorded_at = models.DateTimeField(default=timezone.now)
    reason = models.TextField()
    reading_count = models.PositiveIntegerField()

    class Meta:
        constraints = [
            models.CheckConstraint(condition=~Q(reason=""), name="equipment_importcommit_reason")
        ]


class TemperatureReading(Correctable):
    """One temperature as read or logged. The limits in force when it was recorded are kept.

    One effective reading per equipment, time and probe position: checked by the services while
    holding the equipment row lock (a database constraint on originals would wrongly block a slot
    that a correction moved away from)."""

    class Assessment(models.TextChoices):
        ABOVE = "above", "Above upper limit"
        BELOW = "below", "Below lower limit"
        WITHIN = "within", "Within recorded limits"
        NONE = "none", "No limits recorded"

        @property
        def is_outside(self):
            return self in (self.ABOVE, self.BELOW)

    class Source(models.TextChoices):
        MANUAL = "manual", "Entered"
        IMPORT = "import", "Imported"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    equipment = models.ForeignKey(Equipment, on_delete=models.PROTECT, related_name="readings")
    measured_at = models.DateTimeField()
    recorded_at = models.DateTimeField(default=timezone.now)
    recorded_by = _actor()
    value = models.DecimalField(**TEMPERATURE)
    unit = models.CharField(max_length=2, default=TEMPERATURE_UNIT, editable=False)
    probe_position = models.CharField(
        max_length=80, blank=True, help_text="Where it was read, as stated: top of rack, display."
    )
    device = models.ForeignKey(
        Equipment, on_delete=models.PROTECT, related_name="device_readings", null=True, blank=True
    )
    source = models.CharField(max_length=6, choices=Source.choices, default=Source.MANUAL)
    batch = models.ForeignKey(
        ReadingImport, on_delete=models.PROTECT, related_name="readings", null=True, blank=True
    )
    note = models.TextField(blank=True)
    lower_limit = models.DecimalField(null=True, blank=True, **TEMPERATURE)
    upper_limit = models.DecimalField(null=True, blank=True, **TEMPERATURE)

    class Meta:
        ordering = ["measured_at", "recorded_at", "id"]
        indexes = [models.Index(fields=["equipment", "measured_at"], name="equipment_reading_time")]
        constraints = [
            *Correctable.correction_constraints("equipment_reading"),
            models.CheckConstraint(
                condition=Q(unit=TEMPERATURE_UNIT) & Q(value__gte=ABSOLUTE_ZERO),
                name="equipment_reading_value",
            ),
            models.CheckConstraint(
                condition=Q(measured_at__lte=F("recorded_at")),
                name="equipment_reading_not_future",
            ),
            models.CheckConstraint(
                condition=Q(source="manual", batch__isnull=True)
                | Q(source="import", batch__isnull=False),
                name="equipment_reading_source",
            ),
        ]

    @property
    def limit_assessment(self):
        if self.upper_limit is None and self.lower_limit is None:
            return self.Assessment.NONE
        if self.upper_limit is not None and self.value > self.upper_limit:
            return self.Assessment.ABOVE
        if self.lower_limit is not None and self.value < self.lower_limit:
            return self.Assessment.BELOW
        return self.Assessment.WITHIN

    @property
    def outside_limits(self):
        """Direction of an actual breach, or an empty string for the other readings."""
        assessment = self.limit_assessment
        return assessment.value if assessment.is_outside else ""


# ------------------------------------------------------------------ excursions


class Excursion(models.Model):
    """A period when a unit's temperature was outside its limits, from start to close.

    Open while it is recorded and investigated, in review once submitted, closed by a second
    person (a manager or administrator who did not submit it). Closed records reopen with a reason.
    """

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        REVIEW = "review", "Awaiting review"
        CLOSED = "closed", "Closed"

    class Detection(models.TextChoices):
        ALARM = "alarm", "Alarm"
        READING = "reading", "Temperature reading"
        INSPECTION = "inspection", "Inspection or level check"
        OTHER = "other", "Other"

    class Cause(models.TextChoices):
        POWER = "power", "Power failure"
        LN2_SUPPLY = "ln2_supply", "LN2 supply or fill"
        EQUIPMENT = "equipment", "Equipment fault"
        DOOR = "door", "Door or lid left open"
        SENSOR = "sensor", "Sensor or probe fault"
        HANDLING = "handling", "Handling or access"
        UNKNOWN = "unknown", "Not yet known"
        OTHER = "other", "Other"

    equipment = models.ForeignKey(Equipment, on_delete=models.PROTECT, related_name="excursions")
    storage_unit = models.ForeignKey(
        "storage.StorageUnit",
        on_delete=models.PROTECT,
        related_name="+",
        null=True,
        blank=True,
    )
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    extreme_value = models.DecimalField(null=True, blank=True, **TEMPERATURE)
    extreme_at = models.DateTimeField(null=True, blank=True)
    unit = models.CharField(max_length=2, default=TEMPERATURE_UNIT, editable=False)
    lower_limit = models.DecimalField(null=True, blank=True, **TEMPERATURE)
    upper_limit = models.DecimalField(null=True, blank=True, **TEMPERATURE)
    detection = models.CharField(max_length=10, choices=Detection.choices)
    cause_category = models.CharField(max_length=10, choices=Cause.choices, default=Cause.UNKNOWN)
    cause = models.TextField(blank=True)
    action = models.TextField("Action taken", blank=True)
    impact = models.TextField("Impact assessment", blank=True)
    status = models.CharField(max_length=6, choices=Status.choices, default=Status.OPEN)
    opened_by = _actor()
    opened_at = models.DateTimeField(default=timezone.now)
    submitted_by = _actor(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    closed_by = _actor(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    closure_note = models.TextField(blank=True)
    revision = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["-started_at", "-id"]
        indexes = [
            models.Index(fields=["equipment", "started_at"], name="equipment_excursion_time"),
            models.Index(fields=["status"], name="equipment_excursion_status"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(unit=TEMPERATURE_UNIT)
                & (Q(extreme_value__isnull=True) | Q(extreme_value__gte=ABSOLUTE_ZERO)),
                name="equipment_excursion_unit",
            ),
            models.CheckConstraint(
                condition=Q(ended_at__isnull=True) | Q(ended_at__gte=F("started_at")),
                name="equipment_excursion_period",
            ),
            models.CheckConstraint(
                condition=Q(extreme_at__isnull=True) | Q(extreme_at__gte=F("started_at")),
                name="equipment_excursion_extreme_time",
            ),
            models.CheckConstraint(
                condition=Q(status="open")
                | (
                    Q(
                        ended_at__isnull=False,
                        submitted_by__isnull=False,
                        submitted_at__isnull=False,
                    )
                    & ~Q(cause="")
                    & ~Q(action="")
                    & ~Q(impact="")
                ),
                name="equipment_excursion_submitted",
            ),
            models.CheckConstraint(
                condition=~Q(status="closed")
                | (
                    Q(closed_by__isnull=False, closed_at__isnull=False)
                    & ~Q(closed_by=F("submitted_by"))
                ),
                name="equipment_excursion_closed",
            ),
        ]

    @property
    def number(self):
        return f"EX-{self.pk:06d}" if self.pk else "EX-new"

    def __str__(self):
        return self.number


class ExcursionEvent(AttachmentMixin):
    """Timeline entry. `changes` keeps {field: [before, after]} for edited details."""

    class Kind(models.TextChoices):
        OPENED = "opened", "Opened"
        UPDATED = "updated", "Details updated"
        SUBMITTED = "submitted", "Submitted for review"
        RETURNED = "returned", "Returned for more work"
        CLOSED = "closed", "Reviewed and closed"
        REOPENED = "reopened", "Reopened"
        NOTE = "note", "Note or attachment"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    excursion = models.ForeignKey(Excursion, on_delete=models.PROTECT, related_name="events")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    note = models.TextField(blank=True)
    changes = models.JSONField(default=dict, blank=True)
    actor = _actor()
    occurred_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["occurred_at", "id"]


class AffectedSpecimen(models.Model):
    """Append-only: a specimen that was in the affected unit during the excursion, as computed
    from the recorded movements when the excursion was closed (see equipment.occupancy)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    excursion = models.ForeignKey(
        Excursion, on_delete=models.PROTECT, related_name="affected_specimens"
    )
    specimen = models.ForeignKey("specimens.Specimen", on_delete=models.PROTECT, related_name="+")
    present_from = models.DateTimeField()
    present_until = models.DateTimeField()
    location = models.CharField(max_length=300, blank=True)
    # The closing timeline entry that captured this list; a reopened and re-closed excursion
    # gets a new list under its new closing entry, and the earlier list stays as it was.
    capture = models.ForeignKey(ExcursionEvent, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["location", "id"]
        constraints = [
            models.UniqueConstraint(fields=["capture", "specimen"], name="equipment_affected_once"),
            models.CheckConstraint(
                condition=Q(present_until__gte=F("present_from")),
                name="equipment_affected_period",
            ),
        ]
