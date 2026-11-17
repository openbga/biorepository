"""Excursion-origin manual holds: explicit review, atomic provenance and ordinary hold gates."""

import uuid
from unittest.mock import patch

import pytest
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, transaction
from django.urls import reverse
from django.utils import timezone

from core.models import AuditEvent
from equipment import queries, services
from equipment.forms import HOLD_SNAPSHOT_SALT, ExcursionHoldsForm
from specimens import services as specimens
from specimens.models import Hold, HoldRelease, Specimen
from specimens.tests.conftest import ask_disposal, codes, decide, execute
from storage import services as storage

from .conftest import ago, check_out, place, register

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def quick_rendering(settings):
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


@pytest.fixture
def affected(people, world, tank):
    tech = people["technician"]
    selected = [register(tech, world), register(tech, world)]
    for specimen, position in zip(selected, ("A1", "A2"), strict=True):
        place(tech, specimen, world.position(position), occurred_at=ago(250))
        specimen.refresh_from_db()
    excursion = services.open_excursion(
        actor=tech,
        equipment_id=tank.pk,
        started_at=ago(200),
        ended_at=ago(100),
        detection="alarm",
        cause="Synthetic probe event",
        action="Probe checked",
        impact="Review recorded by staff",
    )
    return excursion, selected


def batch(actor, excursion, selected, **overrides):
    values = {
        "actor": actor,
        "excursion_id": excursion.pk,
        "expected_revision": excursion.revision,
        "specimen_ids": [s.pk for s in selected],
        "specimen_revisions": {s.pk: s.revision for s in selected},
        "scope": "use",
        "reason": "Review affected stock",
    }
    return services.place_excursion_holds(**(values | overrides))


def submit(people, excursion):
    return services.submit_excursion(
        actor=people["technician"],
        excursion_id=excursion.pk,
        expected_revision=excursion.revision,
    )


def close(people, excursion):
    return services.close_excursion(
        actor=people["manager"],
        excursion_id=excursion.pk,
        expected_revision=excursion.revision,
        note="Reviewed synthetic event",
    )


@pytest.mark.parametrize("role", ["admin", "manager", "technician"])
@pytest.mark.parametrize("state", ["open", "review", "closed"])
def test_operators_place_manual_holds_in_every_state(people, affected, role, state):
    excursion, selected = affected
    if state in ("review", "closed"):
        excursion = submit(people, excursion)
    if state == "closed":
        excursion = close(people, excursion)
    revision = excursion.revision
    holds = batch(people[role], excursion, selected)
    assert len(holds) == 2
    assert {h.specimen_id for h in holds} == {s.pk for s in selected}
    assert all(h.reference == "" and h.actor == people[role] for h in holds)
    assert all(h.reason == f"Excursion {excursion.number}: Review affected stock" for h in holds)
    excursion.refresh_from_db()
    assert excursion.revision == revision + 1 and excursion.status == state
    event = excursion.events.get(kind="note")
    provenance = event.changes["manual_holds"]
    assert provenance[0] == []
    assert {item["hold_id"] for item in provenance[1]} == {str(h.pk) for h in holds}
    assert {item["specimen_id"] for item in provenance[1]} == {str(s.pk) for s in selected}
    assert event.actor == people[role]
    audit = AuditEvent.objects.get(action="excursion.holds_placed")
    assert audit.after["manual_holds"] == provenance[1]
    assert audit.actor == people[role]
    assert AuditEvent.objects.filter(action="specimens.hold_placed").count() == 2


@pytest.mark.parametrize("role", ["viewer", "nobody"])
def test_role_refused_before_reading_excursion(people, role):
    with pytest.raises(PermissionDenied):
        services.place_excursion_holds(
            actor=people[role],
            excursion_id=999999,
            expected_revision=1,
            specimen_ids=[],
            specimen_revisions={},
            scope="use",
            reason="Review",
        )
    with pytest.raises(PermissionDenied):
        queries.excursion_hold_candidates(None, actor=people[role])


@pytest.mark.parametrize("revision", [None, False, True, 0, "1"])
def test_missing_or_invalid_revision_cannot_bypass_replay_guard(people, affected, revision):
    excursion, selected = affected
    with pytest.raises(ValidationError) as exc:
        batch(people["technician"], excursion, selected, expected_revision=revision)
    assert codes(exc.value) == {"stale_revision"}
    assert not Hold.objects.exists()


