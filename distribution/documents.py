"""Pick list and shipment manifest: the data both the document pages and the PDFs show, and the
PDF rendering.

A pick list is a printout of one request's allocated specimens in walking order; it is not a
record. A manifest of a draft or packed shipment shows the current contents and is marked "Not
dispatched"; once dispatched it is built only from the shipment's append-only ShipmentItem rows
and its own fields, so later catalogue or specimen corrections never change what was sent.
Neither document classifies a shipment or states that anything complies with a rule: the
classification printed is the one the shipper declared.

PDFs are rendered with WeasyPrint outside any transaction, from the application's templates and
stylesheet with escaped data; external and file fetches are refused. Each successful render is
recorded (OutputRecord with identifiers and counts, never the bytes) and audited on the request
or shipment in a short transaction of its own.
"""

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone

from accounts.roles import require_role
from core.audit import record_event
from core.models import Repository

from . import queries
from .models import Allocation, OutputRecord, Shipment, SpecimenRequest
from .services import READ_ROLES, manifest_participant_code

STYLESHEET = Path(__file__).resolve().parent / "documents.css"
TEMPLATE_VERSION = 1


@dataclass(frozen=True)
class ManifestLine:
    line: int
    accession: str
    specimen_type: str
    container: str
    quantity: Decimal
    unit: str
    study_code: str
    participant_code: str


def repository_name():
    repository = Repository.objects.filter(pk=1).only("name").first()
    return repository.name if repository else ""


def pick_list_context(*, actor, request_id):
    require_role(actor, *READ_ROLES)
    request = SpecimenRequest.objects.select_related("investigator", "institution").get(
        pk=request_id
    )
    rows = queries.pick_list(actor=actor, request_id=request_id)
    return {
        "request_obj": request,
        "rows": rows,
        "blocked_count": sum(1 for row in rows if row.blockers),
        "repository_name": repository_name(),
        "generated_at": timezone.localtime(),
    }


def _planned_lines(shipment, show_participants):
    allocations = list(
        Allocation.objects.filter(shipment=shipment, state=Allocation.State.PICKED).select_related(
            "specimen",
            "specimen__specimen_type",
            "specimen__container_type",
            "specimen__unit",
            "specimen__participant",
            "specimen__collection",
        )
    )
    specimens = [a.specimen for a in allocations]
    studies = queries.governing_studies(specimens)
    lines = []
    for number, specimen in enumerate(sorted(specimens, key=lambda s: s.accession), start=1):
        study = studies.get(specimen.pk)
        lines.append(
            ManifestLine(
                line=number,
                accession=specimen.accession,
                specimen_type=str(specimen.specimen_type),
                container=str(specimen.container_type),
                quantity=specimen.quantity_on_hand,
                unit=specimen.unit.code,
                study_code=study.code if study else "",
                participant_code=(
                    manifest_participant_code(specimen, shipment) if show_participants else ""
                ),
            )
        )
    return lines


def _sent_lines(shipment):
    return [
        ManifestLine(
            line=item.line,
            accession=item.accession,
            specimen_type=item.specimen_type,
            container=item.container,
            quantity=item.quantity,
            unit=item.unit,
            study_code=item.study_code,
            participant_code=item.participant_code,
        )
        for item in shipment.items.order_by("line")
    ]


def manifest_context(*, actor, shipment_id):
    """Everything the manifest shows. Sent shipments: ShipmentItem rows and Shipment fields only."""
    require_role(actor, *READ_ROLES)
    shipment = Shipment.objects.select_related("packed_by", "checked_by", "dispatched_by").get(
        pk=shipment_id
    )
    sent = shipment.is_sent
    # Before dispatch the codes print only while the request's agreement allows them (pack
    # checks it, but the agreement can change after packing).
    show = shipment.include_participant_codes and (
        sent or queries.participant_code_blocker(shipment) is None
    )
    lines = _sent_lines(shipment) if sent else _planned_lines(shipment, show)
    return {
        "shipment": shipment,
        "request_number": f"RQ-{shipment.request_id:06d}",
        "lines": lines,
        "studies": sorted({line.study_code for line in lines if line.study_code}),
        "show_participants": show,
        "sent": sent,
        "repository_name": repository_name(),
        "generated_at": timezone.localtime(),
    }


def _pdf(html):
    from weasyprint import CSS, HTML
    from weasyprint.urls import URLFetcher

    class DenyFetcher(URLFetcher):
        def fetch(self, url, headers=None):
            raise ValueError("Documents cannot load external resources.")

    fetcher = DenyFetcher()
    stylesheet = CSS(string=STYLESHEET.read_text(), url_fetcher=fetcher)
    return HTML(string=html, url_fetcher=fetcher).write_pdf(stylesheets=[stylesheet])


def _record(actor, kind, count, details, obj, action, reason):
    with transaction.atomic():
        record = OutputRecord.objects.create(kind=kind, actor=actor, count=count, details=details)
        record_event(
            actor=actor,
            action=action,
            obj=obj,
            after={"output": str(record.pk), "count": count, **details},
            reason=reason,
        )


def render_pick_list(*, actor, request_id):
    """PDF bytes of the request's pick list (allocated specimens in walking order)."""
    context = pick_list_context(actor=actor, request_id=request_id)
    pdf = _pdf(render_to_string("distribution/pdf/pick_list.html", context))
    request = context["request_obj"]
    _record(
        actor,
        OutputRecord.Kind.PICK_LIST,
        len(context["rows"]),
        {
            "request": request.number,
            "allocations": [str(row.allocation.pk) for row in context["rows"]],
            "template_version": TEMPLATE_VERSION,
        },
        request,
        "distribution.pick_list_printed",
        "Pick list generated (printing is not recorded)",
    )
    return pdf


def render_manifest(*, actor, shipment_id):
    """PDF bytes of the shipment's manifest; draft and packed ones are marked Not dispatched."""
    context = manifest_context(actor=actor, shipment_id=shipment_id)
    pdf = _pdf(render_to_string("distribution/pdf/manifest.html", context))
    shipment = context["shipment"]
    _record(
        actor,
        OutputRecord.Kind.MANIFEST,
        len(context["lines"]),
        {
            "shipment": shipment.number,
            "shipment_revision": shipment.revision,
            "state": shipment.state,
            "from_snapshot": context["sent"],
            "template_version": TEMPLATE_VERSION,
        },
        shipment,
        "distribution.manifest_printed",
        "Manifest generated (printing is not recorded)",
    )
    return pdf
