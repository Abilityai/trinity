## 2026-10-09 — pitfall — A kept-alive page keeps its children's timers running, and several watchers on one switch each re-read, one of them for the agent being left
**Context**: trinity-enterprise#754, PR review of #3412. AgentDetail is KeepAlive'd, so its tabs are deactivated, not unmounted, when the user leaves the page or switches agents. Three defects had one root:
- `TasksPanel`'s 5 s `setInterval` had only `onMounted`/`onUnmounted`. It kept polling while the page was away, and the PR's new list re-read rode on it: 12 reads in a minute spent elsewhere.
- SkillsTab watched `agentName`, `agentStatus` and a per-agent "skills changed" tick separately. Switching to an agent in another state fired all three: two list reads and two probes for the next agent.
- The tick watcher ran before the store had switched agents, so its `loadAgentList()` (defaulting to the store's current agent) read the agent being LEFT. A test that counted only the next agent's reads could not see it; counting every read did.
**Lesson**: In a component under a KeepAlive'd page:
(1) pair every interval or subscription opened on mount with `onDeactivated` (stop) and `onActivated` (start). `onUnmounted` never fires while the page is cached.
(2) A switch is one event. Watch the key and the state that changes with it together (a multi-source watch), so the switch is handled once. Make every other watcher that depends on the key ignore the switch (`name === prevName`).
(3) A watcher callback that reads store state through a default argument (`store.load()` with "the current agent") can run before the watcher that moves the store, so pass the key explicitly or skip the switch.
Test each mounted: under a real `<KeepAlive>`, toggle away and advance fake timers; on a switch, assert the full list of reads across all keys, not a count for the new key.
