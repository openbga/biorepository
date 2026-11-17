"""Record a destruction instruction from consent and continue to ordinary disposal requests."""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone
from helpers import field, sign_in
from playwright.sync_api import expect

from distribution import services
from distribution.tests import conftest as distribution_fixtures
from specimens.models import DisposalRequest
from specimens.tests.conftest import PASSWORD

pytestmark = pytest.mark.django_db(transaction=True)
people = distribution_fixtures.people
world = distribution_fixtures.world
dist = distribution_fixtures.dist


def test_escalate_an_existing_withdrawal_and_open_ordinary_disposals(page, people, dist):
    consent = dist.consent
    withdrawal = services.withdraw_consent(
        actor=people["manager"],
        consent_id=consent.pk,
        expected_revision=consent.revision,
        kind="no_use",
        effective_on=timezone.localdate(),
        received_via="Initial letter (synthetic)",
        occurred_at=timezone.now() - timedelta(minutes=5),
        reason="Initial request to stop use (synthetic)",
    )
    hold_ids = set(withdrawal.holds.values_list("hold_id", flat=True))
    sign_in(page, people["manager2"].username, PASSWORD)
    page.goto(reverse("distribution:consent-detail", args=[consent.pk]))
    more = page.get_by_role("button", name="More", exact=True)
    more.click()
    page.get_by_role("link", name="Escalate to destruction", exact=True).click()

    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()
    expect(page.locator("#vr-form-content")).to_have_attribute("aria-busy", "false")
    expect(dialog.get_by_role("heading", name="Escalate to destruction")).to_be_visible()
    dialog.locator("[data-vr-documentation-toggle]").click()
    help_panel = page.locator("#vr-documentation")
    expect(help_panel).to_be_visible()
    expect(help_panel.get_by_role("heading", level=2)).to_have_text("Escalate to destruction")
    expect(help_panel).to_contain_text("File disposal requests".lower(), ignore_case=True)
    help_panel.get_by_role("button", name="Close documentation").click()

    field(dialog, "Received via").fill("Second letter (synthetic)")
    reason = "Participant requested destruction in a second letter (synthetic)"
    field(dialog, "Reason").fill(reason)
    dialog.get_by_role("button", name="Escalate to destruction", exact=True).click()
    expect(dialog).to_be_hidden()
    expect(page.get_by_text("Recorded the destruction instruction.", exact=False)).to_be_visible()

    history = page.locator("section[aria-labelledby=withdrawals-heading]")
    expect(history).to_contain_text("No further use")
    history.get_by_text("Escalated to destruction", exact=True).click()
    expect(history.get_by_text(reason, exact=True)).to_be_visible()
    assert set(
        withdrawal.holds.filter(hold__release__isnull=True).values_list("hold_id", flat=True)
    ) == (hold_ids)
    assert not DisposalRequest.objects.exists()

    more.click()
    page.get_by_role("link", name="Request disposal of held specimens", exact=True).click()
    expect(dialog).to_be_visible()
    expect(dialog).to_contain_text("approval by another person with disposal approval")
    field(dialog, "Disposal method").fill("Ordinary disposal (synthetic)")
    field(dialog, "Destination").fill("Example waste stream")
    field(dialog, "Reason").fill(reason)
    dialog.get_by_role("button", name="Request disposal of held specimens", exact=True).click()
    expect(dialog).to_be_hidden()
    expect(page.get_by_text("Filed 3 disposal request(s)", exact=False)).to_be_visible()
    assert DisposalRequest.objects.count() == len(hold_ids)
    assert page.console_errors == []
