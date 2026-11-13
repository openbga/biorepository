"""Agreements: draft, execute, fixed terms, terminate, documents and validity dates. Synthetic."""

from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from distribution import services
from distribution.models import Agreement, AgreementDocument, AgreementEvent

from .helpers import (
    ConsentScopeCategory,
    csv_file,
    fast_password_hashing,  # noqa: F401
    make_agreement,
    refused,
)

pytestmark = pytest.mark.django_db

TODAY = timezone.localdate


def draft(people, dist, **values):
    return services.save_agreement(
        actor=people["manager"],
        agreement_id=None,
        expected_revision=None,
        values={
            "kind": "mta",
            "title": "Synthetic MTA",
            "institution": dist.institution.pk,
            "data_level": "coded",
            "end_disposition": "destroy",
            **values,
        },
        permitted_categories=(),
    )


def execute(people, agreement, **overrides):
    agreement.refresh_from_db()
    values = {
        "executed_on": TODAY() - timedelta(days=3),
        "effective_on": TODAY() - timedelta(days=3),
        "expires_on": TODAY() + timedelta(days=100),
        "document_reference": "Agreements cabinet, folder 3 (synthetic)",
    }
    values.update(overrides)
    return services.execute_agreement(
        actor=people["manager"],
        agreement_id=agreement.pk,
        expected_revision=agreement.revision,
        reason="Signed by both parties",
        **values,
    )


def edit(people, agreement, values, categories=None, reason="Corrected after review"):
    agreement.refresh_from_db()
    return services.save_agreement(
        actor=people["manager"],
        agreement_id=agreement.pk,
        expected_revision=agreement.revision,
        values=values,
        permitted_categories=categories,
        reason=reason,
    )


def test_a_new_agreement_is_a_draft_with_a_number_and_a_created_event(people, dist, world):
    agreement = draft(people, dist)
    assert agreement.status == "draft" and agreement.number.startswith("AG-")
    assert agreement.revision == 1 and agreement.executed_on is None
    assert list(agreement.events.values_list("kind", flat=True)) == ["created"]


def test_a_draft_can_change_every_term(people, dist, world):
    agreement = draft(people, dist)
    edited = edit(
        people,
        agreement,
        {"data_level": "limited", "title": "Revised title", "onward_transfer": "prohibited"},
        categories=(ConsentScopeCategory.objects.get(code="GENERAL").pk,),
    )
    assert edited.data_level == "limited" and edited.permitted_categories.count() == 1
    assert AgreementEvent.objects.filter(agreement=agreement, kind="edited").get().changes


def test_a_draft_edit_needs_a_reason(people, dist, world):
    agreement = draft(people, dist)
    refused(lambda: edit(people, agreement, {"title": "Another"}, reason=" "), "required")


def test_the_investigator_must_belong_to_the_institution(people, dist, world):
    other = services.save_distribution_catalogue(
        actor=people["manager"],
        kind="institution",
        object_id=None,
        expected_revision=None,
        values={"code": "OTH", "name": "Other Institute"},
    )
    stranger = services.save_distribution_catalogue(
        actor=people["manager"],
        kind="investigator",
        object_id=None,
        expected_revision=None,
        values={"code": "INV-002", "name": "Another Investigator", "institution": other.pk},
    )
    refused(lambda: draft(people, dist, investigator=stranger.pk), "invalid")


# ------------------------------------------------------------------ execute


def test_execute_needs_a_filing_reference_or_an_executed_document(people, dist, world):
    agreement = draft(people, dist)
    refused(lambda: execute(people, agreement, document_reference="  "), "required")
    agreement.refresh_from_db()
    assert agreement.status == "draft"
    services.add_agreement_document(
        actor=people["manager"],
        agreement_id=agreement.pk,
        kind="executed",
        title="Signed copy (synthetic)",
        note="",
        upload=csv_file("signed,yes\n", "signed.csv"),
    )
    executed = execute(people, agreement, document_reference="")
    assert executed.status == "executed" and executed.document_reference == ""


