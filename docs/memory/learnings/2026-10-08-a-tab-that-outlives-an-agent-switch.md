## 2026-10-08 — pitfall — A tab that outlives an agent switch carries its own state, every async answer and its "loaded" flag across, unless each is keyed to the agent
**Context**: trinity-enterprise#754 (the merged Skills tab, review round). AgentDetail is KeepAlive'd and the new tab was visible to everyone, so the same `SkillsTab` instance survived every switch. Three leaks surfaced:
- The component's own verb state followed the page: the outcome line, card errors, dialogs, and a run still starting. A late Run answer opened the NEW agent's Tasks tab on the old agent's execution id.
- `stores/skills.js` reads (`load`, `inject`, set writes) applied whatever answered last. A slow load for agent A overwrote B's assignments, so B's Unassign could PUT A's list onto B. The 10-07 fragment covered only a write's follow-up reload.
- "Loaded" was the platform-wide `libraryStatus !== null`, which survives the switch. B therefore read "No shared skills yet", with an Assign draft built from an empty list.
**Lesson**: When a surface becomes reachable across keys (a tab now shown to everyone, a KeepAlive'd view), audit three things before merging.
(1) Every component-local `ref` that describes the last verb resets on the key change.
(2) Every await in a key-scoped store, read or write, is followed by a check that the key it was asked for is still current. A read also checks it is still the newest read (a sequence number); the per-key busy flags reset with the key.
(3) "Has this key's data answered?" is a per-key flag reset on the switch, never a shared or platform-wide value that happens to be non-null.
Test each mounted: start the call, `setProps` to another key, resolve, and assert nothing landed.
