import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { test } from "node:test";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import type { TrinityClient } from "../client.js";
import { createChatTools } from "./chat.js";
import { createReminderTools } from "./reminders.js";


type EffectArguments = Record<string, unknown>;

const fakeClient = {} as TrinityClient;
const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../../../..");
const templateLibrary = resolve(
  repositoryRoot,
  "config/agent-templates/delivery-conductor/lib",
);
const pythonFixture = String.raw`
from datetime import datetime, timedelta, timezone
import hashlib
import json

from delivery_conductor.cli import _effect_arguments
from delivery_conductor.contracts import BudgetView, ProposedAction, ReminderSpec
from delivery_conductor.ledger import Lease
from delivery_conductor.tick import TickHandoff, _reminder_action

now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
lease = Lease("wake-contract", 1, now, now + timedelta(minutes=5))
budget = BudgetView(1, 1, 1)

chat_payload = json.dumps(
    {"identifier": "target-agent"}, separators=(",", ":"), sort_keys=True
)
chat_action = ProposedAction(
    "chat", "action-contract-1", chat_payload, "revision-1", "delivery-intent"
)
chat_handoff = TickHandoff(
    "action",
    lease,
    "revision-1",
    budget,
    chat_action,
    hashlib.sha256(chat_payload.encode("utf-8")).hexdigest(),
)

reminder = ReminderSpec(
    "reminder-contract-1", "2026-08-03T12:00:00Z", "follow-up"
)
reminder_action = _reminder_action(reminder, "revision-1")
reminder_handoff = TickHandoff(
    "reminder",
    lease,
    "revision-1",
    budget,
    reminder_action,
    hashlib.sha256(reminder_action.payload_json.encode("utf-8")).hexdigest(),
    reminder,
)

print(json.dumps({
    "chat": _effect_arguments(chat_action, chat_handoff, None, now),
    "reminder": _effect_arguments(reminder_action, reminder_handoff, reminder, now),
}, separators=(",", ":"), sort_keys=True))
`;


function loadGeneratedArguments(): { chat: EffectArguments; reminder: EffectArguments } {
  const completed = spawnSync(process.env.PYTHON ?? "python3", ["-P", "-c", pythonFixture], {
    cwd: repositoryRoot,
    encoding: "utf8",
    env: {
      PATH: process.env.PATH ?? "/usr/local/bin:/usr/bin:/bin",
      PYTHONDONTWRITEBYTECODE: "1",
      PYTHONPATH: templateLibrary,
    },
  });
  assert.equal(completed.status, 0, completed.stderr);
  assert.equal(completed.stderr, "");
  return JSON.parse(completed.stdout) as {
    chat: EffectArguments;
    reminder: EffectArguments;
  };
}


function assertExactKeys(value: EffectArguments, expected: string[]): void {
  assert.deepEqual(Object.keys(value).sort(), [...expected].sort());
}


function parseCanonicalMessage(argumentsValue: EffectArguments): EffectArguments {
  assert.equal(typeof argumentsValue.message, "string");
  const message = JSON.parse(argumentsValue.message as string) as EffectArguments;
  assertExactKeys(message, ["action_key", "payload_sha256", "references"]);
  assert.match(message.payload_sha256 as string, /^[a-f0-9]{64}$/);
  return message;
}


function assertNoAuthorityFields(value: unknown): void {
  const forbidden = new Set([
    "allowed_tools",
    "async",
    "delay_seconds",
    "execution_id",
    "model",
    "parallel",
    "system_prompt",
    "timeout_seconds",
  ]);
  if (Array.isArray(value)) {
    value.forEach(assertNoAuthorityFields);
  } else if (value !== null && typeof value === "object") {
    for (const [key, child] of Object.entries(value)) {
      assert.equal(forbidden.has(key), false, `forbidden authority field: ${key}`);
      assertNoAuthorityFields(child);
    }
  }
}


const generated = loadGeneratedArguments();


test("Python-generated chat arguments match the Trinity tool schema", () => {
  assertExactKeys(generated.chat, ["agent_name", "message"]);
  assert.equal(generated.chat.agent_name, "target-agent");
  const message = parseCanonicalMessage(generated.chat);
  assert.equal(message.action_key, "action-contract-1");
  assert.deepEqual(message.references, { identifier: "target-agent" });
  assert.equal(
    message.payload_sha256,
    createHash("sha256").update('{"identifier":"target-agent"}').digest("hex"),
  );
  assertNoAuthorityFields(generated.chat);

  const schema = createChatTools(fakeClient, false).chatWithAgent.parameters;
  assert.equal(schema.safeParse(generated.chat).success, true);
  assert.equal(schema.safeParse({ message: generated.chat.message }).success, false);
});


test("Python-generated reminder arguments match the Trinity tool schema", () => {
  assertExactKeys(generated.reminder, ["message", "fire_at"]);
  assert.equal(generated.reminder.fire_at, "2026-08-03T12:00:00Z");
  const message = parseCanonicalMessage(generated.reminder);
  assert.match(message.action_key as string, /^reminder-[a-f0-9]{64}$/);
  const references = message.references as EffectArguments;
  assertExactKeys(references, ["digest", "references"]);
  assert.match(references.digest as string, /^[a-f0-9]{64}$/);
  assert.deepEqual(references.references, {
    identifiers: ["reminder-contract-1"],
    reason_code: "follow-up",
    utc_timestamp: "2026-08-03T12:00:00Z",
  });
  assertNoAuthorityFields(generated.reminder);

  const schema = createReminderTools(fakeClient, false).setReminder.parameters;
  assert.equal(schema.safeParse(generated.reminder).success, true);
  assert.equal(schema.safeParse({ message: "" }).success, false);
  assert.equal(schema.safeParse({ message: "x".repeat(4001) }).success, false);
});


test("local exact-key checks reject fields that generic Zod objects strip", () => {
  assert.throws(
    () => assertExactKeys({ ...generated.chat, system_prompt: "forbidden" }, [
      "agent_name",
      "message",
    ]),
    assert.AssertionError,
  );
  assert.throws(
    () => assertExactKeys({ ...generated.reminder, delay_seconds: 60 }, [
      "fire_at",
      "message",
    ]),
    assert.AssertionError,
  );
});
