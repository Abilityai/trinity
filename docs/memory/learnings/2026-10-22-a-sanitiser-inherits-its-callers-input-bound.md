## 2026-10-22 — pitfall — A sanitiser inherits the input bound of every path that calls it; on the event loop, a quadratic pass over unbounded text is a stall anyone can trigger
**Context**: trinity-enterprise#419. The memory envelope's helper (`margin_lines`) was written for an 8,000-character note, then also used by the conversation summarizer to quote chat messages. `PublicChatRequest.message` has no length limit. The summarizer is an `asyncio` task on the backend loop and runs every 5th message for any email-verified visitor. Two passes in the helper were quadratic:
- a delete-to-a-fixed-point marker strip, which removes one nesting level per pass;
- a `pop(0)` blank-line trim.

An adversarial review measured a 7.3 s loop stall from three 200k-character messages. Before the change the summarizer only joined strings, which is linear. Every unit test was green, because the time-budget test used 8,000 characters, the bound of the first caller only.

**Lesson**:
1. When a sanitiser gains a second caller, take that caller's input bound, not the first one's.
2. Clamp untrusted text at the boundary where it enters a CPU pass on the loop.
3. Prefer transforms that are linear by construction. Defanging a marker (`[` → `(`) cannot reassemble one, so one pass is the fixed point. Deleting the marker has to loop.
4. Never `pop(0)` in a loop.
5. Size the time-budget test to the largest input any caller can pass, and run one through the real background path, not only the helper.
