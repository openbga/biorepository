"""Synthetic demo distribution records, created through the services and idempotent by codes.

`seed_distribution` runs after the specimens and equipment demo seeds (`seed_demo`). With
obviously fake data it walks the distribution journey: scope categories, study DEMO-STUDY
governing collection DEMO under consent records, broad consent form DEMO-BROAD v1, participant
P-000123's consent (COMMERCIAL refused), institution EXU and investigator INV-001, an executed
material transfer agreement with a generated one-page synthetic PDF, a request created and
submitted by the technician and approved by the manager (who receives the explicit request
decision permission here, once), the stored DEMO specimens the gate allows (two after the
specimens demo; never the tube the aliquots came from) allocated, picked, packed (checked by
the manager), dispatched, the receipt recorded with one thawed vial, one returned and awaiting
review; and a second request awaiting a decision. Recognised by study DEMO-STUDY.

`seed_full_distribution` (for `seed_demo --full`, after the full specimens story) adds STUDY-A
governing COH-A under consent records (one participant withdrawn with holds, one refused),
STUDY-B governing COH-B under a study-level determination, PILOT left ungoverned, a second
institution with an agreement expiring within 60 days and a shipment packed awaiting dispatch.
Recognised by study STUDY-A.

Nothing here is a real agreement, consent or person. Accessions are whatever the specimens seed
produced; nothing assumes a number.
"""

from datetime import timedelta

from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.utils import timezone

from specimens import services as specimen_services
from specimens.models import Collection, Participant, Specimen
from storage.models import Occupancy

from . import queries, services
from .models import (
    ConsentForm,
    ConsentScopeCategory,
    Institution,
    Investigator,
    ShipmentItem,
    Study,
)

CATEGORIES = (
    ("GENERAL", "General research use", True),
    ("GENETIC", "Genetic research, including whole genome sequencing", True),
    ("COMMERCIAL", "Commercial use", True),
    ("EXTERNAL", "Sharing with researchers outside the repository", True),
    ("RECONTACT", "Re-contact about future research", False),
)
DEMO_REASON = "Synthetic demonstration data"


def _pdf(text):
    """A minimal one-page PDF stating `text` (ASCII), built without a rendering library."""
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return out


def _catalogue(actor, kind, model, code, **values):
    existing = model.objects.filter(code=code).first()
    if existing is not None:
        return existing
    return services.save_distribution_catalogue(
        actor=actor,
        kind=kind,
        object_id=None,
        expected_revision=None,
        values={"code": code, **values},
    )


def _categories(actor):
    return {
        code: _catalogue(actor, "category", ConsentScopeCategory, code, name=name, gates_use=gates)
        for code, name, gates in CATEGORIES
    }


def _study(actor, code, title, collection_code, **values):
    study = Study.objects.filter(code=code).first()
    if study is None:
        study = services.save_study(
            actor=actor,
            study_id=None,
            expected_revision=None,
            values={"code": code, "title": title, **values},
        )
    collection = Collection.objects.get(code=collection_code)
    if not study.collection_links.filter(collection=collection, ended_at__isnull=True).exists():
        services.link_collection(
            actor=actor,
            study_id=study.pk,
            expected_revision=study.revision,
            collection_id=collection.pk,
            reason=f"{collection_code} governed under {code} ({DEMO_REASON.lower()})",
        )
        study.refresh_from_db()
    return study


def _form(actor, study, code, categories):
    form = ConsentForm.objects.filter(study=study, code=code, version="1").first()
    if form is not None:
        return form
    return services.save_distribution_catalogue(
        actor=actor,
        kind="consent_form",
        object_id=None,
        expected_revision=None,
        values={
            "study": study.pk,
            "code": code,
            "version": "1",
            "title": f"{code} consent form (synthetic)",
            "kind": "broad",
            "categories": [c.pk for c in categories.values()],
        },
    )


def _consent(actor, participant_code, study, form, categories, *, refused=(), decision="consented"):
    participant = Participant.objects.get(code=participant_code)
    answers = {}
    if decision == "consented":
        answers = {
            c.pk: ("refused" if code in refused else "permitted") for code, c in categories.items()
        }
    return services.record_consent(
        actor=actor,
        participant_id=participant.pk,
        study_id=study.pk,
        form_id=form.pk,
        decision=decision,
        answers=answers,
        consented_on=timezone.localdate() - timedelta(days=60),
        signer="participant",
        documentation="signed",
        source_reference="Consent binder, demonstration only",
        restrictions="",
        expires_on=None,
        expected_revision=None,
        reason=f"Transcribed from the filed form ({DEMO_REASON.lower()})",
    )


