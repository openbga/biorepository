"""Governance pages: every page renders for each reading role, writes are refused to technicians
and viewers, viewers see no write controls, consent withdrawal and reinstatement through the
pages keep their four-eyes rule, the participant filter the specimen record links to works,
attachments download only as files, CSV exports are audited, and the dashboard queues render and
link to filtered lists. All data is synthetic."""

import csv
import io
from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from distribution import services
from distribution.models import (
    AgreementDocument,
    Consent,
    ConsentHold,
    ConsentRecord,
    OutputRecord,
    WithdrawalFollowUp,
)

from .conftest import (
    allocate,
    approved_request,
    catalogue,
    dispatch,
    packed_shipment,
    pick,
    register,
)

pytestmark = pytest.mark.django_db
PDF = b"%PDF-1.4\n% synthetic test document\n%%EOF\n"
READERS = ("viewer", "technician", "manager", "admin")


@pytest.fixture(autouse=True)
def without_page_help(monkeypatch):
    """The page help names every control; these tests look for the controls themselves, so the
    help panel is left out of the rendered page (its coverage is tests/test_page_docs.py)."""
    monkeypatch.setattr("core.context_processors.documentation_for_request", lambda request: {})


@pytest.fixture(autouse=True)
def plain_static_storage(settings):
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


def as_user(client, user):
    client.force_login(user)
    return client


def when(moment=None):
    return timezone.localtime(moment or timezone.now()).strftime("%Y-%m-%dT%H:%M:%S")


def withdraw(client, consent, kind="no_use", **extra):
    consent.refresh_from_db()
    data = {
        "kind": kind,
        "effective_on": timezone.localdate().isoformat(),
        "received_via": "Letter to the repository (synthetic)",
        "occurred_at": when(timezone.now() - timedelta(minutes=5)),
        "reason": "Participant asked to stop use (synthetic)",
        "expected_revision": consent.revision,
        **extra,
    }
    return client.post(reverse("distribution:consent-withdraw", args=[consent.pk]), data)


def page_urls(dist):
    return [
        reverse("distribution:study-list"),
        reverse("distribution:study-detail", args=[dist.study.pk]),
        reverse("distribution:consent-list"),
        reverse("distribution:consent-detail", args=[dist.consent.pk]),
        reverse("distribution:withdrawal-list"),
        reverse("distribution:agreement-list"),
        reverse("distribution:agreement-detail", args=[dist.agreement.pk]),
        reverse("distribution:category-list"),
        reverse("distribution:consent-form-list"),
        reverse("distribution:institution-list"),
        reverse("distribution:investigator-list"),
    ]


def write_urls(dist):
    link = dist.study.collection_links.get()
    return [
        reverse("distribution:study-create"),
        reverse("distribution:study-edit", args=[dist.study.pk]),
        reverse("distribution:study-collection-link", args=[dist.study.pk]),
        reverse("distribution:study-collection-end", args=[link.pk]),
        reverse("distribution:consent-create"),
        reverse("distribution:consent-correct", args=[dist.consent.pk]),
        reverse("distribution:consent-withdraw", args=[dist.consent.pk]),
        reverse("distribution:consent-hold", args=[dist.consent.pk]),
        reverse("distribution:consent-disposals", args=[dist.consent.pk]),
        reverse("distribution:consent-reinstate", args=[dist.consent.pk]),
        reverse("distribution:agreement-create"),
        reverse("distribution:agreement-edit", args=[dist.agreement.pk]),
        reverse("distribution:agreement-execute", args=[dist.agreement.pk]),
        reverse("distribution:agreement-terminate", args=[dist.agreement.pk]),
        reverse("distribution:agreement-document", args=[dist.agreement.pk]),
        reverse("distribution:category-create"),
        reverse("distribution:category-edit", args=[dist.categories["GENERAL"].pk]),
        reverse("distribution:consent-form-create"),
        reverse("distribution:consent-form-edit", args=[dist.form.pk]),
        reverse("distribution:institution-create"),
        reverse("distribution:institution-edit", args=[dist.institution.pk]),
        reverse("distribution:investigator-create"),
        reverse("distribution:investigator-edit", args=[dist.investigator.pk]),
    ]


