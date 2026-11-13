"""The synthetic working repository behind ``seed_demo --full``.

Everything here is invented demonstration data: about 350 specimens from 70 coded participants in
three collections, spread over tanks, freezers and a refrigerator, with the work a repository
does on them: aliquot families, derivatives, check-outs that were returned and ones still out
(some for weeks), thaws, holds, disposal requests in every state, bulk imports, labels and
reasoned corrections. All of it is made through the specimen and storage services the
application itself uses, so every record carries its checks, custody events, audit events and
history. Nothing is a real participant, result, place or policy.

Idempotent: the work is created once, recognised by the specimens of collection COH-A, and
skipped afterwards. The caller runs it inside one transaction, so a failure leaves nothing
behind. Times are relative to the moment of seeding, so a daily reset always shows a repository
whose newest registrations are hours old and whose oldest check-out is about a month old.
"""

import random
from collections import deque
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management.base import CommandError
from django.utils import timezone

from accounts.models import User
from specimens import importing, services
from specimens.models import Collection, CustodyEvent, Specimen, ThawEvent
from specimens.seed import COHORT_A, COHORT_B, PILOT, SHIPMENT, seed_full_catalogues
from specimens.services import ChildSpec, CorrectionValues, Registration
from storage import services as storage
from storage.models import Box, LayoutNode, Position, StorageGate
from storage.seed import seed_full_storage

MARKER = "COH-A"  # the collection whose specimens mark the full demonstration as seeded
SEED = 20261112
ML = "mL"
VIAL, CRYOTUBE, MICRO, URINE_TUBE = (
    "CRYOVIAL-2ML",
    "CRYOTUBE-5ML",
    "MICROTUBE-0.5ML",
    "URINE-TUBE-10ML",
)

# Specimens stored one by one: box, type, container, quantity (mL), how many, collection, whether
# the participant's thaw history before registration is known (the pilot's is not).
SINGLES = (
    ("BOX-006", "SERUM", VIAL, "1.500000", 24, "COH-A", True),
    ("BOX-007", "SERUM", VIAL, "1.200000", 20, "COH-B", True),
    ("MF02-B3", "BUFFY-COAT", MICRO, "0.400000", 12, "COH-B", True),
    ("MF02-B1", "URINE", URINE_TUBE, "8.000000", 14, "COH-B", True),
    ("MF01-B3", "URINE", URINE_TUBE, "6.500000", 16, "COH-A", True),
    ("VP-02-BOX-001", "PBMC", VIAL, "1.000000", 10, "COH-A", True),
    ("VP-02-BOX-002", "SERUM", VIAL, "1.000000", 18, "COH-B", True),
    ("BOX-009", "SERUM", VIAL, "1.000000", 8, "PILOT", False),
    ("BOX-009", "PLASMA", VIAL, "1.000000", 8, "PILOT", False),
    ("BOX-009", "BUFFY-COAT", MICRO, "0.400000", 8, "PILOT", False),
    ("BOX-009", "PBMC", VIAL, "1.000000", 1, "PILOT", False),
    ("VP-03-L01", "SERUM", VIAL, "1.000000", 16, "COH-A", True),
    ("VP-03-L03", "PLASMA", VIAL, "1.000000", 10, "COH-B", True),
    ("MF01-B1", "SERUM", VIAL, "0.900000", 20, "COH-A", True),
    ("MF01-B2", "SERUM", VIAL, "0.900000", 6, "COH-B", True),
    ("MF02-B2", "PLASMA", VIAL, "0.500000", 25, None, True),  # pooled reference, no participant
    ("RF01-B1", "URINE", URINE_TUBE, "7.000000", 9, "COH-B", True),
)
# Boxes whose container later moves; no single-specimen work is done in them afterwards.
MOVED = {"BOX-009", "VP-03-L03", "MF02-B3"}
COHORTS = {"COH-A": COHORT_A, "COH-B": COHORT_B, "PILOT": PILOT}
# first and last draw, in days before the seed, per collection
DRAW_DAYS = {"COH-A": (42, 26), "COH-B": (30, 5), "PILOT": (30, 22)}
FAMILIES = (("COH-A", 12, "BOX-004"), ("COH-B", 8, "BOX-005"))

