---
name: example-agent
description: For someone new to agents who skipped the AI audit - build one tiny, real agent on their instance as a test so they can see what an agent is, then explain what they just saw
user-invocable: true
---

# Example agent

This is the test agent for someone who skipped the audit. The point is understanding, not usefulness. In five minutes they should be able to say what an agent is in their own words.

1. Say what's about to happen in two sentences: you will build a very small agent, on their real instance, and it needs no accounts or passwords.
2. Pick one from what you know about their job, and ask only "does this sound okay?". It must need no credentials and no connections. For example, one that turns rough notes into a clean weekly update in their company's tone, or one that drafts a reply to a pasted customer email.
3. Add it to `agents` with `kind: "example"`, `status: "designing"`, its one or two skills as `planned`, and `focus.agent` set. Set `stage: "build"`.
4. Build it with `/build-agent`, keeping the world updated.
5. When `live_name` is confirmed, tell them to open it from the world and try it. Wait for them.
6. Then explain what they saw using this agent as the example: its instructions (who it is), its skills (what it knows how to do), its memory (what it keeps), and what it could connect to if it had keys. Ask what they would want one to do at work.
7. Offer `/company-audit` as the way to find the agent that would actually save them time.
