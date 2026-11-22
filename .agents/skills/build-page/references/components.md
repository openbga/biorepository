# Component and control reference

Source of truth: `COMPONENTS` in `core/templatetags/ui.py`, and the header comment of each
`templates/includes/<name>.html`, which documents its parameters, parts and an example. Read
that comment before using a tag. This table tells you which tag does which job (ui-standards §8).

## Contents

1. Components by job
2. Form controls
3. List columns and preferences
4. Buttons

## 1. Components by job (`{% load ui %}`)

Block tags end with `{% end<name> %}`. Parts are filled with `{% fill <part> %}…{% endfill %}`.

| Job | Tag | Notes |
| --- | --- | --- |
| Titled section or table card | `panel` (block) | `title`, `count`, `table=True` (flush table card), `hidden_title` (a list page's single card), `collapsible`/`open`, `class` for spacing between panels; parts `title`, `actions`, `count`, `footer` |
| Sub-block inside a panel body | `panel_section` (block) | Keeps the panel's inset and stack; `rule` for a separator |
| List body and its states | `list_results` (block) | Table and pagination, or the empty, no-results or filter-error state; `empty_title`, `action_url`/`action_label` for the create action |
| Notice inside a page | `notice` (block) | `tone` neutral, info, progress, success, warning, danger or hold; `blocking=True` gives role="alert" |
| Key facts under the title | `page_meta` (block) | Body is `<li>` items: status first, at most four facts |
| Header "More" menu | `more_actions` (block) | Beyond one primary and three secondary buttons; body is dropdown `<li>`s |
| Record layout | `record_layout` (block) | Parts `lead` (first panel) and `aside` (launch panel); body is the full-width panels |
| One fact row | `fact` | Inside `<dl class="vr-facts">`; blank shows "—" or `empty`; `wide` for a long phrase |
| Form page or dialog body | `form_page` (block) | Errors, required note, sections, action row; `cancel_url`, `submit_label`, `destructive`; `local=True` for an action form inside a record panel |
| Form action row only | `form_actions` (block) | Body holds extra buttons |
| History | `history_panel` (block), `history_item` | Newest last, final panel; item has `title`, `at`, `actor`, `reason`, `changes` |
| Status of a record | `status` | `label`, `tone`, `plain`: an outlined capsule with icon and word; the per-app state-to-tone mapping lives in a small partial |
| Dated obligation | `due_pill` | `due`, `soon_days` (default 30), `plain` in cells; rule in `core.due.due_state` |
| Time out of storage | `clock` | The warm running clock (`span.vr-clock[data-state]`) |
| Icon | `icon` | `name` = a Bootstrap Icons name present in `static/img/icons.svg` |
| Breadcrumbs | `breadcrumbs` | Records, forms, actions and documents only |
| On-this-page anchors | `page_toc` | Pages with four or more sections |
| One-button POST action | `post_button` | `action`, `label`, `variant`, `fields` |
| Destructive yes/no | `confirm_dialog` | Compact confirmation; use the form dialog when input is needed |
| Empty, no results, invalid filter | `empty_state` | Through `list_results` on lists |
| Toolbar, chips, columns | `grid_toolbar`, `filter_chips`, `grid_columns_button`, `grid_search` | One toolbar per list; `grid` = the table's `data-vr-grid` |
| Sortable column header | `sort_header` | `col`, `locked`, `hidden`, `priority="low"` |
| Pagination | `pagination` | Count, rows per page (25/50/100), pager |
| Row selection | `select_all`, `row_select` | Page-bounded; disabled rows show the reason |
| Form pieces | `form_errors`, `form_sections`, `required_note`, `field` | Usually via `form_page` |
| Brand and footers | `brand`, `record_foot`, `source_footer` | Shell and page foot |

Plain `{% include %}` stays for partials that are not registered tags: `includes/messages.html`,
`includes/form_dialog.html`, `includes/grid_columns_dialog.html`, the shell includes (navigation,
theme and user menus, dateline, entrance scene) and app-local partials such as
`storage/_path.html`. Section tabs are rendered by the navigation `{% section_tabs %}` tag.

## 2. Form controls (ui-standards §8)

Plain Django widgets, rendered by the templates in `templates/django/forms/widgets/`
(`input.html` dispatches) and initialised by `static/js/controls.js`. There is no project widget
module except `core.lookups.RemoteSelect`.

| Field | Use | Never |
| --- | --- | --- |
| Choice (few options) | `forms.ChoiceField` / `ModelChoiceField`: Tom Select via the select widget template | A raw `<select>` in a template |
| Large or growing model choice | `ModelChoiceField(queryset=…, widget=core.lookups.RemoteSelect("<lookup>"))` with `register(Lookup("<name>", staff(lambda request: …), …))` in `core/lookups.py` (one route, `lookups/<name>/`) | Rendering every option inline; a lookup queryset that skips the page's authorization |
| Multiple choice | `RemoteSelectMultiple` or `SelectMultiple` (Tom Select chips); short lists `CheckboxSelectMultiple` | |
| Date | `forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")` (the apps' `DATE` constants): rendered as the shared date field (`data-vr-date`), repository local time | The browser's date popup |
| Date and time | `forms.DateTimeInput(attrs={"type": "datetime-local"}, format=…)` (the apps' `DATETIME` constants) | `datetime-local` popups |
| Number | Any `NumberInput`/`DecimalField`/`IntegerField` renders as text (inputmode only when min ≥ 0) so the exact string is kept; unit via `attrs={"unit": "°C"}`, an addon read with the field | `type="number"`, spinners, float conversion, a unit in the label |
| File | `FileInput`/`ClearableFileInput` becomes the drop zone (`data-vr-file`) | A bare file input |
| Checkbox, radio, switch | Bootstrap `form-check` through the widget | |

Every form is `novalidate`. The server's messages are the only errors. `tests/test_controls.py`
checks page templates for native controls and the rendering of number, unit, date and file
fields; `e2e/test_controls.py` covers their behaviour.

## 3. List columns and preferences

- Table: `<table class="table table-hover vr-table" data-vr-grid="<app>.<list>" data-vr-grid-version="1">`.
- Headers: `{% sort_header label=… col="<id>" … %}` with `locked=True` on the identifier and
  actions columns, and `priority="low"` on columns that can hide at narrower widths.
- The first cell is `<th scope="row"><a class="vr-row-link" href="…">identifier</a></th>`.
  Identifiers, dates, figures and statuses don't wrap. Numbers are right-aligned with the unit
  in the header. Dates go in `<time>`. A location cell is `td.vr-id.vr-path` via
  `storage/_path.html`.
- Saved per user on the server (`core/preferences.py`): `grid:<grid>:v<version>` holds column
  order and hidden columns; page size and sort are kept by its middleware. A saved value that no
  longer fits is ignored, so bump `data-vr-grid-version` when column ids change. Preferences are
  presentation only and never widen what a user may see.

## 4. Buttons

| Role | Classes | Where |
| --- | --- | --- |
| Primary | `btn btn-primary` (ice) | One per page or form |
| Secondary | `btn btn-outline-secondary` | Header, toolbar, card header |
| Quiet | `btn btn-link` | Cancel, Clear all |
| Destructive | `btn btn-outline-danger` | First in its row; the label names its object |
| Row | add `btn-sm` | Table cells and card headers only |

Buttons are pills. Header buttons carry an icon only to create (`plus-lg`) or to download or print.
