from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from unittest.mock import patch

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from core.models import AuditEvent
from quality import training_services as services
from quality.models import TrainingEvent, TrainingRecord
from quality.training_forms import TrainingForm

pytestmark = pytest.mark.django_db


def create(people, **values):
    return services.create_training(
        actor=people["manager"],
        trainee_id=people["technician"].pk,
        values={"topic": "Synthetic LN2 observation", **values},
        reason="Synthetic assignment",
    )


def completion(**overrides):
    return {
        "completed_on": timezone.localdate() - timedelta(days=2),
        "trainer_provider": "Synthetic Example Trainer",
        "completion_evidence": "Synthetic observed exercise",
        "competency_assessment": "Synthetic human assessment",
        **overrides,
    }


def change(people, record, action, **kwargs):
    return getattr(services, f"{action}_training")(
        actor=people["manager"],
        training_id=record.pk,
        expected_revision=record.revision,
        reason="Synthetic reason",
        **kwargs,
    )


@pytest.mark.parametrize("role", ["technician", "viewer", "nobody", "bare_superuser", "inactive"])
@pytest.mark.parametrize("action", ["create", "update", "complete", "correct", "cancel"])
def test_mutation_roles(people, role, action):
    record = create(people)
    actor = people.get(role)
    if role == "bare_superuser":
        actor = User.objects.create_superuser("training-root", "root@example.invalid", "fake-pass")
    if role == "inactive":
        actor = people["manager"]
        actor.is_active = False
        actor.save()
    kwargs = {"actor": actor, "reason": "Denied synthetic attempt"}
    if action == "create":
        kwargs.update(trainee_id=people["technician"].pk, values={"topic": "Synthetic"})
    else:
        kwargs.update(training_id=record.pk, expected_revision=record.revision)
        if action != "cancel":
            kwargs["values"] = completion() if action == "complete" else {"topic": "Changed"}
    with pytest.raises(PermissionDenied):
        getattr(services, f"{action}_training")(**kwargs)
    assert record.events.count() == 1


@pytest.mark.parametrize("role", ["admin", "manager", "technician", "viewer", "nobody"])
def test_read_roles(people, role):
    record = create(people)
    if role == "nobody":
        with pytest.raises(PermissionDenied):
            services.get_training(actor=people[role], training_id=record.pk)
    else:
        assert services.get_training(actor=people[role], training_id=record.pk) == record


def test_eligible_trainee_and_immutable_identity_and_form(people):
    assert not people["technician"].is_staff
    record = create(people)
    for trainee in (people["nobody"],):
        with pytest.raises(ValidationError, match="active staff"):
            services.create_training(
                actor=people["manager"],
                trainee_id=trainee.pk,
                values={"topic": "Synthetic"},
                reason="Synthetic",
            )
    trainee = people["technician"]
    trainee.is_active = False
    trainee.save()
    trainee.groups.clear()
    with pytest.raises(ValidationError):
        services.create_training(
            actor=people["manager"],
            trainee_id=trainee.pk,
            values={"topic": "Synthetic"},
            reason="Synthetic",
        )
    form = TrainingForm(actor=people["manager"])
    assert trainee not in form.fields["trainee"].queryset
    record = change(
        people, record, "update", values={"topic": "Synthetic correction after departure"}
    )
    record = change(people, record, "complete", values=completion())
    record = change(
        people, record, "correct", values={"completion_evidence": "Synthetic corrected evidence"}
    )
    assert record.trainee_id == trainee.pk
    with pytest.raises(ValidationError, match="trainee cannot change"):
        change(people, record, "correct", values={"trainee_id": people["viewer"].pk})


def test_manager_can_record_own_training_and_exact_history(people):
    stamp = timezone.now().replace(microsecond=123456)
    with patch("quality.training_services.timezone.now", return_value=stamp):
        record = services.create_training(
            actor=people["manager"],
            trainee_id=people["manager"].pk,
            values={"topic": " Synthetic own training "},
            reason=" Synthetic ",
        )
    first = record.events.get()
    assert first.changes["created_at"] == [None, stamp.isoformat()]
    assert first.changes["topic"] == [None, "Synthetic own training"]
    record = change(people, record, "complete", values=completion())
    original = completion()["completed_on"]
    record = change(
        people,
        record,
        "correct",
        values={
            "topic": "Synthetic corrected topic",
            "procedure_reference": "SYN-02 v2",
            "completed_on": original - timedelta(days=1),
            "renewal_due": original,
        },
    )
    assert record.events.last().changes["completed_on"] == [
        original.isoformat(),
        (original - timedelta(days=1)).isoformat(),
    ]
    record = change(people, record, "cancel")
    assert (
        record.status == "cancelled" and record.completion_evidence == "Synthetic observed exercise"
    )
    assert list(record.events.values_list("kind", flat=True)) == [
        "created",
        "completed",
        "corrected",
        "cancelled",
    ]
    assert (
        AuditEvent.objects.filter(
            object_type="quality.trainingrecord", object_id=str(record.pk)
        ).count()
        == 4
    )
    for action in ("update", "complete", "correct", "cancel"):
        with pytest.raises(ValidationError) as exc:
            change(people, record, action, **({} if action == "cancel" else {"values": {}}))
        assert exc.value.code == "invalid_transition"


