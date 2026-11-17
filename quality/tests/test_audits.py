from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.urls import reverse

from core.models import AuditEvent
from quality import services
from quality.models import AuditFinalization, InventoryAudit
from specimens.tests.conftest import check_in, check_out, move_box, stored
from storage.models import Box, Occupancy, Position

pytestmark = pytest.mark.django_db


def create(people, world, **kwargs):
    return services.create_audit(
        actor=people["technician"],
        box_id=world.box.pk,
        reason="Scheduled synthetic inventory check",
        **kwargs,
    )


def expectations(audit):
    return {position["id"]: position["expected"] for position in audit.positions}


def complete(people, audit, observations=None, **kwargs):
    return services.complete_audit(
        actor=people["technician"],
        audit_id=audit.pk,
        observations=expectations(audit) if observations is None else observations,
        reason="Checked every position",
        **kwargs,
    )


def test_snapshot_and_results_are_evidence_only(people, world):
    specimen = stored(people["technician"], world)
    before = list(Occupancy.objects.values())
    positions = list(Position.objects.values())
    audit = create(people, world)
    assert audit.positions[0]["expected"] == specimen.accession
    assert audit.positions[0]["expected_specimen_id"] == str(specimen.pk)
    assert audit.location["box"]["id"] == str(world.box.pk)
    observed = expectations(audit)
    observed[audit.positions[0]["id"]] = ""
    observed[audit.positions[1]["id"]] = "UNKNOWN-SYNTHETIC-BARCODE"
    complete(
        people, audit, observed, discrepancy_explanation="One missing; one unknown label observed"
    )
    audit = services.get_audit(actor=people["viewer"], audit_id=audit.pk)
    assert audit.status == "completed"
    assert audit.finalization.counts == {"match": 7, "missing": 1, "unexpected": 1, "mismatch": 0}
    assert list(Occupancy.objects.values()) == before
    assert list(Position.objects.values()) == positions
    specimen.refresh_from_db()
    assert specimen.status == "available"
    assert (
        AuditEvent.objects.filter(
            object_type="quality.inventoryaudit", object_id=str(audit.pk)
        ).count()
        == 2
    )


@pytest.mark.parametrize("kind", ["missing", "duplicate", "unchecked", "explanation"])
def test_complete_validation_rolls_back(people, world, kind):
    stored(people["technician"], world)
    audit = create(people, world)
    observed = expectations(audit)
    if kind == "missing":
        observed.pop(audit.positions[-1]["id"])
    elif kind == "duplicate":
        observed[audit.positions[1]["id"]] = observed[audit.positions[0]["id"]].lower()
    elif kind == "unchecked":
        observed[audit.positions[-1]["id"]] = None
    else:
        observed[audit.positions[0]["id"]] = "ANOTHER-SYNTHETIC-CODE"
    with pytest.raises(ValidationError):
        complete(people, audit, observed)
    assert not AuditFinalization.objects.exists()
    assert AuditEvent.objects.filter(action="quality.audit_completed").count() == 0


def test_mismatch_and_immutable_snapshot(people, world):
    stored(people["technician"], world)
    audit = create(people, world)
    observed = expectations(audit)
    observed[audit.positions[0]["id"]] = "UNKNOWN-SYNTHETIC-CODE"
    complete(people, audit, observed, discrepancy_explanation="Different label at A1")
    audit.refresh_from_db()
    assert audit.finalization.counts["mismatch"] == 1
    assert audit.positions[0]["expected"].startswith("VP-")


@pytest.mark.parametrize("table", ["snapshot", "result"])
def test_history_database_guards(people, world, table):
    audit = create(people, world)
    complete(people, audit)
    obj = audit if table == "snapshot" else audit.finalization
    with pytest.raises(DatabaseError), transaction.atomic():
        type(obj).objects.filter(pk=obj.pk).update(reason="Changed")
    with pytest.raises(DatabaseError), transaction.atomic():
        type(obj).objects.filter(pk=obj.pk).delete()


@pytest.mark.parametrize("action", ["create", "complete", "cancel", "read"])
@pytest.mark.parametrize("role", ["nobody", "bare_superuser", "viewer"])
def test_roles_enforced_before_reads(people, world, action, role):
    actor = people["nobody"] if role == "bare_superuser" else people[role]
    if role == "bare_superuser":
        actor.is_superuser = True
        actor.save()
    if role == "viewer" and action == "read":
        assert services.audits(actor=actor).count() == 0
        return
    with pytest.raises(PermissionDenied):
        if action == "create":
            services.create_audit(actor=actor, box_id="nonexistent", reason="Test")
        elif action == "read":
            services.get_audit(actor=actor, audit_id=999999)
        elif action == "complete":
            services.complete_audit(actor=actor, audit_id=999999, observations={}, reason="Test")
        else:
            services.cancel_audit(actor=actor, audit_id=999999, reason="Test")


