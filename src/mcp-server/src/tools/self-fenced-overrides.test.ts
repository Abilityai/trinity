/**
 * trinity-enterprise#840 — a tool whose backend route is self-fenced does not
 * advertise its `agent_name` parameter to an agent-scoped key as an override.
 *
 * The backend refuses an agent key that names another agent
 * (`dependencies.assert_own_agent`, the route census's `AGENT_CALLABLE_SELF`),
 * so for that caller the parameter is dead. A prompt-injected agent still reads
 * the tool schema, though, and "Agent name override ... Omit in normal use"
 * reads as a pivot it may take. The wording is the `decisions.ts` one: the
 * parameter is for a user-scoped key only, and naming another agent is refused.
 *
 * Runner: node:test → `node --import tsx --test src/tools/*.test.ts`.
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";
import { createMemoryTools } from "./memory.js";
import { createReminderTools } from "./reminders.js";
import { createDecisionTools } from "./decisions.js";
import { createVoiceReplyTools } from "./voice.js";
import type { TrinityClient } from "../client.js";

const fake = { getBaseUrl: () => "http://backend:8000" } as unknown as TrinityClient;

type Tool = { name: string; parameters: { shape: Record<string, { description?: string }> } };

const memory = createMemoryTools(fake, false);
const reminders = createReminderTools(fake, false);
const decisions = createDecisionTools(fake, false);
const voice = createVoiceReplyTools(fake, false);

// Every tool whose route is in AGENT_CALLABLE_SELF (tests/unit/_route_census.py)
// and still takes `agent_name` — kept by hand; the other self-fenced tools take
// the agent from the key. (`call_a2a_agent` and `get_a2a_task` REQUIRE the name,
// so it is not an override; both say an agent key may only pass its own.)
const SELF_FENCED: Tool[] = [
  memory.writeUserMemory,
  reminders.setReminder,
  reminders.listReminders,
  reminders.cancelReminder,
  decisions.recordDecision,
  decisions.listSeatDecisions,
  voice.sendVoiceReply,
] as unknown as Tool[];

describe("self-fenced tools describe agent_name as user-key-only", () => {
  for (const tool of SELF_FENCED) {
    it(tool.name, () => {
      const description = tool.parameters.shape.agent_name?.description ?? "";
      assert.match(description, /for a user-scoped key only/i, `${tool.name}: ${description}`);
      assert.match(description, /naming another is refused/i, `${tool.name}: ${description}`);
      assert.doesNotMatch(
        description,
        /override|omit in normal use|default to the bound agent/i,
        `${tool.name}: ${description}`
      );
    });
  }
});