@pytest.mark.parametrize(
    "values",
    [
        {"completed_on": None},
        {"completed_on": timezone.localdate() + timedelta(days=1)},
        {"completed_on": "2026-01-01"},
        {"completed_on": timezone.now()},
        {"trainer_provider": " "},
        {"completion_evidence": " "},
        {"renewal_due": timezone.localdate() - timedelta(days=3)},
    ],
)
def test_completion_validation_is_atomic(people, values):
    record = create(people)
    with pytest.raises(ValidationError):
        change(people, record, "complete", values=completion(**values))
    record.refresh_from_db()
    assert record.status == "assigned" and record.revision == 1 and record.events.count() == 1


def test_reasons_noop_state_and_stale_validation(people):
    record = create(people)
    with pytest.raises(ValidationError, match="at least one"):
        change(people, record, "update", values={"topic": record.topic})
    for reason in ("", " "):
        with pytest.raises(ValidationError):
            services.cancel_training(
                actor=people["manager"], training_id=record.pk, expected_revision=1, reason=reason
            )
    with pytest.raises(ValidationError):
        change(people, record, "correct", values={"topic": "Synthetic"})
    record = change(people, record, "complete", values=completion())
    with pytest.raises(ValidationError, match="at least one"):
        change(people, record, "correct", values=completion())
    for revision in (1, True, "2", None):
        with pytest.raises(ValidationError) as exc:
            services.cancel_training(
                actor=people["manager"],
                training_id=record.pk,
                expected_revision=revision,
                reason="Synthetic",
            )
        assert exc.value.code == "stale_revision"
    assert record.events.count() == 2


def test_audit_failure_rolls_back_domain_and_event(people):
    record = create(people)
    with patch(
        "quality.training_services.record_event", side_effect=RuntimeError("Synthetic failure")
    ):
        with pytest.raises(RuntimeError):
            change(people, record, "complete", values=completion())
    record.refresh_from_db()
    assert record.status == "assigned" and record.events.count() == 1


def test_database_constraints_retention_and_history_protection(people):
    record = create(people)
    event = record.events.get()
    for mutation in (
        lambda: TrainingRecord.objects.filter(pk=record.pk).delete(),
        lambda: TrainingEvent.objects.filter(pk=event.pk).delete(),
        lambda: TrainingEvent.objects.filter(pk=event.pk).update(reason="Rewrite"),
        lambda: TrainingRecord.objects.filter(pk=record.pk).update(status="invented"),
        lambda: TrainingRecord.objects.filter(pk=record.pk).update(topic=" "),
        lambda: TrainingRecord.objects.filter(pk=record.pk).update(status="completed"),
        lambda: TrainingRecord.objects.filter(pk=record.pk).update(revision=0),
    ):
        with pytest.raises(DatabaseError), transaction.atomic():
            mutation()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("action", ["complete", "update", "correct", "cancel"])
def test_concurrent_mutations_commit_once(people, action):
    record = create(people)
    if action == "correct":
        record = change(people, record, "complete", values=completion())
    initial_revision = record.revision
    barrier = Barrier(2)

    def worker():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            kwargs = (
                {}
                if action == "cancel"
                else {
                    "values": completion()
                    if action == "complete"
                    else {"topic": "Concurrent synthetic"}
                }
            )
            change(people, record, action, **kwargs)
            return "saved"
        except ValidationError as exc:
            return exc.code
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: worker(), range(2)))
    assert sorted(outcomes) == ["saved", "stale_revision"]
    record.refresh_from_db()
    assert record.revision == initial_revision + 1
    assert record.events.count() == initial_revision + 1


