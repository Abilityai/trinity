# System chain tests (trinity-enterprise#794)

Part of the Trinity 1.0 system test (trinity-enterprise#783). Each component has
its own tests; a **chain** checks that a change in one part of a rolled-out
system shows up in another part — and passes only on records the system wrote
(platform rows, files the platform wrote into a container, the execution
record), never on an agent's own report that it is done.

## The chains

| Id | Chain | Status on `dev` |
|---|---|---|
| J15 | A fleet rolls out from one definition | **not run** — blocked by ent#793 (the reference company) |
| J16 | A canon fact reaches its consumers | **not run** — blocked by ent#793 and the change-propagation work (ent#804); no canon-version or staleness record exists |
| J17 | A fact reaches its owner | **not run** — blocked by ent#793 and fact routing (ent#804) |
| J18 | A library change reaches every holder | runs when given a test skills repo (below), else **not run** |
| J19 | An objective reaches the person | runs on any stack with Docker; **partial** — the brief step is not run (below) |

They are journeys J15–J19 in `tests/journeys/catalog.yaml`; the harnesses are in
`tests/system_chains/`.

## Running them

Against a release candidate, or any instance you can afford to mutate (the
chains create and delete their own `pytest-ephemeral-chain-*` agents, register
and remove a skill source, and toggle fleet re-inject for the run):

```bash
TRINITY_API_URL=http://localhost:8000 \
TRINITY_TEST_PASSWORD=<admin password> \
CHAIN_SKILLS_REPO=owner/name CHAIN_SKILLS_GITHUB_TOKEN=<token> \
scripts/system/run_chains.sh --report-dir ./chain-report
```

Or dispatch the **system-chains** workflow with the ref to test (dispatchable
once the next release cut carries the file to `main` — GitHub registers
`workflow_dispatch` from the default branch); it boots a fresh
stack, runs the chains and uploads `chain-report` (configure
`vars.CHAIN_SKILLS_REPO` and `secrets.CHAIN_SKILLS_GITHUB_TOKEN` for J18).

To look at what a chain did in the UI, set `CHAIN_KEEP_AGENTS=1`: the agents it
created are left in place (named in the report as **kept agents**) instead of
deleted — delete them yourself afterwards.

The chains are collected only with `TRINITY_CHAIN_TESTS=1`, which the runner
sets — they never ride along with `run-full.sh` or the per-PR journey lane.
Reading an agent's own key needs the Docker socket of the host running the
stack; without it the steps that need the key report **not run**.

## The report

Every run writes `chain-report.json` and `chain-report.md`. Per chain:

* **passed** — every step ran and passed;
* **failed** — at the named step, with the reason;
* **partial** — everything that ran passed, but a named step could not run;
* **not run** — the chain could not start, with the reason.

Only **passed** is evidence. The summary is a go only when every chain passed,
and `run_chains.sh` exits non-zero otherwise. A test that records no step is
reported failed, never passed. The verdict rules are pinned by
`tests/unit/test_ent794_chain_report.py`.

## What each running chain proves

**J18** — a skill committed to the test repo is synced into the library
(`GET /api/skills/library` → its `version`, the git tree SHA), assigned to two
agents, and carried at that version in each container's platform-written
`~/.claude/skills/<name>/.trinity-skill.json`. The skill changes; after one
library sync both holders carry the new version and the new `SKILL.md` with no
per-agent action. An agent key without `skills.manage` is refused
(`skill_management_not_permitted`) on itself and on another agent and changes
nothing. Fleet re-inject after a sync is **off by default**
(`skills_library_auto_reinject_enabled`); with it off a change reaches a holder
only when that agent restarts. The chain turns it on for the run, says so on
the step, and restores it.

**J19** — a canon objective names a target; the agent records the number with
its own key; the Workspace role card and the operator objectives read show it
owned, against the target, and fresh; with recording stopped it reads stale on
the card past 2× the metric's cadence (the platform's minimum cadence is 60 s),
keeping the last value beside the mark.

**The brief step of J19 is not run, deliberately.** The scheduled brief gets no
objective number from the platform — any number in a brief is written by the
model, and a chain does not pass on what an agent says. The chain reports
**partial** until the platform carries the number into the brief.
