# Writing page help: the friendly layer (full guide)

The summary of these rules is in the skill's SKILL.md; this file has
the complete guide and its examples.

The Help drawer's four parts and their size limits are in SKILL.md ("Each entry has two layers").

## Tone

- Write for a repository technician or manager in the middle of their work. Use plain words, second person and the
  present tense: "Select **Apply**", not "The Apply control submits the query".
- Say what something does for them, not how it is implemented. Leave out limits like "accepts at
  most 200 characters" unless people hit them; those belong in the reference.
- Use one idea per sentence and avoid parenthetical stacks. Use American English and sentence
  case.
- Use the repository's own terms: specimen, aliquot, custody, hold, disposal, consent. Never use
  internal names such as route names, model fields or CSS classes.
- Never invent anything: every sentence must be traceable to the reference or the source listed in
  `sources` (AGENTS.md rule 1). No policy, thresholds, compliance claims or features that don't
  exist.

## Summary

- Name the record or workflow and what the person can find or do here. Add who can use the page
  only when access is restricted ("Admin and manager only.").
- `purpose` stays required. `summary` is the friendlier version shown in the drawer; when
  `summary` is missing, the drawer falls back to `purpose`.

## Tasks

- Use 2–5 tasks, ordered by how often people do them. The first task is the page's main job.
- A task title is a verb phrase in the user's words: "Find a specimen", "Take it out of storage
  and put it back". It is not a control name.
- Each step is one action. Wrap every exact on-screen label in `[[ ]]`: `Select [[Register specimen]].`
  The drawer shows labels as quiet chips, so the reader can find the same words on the page. Copy
  labels character for character from the template.
- Add an outcome only when it matters: "The specimen becomes Disposed." Leave out keyboard
  details, validation messages and dialog mechanics; the reference covers them.
- A task may end on another page ("Open the specimen and select Request disposal there") when this page is
  where people start it. Take the wording from that page's entry.
- Leave out tasks a role can never perform on this page.

## Good to know

Include something only if it:

- regularly surprises people, for example "Time out of storage is worked out from the custody events, never typed";
- prevents a wrong conclusion, for example "Nominal temperature is a set point, not a measurement";
- explains a block or refusal, for example "A custody transfer cannot be dated before the latest
  custody event"; or
- protects the record, for example "Earlier events are never changed".

Leave everything else in the reference. That covers default page sizes, sort tie-breakers,
character limits, dialog buttons, column lists and anything that reads like a test assertion.
Use 3–5 items; if you have more, the page probably needs a better task instead.

## The reference ("All fields and actions")

- Keep it complete and verified (SKILL.md, "What to explain"). Every item is a `label` /
  `description` pair, which the drawer shows as a definition list in collapsible sections with
  a filter box.
- Write labels exactly as on screen. Descriptions may stay precise and dense, because people reach
  them by searching, not by reading top to bottom.
- Don't repeat the tasks or "Good to know" items word for word; the reference states the rule
  fully, the friendly layer explains why it matters.
