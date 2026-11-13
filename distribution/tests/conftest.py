"""Distribution test fixtures on top of the specimens fixtures (imported, not copied).

`dist` builds a governed world: study DEMO-STUDY (basis records) governing collection DEMO, a
broad consent form asking GENERAL, GENETIC, COMMERCIAL and RECONTACT, P-000123 consented
(GENERAL and GENETIC permitted, COMMERCIAL refused, RECONTACT permitted), institution EXU with
investigator INV-001, an executed MTA, and three stored plasma specimens of P-000123. People:
the specimens `people` plus `decider` (technician with the explicit request decision grant) and
`decider_manager` (manager with the grant). All data is synthetic.
"""

from dataclasses import dataclass, field
from datetime import timedelta

import pytest
from django.contrib.auth.models import Permission
from django.utils import timezone

from distribution import services
from distribution.models import ConsentScopeCategory
from specimens.tests import conftest as specimen_fixtures
from specimens.tests.conftest import (  # noqa: F401  (helpers re-exported to the tests)
    PASSWORD,
    ago,
    catalogue,
    codes,
    fresh,
    make_user,
    occ_rev,
    place,
    register,
)

world = specimen_fixtures.world


def grant_decisions(user):
    user.user_permissions.add(
        Permission.objects.get(content_type__app_label="accounts", codename="approve_distribution")
    )
    return user


@pytest.fixture
def people(db):
    people = {
        "admin": make_user("sp-admin", "admin"),
        "manager": make_user("sp-manager", "manager"),
        "technician": make_user("sp-technician", "technician"),
        "technician2": make_user("sp-technician-2", "technician"),
        "approver": make_user("sp-approver", "technician", approver=True),
        "superuser": make_user("sp-superuser", "admin", superuser=True),
        "viewer": make_user("sp-viewer", "viewer"),
        "nobody": make_user("sp-nobody"),
    }
    people["decider"] = grant_decisions(make_user("dist-decider", "technician"))
    people["decider_manager"] = grant_decisions(make_user("dist-decider-manager", "manager"))
    people["manager2"] = make_user("dist-manager-2", "manager")
    return people


def save(actor, catalogue_kind, **values):
    return services.save_distribution_catalogue(
        actor=actor, kind=catalogue_kind, object_id=None, expected_revision=None, values=values
    )


@dataclass
class Dist:
    study: object
    form: object
    categories: dict
    consent: object
    institution: object
    investigator: object
    agreement: object
    specimens: list = field(default_factory=list)


def stored(actor, world, count, start=1, box=None, **kwargs):
    labels = ["A1", "A2", "A3", "B1", "B2", "B3", "C1", "C2", "C3"]
    result = []
    for label in labels[start - 1 : start - 1 + count]:
        specimen = register(actor, world, **kwargs)
        place(actor, specimen, world.position(label, box))
        result.append(fresh(specimen))
    return result


@pytest.fixture
def dist(people, world):
    m = people["manager"]
    categories = {
        code: save(m, "category", code=code, name=code.title(), gates_use=code != "RECONTACT")
        for code in ("GENERAL", "GENETIC", "COMMERCIAL", "RECONTACT")
    }
    study = services.save_study(
        actor=m,
        study_id=None,
        expected_revision=None,
        values={"code": "DEMO-STUDY", "title": "Demonstration study (synthetic)"},
    )
    services.link_collection(
        actor=m,
        study_id=study.pk,
        expected_revision=study.revision,
        collection_id=world.collection.pk,
        reason="Collection governed under the study",
    )
    form = save(
        m,
        "consent_form",
        study=study.pk,
        code="DEMO-BROAD",
        version="1",
        title="Broad consent (synthetic)",
        kind="broad",
        categories=[c.pk for c in categories.values()],
    )
    record = services.record_consent(
        actor=m,
        participant_id=world.participant.pk,
        study_id=study.pk,
        form_id=form.pk,
        decision="consented",
        answers={
            categories["GENERAL"].pk: "permitted",
            categories["GENETIC"].pk: "permitted",
            categories["COMMERCIAL"].pk: "refused",
            categories["RECONTACT"].pk: "permitted",
        },
        consented_on=timezone.localdate() - timedelta(days=30),
        signer="participant",
        documentation="signed",
        source_reference="Consent binder 1 (synthetic)",
        restrictions="",
        expires_on=None,
        expected_revision=None,
        reason="Transcribed from the filed form",
    )
    institution = save(
        m, "institution", code="EXU", name="Example University", address="100 Example Avenue"
    )
    investigator = save(
        m,
        "investigator",
        code="INV-001",
        name="Example Investigator",
        institution=institution.pk,
        email="investigator@example.invalid",
        phone="+1 555 0100",
    )
    agreement = services.save_agreement(
        actor=m,
        agreement_id=None,
        expected_revision=None,
        values={
            "kind": "mta",
            "title": "Synthetic MTA",
            "institution": institution.pk,
            "data_level": "coded",
            "end_disposition": "destroy",
        },
        permitted_categories=(),
    )
    agreement = services.execute_agreement(
        actor=m,
        agreement_id=agreement.pk,
        expected_revision=agreement.revision,
        executed_on=timezone.localdate() - timedelta(days=10),
        effective_on=timezone.localdate() - timedelta(days=10),
        expires_on=timezone.localdate() + timedelta(days=365),
        document_reference="Agreements cabinet, folder 1 (synthetic)",
        reason="Signed by both parties",
    )
    specimens = stored(people["technician"], world, 3)
    return Dist(
        study=study,
        form=form,
        categories=categories,
        consent=record.consent,
        institution=institution,
        investigator=investigator,
        agreement=agreement,
        specimens=specimens,
    )