# --- rendering and roles --------------------------------------------------------------------


@pytest.mark.parametrize("role", READERS)
def test_every_governance_page_renders_for_each_reading_role(client, people, dist, role):
    as_user(client, people[role])
    for url in page_urls(dist):
        response = client.get(url)
        assert response.status_code == 200, (role, url)


def test_manager_forms_render(client, people, dist):
    as_user(client, people["manager"])
    for url in write_urls(dist):
        response = client.get(url)
        assert response.status_code == 200, url


def test_signed_out_and_roleless_users_are_refused(client, people, dist):
    url = reverse("distribution:consent-detail", args=[dist.consent.pk])
    assert client.get(url).status_code == 302
    as_user(client, people["nobody"])
    assert client.get(url).status_code == 403


@pytest.mark.parametrize("role", ("technician", "viewer"))
def test_governance_writes_are_refused_to_technicians_and_viewers(client, people, dist, role):
    as_user(client, people[role])
    before = Consent.objects.get(pk=dist.consent.pk).revision
    for url in write_urls(dist):
        assert client.get(url).status_code == 403, url
        assert client.post(url, {"reason": "Not allowed (synthetic)"}).status_code == 403, url
    assert Consent.objects.get(pk=dist.consent.pk).revision == before
    assert client.get(
        reverse("distribution:withdrawal-follow-up", args=[dist.consent.pk])
    ).status_code in (403, 404)


def test_viewer_sees_no_write_controls(client, people, dist):
    as_user(client, people["viewer"])
    consent = client.get(reverse("distribution:consent-detail", args=[dist.consent.pk]))
    html = consent.content.decode()
    for text in ("Withdraw consent", "Correct consent", "Reinstate consent", "Record consent"):
        assert text not in html, text
    assert reverse("distribution:consent-withdraw", args=[dist.consent.pk]) not in html
    agreement = client.get(
        reverse("distribution:agreement-detail", args=[dist.agreement.pk])
    ).content.decode()
    for text in ("Terminate agreement", "Add document", ">Edit<"):
        assert text not in agreement, text
    study = client.get(reverse("distribution:study-detail", args=[dist.study.pk])).content.decode()
    assert "Link collection" not in study and "End link" not in study
    categories = client.get(reverse("distribution:category-list")).content.decode()
    assert reverse("distribution:category-edit", args=[dist.categories["GENERAL"].pk]) not in (
        categories
    )
    assert "New scope category" not in categories


def test_manager_sees_controls_and_catalogue_codes_open_the_edit_form(client, people, dist):
    as_user(client, people["manager"])
    html = client.get(
        reverse("distribution:consent-detail", args=[dist.consent.pk])
    ).content.decode()
    assert reverse("distribution:consent-withdraw", args=[dist.consent.pk]) in html
    assert "Correct consent" in html
    categories = client.get(reverse("distribution:category-list")).content.decode()
    assert reverse("distribution:category-edit", args=[dist.categories["GENERAL"].pk]) in (
        categories
    )


def test_consent_page_shows_what_is_recorded(client, people, dist):
    as_user(client, people["viewer"])
    html = client.get(
        reverse("distribution:consent-detail", args=[dist.consent.pk])
    ).content.decode()
    assert "P-000123 · DEMO-STUDY" in html
    assert "DEMO-BROAD v1" in html
    assert "ConsentRecord object" not in html
    assert "Consent binder 1 (synthetic)" in html
    for specimen in dist.specimens:
        assert specimen.accession in html


# --- consent list filter --------------------------------------------------------------------