def test_a_draft_document_does_not_count_as_the_executed_copy(people, dist, world):
    agreement = draft(people, dist)
    services.add_agreement_document(
        actor=people["manager"],
        agreement_id=agreement.pk,
        kind="draft",
        title="Draft copy",
        note="",
        upload=csv_file(),
    )
    refused(lambda: execute(people, agreement, document_reference=""), "required")


def test_execute_records_the_dates_and_an_event(people, dist, world):
    agreement = execute(people, draft(people, dist))
    assert agreement.status == "executed"
    assert agreement.executed_on == TODAY() - timedelta(days=3)
    assert agreement.effective_on == TODAY() - timedelta(days=3)
    assert agreement.expires_on == TODAY() + timedelta(days=100)
    assert AgreementEvent.objects.filter(agreement=agreement, kind="executed").exists()
    refused(lambda: execute(people, agreement), "invalid_transition")  # only a draft


def test_execute_refuses_inconsistent_dates(people, dist, world):
    agreement = draft(people, dist)
    refused(lambda: execute(people, agreement, expires_on=TODAY() - timedelta(days=30)), "invalid")
    refused(
        lambda: execute(people, agreement, executed_on=TODAY() + timedelta(days=1)),
        "invalid_time",
    )
    refused(lambda: execute(people, agreement, effective_on=None), "required")
    agreement.refresh_from_db()
    assert agreement.status == "draft"


# ------------------------------------------------------------------ executed terms are fixed


def test_executed_terms_cannot_change_but_title_and_expiry_can_with_a_reason(people, dist, world):
    agreement = execute(people, draft(people, dist))
    refused(lambda: edit(people, agreement, {"data_level": "limited"}), "invalid")
    refused(lambda: edit(people, agreement, {"end_disposition": "retain"}), "invalid")
    refused(lambda: edit(people, agreement, {"kind": "dua"}), "invalid")
    refused(
        lambda: edit(
            people,
            agreement,
            {},
            categories=(ConsentScopeCategory.objects.get(code="GENERAL").pk,),
        ),
        "invalid",
    )
    agreement.refresh_from_db()
    assert agreement.data_level == "coded" and agreement.permitted_categories.count() == 0
    new_expiry = TODAY() + timedelta(days=200)
    edited = edit(people, agreement, {"title": "Renamed MTA", "expires_on": new_expiry})
    assert edited.title == "Renamed MTA" and edited.expires_on == new_expiry
    event = AgreementEvent.objects.filter(agreement=agreement, kind="edited").get()
    assert event.reason == "Corrected after review" and set(event.changes) == {
        "title",
        "expires_on",
    }
    refused(lambda: edit(people, agreement, {"title": "Again"}, reason=""), "required")


def test_resubmitting_unchanged_categories_on_an_executed_agreement_is_fine(people, dist, world):
    general = ConsentScopeCategory.objects.get(code="GENERAL")
    agreement = make_agreement(people, dist, categories=("GENERAL",))
    edited = edit(people, agreement, {"title": "Renamed"}, categories=(general.pk,))
    assert edited.title == "Renamed"


def test_the_expiry_cannot_precede_the_effective_date(people, dist, world):
    agreement = execute(people, draft(people, dist))
    refused(
        lambda: edit(people, agreement, {"expires_on": agreement.effective_on - timedelta(days=1)}),
        "invalid",
    )


# ------------------------------------------------------------------ terminate


def test_terminate_records_the_date_and_freezes_the_agreement(people, dist, world):
    agreement = execute(people, draft(people, dist))
    agreement.refresh_from_db()
    ended = services.terminate_agreement(
        actor=people["manager"],
        agreement_id=agreement.pk,
        expected_revision=agreement.revision,
        terminated_on=TODAY(),
        reason="Terminated by the institution",
    )
    assert ended.status == "terminated" and ended.terminated_on == TODAY()
    assert AgreementEvent.objects.filter(agreement=agreement, kind="terminated").exists()
    refused(lambda: edit(people, ended, {"title": "Late edit"}), "invalid_transition")
    refused(
        lambda: services.terminate_agreement(
            actor=people["manager"],
            agreement_id=agreement.pk,
            expected_revision=ended.revision,
            terminated_on=TODAY(),
            reason="Again",
        ),
        "invalid_transition",
    )