def test_cancel_reaudit_and_double_finalization(people, world):
    audit = create(people, world)
    with pytest.raises(ValidationError):
        create(people, world, supersedes_id=audit.pk)
    services.cancel_audit(actor=people["manager"], audit_id=audit.pk, reason="Restart observation")
    with pytest.raises(ValidationError):
        complete(people, audit)
    with pytest.raises(ValidationError):
        services.cancel_audit(actor=people["technician"], audit_id=audit.pk, reason="Again")
    newer = create(people, world, supersedes_id=audit.pk)
    assert newer.supersedes_id == audit.pk
    with pytest.raises(ValidationError):
        services.create_audit(
            actor=people["technician"], box_id=world.box2.pk, reason="Test", supersedes_id=audit.pk
        )


def test_audit_event_failure_rolls_back(people, world):
    with patch("quality.services.record_event", side_effect=RuntimeError("Synthetic failure")):
        with pytest.raises(RuntimeError):
            create(people, world)
    assert not InventoryAudit.objects.exists()
    audit = create(people, world)
    with patch("quality.services.record_event", side_effect=RuntimeError("Synthetic failure")):
        with pytest.raises(RuntimeError):
            complete(people, audit)
    assert not AuditFinalization.objects.exists()


def test_backdated_movement_out_and_back_is_stale(people, world):
    specimen = stored(people["technician"], world)
    audit = create(people, world)
    check_out(people["technician"], specimen, people["technician"])
    check_in(people["technician"], specimen, world.position("A1"))
    with pytest.raises(ValidationError, match="Storage changed"):
        complete(people, audit)
    services.cancel_audit(actor=people["technician"], audit_id=audit.pk, reason="Snapshot changed")


def test_unrelated_movement_does_not_invalidate(people, world):
    audit = create(people, world)
    move_box(people["manager"], world.box2, world.shelf2)
    complete(people, audit)
    assert AuditFinalization.objects.get(audit=audit).outcome == "completed"


def test_empty_box_move_out_and_back_is_stale(people, world):
    audit = create(people, world)
    move_box(people["manager"], world.box, world.shelf2)
    move_box(people["manager"], world.box, world.shelf1)
    with pytest.raises(ValidationError, match="Storage changed"):
        complete(people, audit)


@pytest.mark.parametrize("mutation", ["blocked", "retired", "inactive_site"])
def test_definition_changes_are_stale(people, world, mutation):
    audit = create(people, world)
    if mutation == "blocked":
        Position.objects.filter(pk=world.position("A1").pk).update(
            blocked=True, block_reason="Check", revision=2
        )
    elif mutation == "retired":
        from django.utils import timezone

        Box.objects.filter(pk=world.box.pk).update(
            is_active=False,
            retired_at=timezone.now(),
            retired_by=people["manager"],
            retirement_reason="Synthetic retirement",
        )
    else:
        site = world.unit.room.site
        site.is_active = False
        site.save()
    with pytest.raises(ValidationError, match="Storage changed"):
        complete(people, audit)


def test_oversize_and_inactive_box_rejected(people, world):
    with patch("quality.services.MAX_AUDIT_POSITIONS", 1):
        with pytest.raises(ValidationError):
            create(people, world)
    site = world.unit.room.site
    site.is_active = False
    site.save()
    with pytest.raises(ValidationError, match="ancestry"):
        create(people, world)


def test_browser_forms_roles_and_preserved_errors(client, people, world):
    client.force_login(people["technician"])
    response = client.post(
        reverse("quality:audit-create"), {"box": world.box.pk, "reason": "Synthetic audit"}
    )
    assert response.status_code == 302
    audit = InventoryAudit.objects.get()
    url = reverse("quality:audit-complete", args=[audit.pk])
    response = client.get(url)
    assert response.status_code == 200
    assert b"Not yet observed" in response.content
    assert b'option value="empty" selected' not in response.content
    data = {"reason": "Every position observed", "discrepancy_explanation": "Unknown barcode"}
    for i in range(len(audit.positions)):
        data[f"state_{i}"] = "empty"
    data["state_0"], data["accession_0"] = "accession", "UNKNOWN-SYNTHETIC-CODE"
    data["state_1"], data["accession_1"] = "accession", "UNKNOWN-SYNTHETIC-CODE"
    response = client.post(url, data)
    assert response.status_code == 200
    assert b"already recorded" in response.content
    assert b'"UNKNOWN-SYNTHETIC-CODE"' in response.content
    data["state_1"], data["accession_1"] = "empty", ""
    assert client.post(url, data).status_code == 302
    assert client.get(reverse("quality:audit-detail", args=[audit.pk])).status_code == 200
    client.force_login(people["viewer"])
    assert client.get(reverse("quality:audit-list")).status_code == 200
    assert client.get(url).status_code == 403
    client.force_login(people["nobody"])
    assert client.get(reverse("quality:audit-detail", args=[audit.pk])).status_code == 403


def test_list_search_status_sort_and_filter_error(client, people, world):
    audit = create(people, world)
    client.force_login(people["viewer"])
    url = reverse("quality:audit-list")
    assert (
        audit
        in client.get(url, {"q": audit.number, "sort": "box", "per_page": 25}).context["page_obj"]
    )
    assert not client.get(url, {"status": "completed"}).context["page_obj"]
    response = client.get(url, {"status": "invalid"})
    assert response.status_code == 200
    assert response.context["filter_form"].errors
    assert client.get(url, {"per_page": 1000}).status_code == 400