def _parties(actor, code, name, investigator_code, investigator_name, email, phone):
    institution = _catalogue(
        actor,
        "institution",
        Institution,
        code,
        name=name,
        address=f"100 {name} Avenue, Example City (synthetic)",
        country="US",
    )
    investigator = _catalogue(
        actor,
        "investigator",
        Investigator,
        investigator_code,
        name=investigator_name,
        institution=institution.pk,
        email=email,
        phone=phone,
    )
    return institution, investigator


def _agreement(actor, institution, title, *, expires_in_days, data_level="coded"):
    today = timezone.localdate()
    agreement = services.save_agreement(
        actor=actor,
        agreement_id=None,
        expected_revision=None,
        values={
            "kind": "mta",
            "title": title,
            "institution": institution.pk,
            "data_level": data_level,
            "end_disposition": "destroy",
            "onward_transfer": "prohibited",
            "permitted_use": "Research use described in the linked requests (synthetic).",
            "provider_reference": "EX-MTA-PROVIDER (synthetic)",
            "reporting_due_on": today + timedelta(days=min(expires_in_days, 180)),
        },
        permitted_categories=(),
    )
    upload = SimpleUploadedFile(
        "synthetic-mta.pdf",
        _pdf("Synthetic MTA (demonstration only, not an agreement)"),
        content_type="application/pdf",
    )
    services.add_agreement_document(
        actor=actor,
        agreement_id=agreement.pk,
        kind="executed",
        title="Synthetic MTA (demonstration only, not an agreement)",
        note="Generated by the demonstration seed.",
        upload=upload,
    )
    agreement.refresh_from_db()
    return services.execute_agreement(
        actor=actor,
        agreement_id=agreement.pk,
        expected_revision=agreement.revision,
        executed_on=today - timedelta(days=20),
        effective_on=today - timedelta(days=20),
        expires_on=today + timedelta(days=expires_in_days),
        document_reference="",
        reason="Signed by both parties (synthetic demonstration)",
    )


def _request(technician, investigator, specimen_type, title, categories, *, count, study=None):
    request = services.save_request(
        actor=technician,
        request_id=None,
        expected_revision=None,
        values={
            "title": title,
            "investigator": investigator.pk,
            "purpose": "Assay development on stored plasma (synthetic demonstration).",
            "ethics_reference": "EX-IRB-0001 (synthetic)",
            "received_via": "email",
            "use_categories": [c.pk for c in categories],
            "studies": [study.pk] if study else [],
        },
        items=(
            services.ItemSpec(
                line=1,
                specimen_type_id=specimen_type.pk,
                count=count,
                study_id=study.pk if study else None,
            ),
        ),
    )
    request.refresh_from_db()
    services.submit_request(
        actor=technician, request_id=request.pk, expected_revision=request.revision
    )
    request.refresh_from_db()
    return request


def _approve(manager, request, agreement):
    services.decide_request(
        actor=manager,
        request_id=request.pk,
        expected_revision=request.revision,
        submission_number=request.latest_submission,
        decision="approve",
        conditions="Report results to the repository within a year.",
        committee_reference="Access committee minute 1 (synthetic)",
        conflicts_note="None declared",
        reason="Within the consented scope (synthetic demonstration)",
    )
    request.refresh_from_db()
    services.link_agreement(
        actor=manager,
        request_id=request.pk,
        expected_revision=request.revision,
        agreement_id=agreement.pk,
        reason="Covered by the umbrella MTA",
    )
    request.refresh_from_db()
    return request


def _candidates(request, item, count):
    """The first `count` stored specimens the gate allows for this line.

    Parents of derived specimens stay in storage: the demo sends aliquots and primaries without
    derivations, never the tube the aliquots came from.
    """
    queryset = (
        queries.candidate_queryset(request, item)
        .filter(open_checkout__isnull=True, children__isnull=True)
        .distinct()
    )
    stored = set(
        Occupancy.objects.filter(
            pk__in=queryset.values("occupancy_id"), position__isnull=False
        ).values_list("pk", flat=True)
    )
    rows = [s for s in queryset if s.occupancy_id in stored]
    found = queries.evaluate(request, [s.pk for s in rows], stage="allocate", item=item)
    chosen = []
    for specimen in rows:
        if found[specimen.pk]:
            continue
        if any(
            specimen_services.request_state(r, specimen) in ("pending", "approved")
            for r in specimen.disposals.all()
        ):
            continue
        chosen.append(specimen)
        if len(chosen) == count:
            break
    return chosen


