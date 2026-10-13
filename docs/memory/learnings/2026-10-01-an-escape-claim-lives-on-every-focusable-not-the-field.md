## 2026-10-01 — pitfall — "Esc drops it first" must be claimed wherever focus can be, not only in the field that set it

**Context**

trinity-enterprise#738, PR #3168 review, `PortalConversation.vue` + `PortalReplyChip.vue`.

The ruling was "innermost first": typeahead, then the reply chip, then the running turn. It was built as a `drop-reply` branch in the textarea's `@keydown` (via `resolveComposerKey`), with `preventDefault()` so the document-level turn-cancel would yield. Every test pressed Escape in the textarea, and all of them passed.

An independent test audit tabbed to the chip's own × and pressed Escape. The keystroke never touched the textarea. It bubbled straight to `onEscapeKeydown` and stopped a running turn, and the chip stayed on screen. That is the opposite of the ruling, on the one control a keyboard user reaches the chip through.

This is the 2026-09-07 class (an overlay must claim Escape for the dialogs it raises too). There, the overlay forgot the dialogs it raised. Here, the thing that owns Escape forgot its own focusable child.

**Lesson**

- An Escape ownership rule belongs to the element that is on screen, not to the input that happened to create it.
- Before shipping, list every element that can hold focus while it shows (its own buttons, the field, sibling controls). For each one, ask which handler sees Escape first.
- The cheapest durable shape is a `@keydown.esc` on the owner's root. It calls `preventDefault` so `ownsEscape` yields, and checks `defaultPrevented` / `isComposing` first. Descendants are covered by bubbling.
- Pin it with a test that moves focus to the non-field control (`el.element.focus()`) and asserts the outcome, plus a positive control that the next Escape still reaches the turn.