@pytest.mark.parametrize("invalid", ["empty", "duplicate", "large", "foreign", "scope", "reason"])
def test_invalid_selection_scope_or_reason_is_refused_atomically(people, world, affected, invalid):
    excursion, selected = affected
    overrides = {}
    if invalid == "empty":
        overrides["specimen_ids"] = []
    elif invalid == "duplicate":
        overrides["specimen_ids"] = [selected[0].pk] * 2
    elif invalid == "large":
        overrides["specimen_ids"] = [uuid.uuid4() for _ in range(201)]
    elif invalid == "foreign":
        foreign = register(people["technician"], world)
        selected = [foreign]
    elif invalid == "scope":
        overrides["scope"] = "quality"
    else:
        overrides["reason"] = " "
    with pytest.raises(ValidationError):
        batch(people["technician"], excursion, selected, **overrides)
    assert not Hold.objects.exists()
    assert not excursion.events.filter(kind="note").exists()
    excursion.refresh_from_db()
    assert excursion.revision == 1


def test_stale_specimen_and_changed_affected_membership_are_refused(people, affected):
    excursion, selected = affected
    specimens.record_thaw(
        actor=people["technician"],
        specimen_id=selected[1].pk,
        expected_revision=selected[1].revision,
        occurred_at=ago(50),
        reason="Observed thaw",
    )
    with pytest.raises(ValidationError) as exc:
        batch(people["technician"], excursion, selected)
    assert codes(exc.value) == {"stale_revision"}
    assert not Hold.objects.exists()
    # The same reviewed revision is insufficient if the live affected set no longer includes it.
    selected[1].refresh_from_db()
    with patch("equipment.queries.excursion_specimens", return_value=([], None)):
        with pytest.raises(ValidationError, match="not affected"):
            batch(people["technician"], excursion, selected)
    assert not Hold.objects.exists()


def test_disposed_selection_rolls_back_any_prior_hold_and_audit(people, affected):
    excursion, selected = affected
    selected.sort(key=lambda s: s.pk)
    final = selected[-1]
    request = ask_disposal(people["technician"], final)
    decide(people["approver"], request)
    execute(people["technician"], request)
    final.refresh_from_db()
    before = AuditEvent.objects.count()
    with pytest.raises(ValidationError, match="disposed"):
        batch(people["technician"], excursion, selected)
    assert not Hold.objects.exists()
    assert AuditEvent.objects.count() == before
    assert not excursion.events.filter(kind="note").exists()
    selected[0].refresh_from_db()
    assert selected[0].revision == 2


def test_audit_failure_rolls_back_holds_note_and_revision(people, affected, monkeypatch):
    excursion, selected = affected
    real = services.record_event

    def fail(**kwargs):
        if kwargs["action"] == "excursion.holds_placed":
            raise RuntimeError("Synthetic audit failure")
        return real(**kwargs)

    monkeypatch.setattr(services, "record_event", fail)
    with pytest.raises(RuntimeError, match="Synthetic audit failure"):
        batch(people["technician"], excursion, selected)
    excursion.refresh_from_db()
    assert excursion.revision == 1
    assert not Hold.objects.exists() and not excursion.events.filter(kind="note").exists()


def test_closed_capture_is_used_even_if_new_backdated_presence_is_recorded(people, world, affected):
    excursion, selected = affected
    excursion = close(people, submit(people, excursion))
    late = register(people["technician"], world)
    place(people["technician"], late, world.position("A3"), occurred_at=ago(240))
    late.refresh_from_db()
    with pytest.raises(ValidationError, match="not affected"):
        batch(people["technician"], excursion, [late])
    assert len(batch(people["technician"], excursion, selected)) == 2


def test_newest_capture_after_reopen_and_reclose_is_used(people, world, affected):
    excursion, selected = affected
    excursion = close(people, submit(people, excursion))
    excursion = services.reopen_excursion(
        actor=people["manager"],
        excursion_id=excursion.pk,
        expected_revision=excursion.revision,
        reason="Additional recorded movement",
    )
    late = register(people["technician"], world)
    place(people["technician"], late, world.position("A3"), occurred_at=ago(240))
    late.refresh_from_db()
    excursion = close(people, submit(people, excursion))
    assert len(batch(people["technician"], excursion, [late])) == 1


