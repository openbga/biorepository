# ruff: noqa: E501 (SVG markup lines)
"""Draw the Cryo sign-in illustration (static/img/entrance/signin-{light,dark}.svg).

A cold store in quiet daylight (light) or under its blue night light (dark): an ultra-low freezer at
-80 C, an LN2 vapour-phase tank cut away to show its rack of boxes above the liquid, the box of
positions seen from above, and the storage path's capsules naming each unit. The palettes follow the
Cryo tokens in static/css/app.css; the files are served as a CSS background on the sign-in page, so
they carry their own colours. Run: python tools/entrance_scene/build.py
"""

from pathlib import Path

OUT = Path(__file__).resolve().parents[2] / "static/img/entrance"

PALETTES = {
    "light": {
        "wall_top": "#e9f1f5",
        "wall_low": "#d6e4ec",
        "floor": "#c9d9e3",
        "floor_near": "#bccfdb",
        "shadow": "#8fa6b6",
        "steel_dark": "#8ea3b2",
        "steel": "#c6d4de",
        "steel_lit": "#f2f6f9",
        "band": "#5f7584",
        "panel": "#163042",
        "screen": "#bfe5f1",
        "screen_text": "#8fdcf5",
        "ice": "#0a6e8e",
        "ice_wash": "#d9f0f7",
        "ice_wash_2": "#bfe5f1",
        "ice_line": "#8ccbe0",
        "ln2": "#0b4a63",
        "ln2_lit": "#2c7593",
        "vapour": "#ffffff",
        "frost": "#ffffff",
        "card": "#fbfdfe",
        "card_line": "#d6e1e8",
        "cap": "#0a6e8e",
        "cap_ring": "#bfe5f1",
        "cap_hold": "#98256a",
        "cap_hold_ring": "#f9e5f0",
        "well": "#e5edf2",
        "text": "#0f2230",
        "muted": "#4a5f6e",
        "crystal": "#bfe5f1",
        "iso": "#8ccbe0",
        "led": "#1b6943",
    },
    "dark": {
        "wall_top": "#0b141a",
        "wall_low": "#10202a",
        "floor": "#0f1c24",
        "floor_near": "#0b161d",
        "shadow": "#02080c",
        "steel_dark": "#1f3442",
        "steel": "#2c4555",
        "steel_lit": "#4f6d80",
        "band": "#8aa2b2",
        "panel": "#06131b",
        "screen": "#134457",
        "screen_text": "#8fdcf5",
        "ice": "#5ccbf0",
        "ice_wash": "#0f3444",
        "ice_wash_2": "#134457",
        "ice_line": "#2c7593",
        "ln2": "#0b3242",
        "ln2_lit": "#1a6b8a",
        "vapour": "#8fdcf5",
        "frost": "#bfe5f1",
        "card": "#111c24",
        "card_line": "#233744",
        "cap": "#5ccbf0",
        "cap_ring": "#134457",
        "cap_hold": "#f29bcd",
        "cap_hold_ring": "#361528",
        "well": "#16242e",
        "text": "#e1ebf1",
        "muted": "#9fb3c0",
        "crystal": "#134457",
        "iso": "#2c7593",
        "led": "#7fd3a5",
    },
}

# BOX-006 as the demo shows it: occupied and held positions, A1 at the top left.
OCCUPIED = {"A2", "A7", "A8", "A9", "B1", "B2", "B3", "B4", "B6", "B9", "B10", "C2", "C3"}
HELD = {"A1", "A3", "A4"}

MONO = "Martian Mono, ui-monospace, SFMono-Regular, Menlo, monospace"
SANS = "Geologica, Segoe UI, system-ui, sans-serif"


def crystal(cx, cy, scale, colour):
    """The brand mark's six-point crystal (static/img/mark.svg), drawn large and quiet."""
    return f"""
  <g transform="translate({cx} {cy}) scale({scale}) translate(-16 -16)" fill="none" stroke="{colour}"
     stroke-width="1.1" stroke-linecap="round" stroke-linejoin="round">
    <path d="M16 5v22M6.5 10.5l19 11M6.5 21.5l19-11"/>
    <path d="M13.5 6.8 16 9l2.5-2.2M13.5 25.2 16 23l2.5 2.2M7.3 13.7l3.1-.9-.6-3.3M24.7 18.3l-3.1.9.6 3.3M7.3 18.3l3.1.9-.6 3.3M24.7 13.7l-3.1-.9.6-3.3"/>
    <circle cx="16" cy="16" r="4.2"/>
  </g>"""


