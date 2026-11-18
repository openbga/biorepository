"""Synthetic quality records for `seed_demo --full`: inventory audits, deviations and training.

Everything is made through the quality services, so every record carries its audit events and the
same checks as work done in the application. Content is obviously synthetic. Idempotent: the
seed is recognised by its first deviation's title and adds nothing when that exists.
"""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from storage.models import Box

from . import deviation_services, services, training_services
from .models import Deviation

MARKER = "Synthetic freezer door left ajar (demonstration)"


def _audit_box(*, actor, exclude=()):
    """An active box with specimens, for an audit that compares something real."""
    candidates = [box for box in services.available_boxes(actor=actor) if box.pk not in exclude]
    for box in candidates:
        if Box.objects.filter(pk=box.pk, positions__occupancy__isnull=False).exists():
            return box
    return candidates[0] if candidates else None


def _observations(audit, *, drop_first=False):
    """What a technician saw: every position as expected, or the first expected one empty."""
    observed = {position["id"]: position["expected"] for position in audit.positions}
    if drop_first:
        first = next((p for p in audit.positions if p["expected"]), None)
        if first:
            observed[first["id"]] = ""
    return observed


def _audits(*, manager, technician):
    """Completed (all match), completed with a discrepancy, cancelled, and one open audit."""
    used = []
    box = _audit_box(actor=technician)
    if box is None:
        return {}
    used.append(box.pk)
    matched = services.create_audit(
        actor=technician, box_id=box.pk, reason="Quarterly spot check (synthetic demonstration)"
    )
    services.complete_audit(
        actor=technician,
        audit_id=matched.pk,
        observations=_observations(matched),
        reason="Every position checked against the snapshot (synthetic)",
    )
    second = _audit_box(actor=technician, exclude=used) or box
    used.append(second.pk)
    discrepancy = services.create_audit(
        actor=manager, box_id=second.pk, reason="Check after a reported mix-up (synthetic)"
    )
    services.complete_audit(
        actor=manager,
        audit_id=discrepancy.pk,
        observations=_observations(discrepancy, drop_first=True),
        reason="Physical check finished (synthetic)",
        discrepancy_explanation="One tube was not found at its position (synthetic example).",
    )
    third = _audit_box(actor=technician, exclude=used) or box
    used.append(third.pk)
    cancelled = services.create_audit(
        actor=technician, box_id=third.pk, reason="Started before a box move (synthetic)"
    )
    services.cancel_audit(
        actor=technician,
        audit_id=cancelled.pk,
        reason="The box was moved during the check; a new audit follows (synthetic)",
    )
    fourth = _audit_box(actor=technician, exclude=used) or box
    services.create_audit(
        actor=technician, box_id=fourth.pk, reason="Monthly check in progress (synthetic)"
    )
    return {"discrepancy": discrepancy}