HOLD_REASONS = (
    ("disposal", "Reserved for a method comparison (synthetic demo)"),
    ("all_use", "Possible labeling discrepancy under review (synthetic demo)"),
    ("all_use", "Container integrity query from the collection site (synthetic demo)"),
)
DESTINATIONS = (
    "Assay bench 2",
    "Assay bench 3",
    "Sequencing preparation",
    "Quality review",
    "Method development lab",
    "Courier pickup, partner laboratory",
)
WASTE = ("Autoclave, then clinical waste", "Biohazard waste stream")


def _fail(exc):
    messages = exc.messages if isinstance(exc, ValidationError) else [str(exc)]
    return CommandError("Full demo seeding stopped; nothing was saved: " + " ".join(messages))


def seed_full_demo():
    """Add the synthetic working repository after the basic demo seed; True if it was added."""
    marker = Collection.objects.filter(code=MARKER).first()
    if marker and Specimen.objects.filter(collection=marker).exists():
        return False
    try:
        Story().run()
    except (ValidationError, PermissionDenied) as exc:
        raise _fail(exc) from exc
    return True


class Story:
    def __init__(self):
        users = {u.username: u for u in User.objects.filter(username__startswith="demo_")}
        try:
            self.mgr = users["demo_manager"]
            self.tech = users["demo_technician"]
            self.admin = users["demo_admin"]
        except KeyError as exc:
            raise CommandError(f"Demo account {exc} is missing.") from exc
        self.now = timezone.now()
        self.rng = random.Random(SEED)
        self.last = {}  # specimen pk -> time of its latest event, to keep new events in order
        self.where = {}  # specimen pk -> the Position it was last stored in
        self.free = {}  # box code -> deque of positions nobody has used yet
        self.singles = []  # specimens stored one by one that no scenario has used yet
        self.box_of = {}  # specimen pk -> code of the box it is stored in
        self.draw = {}  # participant code -> collection time

    # ------------------------------------------------------------------ time and helpers

    def ago(self, *, days=0, hours=0, minutes=0):
        return self.now - timedelta(days=days, hours=hours, minutes=minutes)

    def fresh(self, specimen):
        return Specimen.objects.select_related("occupancy").get(
            pk=getattr(specimen, "pk", specimen)
        )

    def position(self, box):
        if box not in self.free:
            self.free[box] = deque(
                Position.objects.filter(
                    box__code=box, blocked=False, occupancy__isnull=True, box__is_active=True
                ).order_by("ordinal")
            )
        return self.free[box].popleft()

    def schedule_draws(self):
        zone = timezone.get_current_timezone()
        for collection, (first, last) in DRAW_DAYS.items():
            codes = COHORTS[collection]
            for i, code in enumerate(codes):
                days = first - (first - last) * i / max(len(codes) - 1, 1)
                day = (self.now - timedelta(days=days)).astimezone(zone).date()
                minute = 8 * 60 + self.rng.randrange(0, 180)
                self.draw[code] = datetime.combine(day, time(minute // 60, minute % 60), zone)

    # ------------------------------------------------------------------ service wrappers

    def register(
        self, kind, container, quantity, participant, collection, box, collected, *, known=True
    ):
        """Register one primary specimen (and store it in `box`); returns it."""
        position = self.position(box) if box else None
        data = Registration(
            specimen_type_id=self.types[kind].pk,
            container_type_id=self.containers[container].pk,
            quantity=Decimal(quantity),
            supplied_text=quantity,
            unit_id=self.units[ML].pk,
            collected_at=collected,
            processing_started_at=collected + timedelta(minutes=25),
            processing_completed_at=collected + timedelta(minutes=80),
            participant_id=self.participants[participant].pk if participant else None,
            collection_id=self.collections[collection].pk if collection else None,
            baseline_thaws=0 if known else None,
            position_id=position.pk if position else None,
        )
        when = collected + timedelta(hours=2, minutes=self.rng.randrange(0, 45))
        specimen = services.register_specimen(
            actor=self.tech,
            data=data,
            occurred_at=when,
            reason="Received and processed (synthetic demo)",
        )
        self.last[specimen.pk] = when
        if position:
            self.where[specimen.pk] = position
            self.box_of[specimen.pk] = box
        return specimen

    def check_out(
        self, specimen, when, destination, *, to=None, reason="Taken for work (synthetic demo)"
    ):
        s = self.fresh(specimen)
        services.check_out(
            actor=self.tech,
            specimen_id=s.pk,
            expected_revision=s.revision,
            expected_occupancy_revision=s.occupancy.revision,
            recipient_id=(to or self.tech).pk,
            destination=destination,
            occurred_at=when,
            reason=reason,
        )
        self.last[s.pk] = when

    def check_in(
        self, specimen, when, thaw, *, to=None, reason="Returned to storage (synthetic demo)"
    ):
        s = self.fresh(specimen)
        services.check_in(
            actor=self.tech,
            specimen_id=s.pk,
            expected_revision=s.revision,
            expected_occupancy_revision=s.occupancy.revision,
            position_id=self.where[s.pk].pk,
            recipient_id=(to or self.tech).pk,
            thaw_observed=thaw,
            occurred_at=when,
            reason=reason,
        )
        self.last[s.pk] = when

    def revision(self, specimen):
        return self.fresh(specimen).revision

    def take(self, before, count=1, *, roomy=False):
        """Remove and return `count` stored singles idle since before `before`. `roomy` keeps
        only those whose box still has a never-used position, for scenarios that move them."""
        chosen = [
            s
            for s in self.singles
            if self.last[s.pk] < before - timedelta(hours=1)
            and self.box_of[s.pk] not in MOVED
            and (not roomy or self.free.get(self.box_of[s.pk]))
        ][:count]
        if len(chosen) < count:
            raise CommandError("The full demonstration ran out of specimens for a scenario.")
        taken = {s.pk for s in chosen}
        self.singles = [s for s in self.singles if s.pk not in taken]
        return chosen

    # ------------------------------------------------------------------ the story

    def run(self):
        (self.units, self.types, self.containers, self.participants, self.collections) = (
            seed_full_catalogues(actor=self.mgr)
        )
        seed_full_storage(actor=self.mgr)
        self.schedule_draws()
        self.register_singles()
        self.aliquot_families()
        self.derivatives()
        self.rng.shuffle(self.singles)
        self.returned_check_outs()
        self.assays()
        self.check_outs_in_progress()
        self.thaws_and_holds()
        self.corrections()
        self.moves()
        self.unstored()
        self.imports()
        self.disposals()

    def register_singles(self):
        counters = {}
        for box, kind, container, quantity, count, collection, known in SINGLES:
            codes = COHORTS[collection] if collection else (None,)
            if box == "RF01-B1":  # the newest draws: tubes still waiting for processing
                codes = codes[-count:]
            for _ in range(count):
                n = counters.setdefault((collection, kind), 0)
                counters[(collection, kind)] = n + 1
                code = codes[n % len(codes)]
                collected = self.draw[code] if code else self.ago(days=40)
                s = self.register(
                    kind, container, quantity, code, collection, box, collected, known=known
                )
                self.singles.append(s)
        # The primary tubes of the aliquot families are registered at the participants' draws too.
        self.primaries = []
        for collection, count, _ in FAMILIES:
            for code in COHORTS[collection][:count]:
                s = self.register(
                    "PLASMA", CRYOTUBE, "4.000000", code, collection, "BOX-008", self.draw[code]
                )
                self.primaries.append((s, collection))

    def aliquot_families(self):
        """Each primary plasma tube is checked out, split into three 1 mL aliquots stored in the
        cohort's aliquot box, and checked back in with the thaw recorded as seen or unknown."""
        boxes = {collection: box for collection, _, box in FAMILIES}
        for i, (parent, collection) in enumerate(self.primaries):
            start = self.last[parent.pk] + timedelta(hours=18 + (i % 5))
            self.check_out(
                parent, start, "Aliquoting bench", reason="Aliquoting after registration"
            )
            s = self.fresh(parent)
            children = services.derive_specimens(
                actor=self.tech,
                parent_id=s.pk,
                expected_revision=s.revision,
                expected_occupancy_revision=s.occupancy.revision,
                kind="aliquot",
                consumed=Decimal("3.000000"),
                children=tuple(
                    ChildSpec(
                        self.types["PLASMA"].pk,
                        self.containers[VIAL].pk,
                        Decimal("1.000000"),
                        "1.0",
                        self.units[ML].pk,
                        self.position(boxes[collection]).pk,
                    )
                    for _ in range(3)
                ),
                procedure="Split into three 1.0 mL aliquots",
                occurred_at=start + timedelta(minutes=15),
                reason="Aliquoting after registration",
            )
            for child in children:
                self.last[child.pk] = start + timedelta(minutes=15)
            self.last[parent.pk] = start + timedelta(minutes=15)
            self.check_in(
                parent,
                start + timedelta(minutes=40),
                True if i % 4 == 0 else None if i % 7 == 3 else False,
                reason="Returned after aliquoting",
            )

    def derivatives(self):
        """Six DNA extracts from buffy coat: a derivative changes type and unit (mL to uL)."""
        buffy = [s for s in self.singles if self.box_of[s.pk] == "MF02-B3"][:6]
        for i, parent in enumerate(buffy):
            start = self.last[parent.pk] + timedelta(days=2, hours=i)
            self.check_out(parent, start, "Extraction bench", reason="Extraction (synthetic demo)")
            s = self.fresh(parent)
            services.derive_specimens(
                actor=self.tech,
                parent_id=s.pk,
                expected_revision=s.revision,
                expected_occupancy_revision=s.occupancy.revision,
                kind="derivative",
                consumed=Decimal("0.200000"),
                children=(
                    ChildSpec(
                        self.types["DNA-EXTRACT"].pk,
                        self.containers[MICRO].pk,
                        Decimal("50.000000"),
                        "50",
                        self.units["uL"].pk,
                        self.position("VP-03-L02").pk,
                    ),
                ),
                procedure="DNA extraction, eluted in 50 uL",
                occurred_at=start + timedelta(minutes=20),
                reason="Extraction (synthetic demo)",
            )
            self.last[parent.pk] = start + timedelta(minutes=20)
            self.check_in(
                parent, start + timedelta(minutes=50), False, reason="Returned after extraction"
            )

    # ------------------------------------------------------------------ work on stored specimens

    def returned_check_outs(self):
        """Six check-outs that came back, with every thaw answer: seen, not seen, not known."""
        first = None
        for i, specimen in enumerate(self.take(self.ago(days=19), 6)):
            out = self.ago(days=18 - 2 * i, hours=i)
            self.check_out(specimen, out, DESTINATIONS[i % len(DESTINATIONS)])
            self.check_in(
                specimen,
                out + timedelta(hours=3 + i),
                (True, True, False, None, True, False)[i],
            )
            self.singles.append(specimen)
            first = first or specimen
        thawed = ThawEvent.objects.filter(specimen=first, kind="thaw").get()
        services.correct_thaw(
            actor=self.mgr,
            event_id=thawed.pk,
            expected_revision=self.revision(first),
            count=0,
            reason="The bench log shows this vial stayed frozen; the thaw was recorded in error",
        )

    def assays(self):
        """Material used: checked out, part used, returned; two vials used up entirely."""
        plan = (
            ("0.300000", False),
            ("0.450000", False),
            ("0.250000", False),
            ("0.400000", False),
            (None, True),
            (None, True),
        )
        for i, (used, everything) in enumerate(plan):
            specimen = self.take(self.ago(days=12 - i), 1)[0]
            out = self.ago(days=11 - i, hours=2)
            self.check_out(specimen, out, "Assay bench 3", reason="Assay run (synthetic demo)")
            s = self.fresh(specimen)
            amount = format(s.quantity_on_hand, "f") if everything else used
            services.consume_quantity(
                actor=self.tech,
                specimen_id=s.pk,
                expected_revision=s.revision,
                quantity=amount,
                supplied_text=amount,
                occurred_at=out + timedelta(hours=1),
                reason="Used up in the assay" if everything else "Used in the assay",
            )
            self.last[s.pk] = out + timedelta(hours=1)
            self.check_in(
                specimen, out + timedelta(hours=2), False, reason="Returned after the assay"
            )
            self.singles.append(specimen)

    def check_outs_in_progress(self):
        """Ten specimens out right now, from two hours to a month; the oldest are the ones a
        manager would chase. Two have been handed to another person."""
        ages_hours = (2, 5, 26, 52, 4 * 24, 8 * 24, 11 * 24, 15 * 24, 24 * 24, 31 * 24)
        people = (self.tech, self.mgr, self.tech, self.admin, self.tech)
        self.out = []
        for i, hours in enumerate(ages_hours):
            specimen = self.take(self.ago(hours=hours), 1)[0]
            recipient = people[i % len(people)]
            when = self.ago(hours=hours)
            self.check_out(
                specimen,
                when,
                DESTINATIONS[(i + 2) % len(DESTINATIONS)],
                to=recipient,
                reason="Needed for a run that was then postponed" if hours > 7 * 24 else "In use",
            )
            self.out.append((specimen, when, recipient))
        for specimen, when, holder in self.out[6:8]:
            s = self.fresh(specimen)
            recipient = self.admin if holder == self.mgr else self.mgr
            services.transfer_custody(
                actor=self.tech,
                specimen_id=s.pk,
                expected_revision=s.revision,
                expected_occupancy_revision=s.occupancy.revision,
                recipient_id=recipient.pk,
                destination="Quality review",
                occurred_at=when + timedelta(days=2),
                reason="Handed over for review (synthetic demo)",
            )

    def thaws_and_holds(self):
        for i, specimen in enumerate(self.take(self.ago(days=9), 4)):
            s = self.fresh(specimen)
            services.record_thaw(
                actor=self.tech,
                specimen_id=s.pk,
                expected_revision=s.revision,
                occurred_at=self.ago(days=8 - i),
                reason="Thaw seen during a freezer defrost (synthetic demo)",
            )
            self.singles.append(specimen)
        for i, (scope, reason) in enumerate(HOLD_REASONS):
            specimen = self.take(self.ago(days=7), 1)[0]
            services.place_hold(
                actor=self.tech,
                specimen_id=specimen.pk,
                expected_revision=self.revision(specimen),
                scope=scope,
                occurred_at=self.ago(days=6 - i),
                reason=reason,
            )
        for scope, reason, released in (
            (
                "disposal",
                "Possible re-test requested (synthetic demo)",
                "The re-test was not needed",
            ),
            (
                "all_use",
                "Label could not be read (synthetic demo)",
                "Label replaced; identity confirmed",
            ),
        ):
            specimen = self.take(self.ago(days=7), 1)[0]
            hold = services.place_hold(
                actor=self.tech,
                specimen_id=specimen.pk,
                expected_revision=self.revision(specimen),
                scope=scope,
                occurred_at=self.ago(days=6),
                reason=reason,
            )
            services.release_hold(
                actor=self.mgr,
                hold_id=hold.pk,
                expected_revision=self.revision(specimen),
                occurred_at=self.ago(days=4),
                reason=released,
            )
            self.singles.append(specimen)

    def corrections(self):
        """A manager's reasoned corrections; the original records stay as they were."""
        a, b, c = self.take(self.ago(days=6), 3)
        # Quantities measured again; the second weighing is undone below. Corrections advance
        # a specimen's clock to now, so the backdated entries come first.
        for specimen, delta, reason in (
            (b, "-0.100000", "Re-measured on the balance: less than recorded"),
            (c, "0.050000", "Re-measured on the balance: more than recorded"),
        ):
            entry = services.adjust_quantity(
                actor=self.mgr,
                specimen_id=specimen.pk,
                expected_revision=self.revision(specimen),
                delta=delta,
                supplied_text=delta,
                occurred_at=self.ago(days=5),
                reason=reason,
            )
        other = self.participants[COHORT_B[-1]]
        services.correct_specimen(
            actor=self.mgr,
            specimen_id=a.pk,
            expected_revision=self.revision(a),
            values=CorrectionValues(fields=frozenset({"participant"}), participant_id=other.pk),
            reason="Registered under the wrong participant code; checked against the receipt log",
        )
        services.correct_specimen(
            actor=self.mgr,
            specimen_id=b.pk,
            expected_revision=self.revision(b),
            values=CorrectionValues(
                fields=frozenset({"collected_at"}),
                collected_at=self.fresh(b).collected_at - timedelta(minutes=20),
            ),
            reason="Collection time was read from the wrong line of the paper log",
        )
        services.correct_specimen(
            actor=self.mgr,
            specimen_id=c.pk,
            expected_revision=self.revision(c),
            values=CorrectionValues(
                fields=frozenset({"container_type"}), container_type_id=self.containers[CRYOTUBE].pk
            ),
            reason="Registered with the wrong container type",
        )
        services.reverse_quantity(
            actor=self.mgr,
            entry_id=entry.pk,
            expected_revision=self.revision(c),
            reason="The second weighing was of the wrong tube",
        )
        self.singles.extend((a, b, c))
        # A destination typed wrongly at check-out.
        target = self.out[3][0]
        event = CustodyEvent.objects.filter(specimen=target, kind="check_out").first()
        services.correct_custody(
            actor=self.mgr,
            event_id=event.pk,
            expected_revision=self.revision(target),
            values={"destination": "Assay bench 3"},
            reason="The destination was typed with the wrong bench number",
        )

    def moves(self):
        """Relocations within a box, a correction of a wrong one, and containers moved with
        everything in them. Specimens in the moved boxes see no later single-specimen work."""
        for specimen in self.take(self.ago(days=6), 4, roomy=True):
            target = self.position(self.box_of[specimen.pk])
            s = self.fresh(specimen)
            services.relocate_specimen(
                actor=self.tech,
                specimen_id=s.pk,
                expected_revision=s.revision,
                expected_occupancy_revision=s.occupancy.revision,
                position_id=target.pk,
                occurred_at=self.ago(days=5),
                reason="Consolidated toward the front of the box",
            )
            self.where[specimen.pk] = target
            self.singles.append(specimen)
        specimen = self.take(self.ago(days=6), 1, roomy=True)[0]
        box = self.box_of[specimen.pk]
        wrong, right = self.position(box), self.position(box)
        s = self.fresh(specimen)
        services.relocate_specimen(
            actor=self.tech,
            specimen_id=s.pk,
            expected_revision=s.revision,
            expected_occupancy_revision=s.occupancy.revision,
            position_id=wrong.pk,
            occurred_at=self.ago(days=5, hours=1),
            reason="Moved to the next free position",
        )
        move = (
            CustodyEvent.objects.filter(specimen=specimen, kind="relocate")
            .latest("occurred_at", "recorded_at")
            .move_event
        )
        s = self.fresh(specimen)
        services.relocate_specimen(
            actor=self.mgr,
            specimen_id=s.pk,
            expected_revision=s.revision,
            expected_occupancy_revision=s.occupancy.revision,
            position_id=right.pk,
            occurred_at=self.ago(days=4, hours=12),
            reason="Recorded the wrong position; the vial is in the next one",
            corrects_id=move.pk,
        )
        self.where[specimen.pk] = right
        self.singles.append(specimen)
        # Containers move with their contents in one event each.
        self.move_box(
            "BOX-009", "T-02-S3", 1, "Pilot box moved to tank VP-02 (synthetic demo)", self.mgr
        )
        self.move_node("VP-03-C1-S2", "VP-03-C2", 2, "Legacy shelf regrouped in canister 2")
        wrong_move = self.move_box(
            "MF02-B3", "MF-02-R2", 2, "Moved to rack 2 (synthetic demo)", self.tech, days=4
        )
        box = Box.objects.get(code="MF02-B3")
        storage.move_container(
            actor=self.mgr,
            kind="box",
            object_id=box.pk,
            expected_revision=box.revision,
            expected_gate_revision=self._gate(),
            target_kind="node",
            target_id=LayoutNode.objects.get(code="MF-02-R1").pk,
            target_slot=4,
            occurred_at=self.ago(days=3, hours=12),
            reason="Recorded the wrong rack; the box belongs on rack 1, slot 4",
            corrects_id=wrong_move.pk,
        )

    def _gate(self):
        return StorageGate.objects.filter(pk=1).values_list("revision", flat=True).first() or 1

    def move_box(self, code, node, slot, reason, actor, *, days=4):
        box = Box.objects.get(code=code)
        return storage.move_container(
            actor=actor,
            kind="box",
            object_id=box.pk,
            expected_revision=box.revision,
            expected_gate_revision=self._gate(),
            target_kind="node",
            target_id=LayoutNode.objects.get(code=node).pk,
            target_slot=slot,
            occurred_at=self.ago(days=days),
            reason=reason,
        )

    def move_node(self, code, target, slot, reason):
        node = LayoutNode.objects.get(code=code)
        return storage.move_container(
            actor=self.mgr,
            kind="node",
            object_id=node.pk,
            expected_revision=node.revision,
            expected_gate_revision=self._gate(),
            target_kind="node",
            target_id=LayoutNode.objects.get(code=target).pk,
            target_slot=slot,
            occurred_at=self.ago(days=4),
            reason=reason,
        )

    def unstored(self):
        """Today's receipts, waiting for a box (the dashboard's queue)."""
        for i, hours in enumerate((5, 6, 8, 29)):
            code = COHORT_B[10 + i]
            self.register(
                "SERUM" if i % 2 else "PLASMA",
                VIAL,
                "1.200000",
                code,
                "COH-B",
                None,
                self.ago(hours=hours + 2),
            )

    # ------------------------------------------------------------------ bulk import

    def csv(self, rows):
        return (
            ",".join(importing.COLUMNS) + "\n" + "".join(",".join(r) + "\n" for r in rows)
        ).encode()

    def import_rows(self, prefix, kind, container, quantity, box, count, participants, days):
        rows = []
        for n in range(count):
            position = self.position(box)
            collected = self.ago(days=days, hours=n % 6).isoformat()
            rows.append(
                (
                    f"{prefix}-{n + 1:03d}",
                    kind,
                    container,
                    quantity,
                    ML,
                    collected,
                    "",
                    "",
                    participants[n % len(participants)],
                    "COH-B",
                    box,
                    position.label,
                    "0",
                )
            )
        return rows

    def imports(self):
        """Two shipments committed, one valid and waiting for its commit, one with errors."""
        first = self.import_rows("SITE2-A", "SERUM", VIAL, "1.000000", "MF03-B1", 30, SHIPMENT, 10)
        batch = importing.validate_import(
            actor=self.tech, csv_bytes=self.csv(first), filename="partner-site-shipment-1.csv"
        )
        commit = importing.commit_import(
            actor=self.tech,
            batch_id=batch.pk,
            expected_sha256=batch.sha256,
            reason="Shipment from the partner site received and checked (synthetic demo)",
        )
        self.shipment = commit.specimen_ids
        second = self.import_rows(
            "URINE-B", "URINE", URINE_TUBE, "6.000000", "MF01-B3", 24, COHORT_B, 3
        )
        batch = importing.validate_import(
            actor=self.tech, csv_bytes=self.csv(second), filename="urine-cohort-b.csv"
        )
        importing.commit_import(
            actor=self.tech,
            batch_id=batch.pk,
            expected_sha256=batch.sha256,
            reason="Second collection round registered from the site spreadsheet",
        )
        waiting = self.import_rows("SITE2-B", "SERUM", VIAL, "1.000000", "MF02-B4", 6, SHIPMENT, 1)
        importing.validate_import(
            actor=self.tech, csv_bytes=self.csv(waiting), filename="partner-site-shipment-2.csv"
        )
        yesterday = self.ago(days=1).isoformat()

        def bad(key, kind, quantity, unit, participant, box, label):
            return (
                key,
                kind,
                VIAL,
                quantity,
                unit,
                yesterday,
                "",
                "",
                participant,
                "COH-B",
                box,
                label,
                "",
            )

        broken = self.csv(
            [
                bad("BAD-1", "SERUM", "1.0000001", ML, "P-999999", "", ""),
                bad("BAD-2", "UNKNOWN-TYPE", "1", "ML", "", "BOX-006", "A1"),
                bad("BAD-3", "SERUM", "1", ML, SHIPMENT[0], "BOX-006", "A1"),
            ]
        )
        importing.validate_import(
            actor=self.tech, csv_bytes=broken, filename="site-export-errors.csv"
        )
        try:
            from specimens.labels import render_labels

            render_labels(actor=self.tech, specimen_ids=tuple(self.shipment))
        except OSError:  # WeasyPrint's native libraries are missing on this machine
            pass

    # ------------------------------------------------------------------ disposal workflow

    def request(self, specimen, *, actor=None, why):
        return services.request_disposal(
            actor=actor or self.tech,
            specimen_id=specimen.pk,
            expected_revision=self.revision(specimen),
            method=WASTE[0],
            destination=WASTE[1],
            reason=why,
        )

    def decide(self, request, decision, why):
        return services.decide_disposal(
            actor=self.mgr,
            request_id=request.pk,
            expected_revision=self.revision(request.specimen_id),
            decision=decision,
            reason=why,
        )

    def execute(self, request, why):
        s = self.fresh(request.specimen_id)
        services.execute_disposal(
            actor=self.tech,
            request_id=request.pk,
            expected_revision=s.revision,
            expected_occupancy_revision=s.occupancy.revision,
            occurred_at=timezone.now(),
            reason=why,
        )

    def disposals(self):
        """Requests in every state. Recorded at the time of seeding: a request, a decision and
        a disposal always carry the moment they were made."""
        now = self.now
        pool = self.take(now - timedelta(days=1), 14, roomy=True)
        # Executed: requested by one person, approved by another, performed and recorded.
        for specimen in pool[:6]:
            request = self.request(specimen, why="Study closed; remaining material not needed")
            self.decide(request, "approve", "Matches the closed study's disposal plan")
            self.execute(request, "Autoclaved and discarded")
        # Approved, waiting for someone to carry it out.
        for specimen in pool[6:8]:
            request = self.request(specimen, why="Duplicate aliquot of a stored vial")
            self.decide(request, "approve", "Duplicate confirmed against the inventory")
        # Waiting for a decision.
        for specimen in pool[8:10]:
            self.request(specimen, actor=self.admin, why="Tube cracked on removal; contents lost")
        self.request(pool[10], why="Not needed after the method comparison")
        # Rejected, and withdrawn by the requester.
        rejected = self.request(pool[11], why="Remaining material not needed")
        self.decide(rejected, "reject", "Some material is still needed for a re-test")
        withdrawn = self.request(pool[12], why="Requested by mistake")
        services.decide_disposal(
            actor=self.tech,
            request_id=withdrawn.pk,
            expected_revision=self.revision(pool[12]),
            decision="cancel",
            reason="Wrong tube selected",
        )
        # A hold placed after the request blocks its approval until the hold is released.
        services.place_hold(
            actor=self.tech,
            specimen_id=pool[10].pk,
            expected_revision=self.revision(pool[10]),
            scope="disposal",
            occurred_at=timezone.now(),
            reason="Investigator asked to keep it until the results are confirmed",
        )
        # An approval that went stale: the specimen was moved afterwards.
        stale = self.request(pool[13], why="Remaining material not needed")
        self.decide(stale, "approve", "Checked against the plan")
        s = self.fresh(pool[13])
        target = self.position(self.box_of[pool[13].pk])
        services.relocate_specimen(
            actor=self.tech,
            specimen_id=s.pk,
            expected_revision=s.revision,
            expected_occupancy_revision=s.occupancy.revision,
            position_id=target.pk,
            occurred_at=timezone.now(),
            reason="Moved to make room before the disposal was carried out",
        )