def test_existing_holds_remain_distinct_and_release_is_manual_after_close(people, affected):
    excursion, selected = affected
    specimen = selected[0]
    ordinary = specimens.place_hold(
        actor=people["technician"],
        specimen_id=specimen.pk,
        expected_revision=specimen.revision,
        scope="use",
        occurred_at=ago(50),
        reason="Other review",
    )
    with storage.operation():
        storage.lock_gate()
        linked = specimens.place_linked_hold(
            actor=people["technician"],
            specimen_id=specimen.pk,
            scope="disposal",
            reference="Synthetic separate workflow",
            occurred_at=ago(40),
            reason="Other workflow",
        )
    specimen.refresh_from_db()
    hold = batch(people["technician"], excursion, [specimen])[0]
    excursion.refresh_from_db()
    excursion = close(people, submit(people, excursion))
    excursion = services.reopen_excursion(
        actor=people["manager"],
        excursion_id=excursion.pk,
        expected_revision=excursion.revision,
        reason="More evidence",
    )
    assert HoldRelease.objects.count() == 0
    specimen.refresh_from_db()
    with pytest.raises(PermissionDenied):
        specimens.release_hold(
            actor=people["technician"],
            hold_id=hold.pk,
            expected_revision=specimen.revision,
            occurred_at=timezone.now(),
            reason="Review finished",
        )
    specimens.release_hold(
        actor=people["manager"],
        hold_id=hold.pk,
        expected_revision=specimen.revision,
        occurred_at=timezone.now(),
        reason="Review finished",
    )
    assert not HoldRelease.objects.filter(hold_id__in=[ordinary.pk, linked.pk]).exists()


@pytest.mark.parametrize("scope", Hold.Scope.values)
def test_holds_use_existing_use_and_disposal_gates(people, affected, scope):
    excursion, selected = affected
    specimen = selected[0]
    batch(people["technician"], excursion, [specimen], scope=scope)
    specimen.refresh_from_db()
    if scope in ("use", "all_use"):
        with pytest.raises(ValidationError, match="hold"):
            check_out(
                people["technician"], specimen, people["technician"], occurred_at=timezone.now()
            )
    if scope in ("disposal", "all_use"):
        with pytest.raises(ValidationError, match="hold"):
            ask_disposal(people["technician"], specimen)


def test_distributed_specimen_can_receive_hold_and_note_is_immutable(people, affected):
    excursion, selected = affected
    specimen = selected[0]
    check_out(people["technician"], specimen, people["technician"], occurred_at=ago(90))
    specimen.refresh_from_db()
    with storage.operation():
        storage.lock_gate()
        specimens.dispatch_specimen(
            actor=people["technician"],
            specimen_id=specimen.pk,
            expected_revision=specimen.revision,
            destination="Example institution",
            occurred_at=ago(80),
            reason="Synthetic distribution",
        )
    specimen.refresh_from_db()
    assert specimen.status == Specimen.Status.DISTRIBUTED
    hold = batch(people["technician"], excursion, [specimen])[0]
    assert hold.specimen_id == specimen.pk
    with pytest.raises(DatabaseError, match="Historical records"), transaction.atomic():
        excursion.events.filter(kind="note").update(note="Changed")


def payload(response, selected, **overrides):
    return {
        "snapshot": response.context["form"].initial["snapshot"],
        "specimen_ids": [str(s.pk) for s in selected],
        "scope": "use",
        "reason": "Review this explicit selection",
    } | overrides


def test_page_requires_explicit_selection_preserves_errors_and_rejects_replay(
    client, people, affected
):
    excursion, selected = affected
    client.force_login(people["technician"])
    url = reverse("equipment:excursion-holds", args=[excursion.pk])
    response = client.get(url)
    assert response.status_code == 200
    assert b"Existing active holds" in response.content
    assert b" checked" not in response.content
    empty = client.post(url, payload(response, []))
    assert empty.status_code == 422 and b"Select at least one specimen" in empty.content
    invalid = client.post(url, payload(response, selected[:1], reason=""))
    assert invalid.status_code == 422 and b" checked" in invalid.content
    assert (
        invalid.context["form"]["snapshot"].value() == response.context["form"].initial["snapshot"]
    )
    valid = payload(response, selected[:1])
    assert client.post(url, valid).status_code == 302
    assert Hold.objects.count() == 1
    replay = client.post(url, valid)
    assert replay.status_code == 422 and b"changed since" in replay.content
    assert Hold.objects.count() == 1
    detail = client.get(reverse("equipment:excursion-detail", args=[excursion.pk]))
    assert b"1 manual holds" in detail.content and b"Use hold" in detail.content
    assert str(Hold.objects.get().pk).encode() not in detail.content


