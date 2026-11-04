# The AI audit playbook

The method behind `/company-audit`. An audit answers one question: **which agent should this company build first, and how will they know it worked?**

It takes about 10 minutes with one person. The result is a ranked list of opportunities, a clear first agent with a way to measure it, a map of where the data lives, and `context.md`.

## Principles

1. **Start from the business, not from AI.** Ask what the company is trying to achieve before asking what could be automated.
2. **Ask about what happened, not what might.** "Walk me through the last time you did this" gets real answers. "Would an agent help?" gets polite ones.
3. **Work with real numbers.** How often, how long, how many people. An estimate from them is fine. A number you made up is not.
4. **Look at the whole workflow, not one task.** The value is often in redesigning how work moves, so ask what happens before and after.
5. **An agent isn't always the answer.** Say so when a simple prompt, a rule or a fix to the process would do. It's what makes the rest of the advice credible. That is about something simpler doing the job, never about the goal being too big: an agent can help with strategy too.
6. **Start small.** Pick something small that works and that someone will notice.
7. **Name the risk before you build.** Know who is affected if it's wrong, and what a person must approve.

## The stages

Go through them in order, one question at a time. Skip anything you already know from `profile.md` or `company-research.md`. Update the world after each answer.

### 1. Goals (1 question)

What is the company trying to do this year, and what is this person measured on? Everything later gets ranked against this. Note what's off limits: anything regulated, sensitive or politically difficult.

Never tell them an agent can't help with a goal. A goal like working out the business model, the pricing or which market to go after is thinking work, and an agent can help with it: it researches, compares, runs the numbers and drafts options, and the decision stays theirs. Say what an agent would do for each goal.

### 2. Walk through the work

Start with their own team: "You're Head of Marketing. Walk me through a normal week. What does your team actually do?" Then pick the two or three jobs that sound biggest, and for each one take the last time it happened:

| Ask | You're finding |
|---|---|
| What starts it? | The trigger |
| What do you need before you can begin, and where does it come from? | The inputs and who supplies them |
| What are the steps? | The process |
| What comes out, and who gets it? | The output and who it's for |

### 3. Look for the four kinds of opportunity

Use these questions to bring out what the walkthrough missed:

- **Repetitive work:** "What's on your anti-to-do list: manual work that isn't the best use of your time?"
- **Bottlenecks:** "Where do you get stuck until someone from another team can help?"
- **Getting stuck:** "What do you struggle to get started on, or keep putting off?"
- **Thinking work:** "What are you trying to work out right now, like the business model, the pricing or where to grow next?" An agent can research it, compare the options, run the numbers and draft a recommendation for them to decide on.

Also ask what gets dropped because nobody has time, and where information gets copied by hand from one place to another.

### 4. Size it

For each candidate, get the numbers: how often, how long each time, how many people, and what a mistake costs. Work out the hours a month together. Record it as they said it, and note when it's a rough guess.

### 5. Find the data

For each candidate: where does the information live, what format is it in, who owns it, and can a tool reach it? Add it to `data_sources`, with `access` set to what you actually know. Information in someone's head or on scanned paper makes it a much bigger job. Say so.

### 6. Check the fit

Choose the simplest thing that will do the job, and say which it is:

| If the work is | Then it needs |
|---|---|
| Broken or pointless | Fixing or dropping, not automating |
| The same fixed steps each time, with few exceptions | A rule or ordinary automation. Say an agent is more than it needs |
| One-off, and a person stays in the loop | A good prompt, or a saved prompt |
| Variable, needing judgement, with steps you can't predict, and a result someone can check | **An agent** |
| Research, analysis or a plan for them to decide on, such as the business model or pricing | **An agent** that gathers, compares and drafts, while the decision stays theirs |

Also check whether the process is stable, and how often exceptions happen. A process that keeps changing or is mostly exceptions is a poor first agent. An agent also needs a way to tell whether it did well, so ask how a person would check the result.

### 7. Check the risk

- Who is affected if it gets this wrong, and how badly? Think of customers, staff and money.
- Does it touch personal, financial or regulated data?
- What can it do alone, and what must a person approve? Default to a person approving anything that sends, publishes, pays or deletes.

High risk doesn't rule something out, but it doesn't make a good first agent.

### 8. Rank

Score each candidate on **value** (against stage 1 and stage 4) and **effort** (stages 5 and 6), then place it:

| | Low effort | High effort |
|---|---|---|
| **High value** | **Start here** | Plan it for later |
| **Low value** | A quick helper for one person | Leave it |

From "start here", choose the first agent. It should be small, have data that can be reached, be low risk, and have someone who will notice it working. Mark it `recommended: true`, and record the rest in `opportunities` with `why`, `effort` and `value`.

### 9. Recommend

Tell them:

1. **The first agent:** what it does for them, in their words.
2. **Why this one:** in one or two sentences, using their numbers and connecting it to their goal from stage 1.
3. **How you'll know it worked:** today's number and the target. For example, "the report takes 4 hours a month now, and should take 30 minutes."
4. **What it may do alone,** and what comes to them.
5. **What you'd do next,** and anything you'd advise against.

Then write `context.md` and go to `/plan-agent` if they agree.

## Fitting it to the person

| Level | How the audit changes |
|---|---|
| `newcomer` | Say what an agent is and what this conversation is for, first. Give an example from their job with each question, and explain stage 6 in everyday words. Keep the framework names out of it. |
| `practitioner` | The same stages, with fewer examples. Share your reasoning at stages 6 and 8. |
| `builder` | Move quickly. Group questions where it's natural, and be explicit about the agent-or-not call and the risks. |

They can stop at any point. If they stop early, finish with what you have, say plainly what you didn't cover, and don't present a partial audit as a complete one. A partial audit still needs stages 6 and 7 for whatever you recommend.

## Where this comes from

Stages 3 and 8 follow OpenAI's guide. Stages 1 and 7 follow the MAP function of NIST's AI Risk Management Framework. Stage 6 draws on Anthropic's guidance on when to use agents, and on common automation-suitability criteria. Stage 2 is a light version of SIPOC, the Six Sigma process map. Principle 2 is the idea behind *The Mom Test*. Principle 4 reflects McKinsey's review of agentic builds.

- OpenAI, *Identifying and scaling AI use cases*: https://cdn.openai.com/business-guides-and-resources/identifying-and-scaling-ai-use-cases.pdf
- Anthropic, *Building effective agents*: https://www.anthropic.com/research/building-effective-agents
- NIST, *AI Risk Management Framework*, core functions: https://airc.nist.gov/airmf-resources/airmf/5-sec-core/
- McKinsey, *One year of agentic AI: Six lessons from the people doing the work*: https://www.mckinsey.com/capabilities/quantumblack/our-insights/one-year-of-agentic-ai-six-lessons-from-the-people-doing-the-work
