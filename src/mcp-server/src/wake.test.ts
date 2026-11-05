/**
 * ent#1102 — what a receipt says about the wake.
 *
 * `resolveWake` is the receipt half of the feature: the backend dispatches a
 * turn on the caller when a delegated run ends (`caller_wake_service`), and the
 * delegation contract now tells callers to end their turn on `wake: on_ending`
 * instead of setting a reminder. That instruction is only safe if the field is
 * honest, so these tests are mostly about the cases where NO wake is coming.
 *
 * Runner: node:test → `node --import tsx --test src/*.test.ts`.
 */
import { strict as assert } from "node:assert";
import { describe, it } from "node:test";

import {
  resolveWake,
  wakePolicyFromAnswer,
  wakeReceiptFields,
  type WakeInput,
} from "./tools/chat.js";

/** A dispatch that would be woken: an agent caller, a receipt, policy on. */
const dispatched: WakeInput = {
  outcome: "receipt",
  hasCaller: true,
  isSelfTask: false,
  policyEnabled: true,
};

describe("ent#1102 resolveWake", () => {
  it("promises the wake when a turn really will be dispatched", () => {
    assert.deepEqual(resolveWake(dispatched), { wake: "on_ending" });
  });

  it("says nobody is coming when the caller is not an agent", () => {
    assert.deepEqual(resolveWake({ ...dispatched, hasCaller: false }),
      { wake: "none", wake_reason: "no_caller" });
  });

  it("says nothing ran when the agent was busy (#3244)", () => {
    assert.deepEqual(resolveWake({ ...dispatched, outcome: "busy" }),
      { wake: "none", wake_reason: "nothing_ran" });
  });

  it("says the reply is already in hand when the call answered inline", () => {
    assert.deepEqual(resolveWake({ ...dispatched, outcome: "inline" }),
      { wake: "none", wake_reason: "answered_inline" });
  });

  it("stands down on a self-task, whose caller is the callee", () => {
    assert.deepEqual(resolveWake({ ...dispatched, isSelfTask: true }),
      { wake: "none", wake_reason: "self_task" });
  });

  it("stands down on a fan_out leg, whose ending the batch join answers (#2524)", () => {
    assert.deepEqual(resolveWake({ ...dispatched, batch: true }),
      { wake: "none", wake_reason: "fan_out" });
  });

  it("reports the owner's opt-out as such, not as silence", () => {
    assert.deepEqual(resolveWake({ ...dispatched, policyEnabled: false }),
      { wake: "none", wake_reason: "opted_out" });
  });

  it("makes NO claim when the dispatch answer carried no policy", () => {
    // The alternative — defaulting to `on_ending` because the instance default
    // is on — would print a promise to exactly the agents whose owner turned
    // the wake off, and the contract tells them to end their turn on it.
    assert.equal(resolveWake({ ...dispatched, policyEnabled: undefined }), undefined);
  });

  it("prefers a structural stand-down over the policy", () => {
    // An opted-IN caller still gets the true reason when nothing ran: the
    // policy cannot make an ending exist.
    assert.deepEqual(resolveWake({ ...dispatched, outcome: "busy", policyEnabled: true }),
      { wake: "none", wake_reason: "nothing_ran" });
    // And an opted-OUT caller is told the structural reason first, because that
    // is the one that would still hold if the flag were flipped back on.
    assert.deepEqual(resolveWake({ ...dispatched, isSelfTask: true, policyEnabled: false }),
      { wake: "none", wake_reason: "self_task" });
  });

  it("never answers `none` without a reason", () => {
    const inputs: WakeInput[] = [
      { ...dispatched, hasCaller: false },
      { ...dispatched, outcome: "busy" },
      { ...dispatched, outcome: "inline" },
      { ...dispatched, isSelfTask: true },
      { ...dispatched, batch: true },
      { ...dispatched, policyEnabled: false },
    ];
    for (const input of inputs) {
      const fields = resolveWake(input);
      assert.ok(fields && fields.wake === "none" && fields.wake_reason,
        `${JSON.stringify(input)} answered ${JSON.stringify(fields)}`);
    }
  });
});

