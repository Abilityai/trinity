## 2026-10-01 — pitfall — A focus-return test passes vacuously when a synthetic click never moved focus

**Context**

trinity-enterprise#738, `portalInChatReply.mount.spec.js`. The composer's reply chip × must hand the caret back to the textarea (`dropComposerReply` → `focusComposer()`).

The first version of the test did three things: clicked Reply (which focuses the textarea), clicked × with `trigger('click')`, and asserted `document.activeElement === textarea`.

The call-site mutation that deletes `focusComposer()` from the handler left the test **green**. A synthetic `click` event does not move focus the way a real pointer press does, so the caret never left the textarea. The assertion was true before the handler ran and stayed true without it.

Seventeen sibling mutations in the same battery went red. This one only showed up because every wiring line was mutated, not just the ones that looked risky.

**Lesson**

- A focus-*return* (or focus-*restore*) assertion proves nothing unless focus is provably somewhere else first. Put it there by hand (`trigger.element.focus()`), assert it moved (`expect(document.activeElement).toBe(trigger.element)`), then act.
- The same applies to any "X ends up focused" check after a `trigger('click')` / `trigger('keydown')` in jsdom. VTU dispatches the event and nothing else: no default focus change, no pointer sequence.
- Treat a green focus assertion with suspicion until the handler's focus call has been mutated out and the test went red.
