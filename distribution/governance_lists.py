"""One definition per governance list: filters, query, sorts, search and CSV columns.

The list pages (distribution.governance_views), their CSV exports and the dashboard queues
(distribution.dashboard) all read these specs, so a download matches what the list shows and a
queue's count equals the rows its link shows. An invalid filter returns no rows, never all.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from django import forms
from django.db.models import Case, Count, Exists, OuterRef, Q, Value, When
from django.utils import timezone

from specimens.models import Specimen

from .models import (
    Agreement,
    Consent,
    ConsentForm,
    ConsentHold,
    ConsentReinstatement,
    ConsentScopeCategory,
    ConsentWithdrawal,
    Institution,
    Investigator,
    ShipmentItem,
    Study,
    StudyCollection,
    WithdrawalFollowUp,
)
from .queries import agreement_horizon

PAGE_SIZES = [(size, f"{size} rows") for size in (25, 50, 100)]
IN_CUSTODY = (Specimen.Status.AVAILABLE, Specimen.Status.EXHAUSTED)
STOPS_USE = (ConsentWithdrawal.Kind.NO_USE, ConsentWithdrawal.Kind.DESTROY)
HORIZON_DAYS = 60


def person(user):
    return (user.get_full_name() or user.get_username()) if user else ""


def local(value):
    return timezone.localtime(value).strftime("%Y-%m-%d %H:%M:%S") if value else ""


def yes_no(value):
    return "yes" if value else "no"


# --- filter forms ---------------------------------------------------------------------------


class ListFilter(forms.Form):
    q = forms.CharField(
        label="Search",
        required=False,
        max_length=200,
        widget=forms.SearchInput(attrs={"placeholder": "Code"}),
    )
    per_page = forms.TypedChoiceField(
        label="Rows per page", choices=PAGE_SIZES, coerce=int, required=False
    )
    sort = forms.CharField(required=False, widget=forms.HiddenInput())

    placeholder = "Code"

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields["q"].widget.attrs["placeholder"] = self.placeholder


def _choice(label, choices, any_label):
    return forms.ChoiceField(label=label, choices=[("", any_label), *choices], required=False)


def _model(label, queryset, any_label):
    return forms.ModelChoiceField(
        label=label, queryset=queryset, required=False, empty_label=any_label
    )


class StudyChoice(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return f"{obj.code} · {obj.title}"


class StudyFilter(ListFilter):
    placeholder = "Code, title or investigator"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["status"] = _choice("Status", Study.Status.choices, "Any status")
        self.fields["basis"] = _choice("Consent basis", Study.Basis.choices, "Any basis")


def _study_field():
    return StudyChoice(
        label="Study",
        queryset=Study.objects.order_by("code"),
        required=False,
        empty_label="Any study",
    )


class ConsentFilter(ListFilter):
    placeholder = "Participant code or study code"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["status"] = _choice("Status", Consent.Status.choices, "Any status")
        self.fields["study"] = _study_field()
        self.fields["participant"] = forms.CharField(
            label="Participant",
            required=False,
            max_length=40,
            help_text="A coded participant identifier, e.g. P-000123 (exact match).",
        )
        self.fields["no_contact"] = forms.BooleanField(
            label="Do not contact",
            required=False,
            help_text="Consents flagged after a withdrawal of contact.",
        )

    def clean_participant(self):
        return (self.cleaned_data.get("participant") or "").strip().upper()


WITHDRAWAL_STATES = [("open", "In effect"), ("reinstated", "Reinstated")]


class WithdrawalFilter(ListFilter):
    placeholder = "Participant code or study code"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["kind"] = _choice(
            "Effective withdrawal", ConsentWithdrawal.Kind.choices, "Any kind"
        )
        self.fields["study"] = _study_field()
        self.fields["state"] = _choice("State", WITHDRAWAL_STATES, "In effect or reinstated")
        self.fields["attention"] = forms.BooleanField(
            label="Needs follow-up",
            required=False,
            help_text="In effect, with specimens in custody that have no hold from it, or with "
            "distributed material whose shipment has no recorded follow-up.",
        )


class AgreementFilter(ListFilter):
    placeholder = "Agreement number, title or reference"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["status"] = _choice("Status", Agreement.Status.choices, "Any status")
        self.fields["institution"] = _model(
            "Institution", Institution.objects.order_by("code"), "Any institution"
        )
        self.fields["kind"] = _choice("Kind", Agreement.Kind.choices, "Any kind")
        self.fields["horizon"] = forms.BooleanField(
            label=f"Expiring or reporting due within {HORIZON_DAYS} days",
            required=False,
            help_text="Executed agreements whose expiry date falls in the next "
            f"{HORIZON_DAYS} days, or whose reporting date is in the past or the next "
            f"{HORIZON_DAYS} days. A display horizon, not a rule.",
        )


ACTIVE = [("", "Active"), ("inactive", "Inactive"), ("all", "Active and inactive")]


class CatalogueFilter(ListFilter):
    placeholder = "Code or name"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["active"] = forms.ChoiceField(label="Status", choices=ACTIVE, required=False)


class ConsentFormFilter(CatalogueFilter):
    placeholder = "Code, version or title"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["study"] = _study_field()


class InvestigatorFilter(CatalogueFilter):
    placeholder = "Code, name or email"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["institution"] = _model(
            "Institution", Institution.objects.order_by("code"), "Any institution"
        )


# --- queries --------------------------------------------------------------------------------


def _search(queryset, text, fields):
    condition = Q()
    for name in fields:
        condition |= Q(**{f"{name}__icontains": text})
    return queryset.filter(condition)


def _active(queryset, cleaned):
    value = cleaned.get("active")
    if value == "":
        return queryset.filter(is_active=True)
    if value == "inactive":
        return queryset.filter(is_active=False)
    return queryset


def study_rows(cleaned, user):
    queryset = Study.objects.annotate(
        current_links=Count(
            "collection_links", filter=Q(collection_links__ended_at__isnull=True), distinct=True
        ),
        consent_count=Count("consents", distinct=True),
    )
    if cleaned.get("status"):
        queryset = queryset.filter(status=cleaned["status"])
    if cleaned.get("basis"):
        queryset = queryset.filter(consent_basis=cleaned["basis"])
    return queryset


def consent_rows(cleaned, user):
    queryset = Consent.objects.select_related(
        "participant", "study", "current_record", "current_record__form"
    )
    if cleaned.get("status"):
        queryset = queryset.filter(status=cleaned["status"])
    if cleaned.get("study"):
        queryset = queryset.filter(study=cleaned["study"])
    if cleaned.get("participant"):
        queryset = queryset.filter(participant__code__iexact=cleaned["participant"])
    if cleaned.get("no_contact"):
        queryset = queryset.filter(no_contact=True)
    return queryset


def _governed_collections(study_ref, current_only):
    links = StudyCollection.objects.filter(study_id=study_ref)
    if current_only:
        links = links.filter(ended_at__isnull=True)
    return links.values("collection_id")


def unheld_specimens():
    """Subquery for a withdrawal row: specimens of its participant in the study's current
    collections, in custody, without an active hold from that withdrawal (consent_impact's
    'unheld')."""
    held = ConsentHold.objects.filter(
        withdrawal_id=OuterRef(OuterRef("pk")),
        specimen_id=OuterRef("pk"),
        hold__release__isnull=True,
    )
    return Specimen.objects.filter(
        participant_id=OuterRef("consent__participant_id"),
        collection_id__in=_governed_collections(OuterRef(OuterRef("consent__study_id")), True),
        status__in=IN_CUSTODY,
    ).filter(~Exists(held))


def unfollowed_shipments():
    """Subquery for a withdrawal row: shipped items of its participant's material in the study
    whose shipment has no follow-up recorded on that withdrawal."""
    followed = WithdrawalFollowUp.objects.filter(
        withdrawal_id=OuterRef(OuterRef("pk")), shipment_id=OuterRef("shipment_id")
    )
    return ShipmentItem.objects.filter(
        specimen__participant_id=OuterRef("consent__participant_id"),
        specimen__collection_id__in=_governed_collections(
            OuterRef(OuterRef("consent__study_id")), False
        ),
    ).filter(~Exists(followed))


def withdrawal_queryset():
    return ConsentWithdrawal.objects.select_related(
        "consent", "consent__participant", "consent__study", "actor", "escalation__actor"
    ).annotate(
        effective_kind_label=Case(
            When(escalation__isnull=False, then=Value(ConsentWithdrawal.Kind.DESTROY.label)),
            *(When(kind=kind, then=Value(label)) for kind, label in ConsentWithdrawal.Kind.choices),
        ),
        follow_up_count=Count("follow_ups", distinct=True),
        is_reinstated=Exists(ConsentReinstatement.objects.filter(withdrawal=OuterRef("pk"))),
    )


def needing_attention(queryset=None):
    """Withdrawals in effect that stop use and still need a step: unheld specimens in custody,
    or distributed material without a recorded follow-up for its shipment."""
    queryset = withdrawal_queryset() if queryset is None else queryset
    return queryset.filter(kind__in=STOPS_USE, is_reinstated=False).filter(
        Exists(unheld_specimens()) | Exists(unfollowed_shipments())
    )


def withdrawal_rows(cleaned, user):
    queryset = withdrawal_queryset()
    if cleaned.get("kind"):
        queryset = queryset.filter(
            effective_kind_label=ConsentWithdrawal.Kind(cleaned["kind"]).label
        )
    if cleaned.get("study"):
        queryset = queryset.filter(consent__study=cleaned["study"])
    if cleaned.get("state") == "open":
        queryset = queryset.filter(is_reinstated=False)
    elif cleaned.get("state") == "reinstated":
        queryset = queryset.filter(is_reinstated=True)
    if cleaned.get("attention"):
        queryset = needing_attention(queryset)
    return queryset


def within_horizon(queryset=None, today=None):
    """Executed agreements expiring within the display horizon, or with a reporting date that
    has passed or falls within it (queries.agreement_horizon)."""
    today, end = agreement_horizon(today, HORIZON_DAYS)
    queryset = Agreement.objects.all() if queryset is None else queryset
    return queryset.filter(status=Agreement.Status.EXECUTED).filter(
        Q(expires_on__gte=today, expires_on__lte=end) | Q(reporting_due_on__lte=end)
    )


def agreement_rows(cleaned, user):
    queryset = Agreement.objects.select_related("institution", "investigator")
    if cleaned.get("status"):
        queryset = queryset.filter(status=cleaned["status"])
    if cleaned.get("institution"):
        queryset = queryset.filter(institution=cleaned["institution"])
    if cleaned.get("kind"):
        queryset = queryset.filter(kind=cleaned["kind"])
    if cleaned.get("horizon"):
        queryset = within_horizon(queryset)
    return queryset


AGREEMENT_NUMBER = re.compile(r"^(?:AG-?)?0*(\d{1,12})$", re.I)


def search_agreements(queryset, text):
    condition = (
        Q(title__icontains=text)
        | Q(provider_reference__icontains=text)
        | Q(recipient_reference__icontains=text)
        | Q(institution__code__icontains=text)
    )
    match = AGREEMENT_NUMBER.match(text.strip())
    if match:
        condition |= Q(pk=int(match.group(1)))
    return queryset.filter(condition)


def category_rows(cleaned, user):
    return _active(ConsentScopeCategory.objects.all(), cleaned)


def consent_form_rows(cleaned, user):
    queryset = _active(
        ConsentForm.objects.select_related("study").prefetch_related("categories"), cleaned
    )
    if cleaned.get("study"):
        queryset = queryset.filter(study=cleaned["study"])
    return queryset


def institution_rows(cleaned, user):
    return _active(Institution.objects.all(), cleaned)


def investigator_rows(cleaned, user):
    queryset = _active(Investigator.objects.select_related("institution"), cleaned)
    if cleaned.get("institution"):
        queryset = queryset.filter(institution=cleaned["institution"])
    return queryset


# --- CSV columns ----------------------------------------------------------------------------


def _day(value):
    return value.isoformat() if value else ""


def _record(consent, name):
    record = consent.current_record
    return getattr(record, name) if record is not None else None


def _escalation(withdrawal, field):
    return getattr(getattr(withdrawal, "escalation", None), field, None)


EXPORT = {
    "study": (
        ("Code", lambda o: o.code),
        ("Title", lambda o: o.title),
        ("Status", lambda o: o.get_status_display()),
        ("Principal investigator", lambda o: o.principal_investigator),
        ("Ethics reference", lambda o: o.ethics_reference),
        ("Ethics approved until", lambda o: _day(o.ethics_approved_until)),
        ("Consent basis", lambda o: o.get_consent_basis_display()),
        ("Determination reference", lambda o: o.determination_reference),
        ("Current collections", lambda o: o.current_links),
        ("Consents", lambda o: o.consent_count),
    ),
    "consent": (
        ("Participant", lambda o: o.participant.code),
        ("Study", lambda o: o.study.code),
        ("Status", lambda o: o.get_status_display()),
        ("Form", lambda o: str(_record(o, "form") or "")),
        ("Decision", lambda o: o.current_record.get_decision_display() if o.current_record else ""),
        ("Date of the decision", lambda o: _day(_record(o, "consented_on"))),
        ("Expires", lambda o: _day(_record(o, "expires_on"))),
        ("Do not contact", lambda o: yes_no(o.no_contact)),
    ),
    "withdrawal": (
        ("Participant", lambda o: o.consent.participant.code),
        ("Study", lambda o: o.consent.study.code),
        ("Withdrawal", lambda o: o.get_kind_display()),
        ("Effective", lambda o: _day(o.effective_on)),
        ("Received", lambda o: local(o.occurred_at)),
        ("Held", lambda o: o.held_count),
        ("Allocations released", lambda o: o.released_count),
        ("Distributed", lambda o: o.distributed_count),
        ("Follow-ups", lambda o: o.follow_up_count),
        ("Reinstated", lambda o: yes_no(o.is_reinstated)),
        ("Recorded by", lambda o: person(o.actor)),
        ("Effective withdrawal", lambda o: o.get_effective_kind_display()),
        ("Escalation effective on", lambda o: _day(_escalation(o, "effective_on"))),
        ("Escalation received at", lambda o: local(_escalation(o, "occurred_at"))),
        ("Escalation received via", lambda o: _escalation(o, "received_via") or ""),
        ("Escalation recorded at", lambda o: local(_escalation(o, "recorded_at"))),
        ("Escalation recorded by", lambda o: person(_escalation(o, "actor"))),
        ("Escalation reason", lambda o: _escalation(o, "reason") or ""),
    ),
    "agreement": (
        ("Agreement", lambda o: o.number),
        ("Kind", lambda o: o.get_kind_display()),
        ("Title", lambda o: o.title),
        ("Status", lambda o: o.get_status_display()),
        ("Institution", lambda o: o.institution.code),
        ("Investigator", lambda o: o.investigator.code if o.investigator_id else ""),
        ("Provider reference", lambda o: o.provider_reference),
        ("Recipient reference", lambda o: o.recipient_reference),
        ("Executed", lambda o: _day(o.executed_on)),
        ("Effective", lambda o: _day(o.effective_on)),
        ("Expires", lambda o: _day(o.expires_on)),
        ("Terminated", lambda o: _day(o.terminated_on)),
        ("Data level", lambda o: o.get_data_level_display()),
        ("End of use", lambda o: o.get_end_disposition_display()),
        ("Onward transfer", lambda o: o.get_onward_transfer_display()),
        ("Reporting due", lambda o: _day(o.reporting_due_on)),
        ("Where filed", lambda o: o.document_reference),
    ),
    "category": (
        ("Code", lambda o: o.code),
        ("Name", lambda o: o.name),
        ("Governs use", lambda o: yes_no(o.gates_use)),
        ("Active", lambda o: yes_no(o.is_active)),
        ("Description", lambda o: o.description),
    ),
    "consent_form": (
        ("Study", lambda o: o.study.code),
        ("Code", lambda o: o.code),
        ("Version", lambda o: o.version),
        ("Title", lambda o: o.title),
        ("Kind", lambda o: o.get_kind_display()),
        ("Categories asked", lambda o: " ".join(c.code for c in o.categories.all())),
        ("Active", lambda o: yes_no(o.is_active)),
    ),
    "institution": (
        ("Code", lambda o: o.code),
        ("Name", lambda o: o.name),
        ("Country", lambda o: o.country),
        ("Shipping address", lambda o: o.address),
        ("Active", lambda o: yes_no(o.is_active)),
    ),
    "investigator": (
        ("Code", lambda o: o.code),
        ("Name", lambda o: o.name),
        ("Institution", lambda o: o.institution.code),
        ("Email", lambda o: o.email),
        ("Phone", lambda o: o.phone),
        ("Active", lambda o: yes_no(o.is_active)),
    ),
}


# --- specs ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class ListSpec:
    key: str
    slug: str  # the catalogue-export kind in the URL
    noun: str
    build: Callable  # (cleaned, user) -> queryset
    empty: Callable
    columns: dict  # sort key -> ORM ordering field
    default_sort: str
    form_class: type
    search_fields: tuple = ()
    search: Callable | None = None
    inline: tuple = ()
    more: tuple = ()
    export_columns: tuple = field(default=())


SPECS = {
    "study": ListSpec(
        key="study",
        slug="studies",
        noun="studies",
        build=study_rows,
        empty=Study.objects.none,
        columns={
            "code": "code",
            "title": "title",
            "status": "status",
            "until": "ethics_approved_until",
        },
        default_sort="code",
        form_class=StudyFilter,
        search_fields=("code", "title", "principal_investigator", "ethics_reference"),
        inline=("status", "basis"),
        export_columns=EXPORT["study"],
    ),
    "consent": ListSpec(
        key="consent",
        slug="consents",
        noun="consents",
        build=consent_rows,
        empty=Consent.objects.none,
        columns={
            "participant": "participant__code",
            "study": "study__code",
            "status": "status",
            "decided": "current_record__consented_on",
        },
        default_sort="participant",
        form_class=ConsentFilter,
        search_fields=("participant__code", "study__code"),
        inline=("status", "study", "participant"),
        more=("no_contact",),
        export_columns=EXPORT["consent"],
    ),
    "withdrawal": ListSpec(
        key="withdrawal",
        slug="withdrawals",
        noun="withdrawals",
        build=withdrawal_rows,
        empty=lambda: withdrawal_queryset().none(),
        columns={
            "participant": "consent__participant__code",
            "kind": "effective_kind_label",
            "effective": "effective_on",
            "received": "occurred_at",
        },
        default_sort="-received",
        form_class=WithdrawalFilter,
        search_fields=("consent__participant__code", "consent__study__code"),
        inline=("kind", "study", "state"),
        more=("attention",),
        export_columns=EXPORT["withdrawal"],
    ),
    "agreement": ListSpec(
        key="agreement",
        slug="agreements",
        noun="agreements",
        build=agreement_rows,
        empty=Agreement.objects.none,
        columns={
            "number": "id",
            "status": "status",
            "institution": "institution__code",
            "expires": "expires_on",
            "reporting": "reporting_due_on",
        },
        default_sort="-number",
        form_class=AgreementFilter,
        search=search_agreements,
        inline=("status", "institution", "kind"),
        more=("horizon",),
        export_columns=EXPORT["agreement"],
    ),
    "category": ListSpec(
        key="category",
        slug="categories",
        noun="scope categories",
        build=category_rows,
        empty=ConsentScopeCategory.objects.none,
        columns={"code": "code", "name": "name"},
        default_sort="code",
        form_class=CatalogueFilter,
        search_fields=("code", "name"),
        inline=("active",),
        export_columns=EXPORT["category"],
    ),
    "consent_form": ListSpec(
        key="consent_form",
        slug="consent-forms",
        noun="consent forms",
        build=consent_form_rows,
        empty=ConsentForm.objects.none,
        columns={"code": "code", "study": "study__code", "title": "title"},
        default_sort="code",
        form_class=ConsentFormFilter,
        search_fields=("code", "version", "title"),
        inline=("active", "study"),
        export_columns=EXPORT["consent_form"],
    ),
    "institution": ListSpec(
        key="institution",
        slug="institutions",
        noun="institutions",
        build=institution_rows,
        empty=Institution.objects.none,
        columns={"code": "code", "name": "name", "country": "country"},
        default_sort="code",
        form_class=CatalogueFilter,
        search_fields=("code", "name"),
        inline=("active",),
        export_columns=EXPORT["institution"],
    ),
    "investigator": ListSpec(
        key="investigator",
        slug="investigators",
        noun="investigators",
        build=investigator_rows,
        empty=Investigator.objects.none,
        columns={"code": "code", "name": "name", "institution": "institution__code"},
        default_sort="code",
        form_class=InvestigatorFilter,
        search_fields=("code", "name", "email"),
        inline=("active", "institution"),
        export_columns=EXPORT["investigator"],
    ),
}
BY_SLUG = {spec.slug: spec for spec in SPECS.values()}


def filtered(spec, data, user):
    """(form, queryset) for request-style filter data; an invalid form yields no rows."""
    form = spec.form_class(data, user=user)
    if not form.is_valid():
        return form, spec.empty()
    cleaned = form.cleaned_data
    queryset = spec.build(cleaned, user)
    if cleaned.get("q"):
        if spec.search:
            queryset = spec.search(queryset, cleaned["q"])
        else:
            queryset = _search(queryset, cleaned["q"], spec.search_fields)
    return form, queryset


def ordered(spec, queryset, sort):
    """Apply an allowlisted sort with the primary key as the stable tie-break."""
    key = (sort or spec.default_sort).removeprefix("-")
    if key not in spec.columns:
        sort = spec.default_sort
        key = sort.removeprefix("-")
    prefix = "-" if sort.startswith("-") else ""
    return queryset.order_by(prefix + spec.columns[key], prefix + "pk")
