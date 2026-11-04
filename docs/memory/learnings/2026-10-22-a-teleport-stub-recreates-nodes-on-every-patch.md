## 2026-10-22 — pitfall — `stubs: { teleport: true }` re-creates the stubbed subtree on every patch, so a test of what happens to an element "in place" passes for the wrong reason
**Context**: trinity-enterprise#539, `src/frontend/tests/unit/skillAssignSelectAll.spec.js`. The mount harness copied from `skillAssignDraftSurvivesSync.spec.js` stubs Teleport inline. Under that stub, every patch replaced the dialog's nodes, so a button that a click disables was re-created instead of being disabled in place.

The focus-handoff code had two branches: "the control is gone" and "the control is disabled". The tests only ever took the first. A mutation that broke the in-place branch, which is the real browser path, left every test green. With the real Teleport it turned two tests red. An independent review found this, and a node-identity assertion (`expect(find(...).element).toBe(before)`) reproduced it: it fails under the stub and passes with the real Teleport.

**Lesson**: A test that reasons about a DOM node's identity or its in-place state must mount with the real Teleport. That covers focus on a control that gets disabled, a node kept across a re-render, and scroll or selection kept on an element.
- Query through `new DOMWrapper(document.body)`, because teleported content is not under the wrapper.
- Use `enableAutoUnmount(afterEach)`, so dialogs from earlier tests are not left in `<body>`.
- Pin the premise with one identity assertion before the behaviour assertion.

The stub is fine for tests that only read state, such as whether a box is checked or what text is shown.
