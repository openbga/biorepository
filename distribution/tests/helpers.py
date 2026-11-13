"""Shared helpers for the distribution service tests. All data is synthetic.

Fixtures (`people`, `world`, `dist`) come from conftest.py; this module holds plain functions and
the fast password hashing fixture (import it into a test module to enable it).
"""

import io
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from distribution import queries, services
from distribution.models import Consent, ConsentScopeCategory
from specimens import services as specimen_services
from specimens.models import Hold
from specimens.tests.conftest import (
    ago,
    catalogue,
    check_out,
    codes,
    consume,
    fresh,
    make_user,
    place,
    registration,
)

from .conftest import (
    allocate,
    approved_request,
    declaration,
    dispatch,
    new_request,
    packed_shipment,
    pick,
    save,
)

__all__ = [
    "PermissionDenied",
    "ValidationError",
    "allocate",
    "approved_request",
    "declaration",
    "dispatch",
    "new_request",
    "packed_shipment",
    "pick",
    "save",
    "ago",
    "catalogue",
    "codes",
    "fresh",
    "make_user",
    "timezone",
    "timedelta",
    "Decimal",
]


@pytest.fixture(autouse=True)
def fast_password_hashing(settings):
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


KEEP = object()


def refused(call, code):
    """Run `call`; it must raise a ValidationError carrying `code`. Returns the error."""
    with pytest.raises(ValidationError) as exc:
        call()
    assert code in codes(exc.value), (code, codes(exc.value), exc.value)
    return exc.value


def denied(call):
    with pytest.raises(PermissionDenied):
        call()


def blocker_params(error):
    """{accession: [codes]} from a `blocked` ValidationError."""
    assert "blocked" in codes(error), (codes(error), error)
    return error.error_list[0].params["blockers"]


def blocked(call):
    return blocker_params(refused(call, "blocked"))


def csv_file(text="a,b\n1,2\n", name="evidence.csv"):
    data = io.BytesIO(text.encode("utf-8"))
    data.name = name
    data.size = len(text.encode("utf-8"))
    return data


def make_specimen(
    people,
    world,
    label,
    *,
    box=KEEP,
    participant=KEEP,
    collection=KEEP,
    specimen_type=None,
    quantity="2.000000",
    placed=True,
):
    """A registered (and by default stored) specimen; box2 by default so the `dist` fixture's
    specimens stay in box 1. `participant`/`collection` may be None for an unset value."""
    actor = people["technician"]
    data = registration(world, quantity=quantity)
    changes = {}
    if participant is not KEEP:
        changes["participant_id"] = participant.pk if participant else None
    if collection is not KEEP:
        changes["collection_id"] = collection.pk if collection else None
    if specimen_type is not None:
        changes["specimen_type_id"] = specimen_type.pk
    specimen = specimen_services.register_specimen(
        actor=actor, data=replace(data, **changes), occurred_at=ago(270), reason="Received"
    )
    if placed:
        place(actor, specimen, world.position(label, world.box2 if box is KEEP else box))
    return fresh(specimen)


def gate(people, request, specimen, *, stage="allocate", on=None, item_id=None):
    """The set of blocker codes the gate reports for one specimen."""
    request.refresh_from_db()
    found = queries.blockers(
        actor=people["viewer"],
        request_id=request.pk,
        specimen_ids=[specimen.pk],
        stage=stage,
        on=on,
        item_id=item_id,
    )
    return {b.code for b in found[specimen.pk]}


def grant_disposal_approval(user):
    from django.contrib.auth.models import Permission

    user.user_permissions.add(
        Permission.objects.get(content_type__app_label="accounts", codename="approve_disposal")
    )
    return user


def consent_for(
    people,
    study,
    form,
    participant,
    *,
    decision="consented",
    answers=None,
    expires_on=None,
    restrictions="",
    actor=None,
    consented_on=None,
):
    """Record a consent decision; `answers` maps category code -> permitted/refused."""
    answers = answers or {}
    existing = Consent.objects.filter(participant=participant, study=study).first()
    return services.record_consent(
        actor=actor or people["manager"],
        participant_id=participant.pk,
        study_id=study.pk,
        form_id=form.pk,
        decision=decision,
        answers={ConsentScopeCategory.objects.get(code=c).pk: a for c, a in answers.items()},
        consented_on=consented_on or timezone.localdate() - timedelta(days=5),
        signer="participant",
        documentation="signed",
        source_reference="Consent binder 2 (synthetic)",
        restrictions=restrictions,
        expires_on=expires_on,
        expected_revision=existing.revision if existing else None,
        reason="Transcribed from the filed form",
    )


def make_study(people, code, *, basis="records", reference="", status="active"):
    values = {"code": code, "title": f"{code} (synthetic)", "consent_basis": basis}
    if reference:
        values["determination_reference"] = reference
    if status != "active":
        values["status"] = status
    return services.save_study(
        actor=people["manager"], study_id=None, expected_revision=None, values=values
    )


def govern(people, study, collection, reason="Governed under the study"):
    study.refresh_from_db()
    return services.link_collection(
        actor=people["manager"],
        study_id=study.pk,
        expected_revision=study.revision,
        collection_id=collection.pk,
        reason=reason,
    )