def test_consent_list_filters_to_one_coded_participant(client, people, dist, world):
    other = catalogue(people["manager"], "participant", code="P-000124")
    services.record_consent(
        actor=people["manager"],
        participant_id=other.pk,
        study_id=dist.study.pk,
        form_id=dist.form.pk,
        decision="refused",
        answers={},
        consented_on=timezone.localdate(),
        signer="participant",
        documentation="signed",
        source_reference="",
        restrictions="",
        expires_on=None,
        expected_revision=None,
        reason="Transcribed (synthetic)",
    )
    as_user(client, people["viewer"])
    url = reverse("distribution:consent-list")
    everything = client.get(url).content.decode()
    assert "P-000123 · DEMO-STUDY" in everything and "P-000124 · DEMO-STUDY" in everything
    one = client.get(url, {"participant": "P-000123"}).content.decode()
    assert "P-000123 · DEMO-STUDY" in one and "P-000124 · DEMO-STUDY" not in one
    lower = client.get(url, {"participant": "p-000124"}).content.decode()
    assert "P-000124 · DEMO-STUDY" in lower and "P-000123 · DEMO-STUDY" not in lower
    none = client.get(url, {"participant": "P-999999"}).content.decode()
    assert "Nothing matches these filters" in none


# --- withdrawal, holds and reinstatement ----------------------------------------------------


def test_withdrawal_from_the_page_holds_the_participants_specimens(client, people, dist):
    as_user(client, people["manager"])
    response = withdraw(client, dist.consent)
    assert response.status_code == 302
    consent = Consent.objects.get(pk=dist.consent.pk)
    assert consent.status == Consent.Status.WITHDRAWN and consent.no_contact
    held = set(ConsentHold.objects.values_list("specimen_id", flat=True))
    assert held == {s.pk for s in dist.specimens}
    as_user(client, people["viewer"])
    html = client.get(reverse("distribution:consent-detail", args=[consent.pk])).content.decode()
    assert "3 held, 0 not held by a withdrawal" in html
    for specimen in dist.specimens:
        assert specimen.accession in html
    withdrawals = client.get(reverse("distribution:withdrawal-list")).content.decode()
    assert "No further use" in withdrawals and "In effect" in withdrawals


def test_specimens_registered_after_withdrawal_are_offered_for_holding(client, people, dist, world):
    as_user(client, people["manager"])
    withdraw(client, dist.consent)
    late = register(people["technician"], world)
    url = reverse("distribution:consent-detail", args=[dist.consent.pk])
    html = client.get(url).content.decode()
    assert "without a hold from the withdrawal" in html
    assert reverse("distribution:consent-hold", args=[dist.consent.pk]) in html
    consent = Consent.objects.get(pk=dist.consent.pk)
    response = client.post(
        reverse("distribution:consent-hold", args=[consent.pk]),
        {"reason": "Registered after the withdrawal", "expected_revision": consent.revision},
    )
    assert response.status_code == 302
    assert ConsentHold.objects.filter(specimen=late).exists()


def test_withdrawal_recorder_cannot_reinstate_but_another_manager_can(client, people, dist):
    as_user(client, people["manager"])
    withdraw(client, dist.consent)
    url = reverse("distribution:consent-reinstate", args=[dist.consent.pk])
    page = client.get(url)
    assert page.status_code == 200
    assert "You recorded the withdrawal" in page.content.decode()
    detail = client.get(reverse("distribution:consent-detail", args=[dist.consent.pk]))
    assert "You recorded the withdrawal" in detail.content.decode()
    consent = Consent.objects.get(pk=dist.consent.pk)
    data = {
        "occurred_at": when(),
        "reason": "Withdrawal recorded in error (synthetic)",
        "expected_revision": consent.revision,
    }
    assert client.post(url, data).status_code == 409
    assert Consent.objects.get(pk=dist.consent.pk).status == Consent.Status.WITHDRAWN
    as_user(client, people["manager2"])
    response = client.post(url, data)
    assert response.status_code == 302
    consent = Consent.objects.get(pk=dist.consent.pk)
    assert consent.status == Consent.Status.CONSENTED
    assert not ConsentHold.objects.filter(hold__release__isnull=True).exists()


