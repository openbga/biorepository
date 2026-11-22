# UI standards

Baseline: 2026-11-10, carried over from the sibling product's platform layer and adapted on 2026-11-12. These rules apply to every page. `tests/test_ui_standards.py` is their executable form.
Code is the reference for how a component is built; this document says
which component does which job and how pages are put together. Basis: a census of every rendered
page, per-area source audits and a review of published design-system guidance (Carbon, PatternFly,
Primer, Cloudscape, GOV.UK, USWDS, Fluent, NN/g, WAI-ARIA APG, Bootstrap 5.3) on that date. Where
the published guidance splits, the rule below is a house decision and says so.

## 1. Library and principles

- Bootstrap 5.3 is the only component library. Controls, dialogs, dropdowns, collapses, tables
  and form widgets are Bootstrap components, themed through the `--vr-*` tokens in
  `static/css/app.css`, which map onto Bootstrap's `--bs-*` variables. DataTables presents grid
  columns, Tom Select enhances selects and vanilla-calendar-pro supplies the date picker
  (`static/vendor/README.md`). Nothing else, and no self-written replacement for something these
  provide.
- One job, one component. Each concept in section 8 has exactly one shared include or one markup
  pattern. A page that needs a variation asks for a parameter on the shared part; it does not
  copy the markup.
- Templates carry no colours, no inline `style`, and no Bootstrap colour utilities that bypass
  the tokens (`bg-white`, `bg-light`, `text-dark`, `text-black`, `table-light`).
- One term per thing. The menu label, the page title, the breadcrumb and the button that
  leads to a page use the same words (section 9).
- The server decides. Hidden controls are a courtesy; every rule here sits on top of server-side
  authorization and validation, never in place of them.

## 2. Page types

Every page is one of these. The type fixes its header, layout and navigation behaviour.

| Type | Purpose | Breadcrumbs | Primary action | Examples |
| --- | --- | --- | --- | --- |
| List | Find and open records | No | `New <thing>` in the header | Specimens, Storage units, Boxes |
| Record | Read one record, start actions on it | Yes | The next step for the record's state, in the header | Specimen, Box, Disposal request |
| Form | Create or edit one record in a dialog | Fallback page only | The submit button, in the form's action row | New storage unit, Edit box |
| Action form | One audited change in a dialog | Fallback page only | The submit button, in the form's action row | Change specimen status, Record check-out |
| Entry grid | Enter many values in a wide, maximizable dialog | Fallback page only | The save button, in a sticky action row | Register aliquots, Store specimens in a box |
| Overview | Work queues and counts that link to filtered lists | No | None, or the one most frequent start | Dashboard, Storage overview |
| Document | A preview of something printed or exported | Yes | The decision about the document, in the header | Label sheet preview, Disposal certificate |

## 3. Navigation

1. The section menus in the top bar own destinations. Dashboard is a top-level link; every other
   list and overview a user can go to is an item in exactly one section menu. A destination
   appears in exactly one navigation component: never in a menu and again as tabs, pills or
   header buttons.
2. Section tabs exist for one case only: one menu item whose content is two to six sibling
   lists that are not themselves menu items (Catalog: Specimen types, Container types, Storage classes), or sibling sub-views of one action (the sections of a specimen correction).
   They are links with their own URLs, rendered by `includes/section_tabs.html` directly under
   the page header, with `aria-current="page"` and no tab roles. One visual style. Tabs stay
   visible on every list in the set and never appear on record or form pages.
3. A long page with four or more sections gets an "On this page" list of anchors
   (`includes/page_toc.html`). It is not a tab set and does not hide content.
4. Every page marks exactly one navigation item as current: the menu item carries
   `aria-current="page"`, and its section shows as current. This includes record, form and
   action pages beneath a list. Pages reached from the user menu (password, two-factor) mark none. The mapping
   from route to menu item is declared in one place and checked by a test (section 11). A
   route shared by two kinds of record (history pages of a box or a rack) is
   declared as shared, and the view names the item for the record at hand.
5. Breadcrumbs appear on every record, form, action, entry and document page, and never on lists
   or overviews. The trail is `Menu item › Record identifier › Action`; the last crumb is the
   current page and is not a link. Under a tabbed section the trail is `Section › Tab › …`, where
   the section is text and the tab links to its list. Rendered by `includes/breadcrumbs.html`.
   There is no separate Back link.
