"""Specimens that were in a storage unit during a period.

Storage answers which storage keys were in the unit and when (`storage.queries.occupants_during`,
from the append-only recorded movements, including the per-item members of box and rack moves);
this module resolves those keys to specimens for the excursion pages and the capture at closing.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from specimens.models import Specimen
from storage import queries as storage_queries


@dataclass(frozen=True)
class AffectedRow:
    specimen_id: UUID
    accession: str
    present_from: datetime
    present_until: datetime
    location: str  # recorded display path when its first presence in the period began
    intervals: tuple[tuple[datetime, datetime], ...]


def affected_specimens(*, actor, unit_id, start, end):
    """Specimens present in the unit during [start, end], ordered by recorded location.

    Read roles; storage raises ValidationError for naive times or an end before the start."""
    presences = {
        p.occupancy_id: p
        for p in storage_queries.occupants_during(
            actor=actor, unit_id=unit_id, start=start, end=end
        )
    }
    if not presences:
        return []
    specimens = Specimen.objects.filter(occupancy_id__in=list(presences)).values_list(
        "pk", "accession", "occupancy_id"
    )
    rows = []
    for pk, accession, occupancy_id in specimens:
        presence = presences[occupancy_id]
        rows.append(
            AffectedRow(
                specimen_id=pk,
                accession=accession,
                present_from=presence.intervals[0][0],
                present_until=presence.intervals[-1][1],
                location=(presence.path or {}).get("display", ""),
                intervals=tuple(presence.intervals),
            )
        )
    rows.sort(key=lambda row: (row.location, row.accession))
    return rows