def test_a_superuser_who_recorded_the_withdrawal_cannot_reinstate_it(client, people, dist):
    as_user(client, people["superuser"])
    assert withdraw(client, dist.consent).status_code == 302
    url = reverse("distribution:consent-reinstate", args=[dist.consent.pk])
    consent = Consent.objects.get(pk=dist.consent.pk)
    response = client.post(
        url,
        {"occurred_at": when(), "reason": "Self check", "expected_revision": consent.revision},
    )
    assert response.status_code == 409
    assert "You recorded the withdrawal" in response.content.decode()
    assert Consent.objects.get(pk=dist.consent.pk).status == Consent.Status.WITHDRAWN


def test_the_recorder_of_the_new_consent_cannot_reinstate(client, people, dist):
    as_user(client, people["manager"])
    withdraw(client, dist.consent)
    consent = Consent.objects.get(pk=dist.consent.pk)
    services.record_consent(
        actor=people["manager2"],
        participant_id=consent.participant_id,
        study_id=consent.study_id,
        form_id=dist.form.pk,
        decision="consented",
        answers={c.pk: "permitted" for c in dist.categories.values()},
        consented_on=timezone.localdate(),
        signer="participant",
        documentation="signed",
        source_reference="",
        restrictions="",
        expires_on=None,
        expected_revision=consent.revision,
        reason="Re-consent (synthetic)",
    )
    as_user(client, people["manager2"])
    page = client.get(reverse("distribution:consent-reinstate", args=[consent.pk]))
    assert "You recorded the new consent" in page.content.decode()


# --- consent forms through the pages --------------------------------------------------------


def test_record_consent_asks_the_chosen_forms_categories(client, people, dist):
    participant = catalogue(people["manager"], "participant", code="P-000125")
    as_user(client, people["manager"])
    url = reverse("distribution:consent-create") + f"?participant=P-000125&study={dist.study.pk}"
    page = client.get(url).content.decode()
    for code in ("GENERAL", "GENETIC", "COMMERCIAL", "RECONTACT"):
        assert f"{code} · " in page
    assert 'value="P-000125"' in page
    data = {
        "participant": "P-000125",
        "study": dist.study.pk,
        "form": dist.form.pk,
        "asked": dist.form.pk,
        "decision": "consented",
        "consented_on": timezone.localdate().isoformat(),
        "signer": "participant",
        "documentation": "signed",
        "reason": "Transcribed from the filed form (synthetic)",
    }
    missing = client.post(url, data)
    assert missing.status_code == 422
    assert "Record an answer for every category" in missing.content.decode()
    for category in dist.categories.values():
        data[f"answer_{category.pk}"] = "permitted"
    response = client.post(url, data)
    assert response.status_code == 302
    consent = Consent.objects.get(participant=participant, study=dist.study)
    assert consent.status == Consent.Status.CONSENTED
    assert consent.current_record.answers.count() == 4


def test_correct_record_keeps_the_prior_values_in_the_history(client, people, dist):
    as_user(client, people["manager"])
    consent = Consent.objects.get(pk=dist.consent.pk)
    record = consent.current_record
    url = reverse("distribution:consent-correct", args=[consent.pk])
    data = {
        "form": dist.form.pk,
        "asked": dist.form.pk,
        "decision": "consented",
        "consented_on": record.consented_on.isoformat(),
        "signer": "representative",
        "documentation": "signed",
        "source_reference": "Consent binder 2 (synthetic)",
        "reason": "Binder number mistyped",
        "expected_revision": consent.revision,
    }
    for answer in record.answers.all():
        data[f"answer_{answer.category_id}"] = answer.answer
    response = client.post(url, data)
    assert response.status_code == 302
    assert ConsentRecord.objects.filter(corrects=record).exists()
    html = client.get(reverse("distribution:consent-detail", args=[consent.pk])).content.decode()
    assert '<del class="vr-prior">Consent binder 1 (synthetic)</del>' in html
    assert "Record corrected" in html


