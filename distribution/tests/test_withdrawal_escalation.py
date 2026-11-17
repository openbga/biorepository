"""A later destruction instruction retains the original withdrawal and independent approvals."""

import csv
import io
from datetime import timedelta
from functools import partial

import pytest
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from core.models import AuditEvent
from distribution import services
from distribution.models import (
    Consent,
    ConsentReinstatement,
    ConsentWithdrawal,
    ConsentWithdrawalEscalation,
)
from specimens.models import DisposalRequest
from specimens.tests.conftest import decide

from .helpers import (
    allocate,
    approved_request,
    fast_password_hashing,  # noqa: F401
    fresh,
    grant_disposal_approval,
    refused,
)
from .test_services_consent import reconsent, reinstate, withdraw
from .test_views_governance import when

pytestmark = pytest.mark.django_db
ACTION = "distribution.withdrawal_escalated"
REASON = "Later instruction to destroy (synthetic)"


def escalation_values(people, consent):
    consent.refresh_from_db()
    return {
        "actor": people["manager2"],
        "consent_id": consent.pk,
        "expected_revision": consent.revision,
        "effective_on": timezone.localdate(),
        "received_via": "Letter (synthetic)",
        "occurred_at": timezone.now(),
        "reason": REASON,
    }


def test_escalation_retains_original_holds_and_allocation_history(people, dist, world):
    consent = dist.consent
    request = approved_request(people, dist, world)
    (allocation,) = allocate(people, request, dist.specimens[:1])
    withdrawal = withdraw(people, consent)
    original = ConsentWithdrawal.objects.values().get(pk=withdrawal.pk)
    hold_links = set(withdrawal.holds.values_list("hold_id", flat=True))
    allocation_events = list(allocation.events.values_list("pk", flat=True))
    consent_before = Consent.objects.values().get(pk=consent.pk)

    instruction = services.escalate_withdrawal(**escalation_values(people, consent))
    assert instruction.withdrawal_id == withdrawal.pk
    assert ConsentWithdrawal.objects.values().get(pk=withdrawal.pk) == original
    assert consent.withdrawals.count() == 1
    assert set(withdrawal.holds.values_list("hold_id", flat=True)) == hold_links
    assert not withdrawal.holds.filter(hold__release__isnull=False).exists()
    assert fresh(allocation).state == "released"
    assert list(allocation.events.values_list("pk", flat=True)) == allocation_events
    assert not DisposalRequest.objects.exists()
    assert Consent.objects.values().get(pk=consent.pk) == {
        **consent_before,
        "revision": consent_before["revision"] + 1,
    }
    row = services.open_withdrawal(consent)
    assert row.kind == "no_use"
    assert row.effective_kind == "destroy"
    assert row.get_effective_kind_display() == "No further use and destroy"
    event = AuditEvent.objects.get(action=ACTION)
    assert event.actor == people["manager2"] and event.reason == REASON
    assert event.before == {"kind": "no_use", "withdrawal": str(withdrawal.pk)}
    assert event.after == {
        "kind": "destroy",
        "withdrawal": str(withdrawal.pk),
        "escalation": str(instruction.pk),
    }


@pytest.mark.parametrize("kind", [None, "no_contact", "destroy", "reinstated"])
def test_escalation_requires_an_open_no_use_withdrawal(people, dist, kind):
    consent = dist.consent
    if kind:
        withdraw(people, consent, "no_use" if kind == "reinstated" else kind)
    if kind == "reinstated":
        reinstate(people, consent)
    refused(
        lambda: services.escalate_withdrawal(**escalation_values(people, consent)),
        "invalid_transition",
    )
    assert not ConsentWithdrawalEscalation.objects.exists()


def test_escalation_validates_role_revision_and_chronology(people, dist):
    consent = dist.consent
    original = withdraw(people, consent)
    values = escalation_values(people, consent)
    for role in ("technician", "viewer", "nobody"):
        with pytest.raises(PermissionDenied):
            services.escalate_withdrawal(**{**values, "actor": people[role]})
    refused(
        lambda: services.escalate_withdrawal(
            **{**values, "expected_revision": consent.revision - 1}
        ),
        "stale_revision",
    )
    for field, value in (
        ("effective_on", original.effective_on - timedelta(days=1)),
        ("effective_on", timezone.localdate() + timedelta(days=1)),
        ("occurred_at", original.occurred_at - timedelta(seconds=1)),
        ("occurred_at", timezone.now() + timedelta(hours=1)),
    ):
        error = refused(
            partial(services.escalate_withdrawal, **{**values, field: value}),
            "invalid_time",
        )
        assert field in error.error_dict
    for field in ("received_via", "reason"):
        refused(partial(services.escalate_withdrawal, **{**values, field: " "}), "required")
    assert not ConsentWithdrawalEscalation.objects.exists()
    services.escalate_withdrawal(**values)
    refused(
        lambda: services.escalate_withdrawal(**escalation_values(people, consent)),
        "invalid_transition",
    )
    assert ConsentWithdrawalEscalation.objects.count() == 1


