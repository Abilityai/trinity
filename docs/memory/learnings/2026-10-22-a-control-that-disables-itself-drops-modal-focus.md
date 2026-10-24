## 2026-10-22 — pitfall — A control that is disabled or removed while it holds focus drops focus out of the modal, and per-verb fixes miss most of the triggers
**Context**: trinity-enterprise#539, `components/skills/SkillAssignModal.vue`. Several controls in this dialog can disable or remove themselves while they hold focus:
- Select all is left with nothing to add and Deselect all with nothing to remove, so each disables itself on every click.
- Reset is `v-if="dirty"`, so it unmounts.
- Save is disabled while it is `loading`.
- The confirm's buttons leave with its overlay.

A focused control that becomes disabled or leaves the DOM drops focus to `<body>`, outside the overlay that owns `BaseModal`'s `@keydown`: Esc stops closing the dialog and Tab is no longer trapped.

The first fix was one handoff per verb. Review found the triggers it missed:
- A manual tick: Safari and Firefox on macOS do not move focus on a mouse click, so focus stays on Select all.
- Any save, which disables both bulk buttons.
- Confirm, after which the confirm hands focus back to a Save that the save has just disabled.

That first version also called `nextTick(cb)` before writing the state. With no flush pending, `nextTick` resolves on the already-resolved promise, so `cb` ran before the render and still read the old DOM. A re-read found it, and a mutation proved the test caught that ordering.

**Lesson**:
1. Re-home focus in ONE `flush: 'post'` watcher over every state that can disable or remove a control. Do not patch it per verb.
2. Treat focus as lost in two cases: `document.activeElement` is a disabled control inside the dialog, or focus is on `<body>` and a control inside the dialog held it last.
3. Track that last control with a `focusin` listener on the overlay, and reset it when the dialog opens.
4. Send focus to the control that undoes the lost one, else to a safe control that is always present (Close).
5. Skip the watcher while a nested confirm is open, because focus is legitimately in it.
6. If you do schedule with `nextTick`, call it after the reactive write, never before.
