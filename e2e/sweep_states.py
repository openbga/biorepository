"""Record states the form sweep needs that the full demo does not contain (e2e database only).

The full demonstration shows most forms, but some exist only for records in a state the demo never
leaves them in: a draft request, a draft agreement or shipment, a dispatched shipment awaiting its
receipt, an excursion awaiting review, a withdrawal that asks for destruction, an announcement, an
account without a password or with an authenticator app. `prepare` makes those states through the
application's own services (so every check and audit event applies) before the sweep crawls, and
returns an opener page for each route, so every FORM_SURFACES route is exercised. Content is
obviously synthetic. Nothing here touches the demo seed.
"""

from datetime import timedelta
from decimal import Decimal

from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret
from django.contrib.auth.models import Group
from django.db.models import Exists, OuterRef
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from core.announcements import save_announcement
from core.models import Announcement
from distribution import seed as distribution_seed
from distribution import services as distribution_services
from distribution.models import Consent, SpecimenRequest
from equipment import services as equipment_services
from equipment.models import Equipment
from specimens import services as specimen_services
from specimens.models import Hold, QuantityEntry, Specimen
from specimens.queries import specimen_queryset

# Accounts the sweep signs in with besides the demo ones (all synthetic, example.invalid).
NO_PASSWORD = "sweep_nopassword"  # signed in through a session: it has no usable password
WITH_APP = "sweep_authenticator"  # has an authenticator app and recovery codes
WITHOUT_APP = "sweep_new_factor"  # signs in fresh before setting up an app (recent sign-in)


def _person(username, password, role="manager"):
    user = User.objects.create_user(
        username, f"{username}@example.invalid", password, first_name="E2E", last_name=username
    )
    user.groups.add(Group.objects.get(name=role))
    return user


def _accounts(password):
    nopass = _person(NO_PASSWORD, password)
    nopass.set_unusable_password()
    nopass.save()
    with_app = _person(WITH_APP, password)
    secret = generate_totp_secret()
    TOTP.activate(with_app, secret)
    RecoveryCodes.activate(with_app)
    _person(WITHOUT_APP, password)
    return secret


def _request_parts():
    """Investigator, specimen type, use categories and agreement of an approved demo request."""
    request = (
        SpecimenRequest.objects.filter(status="approved", agreement__isnull=False)
        .order_by("pk")
        .first()
    )
    return (
        request.investigator,
        request.items.order_by("line").first().specimen_type,
        list(request.use_categories.all()),
        request.agreement,
    )


def _distribution(manager, technician):
    investigator, specimen_type, categories, agreement = _request_parts()
    openers = {}
    # A draft request with one line: Edit, Edit lines and Submit for decision.
    draft = distribution_services.save_request(
        actor=technician,
        request_id=None,
        expected_revision=None,
        values={
            "title": "Draft request for the form sweep (synthetic)",
            "investigator": investigator.pk,
            "purpose": "Synthetic purpose for the form sweep.",
            "received_via": "email",
            "use_categories": [c.pk for c in categories],
            "studies": [],
        },
        items=(distribution_services.ItemSpec(line=1, specimen_type_id=specimen_type.pk, count=1),),
    )
    for route in ("request-edit", "request-items", "request-submit"):
        openers[f"distribution:{route}"] = reverse("distribution:request-detail", args=[draft.pk])
    # A request awaiting its decision (submitted by the technician): Decide.
    submitted = SpecimenRequest.objects.filter(status="submitted").order_by("pk").first()
    if submitted:
        openers["distribution:request-decide"] = reverse(
            "distribution:request-detail", args=[submitted.pk]
        )
    # A draft agreement: Execute agreement.
    draft_agreement = distribution_services.save_agreement(
        actor=manager,
        agreement_id=None,
        expected_revision=None,
        values={
            "kind": "mta",
            "title": "Draft agreement for the form sweep (synthetic)",
            "institution": investigator.institution_id,
            "data_level": "coded",
            "end_disposition": "destroy",
            "onward_transfer": "prohibited",
        },
        permitted_categories=(),
    )
    openers["distribution:agreement-execute"] = reverse(
        "distribution:agreement-detail", args=[draft_agreement.pk]
    )
    # A draft shipment holding picked specimens: Change contents and Pack.
    picked = distribution_seed._request(
        technician,
        investigator,
        specimen_type,
        "Sweep: draft shipment (synthetic)",
        categories,
        count=2,
    )
    picked = distribution_seed._approve(manager, picked, agreement)
    allocations = distribution_seed._allocate(manager, picked, 2)
    distribution_seed._pick(technician, picked, allocations)
    picked.refresh_from_db()
    shipment = distribution_services.create_shipment(
        actor=technician, request_id=picked.pk, expected_revision=picked.revision, values={}
    )
    distribution_services.set_shipment_contents(
        actor=technician,
        shipment_id=shipment.pk,
        expected_revision=shipment.revision,
        allocation_ids=tuple(a.pk for a in allocations),
    )
    for route in ("shipment-contents", "shipment-pack"):
        openers[f"distribution:{route}"] = reverse(
            "distribution:shipment-detail", args=[shipment.pk]
        )
    # A dispatched shipment awaiting its receipt: Record receipt.
    sent = distribution_seed._request(
        technician,
        investigator,
        specimen_type,
        "Sweep: dispatched shipment (synthetic)",
        categories,
        count=1,
    )
    sent = distribution_seed._approve(manager, sent, agreement)
    allocations = distribution_seed._allocate(manager, sent, 1)
    distribution_seed._pick(technician, sent, allocations)
    sent.refresh_from_db()
    packed = distribution_seed._pack(technician, manager, sent, allocations)
    distribution_services.dispatch_shipment(
        actor=technician,
        shipment_id=packed.pk,
        expected_revision=packed.revision,
        courier="Example Courier",
        tracking="EX-SWEEP-0001",
        handed_to="Courier driver (synthetic)",
        occurred_at=timezone.now(),
        reason="Collected by the courier (synthetic)",
    )
    openers["distribution:shipment-receipt"] = reverse(
        "distribution:shipment-detail", args=[packed.pk]
    )
    # A withdrawal asking for destruction of a participant whose material was distributed, and a
    # specimen registered after it: Hold withdrawn specimens, Request disposal of held
    # specimens, Record follow-up.
    distributed = Specimen.objects.filter(status="distributed", participant__isnull=False)
    consent = (
        Consent.objects.filter(
            status="consented", participant__in=distributed.values("participant")
        )
        .order_by("pk")
        .first()
    )
    distribution_services.withdraw_consent(
        actor=manager,
        consent_id=consent.pk,
        expected_revision=consent.revision,
        kind="destroy",
        effective_on=timezone.localdate(),
        received_via="Letter to the repository (synthetic)",
        occurred_at=timezone.now(),
        reason="Withdrawal for the form sweep (synthetic)",
    )
    model = distributed.filter(participant=consent.participant).first()
    specimen_services.register_specimen(
        actor=technician,
        data=specimen_services.Registration(
            specimen_type_id=model.specimen_type_id,
            container_type_id=model.container_type_id,
            quantity=Decimal("1.000000"),
            supplied_text="1.000000",
            unit_id=model.unit_id,
            collected_at=timezone.now() - timedelta(hours=2),
            participant_id=model.participant_id,
            collection_id=model.collection_id,
            baseline_thaws=0,
        ),
        occurred_at=timezone.now(),
        reason="Registered after the withdrawal (synthetic)",
    )
    for route in ("consent-hold", "consent-disposals", "withdrawal-follow-up"):
        openers[f"distribution:{route}"] = reverse("distribution:consent-detail", args=[consent.pk])
    return openers