def make_form(people, study, categories, code="FORM", version="1"):
    return save(
        people["manager"],
        "consent_form",
        study=study.pk,
        code=code,
        version=version,
        title="Form (synthetic)",
        kind="study",
        categories=[c.pk for c in categories],
    )


def category(people, code, gates_use=True):
    return save(people["manager"], "category", code=code, name=code.title(), gates_use=gates_use)


def participant(people, code):
    return catalogue(people["manager"], "participant", code=code)


def collection(people, code):
    return catalogue(people["manager"], "collection", code=code, name=f"Collection {code}")


def request_with(
    people,
    dist,
    world,
    *,
    categories=("GENERAL",),
    studies=(),
    items=None,
    agreement=True,
    count=3,
):
    """Create, submit, approve (and optionally link the shared agreement to) a request with the
    given studies and lines."""
    tech = people["technician"]
    items = items or (services.ItemSpec(line=1, specimen_type_id=world.plasma.pk, count=count),)
    request = services.save_request(
        actor=tech,
        request_id=None,
        expected_revision=None,
        values={
            "title": "Specimens for assay development",
            "investigator": dist.investigator.pk,
            "purpose": "Method validation (synthetic)",
            "received_via": "email",
            "use_categories": [ConsentScopeCategory.objects.get(code=c).pk for c in categories],
            "studies": [s.pk for s in studies],
        },
        items=tuple(items),
    )
    return approve(people, request, agreement=dist.agreement if agreement else None)


def approve(people, request, agreement=None, decider="decider_manager"):
    request.refresh_from_db()
    services.submit_request(
        actor=people["technician"], request_id=request.pk, expected_revision=request.revision
    )
    request.refresh_from_db()
    services.decide_request(
        actor=people[decider],
        request_id=request.pk,
        expected_revision=request.revision,
        submission_number=request.latest_submission,
        decision="approve",
        conditions="",
        committee_reference="Access committee minute (synthetic)",
        conflicts_note="None declared",
        reason="Within the consented scope",
    )
    request.refresh_from_db()
    if agreement is not None:
        link(people, request, agreement)
    return request


def link(people, request, agreement, reason="Umbrella MTA"):
    request.refresh_from_db()
    services.link_agreement(
        actor=people["manager"],
        request_id=request.pk,
        expected_revision=request.revision,
        agreement_id=agreement.pk if agreement else None,
        reason=reason,
    )
    request.refresh_from_db()
    return request


def make_agreement(
    people,
    dist,
    *,
    execute=True,
    data_level="coded",
    effective_days=-10,
    expires_days=365,
    categories=(),
    institution=None,
):
    """A new agreement with the shared institution (synthetic MTA)."""
    today = timezone.localdate()
    agreement = services.save_agreement(
        actor=people["manager"],
        agreement_id=None,
        expected_revision=None,
        values={
            "kind": "mta",
            "title": "Synthetic agreement",
            "institution": (institution or dist.institution).pk,
            "data_level": data_level,
            "end_disposition": "destroy",
        },
        permitted_categories=tuple(ConsentScopeCategory.objects.get(code=c).pk for c in categories),
    )
    if execute:
        effective = today + timedelta(days=effective_days)
        agreement = services.execute_agreement(
            actor=people["manager"],
            agreement_id=agreement.pk,
            expected_revision=agreement.revision,
            executed_on=min(effective, today),
            effective_on=effective,
            expires_on=None if expires_days is None else today + timedelta(days=expires_days),
            document_reference="Agreements cabinet, folder 2 (synthetic)",
            reason="Signed by both parties",
        )
    return agreement


def hold_manually(actor, specimen, scope="use"):
    """A manual hold (no reference) placed now."""
    return specimen_services.place_hold(
        actor=actor,
        specimen_id=specimen.pk,
        expected_revision=fresh(specimen).revision,
        scope=scope,
        occurred_at=timezone.now(),
        reason="Quality review",
    )


def release_manually(actor, held):
    return specimen_services.release_hold(
        actor=actor,
        hold_id=held.pk,
        expected_revision=fresh(held.specimen).revision,
        occurred_at=timezone.now(),
        reason="Review complete",
    )


def active_holds(specimen):
    return Hold.objects.filter(specimen=specimen, release__isnull=True)


def exhausted_specimen(people, world, label):
    """Checked out and used up: status exhausted."""
    specimen = make_specimen(people, world, label)
    tech = people["technician"]
    check_out(tech, specimen, tech, occurred_at=ago(200))
    consume(tech, specimen, "2", occurred_at=ago(190))
    return fresh(specimen)


def picked_request(people, dist, world, count=2, **kwargs):
    """Approved request with `count` of the dist specimens allocated and picked."""
    request = approved_request(people, dist, world, **kwargs)
    allocations = allocate(people, request, dist.specimens[:count])
    pick(people, request, allocations)
    for allocation in allocations:
        allocation.refresh_from_db()
    return request, allocations


def shipment_for(people, dist, world, count=2, **declared):
    """Approved request, `count` specimens allocated, picked and packed."""
    request, allocations = picked_request(people, dist, world, count=count)
    shipment = packed_shipment(people, request, allocations, **declared)
    return request, allocations, shipment


def dispatched(people, dist, world, count=2, **declared):
    request, allocations, shipment = shipment_for(people, dist, world, count, **declared)
    dispatch(people, shipment)
    shipment.refresh_from_db()
    request.refresh_from_db()
    return request, allocations, shipment
