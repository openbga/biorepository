"""Counts open their rows: every dashboard queue's number equals the rows its link shows.

Runs on the full synthetic demo, for every role and for named people who hold the disposal
approval and request decision permissions, so each queue is shown to someone.
"""

import pytest
from django.contrib.auth.models import Group, Permission
from django.core.management import call_command

from accounts.models import User
from distribution.models import SpecimenRequest

pytestmark = pytest.mark.django_db

ROLES = ("admin", "manager", "technician", "viewer")


@pytest.fixture
def demo(monkeypatch, settings):
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
    monkeypatch.setenv("DEMO_PASSWORD", "synthetic-test-password-only")
    call_command("seed_demo", full=True, verbosity=0)


def decider(role):
    """A synthetic person in `role` who holds both explicit approval permissions."""
    user = User.objects.create_user(
        f"counts-{role}", f"counts-{role}@example.invalid", "test-password-729!"
    )
    user.groups.add(Group.objects.get(name=role))
    user.user_permissions.add(
        *Permission.objects.filter(
            content_type__app_label="accounts",
            codename__in=("approve_disposal", "approve_distribution"),
        )
    )
    return user


def test_every_dashboard_count_equals_the_rows_its_link_shows(client, demo):
    people = [User.objects.get(username=f"demo_{role}") for role in ROLES]
    deciders = [decider(role) for role in ("manager", "technician")]
    people += deciders
    # A submitted request the manager created: their decision queue must leave it out, and so
    # must the list the queue links to.
    submitted = SpecimenRequest.objects.filter(status=SpecimenRequest.Status.SUBMITTED)
    assert submitted.exists(), "the demo needs submitted requests for this check"
    submitted.filter(pk=submitted.first().pk).update(created_by=deciders[0])
    checked, labels, mismatches, journey_checks = 0, set(), [], 0
    for person in people:
        client.force_login(person)
        dashboard = client.get("/").context
        queues = dashboard["queues"]
        journey_checks += check_journey(client, person, dashboard["journey"], queues, mismatches)
        for queue in queues:
            if not queue.get("url"):
                continue
            response = client.get(queue["url"])
            assert response.status_code == 200, (person.username, queue["label"], queue["url"])
            rows = response.context["page_obj"].paginator.count
            checked += 1
            labels.add(queue["label"])
            if rows != queue["count"]:
                mismatches.append(
                    f"{person.username}: {queue['label']} shows {queue['count']}, "
                    f"its list {queue['url']} has {rows} rows"
                )
    assert not mismatches, "\n".join(mismatches)
    # Every queue was shown to somebody, so none escaped the comparison.
    assert {
        "Requests awaiting my decision",
        "Disposal requests awaiting my approval",
        "Excursions awaiting my review",
        "Approved requests with allocations to pick",
        "Returns awaiting review",
        "Withdrawals needing follow-up",
    } <= labels, sorted(labels)
    assert checked > 40
    assert journey_checks >= len(people) * 3


def check_journey(client, person, journey, queues, mismatches):
    """Each journey stage that links opens as many rows as it counts (Receive, Store, Check out);
    Distribute and Dispose count the sum of their queues, which are the dashboard's own."""
    assert [stage["key"] for stage in journey] == [
        "receive",
        "store",
        "out",
        "distribute",
        "dispose",
    ]
    checks = 0
    for stage in journey:
        assert all(any(q is queue for queue in queues) for q in stage["queues"]), stage["key"]
        if stage["key"] in ("distribute", "dispose"):
            total = sum(q["count"] for q in stage["queues"])
            if stage["count"] != total:
                mismatches.append(
                    f"{person.username}: journey {stage['key']} {stage['count']} "
                    f"is not the sum of its queues ({total})"
                )
        elif stage["url"]:
            rows = client.get(stage["url"]).context["page_obj"].paginator.count
            if rows != stage["count"]:
                mismatches.append(
                    f"{person.username}: journey {stage['key']} shows "
                    f"{stage['count']}, its list {stage['url']} has {rows} rows"
                )
        checks += 1
    return checks
