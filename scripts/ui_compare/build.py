"""Build .local/shots/index.html: before/after pairs by role, page key, width and theme.

capture.py runs this at the end of each run; rebuild by hand with
    python3 scripts/ui_compare/build.py
"""

import html
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
SHOTS = HERE.parents[1] / ".local" / "shots"
SIDES = ("before", "after")


def main() -> None:
    manifests = {
        side: json.loads((SHOTS / side / "manifest.json").read_text())
        for side in SIDES
        if (SHOTS / side / "manifest.json").exists()
    }
    pages: dict[str, dict[str, dict]] = {}
    for side, manifest in manifests.items():
        for role in manifest["roles"]:
            entries = pages.setdefault(role["account"], {})
            for item in role["pages"] + role["dialogs"]:
                entry = entries.setdefault(
                    item["key"], {"key": item["key"], "path": item["path"], "sides": []}
                )
                if "from" in item:
                    entry["from"] = item["from"]
                if any((SHOTS / side / role["account"]).glob(f"{item['key']}--*.png")):
                    entry["sides"].append(side)
    data = {
        "roles": list(pages),
        "pages": {role: list(entries.values()) for role, entries in pages.items()},
    }
    widths = sorted(
        {str(w) for m in manifests.values() for w in m["widths"] + [m["narrow"]]}, key=int
    )
    gaps = [f"{side}: {e}" for side, m in manifests.items() for e in m["errors"]]
    summary = " · ".join(
        f"{side} {m['captured']}, {m['shots']} screenshots" for side, m in manifests.items()
    )
    seg = lambda opt, values: (  # noqa: E731
        f'<span class="seg" data-opt="{opt}">'
        + "".join(
            f'<button data-v="{html.escape(v)}">{html.escape(label)}</button>'
            for v, label in values
        )
        + "</span>"
    )
    toolbar = (
        '<div class="top" role="toolbar" aria-label="View options">'
        "<strong>OpenBGA Biorepository · UI before / after</strong>"
        + seg("role", [(r, r.removeprefix("demo_")) for r in data["roles"]])
        + seg("width", [(w, w) for w in widths])
        + seg("theme", [("light", "Light"), ("dark", "Dark")])
        + seg("mode", [("slider", "Slider"), ("side", "Side by side")])
        + '<input id="filter" type="search" placeholder="Filter pages" aria-label="Filter pages">'
        "</div>"
    )
    gap_html = (
        '<h3 class="errors">Capture gaps</h3><ul class="errors">'
        + "".join(f"<li>{html.escape(g)}</li>" for g in gaps)
        + "</ul>"
        if gaps
        else ""
    )
    doc = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>OpenBGA Biorepository · UI before / after</title>"
        f"<style>{(HERE / 'viewer.css').read_text()}</style></head><body>{toolbar}<main>"
        '<div class="intro"><h1>UI before / after</h1>'
        f'<p class="muted">{html.escape(summary)}. 768 is captured for pages reached from the '
        "navigation only; dialogs at 1280 and 1440.</p>"
        f'{gap_html}<nav class="toc" id="toc" aria-label="Pages"></nav></div>'
        '<div id="pages"></div></main>'
        f"<script>const DATA = {json.dumps(data)};\n{(HERE / 'viewer.js').read_text()}</script>"
        "</body></html>\n"
    )
    (SHOTS / "index.html").write_text(doc)
    print(SHOTS / "index.html")


if __name__ == "__main__":
    main()