# --- downloads and exports ------------------------------------------------------------------


def test_follow_up_and_agreement_attachments_download_as_files(client, people, dist, world):
    request = approved_request(people, dist, world)
    allocations = allocate(people, request, dist.specimens[:1])
    pick(people, request, allocations)
    shipment = packed_shipment(people, request, allocations)
    dispatch(people, shipment)
    as_user(client, people["manager"])
    assert withdraw(client, dist.consent).status_code == 302
    withdrawal = Consent.objects.get(pk=dist.consent.pk).withdrawals.get()
    page = client.get(
        reverse("distribution:consent-detail", args=[dist.consent.pk])
    ).content.decode()
    assert shipment.number in page and "Record follow-up" in page
    response = client.post(
        reverse("distribution:withdrawal-follow-up", args=[withdrawal.pk]),
        {
            "shipment": shipment.pk,
            "action": "recipient_notified",
            "note": "Recipient asked to destroy the material (synthetic)",
            "occurred_at": when(),
            "attachment": SimpleUploadedFile("letter.pdf", PDF, "application/pdf"),
        },
    )
    assert response.status_code == 302
    follow_up = WithdrawalFollowUp.objects.get()
    response = client.post(
        reverse("distribution:agreement-document", args=[dist.agreement.pk]),
        {
            "kind": "executed",
            "title": "Signed copy (synthetic)",
            "attachment": SimpleUploadedFile("mta.pdf", PDF, "application/pdf"),
        },
    )
    assert response.status_code == 302
    document = AgreementDocument.objects.get()
    as_user(client, people["viewer"])
    for kind, row in (("follow-up", follow_up), ("agreement-document", document)):
        download = client.get(reverse("distribution:attachment", args=[kind, row.pk]))
        assert download.status_code == 200
        assert download["Content-Disposition"].startswith("attachment")
        assert download["X-Content-Type-Options"] == "nosniff"
        assert download.content == PDF
    assert (
        client.get(reverse("distribution:attachment", args=["unknown", document.pk])).status_code
        == 404
    )
    client.logout()
    assert (
        client.get(
            reverse("distribution:attachment", args=["agreement-document", document.pk])
        ).status_code
        == 302
    )


def test_agreement_document_requires_a_file(client, people, dist):
    as_user(client, people["manager"])
    response = client.post(
        reverse("distribution:agreement-document", args=[dist.agreement.pk]),
        {"kind": "amendment", "title": "Amendment 1 (synthetic)"},
    )
    assert response.status_code == 422
    assert not AgreementDocument.objects.exists()


def test_consent_export_matches_the_filter_and_is_audited(client, people, dist):
    as_user(client, people["viewer"])
    response = client.get(
        reverse("distribution:catalogue-export", args=["consents"]), {"participant": "P-000123"}
    )
    assert response.status_code == 200
    assert response["Content-Disposition"].startswith("attachment")
    rows = list(csv.reader(io.StringIO(response.content.decode())))
    assert rows[0][:3] == ["Participant", "Study", "Status"]
    assert rows[1][:3] == ["P-000123", "DEMO-STUDY", "Consented"]
    record = OutputRecord.objects.get()
    assert record.kind == "export" and record.count == 1
    assert record.details["filters"] == {"participant": "P-000123"}
    bad = client.get(reverse("distribution:catalogue-export", args=["consents"]), {"x": "1"})
    assert bad.status_code == 400
    assert client.get(reverse("distribution:catalogue-export", args=["nope"])).status_code == 404