6. Cross-links to another section belong in the content where they are relevant (a box identifier
   in a table cell), not in the page header as a button.
7. Signed-in users keep the application shell on error pages (400, 403, 404).
8. Storage path bar. On pages with a storage context (a record or action about a located thing,
   the dashboard and storage overview, lists of located things), a bar under the top bar shows
   where the thing is, from the site down to the position, with the storage unit's nominal
   temperature; the current level carries `aria-current="location"`. Pages without a storage
   context have no bar. It is wayfinding, not a list of destinations.

## 4. Page header

Top to bottom, all through the blocks of `templates/layouts/document.html`:

| Part | Block | Rule |
| --- | --- | --- |
| Breadcrumbs | `breadcrumbs` | Section 3, rule 5 |
| Title | `page_heading` | List and overview: the menu label. Record: the record's identifier. Form: `New <thing>` or `Edit <identifier>`. Action: the same words as the button that opened it |
| Key facts | `page_meta` | Record pages: status first (section 8), then at most four facts that identify the record. Not used for prose |
| Description | `page_intro` | At most one sentence, under the title. Explanatory text never floats between the header and the first panel |
| Actions | `page_actions` | Right of the title on one row at every width from 992px. Order left to right: destructive (outline danger), secondary actions, primary last. At most one primary and three secondary; anything further goes in a `More` dropdown |

- A button-only action (no input needed) that changes the record's state lives in the header.
  An action that needs input opens a form dialog with its own action row.
- A header button carries an icon only when it creates something (`plus`) or downloads or prints
  a file. No other header icons.
- A list has one primary button on the page: the header's `New <thing>`. The toolbar's `Apply`
  and an empty state's action are secondary.

## 5. Lists

The server sorts, filters and paginates the full authorized queryset; DataTables only shows,
hides and reorders columns. One list is one `section.card.vr-table-card`.

1. Toolbar. One toolbar, `includes/grid_toolbar.html`: search (labelled "Search"), up to three
   everyday filters, `More filters` (a Bootstrap collapse, open when one of its filters is
   active), `Apply`, `Clear all` when anything is active, then `Columns` at the right. Active
   filters show as removable chips under the toolbar. A default value is not an active filter.
2. Columns. Every grid has a `Columns` button. Identifier first
   and locked; status next to the identifier in queues; numbers right-aligned with the unit in
   the header; dates as `YYYY-MM-DD HH:MM` in a `<time>` element; actions last and locked.
3. Opening a record. The identifier in the first column is the link to the record page
   (`th[scope=row] > a.vr-row-link`). It is the only way a row opens: no `Open`, `View` or
   `Details` link, no whole-row click, no `Edit` button standing in for it.
   - The link text is the row's own identifier (the box label on a box, not its storage unit), and
     an identifier always leads to the record it names, on every list where it appears.
   - Where a record has no page of its own (catalog specimen types, container types, storage classes,
     import mappings), the identifier links to the edit form for users who
     may edit, and is plain text for everyone else. House decision: published systems do not
     settle this case, and one rule for every grid is worth more than a second pattern.
   - Queue and count tables on overviews follow the same rule: the label is the link.
4. Row actions. Only for something done to the row that is not opening it. At most one inline
   button (`btn btn-sm btn-outline-secondary`), the next step for that row's state. Two or more
   go in an `Actions` dropdown. A destructive row action is a button that asks for confirmation,
   never a link that posts.
5. Selection. One mechanism, `includes/select_all.html`: a header checkbox bounded to the page,
   a count, and a bulk action bar. Ineligible rows are disabled with a visible reason.
6. Footer. `includes/pagination.html`: count, rows per page (25, 50, 100), pager. Shown even
   when everything fits on one page; replaced by the empty state when there are no rows.
7. States. `includes/empty_state.html` in place of the table: `empty` with the create action for
   users who may create, `no-results` with `Clear filters`, `error` when a filter is invalid. No
   placeholder rows, no bare "None" paragraphs.
8. Several tables on one page. Each is its own table card with an `h2` in the card header, its
   count, and its `Add <thing>` button in that header. The page has no primary button.

## 6. Record pages

1. Order of content: header; alerts that need action; facts; related tables; history last.
2. Facts are `dl.vr-facts` inside a panel with an `h2`. Never a Bootstrap grid of labels, never
   a two-column table.
3. Each related list is a table card following section 5, with its `Add <thing>` button in the
   card header. A related record opens through its identifier link.
