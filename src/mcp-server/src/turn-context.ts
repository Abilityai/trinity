/**
 * The turn a tool call is part of (trinity-enterprise#926).
 *
 * An agent's process sends `X-Trinity-Execution-Id` on every MCP request
 * (#2392, from its spawn's TRINITY_EXECUTION_ID). The backend reads it to rate
 * what the agent's key may do for the person the turn is for: narrow its reads,
 * refuse standing work (schedules, loops, reminders, events) in a shared
 * person's turn, and stamp the turns it dispatches with that person.
 *
 * It used to be forwarded by a handful of client methods that remembered to.
 * A method that forgot sent nothing, and the backend then saw "no platform
 * turn" and allowed everything, so the failure was silent and open. Now
 * `withAudit` (which wraps every registered tool) runs the tool inside this
 * store, and `TrinityClient` adds the header to every backend call made while
 * it runs. A new tool or client method is covered without remembering anything.
 */
import { AsyncLocalStorage } from "node:async_hooks";

// Same shape the client already validates for the header (#2392).
const EXECUTION_ID_SHAPE = /^[A-Za-z0-9_-]{1,128}$/;

const store = new AsyncLocalStorage<string | undefined>();

/** Run `fn` as part of the given turn; an id of the wrong shape counts as none. */
export function runInTurn<R>(executionId: string | undefined, fn: () => R): R {
  const id = executionId && EXECUTION_ID_SHAPE.test(executionId) ? executionId : undefined;
  return store.run(id, fn);
}

/** The turn the current tool call is part of, if any. */
export function currentTurn(): string | undefined {
  return store.getStore();
}
