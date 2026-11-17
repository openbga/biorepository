"""Distribution: studies and collection governance, consent records and withdrawal, specimen
requests with approval, allocation and pick lists, shipments and manifests, receipt confirmation,
returns, and agreements (MTA/DUA).

Projections (studies, catalogues, consents, requests, allocations, shipments, agreements) are
mutable rows with an optimistic `revision`, changed only by `distribution.services` under the
StorageGate and their row lock; DELETE is refused by trigger (draft request lines excepted).
Event, snapshot and decision tables are append-only: UPDATE and DELETE are refused by trigger
(migration 0002), and a mistake is fixed by a correction row with a reason.

The software records what people decided and did and checks recorded facts against each other at
each step. It does not decide consent, approve on a committee's behalf, classify a shipment or
claim compliance. No donor identity is stored: a participant is the coded
`specimens.Participant`; a consent record holds scope and status, never names, signatures or
scanned consent forms.
"""

import uuid

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from specimens.models import CODE_PATTERN, NO_IDENTIFYING_TEXT, QUANTITY_DIGITS, QUANTITY_PLACES

NO_PARTICIPANT_TEXT = (
    "Do not enter names, signatures, record numbers, birth dates or contact details of a "
    "participant."
)
UPLOAD_TEXT = (
    "Do not upload consent forms, signatures or any document that identifies a participant."
)

__all__ = ["NO_IDENTIFYING_TEXT"]


def _actor(related_name="+", **kwargs):
    return models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name=related_name, **kwargs
    )


def _quantity(**kwargs):
    return models.DecimalField(max_digits=QUANTITY_DIGITS, decimal_places=QUANTITY_PLACES, **kwargs)


def _choices(field, values, name):
    return models.CheckConstraint(condition=Q(**{f"{field}__in": list(values)}), name=name)


def _reason(name, field="reason"):
    return models.CheckConstraint(condition=~Q(**{field: ""}), name=name)


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


class Catalogue(models.Model):
    code = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=160)
    is_active = models.BooleanField(default=True)
    revision = models.PositiveIntegerField(default=1)
    created_by = _actor(editable=False)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        abstract = True
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} · {self.name}"


# ------------------------------------------------------------------ governance and consent


class Study(models.Model):
    class Basis(models.TextChoices):
        RECORDS = "records", "Consent records"
        DETERMINATION = "determination", "Study-level determination"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        SUSPENDED = "suspended", "Suspended"
        CLOSED = "closed", "Closed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=40, unique=True)
    title = models.CharField(max_length=160)
    description = models.TextField(blank=True)
    principal_investigator = models.CharField(max_length=160, blank=True)
    ethics_reference = models.CharField(max_length=80, blank=True)
    ethics_approved_until = models.DateField(null=True, blank=True)
    consent_basis = models.CharField(max_length=13, choices=Basis.choices, default=Basis.RECORDS)
    determination_reference = models.CharField(max_length=160, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ACTIVE)
    revision = models.PositiveIntegerField(default=1)
    created_by = _actor(editable=False)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ["code"]
        verbose_name_plural = "studies"
        constraints = [
            models.CheckConstraint(condition=Q(code__regex=CODE_PATTERN), name="dist_study_code"),
            _choices("consent_basis", ["records", "determination"], "dist_study_basis"),
            _choices("status", ["active", "suspended", "closed"], "dist_study_status"),
            models.CheckConstraint(
                condition=Q(consent_basis="records") | ~Q(determination_reference=""),
                name="dist_study_determination",
            ),
        ]

    def __str__(self):
        return self.code


class StudyCollection(models.Model):
    """A collection governed under a study. One current link per collection."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    study = models.ForeignKey(Study, on_delete=models.PROTECT, related_name="collection_links")
    collection = models.ForeignKey(
        "specimens.Collection", on_delete=models.PROTECT, related_name="+"
    )
    linked_by = _actor()
    linked_at = models.DateTimeField(default=timezone.now)
    link_reason = models.TextField()
    ended_by = _actor(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    end_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["linked_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["collection"],
                condition=Q(ended_at__isnull=True),
                name="dist_collection_one_current_study",
            ),
            models.CheckConstraint(
                condition=Q(ended_at__isnull=True, ended_by__isnull=True, end_reason="")
                | (Q(ended_at__isnull=False, ended_by__isnull=False) & ~Q(end_reason="")),
                name="dist_studycollection_end",
            ),
            models.CheckConstraint(
                condition=Q(ended_at__isnull=True) | Q(ended_at__gte=F("linked_at")),
                name="dist_studycollection_period",
            ),
            _reason("dist_studycollection_reason", "link_reason"),
        ]


class ConsentScopeCategory(Catalogue):
    """A use a consent form asks about (data, not code). `gates_use` categories can be required
    by a request; others (e.g. re-contact) are recorded and shown only."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    description = models.TextField(blank=True)
    gates_use = models.BooleanField("Governs use", default=True)

    class Meta(Catalogue.Meta):
        verbose_name = "scope category"
        verbose_name_plural = "scope categories"
        constraints = [
            models.CheckConstraint(condition=Q(code__regex=CODE_PATTERN), name="dist_category_code")
        ]


