"""Record evidence only: these operations never change specimens or storage."""

from collections import Counter

from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils import timezone

from accounts.roles import require_role
from core.audit import record_event
from specimens.models import Specimen
from storage import services as storage
from storage.models import Box, MoveMember, Position

from .models import MAX_AUDIT_POSITIONS, AuditFinalization, InventoryAudit
from .scanner_import import parse_observations

READ_ROLES = storage.READ_ROLES
OPERATE_ROLES = storage.OPERATE_ROLES
TEXT_LIMIT = 4000
ACCESSION_LIMIT = 80


def text(value, field="reason", *, required=True):
    value = value.strip() if isinstance(value, str) else ""
    if required and not value:
        raise ValidationError({field: "Give the reason. It is kept in the history."})
    if len(value) > TEXT_LIMIT:
        raise ValidationError({field: f"Use at most {TEXT_LIMIT} characters."})
    return value


def audits(*, actor):
    require_role(actor, *READ_ROLES)
    return InventoryAudit.objects.select_related(
        "created_by", "box", "finalization__actor", "supersedes"
    )


def get_audit(*, actor, audit_id):
    return audits(actor=actor).get(pk=audit_id)


def available_boxes(*, actor):
    require_role(actor, *OPERATE_ROLES)
    return Box.objects.filter(is_active=True).order_by("code")


def _snapshot(box):
    positions = list(Position.objects.filter(box=box).order_by("ordinal"))
    if not 1 <= len(positions) <= MAX_AUDIT_POSITIONS:
        raise ValidationError({"box": f"Choose a box with 1 to {MAX_AUDIT_POSITIONS} positions."})
    expected = {
        position_id: (accession, str(specimen_id))
        for position_id, accession, specimen_id in Specimen.objects.filter(
            occupancy__position__box=box
        ).values_list("occupancy__position_id", "accession", "pk")
    }
    return [
        {
            "id": str(p.pk),
            "label": p.label,
            "ordinal": p.ordinal,
            "row": p.row,
            "column": p.column,
            "blocked": p.blocked,
            "block_reason": p.block_reason,
            "revision": p.revision,
            "expected": expected.get(p.pk, ("", ""))[0],
            "expected_specimen_id": expected.get(p.pk, ("", ""))[1],
        }
        for p in positions
    ]


@storage.operation()
def create_audit(*, actor, box_id, reason, supersedes_id=None):
    require_role(actor, *OPERATE_ROLES)
    reason = text(reason)
    storage.lock_gate()
    box = Box.objects.select_related("node__unit__room__site", "container_type").get(pk=box_id)
    if not box.is_active:
        raise ValidationError({"box": "Choose an active box."})
    chain = storage.node_chain(box.node)
    unit = box.node.unit
    if (
        not unit.is_active
        or not unit.room.is_active
        or not unit.room.site.is_active
        or any(not node.is_active for node in chain)
    ):
        raise ValidationError({"box": "The box's storage ancestry must be active."})
    supersedes = None
    if supersedes_id is not None:
        supersedes = get_audit(actor=actor, audit_id=supersedes_id)
        if supersedes.box_id != box.pk or supersedes.status == "open":
            raise ValidationError({"supersedes": "Choose a finalized audit of the same box."})
    positions = _snapshot(box)
    audit = InventoryAudit.objects.create(
        box=box,
        box_code=box.code,
        box_revision=box.revision,
        location=storage.build_path(unit=unit, nodes=chain, box=box, slot=box.slot),
        positions=positions,
        created_at=timezone.now(),
        created_by=actor,
        reason=reason,
        supersedes=supersedes,
    )
    record_event(
        actor=actor,
        action="quality.audit_created",
        obj=audit,
        after={"box": box.code, "positions": len(positions), "supersedes": supersedes_id},
        reason=reason,
    )
    return audit


