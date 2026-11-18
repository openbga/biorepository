"""Cryo design tokens (docs/ui-standards.md section 12).

static/css/app.css defines every colour once, in its Tokens section, for the light, dark and print
palettes; the rest of the stylesheet (and static/css/documentation.css) only reads tokens. The
retired "cold-store log" names are gone: neither defined nor read.
"""

import re
from pathlib import Path

from django.conf import settings

ROOT = Path(settings.BASE_DIR)
APP_CSS = ROOT / "static/css/app.css"
DOC_CSS = ROOT / "static/css/documentation.css"

SECTION = re.compile(r"/\* =+ ([^\n*]+)")
COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?)\((?!\s*var\()")

# Sections still drawn with literal colours (none since the Cryo sign-in and print redraw).
PENDING_SECTIONS: set[str] = set()

# Retired cold-store log tokens: never defined or read again.
RETIRED = (
    "paper-rule",
    "paper-cell",
    "paper-grid",
    "paper-haze",
    "paper-haze-opacity",
    "margin",
    "margin-width",
    "margin-x",
    "marker",
    "marker-tip",
    "highlight",
    "highlight-wash",
    "highlight-edge",
    "on-highlight",
    "band",
    "heading",
    "label-size",
    "label-tracking",
    "sidenav-width",
    "topbar-edge",
    "topbar-field-border",
    "topbar-accent",
)


def sections(css):
    """(name, text) for each "/* ===== Name" section, the text before the first one as "Header"."""
    marks = [(m.start(), m.group(1).strip()) for m in SECTION.finditer(css)]
    bounds = [(0, "Header"), *marks]
    for (start, name), (end, _) in zip(bounds, [*bounds[1:], (len(css), "")], strict=True):
        yield name, css[start:end]


def without_urls(css):
    return re.sub(r"url\([^)]*\)", "url()", css)


def without_comments(css):
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def mode_block(css, mode):
    start = css.index(f'[data-bs-theme="{mode}"] {{')
    return css[start : css.index("\n}", start)]


def test_colours_live_only_in_the_tokens_section():
    offenders = []
    for name, text in sections(APP_CSS.read_text()):
        if name in {"Tokens", *PENDING_SECTIONS}:
            continue
        for match in COLOUR.finditer(without_urls(without_comments(text))):
            offenders.append(f"{name}: {match.group(0)}")
    for match in COLOUR.finditer(without_urls(without_comments(DOC_CSS.read_text()))):
        offenders.append(f"documentation.css: {match.group(0)}")
    assert not offenders, f"Use a --vr-* token, not a colour literal: {offenders}"


def test_every_mode_token_is_defined_in_both_modes():
    css = APP_CSS.read_text()
    light = set(re.findall(r"(--vr-[\w-]+):", mode_block(css, "light")))
    dark = set(re.findall(r"(--vr-[\w-]+):", mode_block(css, "dark")))
    assert light and dark
    assert light == dark, f"light only: {sorted(light - dark)}; dark only: {sorted(dark - light)}"


def test_retired_tokens_are_gone():
    css = APP_CSS.read_text() + DOC_CSS.read_text()
    found = [
        f"--vr-{token}" for token in RETIRED if re.search(rf"--vr-{re.escape(token)}(?![\w-])", css)
    ]
    assert not found, f"Retired cold-store log tokens defined or read: {found}"


def test_every_token_read_is_defined():
    css = APP_CSS.read_text() + DOC_CSS.read_text()
    defined = set(re.findall(r"(--[\w-]+)\s*:", css))
    read = set(re.findall(r"var\((--vr-[\w-]+)", css))
    assert not read - defined, f"Undefined tokens: {sorted(read - defined)}"


def test_pending_sections_still_exist():
    names = {name for name, _ in sections(APP_CSS.read_text())}
    assert PENDING_SECTIONS <= names, (
        f"Delete from PENDING_SECTIONS: {sorted(PENDING_SECTIONS - names)}"
    )
