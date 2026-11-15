from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from functools import partial
from threading import Barrier
from unittest.mock import patch

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.urls import reverse
from django.utils import timezone

from core.models import AuditEvent
from equipment import services as equipment_services
from equipment.tests import conftest as equipment_fixtures
from quality import deviation_services as services
from quality import services as audit_services
from quality.models import Deviation, DeviationEvent
from specimens.models import Hold
from storage.models import Occupancy

tank = equipment_fixtures.tank

pytestmark = pytest.mark.django_db


def values(**overrides):
    return {
        "title": "Synthetic container label discrepancy",
        "occurred_at": timezone.now() - timedelta(minutes=20),
        "description": "An invented label was unreadable during a synthetic check.",
        **overrides,
    }


def create(people, *, role="technician", complete=False, **overrides):
    if complete:
        overrides = {
            "investigation": "Synthetic label abrasion confirmed.",
            "corrective_action": "Replaced the demonstration label.",
            "effectiveness": "Synthetic scanner read confirmed.",
            **overrides,
        }
    return services.create_deviation(
        actor=people[role], values=values(**overrides), reason="Record synthetic observation"
    )


def submit(people, deviation, role="technician"):
    return services.submit_deviation(
        actor=people[role],
        deviation_id=deviation.pk,
        expected_revision=deviation.revision,
        reason="Investigation ready for independent review",
    )


def review(people, deviation, decision="close", role="manager"):
    return services.review_deviation(
        actor=people[role],
        deviation_id=deviation.pk,
        expected_revision=deviation.revision,
        decision=decision,
        reason="Synthetic independent review",
    )


@pytest.mark.parametrize(
    "action", ["list", "read", "create", "update", "submit", "review", "reopen"]
)
@pytest.mark.parametrize(
    "role", ["admin", "manager", "technician", "viewer", "nobody", "bare_superuser", "inactive"]
)
def test_role_matrix_before_record_reads(people, action, role):
    actor = (
        people["nobody"]
        if role == "bare_superuser"
        else people["admin"]
        if role == "inactive"
        else people[role]
    )
    if role == "bare_superuser":
        actor.is_superuser = True
        actor.save()
    if role == "inactive":
        actor.is_active = False
        actor.save()
    allowed = (
        role in services.READ_ROLES
        if action in {"list", "read"}
        else role
        in (services.MANAGE_ROLES if action in {"review", "reopen"} else services.OPERATE_ROLES)
    )
    if action == "list":
        operation = partial(services.deviations, actor=actor)
    elif action == "read":
        operation = partial(services.get_deviation, actor=actor, deviation_id=999999)
    elif action == "create":
        operation = partial(services.create_deviation, actor=actor, values=values(), reason="Test")
    elif action == "update":
        operation = partial(
            services.update_deviation,
            actor=actor,
            deviation_id=999999,
            expected_revision=1,
            values={},
            reason="Test",
        )
    elif action == "submit":
        operation = partial(
            services.submit_deviation,
            actor=actor,
            deviation_id=999999,
            expected_revision=1,
            reason="Test",
        )
    elif action == "review":
        operation = partial(
            services.review_deviation,
            actor=actor,
            deviation_id=999999,
            expected_revision=1,
            decision="close",
            reason="Test",
        )
    else:
        operation = partial(
            services.reopen_deviation,
            actor=actor,
            deviation_id=999999,
            expected_revision=1,
            reason="Test",
        )
    if not allowed:
        with pytest.raises(PermissionDenied):
            operation()
    elif action in {"create", "list"}:
        operation()
    else:
        with pytest.raises(Deviation.DoesNotExist):
            operation()


@pytest.mark.parametrize(
    "field,value",
    [
        ("title", " "),
        ("description", "\n"),
        ("title", "x" * 161),
        *[(field, "x" * 4001) for field in services.NARRATIVES],
        ("occurred_at", timezone.now() + timedelta(days=1)),
        ("occurred_at", timezone.now().replace(tzinfo=None)),
        ("occurred_at", "yesterday"),
        ("excursion_id", 999999),
        ("inventory_audit_id", 999999),
    ],
)
def test_report_validation_is_atomic(people, field, value):
    with pytest.raises(ValidationError):
        create(people, **{field: value})
    assert not Deviation.objects.exists()
    assert not DeviationEvent.objects.exists()
    assert not AuditEvent.objects.filter(action__startswith="quality.deviation_").exists()


@pytest.mark.parametrize("reason", ["", " \n ", "x" * 4001, None])
def test_creation_reason_required_and_bounded(people, reason):
    with pytest.raises(ValidationError):
        services.create_deviation(actor=people["technician"], values=values(), reason=reason)
    assert not Deviation.objects.exists()


