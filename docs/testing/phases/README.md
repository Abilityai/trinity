# Trinity UI Test Phases

Scripted click-through scenarios for the Trinity web UI. Each `PHASE_NN_*.md` file is one
self-contained scenario: a browser-driving agent (or a person) follows its numbered steps
against a running local instance and checks each **Expected** line.

[`INDEX.md`](INDEX.md) is the catalog — every phase, its status and what it covers.

## How the phases are run

- **Core team**: the `/ui-sweep` skill runs one phase per invocation (`scenarios` lane) through
  the `ui-integration-tester` subagent and Playwright, and writes a dated report to
  [`docs/testing/ui-sweep/`](../ui-sweep/). The same skill has a `refresh` lane that re-checks
  one phase file against the code.
- **Anyone else**: open a phase file and follow it by hand, or hand it to any agent that can
  drive a browser. Nothing in a phase depends on private tooling.

There is no `run_test_phases.py` runner; earlier versions of this folder referred to one that
was never written.

## The rules every phase follows

These are what make a phase safe to run unattended and cheap to keep true. A phase that breaks
one is a bug in the phase.

1. **Independent.** No phase depends on state left by another. A phase that needs a known
   state sets it in its own Setup section.
2. **Three fixtures, nothing else.** Phases use the local fixture agents `test-echo`,
   `test-counter` and `test-delegator` (templates in `config/agent-templates/`), which stay
   running between runs. A phase never creates, stops or deletes any other agent, and never
   changes `trinity-system`. A phase that must create an agent creates one throwaway whose name
   starts with `sweep-tmp-<phase>` and deletes it in its own Cleanup. (Deleting an agent keeps its
   name reserved for a while, so phases add a time suffix rather than reuse a fixed name.)
3. **Restores what it changes.** Any setting, permission, policy or file a phase changes is
   read first and put back in a `Cleanup / Restore` section.
4. **Source-verified.** Every route, tab, label and endpoint in a step exists in the current
   code. The `Last verified` line in each header says when that was last checked, and whether
   the phase has been run in a browser since.
5. **No secrets.** Log in as `admin` with `ADMIN_PASSWORD` from `.env`; no password, token or
   real email address appears in a phase file or in a report.
6. **Bounded.** One phase fits in about 25 minutes, sends at most four messages to fixture
   agents (each is a real model call), and has no wait longer than about a minute.
7. **Unattended-safe.** Steps that need a second user, a real mailbox, an external GitHub
   repository, a wiped database or host-level access sit under a `Manual-only` heading and
   are not run by the scenario runner.
8. **Honest about what is not there.** Some tabs and routes are not present on every
   instance. A step for one says "if it is not present, record `SKIPPED (not present)`" and
   moves on.

## Fixtures

| Agent | Template | What it does |
|---|---|---|
| `test-echo` | `local:test-echo` | Echoes the message back; the default target for chat, tasks, links |
| `test-counter` | `local:test-counter` | Keeps a counter in a file; used for state and files |
| `test-delegator` | `local:test-delegator` | Lists agents and delegates to them; used for collaboration |

The `/ui-sweep` seed script also prepares a chat and a task execution on `test-echo`, a task on
`test-counter`, a workspace thread, a room and a public chat link, and hands their ids to the
tester. Run by hand, create the equivalent through the UI where a phase asks for one.

## Statuses

| Mark | Meaning |
|---|---|
| 🟢 | Run in a browser and passed since its last rewrite |
| 🟡 | Rewritten against source; not yet run in a browser |
| 🆕 | New phase; not yet run in a browser |
| ⛔ | Retired — the file is a stub saying why and what covers it now |

## Writing or changing a phase

Follow the skeleton of any current phase (header block with Purpose / Duration / Assumes /
Output / Last verified, then Background, Prerequisites, numbered `Test:` steps with Action and
Expected, Cleanup / Restore, optional Manual-only, Critical Validations, Success Criteria,
Troubleshooting). Before you write a label or a route, find it in `src/frontend/src/` — the
router (`router/index.js`), the agent tab list (`utils/agentTabs.js`) and the nav
(`utils/navLinks.js`) are the three places the January phases drifted from. Add the phase to
`INDEX.md`. Numbers are never reused: a retired phase keeps its number and its stub.