class ConsentForm(models.Model):
    class Kind(models.TextChoices):
        STUDY = "study", "Study consent"
        BROAD = "broad", "Broad consent"
        AUTHORIZATION = "authorization", "Authorization"
        OTHER = "other", "Other"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    study = models.ForeignKey(Study, on_delete=models.PROTECT, related_name="consent_forms")
    code = models.CharField(max_length=40)
    version = models.CharField(max_length=20)
    title = models.CharField(max_length=160)
    kind = models.CharField(max_length=13, choices=Kind.choices, default=Kind.STUDY)
    categories = models.ManyToManyField(ConsentScopeCategory, related_name="forms")
    is_active = models.BooleanField(default=True)
    revision = models.PositiveIntegerField(default=1)
    created_by = _actor(editable=False)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ["study__code", "code", "version"]
        constraints = [
            models.UniqueConstraint(
                fields=["study", "code", "version"], name="dist_consentform_version"
            ),
            models.CheckConstraint(
                condition=Q(code__regex=CODE_PATTERN), name="dist_consentform_code"
            ),
            models.CheckConstraint(condition=~Q(version=""), name="dist_consentform_version_set"),
            _choices("kind", ["study", "broad", "authorization", "other"], "dist_consentform_kind"),
        ]

    def __str__(self):
        return f"{self.code} v{self.version}"


class Consent(models.Model):
    """Projection: the consent status of one coded participant in one study."""

    class Status(models.TextChoices):
        CONSENTED = "consented", "Consented"
        REFUSED = "refused", "Refused"
        WITHDRAWN = "withdrawn", "Withdrawn"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    participant = models.ForeignKey(
        "specimens.Participant", on_delete=models.PROTECT, related_name="+"
    )
    study = models.ForeignKey(Study, on_delete=models.PROTECT, related_name="consents")
    status = models.CharField(max_length=12, choices=Status.choices)
    current_record = models.OneToOneField(
        "ConsentRecord", on_delete=models.PROTECT, related_name="+", null=True, blank=True
    )
    no_contact = models.BooleanField("Do not contact", default=False)
    revision = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ["participant__code", "study__code"]
        constraints = [
            models.UniqueConstraint(
                fields=["participant", "study"], name="dist_consent_participant_study"
            ),
            _choices("status", ["consented", "refused", "withdrawn"], "dist_consent_status"),
        ]

    def __str__(self):
        return f"{self.participant.code} · {self.study.code}"


class ConsentRecord(models.Model):
    """Append-only: what a participant's consent says, as recorded from the filed original."""

    class Decision(models.TextChoices):
        CONSENTED = "consented", "Consented"
        REFUSED = "refused", "Refused"

    class Signer(models.TextChoices):
        PARTICIPANT = "participant", "Participant"
        REPRESENTATIVE = "representative", "Legally authorized representative"
        NOT_RECORDED = "not_recorded", "Not recorded"

    class Documentation(models.TextChoices):
        SIGNED = "signed", "Signed on paper"
        ELECTRONIC = "electronic", "Electronic"
        WAIVED = "waived", "Documentation waived"
        NOT_RECORDED = "not_recorded", "Not recorded"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    consent = models.ForeignKey(Consent, on_delete=models.PROTECT, related_name="records")
    form = models.ForeignKey(ConsentForm, on_delete=models.PROTECT, related_name="records")
    decision = models.CharField(max_length=12, choices=Decision.choices)
    consented_on = models.DateField("Date of the decision")
    signer = models.CharField(max_length=16, choices=Signer.choices)
    documentation = models.CharField(max_length=16, choices=Documentation.choices)
    source_reference = models.CharField(
        "Where the original is filed", max_length=80, blank=True, help_text=NO_PARTICIPANT_TEXT
    )
    restrictions = models.TextField(blank=True, help_text=NO_PARTICIPANT_TEXT)
    expires_on = models.DateField(null=True, blank=True)
    corrects = models.OneToOneField(
        "self", on_delete=models.PROTECT, related_name="corrected_by", null=True, blank=True
    )
    recorded_by = _actor()
    recorded_at = models.DateTimeField(default=timezone.now)
    reason = models.TextField()

    class Meta:
        ordering = ["recorded_at", "id"]
        constraints = [
            _choices("decision", ["consented", "refused"], "dist_consentrecord_decision"),
            _choices(
                "signer",
                ["participant", "representative", "not_recorded"],
                "dist_consentrecord_signer",
            ),
            _choices(
                "documentation",
                ["signed", "electronic", "waived", "not_recorded"],
                "dist_consentrecord_documentation",
            ),
            models.CheckConstraint(
                condition=Q(expires_on__isnull=True) | Q(expires_on__gte=F("consented_on")),
                name="dist_consentrecord_expiry",
            ),
            _reason("dist_consentrecord_reason"),
        ]


class ConsentAnswer(models.Model):
    class Answer(models.TextChoices):
        PERMITTED = "permitted", "Permitted"
        REFUSED = "refused", "Refused"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    record = models.ForeignKey(ConsentRecord, on_delete=models.PROTECT, related_name="answers")
    category = models.ForeignKey(ConsentScopeCategory, on_delete=models.PROTECT, related_name="+")
    answer = models.CharField(max_length=10, choices=Answer.choices)

    class Meta:
        ordering = ["category__code"]
        constraints = [
            models.UniqueConstraint(fields=["record", "category"], name="dist_answer_once"),
            _choices("answer", ["permitted", "refused"], "dist_answer_value"),
        ]


class ConsentWithdrawal(models.Model):
    class Kind(models.TextChoices):
        NO_CONTACT = "no_contact", "No further contact"
        NO_USE = "no_use", "No further use"
        DESTROY = "destroy", "No further use and destroy"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    consent = models.ForeignKey(Consent, on_delete=models.PROTECT, related_name="withdrawals")
    kind = models.CharField(max_length=12, choices=Kind.choices)
    effective_on = models.DateField()
    received_via = models.CharField(max_length=80, help_text=NO_PARTICIPANT_TEXT)
    held_count = models.PositiveIntegerField(default=0)
    released_count = models.PositiveIntegerField(default=0)
    distributed_count = models.PositiveIntegerField(default=0)
    actor = _actor()
    occurred_at = models.DateTimeField()
    recorded_at = models.DateTimeField(default=timezone.now)
    reason = models.TextField()

    class Meta:
        ordering = ["recorded_at", "id"]
        constraints = [
            _choices("kind", ["no_contact", "no_use", "destroy"], "dist_withdrawal_kind"),
            _reason("dist_withdrawal_reason"),
        ]

    @property
    def effective_kind(self):
        return self.Kind.DESTROY if hasattr(self, "escalation") else self.kind

    def get_effective_kind_display(self):
        return self.Kind(self.effective_kind).label

    @property
    def stops_use(self):
        return self.kind in (self.Kind.NO_USE, self.Kind.DESTROY)