def test_full_lifecycle_retains_narratives_review_facts_and_events(people):
    deviation = create(people, complete=True)
    assert deviation.number == f"DEV-{deviation.pk:06d}"
    created = deviation.events.get()
    assert created.revision == 1
    assert created.changes["description"] == [None, deviation.description]
    deviation = services.update_deviation(
        actor=people["technician2"],
        deviation_id=deviation.pk,
        expected_revision=1,
        values={
            "description": "Revised synthetic account.",
            "immediate_action": " No action needed. ",
        },
        reason="Corrected observation detail",
    )
    assert deviation.immediate_action == "No action needed."
    deviation = submit(people, deviation, role="technician2")
    assert deviation.status == "awaiting_review"
    deviation = review(people, deviation, decision="return")
    assert deviation.status == "open"
    assert deviation.submitted_by == people["technician2"]
    deviation = submit(people, deviation)
    deviation = review(people, deviation)
    closed_time = deviation.closed_at
    deviation = services.reopen_deviation(
        actor=people["admin"],
        deviation_id=deviation.pk,
        expected_revision=deviation.revision,
        reason="Additional synthetic evidence",
    )
    assert deviation.status == "open"
    assert deviation.closed_at is None and deviation.closed_by is None
    assert deviation.submitted_by == people["technician"]
    assert list(deviation.events.values_list("kind", flat=True)) == [
        "created",
        "updated",
        "submitted",
        "returned",
        "submitted",
        "closed",
        "reopened",
    ]
    assert list(deviation.events.values_list("revision", flat=True)) == list(range(1, 8))
    reopened = deviation.events.get(kind="reopened")
    assert reopened.changes["closed_at"] == [closed_time.isoformat(), None]
    assert deviation.events.get(kind="updated").changes["description"][0].startswith("An invented")
    audit = AuditEvent.objects.get(action="quality.deviation_updated")
    assert audit.before["description"].startswith("An invented")
    assert audit.after["description"] == "Revised synthetic account."
    assert len(AuditEvent.objects.filter(object_type="quality.deviation")) == 7


@pytest.mark.parametrize("decision", ["close", "return"])
@pytest.mark.parametrize("reviewer", ["creator", "submitter"])
def test_second_person_excludes_creator_and_latest_submitter(people, decision, reviewer):
    deviation = create(people, role="admin", complete=True)
    deviation = submit(people, deviation, role="manager")
    role = "admin" if reviewer == "creator" else "manager"
    with pytest.raises(PermissionDenied):
        review(people, deviation, decision=decision, role=role)
    deviation.refresh_from_db()
    assert deviation.status == "awaiting_review" and deviation.revision == 2
    assert deviation.events.count() == 2


@pytest.mark.parametrize("missing", ["investigation", "corrective_action", "effectiveness"])
def test_incomplete_submission_names_missing_narrative(people, missing):
    deviation = create(people, complete=True, **{missing: ""})
    with pytest.raises(ValidationError, match=missing.replace("_", " ")):
        submit(people, deviation)
    deviation.refresh_from_db()
    assert deviation.status == "open" and deviation.revision == 1


def test_state_gates_and_invalid_decision(people):
    deviation = create(people, complete=True)
    with pytest.raises(ValidationError):
        review(people, deviation)
    with pytest.raises(ValidationError):
        services.reopen_deviation(
            actor=people["manager"], deviation_id=deviation.pk, expected_revision=1, reason="Test"
        )
    deviation = submit(people, deviation)
    with pytest.raises(ValidationError):
        submit(people, deviation)
    with pytest.raises(ValidationError):
        services.update_deviation(
            actor=people["manager"],
            deviation_id=deviation.pk,
            expected_revision=2,
            values={"title": "Changed"},
            reason="Test",
        )
    with pytest.raises(ValidationError):
        review(people, deviation, decision="invented")
    deviation = review(people, deviation)
    with pytest.raises(ValidationError):
        review(people, deviation)
    with pytest.raises(ValidationError):
        submit(people, deviation)
    with pytest.raises(ValidationError):
        services.update_deviation(
            actor=people["manager"],
            deviation_id=deviation.pk,
            expected_revision=3,
            values={"title": "Changed"},
            reason="Test",
        )


def test_noop_and_uncontrolled_values_rejected(people):
    deviation = create(people)
    for data in (
        {"title": deviation.title},
        {"status": "closed"},
        {"created_by_id": people["admin"].pk},
    ):
        with pytest.raises(ValidationError):
            services.update_deviation(
                actor=people["technician"],
                deviation_id=deviation.pk,
                expected_revision=1,
                values=data,
                reason="Test",
            )
    assert deviation.events.count() == 1