describe("ent#1102 wakeReceiptFields", () => {
  it("prints the promise, so the contract's `end your turn` has something to read", () => {
    assert.deepEqual(wakeReceiptFields(dispatched), { wake: "on_ending" });
  });

  for (const [label, input] of [
    ["the agent was busy", { ...dispatched, outcome: "busy" }],
    ["the leg belongs to a fan_out batch", { ...dispatched, batch: true }],
    ["the owner opted out", { ...dispatched, policyEnabled: false }],
  ] as [string, WakeInput][]) {
    it(`corrects a dispatching caller when ${label}`, () => {
      assert.deepEqual(wakeReceiptFields(input), resolveWake(input));
    });
  }

  for (const [label, input] of [
    ["a person called, so there is no agent to wake", { ...dispatched, hasCaller: false }],
    ["the call answered inline and the reply is in hand", { ...dispatched, outcome: "inline" }],
    ["the caller is the callee", { ...dispatched, isSelfTask: true }],
  ] as [string, WakeInput][]) {
    it(`stays silent — and leaves the result byte-unchanged — when ${label}`, () => {
      // The verdict still names the reason; only the receipt declines to print
      // it, exactly as `chatRouteFields` declines on a default-armed inline
      // answer. A field here would change results pinned byte-for-byte in
      // delegation-contract.test.ts ("only `message` may change").
      assert.ok(resolveWake(input)?.wake_reason, "the verdict must still name it");
      assert.equal(wakeReceiptFields(input), undefined);
    });
  }

  it("says nothing at all when the dispatch answer carried no policy", () => {
    assert.equal(wakeReceiptFields({ ...dispatched, policyEnabled: undefined }), undefined);
  });
});

/**
 * The live input. `resolveWake` above is pure, and every test of it hands the
 * policy in by hand — which is exactly how the field shipped dead the first
 * time: the resolver was green while NO backend path emitted the key it reads
 * (reading review C1). These drive the real receipt shape the backend answers a
 * delegation with, so the two halves are pinned against each other.
 */
describe("ent#1102 the policy comes off a real backend receipt", () => {
  /** What `chat_execution_service._wake_on_ending_receipt` stamps. */
  const receipt = (effective: boolean, source: string) => ({
    status: "accepted",
    execution_id: "exec-1",
    agent_name: "callee",
    message: "Task accepted.",
    async_mode: true,
    wake_on_ending: { effective, source },
  });

  it("promises the wake when the backend receipt says the caller is woken", () => {
    assert.equal(wakePolicyFromAnswer(receipt(true, "instance")), true);
    assert.deepEqual(
      wakeReceiptFields({
        outcome: "receipt",
        hasCaller: true,
        isSelfTask: false,
        policyEnabled: wakePolicyFromAnswer(receipt(true, "instance")),
      }),
      { wake: "on_ending" },
    );
  });

  it("corrects the caller when the backend receipt says the owner opted out", () => {
    // The trap this closes: `{effective: false}` is a TRUTHY object, so reading
    // the field itself rather than `effective` promises a turn that never comes.
    assert.equal(wakePolicyFromAnswer(receipt(false, "agent")), false);
    assert.deepEqual(
      wakeReceiptFields({
        outcome: "receipt",
        hasCaller: true,
        isSelfTask: false,
        policyEnabled: wakePolicyFromAnswer(receipt(false, "agent")),
      }),
      { wake: "none", wake_reason: "opted_out" },
    );
  });

  it("stays silent on a receipt that carries no policy at all", () => {
    const { wake_on_ending: _omitted, ...bare } = receipt(true, "instance");
    assert.equal(wakePolicyFromAnswer(bare), undefined);
    assert.equal(wakePolicyFromAnswer(undefined), undefined);
    assert.equal(wakePolicyFromAnswer("not an object"), undefined);
  });

  it("reads a flattened boolean too, so neither side can drift silently", () => {
    assert.equal(wakePolicyFromAnswer({ wake_on_ending: true }), true);
    assert.equal(wakePolicyFromAnswer({ wake_on_ending: false }), false);
  });
});