@pytest.mark.django_db(transaction=True)
def test_complete_cancel_race_has_one_winner(people, world):
    audit = create(people, world)
    actor_id = people["technician"].pk
    observations = expectations(audit)
    barrier = Barrier(2)

    def run(action):
        from accounts.models import User

        close_old_connections()
        try:
            actor = User.objects.get(pk=actor_id)
            barrier.wait(timeout=10)
            try:
                if action == "complete":
                    services.complete_audit(
                        actor=actor, audit_id=audit.pk, observations=observations, reason="Observed"
                    )
                else:
                    services.cancel_audit(actor=actor, audit_id=audit.pk, reason="Restart")
            except ValidationError:
                return "rejected"
            return "saved"
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ["complete", "cancel"]))
    assert sorted(results) == ["rejected", "saved"]
    assert AuditFinalization.objects.filter(audit=audit).count() == 1
    assert (
        AuditEvent.objects.filter(
            action__in=["quality.audit_completed", "quality.audit_cancelled"]
        ).count()
        == 1
    )


def test_empty_box_ancestor_move_is_stale(people, world):
    from django.utils import timezone

    from specimens.tests.conftest import gate_revision
    from storage import services as storage

    audit = create(people, world)
    tower = world.shelf1.parent
    storage.move_container(
        actor=people["manager"],
        kind="node",
        object_id=tower.pk,
        expected_revision=tower.revision,
        expected_gate_revision=gate_revision(),
        target_kind="unit",
        target_id=world.unit.pk,
        target_slot=2,
        occurred_at=timezone.now(),
        reason="Synthetic tower position check",
    )
    with pytest.raises(ValidationError, match="Storage changed"):
        complete(people, audit)


def test_empty_post_shows_required_errors(client, people, world):
    client.force_login(people["technician"])
    response = client.post(reverse("quality:audit-create"), {})
    assert response.status_code == 200
    assert response.context["form"].errors


@pytest.mark.parametrize("state", ["stale", "completed", "cancelled"])
def test_blocked_completion_post_preserves_submitted_values(client, people, world, state):
    audit = create(people, world)
    if state == "stale":
        move_box(people["manager"], world.box, world.shelf2)
    elif state == "completed":
        complete(people, audit)
    else:
        services.cancel_audit(
            actor=people["technician"], audit_id=audit.pk, reason="Earlier cancellation"
        )
    before_results = list(AuditFinalization.objects.values())
    before_events = AuditEvent.objects.filter(object_type="quality.inventoryaudit").count()
    client.force_login(people["technician"])
    data = {
        "reason": "Submitted inspection reason",
        "discrepancy_explanation": "Submitted explanation",
    }
    for index in range(len(audit.positions)):
        data[f"state_{index}"] = "empty"
    data["state_0"] = "accession"
    data["accession_0"] = "SYNTHETIC-RETAINED-OBSERVATION"
    with patch("quality.views.services.complete_audit") as save:
        response = client.post(reverse("quality:audit-complete", args=[audit.pk]), data)
    save.assert_not_called()
    assert response.status_code == 200
    assert response.context["blocked"]
    form = response.context["form"]
    for name in ["state_0", "state_1", "accession_0", "reason", "discrepancy_explanation"]:
        assert form[name].value() == data[name]
        assert form.fields[name].widget.attrs["disabled"]
    assert b"SYNTHETIC-RETAINED-OBSERVATION" in response.content
    assert b"Submitted inspection reason" in response.content
    assert b"Submitted explanation" in response.content
    assert b'value="accession" selected' in response.content
    assert b'type="submit">Complete audit' not in response.content
    assert list(AuditFinalization.objects.values()) == before_results
    assert AuditEvent.objects.filter(object_type="quality.inventoryaudit").count() == before_events


@pytest.mark.parametrize("state", ["completed", "cancelled"])
def test_blocked_cancel_post_preserves_submitted_reason(client, people, world, state):
    audit = create(people, world)
    if state == "completed":
        complete(people, audit)
    else:
        services.cancel_audit(
            actor=people["technician"], audit_id=audit.pk, reason="Earlier cancellation"
        )
    before_results = list(AuditFinalization.objects.values())
    before_events = AuditEvent.objects.filter(object_type="quality.inventoryaudit").count()
    client.force_login(people["technician"])
    with patch("quality.views.services.cancel_audit") as save:
        response = client.post(
            reverse("quality:audit-cancel", args=[audit.pk]),
            {"reason": "Submitted cancellation reason"},
        )
    save.assert_not_called()
    assert response.status_code == 200
    assert response.context["blocked"]
    assert response.context["form"]["reason"].value() == "Submitted cancellation reason"
    assert response.context["form"].fields["reason"].widget.attrs["disabled"]
    assert b"Submitted cancellation reason" in response.content
    assert b'type="submit">Cancel audit' not in response.content
    assert list(AuditFinalization.objects.values()) == before_results
    assert AuditEvent.objects.filter(object_type="quality.inventoryaudit").count() == before_events