4. History is one component, `includes/history_item.html`, newest last, in the final panel:
   what happened, when, who, and the reason where one was required.
5. A side column (from 1200px) may hold the context and launch button for the most frequent
   record action. Its editing fields open in the shared form dialog. No panel inside a panel.
6. `Edit` is a secondary header button for records that can be edited freely. A locked record
   is changed through `Correct <thing>`, which requires a reason and is recorded in history.

## 7. Forms

House decision, approved 2026-11-10: use one Bootstrap modal system for editing, including
record-local action forms. A drawer would split equivalent workflows into two interaction
patterns. The record or list stays behind the dialog; long forms and entry grids can be maximized.
Small screens use the full screen. Normal dialogs have comfortable outer and inner margins;
the heading and close controls remain visible while the body scrolls. Controls use the Soft
paper treatment: lightly tinted fields, muted boundaries, modest rounding and quiet shadows,
while preserving graph paper, the margin rule and the menu marker. (The paper treatment is
superseded by the Cryo visual language, section 12.)

Use one dialog at a time. Move focus inside on open, retain keyboard focus there, and return it
to the opener on close. Escape and Cancel close clean forms; unsaved input offers Keep editing
or Discard changes. Loading, failed requests and validation have visible states. Validation
keeps entered values and focuses the first invalid field. Save follows the existing server
destination; multi-step forms and Add another remain in the dialog. Direct form URLs open the
same dialog, with a server-rendered page as the no-JavaScript fallback. Authentication,
authorization, CSRF, concurrency and domain validation stay on the server.