def _equipment(technician):
    """An excursion submitted by the technician: Review excursion (by a manager)."""
    unit = Equipment.objects.filter(kind="freezer", status="in_service").order_by("code").first()
    now = timezone.now()
    excursion = equipment_services.open_excursion(
        actor=technician,
        equipment_id=unit.pk,
        started_at=now - timedelta(hours=5),
        ended_at=now - timedelta(hours=4),
        extreme_value=Decimal("-61.00"),
        extreme_at=now - timedelta(hours=4, minutes=30),
        detection="reading",
        cause_category="door",
        cause="Door left ajar (synthetic, form sweep).",
        action="Door closed; readings watched (synthetic).",
        impact="No specimen affected beyond the period (synthetic).",
    )
    equipment_services.submit_excursion(
        actor=technician, excursion_id=excursion.pk, expected_revision=excursion.revision
    )
    return {
        "equipment:excursion-review": reverse("equipment:excursion-detail", args=[excursion.pk])
    }


def _specimens():
    """Specimen states the full demo already has, past the crawl's records-per-shape limit."""
    openers = {}
    out = (
        Specimen.objects.filter(open_checkout__isnull=False, status="available")
        .exclude(
            Exists(
                Hold.objects.filter(
                    specimen=OuterRef("pk"), release__isnull=True, scope__in=("use", "all_use")
                )
            )
        )
        .first()
    )
    if out:
        for route in ("check-in", "consume", "derive"):
            openers[f"specimens:{route}"] = reverse("specimens:specimen-detail", args=[out.pk])
    unstored = specimen_queryset({"status": "available", "placed": "no", "checked_out": "no"})
    if unstored.exists():
        openers["specimens:place"] = reverse(
            "specimens:specimen-detail", args=[unstored.first().pk]
        )
    hold = (
        Hold.objects.filter(release__isnull=True, reference="")
        .exclude(specimen__status="disposed")
        .first()
    )
    if hold:
        openers["specimens:hold-release"] = reverse(
            "specimens:specimen-detail", args=[hold.specimen_id]
        )
    entry = (
        QuantityEntry.objects.filter(kind__in=("consume", "adjust"), reversed_by__isnull=True)
        .exclude(specimen__status__in=("disposed", "distributed"))
        .first()
    )
    if entry:
        openers["specimens:reverse"] = reverse(
            "specimens:specimen-detail", args=[entry.specimen_id]
        )
    return openers


def prepare(password):
    """Make the missing states; return ({route: (account, opener)}, the authenticator secret)."""
    manager = User.objects.get(username="demo_manager")
    technician = User.objects.get(username="demo_technician")
    admin = User.objects.get(username="demo_admin")
    secret = _accounts(password)
    save_announcement(
        actor=admin,
        instance=Announcement(
            title="Synthetic sweep notice",
            body="An announcement for the form sweep (synthetic).",
            show_to_staff=True,
        ),
    )
    openers = {
        route: ("demo_manager", path) for route, path in _distribution(manager, technician).items()
    }
    openers.update({r: ("demo_manager", p) for r, p in _equipment(technician).items()})
    openers.update({r: ("demo_manager", p) for r, p in _specimens().items()})
    openers["announcement-edit"] = ("demo_admin", reverse("announcement-list"))
    openers["account_set_password"] = (NO_PASSWORD, reverse("account_password"))
    openers["mfa_activate_totp"] = (WITHOUT_APP, reverse("mfa_index"))
    for route in ("mfa_deactivate_totp", "mfa_generate_recovery_codes"):
        openers[route] = (WITH_APP, reverse("mfa_index"))
    return openers, secret
