---
paths:
  - "templates/**"
  - "*/templates/**"
  - "static/css/**"
  - "static/js/**"
  - "*/static/**"
  - "core/templatetags/**"
  - "*/forms.py"
  - "*/*_forms.py"
---

# Frontend rules

Full standard: `docs/ui-standards.md`; the working order is the `build-page` skill. Enforced by
`tests/test_ui_standards.py`, `tests/test_include_isolation.py`, `tests/test_controls.py`,
`tests/test_icons.py` and `tests/test_form_dialog.py`.

- Build pages only from the shared components: `{% load ui %}` tags registered in `COMPONENTS`
  in `core/templatetags/ui.py` (`panel`, `list_results`, `notice`, `form_page`, `history_panel`,
  `fact`, `page_meta`, `status`, …). Read the header comment of `templates/includes/<name>.html`
  before using a tag. Never `{% include %}` a registered component or copy its markup; add a
  parameter instead.
- Colours, spacing, type, radius, shadow and z-index come only from `--vr-*` tokens in the Tokens
  section of `static/css/app.css` (the Cryo palette). No hex/rgb/hsl, raw px/rem, inline `style`
  or untokened Bootstrap colour utilities. Ice is the primary action, links and the current
  place; warm marks time out of storage only; status is an outlined capsule with icon and word.
- Icons are Bootstrap Icons only (pinned version in `static/vendor/README.md`), copied verbatim
  into `static/img/icons.svg` as `i-<name>`, rendered through `{% icon %}` or `vrUi.icon`. Never
  draw or invent an SVG, glyph or CSS shape for an icon. Exceptions: the brand mark, the sign-in
  art and diagram graphics.
- Controls are Bootstrap form components through the Django widget templates in
  `templates/django/forms/widgets/`. Never render a browser-default control (native select menu,
  date/time popup, number spinner, datalist, validation bubble, title tooltip). Large model
  lists use `core.lookups.RemoteSelect` with a registered, permission-scoped lookup. Numbers are
  text inputs with the unit on the widget.
- Every form opens in the shared form dialog: register POST routes in `FORM_SURFACES`
  (`core/form_surfaces.py`) or list the exception in `NOT_FORM_PAGES`.
- Validation is server-side in the Django form; errors render inline through the form
  components. No toasts. One primary action per header, the rest in `More`. Codes shown in words.
- When a page's visible content, actions or fields change, update its help
  (`.agents/skills/document-page`).
