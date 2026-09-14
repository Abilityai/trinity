# Mobile Admin

Standalone mobile-optimized PWA at `/m` for managing agents on the go — check agent status, answer the operator queue, toggle autonomy, and reach the fleet-level controls from a phone.

## How It Works

1. Navigate to `http://localhost/m` on a mobile device (or any browser) and sign in with the admin password. The page manages its own sign-in; it never redirects to the desktop login.
2. Install as a PWA via **Add to Home Screen** for a native app experience.
3. The interface has three tabs:
   - **Agents** -- List agents, start or stop one, toggle autonomy (**AUTO**/**Manual**), open a chat, view logs. Each card carries a success-rate bar; search filters by name.
   - **Ops** -- Two sub-tabs. **Queue** shows the operator queue items awaiting a response (the same items as the desktop [Operations page](../operations/operating-room.md)); **Alerts** shows notifications with an acknowledge button. Tab and sub-tab badges carry the pending counts.
   - **System** -- Fleet health (Total / Running / Stopped / High Context) and the fleet-level actions: **Emergency Stop**, **Fleet Restart**, **Pause Schedules**, **Resume Schedules**. Each confirms in a bottom sheet before running.
4. Each tab refreshes every 15 seconds while it is open, and pull-to-refresh forces one. A refresh that fails keeps what is on screen and says when it was last fetched, with **Retry**.

### Answering the queue

A queue card shows the agent, the priority, the type — **Needs approval**, **Question** or **Heads up** — the title and the question. The controls depend on the type, and they match the desktop page:

- **Needs approval** — tap an option. The card restates what is about to happen (*Sending **Deny** to my-agent — it reads this as your decision on its next run.*), offers an optional note (*Add a note (optional)...*), and only then **Send: Deny** sends it. **Cancel** clears the selection. Nothing is sent on a single tap, and pressing Enter in the note does not send. The tapped option is the decision the agent reads; the note travels alongside it.
- **Question** — type a response and tap **Send**.
- **Heads up** (and any platform heads-up with no decision) — tap **Got it**.

If the send fails on the network, the card keeps your selection and note and shows the error inline so you can retry. If the item was answered, cancelled or expired elsewhere in the meantime, a banner says *This item is no longer pending (already answered, cancelled or expired) — your response was not recorded.* and the card leaves the list.

Every request from `/m` is bounded to 30 seconds, so a request that never answers cannot leave a button disabled for the life of the tab.

There is no desktop equivalent -- this is a dedicated mobile interface.

## For Agents

Mobile Admin uses the same backend API as the desktop UI. No additional endpoints are required. All authenticated API calls work identically from the mobile interface. The respond body it sends is the same as the desktop's — see [Approvals](../automation/approvals.md#for-agents).

## See Also

- [Approvals](../automation/approvals.md) — what a decision means to the agent, and when it acts on it
- [Dashboard](../operations/dashboard.md)
- [Operations Page](../operations/operating-room.md)
- [Managing Agents](../agents/managing-agents.md)
