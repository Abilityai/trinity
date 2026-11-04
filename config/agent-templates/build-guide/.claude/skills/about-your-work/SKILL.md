---
name: about-your-work
description: A short talk, about 2 minutes, about what the person's own job involves day to day, so their profile is complete without the full AI audit
user-invocable: true
---

# About your work

Trinity should know at least who this person is and what their company does, even when they skip the audit. The setup questions and the company research give the company. This talk gives their own job. It takes about 2 minutes, so keep it short.

Read `profile.md` and `company-research.md` first. Don't ask what you already know.

Ask one question at a time, at their level from `profile.md`, and wait for the answer:

1. **A normal week.** What does a normal week in their job involve?
2. **What repeats.** Which jobs come back every week, or eat the most time?
3. **The first thing to hand over.** If an agent took one of those off their hands, which would they pick first?

Follow up only when an answer is too vague to use. Three or four questions in all is enough.

Never ask for passwords, keys or tokens. If they paste one, tell them to change it and don't repeat it.

## Afterwards

1. Add a "Their work" section to `profile.md`: what their week involves, what repeats or takes the most time, and what they'd hand to an agent first, in their words.
2. Update `person` on the `build` canvas with what you learned, and set `profile_complete` only when the rule in `CLAUDE.md` holds: they answered the setup questions, you have read `company-research.md` or they gave no website, and you now know what their job involves. If the research hasn't finished yet, leave it false and set it on the turn you first read `company-research.md`. If the research couldn't run (it failed, or there is no report and nothing running), ask them in one question what the company does, and that counts instead. Write the whole document, as always.
3. Thank them in one sentence, then offer the next step with your recommendation: the AI audit if they're not sure what to build, or planning the agent they have in mind with `/plan-agent`.
