"""Specimen IDs for storage pages, without storage importing specimens.

A storage template loads this library and names the occupied positions it is about to draw:

    {% load specimen_wells %}
    {% attach_specimens occupied request.user %}
    {% if p.claim.specimen %}
      <a href="{{ p.claim.specimen.url }}">{{ p.claim.specimen.accession }}</a>
    {% endif %}

`attach_specimens` renders nothing. It reads every specimen for the given positions' claims in one
query and sets `claim.specimen` (accession, type code, URL) on each claim it resolves; a claim it
cannot resolve, or a reader without a staff role, gets no attribute. Positions are anything with a
`claim` attribute holding a storage Occupancy (or None), as on storage's box page.
"""

from dataclasses import dataclass

from django import template
from django.urls import reverse

from accounts.roles import has_role
from specimens.models import Specimen
from specimens.services import READ_ROLES

register = template.Library()


@dataclass(frozen=True)
class WellSpecimen:
    pk: object
    accession: str
    type_code: str
    url: str


@register.simple_tag
def attach_specimens(positions, user):
    claims = {p.claim.pk: p.claim for p in positions if getattr(p, "claim", None) is not None}
    if not claims or not has_role(user, *READ_ROLES):
        return ""
    rows = Specimen.objects.filter(occupancy_id__in=list(claims)).values_list(
        "pk", "occupancy_id", "accession", "specimen_type__code"
    )
    for pk, occupancy_id, accession, type_code in rows:
        claims[occupancy_id].specimen = WellSpecimen(
            pk=pk,
            accession=accession,
            type_code=type_code,
            url=reverse("specimens:specimen-detail", args=[pk]),
        )
    return ""