# --- agreements and catalogues --------------------------------------------------------------


def test_executed_agreement_terms_are_fixed_on_the_edit_form(client, people, dist):
    as_user(client, people["manager"])
    url = reverse("distribution:agreement-edit", args=[dist.agreement.pk])
    page = client.get(url).content.decode()
    assert "Fixed: the agreement is executed" in page
    dist.agreement.refresh_from_db()
    response = client.post(
        url,
        {
            "kind": "dua",  # ignored: disabled
            "title": "Synthetic MTA, renamed",
            "reporting_due_on": (timezone.localdate() + timedelta(days=30)).isoformat(),
            "reason": "Report date agreed (synthetic)",
            "expected_revision": dist.agreement.revision,
        },
    )
    assert response.status_code == 302
    dist.agreement.refresh_from_db()
    assert dist.agreement.title == "Synthetic MTA, renamed"
    assert dist.agreement.kind == "mta"


def test_category_in_use_renders_its_fixed_fields_disabled(client, people, dist):
    as_user(client, people["manager"])
    page = client.get(
        reverse("distribution:category-edit", args=[dist.categories["GENERAL"].pk])
    ).content.decode()
    assert "Fixed: recorded data already uses this entry." in page
    assert 'name="code"' in page and "disabled" in page


# --- dashboard ------------------------------------------------------------------------------


def test_dashboard_renders_with_empty_distribution_tables(client, people):
    for role in ("viewer", "technician", "manager", "decider_manager"):
        as_user(client, people[role])
        assert client.get(reverse("dashboard")).status_code == 200, role


def test_dashboard_queues_link_to_filtered_lists(client, people, dist, world):
    as_user(client, people["manager"])
    withdraw(client, dist.consent)
    register(people["technician"], world)  # in custody, without the withdrawal's hold
    services.save_agreement(
        actor=people["manager"],
        agreement_id=dist.agreement.pk,
        expected_revision=dist.agreement.revision,
        values={"reporting_due_on": timezone.localdate() + timedelta(days=10)},
        reason="Report due (synthetic)",
    )
    html = client.get(reverse("dashboard")).content.decode()
    withdrawals = reverse("distribution:withdrawal-list") + "?attention=1"
    agreements = reverse("distribution:agreement-list") + "?horizon=1"
    assert f'href="{withdrawals}"' in html
    assert f'href="{agreements}"' in html
    listed = client.get(withdrawals).content.decode()
    assert "P-000123 · DEMO-STUDY" in listed
    assert dist.agreement.number in client.get(agreements).content.decode()
    from .conftest import new_request

    submitted = new_request(people["technician"], dist, world)
    submitted.refresh_from_db()
    services.submit_request(
        actor=people["technician"], request_id=submitted.pk, expected_revision=submitted.revision
    )
    as_user(client, people["decider_manager"])
    html = client.get(reverse("dashboard")).content.decode()
    assert "Requests awaiting my decision" in html
    decide = reverse("distribution:request-list") + "?awaiting_me=on"
    assert f'href="{decide}"' in html
    # The link opens the requests the queue counts: this one, submitted by someone else.
    assert submitted.number in client.get(decide).content.decode()
    as_user(client, people["viewer"])
    viewer = client.get(reverse("dashboard")).content.decode()
    assert "Withdrawals needing follow-up" not in viewer
    assert "Requests awaiting my decision" not in viewer


def test_decision_queue_excludes_requests_the_user_created(people, dist, world):
    from distribution.dashboard import awaiting_decision

    from .conftest import new_request

    request = new_request(people["decider_manager"], dist, world)
    request.refresh_from_db()
    services.submit_request(
        actor=people["decider_manager"], request_id=request.pk, expected_revision=request.revision
    )
    assert not awaiting_decision(people["decider_manager"]).exists()
    assert awaiting_decision(people["decider"]).filter(pk=request.pk).exists()
