"""Specimen label PDFs: the accession in text and as a Code 128 barcode, one label per page.

Nothing else is printed (no participant, no location). The label stock (size) is owner
configuration: settings.SPECIMEN_LABEL_STOCK = {"code", "version", "width_mm", "height_mm"}; the
default below is a working assumption, not a recommendation for cold-storage labels. Rendering
uses only the application's template and stylesheet with escaped data; external and file fetches
are refused. Printing changes no physical state: each successful render is audited with the
accessions, count, stock and template version, and claims nothing about a label being affixed.
"""

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from barcode import Code128
from barcode.writer import SVGWriter
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.template.loader import render_to_string
from django.utils.safestring import mark_safe

from accounts.roles import require_role
from core.audit import record_event

from .models import OutputRecord, Specimen
from .services import READ_ROLES

MAX_LABELS = 200
TEMPLATE_VERSION = 1
MODULE_MM = Decimal("0.25")  # narrowest bar
QUIET_MODULES = 10
MARGIN_MM = Decimal("2")
STYLESHEET = Path(__file__).resolve().parent / "label.css"


@dataclass(frozen=True)
class LabelStock:
    code: str
    version: int
    width_mm: Decimal
    height_mm: Decimal

    @property
    def description(self):
        return f"{self.code} v{self.version}, {self.width_mm} × {self.height_mm} mm"


def default_stock():
    configured = getattr(settings, "SPECIMEN_LABEL_STOCK", None) or {}
    return LabelStock(
        code=configured.get("code", "DEFAULT-51X25"),
        version=int(configured.get("version", 1)),
        width_mm=Decimal(str(configured.get("width_mm", "50.8"))),
        height_mm=Decimal(str(configured.get("height_mm", "25.4"))),
    )


def _modules(code):
    return len(Code128(code).build()[0])


def barcode_svg(code, stock):
    if not code or len(code) > 32 or not all(32 <= ord(c) <= 126 for c in code):
        raise ValidationError("A label identifier is 1 to 32 printable ASCII characters.")
    width = (_modules(code) + 2 * QUIET_MODULES) * MODULE_MM
    if width > stock.width_mm - 2 * MARGIN_MM:
        raise ValidationError(
            f"The barcode for {code} needs {width} mm and does not fit the "
            f"{stock.width_mm} mm label stock at a readable bar width."
        )
    svg = (
        Code128(code, writer=SVGWriter())
        .render(
            {
                "module_width": float(MODULE_MM),
                "module_height": float(min(Decimal(10), stock.height_mm / 2)),
                "quiet_zone": float(QUIET_MODULES * MODULE_MM),
                "font_size": 0,
                "write_text": False,
            }
        )
        .decode("utf-8")
    )
    # Presentation attributes instead of style declarations (WeasyPrint ignores those in
    # inline SVG) and no repeated element id when several labels share one document.
    svg = (
        svg[svg.index("<svg") :]
        .replace('style="fill:black;"', 'fill="black"')
        .replace('style="fill:white"', 'fill="white"')
        .replace(' id="barcode_group"', "")
    )
    # Only library-generated SVG of a validated identifier is marked safe.
    return mark_safe(svg)  # noqa: S308


def _stylesheet(stock):
    return STYLESHEET.read_text().replace(
        "/* PAGE */",
        f"@page {{ size: {stock.width_mm}mm {stock.height_mm}mm; margin: {MARGIN_MM}mm; }}",
    )


def label_specimens(specimen_ids):
    ids = list(specimen_ids)
    if not 1 <= len(ids) <= MAX_LABELS:
        raise ValidationError(f"Print from 1 to {MAX_LABELS} labels at a time.")
    found = Specimen.objects.in_bulk(ids)
    missing = [str(i) for i in ids if i not in found and str(i) not in map(str, found)]
    if missing:
        raise Specimen.DoesNotExist(missing[0])
    by_str = {str(k): v for k, v in found.items()}
    return [by_str[str(i)] for i in ids]


def render_labels(*, actor, specimen_ids, stock=None):
    """PDF bytes, one page per given specimen id (repeats are reprints), in the given order."""
    require_role(actor, *READ_ROLES)
    stock = stock or default_stock()
    specimens = label_specimens(specimen_ids)
    accessions = [s.accession for s in specimens]  # immutable snapshot
    rows = [{"accession": a, "barcode": barcode_svg(a, stock)} for a in accessions]
    html = render_to_string("specimens/pdf/labels.html", {"rows": rows})

    from weasyprint import CSS, HTML
    from weasyprint.urls import URLFetcher

    class DenyFetcher(URLFetcher):
        def fetch(self, url, headers=None):
            raise ValueError("Labels cannot load external resources.")

    fetcher = DenyFetcher()
    document = HTML(string=html, url_fetcher=fetcher).render(
        stylesheets=[CSS(string=_stylesheet(stock), url_fetcher=fetcher)]
    )
    if len(document.pages) != len(rows):
        raise ValidationError(
            f"The label text does not fit the {stock.description} stock. Choose larger stock."
        )
    pdf = document.write_pdf()
    with transaction.atomic():
        record = OutputRecord.objects.create(
            kind=OutputRecord.Kind.LABELS,
            actor=actor,
            count=len(rows),
            details={
                "accessions": accessions,
                "stock": stock.code,
                "stock_version": stock.version,
                "width_mm": str(stock.width_mm),
                "height_mm": str(stock.height_mm),
                "template_version": TEMPLATE_VERSION,
            },
        )
        record_event(
            actor=actor,
            action="specimens.labels_printed",
            obj=record,
            after={"count": len(rows), "accessions": accessions, "stock": stock.code},
            reason="Labels generated (printing and affixing are not recorded)",
        )
    return pdf