def test_escalation_and_revision_roll_back_if_audit_fails(people, dist, monkeypatch):
    consent = dist.consent
    original = withdraw(people, consent)
    values = escalation_values(people, consent)

    def fail(**kwargs):
        raise RuntimeError("Synthetic audit failure")

    monkeypatch.setattr(services, "record_event", fail)
    with pytest.raises(RuntimeError, match="Synthetic audit failure"):
        services.escalate_withdrawal(**values)
    assert not ConsentWithdrawalEscalation.objects.exists()
    assert fresh(consent).revision == values["expected_revision"]
    assert fresh(original).effective_kind == "no_use"


def test_escalated_disposal_requests_still_require_independent_approval(people, dist):
    consent = dist.consent
    withdraw(people, consent)
    services.escalate_withdrawal(**escalation_values(people, consent))
    requester = grant_disposal_approval(people["manager2"])
    filed = services.request_withdrawal_disposals(
        actor=requester,
        consent_id=consent.pk,
        expected_revision=fresh(consent).revision,
        method="Ordinary disposal",
        destination="Example waste stream",
        reason=REASON,
    )
    assert {row.specimen_id for row in filed} == {row.pk for row in dist.specimens}
    refused(lambda: decide(requester, filed[0]), "self_approval")
    assert decide(people["approver"], filed[0]).decision == "approve"
    assert all(fresh(specimen).status == "available" for specimen in dist.specimens)


def test_reinstatement_cannot_bypass_intervening_consent_or_self_review(people, dist):
    consent = dist.consent
    withdrawal = withdraw(people, consent)
    between = reconsent(people, dist, actor="manager")
    escalation = services.escalate_withdrawal(**escalation_values(people, consent))

    refused(lambda: reinstate(people, consent, between, actor="manager2"), "self_approval")
    for record in (None, between):
        error = refused(partial(reinstate, people, consent, record), "invalid")
        assert "Record consent after the escalation" in error.message_dict["record"][0]
    assert not withdrawal.holds.filter(hold__release__isnull=False).exists()

    newer = reconsent(people, dist, actor="decider_manager")
    refused(lambda: reinstate(people, consent, newer, actor="decider_manager"), "self_approval")
    refused(
        lambda: services.reinstate_consent(
            actor=people["admin"],
            consent_id=consent.pk,
            expected_revision=fresh(consent).revision,
            record_id=newer.pk,
            occurred_at=escalation.occurred_at - timedelta(seconds=1),
            reason="Synthetic early reinstatement",
        ),
        "invalid_time",
    )
    reinstatement = reinstate(people, consent, newer)
    assert reinstatement.withdrawal_id == withdrawal.pk and reinstatement.record_id == newer.pk
    assert fresh(withdrawal).escalation.pk == escalation.pk
    assert not withdrawal.holds.filter(hold__release__isnull=True).exists()
    assert fresh(consent).status == "consented"


def test_database_refuses_older_writers_that_miss_the_escalation(client, people, dist):
    consent = dist.consent
    withdrawal = withdraw(people, consent)
    between = reconsent(people, dist, actor="manager")
    values = escalation_values(people, consent)
    # Exercise the strict boundary for system recording times, independent of receipt times.
    escalation = ConsentWithdrawalEscalation.objects.create(
        withdrawal=withdrawal,
        effective_on=values["effective_on"],
        received_via=values["received_via"],
        actor=values["actor"],
        occurred_at=values["occurred_at"],
        recorded_at=between.recorded_at,
        reason=REASON,
    )
    refused(lambda: reinstate(people, consent, between), "invalid")
    client.force_login(people["admin"])
    reinstate_url = reverse("distribution:consent-reinstate", args=[consent.pk])
    form = client.get(reinstate_url).context["form"]
    assert form.fields["record"].disabled
    assert not form.fields["record"].queryset.exists()
    newest = reconsent(people, dist, actor="decider_manager")
    consent.refresh_from_db()
    valid = {
        "consent": consent,
        "withdrawal": withdrawal,
        "record": newest,
        "actor": people["admin"],
        "occurred_at": timezone.now(),
        "reason": "Synthetic reinstatement",
    }
    for invalid in (
        {"actor": escalation.actor},
        {"record": between},
        {"record": None},
        {"occurred_at": escalation.occurred_at - timedelta(seconds=1)},
    ):
        with (
            pytest.raises(IntegrityError, match="independent review of the latest instruction"),
            transaction.atomic(),
        ):
            # This is the insert an older writer attempts before releasing the holds.
            ConsentReinstatement.objects.create(**{**valid, **invalid})
    assert not ConsentReinstatement.objects.exists()
    assert not withdrawal.holds.filter(hold__release__isnull=False).exists()
    assert fresh(consent).status == "withdrawn"