def test_only_an_executed_agreement_can_be_terminated_and_not_before_it_began(people, dist, world):
    agreement = draft(people, dist)
    refused(
        lambda: services.terminate_agreement(
            actor=people["manager"],
            agreement_id=agreement.pk,
            expected_revision=agreement.revision,
            terminated_on=TODAY(),
            reason="Never signed",
        ),
        "invalid_transition",
    )
    executed = execute(people, agreement)
    executed.refresh_from_db()
    refused(
        lambda: services.terminate_agreement(
            actor=people["manager"],
            agreement_id=agreement.pk,
            expected_revision=executed.revision,
            terminated_on=executed.effective_on - timedelta(days=1),
            reason="Before it began",
        ),
        "invalid",
    )


# ------------------------------------------------------------------ documents


def test_documents_need_a_file_and_may_supersede_an_earlier_one(people, dist, world):
    agreement = draft(people, dist)
    refused(
        lambda: services.add_agreement_document(
            actor=people["manager"],
            agreement_id=agreement.pk,
            kind="amendment",
            title="Amendment 1",
            note="",
            upload=None,
        ),
        "required",
    )
    first = services.add_agreement_document(
        actor=people["manager"],
        agreement_id=agreement.pk,
        kind="executed",
        title="Signed copy",
        note="",
        upload=csv_file(),
    )
    second = services.add_agreement_document(
        actor=people["manager"],
        agreement_id=agreement.pk,
        kind="amendment",
        title="Amendment 1",
        note="Extends the term",
        upload=csv_file("amended,yes\n"),
        supersedes_id=first.pk,
    )
    assert second.supersedes_id == first.pk and second.attachment_id != first.attachment_id
    assert AgreementDocument.objects.filter(agreement=agreement).count() == 2
    assert AgreementEvent.objects.filter(agreement=agreement, kind="document").count() == 2
    other = draft(people, dist)
    refused(
        lambda: services.add_agreement_document(
            actor=people["manager"],
            agreement_id=other.pk,
            kind="amendment",
            title="Not mine",
            note="",
            upload=csv_file(),
            supersedes_id=first.pk,
        ),
        "invalid",
    )


def test_a_document_with_an_unsupported_type_is_refused(people, dist, world):
    agreement = draft(people, dist)
    with pytest.raises((ValidationError, ValueError)):
        services.add_agreement_document(
            actor=people["manager"],
            agreement_id=agreement.pk,
            kind="other",
            title="Program",
            note="",
            upload=csv_file("x", "program.exe"),
        )
    assert not AgreementDocument.objects.exists()


# ------------------------------------------------------------------ is_valid_on


def test_is_valid_on_boundaries(people, dist, world):
    today = TODAY()
    effective, expires = today - timedelta(days=10), today + timedelta(days=10)
    agreement = Agreement(
        status="executed", effective_on=effective, expires_on=expires, terminated_on=None
    )
    assert not agreement.is_valid_on(effective - timedelta(days=1))
    assert agreement.is_valid_on(effective)  # the effective day counts
    assert agreement.is_valid_on(expires)  # the expiry day is inclusive
    assert not agreement.is_valid_on(expires + timedelta(days=1))
    open_ended = Agreement(status="executed", effective_on=effective, expires_on=None)
    assert open_ended.is_valid_on(today + timedelta(days=5000))
    terminated_on = today
    ended = Agreement(
        status="terminated",
        effective_on=effective,
        expires_on=expires,
        terminated_on=terminated_on,
    )
    assert ended.is_valid_on(terminated_on - timedelta(days=1))
    assert not ended.is_valid_on(terminated_on)  # the termination day is excluded
    assert not ended.is_valid_on(terminated_on + timedelta(days=1))
    assert not Agreement(status="draft", effective_on=effective).is_valid_on(today)
    assert not Agreement(status="executed", effective_on=None).is_valid_on(today)


def test_an_executed_agreement_in_the_database_follows_the_same_rule(people, dist, world):
    agreement = make_agreement(people, dist, effective_days=0, expires_days=0)
    assert agreement.is_valid_on(TODAY())
    assert not agreement.is_valid_on(TODAY() + timedelta(days=1))
    assert not agreement.is_valid_on(TODAY() - timedelta(days=1))