@pytest.mark.parametrize("action", ["create", "update", "submit", "review", "reopen"])
def test_audit_failure_rolls_back_report_revision_and_history(people, action):
    deviation = None if action == "create" else create(people, complete=True)
    if action in {"review", "reopen"}:
        deviation = submit(people, deviation)
    if action == "reopen":
        deviation = review(people, deviation)
    before = list(Deviation.objects.values())
    event_count = DeviationEvent.objects.count()
    with patch(
        "quality.deviation_services.record_event", side_effect=RuntimeError("Synthetic failure")
    ):
        with pytest.raises(RuntimeError):
            if action == "create":
                create(people)
            elif action == "update":
                services.update_deviation(
                    actor=people["technician"],
                    deviation_id=deviation.pk,
                    expected_revision=deviation.revision,
                    values={"title": "Revised title"},
                    reason="Test",
                )
            elif action == "submit":
                submit(people, deviation)
            elif action == "review":
                review(people, deviation)
            else:
                services.reopen_deviation(
                    actor=people["manager"],
                    deviation_id=deviation.pk,
                    expected_revision=deviation.revision,
                    reason="Test",
                )
    assert list(Deviation.objects.values()) == before
    assert DeviationEvent.objects.count() == event_count


def test_source_link_corrections_keep_prior_values_and_have_no_inventory_side_effects(
    people, world, tank
):
    excursion = equipment_services.open_excursion(
        actor=people["technician"],
        equipment_id=tank.pk,
        started_at=timezone.now() - timedelta(hours=1),
        detection="inspection",
    )
    audit = audit_services.create_audit(
        actor=people["technician"], box_id=world.box.pk, reason="Synthetic evidence"
    )
    stock, holds = list(Occupancy.objects.values()), list(Hold.objects.values())
    deviation = create(people, excursion_id=excursion.pk, inventory_audit_id=audit.pk)
    deviation = services.update_deviation(
        actor=people["technician"],
        deviation_id=deviation.pk,
        expected_revision=1,
        values={"excursion_id": None, "inventory_audit_id": None},
        reason="Correct source links",
    )
    event = deviation.events.get(kind="updated")
    assert event.changes["excursion_id"] == [excursion.pk, None]
    assert event.changes["inventory_audit_id"] == [audit.pk, None]
    assert list(Occupancy.objects.values()) == stock
    assert list(Hold.objects.values()) == holds
    excursion.refresh_from_db()
    assert excursion.status == "open"
    assert audit.status == "open"


@pytest.mark.parametrize("action", ["update", "submit", "review", "reopen"])
def test_stale_revision_rejected_without_new_events(people, action):
    deviation = create(people, complete=True)
    if action == "review":
        deviation = submit(people, deviation)
    if action == "reopen":
        deviation = review(people, submit(people, deviation))
    before = list(Deviation.objects.values())
    event_count = deviation.events.count()
    kwargs = dict(
        actor=people["manager"],
        deviation_id=deviation.pk,
        expected_revision=deviation.revision - 1,
        reason="Test",
    )
    with pytest.raises(ValidationError) as exc:
        if action == "update":
            services.update_deviation(**kwargs, values={"title": "Stale input"})
        elif action == "submit":
            services.submit_deviation(**kwargs)
        elif action == "review":
            services.review_deviation(**kwargs, decision="close")
        else:
            services.reopen_deviation(**kwargs)
    assert exc.value.code == "stale_revision"
    assert list(Deviation.objects.values()) == before
    assert deviation.events.count() == event_count


def test_database_retention_and_state_constraints(people):
    deviation = create(people, complete=True)
    event = deviation.events.get()
    with pytest.raises(DatabaseError), transaction.atomic():
        DeviationEvent.objects.filter(pk=event.pk).update(reason="Rewritten")
    for table, pk in [("quality_deviationevent", event.pk), ("quality_deviation", deviation.pk)]:
        with pytest.raises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(f'DELETE FROM "{table}" WHERE id = %s', [pk])
    for changes in (
        {"revision": 0},
        {"title": " "},
        {"description": ""},
        {"status": "invented"},
        {"status": "awaiting_review"},
        {"closed_by": people["manager"]},
    ):
        with pytest.raises(DatabaseError), transaction.atomic():
            Deviation.objects.filter(pk=deviation.pk).update(**changes)
    deviation = submit(people, deviation)
    with pytest.raises(DatabaseError), transaction.atomic():
        Deviation.objects.filter(pk=deviation.pk).update(
            status="closed", closed_by=people["technician"], closed_at=deviation.updated_at
        )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("action", ["submit", "review", "edit"])