def _deviations(*, manager, technician, audits):
    """Open, awaiting review, closed, and closed after a return for revision."""
    now = timezone.now()
    complete = {
        "investigation": "Door alarm log reviewed; the latch did not engage (synthetic).",
        "corrective_action": "Latch replaced and door check added to the shift list (synthetic).",
        "effectiveness": "No recurrence over the following two weeks (synthetic).",
    }
    closed = deviation_services.create_deviation(
        actor=technician,
        values={
            "title": MARKER,
            "occurred_at": now - timedelta(days=21, hours=3),
            "description": "Freezer door found ajar at the start of the shift (synthetic).",
            "immediate_action": "Door closed; temperature log checked (synthetic).",
            **complete,
        },
        reason="Reported at the start of the shift (synthetic)",
    )
    closed = deviation_services.submit_deviation(
        actor=technician,
        deviation_id=closed.pk,
        expected_revision=closed.revision,
        reason="Investigation complete (synthetic)",
    )
    deviation_services.review_deviation(
        actor=manager,
        deviation_id=closed.pk,
        expected_revision=closed.revision,
        decision="close",
        reason="Evidence reviewed; actions are effective (synthetic)",
    )

    review = deviation_services.create_deviation(
        actor=technician,
        values={
            "title": "Synthetic label printed with the wrong stock (demonstration)",
            "occurred_at": now - timedelta(days=6, hours=2),
            "description": "Labels came out on the smaller stock and were reprinted (synthetic).",
            "immediate_action": "Affected labels reprinted on the right stock (synthetic).",
            "investigation": "The printer tray had the wrong roll loaded (synthetic).",
            "corrective_action": "Tray labelled with the stock it takes (synthetic).",
            "effectiveness": "To be confirmed at the next print run (synthetic).",
        },
        reason="Reported after reprinting (synthetic)",
    )
    deviation_services.submit_deviation(
        actor=technician,
        deviation_id=review.pk,
        expected_revision=review.revision,
        reason="Ready for review (synthetic)",
    )

    deviation_services.create_deviation(
        actor=technician,
        values={
            "title": "Synthetic tube missing at audit (demonstration)",
            "occurred_at": now - timedelta(days=1, hours=5),
            "description": "An inventory audit found one position empty (synthetic).",
            "inventory_audit_id": audits["discrepancy"].pk if audits.get("discrepancy") else None,
        },
        reason="Raised from the audit discrepancy (synthetic)",
    )

    returned = deviation_services.create_deviation(
        actor=technician,
        values={
            "title": "Synthetic late check-in after processing (demonstration)",
            "occurred_at": now - timedelta(days=40),
            "description": "A specimen was checked in later than planned (synthetic).",
            "immediate_action": "Time out of storage reviewed (synthetic).",
            "investigation": "Bench queue longer than usual (synthetic).",
            "corrective_action": "None needed yet (synthetic).",
            "effectiveness": "Not applicable (synthetic).",
        },
        reason="Reported by the bench (synthetic)",
    )
    returned = deviation_services.submit_deviation(
        actor=technician,
        deviation_id=returned.pk,
        expected_revision=returned.revision,
        reason="Submitted (synthetic)",
    )
    returned = deviation_services.review_deviation(
        actor=manager,
        deviation_id=returned.pk,
        expected_revision=returned.revision,
        decision="return",
        reason="Explain why no corrective action is needed (synthetic)",
    )
    returned = deviation_services.update_deviation(
        actor=technician,
        deviation_id=returned.pk,
        expected_revision=returned.revision,
        values={
            "corrective_action": "Queue reviewed weekly; no change to the procedure (synthetic)."
        },
        reason="Corrective action explained (synthetic)",
    )
    returned = deviation_services.submit_deviation(
        actor=technician,
        deviation_id=returned.pk,
        expected_revision=returned.revision,
        reason="Resubmitted (synthetic)",
    )
    deviation_services.review_deviation(
        actor=manager,
        deviation_id=returned.pk,
        expected_revision=returned.revision,
        decision="close",
        reason="Explanation accepted (synthetic)",
    )


def _training(*, manager, technician, admin):
    """Assigned (one overdue), completed (one with renewal due soon), corrected and cancelled."""
    today = timezone.localdate()

    def assign(trainee, topic, due, reference=""):
        return training_services.create_training(
            actor=manager,
            trainee_id=trainee.pk,
            values={"topic": topic, "procedure_reference": reference, "due_on": due},
            reason="Assigned for the demonstration (synthetic)",
        )

    def complete(record, days_ago, renewal=None, assessment=""):
        return training_services.complete_training(
            actor=manager,
            training_id=record.pk,
            expected_revision=record.revision,
            values={
                "completed_on": today - timedelta(days=days_ago),
                "trainer_provider": "Example Training Co. (synthetic)",
                "completion_evidence": "Attendance sheet filed in the training binder (synthetic).",
                "competency_assessment": assessment,
                "renewal_due": renewal,
            },
            reason="Completion recorded (synthetic)",
        )

    assign(technician, "LN2 handling refresher", today + timedelta(days=14), "SOP-LN2-01 v3")
    assign(technician, "Shipping on dry ice", today - timedelta(days=5), "SOP-SHIP-02 v1")
    done = assign(technician, "Specimen check-out and check-in", today - timedelta(days=60))
    complete(done, 45, renewal=today + timedelta(days=320), assessment="Observed two check-ins.")
    soon = assign(admin, "Freezer alarm response", today - timedelta(days=380), "SOP-ALM-01 v2")
    soon = complete(soon, 360, renewal=today + timedelta(days=20))
    corrected = assign(manager, "Disposal approval duties", today - timedelta(days=90))
    corrected = complete(corrected, 80, renewal=today + timedelta(days=285))
    training_services.correct_training(
        actor=manager,
        training_id=corrected.pk,
        expected_revision=corrected.revision,
        values={"completed_on": today - timedelta(days=82)},
        reason="Completion date transcribed wrongly (synthetic)",
    )
    cancelled = assign(technician, "Duplicate assignment (synthetic)", today + timedelta(days=30))
    training_services.cancel_training(
        actor=manager,
        training_id=cancelled.pk,
        expected_revision=cancelled.revision,
        reason="Assigned twice by mistake (synthetic)",
    )


def seed_full_quality(*, manager, technician, admin):
    """Add the quality records of the full demonstration, once. Returns True when it added them."""
    if Deviation.objects.filter(title=MARKER).exists():
        return False
    with transaction.atomic():
        audits = _audits(manager=manager, technician=technician)
        _deviations(manager=manager, technician=technician, audits=audits)
        _training(manager=manager, technician=technician, admin=admin)
    return True
