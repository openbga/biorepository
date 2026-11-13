"""`seed_demo --full`: a synthetic working repository made through the services, once."""

from collections import Counter
from datetime import timedelta

import pytest
from django.apps import apps
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.utils import timezone

from accounts.models import User
from core.models import AuditEvent
from distribution import services as distribution_services
from distribution.models import (
    Agreement,
    Allocation,
    Consent,
    Shipment,
    SpecimenRequest,
    Study,
)
from equipment import queries as equipment_queries
from equipment.models import (
    AffectedSpecimen,
    Equipment,
    Excursion,
    LN2Entry,
    ServiceEvent,
    StatusChange,
    TemperatureReading,
)
from specimens import services
from specimens.models import (
    Correction,
    CustodyEvent,
    DisposalRequest,
    Hold,
    ImportBatch,
    ImportCommit,
    Participant,
    Specimen,
    ThawEvent,
)
from storage.models import Box, MoveEvent, Position, StorageUnit

pytestmark = pytest.mark.django_db

SHARED = "synthetic-test-password-only"
ADMIN_ONLY = "separate-admin-password-only"


@pytest.fixture(autouse=True)
def plain_static_storage(settings):
    """Render the real sign-in page without a collectstatic manifest."""
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


@pytest.fixture
def password(monkeypatch):
    monkeypatch.setenv("DEMO_PASSWORD", SHARED)
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", ADMIN_ONLY)
    monkeypatch.setenv("ALLOW_DEMO_RESET", "true")


def row_counts():
    return {
        model._meta.label: model.objects.count()
        for model in apps.get_models()
        if not model._meta.proxy and model._meta.managed
    }


def passwords():
    return dict(User.objects.values_list("username", "password"))