def test_escalation_form_authorizes_and_preserves_invalid_and_stale_input(client, people, dist):
    consent = dist.consent
    original = withdraw(people, consent)
    values = escalation_values(people, consent)
    data = {
        "expected_revision": values["expected_revision"],
        "effective_on": values["effective_on"].isoformat(),
        "received_via": values["received_via"],
        "occurred_at": when(values["occurred_at"]),
        "reason": REASON,
    }
    url = reverse("distribution:consent-escalate", args=[consent.pk])
    for role in ("technician", "viewer"):
        client.force_login(people[role])
        assert client.get(url).status_code == 403
        assert client.post(url, data).status_code == 403

    client.force_login(people["manager2"])
    page = client.get(url)
    assert page.context["page_documentation"]["id"] == "distribution-consent-escalate"
    invalid = client.post(
        url, {**data, "occurred_at": when(original.occurred_at - timedelta(days=1))}
    )
    assert invalid.status_code == 422
    assert invalid.context["form"].errors["occurred_at"]
    assert invalid.context["form"]["reason"].value() == REASON
    stale = client.post(url, {**data, "expected_revision": data["expected_revision"] - 1})
    assert stale.status_code == 422
    assert "changed since you opened it" in stale.context["form"].non_field_errors()[0]
    assert stale.context["form"]["reason"].value() == REASON
    assert int(stale.context["form"]["expected_revision"].value()) == data["expected_revision"]

    detail = reverse("distribution:consent-detail", args=[consent.pk])
    assert client.post(url, data)["Location"] == detail
    assert client.post(url, data).status_code == 409
    page = client.get(detail)
    assert b"Escalated to destruction" in page.content
    assert REASON.encode() in page.content
    assert reverse("distribution:consent-disposals", args=[consent.pk]).encode() in page.content
    reinstate_url = reverse("distribution:consent-reinstate", args=[consent.pk])
    assert b"You recorded the escalation" in client.get(reinstate_url).content
    follow_up_url = reverse("distribution:withdrawal-follow-up", args=[original.pk])
    facts = dict(client.get(follow_up_url).context["facts"])
    assert facts["Original withdrawal"].startswith("No further use,")
    assert facts["Destruction instruction"].startswith("No further use and destroy,")


def test_withdrawal_register_and_csv_filter_on_effective_instruction(client, people, dist):
    consent = dist.consent
    contact = withdraw(people, consent, kind="no_contact")
    original = withdraw(people, consent)
    services.escalate_withdrawal(**escalation_values(people, consent))
    reinstate(people, consent)
    next_no_use = withdraw(people, consent)
    client.force_login(people["viewer"])
    url = reverse("distribution:withdrawal-list")
    rows = client.get(url, {"kind": "no_use"}).context["page_obj"]
    assert [row.pk for row in rows] == [next_no_use.pk]
    page = client.get(url, {"kind": "destroy"})
    assert [row.pk for row in page.context["page_obj"]] == [original.pk]
    assert b"Originally no further use" in page.content
    assert b"Escalated by" in page.content
    page = client.get(url, {"sort": "kind"})
    assert [row.pk for row in page.context["page_obj"]] == [contact.pk, next_no_use.pk, original.pk]
    page = client.get(url, {"sort": "-kind"})
    assert [row.pk for row in page.context["page_obj"]] == [original.pk, next_no_use.pk, contact.pk]
    for field, value in (("kind", "invalid"), ("study", "invalid")):
        page = client.get(url, {field: value, "sort": "kind"})
        assert page.status_code == 200
        assert page.context["filter_form"].errors[field]
        assert not list(page.context["page_obj"])

    response = client.get(
        reverse("distribution:catalogue-export", args=["withdrawals"]), {"kind": "destroy"}
    )
    (row,) = csv.DictReader(io.StringIO(response.content.decode()))
    assert row["Withdrawal"] == "No further use"
    assert row["Effective withdrawal"] == "No further use and destroy"
    assert row["Escalation received via"] == "Letter (synthetic)"
    assert row["Escalation recorded by"] == people["manager2"].username
    assert row["Escalation reason"] == REASON