def test_concurrent_actions_commit_one_transition(people, action):
    deviation = create(people, complete=True)
    if action == "review":
        deviation = submit(people, deviation)
    initial_revision = deviation.revision
    barrier = Barrier(2)

    def worker():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            kwargs = dict(
                actor=people["manager"],
                deviation_id=deviation.pk,
                expected_revision=initial_revision,
                reason="Concurrent synthetic action",
            )
            if action == "submit":
                services.submit_deviation(**kwargs)
            elif action == "review":
                services.review_deviation(**kwargs, decision="close")
            else:
                services.update_deviation(**kwargs, values={"title": "Concurrent correction"})
            return "saved"
        except ValidationError as exc:
            return exc.code
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: worker(), range(2)))
    assert sorted(outcomes) == ["saved", "stale_revision"]
    deviation.refresh_from_db()
    assert deviation.revision == initial_revision + 1
    assert deviation.events.count() == initial_revision + 1
    kind = {"submit": "submitted", "review": "closed", "edit": "updated"}[action]
    assert AuditEvent.objects.filter(action=f"quality.deviation_{kind}").count() == 1


@pytest.mark.parametrize(
    "route", ["list", "create", "detail", "edit", "submit", "review", "reopen"]
)
@pytest.mark.parametrize("role", ["viewer", "technician", "manager", "nobody"])
def test_page_roles_and_help(client, people, route, role):
    deviation = create(people, complete=True)
    client.force_login(people[role])
    url = reverse(
        f"quality:deviation-{route}", args=[deviation.pk] if route not in {"list", "create"} else []
    )
    response = client.get(url)
    allowed = role != "nobody" and (route in {"list", "detail"} or role != "viewer")
    if route in {"review", "reopen"} and role != "manager":
        allowed = False
    assert response.status_code == (200 if allowed else 403)
    if allowed:
        assert response.context["page_documentation"]["id"].startswith("quality-deviation-")


def test_stale_edit_and_review_preserve_posted_revision_and_values(client, people):
    deviation = create(people, complete=True)
    client.force_login(people["technician"])
    original = values(title="Unsaved synthetic title")
    post = {
        **original,
        "occurred_at": timezone.localtime(original["occurred_at"]).isoformat(),
        "revision": "1",
        "reason": "My correction",
    }
    deviation = services.update_deviation(
        actor=people["technician2"],
        deviation_id=deviation.pk,
        expected_revision=1,
        values={"title": "A newer title"},
        reason="Another person's correction",
    )
    response = client.post(reverse("quality:deviation-edit", args=[deviation.pk]), post)
    assert response.status_code == 200
    assert response.context["blocked"]
    form = response.context["form"]
    assert form["revision"].value() == "1"
    assert form["title"].value() == "Unsaved synthetic title"
    assert "disabled" in form["title"].field.widget.attrs
    assert deviation.events.count() == 2
    deviation = submit(people, deviation)
    client.force_login(people["manager"])
    review(people, deviation, decision="return")
    response = client.post(
        reverse("quality:deviation-review", args=[deviation.pk]),
        {
            "revision": "3",
            "decision": "close",
            "reason": "Unsaved review reason",
        },
    )
    assert response.context["blocked"]
    assert response.context["form"]["reason"].value() == "Unsaved review reason"
    assert response.context["form"]["revision"].value() == "3"
    deviation.refresh_from_db()
    assert deviation.status == "open" and deviation.revision == 4


def test_list_search_status_sort_invalid_filter_and_readable_history(client, people):
    first = create(people, complete=True, title="Zulu synthetic record")
    second = create(people, title="Alpha demonstration", description="Distinct synthetic wording")
    first = submit(people, first)
    client.force_login(people["viewer"])
    url = reverse("quality:deviation-list")
    for query, expected in [
        (first.number.lower(), [first.pk]),
        ("distinct", [second.pk]),
        ("demonstration", [second.pk]),
        ("no-such-report", []),
    ]:
        response = client.get(url, {"q": query})
        assert [item.pk for item in response.context["page_obj"]] == expected
    response = client.get(url, {"status": "awaiting_review"})
    assert [item.pk for item in response.context["page_obj"]] == [first.pk]
    response = client.get(url, {"sort": "title"})
    assert [item.pk for item in response.context["page_obj"]] == [second.pk, first.pk]
    response = client.get(url, {"status": "invented"})
    assert response.context["filter_form"].errors
    assert not response.context["page_obj"]
    assert client.get(url, {"per_page": "999"}).status_code == 400
    response = client.get(reverse("quality:deviation-detail", args=[first.pk]))
    rendered = response.content.decode()
    assert "Read-only access" in rendered
    assert people["technician"].username in rendered
    assert "Awaiting review" in rendered
    assert "Record review</a>" not in rendered