def _allocate(manager, request, count):
    item = request.items.get(line=1)
    chosen = _candidates(request, item, count)
    if not chosen:
        return ()
    rows = services.allocate(
        actor=manager,
        request_id=request.pk,
        expected_revision=request.revision,
        item_id=item.pk,
        choices=tuple(services.Choice(s.pk, s.revision) for s in chosen),
        below_minimum_ok=True,
        occurred_at=timezone.now(),
        reason="Allocated for the approved request (synthetic demonstration)",
    )
    request.refresh_from_db()
    return rows


def _pick(technician, request, allocations):
    lines = []
    for allocation in allocations:
        specimen = Specimen.objects.get(pk=allocation.specimen_id)
        occupancy = Occupancy.objects.get(pk=specimen.occupancy_id)
        lines.append(
            services.PickLine(
                allocation_id=allocation.pk,
                expected_revision=specimen.revision,
                expected_occupancy_revision=occupancy.revision,
            )
        )
    services.pick(
        actor=technician,
        request_id=request.pk,
        expected_revision=request.revision,
        lines=tuple(lines),
        recipient_id=technician.pk,
        occurred_at=timezone.now(),
        reason="Retrieved for shipment (synthetic demonstration)",
    )
    request.refresh_from_db()


def _pack(technician, manager, request, allocations):
    shipment = services.create_shipment(
        actor=technician, request_id=request.pk, expected_revision=request.revision, values={}
    )
    shipment = services.set_shipment_contents(
        actor=technician,
        shipment_id=shipment.pk,
        expected_revision=shipment.revision,
        allocation_ids=tuple(a.pk for a in allocations),
    )
    return services.pack_shipment(
        actor=technician,
        shipment_id=shipment.pk,
        expected_revision=shipment.revision,
        declaration=services.PackDeclaration(
            temperature_class="minus80c",
            refrigerant="dry_ice",
            dry_ice_kg="5.00",
            classification="exempt_specimen",
            classification_note="As declared by the shipper (synthetic demonstration)",
            package_count=1,
            emergency_contact="Repository duty phone +1 555 0199",
            logger_ids="LOGGER-01 (synthetic)",
            storage_instructions="Store at -80 °C on arrival.",
            include_participant_codes=False,
            training_confirmed=True,
        ),
        checked_by_id=manager.pk,
        occurred_at=timezone.now(),
        reason="Packed on dry ice (synthetic demonstration)",
    )


@transaction.atomic
def seed_distribution(*, manager, technician):
    """The first distribution journey; returns True when it added the data."""
    if Study.objects.filter(code="DEMO-STUDY").exists():
        return False
    manager.user_permissions.add(
        Permission.objects.get(content_type__app_label="accounts", codename="approve_distribution")
    )
    categories = _categories(manager)
    study = _study(manager, "DEMO-STUDY", "Demonstration study (synthetic)", "DEMO")
    form = _form(manager, study, "DEMO-BROAD", categories)
    _consent(manager, "P-000123", study, form, categories, refused=("COMMERCIAL",))
    institution, investigator = _parties(
        manager,
        "EXU",
        "Example University",
        "INV-001",
        "Example Investigator",
        "investigator@example.invalid",
        "+1 555 0100",
    )
    agreement = _agreement(
        manager, institution, "Umbrella MTA with Example University", expires_in_days=365
    )
    plasma = Specimen.objects.filter(collection__code="DEMO").first().specimen_type
    wanted = [categories["GENERAL"], categories["EXTERNAL"]]
    request = _request(
        technician, investigator, plasma, "Plasma for assay development", wanted, count=3
    )
    request = _approve(manager, request, agreement)
    allocations = _allocate(manager, request, 3)
    if len(allocations) >= 2:
        picked = allocations[:2]
        _pick(technician, request, picked)
        shipment = _pack(technician, manager, request, picked)
        shipment.refresh_from_db()
        services.record_notification(
            actor=technician,
            shipment_id=shipment.pk,
            expected_revision=shipment.revision,
            kind="notified",
            occurred_at=timezone.now(),
            note="Shipping notice sent by email to the investigator (recorded, not sent by the "
            "system).",
        )
        shipment = services.dispatch_shipment(
            actor=technician,
            shipment_id=shipment.pk,
            expected_revision=shipment.revision,
            courier="Example Courier",
            tracking="EX-TRACK-0001",
            handed_to="Courier driver (synthetic)",
            occurred_at=timezone.now(),
            reason="Collected by the courier",
        )
        items = list(ShipmentItem.objects.filter(shipment=shipment).order_by("line"))
        services.record_receipt(
            actor=technician,
            shipment_id=shipment.pk,
            expected_revision=shipment.revision,
            values=services.ReceiptValues(
                received_at=timezone.now(),
                received_by_name="Example Receiving Desk",
                unpacked_by_name="Example Lab Technician",
                condition="discrepancy",
                refrigerant_on_arrival="adequate",
                temperature_note="Dry ice remaining on arrival",
                discrepancies="One vial arrived thawed (synthetic demonstration).",
                received_via="email",
                exceptions=((items[0].pk, "thawed", "Cap loose, contents thawed"),),
            ),
            reason="Recipient's verification email (synthetic)",
        )
        returned = Specimen.objects.get(pk=items[-1].specimen_id)
        services.record_return(
            actor=technician,
            item_id=items[-1].pk,
            expected_revision=returned.revision,
            quantity=items[-1].quantity,
            supplied_text=format(items[-1].quantity.normalize(), "f"),
            thaw_count=None,
            condition="Unopened, returned on dry ice",
            recipient_id=technician.pk,
            occurred_at=timezone.now(),
            reason="Not needed after all (synthetic demonstration)",
        )
    _request(
        technician,
        investigator,
        plasma,
        "Plasma for a pilot genetic study",
        [categories["GENETIC"], categories["EXTERNAL"]],
        count=2,
    )
    return True