@pytest.mark.parametrize(
    "route", ["list", "detail", "create", "edit", "complete", "correct", "cancel"]
)
@pytest.mark.parametrize("role", ["viewer", "technician", "manager", "admin", "nobody"])
def test_page_roles_and_help(client, people, route, role):
    record = create(people)
    client.force_login(people[role])
    response = client.get(
        reverse(
            f"quality:training-{route}", args=[record.pk] if route not in {"list", "create"} else []
        )
    )
    allowed = role != "nobody" and (route in {"list", "detail"} or role in {"admin", "manager"})
    assert response.status_code == (200 if allowed else 403)
    if allowed:
        assert response.context["page_documentation"]["id"].startswith("quality-training-")


def test_stale_forms_preserve_input_and_completion_errors(client, people):
    record = create(people)
    change(people, record, "update", values={"topic": "New synthetic topic"})
    client.force_login(people["manager"])
    response = client.post(
        reverse("quality:training-edit", args=[record.pk]),
        {
            "revision": "1",
            "topic": "Unsaved synthetic topic",
            "reason": "Unsaved reason",
        },
    )
    assert response.context["blocked"]
    form = response.context["form"]
    assert form["topic"].value() == "Unsaved synthetic topic" and form["revision"].value() == "1"
    assert form["topic"].field.widget.attrs["disabled"]
    response = client.post(
        reverse("quality:training-complete", args=[record.pk]),
        {
            "revision": "2",
            **completion(completed_on=timezone.localdate() + timedelta(days=1)),
            "reason": "Synthetic",
        },
    )
    assert response.context["form"]["completed_on"].errors
    assert response.context["form"]["completion_evidence"].value() == "Synthetic observed exercise"


def test_list_search_filters_sort_pagination_and_readable_history(client, people):
    today = timezone.localdate()
    overdue = create(people, due_on=today - timedelta(days=1))
    upcoming = create(
        people,
        topic="Alpha synthetic course",
        procedure_reference="SYN-42 v3",
        due_on=today + timedelta(days=30),
    )
    renewal = create(people)
    renewal = change(
        people, renewal, "complete", values=completion(renewal_due=today - timedelta(days=1))
    )
    cancelled = create(people, due_on=today - timedelta(days=1))
    change(people, cancelled, "cancel")
    client.force_login(people["viewer"])
    url = reverse("quality:training-list")
    for params, expected in [
        ({"q": upcoming.number.lower()}, [upcoming.pk]),
        ({"q": "SYN-42"}, [upcoming.pk]),
        ({"q": "Alpha"}, [upcoming.pk]),
        ({"due": "assignment_overdue"}, [overdue.pk]),
        ({"due": "renewal_overdue"}, [renewal.pk]),
        ({"due": "next_30_days"}, [upcoming.pk]),
        ({"q": "no-such-topic"}, []),
    ]:
        response = client.get(url, params)
        assert [row.pk for row in response.context["page_obj"]] == expected
    response = client.get(url, {"q": people["technician"].username, "sort": "number"})
    assert len(response.context["page_obj"]) == 4
    assert response.context["page_obj"][0] == overdue
    response = client.get(url, {"due": "invented"})
    assert response.context["filter_form"].errors and not response.context["page_obj"]
    assert client.get(url, {"per_page": "999"}).status_code == 400
    response = client.get(reverse("quality:training-detail", args=[renewal.pk]))
    assert people["manager"].username in response.content.decode()
    assert "Read-only access" in response.content.decode()
    assert "Completed" in response.content.decode()


def test_record_database_delete_guard_without_orm_collection(people):
    record = create(people)
    with (
        pytest.raises(DatabaseError, match="Historical records cannot be updated or deleted"),
        transaction.atomic(),
    ):
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM quality_trainingrecord WHERE id = %s", [record.pk])
    assert TrainingRecord.objects.filter(pk=record.pk).exists()


def test_due_date_boundaries_use_repository_timezone(client, people):
    # UTC has moved to Nov 15, while the deployment's local day is still Nov 14.
    local_today = datetime(2026, 11, 14).date()
    today = create(people, due_on=local_today)
    overdue = create(people, due_on=local_today - timedelta(days=1))
    day30 = create(people, due_on=local_today + timedelta(days=30))
    create(people, due_on=local_today + timedelta(days=31))
    client.force_login(people["viewer"])
    instant = datetime(2026, 11, 15, 0, 30, tzinfo=UTC)
    with (
        timezone.override("America/New_York"),
        patch("quality.training_lists.timezone.now", return_value=instant),
    ):
        response = client.get(reverse("quality:training-list"), {"due": "assignment_overdue"})
        assert [row.pk for row in response.context["page_obj"]] == [overdue.pk]
        response = client.get(
            reverse("quality:training-list"), {"due": "next_30_days", "sort": "number"}
        )
        assert [row.pk for row in response.context["page_obj"]] == [today.pk, day30.pk]