@pytest.mark.parametrize("bad", ["tampered", "expired", "actor", "excursion", "foreign"])
def test_snapshot_and_selection_tampering_are_refused(client, people, affected, bad):
    excursion, selected = affected
    client.force_login(people["technician"])
    url = reverse("equipment:excursion-holds", args=[excursion.pk])
    response = client.get(url)
    data = payload(response, selected[:1])
    snapshot = data["snapshot"]
    if bad == "tampered":
        data["snapshot"] += "broken"
    elif bad in ("actor", "excursion"):
        decoded = signing.loads(snapshot, salt=HOLD_SNAPSHOT_SALT)
        decoded[bad] = "foreign"
        data["snapshot"] = signing.dumps(decoded, salt=HOLD_SNAPSHOT_SALT)
    elif bad == "foreign":
        data["specimen_ids"] = [str(uuid.uuid4())]
    if bad == "expired":
        with patch("django.core.signing.time.time", return_value=timezone.now().timestamp() + 3602):
            result = client.post(url, data)
    else:
        result = client.post(url, data)
    assert result.status_code == 422
    assert not Hold.objects.exists()


def test_stale_snapshot_keeps_submitted_values_without_refresh(client, people, affected):
    excursion, selected = affected
    client.force_login(people["technician"])
    url = reverse("equipment:excursion-holds", args=[excursion.pk])
    response = client.get(url)
    data = payload(response, selected[:1])
    specimens.record_thaw(
        actor=people["technician"],
        specimen_id=selected[0].pk,
        expected_revision=selected[0].revision,
        occurred_at=ago(50),
        reason="Observed thaw",
    )
    result = client.post(url, data)
    assert result.status_code == 422 and b"changed since you reviewed" in result.content
    assert result.context["form"]["snapshot"].value() == data["snapshot"]
    assert result.context["form"]["reason"].value() == data["reason"]
    assert result.context["form"]["scope"].value() == "use"
    assert not Hold.objects.exists()


def test_viewers_and_revoked_operators_cannot_post(client, people, affected):
    excursion, selected = affected
    url = reverse("equipment:excursion-holds", args=[excursion.pk])
    client.force_login(people["viewer"])
    assert client.get(url).status_code == 403
    assert client.post(url, {}).status_code == 403
    detail = client.get(reverse("equipment:excursion-detail", args=[excursion.pk]))
    assert f'href="{url}"'.encode() not in detail.content
    client.force_login(people["technician"])
    response = client.get(url)
    people["technician"].groups.clear()
    assert client.post(url, payload(response, selected)).status_code == 403
    assert not Hold.objects.exists()


def test_candidate_search_and_paging_never_hide_the_remaining_set(
    client, people, affected, monkeypatch
):
    excursion, selected = affected
    client.force_login(people["technician"])
    monkeypatch.setattr(services, "EXCURSION_HOLD_LIMIT", 1)
    url = reverse("equipment:excursion-holds", args=[excursion.pk])
    first = client.get(url)
    second = client.get(url, {"page": 2})
    assert first.context["page_obj"].paginator.count == 2
    assert b"Next" in first.content and b"Previous" in second.content
    seen = {item["specimen"].pk for r in (first, second) for item in r.context["page_obj"]}
    assert seen == {s.pk for s in selected}
    filtered = client.get(url, {"q": selected[1].accession.lower()})
    assert filtered.context["page_obj"].paginator.count == 1
    assert filtered.context["page_obj"][0]["specimen"].pk == selected[1].pk
    bad_page_selection = client.post(
        url, payload(first, [second.context["page_obj"][0]["specimen"]])
    )
    assert bad_page_selection.status_code == 422 and not Hold.objects.exists()


def test_empty_and_disposed_candidates_are_visible_without_submit(client, people, affected):
    excursion, selected = affected
    client.force_login(people["technician"])
    for specimen in selected:
        request = ask_disposal(people["technician"], specimen)
        decide(people["approver"], request)
        execute(people["technician"], request)
    url = reverse("equipment:excursion-holds", args=[excursion.pk])
    response = client.get(url)
    assert b"Disposed; unavailable" in response.content
    assert b">Place holds</button>" not in response.content
    empty = client.get(url, {"q": "VP-NOT-REAL"})
    assert b"No matching affected specimens" in empty.content
    assert b">Place holds</button>" not in empty.content


def test_form_reason_leaves_space_for_excursion_number(people, affected):
    excursion, selected = affected
    candidates, _ = queries.excursion_hold_candidates(excursion, actor=people["technician"])
    unbound = ExcursionHoldsForm(
        actor=people["technician"], excursion=excursion, candidates=candidates
    )
    limit = unbound.fields["reason"].max_length
    form = ExcursionHoldsForm(
        {
            "snapshot": unbound.initial["snapshot"],
            "scope": "use",
            "specimen_ids": [str(selected[0].pk)],
            "reason": "x" * (limit + 1),
        },
        actor=people["technician"],
        excursion=excursion,
        candidates=candidates,
    )
    assert not form.is_valid() and "reason" in form.errors