def new_request(actor, dist, world, *, count=3, categories=("GENERAL",), agreement=True):
    request = services.save_request(
        actor=actor,
        request_id=None,
        expected_revision=None,
        values={
            "title": "Plasma for assay development",
            "investigator": dist.investigator.pk,
            "purpose": "Method validation (synthetic)",
            "received_via": "email",
            "use_categories": [ConsentScopeCategory.objects.get(code=c).pk for c in categories],
        },
        items=(services.ItemSpec(line=1, specimen_type_id=world.plasma.pk, count=count),),
    )
    return request


def approved_request(people, dist, world, **kwargs):
    request = new_request(people["technician"], dist, world, **kwargs)
    request.refresh_from_db()
    services.submit_request(
        actor=people["technician"], request_id=request.pk, expected_revision=request.revision
    )
    request.refresh_from_db()
    services.decide_request(
        actor=people["decider_manager"],
        request_id=request.pk,
        expected_revision=request.revision,
        submission_number=request.latest_submission,
        decision="approve",
        conditions="",
        committee_reference="Access committee minute 1 (synthetic)",
        conflicts_note="None declared",
        reason="Within the consented scope",
    )
    request.refresh_from_db()
    if kwargs.get("agreement", True):
        services.link_agreement(
            actor=people["manager"],
            request_id=request.pk,
            expected_revision=request.revision,
            agreement_id=dist.agreement.pk,
            reason="Umbrella MTA",
        )
        request.refresh_from_db()
    return request


def allocate(people, request, specimens, occurred_at=None):
    request.refresh_from_db()
    item = request.items.get(line=1)
    rows = services.allocate(
        actor=people["manager"],
        request_id=request.pk,
        expected_revision=request.revision,
        item_id=item.pk,
        choices=tuple(services.Choice(s.pk, fresh(s).revision) for s in specimens),
        below_minimum_ok=False,
        occurred_at=occurred_at or timezone.now(),
        reason="Allocated for the approved request",
    )
    request.refresh_from_db()
    return rows


def pick(people, request, allocations, actor=None, missing=()):
    request.refresh_from_db()
    lines = []
    for allocation in allocations:
        specimen = fresh(allocation.specimen)
        lines.append(
            services.PickLine(
                allocation_id=allocation.pk,
                expected_revision=specimen.revision,
                expected_occupancy_revision=occ_rev(specimen),
                missing=allocation.pk in missing,
            )
        )
    actor = actor or people["technician"]
    result = services.pick(
        actor=actor,
        request_id=request.pk,
        expected_revision=request.revision,
        lines=tuple(lines),
        recipient_id=actor.pk,
        occurred_at=timezone.now(),
        reason="Retrieved for shipment",
    )
    request.refresh_from_db()
    return result


def declaration(**overrides):
    values = {
        "temperature_class": "minus80c",
        "refrigerant": "dry_ice",
        "dry_ice_kg": "5.00",
        "classification": "exempt_specimen",
        "classification_note": "",
        "package_count": 1,
        "emergency_contact": "Repository duty phone +1 555 0199",
        "logger_ids": "",
        "storage_instructions": "Store at -80 °C on arrival",
        "include_participant_codes": False,
        "training_confirmed": True,
    }
    values.update(overrides)
    return services.PackDeclaration(**values)


def packed_shipment(people, request, allocations, *, checker=None, **declared):
    request.refresh_from_db()
    shipment = services.create_shipment(
        actor=people["technician"],
        request_id=request.pk,
        expected_revision=request.revision,
        values={},
    )
    shipment = services.set_shipment_contents(
        actor=people["technician"],
        shipment_id=shipment.pk,
        expected_revision=shipment.revision,
        allocation_ids=tuple(a.pk for a in allocations),
    )
    shipment = services.pack_shipment(
        actor=people["technician"],
        shipment_id=shipment.pk,
        expected_revision=shipment.revision,
        declaration=declaration(**declared),
        checked_by_id=(checker or people["manager"]).pk,
        occurred_at=timezone.now(),
        reason="Packed on dry ice",
    )
    return shipment


def dispatch(people, shipment, actor=None):
    shipment.refresh_from_db()
    return services.dispatch_shipment(
        actor=actor or people["technician"],
        shipment_id=shipment.pk,
        expected_revision=shipment.revision,
        courier="Example Courier",
        tracking="EX123456789",
        handed_to="Courier driver",
        occurred_at=timezone.now(),
        reason="Collected by the courier",
    )