class ConsentWithdrawalEscalation(models.Model):
    """A retained instruction to destroy after an existing no-further-use withdrawal."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    withdrawal = models.OneToOneField(
        ConsentWithdrawal, on_delete=models.PROTECT, related_name="escalation"
    )
    effective_on = models.DateField()
    received_via = models.CharField(max_length=80, help_text=NO_PARTICIPANT_TEXT)
    actor = _actor()
    occurred_at = models.DateTimeField()
    recorded_at = models.DateTimeField(default=timezone.now)
    reason = models.TextField()

    class Meta:
        ordering = ["recorded_at", "id"]
        constraints = [_reason("dist_escalation_reason")]


class ConsentHold(models.Model):
    """A specimens `use` hold placed because of a withdrawal."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    withdrawal = models.ForeignKey(
        ConsentWithdrawal, on_delete=models.PROTECT, related_name="holds"
    )
    specimen = models.ForeignKey("specimens.Specimen", on_delete=models.PROTECT, related_name="+")
    hold = models.OneToOneField("specimens.Hold", on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["specimen__accession"]
        constraints = [
            models.UniqueConstraint(fields=["withdrawal", "specimen"], name="dist_consenthold_once")
        ]


class ConsentReinstatement(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    consent = models.ForeignKey(Consent, on_delete=models.PROTECT, related_name="reinstatements")
    withdrawal = models.OneToOneField(
        ConsentWithdrawal, on_delete=models.PROTECT, related_name="reinstatement"
    )
    record = models.ForeignKey(
        ConsentRecord, on_delete=models.PROTECT, related_name="+", null=True, blank=True
    )
    actor = _actor()
    occurred_at = models.DateTimeField()
    recorded_at = models.DateTimeField(default=timezone.now)
    reason = models.TextField()

    class Meta:
        ordering = ["recorded_at", "id"]
        constraints = [_reason("dist_reinstatement_reason")]


class WithdrawalFollowUp(AttachmentMixin):
    class Action(models.TextChoices):
        RECIPIENT_NOTIFIED = "recipient_notified", "Recipient notified"
        DESTRUCTION_CONFIRMED = "destruction_confirmed", "Destruction confirmed by recipient"
        RETURNED = "returned", "Material returned"
        RECIPIENT_UNABLE = "recipient_unable", "Recipient unable to act"
        NOTE = "note", "Note"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    withdrawal = models.ForeignKey(
        ConsentWithdrawal, on_delete=models.PROTECT, related_name="follow_ups"
    )
    shipment = models.ForeignKey("Shipment", on_delete=models.PROTECT, related_name="+")
    action = models.CharField(max_length=24, choices=Action.choices)
    note = models.TextField(blank=True)
    actor = _actor()
    occurred_at = models.DateTimeField()
    recorded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["occurred_at", "recorded_at", "id"]
        constraints = [
            _choices(
                "action",
                [
                    "recipient_notified",
                    "destruction_confirmed",
                    "returned",
                    "recipient_unable",
                    "note",
                ],
                "dist_followup_action",
            )
        ]


# ------------------------------------------------------------------ parties and requests


class Institution(Catalogue):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    address = models.TextField(
        "Shipping address", blank=True, help_text="The organization's address for shipments."
    )
    country = models.CharField(max_length=2, blank=True, help_text="ISO 3166 two-letter code.")

    class Meta(Catalogue.Meta):
        constraints = [
            models.CheckConstraint(
                condition=Q(code__regex=CODE_PATTERN), name="dist_institution_code"
            ),
            models.CheckConstraint(
                condition=Q(country="") | Q(country__regex=r"^[A-Z]{2}$"),
                name="dist_institution_country",
            ),
        ]


class Investigator(Catalogue):
    """A requesting investigator: a professional contact, not a user account."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    institution = models.ForeignKey(
        Institution, on_delete=models.PROTECT, related_name="investigators"
    )
    email = models.EmailField(max_length=254, blank=True)
    phone = models.CharField(max_length=40, blank=True)

    class Meta(Catalogue.Meta):
        constraints = [
            models.CheckConstraint(
                condition=Q(code__regex=CODE_PATTERN), name="dist_investigator_code"
            )
        ]


class SpecimenRequest(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SUBMITTED = "submitted", "Awaiting decision"
        RETURNED = "returned", "Returned for changes"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        CANCELLED = "cancelled", "Cancelled"
        CLOSED = "closed", "Closed"

    class ReceivedVia(models.TextChoices):
        EMAIL = "email", "Email"
        LETTER = "letter", "Letter"
        FORM = "form", "Request form"
        MEETING = "meeting", "Meeting"
        OTHER = "other", "Other"

    EDITABLE = ("draft", "returned")
    FINAL = ("rejected", "cancelled", "closed")

    title = models.CharField(max_length=160)
    investigator = models.ForeignKey(Investigator, on_delete=models.PROTECT, related_name="+")
    institution = models.ForeignKey(Institution, on_delete=models.PROTECT, related_name="+")
    purpose = models.TextField(help_text=NO_PARTICIPANT_TEXT)
    ethics_reference = models.CharField(max_length=80, blank=True)
    ethics_approved_until = models.DateField(null=True, blank=True)
    use_categories = models.ManyToManyField(ConsentScopeCategory, related_name="+", blank=True)
    studies = models.ManyToManyField(Study, related_name="+", blank=True)
    data_requested = models.TextField(blank=True, help_text=NO_PARTICIPANT_TEXT)
    needed_by = models.DateField(null=True, blank=True)
    received_via = models.CharField(max_length=16, choices=ReceivedVia.choices)
    agreement = models.ForeignKey(
        "Agreement", on_delete=models.PROTECT, related_name="requests", null=True, blank=True
    )
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    latest_submission = models.PositiveIntegerField(default=0)
    revision = models.PositiveIntegerField(default=1)
    created_by = _actor(editable=False)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ["-id"]
        verbose_name = "specimen request"
        constraints = [
            _choices(
                "status",
                ["draft", "submitted", "returned", "approved", "rejected", "cancelled", "closed"],
                "dist_request_status",
            ),
            _choices(
                "received_via",
                ["email", "letter", "form", "meeting", "other"],
                "dist_request_received_via",
            ),
        ]
        indexes = [models.Index(fields=["status"], name="dist_request_status_idx")]

    @property
    def number(self):
        return f"RQ-{self.pk:06d}" if self.pk else "RQ-new"

    def __str__(self):
        return self.number

    @property
    def is_editable(self):
        return self.status in self.EDITABLE


class RequestItem(models.Model):
    """Draft line of a request: what kind of material and how much. Editable while the request
    is a draft or returned and no allocation refers to it."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey(SpecimenRequest, on_delete=models.PROTECT, related_name="items")
    line = models.PositiveIntegerField()
    specimen_type = models.ForeignKey(
        "specimens.SpecimenType", on_delete=models.PROTECT, related_name="+"
    )
    count = models.PositiveIntegerField()
    minimum_quantity = _quantity(null=True, blank=True)
    unit = models.ForeignKey(
        "specimens.QuantityUnit", on_delete=models.PROTECT, related_name="+", null=True, blank=True
    )
    study = models.ForeignKey(
        Study, on_delete=models.PROTECT, related_name="+", null=True, blank=True
    )
    notes = models.CharField(max_length=500, blank=True, help_text=NO_PARTICIPANT_TEXT)

    class Meta:
        ordering = ["request", "line"]
        constraints = [
            models.UniqueConstraint(fields=["request", "line"], name="dist_item_line"),
            models.CheckConstraint(
                condition=Q(count__gte=1, count__lte=500), name="dist_item_count"
            ),
            models.CheckConstraint(
                condition=Q(minimum_quantity__isnull=True, unit__isnull=True)
                | Q(minimum_quantity__gt=0, unit__isnull=False),
                name="dist_item_quantity",
            ),
        ]


class RequestSubmission(models.Model):
    """Append-only snapshot of the request as submitted for a decision."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey(
        SpecimenRequest, on_delete=models.PROTECT, related_name="submissions"
    )
    number = models.PositiveIntegerField()
    data = models.JSONField()
    submitted_by = _actor()
    submitted_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["request", "number"]
        constraints = [
            models.UniqueConstraint(fields=["request", "number"], name="dist_submission_number")
        ]


class RequestDecision(models.Model):
    class Decision(models.TextChoices):
        APPROVE = "approve", "Approved"
        REJECT = "reject", "Rejected"
        RETURN = "return", "Returned for changes"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    submission = models.OneToOneField(
        RequestSubmission, on_delete=models.PROTECT, related_name="decision"
    )
    decision = models.CharField(max_length=8, choices=Decision.choices)
    conditions = models.TextField(blank=True)
    committee_reference = models.CharField(max_length=160, blank=True)
    conflicts_note = models.TextField("Conflicts of interest", blank=True)
    actor = _actor()
    recorded_at = models.DateTimeField(default=timezone.now)
    reason = models.TextField()

    class Meta:
        ordering = ["recorded_at", "id"]
        constraints = [
            _choices("decision", ["approve", "reject", "return"], "dist_decision_value"),
            _reason("dist_decision_reason"),
        ]


class RequestEvent(models.Model):
    class Kind(models.TextChoices):
        CREATED = "created", "Created"
        EDITED = "edited", "Edited"
        AGREEMENT_LINKED = "agreement_linked", "Agreement linked"
        REOPENED = "reopened", "Reopened"
        CANCELLED = "cancelled", "Cancelled"
        CLOSED = "closed", "Closed"
        NOTE = "note", "Note"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey(SpecimenRequest, on_delete=models.PROTECT, related_name="events")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    changes = models.JSONField(default=dict, blank=True)
    actor = _actor()
    recorded_at = models.DateTimeField(default=timezone.now)
    reason = models.TextField(blank=True)

    class Meta:
        ordering = ["recorded_at", "id"]
        constraints = [
            _choices(
                "kind",
                [
                    "created",
                    "edited",
                    "agreement_linked",
                    "reopened",
                    "cancelled",
                    "closed",
                    "note",
                ],
                "dist_requestevent_kind",
            )
        ]


class Allocation(models.Model):
    """A specimen reserved for an approved request (the specimen, never its position)."""

    class State(models.TextChoices):
        ALLOCATED = "allocated", "Allocated"
        PICKED = "picked", "Picked"
        SHIPPED = "shipped", "Shipped"
        RELEASED = "released", "Released"

    OPEN = ("allocated", "picked")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey(
        SpecimenRequest, on_delete=models.PROTECT, related_name="allocations"
    )
    item = models.ForeignKey(RequestItem, on_delete=models.PROTECT, related_name="allocations")
    specimen = models.ForeignKey("specimens.Specimen", on_delete=models.PROTECT, related_name="+")
    state = models.CharField(max_length=10, choices=State.choices, default=State.ALLOCATED)
    hold = models.OneToOneField("specimens.Hold", on_delete=models.PROTECT, related_name="+")
    shipment = models.ForeignKey(
        "Shipment", on_delete=models.PROTECT, related_name="allocations", null=True, blank=True
    )
    packed_revision = models.PositiveIntegerField(null=True, blank=True)
    allocated_by = _actor()
    allocated_at = models.DateTimeField(default=timezone.now)
    revision = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["allocated_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["specimen"],
                condition=Q(state__in=["allocated", "picked"]),
                name="dist_allocation_one_open",
            ),
            _choices(
                "state", ["allocated", "picked", "shipped", "released"], "dist_allocation_state"
            ),
            models.CheckConstraint(
                condition=~Q(state="shipped") | Q(shipment__isnull=False),
                name="dist_allocation_shipped",
            ),
            models.CheckConstraint(
                condition=Q(packed_revision__isnull=True)
                | Q(state="picked", shipment__isnull=False),
                name="dist_allocation_packed",
            ),
        ]
        indexes = [models.Index(fields=["request", "state"], name="dist_allocation_state_idx")]


class AllocationEvent(models.Model):
    class Kind(models.TextChoices):
        ALLOCATED = "allocated", "Allocated"
        PICKED = "picked", "Picked"
        NOT_FOUND = "not_found", "Not found at its location"
        PUT_BACK = "put_back", "Put back"
        RELEASED = "released", "Released"
        SHIPPED = "shipped", "Shipped"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    allocation = models.ForeignKey(Allocation, on_delete=models.PROTECT, related_name="events")
    kind = models.CharField(max_length=12, choices=Kind.choices)
    custody_event = models.ForeignKey(
        "specimens.CustodyEvent", on_delete=models.PROTECT, related_name="+", null=True, blank=True
    )
    actor = _actor()
    occurred_at = models.DateTimeField()
    recorded_at = models.DateTimeField(default=timezone.now)
    reason = models.TextField()

    class Meta:
        ordering = ["occurred_at", "recorded_at", "id"]
        constraints = [
            _choices(
                "kind",
                ["allocated", "picked", "not_found", "put_back", "released", "shipped"],
                "dist_allocationevent_kind",
            ),
            models.CheckConstraint(
                condition=~Q(kind__in=["picked", "put_back", "shipped"])
                | Q(custody_event__isnull=False),
                name="dist_allocationevent_custody",
            ),
            _reason("dist_allocationevent_reason"),
        ]


# ------------------------------------------------------------------ shipments


class Shipment(models.Model):
    class State(models.TextChoices):
        DRAFT = "draft", "Draft"
        PACKED = "packed", "Packed"
        DISPATCHED = "dispatched", "Dispatched"
        RECEIVED = "received", "Receipt recorded"
        CANCELLED = "cancelled", "Cancelled"

    class Temperature(models.TextChoices):
        AMBIENT = "ambient", "Ambient"
        COOL = "2_8c", "2 to 8 °C"
        MINUS20 = "minus20c", "−20 °C"
        MINUS80 = "minus80c", "−80 °C"
        CRYOGENIC = "cryogenic", "Cryogenic (vapor phase)"
        OTHER = "other", "Other"

    class Refrigerant(models.TextChoices):
        NONE = "none", "None"
        GEL_ICE = "gel_ice", "Gel packs"
        DRY_ICE = "dry_ice", "Dry ice"
        DRY_SHIPPER = "dry_shipper", "Dry shipper (liquid nitrogen absorbed)"
        OTHER = "other", "Other"

    class Classification(models.TextChoices):
        NOT_RESTRICTED = "not_restricted", "Not restricted"
        EXEMPT = "exempt_specimen", "Exempt specimen"
        CATEGORY_B = "category_b", "Category B (UN3373)"
        CATEGORY_A = "category_a", "Category A"
        OTHER = "other", "Other"

    request = models.ForeignKey(SpecimenRequest, on_delete=models.PROTECT, related_name="shipments")
    state = models.CharField(max_length=10, choices=State.choices, default=State.DRAFT)
    ship_to_name = models.CharField("Recipient", max_length=160)
    ship_to_institution = models.CharField("Institution", max_length=160)
    ship_to_address = models.TextField("Address")
    ship_to_contact = models.CharField("Contact", max_length=160, blank=True)
    planned_on = models.DateField(null=True, blank=True)
    courier = models.CharField(max_length=80, blank=True)
    service_level = models.CharField(max_length=80, blank=True)
    tracking = models.CharField("Tracking number", max_length=80, blank=True)
    handed_to = models.CharField(max_length=160, blank=True)
    temperature_class = models.CharField(max_length=12, choices=Temperature.choices, blank=True)
    refrigerant = models.CharField(max_length=12, choices=Refrigerant.choices, blank=True)
    dry_ice_kg = models.DecimalField(
        "Dry ice (kg)", max_digits=6, decimal_places=2, null=True, blank=True
    )
    classification = models.CharField(
        "Classification declared by the shipper",
        max_length=16,
        choices=Classification.choices,
        blank=True,
    )
    classification_note = models.CharField(max_length=160, blank=True)
    package_count = models.PositiveSmallIntegerField(null=True, blank=True)
    emergency_contact = models.CharField(max_length=160, blank=True)
    logger_ids = models.CharField("Temperature loggers", max_length=160, blank=True)
    storage_instructions = models.CharField(max_length=500, blank=True)
    include_participant_codes = models.BooleanField(default=False)
    training_confirmed = models.BooleanField(null=True, blank=True)
    packed_by = _actor(null=True, blank=True)
    packed_at = models.DateTimeField(null=True, blank=True)
    checked_by = _actor(null=True, blank=True)
    dispatched_by = _actor(null=True, blank=True)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    revision = models.PositiveIntegerField(default=1)
    created_by = _actor(editable=False)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ["-id"]
        constraints = [
            _choices(
                "state",
                ["draft", "packed", "dispatched", "received", "cancelled"],
                "dist_shipment_state",
            ),
            _choices(
                "temperature_class",
                ["", "ambient", "2_8c", "minus20c", "minus80c", "cryogenic", "other"],
                "dist_shipment_temperature",
            ),
            _choices(
                "refrigerant",
                ["", "none", "gel_ice", "dry_ice", "dry_shipper", "other"],
                "dist_shipment_refrigerant",
            ),
            _choices(
                "classification",
                ["", "not_restricted", "exempt_specimen", "category_b", "category_a", "other"],
                "dist_shipment_class",
            ),
            models.CheckConstraint(
                condition=Q(refrigerant="dry_ice", dry_ice_kg__gt=0)
                | (~Q(refrigerant="dry_ice") & Q(dry_ice_kg__isnull=True)),
                name="dist_shipment_dry_ice",
            ),
            models.CheckConstraint(
                condition=Q(package_count__isnull=True)
                | Q(package_count__gte=1, package_count__lte=99),
                name="dist_shipment_packages",
            ),
            models.CheckConstraint(
                condition=Q(state__in=["draft", "cancelled"])
                | (
                    Q(
                        packed_by__isnull=False,
                        packed_at__isnull=False,
                        package_count__isnull=False,
                        training_confirmed__isnull=False,
                    )
                    & ~Q(classification="")
                    & ~Q(refrigerant="")
                    & ~Q(temperature_class="")
                    & ~Q(emergency_contact="")
                ),
                name="dist_shipment_packed",
            ),
            models.CheckConstraint(
                condition=Q(checked_by__isnull=True) | ~Q(checked_by=F("packed_by")),
                name="dist_shipment_checker",
            ),
            models.CheckConstraint(
                condition=~Q(state__in=["dispatched", "received"])
                | Q(dispatched_by__isnull=False, dispatched_at__gte=F("packed_at")),
                name="dist_shipment_dispatched",
            ),
        ]
        indexes = [models.Index(fields=["state"], name="dist_shipment_state_idx")]

    @property
    def number(self):
        return f"SH-{self.pk:06d}" if self.pk else "SH-new"

    def __str__(self):
        return self.number

    @property
    def is_sent(self):
        return self.state in (self.State.DISPATCHED, self.State.RECEIVED)


class ShipmentItem(models.Model):
    """Append-only: what was sent, written at dispatch and never changed afterwards."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    shipment = models.ForeignKey(Shipment, on_delete=models.PROTECT, related_name="items")
    line = models.PositiveIntegerField()
    allocation = models.OneToOneField(
        Allocation, on_delete=models.PROTECT, related_name="shipment_item"
    )
    specimen = models.ForeignKey("specimens.Specimen", on_delete=models.PROTECT, related_name="+")
    accession = models.CharField(max_length=32)
    specimen_type = models.CharField(max_length=210)
    container = models.CharField(max_length=210)
    quantity = _quantity()
    unit = models.CharField(max_length=16)
    study_code = models.CharField(max_length=40)
    participant_code = models.CharField(max_length=40, blank=True)
    specimen_revision = models.PositiveIntegerField()
    custody_event = models.OneToOneField(
        "specimens.CustodyEvent", on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["shipment", "line"]
        constraints = [
            models.UniqueConstraint(fields=["shipment", "line"], name="dist_shipmentitem_line"),
            models.CheckConstraint(condition=Q(quantity__gt=0), name="dist_shipmentitem_quantity"),
        ]


class ShipmentEvent(AttachmentMixin):
    class Kind(models.TextChoices):
        CREATED = "created", "Created"
        EDITED = "edited", "Details changed"
        CONTENTS = "contents", "Contents changed"
        PACKED = "packed", "Packed"
        UNPACKED = "unpacked", "Unpacked"
        NOTIFIED = "notified", "Recipient notified"
        ACKNOWLEDGED = "acknowledged", "Recipient acknowledged"
        DISPATCHED = "dispatched", "Dispatched"
        RECEIPT = "receipt", "Receipt recorded"
        RETURNED = "returned", "Return recorded"
        CANCELLED = "cancelled", "Cancelled"
        NOTE = "note", "Note or attachment"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    shipment = models.ForeignKey(Shipment, on_delete=models.PROTECT, related_name="events")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    note = models.TextField(blank=True, help_text=NO_PARTICIPANT_TEXT)
    changes = models.JSONField(default=dict, blank=True)
    actor = _actor()
    occurred_at = models.DateTimeField()
    recorded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["occurred_at", "recorded_at", "id"]
        constraints = [
            _choices(
                "kind",
                [
                    "created",
                    "edited",
                    "contents",
                    "packed",
                    "unpacked",
                    "notified",
                    "acknowledged",
                    "dispatched",
                    "receipt",
                    "returned",
                    "cancelled",
                    "note",
                ],
                "dist_shipmentevent_kind",
            )
        ]


class ShipmentReceipt(AttachmentMixin):
    """Append-only: the recipient's verification as reported, transcribed by staff."""

    class Condition(models.TextChoices):
        AS_EXPECTED = "as_expected", "As expected"
        DISCREPANCY = "discrepancy", "With discrepancies"
        DAMAGED = "damaged", "Damaged"
        LOST = "lost", "Lost in transit"

    class Refrigerant(models.TextChoices):
        ADEQUATE = "adequate", "Refrigerant remaining"
        DEPLETED = "depleted", "Refrigerant depleted"
        NOT_APPLICABLE = "not_applicable", "Not applicable"
        UNKNOWN = "unknown", "Not reported"

    class Via(models.TextChoices):
        EMAIL = "email", "Email"
        DOCUMENT = "document", "Signed receipt document"
        PHONE = "phone", "Phone call"
        OTHER = "other", "Other"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    shipment = models.ForeignKey(Shipment, on_delete=models.PROTECT, related_name="receipts")
    received_at = models.DateTimeField()
    received_by_name = models.CharField("Received by", max_length=160)
    unpacked_by_name = models.CharField("Unpacked by", max_length=160, blank=True)
    condition = models.CharField(max_length=12, choices=Condition.choices)
    refrigerant_on_arrival = models.CharField(max_length=16, choices=Refrigerant.choices)
    temperature_note = models.CharField(max_length=160, blank=True)
    discrepancies = models.TextField(blank=True, help_text=NO_PARTICIPANT_TEXT)
    received_via = models.CharField("Reported by", max_length=16, choices=Via.choices)
    corrects = models.OneToOneField(
        "self", on_delete=models.PROTECT, related_name="corrected_by", null=True, blank=True
    )
    actor = _actor()
    recorded_at = models.DateTimeField(default=timezone.now)
    reason = models.TextField()

    class Meta:
        ordering = ["recorded_at", "id"]
        constraints = [
            _choices(
                "condition",
                ["as_expected", "discrepancy", "damaged", "lost"],
                "dist_receipt_condition",
            ),
            _choices(
                "refrigerant_on_arrival",
                ["adequate", "depleted", "not_applicable", "unknown"],
                "dist_receipt_refrigerant",
            ),
            _choices("received_via", ["email", "document", "phone", "other"], "dist_receipt_via"),
            models.CheckConstraint(
                condition=Q(condition="as_expected") | ~Q(discrepancies=""),
                name="dist_receipt_discrepancies",
            ),
            _reason("dist_receipt_reason"),
        ]


class ReceiptException(models.Model):
    class Outcome(models.TextChoices):
        MISSING = "missing", "Missing"
        DAMAGED = "damaged", "Damaged"
        THAWED = "thawed", "Arrived thawed"
        MISMATCH = "mismatch", "Does not match the manifest"
        OTHER = "other", "Other"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    receipt = models.ForeignKey(
        ShipmentReceipt, on_delete=models.PROTECT, related_name="exceptions"
    )
    item = models.ForeignKey(ShipmentItem, on_delete=models.PROTECT, related_name="+")
    outcome = models.CharField(max_length=12, choices=Outcome.choices)
    note = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["item__line"]
        constraints = [
            models.UniqueConstraint(fields=["receipt", "item"], name="dist_exception_once"),
            _choices(
                "outcome",
                ["missing", "damaged", "thawed", "mismatch", "other"],
                "dist_exception_outcome",
            ),
        ]


class SpecimenReturn(AttachmentMixin):
    """Append-only: a shipped specimen came back. It is held (use hold) until reviewed."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    shipment = models.ForeignKey(Shipment, on_delete=models.PROTECT, related_name="returns")
    item = models.OneToOneField(
        ShipmentItem, on_delete=models.PROTECT, related_name="specimen_return"
    )
    specimen = models.ForeignKey("specimens.Specimen", on_delete=models.PROTECT, related_name="+")
    occurred_at = models.DateTimeField()
    quantity = _quantity()
    unit = models.ForeignKey("specimens.QuantityUnit", on_delete=models.PROTECT, related_name="+")
    supplied_text = models.CharField(max_length=80, blank=True)
    thaw_count = models.PositiveIntegerField(null=True, blank=True)
    condition = models.TextField(blank=True, help_text=NO_PARTICIPANT_TEXT)
    custody_event = models.OneToOneField(
        "specimens.CustodyEvent", on_delete=models.PROTECT, related_name="+"
    )
    hold = models.OneToOneField("specimens.Hold", on_delete=models.PROTECT, related_name="+")
    actor = _actor()
    recorded_at = models.DateTimeField(default=timezone.now)
    reason = models.TextField()

    class Meta:
        ordering = ["-occurred_at", "id"]
        constraints = [
            models.CheckConstraint(condition=Q(quantity__gte=0), name="dist_return_quantity"),
            _reason("dist_return_reason"),
        ]


class ReturnReview(models.Model):
    class Decision(models.TextChoices):
        RELEASE = "release", "Released for use"
        KEEP = "keep", "Kept on hold"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    specimen_return = models.ForeignKey(
        SpecimenReturn, on_delete=models.PROTECT, related_name="reviews"
    )
    decision = models.CharField(max_length=8, choices=Decision.choices)
    actor = _actor()
    recorded_at = models.DateTimeField(default=timezone.now)
    reason = models.TextField()

    class Meta:
        ordering = ["recorded_at", "id"]
        constraints = [
            _choices("decision", ["release", "keep"], "dist_review_decision"),
            models.UniqueConstraint(
                fields=["specimen_return"],
                condition=Q(decision="release"),
                name="dist_review_one_release",
            ),
            _reason("dist_review_reason"),
        ]


# ------------------------------------------------------------------ agreements


class Agreement(models.Model):
    class Kind(models.TextChoices):
        MTA = "mta", "Material transfer agreement"
        DUA = "dua", "Data use agreement"
        MTA_DUA = "mta_dua", "Material transfer and data use agreement"
        OTHER = "other", "Other agreement"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        EXECUTED = "executed", "Executed"
        TERMINATED = "terminated", "Terminated"

    class DataLevel(models.TextChoices):
        NONE = "none", "No data"
        DE_IDENTIFIED = "de_identified", "De-identified"
        CODED = "coded", "Coded"
        LIMITED = "limited", "Limited data set"
        NOT_STATED = "not_stated", "Not stated"

    class EndDisposition(models.TextChoices):
        RETURN = "return", "Return to the repository"
        DESTROY = "destroy", "Destroy"
        RETAIN = "retain", "Recipient may retain"
        NOT_STATED = "not_stated", "Not stated"

    class OnwardTransfer(models.TextChoices):
        PROHIBITED = "prohibited", "Prohibited"
        PERMITTED = "permitted", "Permitted"
        NOT_STATED = "not_stated", "Not stated"

    kind = models.CharField(max_length=8, choices=Kind.choices)
    title = models.CharField(max_length=160)
    institution = models.ForeignKey(
        Institution, on_delete=models.PROTECT, related_name="agreements"
    )
    investigator = models.ForeignKey(
        Investigator, on_delete=models.PROTECT, related_name="+", null=True, blank=True
    )
    provider_reference = models.CharField(max_length=80, blank=True)
    recipient_reference = models.CharField(max_length=80, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    executed_on = models.DateField(null=True, blank=True)
    effective_on = models.DateField(null=True, blank=True)
    expires_on = models.DateField(null=True, blank=True)
    terminated_on = models.DateField(null=True, blank=True)
    data_level = models.CharField(
        max_length=16, choices=DataLevel.choices, default=DataLevel.NOT_STATED
    )
    end_disposition = models.CharField(
        max_length=12, choices=EndDisposition.choices, default=EndDisposition.NOT_STATED
    )
    onward_transfer = models.CharField(
        max_length=12, choices=OnwardTransfer.choices, default=OnwardTransfer.NOT_STATED
    )
    permitted_use = models.TextField(blank=True)
    permitted_categories = models.ManyToManyField(
        ConsentScopeCategory, related_name="+", blank=True
    )
    reporting_due_on = models.DateField(null=True, blank=True)
    document_reference = models.CharField(
        "Where the signed agreement is filed", max_length=160, blank=True
    )
    revision = models.PositiveIntegerField(default=1)
    created_by = _actor(editable=False)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ["-id"]
        constraints = [
            _choices("kind", ["mta", "dua", "mta_dua", "other"], "dist_agreement_kind"),
            _choices("status", ["draft", "executed", "terminated"], "dist_agreement_status"),
            _choices(
                "data_level",
                ["none", "de_identified", "coded", "limited", "not_stated"],
                "dist_agreement_data_level",
            ),
            _choices(
                "end_disposition",
                ["return", "destroy", "retain", "not_stated"],
                "dist_agreement_disposition",
            ),
            _choices(
                "onward_transfer",
                ["prohibited", "permitted", "not_stated"],
                "dist_agreement_onward",
            ),
            models.CheckConstraint(
                condition=Q(status="draft")
                | Q(executed_on__isnull=False, effective_on__isnull=False),
                name="dist_agreement_executed",
            ),
            models.CheckConstraint(
                condition=Q(expires_on__isnull=True)
                | Q(effective_on__isnull=True)
                | Q(expires_on__gte=F("effective_on")),
                name="dist_agreement_period",
            ),
            models.CheckConstraint(
                condition=Q(status="terminated", terminated_on__isnull=False)
                | (~Q(status="terminated") & Q(terminated_on__isnull=True)),
                name="dist_agreement_terminated",
            ),
        ]

    @property
    def number(self):
        return f"AG-{self.pk:06d}" if self.pk else "AG-new"

    def __str__(self):
        return f"{self.number} · {self.title}"

    def is_valid_on(self, day):
        """Executed, in effect on `day`, not expired, and not terminated on or before `day`."""
        return (
            self.status in (self.Status.EXECUTED, self.Status.TERMINATED)
            and self.effective_on is not None
            and self.effective_on <= day
            and (self.expires_on is None or self.expires_on >= day)
            and (self.terminated_on is None or day < self.terminated_on)
        )


class AgreementDocument(AttachmentMixin):
    class Kind(models.TextChoices):
        EXECUTED = "executed", "Executed agreement"
        AMENDMENT = "amendment", "Amendment"
        DRAFT = "draft", "Draft"
        CORRESPONDENCE = "correspondence", "Correspondence"
        OTHER = "other", "Other"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agreement = models.ForeignKey(Agreement, on_delete=models.PROTECT, related_name="documents")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    title = models.CharField(max_length=160)
    note = models.TextField(blank=True)
    supersedes = models.ForeignKey(
        "self", on_delete=models.PROTECT, related_name="superseded_by", null=True, blank=True
    )
    actor = _actor()
    recorded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["recorded_at", "id"]
        constraints = [
            _choices(
                "kind",
                ["executed", "amendment", "draft", "correspondence", "other"],
                "dist_document_kind",
            ),
            models.CheckConstraint(
                condition=Q(attachment__isnull=False), name="dist_document_attachment"
            ),
        ]


class AgreementEvent(models.Model):
    class Kind(models.TextChoices):
        CREATED = "created", "Created"
        EDITED = "edited", "Edited"
        EXECUTED = "executed", "Executed"
        TERMINATED = "terminated", "Terminated"
        DOCUMENT = "document", "Document added"
        NOTE = "note", "Note"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agreement = models.ForeignKey(Agreement, on_delete=models.PROTECT, related_name="events")
    kind = models.CharField(max_length=12, choices=Kind.choices)
    changes = models.JSONField(default=dict, blank=True)
    actor = _actor()
    recorded_at = models.DateTimeField(default=timezone.now)
    reason = models.TextField(blank=True)

    class Meta:
        ordering = ["recorded_at", "id"]
        constraints = [
            _choices(
                "kind",
                ["created", "edited", "executed", "terminated", "document", "note"],
                "dist_agreementevent_kind",
            )
        ]


class OutputRecord(models.Model):
    """What was printed or exported, by whom and when: identifiers and counts, never bytes."""

    class Kind(models.TextChoices):
        PICK_LIST = "pick_list", "Pick list printed"
        MANIFEST = "manifest", "Manifest printed"
        EXPORT = "export", "List exported"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=12, choices=Kind.choices)
    actor = _actor()
    recorded_at = models.DateTimeField(default=timezone.now)
    count = models.PositiveIntegerField()
    details = models.JSONField(default=dict)

    class Meta:
        ordering = ["-recorded_at", "id"]