def is_stale(*, actor, audit):
    """Call under StorageGate when completing. Ignore movements in unrelated boxes.

    Position revisions detect block/unblock, box revisions include moves of empty ancestors,
    and recorded movement members detect occupancy changes even after a move out and back.
    recorded_at is used because occurred_at can be backdated.
    """
    require_role(actor, *READ_ROLES)
    box = Box.objects.select_related("node__unit__room__site").get(pk=audit.box_id)
    unit = box.node.unit
    if (
        not unit.is_active
        or not unit.room.is_active
        or not unit.room.site.is_active
        or any(not node.is_active for node in storage.node_chain(box.node))
    ):
        return True
    if not box.is_active or box.revision != audit.box_revision:
        return True
    if storage.container_location(box) != audit.location:
        return True
    positions = list(Position.objects.filter(box=box).order_by("ordinal"))
    if [(str(p.pk), p.revision) for p in positions] != [
        (p["id"], p["revision"]) for p in audit.positions
    ]:
        return True
    return (
        MoveMember.objects.filter(event__recorded_at__gte=audit.created_at)
        .filter(Q(from_position__box_id=audit.box_id) | Q(to_position__box_id=audit.box_id))
        .exists()
    )


def _open_locked(audit_id):
    audit = InventoryAudit.objects.select_for_update().get(pk=audit_id)
    if AuditFinalization.objects.filter(audit=audit).exists():
        raise ValidationError(
            "This audit is already finalized. Start a new audit to add evidence.",
            code="invalid_transition",
        )
    return audit


@storage.operation()
def load_scanner_observations(*, actor, audit_id, upload):
    """Validate a preview against a current open snapshot; save no file or draft."""
    require_role(actor, *OPERATE_ROLES)
    storage.lock_gate()
    audit = _open_locked(audit_id)
    if is_stale(actor=actor, audit=audit):
        raise ValidationError(
            "Storage changed since this audit began. Cancel this audit and start a new one.",
            code="stale_snapshot",
        )
    return parse_observations(upload, audit=audit)


def _observations(audit, observations):
    if not isinstance(observations, dict) or set(observations) != {
        p["id"] for p in audit.positions
    }:
        raise ValidationError("Check every position exactly once before completing the audit.")
    rows, seen = [], set()
    for position in audit.positions:
        value = observations[position["id"]]
        if value is None:
            raise ValidationError("Check every position; unobserved is different from empty.")
        if not isinstance(value, str) or len(value.strip()) > ACCESSION_LIMIT:
            raise ValidationError(
                f"Observed accessions must be at most {ACCESSION_LIMIT} characters."
            )
        observed = value.strip()
        if observed:
            key = observed.casefold()
            if key in seen:
                raise ValidationError(
                    "An observed accession cannot appear in more than one position."
                )
            seen.add(key)
        expected = position["expected"]
        result = (
            "match"
            if expected == observed
            else "missing"
            if not observed
            else "unexpected"
            if not expected
            else "mismatch"
        )
        rows.append(
            {
                "position_id": position["id"],
                "label": position["label"],
                "observed": observed,
                "result": result,
            }
        )
    return rows


@storage.operation()
def complete_audit(*, actor, audit_id, observations, reason, discrepancy_explanation=""):
    require_role(actor, *OPERATE_ROLES)
    reason = text(reason)
    explanation = text(discrepancy_explanation, "discrepancy_explanation", required=False)
    storage.lock_gate()
    audit = _open_locked(audit_id)
    if is_stale(actor=actor, audit=audit):
        raise ValidationError(
            "Storage changed since this audit began. Cancel this audit and start a new one; "
            "the earlier snapshot remains in history.",
            code="stale_snapshot",
        )
    rows = _observations(audit, observations)
    counts = {key: 0 for key in ("match", "missing", "unexpected", "mismatch")}
    counts.update(Counter(row["result"] for row in rows))
    if counts["match"] != len(rows) and not explanation:
        raise ValidationError(
            {"discrepancy_explanation": "Explain the discrepancies before completing."}
        )
    final = AuditFinalization.objects.create(
        audit=audit,
        outcome="completed",
        actor=actor,
        reason=reason,
        observations={"positions": rows, "discrepancy_explanation": explanation},
        counts=counts,
    )
    record_event(
        actor=actor,
        action="quality.audit_completed",
        obj=audit,
        after={"finalization": final.pk, "counts": counts},
        reason=reason,
    )
    return audit


@storage.operation()
def cancel_audit(*, actor, audit_id, reason):
    require_role(actor, *OPERATE_ROLES)
    reason = text(reason)
    storage.lock_gate()
    audit = _open_locked(audit_id)
    final = AuditFinalization.objects.create(
        audit=audit, outcome="cancelled", actor=actor, reason=reason
    )
    record_event(
        actor=actor,
        action="quality.audit_cancelled",
        obj=audit,
        after={"finalization": final.pk},
        reason=reason,
    )
    return audit