This follows [Bootstrap's scrollable and fullscreen modal patterns](https://getbootstrap.com/docs/5.3/components/modal/)
and [WAI-ARIA dialog keyboard and focus guidance](https://www.w3.org/WAI/ARIA/apg/patterns/dialog-modal/),
reviewed 2026-11-10. Choosing dialogs for every editing task is this application's consistency
decision, not a claim that all applications should do so. Read-only record pages, review queues,
label previews, search/filter controls and account sign-in remain ordinary workspaces.

1. Rendering. Django forms through the project renderer (`{{ form }}`, `as_field_group`,
   `includes/field.html`). No hand-written inputs where a form field exists.
2. Structure. `form.vr-form` containing one or more `fieldset.vr-form-section`, each with a
   legend when there is more than one. One column; two columns from 768px only for short related
   pairs; notes full width. Field width follows the expected value.
3. Required fields carry `*` after the label, and the form says "Fields marked * are required."
   once at the top. House decision; the published systems split on this.
4. Errors. `includes/form_errors.html` at the top with `role="alert"`, the message under each
   field, focus on the first invalid field, and every entered value kept.
5. Action row. `div.vr-form-actions`, right-aligned and sticky at the bottom of the viewport:
   destructive action first (`btn-outline-danger`), then `Cancel` (`btn btn-link`), then the one
   primary button last. House decision; the published systems split and consistency is what
   matters. Dialog action rows stick to the bottom of their scrollable body. Dropdown and
   sign-in action rows remain in normal flow.
6. Destinations. After saving, the record page; where the record has no page, the list. `Cancel`
   returns to the page the user came from: the record when editing, the list when creating.
7. Entry grids follow rules 4 to 6 and are never DataTables-enhanced: fixed columns, units in
   the header, tab order left to right then down, row-level messages under the row.
8. Child rows (aliquots, box positions) use one pattern: a row per item, an `Add <thing>`
   button under the rows, and a `Remove` control on each row that works without JavaScript.

## 8. Components

| Job | The one way to do it |
| --- | --- |
| Status of a record | `includes/status.html` (text, glyph, tone); the mapping from a domain state to one of seven tones lives in one small partial per app |
| Notice inside a page | Bootstrap `.alert` in the tone's variant, opening with `includes/status.html` in its `plain` form; `role="alert"` when it blocks the task, otherwise `role="status"` |
| Result of the last action | Django message at the top of the next page (`includes/messages.html`); it stays until dismissed or the next navigation. No toasts |
| Breadcrumbs | `includes/breadcrumbs.html` |
| Section tabs | `includes/section_tabs.html` |
| On-this-page anchors | `includes/page_toc.html` |
| List toolbar, chips, columns | `includes/grid_toolbar.html`, `filter_chips.html`, `grid_columns_button.html` |
| Sortable header | `includes/sort_header.html` |
| Pagination and count | `includes/pagination.html` |
| Empty, no results, filter error | `includes/empty_state.html` |
| Row selection | `includes/select_all.html` |
| Facts | `dl.vr-facts` |
| History | `includes/history_item.html` |
| Form error summary | `includes/form_errors.html` |
| Disclosure of filters | Bootstrap collapse, through the toolbar include |
| Disclosure of a note or long value | `<details>` |
| Menu of actions | Bootstrap dropdown |
| Editing or an action requiring input | Shared maximizable Bootstrap form dialog, with the object named |
| Confirmation of a destructive action | Compact Bootstrap confirmation dialog for a single yes or no; shared form dialog when input is required |
| Brand mark and name | `includes/brand.html` |
| Icon | `includes/icon.html` |

Buttons:

| Role | Classes | Where |
| --- | --- | --- |
| Primary | `btn btn-primary` | One per page or form |
| Secondary | `btn btn-outline-secondary` | Header, toolbar, card header |
| Quiet | `btn btn-link` | `Cancel`, `Clear all` |
| Destructive | `btn btn-outline-danger` | Header or action row, first in the row |
| Row | add `btn-sm` | Table cells and card headers only |

## 9. Wording

| Meaning | Words | Notes |
| --- | --- | --- |
| Create a top-level record | `New <thing>` | List header. The form's title is the same; its submit is `Create <thing>` |
| Attach a child to the open record | `Add <thing>` | Card header or under the child rows |
| Log something that happened | `Record <event>` | Check-outs, check-ins, moves, disposal |
| Take specimens in | `Register <thing>` | The repository's verb for a new specimen record |
| Change a freely editable record | `Edit` in the header; `Edit <identifier>` as the title; `Save changes` to submit | |
| Change a locked record with a reason | `Correct <thing>` | Title and submit use the same words |
| Move a record to another state | `Change <state>`, or the state's own verb (`Approve disposal`, `Place hold`, `Release hold`) | Not `Update` |
| Leave a form without saving | `Cancel` | Never used alone for anything else |
| End or void a record | Verb plus object (`Cancel disposal request`, `Delete draft`) | A destructive label always names its object |
| Detach a child | `Remove` | |
| Apply filters / reset them | `Apply` / `Clear all` | |

Sentence case everywhere. A title, its breadcrumb, its menu item and the button that opens
it use the same words. Every page uses the same term for the same thing.

## 10. Roles and states

- A control the user's role can never use is not rendered. A control that is unavailable only
  in the record's current state is rendered disabled with the reason in text beside it.
- Approval pages show the user's own request as read-only with one sentence, the same wherever it
  occurs: "You requested this. Another person with disposal approval must approve it."
- Every page has its states: empty, no results, invalid filter, validation error, denied and
  read-only. Loading states apply only where a request updates part of a page.

## 11. Enforcement

- `tests/test_ui_standards.py` scans every template for the patterns this document retires:
  colour literals and inline styles, banned utilities, pill navigation, hand-written pagination,
  status spans, breadcrumbs and toolbars outside the shared includes, `Open` and `View` row
  links, more than one primary button in a page header.
- `tests/test_navigation.py` resolves every HTML route and asserts that it marks exactly one
  menu item (or is declared as an account page), and that the item's label equals
  the list page's title.
- The browser suite keeps one journey per page type and the contrast check for both themes.

## 12. Visual language

Cryo, chosen on 2026-11-18 from three design directions. The
platform layer (shell structure, components, controls, help drawer, panel spacing) stays shared
with Environmental LIMS; only the visual language below differs. `static/css/app.css` holds the
one definition of each token and component for light, dark and print; this section states the
intent and the rules. `tests/test_css_tokens.py` is their executable form.

**Colour has two meanings.** On clear cold neutrals, ice means in the cold, the primary action,
links and where you are; warm means time out of storage and nothing else (warnings keep their
amber tone). No other colour is decoration.

| Role | Tokens | Used for |
| --- | --- | --- |
| Ground and surfaces | `--vr-paper`, `--vr-surface`, `--vr-surface-2`, `--vr-surface-hover`, `--vr-raised`, `--vr-pathbar-bg` | The cold ground; panels, the top bar and the record header; tonal insets (table heads, stations, dialog action row); hover; menus and dialogs; the storage path bar |
| Lines | `--vr-border`, `--vr-border-strong` | Row separators and bar edges; control boundaries (3:1 on every surface). Panels have no outline |
| Text | `--vr-text`, `--vr-text-muted`, `--vr-text-faint` | Text; labels and help; codes after their words |
| Ice | `--vr-ink` = `--vr-pen`, `--vr-cold-*`, `--vr-ln2`, `--vr-focus`, `--vr-selection` | Primary button, links, focus; current menu section and item, selected rows, chips, the bulk bar; storage temperatures and the cold band of the journey |
| Warm | `--vr-warm`, `--vr-warm-text`, `--vr-warm-bg`, `--vr-warm-border` | Time out of storage only: the running clock, "Checked out · 7 min", the Check out station |
| Status tones | `--vr-{neutral,info,progress,success,warning,danger,hold}-{text,border,bg}` | Statuses and notices only |

**Type.** Two voices, both self-hosted (OFL). Geologica for everything read: body, labels,
headings, buttons, menus; sentence case, no uppercase labels. Martian Mono, narrowed, for the
datum voice only: identifiers, figures, dates and times, storage paths and units (its zero is
slashed, so 0/O stay distinct). A sentence ("Checked out", "2 hours ago") is never in the datum
voice.

**Shape.** Rounded, separated by tone and soft shadow, never by outlines: panels and dialogs 22px,
menus and notices 14px, fields 10px; buttons, path segments, statuses, counts, chips and the pager
are capsules. Panel content sits `--vr-panel-inset` from the edge and stacked blocks are
`--vr-stack` apart (the shared panel spacing system).

**Devices, and when each is used.**

- Top bar: brand, the section menus (the destinations, section 3), the specimen lookup ("Scan or
  type ID") and the tools, on a light surface. Below 992px the menus open as an offcanvas list.
- Storage path bar: under the top bar, only on pages with a storage context (section 3, rule 8).
  The storage unit's segment is ice with its temperature; the current level is filled ice; a
  position freed by a check-out is dashed; an open clock shows as a warm capsule at the end. On
  the dashboard and storage overview the bar is the cold census of every unit.
- Status: an outlined capsule, tone outline plus icon plus words (`includes/status.html`). The
  record's own status, first in the header's key facts, is the large capsule with its tone wash;
  inside a notice the status is plain.
- Clock: time out of storage as a warm capsule with a stopwatch (`{% clock %}`).
- Specimen journey: the dashboard draws the work queues as a temperature profile (room →
  storage → room when checked out → dry ice or disposal) with five stations; the Check out station
  is the region's one emphasis.
- Custody timeline and history: round nodes on a rail; cold events in ice, out-of-storage events
  in warm, others neutral; oldest first.
- Box position map: a recessed tray; wells seen from above. Occupied is an ice cap with a frost
  ring, held (an active hold) the same in the hold tone, free an open ring whose label shows on
  hover and focus, blocked a hatched danger ring; a key counts each state and hidden text names it
  per cell.
- Mark: an ice tile with a six-point crystal and a vial cap (`static/img/mark.svg`).
- Record foot line (`includes/record_foot.html`): the record's identifier, and when and by whom
  the view was produced, under the last panel and on its printout. It is a viewing note, not a
  statement about the record.
- Application footer (`includes/source_footer.html`): product name, the "Source code" link
  (`SOURCE_CODE_URL`, required by AGPL-3.0 section 13) and the licence, on every staff,
  entrance, error and admin page. Keep it; it is hidden only in form dialogs and on printouts.
- Dateline: today's date and ISO week under an overview's title (`includes/dateline.html`).

Dates and times in these devices are rendered by the server in the repository's time zone
(`TIME_ZONE`), never the browser's.

**Dark mode** is the same cold world at night: deep blue-black ground, slightly lighter panels,
bright ice for actions and current, a soft warm for time out of storage.

**Print** always uses the light palette whatever the screen mode, with no top bar, path bar,
navigation, toolbars or buttons; statuses keep their outlines and wells keep fill and ring.

**Rules.** Text and its background meet WCAG 2.2 AA (4.5:1; small labels and statuses count as
small text) and control boundaries, focus rings and state indicators 3:1, in both modes and in
print. Focus is a 3px ice outline outside the control (fields: the field outline turns ice with a
soft ring). Motion is limited to short colour and transform transitions and stops under
`prefers-reduced-motion`. Templates never carry colours: a new need is a token or a component rule
in `app.css`. Any chart library added later reads the tokens, so charts follow the mode.