def test_full_seed_builds_a_working_repository_once_and_reset_restores_it(password, client):
    call_command("seed_demo", full=True, announce_accounts=True, verbosity=0)
    seeded = row_counts()
    now = timezone.now()

    # A few hundred specimens of several types, from coded participants only.
    assert 300 <= Specimen.objects.count() <= 500
    assert len(set(Specimen.objects.values_list("specimen_type__code", flat=True))) >= 6
    assert Participant.objects.count() >= 60
    assert not Participant.objects.exclude(code__regex=r"^P-\d{6}$").exists()
    assert not User.objects.exclude(email__endswith="@example.invalid").exists()
    statuses = Counter(Specimen.objects.values_list("status", flat=True))
    assert statuses["available"] > 250 and statuses["exhausted"] >= 1 and statuses["disposed"] >= 5
    assert Specimen.objects.filter(parent__isnull=False, derivation_kind="aliquot").count() >= 60
    assert Specimen.objects.filter(derivation_kind="derivative", unit__code="uL").count() >= 4
    assert Specimen.objects.filter(participant__isnull=True).exists()  # a pooled reference
    # Thaw history: observed thaws, and a pilot whose history before registration is unknown.
    assert ThawEvent.objects.filter(kind="thaw").count() >= 8
    assert ThawEvent.objects.filter(kind="baseline", count__isnull=True).exists()

    # Storage: several tanks, freezers and a refrigerator, boxes from empty to full, one retired.
    technologies = set(StorageUnit.objects.values_list("technology", flat=True))
    assert {"ln2_vapor", "ln2_liquid", "mechanical"} <= technologies
    assert StorageUnit.objects.count() >= 6
    assert Box.objects.filter(is_active=True).count() >= 20 and Box.objects.filter(is_active=False)
    fills = []
    for box in Box.objects.filter(is_active=True):
        fills.append(
            Position.objects.filter(box=box, occupancy__isnull=False).count()
            / (box.container_type.capacity - Position.objects.filter(box=box, blocked=True).count())
        )
    assert min(fills) == 0 and max(fills) == 1 and len([f for f in fills if 0.2 < f < 0.8]) >= 6
    assert Position.objects.filter(blocked=True).count() >= 3
    kinds = Counter(MoveEvent.objects.values_list("kind", flat=True))
    assert kinds["container_move"] >= 3 and kinds["correction"] >= 2

    # Check-outs in progress, some for weeks, and a few handed on.
    out = Specimen.objects.filter(open_checkout__isnull=False).select_related("open_checkout")
    ages = sorted(now - s.open_checkout.occurred_at for s in out)
    assert len(ages) >= 8 and ages[0] < timedelta(days=1) and ages[-1] > timedelta(days=21)
    assert CustodyEvent.objects.filter(kind="transfer").exists()
    # Holds: active in both scopes, and released ones that keep their history.
    active = Hold.objects.filter(release__isnull=True)
    # Disposal and all-use holds placed by hand; use holds belong to a withdrawal or a return.
    assert set(active.values_list("scope", flat=True)) == {"disposal", "all_use", "use"}
    assert not active.filter(scope="use", reference="").exists()
    assert Hold.objects.filter(release__isnull=False).exists()

    # Disposal requests in every state, nobody approving their own.
    states = Counter(services.request_state(r) for r in DisposalRequest.objects.all())
    assert {"pending", "approved", "stale", "rejected", "cancelled", "executed"} <= set(states)
    assert states["executed"] >= 6 and states["pending"] >= 3
    for request in DisposalRequest.objects.filter(decision__decision__in=("approve", "reject")):
        assert request.decision.actor_id != request.requested_by_id
    assert any(
        r.specimen.holds.filter(release__isnull=True).exists()
        for r in DisposalRequest.objects.filter(decision__isnull=True)
    )

    # Imports: committed shipments, one valid batch waiting for its commit, one with errors.
    batches = ImportBatch.objects.all()
    assert ImportCommit.objects.count() >= 3
    committed = set(ImportCommit.objects.values_list("batch_id", flat=True))
    assert any(b.is_valid and b.pk not in committed for b in batches)
    assert any(b.errors for b in batches)

    # Reasoned corrections beside the records they correct, and every step audited.
    assert Correction.objects.count() >= 3
    assert CustodyEvent.objects.filter(kind="correction").exists()
    assert ThawEvent.objects.filter(kind="correction").exists()
    assert AuditEvent.objects.count() > 1500
    assert not AuditEvent.objects.filter(actor__isnull=True).exists()

    # Equipment for every storage unit, with its history; nothing here is a default limit.
    assert StorageUnit.objects.count() == Equipment.objects.exclude(storage_unit=None).count()
    assert {"ln2_tank", "freezer", "monitor", "supply_dewar"} <= set(
        Equipment.objects.values_list("kind", flat=True)
    )
    assert ServiceEvent.objects.count() >= 10 and LN2Entry.objects.count() >= 30
    assert TemperatureReading.objects.count() >= 100
    assert StatusChange.objects.count() >= 2  # out of service, then back in service
    assert equipment_queries.due_queryset(state="overdue").exists()
    assert equipment_queries.uncovered_readings().exists()
    closed = Excursion.objects.filter(status="closed")
    assert closed.exists() and Excursion.objects.filter(status="open").exists()
    assert all(e.closed_by_id != e.submitted_by_id for e in closed)
    assert AffectedSpecimen.objects.exists()

    # Distribution: governed cohorts, a withdrawal that holds the participant's specimens, an
    # agreement near its end, and requests in more than one state.
    assert {"records", "determination"} <= set(
        Study.objects.values_list("consent_basis", flat=True)
    )
    assert Consent.objects.filter(status="withdrawn").count() == 1
    assert Consent.objects.filter(status="refused").exists()
    withdrawn_holds = Hold.objects.filter(
        release__isnull=True, scope="use", reference__startswith="Consent withdrawn"
    )
    assert withdrawn_holds.count() >= 1
    assert {"approved", "submitted"} <= set(
        SpecimenRequest.objects.values_list("status", flat=True)
    )
    assert {"packed", "received"} <= {s.state for s in Shipment.objects.all()}
    assert Specimen.objects.filter(status="distributed").exists()
    assert Allocation.objects.filter(state__in=("allocated", "picked", "shipped")).count() >= 5
    soon = timezone.localdate() + timedelta(days=60)
    assert Agreement.objects.filter(status="executed", expires_on__lte=soon).exists()
    # A visitor can decide a request with the manager account, never with the technician's.
    waiting = SpecimenRequest.objects.get(status="submitted")
    assert waiting.created_by.username == "demo_technician"

    # The sign-in page lists the shared accounts by role and never the administrator's.
    page = client.get("/accounts/login/").content.decode()
    for name in ("demo_manager", "demo_technician", "demo_viewer"):
        assert name in page
    assert "demo_admin" not in page and ADMIN_ONLY not in page
    assert "request decisions" in page

    # Run again: nothing is added and nothing already there changes.
    before = passwords()
    call_command("seed_demo", full=True, announce_accounts=True, verbosity=0)
    assert row_counts() == seeded and passwords() == before

    # The daily reset returns a used demonstration to the same state.
    technician = User.objects.get(username="demo_technician")
    technician.set_password("changed-by-a-visitor")
    technician.save()
    call_command("reset_demo", interactive=False, full=True, announce_accounts=True, verbosity=0)
    assert row_counts() == seeded
    assert User.objects.get(username="demo_technician").check_password(SHARED)
    assert "demo_manager" in client.get("/accounts/login/").content.decode()


def test_plain_seed_stays_small(password):
    call_command("seed_demo", verbosity=0)
    assert Specimen.objects.count() < 15 and Box.objects.count() == 3
    assert Equipment.objects.filter(storage_unit__isnull=False).count() == 2
    assert Equipment.objects.count() <= 4
    assert not Hold.objects.filter(scope="all_use").exists()
    # Distribution: one study, one shipment, a request waiting for the manager's decision.
    assert Study.objects.count() == 1 and Shipment.objects.count() == 1
    assert SpecimenRequest.objects.filter(status="submitted").count() == 1


def test_two_accounts_take_the_waiting_request_to_a_decision(password):
    """The sign-in page invites a visitor to try a request: the technician's cannot decide it."""
    call_command("seed_demo", verbosity=0)
    technician = User.objects.get(username="demo_technician")
    manager = User.objects.get(username="demo_manager")
    request = SpecimenRequest.objects.get(status="submitted")
    decision = {
        "request_id": request.pk,
        "expected_revision": request.revision,
        "submission_number": request.latest_submission,
        "decision": "approve",
        "conditions": "",
        "committee_reference": "",
        "conflicts_note": "",
        "reason": "Within the consented scope (synthetic test)",
    }
    with pytest.raises(PermissionDenied):
        distribution_services.decide_request(actor=technician, **decision)
    distribution_services.decide_request(actor=manager, **decision)
    request.refresh_from_db()
    assert request.status == "approved"