@transaction.atomic
def seed_full_distribution(*, manager, technician, admin):
    """The larger demonstration's governance and one packed shipment; returns True when added.

    `admin` records the withdrawal so another manager can reinstate it in the demonstration."""
    seed_distribution(manager=manager, technician=technician)
    if Study.objects.filter(code="STUDY-A").exists():
        return False
    if not Collection.objects.filter(code="COH-A").exists():
        return False
    from specimens.seed import COHORT_A

    categories = {c.code: c for c in ConsentScopeCategory.objects.all()}
    study_a = _study(manager, "STUDY-A", "Cohort A study (synthetic)", "COH-A")
    form_a = _form(manager, study_a, "STUDY-A-MAIN", categories)
    present = set(Participant.objects.filter(code__in=COHORT_A).values_list("code", flat=True))
    codes = [c for c in COHORT_A if c in present]
    for code in codes[2:]:
        _consent(manager, code, study_a, form_a, categories, refused=("COMMERCIAL",))
    if len(codes) >= 2:
        _consent(manager, codes[0], study_a, form_a, categories, refused=("COMMERCIAL",))
        _consent(manager, codes[1], study_a, form_a, categories, decision="refused")
        consent = study_a.consents.get(participant__code=codes[0])
        services.withdraw_consent(
            actor=admin,
            consent_id=consent.pk,
            expected_revision=consent.revision,
            kind="no_use",
            effective_on=timezone.localdate(),
            received_via="Letter to the repository (synthetic)",
            occurred_at=timezone.now(),
            reason="Participant asked that their samples are no longer used (synthetic)",
        )
    _study(
        manager,
        "STUDY-B",
        "Cohort B study (synthetic)",
        "COH-B",
        consent_basis="determination",
        determination_reference="EX-IRB-DET-0001 (synthetic)",
    )
    institution, investigator = _parties(
        manager,
        "EXV",
        "Example Valley Institute",
        "INV-002",
        "Second Example Investigator",
        "second.investigator@example.invalid",
        "+1 555 0101",
    )
    agreement = _agreement(
        manager, institution, "Project MTA with Example Valley Institute", expires_in_days=45
    )
    plasma = Specimen.objects.filter(collection__code="COH-A").first().specimen_type
    request = _request(
        technician,
        investigator,
        plasma,
        "Cohort A plasma for biomarker work",
        [categories["GENERAL"], categories["EXTERNAL"]],
        count=4,
        study=study_a,
    )
    request = _approve(manager, request, agreement)
    allocations = _allocate(manager, request, 4)
    if len(allocations) >= 2:
        picked = allocations[:2]
        _pick(technician, request, picked)
        _pack(technician, manager, request, picked)
    return True