def capsule(x, y, kind, code, temp, c):
    w = 30 + 8.4 * (len(kind) + len(code)) + 9 * len(temp) + 26
    return f"""
  <g transform="translate({x} {y})">
    <rect width="{w:.0f}" height="34" rx="17" fill="{c["card"]}"/>
    <rect width="{w:.0f}" height="34" rx="17" fill="none" stroke="{c["ice_line"]}" stroke-width="1"/>
    <text x="14" y="22" font-family="{SANS}" font-size="12" fill="{c["muted"]}">{kind}</text>
    <text x="{20 + 7.2 * len(kind):.0f}" y="22" font-family="{MONO}" font-size="13" font-weight="600" fill="{c["text"]}">{code}</text>
    <rect x="{w - 14 - 9 * len(temp):.0f}" y="7" width="{9 * len(temp) + 6:.0f}" height="20" rx="10" fill="{c["ice_wash"]}"/>
    <text x="{w - 11 - 9 * len(temp):.0f}" y="21.5" font-family="{MONO}" font-size="12" fill="{c["ice"]}">{temp}</text>
  </g>"""


def box_card(x, y, c):
    rows = "ABCDEFGHIJ"
    pitch, r = 21, 7.6
    cells = []
    for i, row in enumerate(rows):
        for j in range(10):
            pos = f"{row}{j + 1}"
            cx, cy = 34 + j * pitch, 58 + i * pitch
            if pos in HELD:
                cells.append(
                    f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{c["cap_hold"]}" stroke="{c["cap_hold_ring"]}" stroke-width="3"/>'
                )
            elif pos in OCCUPIED:
                cells.append(
                    f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{c["cap"]}" stroke="{c["cap_ring"]}" stroke-width="3"/>'
                )
            elif pos == "J10":
                cells.append(
                    f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{c["well"]}" stroke="{c["cap_hold"]}" stroke-width="1.2" stroke-dasharray="2 2"/>'
                )
            else:
                cells.append(
                    f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{c["shadow"]}" stroke-opacity="0.55" stroke-width="1.2"/>'
                )
    return f"""
  <g transform="translate({x} {y}) rotate(-4)">
    <rect x="4" y="8" width="252" height="270" rx="22" fill="{c["shadow"]}" opacity="0.18"/>
    <rect width="252" height="266" rx="22" fill="{c["card"]}"/>
    <rect x="14" y="40" width="224" height="212" rx="14" fill="{c["well"]}"/>
    <text x="18" y="27" font-family="{MONO}" font-size="13" font-weight="600" fill="{c["text"]}">BOX-006</text>
    <text x="234" y="27" text-anchor="end" font-family="{SANS}" font-size="11" fill="{c["muted"]}">16 of 100</text>
    {"".join(cells)}
  </g>"""


def freezer(x, y, c):
    return f"""
  <g transform="translate({x} {y})">
    <ellipse cx="125" cy="508" rx="150" ry="16" fill="{c["shadow"]}" opacity="0.28"/>
    <rect width="250" height="500" rx="18" fill="url(#steel)"/>
    <rect x="10" y="10" width="230" height="400" rx="12" fill="{c["steel_lit"]}" opacity="0.55"/>
    <rect x="10" y="10" width="230" height="400" rx="12" fill="none" stroke="{c["steel_dark"]}" stroke-width="1.5"/>
    <rect x="208" y="140" width="12" height="130" rx="6" fill="{c["band"]}"/>
    <rect x="34" y="34" width="120" height="58" rx="10" fill="{c["panel"]}"/>
    <text x="94" y="72" text-anchor="middle" font-family="{MONO}" font-size="21" font-weight="600" fill="{c["screen_text"]}">−80.0</text>
    <circle cx="170" cy="48" r="5" fill="{c["led"]}"/>
    <g stroke="{c["band"]}" stroke-width="3" stroke-linecap="round" opacity="0.6">
      <path d="M40 440 H210"/><path d="M40 456 H210"/><path d="M40 472 H210"/>
    </g>
    <path d="M10 22 Q 60 8 120 18 T 240 16" fill="none" stroke="{c["frost"]}" stroke-width="5" stroke-linecap="round" opacity="0.8"/>
  </g>"""


