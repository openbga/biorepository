"""One definition per list of the request and shipment pages: filters, query, sorts, search and
CSV columns. The list pages (request_views) and the CSV exports (distribution.exports) both read
these specs, so a download always matches what the list shows. An invalid filter returns no rows,
never all of them. Every role that can read distribution pages reads every row (no row-level
scoping: the pages are staff-only).
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from django import forms
from django.db.models import Count, Exists, OuterRef, Q, Subquery
from django.utils import timezone

from accounts.roles import can_decide_requests
from specimens.queries import normalize_accession

from .models import (
    Allocation,
    Institution,
    Investigator,
    RequestSubmission,
    ReturnReview,
    Shipment,
    ShipmentItem,
    ShipmentReceipt,
    SpecimenRequest,
    SpecimenReturn,
)

PAGE_SIZES = [(size, f"{size} rows") for size in (25, 50, 100)]
REQUEST_NUMBER = re.compile(r"^(?:RQ-?)?0*(\d{1,12})$", re.I)
SHIPMENT_NUMBER = re.compile(r"^(?:SH-?)?0*(\d{1,12})$", re.I)


def person(user):
    return (user.get_full_name() or user.get_username()) if user else ""


def local(value):
    return timezone.localtime(value).strftime("%Y-%m-%d %H:%M:%S") if value else ""


# --- filter forms ---------------------------------------------------------------------------


class ListFilter(forms.Form):
    q = forms.CharField(label="Search", required=False, max_length=200)
    per_page = forms.TypedChoiceField(
        label="Rows per page", choices=PAGE_SIZES, coerce=int, required=False
    )
    sort = forms.CharField(required=False, widget=forms.HiddenInput())

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user


def _choice(label, choices, any_label):
    return forms.ChoiceField(label=label, choices=[("", any_label), *choices], required=False)


def _specimen_field():
    return forms.CharField(
        label="Specimen",
        required=False,
        max_length=40,
        help_text="Specimen ID, for example VP-000123.",
    )


REQUEST_STATUS = [
    ("open", "Open (not final)"),
    *SpecimenRequest.Status.choices,
]


class RequestFilter(ListFilter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["q"].widget = forms.SearchInput(
            attrs={"placeholder": "Request number or title"}
        )
        self.fields["status"] = _choice("Status", REQUEST_STATUS, "Any status")
        self.fields["investigator"] = forms.ModelChoiceField(
            label="Investigator",
            queryset=Investigator.objects.order_by("code"),
            required=False,
            empty_label="Any investigator",
        )
        self.fields["institution"] = forms.ModelChoiceField(
            label="Institution",
            queryset=Institution.objects.order_by("code"),
            required=False,
            empty_label="Any institution",
        )
        self.fields["specimen"] = _specimen_field()
        self.fields["specimen"].help_text += " Requests that have allocated it."
        self.fields["to_pick"] = forms.BooleanField(
            label="Specimens to pick",
            required=False,
            help_text="Requests with allocated specimens not yet picked.",
        )
        if self.user is not None and can_decide_requests(self.user):
            self.fields["awaiting_me"] = forms.BooleanField(
                label="Awaiting my decision",
                required=False,
                help_text="Submitted requests you did not create or submit.",
            )

    def clean_specimen(self):
        return normalize_accession(self.cleaned_data.get("specimen") or "")


SHIPMENT_STATES = [
    ("open", "In preparation (draft or packed)"),
    *Shipment.State.choices,
]


class ShipmentFilter(ListFilter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["q"].widget = forms.SearchInput(
            attrs={"placeholder": "Shipment or request number, tracking, institution"}
        )
        self.fields["state"] = _choice("State", SHIPMENT_STATES, "Any state")
        self.fields["institution"] = forms.ModelChoiceField(
            label="Institution",
            queryset=Institution.objects.order_by("code"),
            required=False,
            empty_label="Any institution",
            help_text="The request's institution.",
        )
        self.fields["specimen"] = _specimen_field()
        self.fields["specimen"].help_text += " Shipments that contain or contained it."

    def clean_specimen(self):
        return normalize_accession(self.cleaned_data.get("specimen") or "")


RETURN_STATES = [
    ("awaiting", "Awaiting review"),
    ("kept", "Kept on hold"),
    ("released", "Released for use"),
]


class ReturnFilter(ListFilter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["q"].widget = forms.SearchInput(
            attrs={"placeholder": "Specimen ID or shipment number"}
        )
        self.fields["review"] = _choice("Review", RETURN_STATES, "Any review state")


# --- queries --------------------------------------------------------------------------------


def request_rows(cleaned, user):
    queryset = SpecimenRequest.objects.select_related("investigator", "institution").annotate(
        line_count=Count("items", distinct=True),
        open_count=Count(
            "allocations",
            filter=Q(allocations__state__in=Allocation.OPEN),
            distinct=True,
        ),
        shipped_count=Count(
            "allocations", filter=Q(allocations__state=Allocation.State.SHIPPED), distinct=True
        ),
    )
    status = cleaned.get("status")
    if status == "open":
        queryset = queryset.exclude(status__in=SpecimenRequest.FINAL)
    elif status:
        queryset = queryset.filter(status=status)
    if cleaned.get("investigator"):
        queryset = queryset.filter(investigator=cleaned["investigator"])
    if cleaned.get("institution"):
        queryset = queryset.filter(institution=cleaned["institution"])
    if cleaned.get("specimen"):
        queryset = queryset.filter(
            pk__in=Allocation.objects.filter(specimen__accession=cleaned["specimen"]).values(
                "request_id"
            )
        )
    if cleaned.get("to_pick"):
        queryset = queryset.filter(
            Exists(
                Allocation.objects.filter(request=OuterRef("pk"), state=Allocation.State.ALLOCATED)
            )
        )
    if cleaned.get("awaiting_me"):
        latest = RequestSubmission.objects.filter(
            request=OuterRef("pk"), number=OuterRef("latest_submission")
        )
        queryset = (
            queryset.filter(status=SpecimenRequest.Status.SUBMITTED)
            .exclude(created_by=user)
            .exclude(Exists(latest.filter(submitted_by=user)))
        )
    return queryset


def search_requests(queryset, text):
    match = REQUEST_NUMBER.match(text.strip())
    condition = Q(title__icontains=text)
    if match:
        condition |= Q(pk=int(match.group(1)))
    return queryset.filter(condition)


def _effective_condition():
    return Subquery(
        ShipmentReceipt.objects.filter(shipment=OuterRef("pk"), corrected_by__isnull=True)
        .order_by("-recorded_at", "-id")
        .values("condition")[:1]
    )


def shipment_rows(cleaned, user):
    queryset = Shipment.objects.select_related("request", "request__institution").annotate(
        planned_count=Count("allocations", distinct=True),
        item_count=Count("items", distinct=True),
        receipt_condition=_effective_condition(),
    )
    state = cleaned.get("state")
    if state == "open":
        queryset = queryset.filter(state__in=(Shipment.State.DRAFT, Shipment.State.PACKED))
    elif state:
        queryset = queryset.filter(state=state)
    if cleaned.get("institution"):
        queryset = queryset.filter(request__institution=cleaned["institution"])
    if cleaned.get("specimen"):
        accession = cleaned["specimen"]
        queryset = queryset.filter(
            Q(pk__in=ShipmentItem.objects.filter(accession=accession).values("shipment_id"))
            | Q(
                pk__in=Allocation.objects.filter(
                    specimen__accession=accession, shipment__isnull=False
                ).values("shipment_id")
            )
        )
    return queryset


def search_shipments(queryset, text):
    condition = Q(tracking__icontains=text) | Q(ship_to_institution__icontains=text)
    shipment = SHIPMENT_NUMBER.match(text.strip())
    if shipment and not text.strip().upper().startswith("RQ"):
        condition |= Q(pk=int(shipment.group(1)))
    request = REQUEST_NUMBER.match(text.strip())
    if request and not text.strip().upper().startswith("SH"):
        condition |= Q(request_id=int(request.group(1)))
    return queryset.filter(condition)


def with_review_state(queryset):
    released = ReturnReview.objects.filter(specimen_return=OuterRef("pk"), decision="release")
    reviewed = ReturnReview.objects.filter(specimen_return=OuterRef("pk"))
    return queryset.annotate(is_released=Exists(released), is_reviewed=Exists(reviewed))


def review_state(row):
    if row.is_released:
        return "released"
    return "kept" if row.is_reviewed else "awaiting"


def return_rows(cleaned, user):
    queryset = with_review_state(
        SpecimenReturn.objects.select_related("shipment", "item", "unit", "actor", "specimen")
    )
    review = cleaned.get("review")
    if review == "awaiting":
        queryset = queryset.filter(is_reviewed=False)
    elif review == "kept":
        queryset = queryset.filter(is_reviewed=True, is_released=False)
    elif review == "released":
        queryset = queryset.filter(is_released=True)
    return queryset


def search_returns(queryset, text):
    condition = Q(item__accession__icontains=text.strip())
    shipment = SHIPMENT_NUMBER.match(text.strip())
    if shipment:
        condition |= Q(shipment_id=int(shipment.group(1)))
    return queryset.filter(condition)


# --- CSV columns ----------------------------------------------------------------------------


def _amount(value):
    return format(value.normalize(), "f") if value is not None else ""


REVIEW_LABELS = dict(RETURN_STATES)

EXPORT = {
    "request": (
        ("request", lambda o: o.number),
        ("status", lambda o: o.get_status_display()),
        ("title", lambda o: o.title),
        ("investigator", lambda o: o.investigator.code),
        ("institution", lambda o: o.institution.code),
        ("received_via", lambda o: o.get_received_via_display()),
        ("lines", lambda o: o.line_count),
        ("allocated_or_picked", lambda o: o.open_count),
        ("shipped", lambda o: o.shipped_count),
        ("needed_by", lambda o: o.needed_by.isoformat() if o.needed_by else ""),
        ("agreement", lambda o: f"AG-{o.agreement_id:06d}" if o.agreement_id else ""),
        ("created_at", lambda o: local(o.created_at)),
    ),
    "shipment": (
        ("shipment", lambda o: o.number),
        ("state", lambda o: o.get_state_display()),
        ("request", lambda o: o.request.number),
        ("ship_to_institution", lambda o: o.ship_to_institution),
        ("courier", lambda o: o.courier),
        ("tracking", lambda o: o.tracking),
        ("specimens", lambda o: o.item_count if o.is_sent else o.planned_count),
        ("temperature_class", lambda o: o.get_temperature_class_display()),
        ("refrigerant", lambda o: o.get_refrigerant_display()),
        ("dry_ice_kg", lambda o: _amount(o.dry_ice_kg)),
        ("classification_declared_by_shipper", lambda o: o.get_classification_display()),
        ("package_count", lambda o: o.package_count if o.package_count is not None else ""),
        ("packed_at", lambda o: local(o.packed_at)),
        ("dispatched_at", lambda o: local(o.dispatched_at)),
        (
            "receipt_condition",
            lambda o: dict(ShipmentReceipt.Condition.choices).get(o.receipt_condition or "", ""),
        ),
    ),
    "return": (
        ("specimen", lambda o: o.item.accession),
        ("shipment", lambda o: o.shipment.number),
        ("returned_at", lambda o: local(o.occurred_at)),
        ("quantity", lambda o: _amount(o.quantity)),
        ("unit", lambda o: o.unit.code),
        ("thaws_reported", lambda o: o.thaw_count if o.thaw_count is not None else "not known"),
        ("review", lambda o: REVIEW_LABELS[review_state(o)]),
        ("recorded_by", lambda o: person(o.actor)),
    ),
}


# --- specs ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class ListSpec:
    key: str
    title: str
    noun: str
    build: Callable  # (cleaned, user) -> queryset
    empty: Callable
    columns: dict  # sort key -> ORM ordering field
    default_sort: str
    form_class: type
    search: Callable
    inline: tuple = ()
    more: tuple = ()
    export_columns: tuple = field(default=())


SPECS = {
    "request": ListSpec(
        key="request",
        title="Requests",
        noun="requests",
        build=request_rows,
        empty=SpecimenRequest.objects.none,
        columns={
            "number": "id",
            "status": "status",
            "title": "title",
            "investigator": "investigator__code",
            "needed": "needed_by",
            "created": "created_at",
        },
        default_sort="-number",
        form_class=RequestFilter,
        search=search_requests,
        inline=("status", "investigator", "institution"),
        more=("specimen", "to_pick", "awaiting_me"),
        export_columns=EXPORT["request"],
    ),
    "shipment": ListSpec(
        key="shipment",
        title="Shipments",
        noun="shipments",
        build=shipment_rows,
        empty=Shipment.objects.none,
        columns={
            "number": "id",
            "state": "state",
            "request": "request_id",
            "institution": "ship_to_institution",
            "packed": "packed_at",
            "dispatched": "dispatched_at",
        },
        default_sort="-number",
        form_class=ShipmentFilter,
        search=search_shipments,
        inline=("state", "institution"),
        more=("specimen",),
        export_columns=EXPORT["shipment"],
    ),
    "return": ListSpec(
        key="return",
        title="Shipments",
        noun="returns",
        build=return_rows,
        empty=SpecimenReturn.objects.none,
        columns={
            "specimen": "item__accession",
            "shipment": "shipment_id",
            "returned": "occurred_at",
        },
        default_sort="-returned",
        form_class=ReturnFilter,
        search=search_returns,
        inline=("review",),
        export_columns=EXPORT["return"],
    ),
}

# URL kind (distribution:export) -> list spec key.
EXPORT_KINDS = {"requests": "request", "shipments": "shipment", "returns": "return"}


def filtered(spec, data, user):
    """(form, queryset) for request-style filter data; an invalid form yields no rows."""
    form = spec.form_class(data, user=user)
    if not form.is_valid():
        return form, spec.empty()
    cleaned = form.cleaned_data
    queryset = spec.build(cleaned, user)
    if cleaned.get("q"):
        queryset = spec.search(queryset, cleaned["q"])
    return form, queryset


def ordered(spec, queryset, sort):
    """Apply an allowlisted sort with the primary key as the stable tie-break."""
    key = (sort or spec.default_sort).removeprefix("-")
    if key not in spec.columns:
        sort = spec.default_sort
        key = sort.removeprefix("-")
    prefix = "-" if sort.startswith("-") else ""
    return queryset.order_by(prefix + spec.columns[key], prefix + "pk")