def tank(x, y, c):
    rack = []
    for i in range(4):
        by = 120 + i * 58
        caps = "".join(
            f'<circle cx="{26 + k * 15}" cy="{by + 22}" r="4.6" fill="{c["cap"] if (i + k) % 3 else c["ice_wash_2"]}"/>'
            for k in range(6)
        )
        rack.append(
            f'<rect x="14" y="{by}" width="100" height="44" rx="6" fill="{c["card"]}" stroke="{c["ice_line"]}" stroke-width="1.2"/>{caps}'
        )
    return f"""
  <g transform="translate({x} {y})">
    <ellipse cx="160" cy="470" rx="190" ry="20" fill="{c["shadow"]}" opacity="0.3"/>
    <path d="M20 70 Q20 40 50 40 H270 Q300 40 300 70 V440 Q300 462 278 462 H42 Q20 462 20 440 Z" fill="url(#steel)"/>
    <path d="M44 92 Q44 78 58 78 H262 Q276 78 276 92 V430 Q276 442 264 442 H56 Q44 442 44 430 Z" fill="url(#vapour)"/>
    <path d="M44 360 H276 V430 Q276 442 264 442 H56 Q44 442 44 430 Z" fill="{c["ln2"]}"/>
    <path d="M44 360 H276" stroke="{c["ln2_lit"]}" stroke-width="3"/>
    <g fill="none" stroke="{c["ln2_lit"]}" stroke-width="1.6" stroke-linecap="round" opacity="0.8">
      <path d="M62 384 q10 -6 20 0 t20 0 t20 0"/><path d="M150 402 q10 -6 20 0 t20 0 t20 0"/><path d="M90 420 q10 -6 20 0 t20 0"/>
    </g>
    <g transform="translate(130 0)">
      <rect x="6" y="96" width="116" height="270" rx="8" fill="none" stroke="{c["band"]}" stroke-width="3"/>
      <rect x="52" y="64" width="24" height="36" rx="5" fill="{c["band"]}"/>
      {"".join(rack)}
    </g>
    <rect x="6" y="28" width="308" height="22" rx="10" fill="{c["band"]}"/>
    <g fill="{c["vapour"]}" opacity="0.55">
      <ellipse cx="96" cy="22" rx="46" ry="10"/><ellipse cx="200" cy="12" rx="38" ry="9"/><ellipse cx="150" cy="-4" rx="30" ry="7"/>
    </g>
    <rect x="54" y="96" width="62" height="40" rx="8" fill="{c["panel"]}"/>
    <text x="85" y="122" text-anchor="middle" font-family="{MONO}" font-size="13" font-weight="600" fill="{c["screen_text"]}">−160</text>
  </g>"""


def svg(c):
    iso = "".join(
        f'<path d="M0 {yy} H800" stroke="{c["iso"]}" stroke-width="1.2" stroke-dasharray="3 7" opacity="0.7"/>'
        f'<text x="360" y="{yy - 8}" text-anchor="middle" font-family="{MONO}" font-size="12" fill="{c["muted"]}">{label}</text>'
        for yy, label in ((560, "−80 °C"), (742, "−160 °C"))
    )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 1000" preserveAspectRatio="xMidYMax slice" role="img" aria-label="A cold store: a freezer at minus 80 degrees, a liquid nitrogen tank cut away to show its rack of boxes, and a box of positions seen from above.">
  <defs>
    <linearGradient id="wall" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{c["wall_top"]}"/><stop offset="1" stop-color="{c["wall_low"]}"/></linearGradient>
    <linearGradient id="floor" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{c["floor"]}"/><stop offset="1" stop-color="{c["floor_near"]}"/></linearGradient>
    <linearGradient id="steel" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="{c["steel_dark"]}"/><stop offset="0.3" stop-color="{c["steel_lit"]}"/><stop offset="0.62" stop-color="{c["steel"]}"/><stop offset="1" stop-color="{c["steel_dark"]}"/></linearGradient>
    <linearGradient id="vapour" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{c["ice_wash"]}"/><stop offset="1" stop-color="{c["ice_wash_2"]}"/></linearGradient>
  </defs>
  <rect width="800" height="1000" fill="url(#wall)"/>
  {crystal(640, 270, 10, c["crystal"])}
  {iso}
  <rect y="868" width="800" height="132" fill="url(#floor)"/>
  {freezer(70, 372, c)}
  {tank(400, 420, c)}
  {box_card(250, 176, c)}
  {capsule(56, 318, "Freezer", "MF-01", "−80 °C", c)}
  {capsule(520, 380, "Tank", "VP-01", "−160 °C", c)}
</svg>
"""


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for name, colours in PALETTES.items():
        (OUT / f"signin-{name}.svg").write_text(svg(colours))
        print("wrote", OUT / f"signin-{name}.svg")


if __name__ == "__main__":
    main()
