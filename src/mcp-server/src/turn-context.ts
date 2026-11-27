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
 *
 * The same door reads one thing back (trinity-enterprise#978): when the backend
 * narrowed a read to the person of a running turn it marks the response with
 * `X-Trinity-Read-Narrowed: <code>`, and `turnFetch` records the first such
 * code for the tool call in progress. `read-narrowing.ts` turns it into the
 * `narrowed` field a tool shows.
 */
import { AsyncLocalStorage } from "node:async_hooks";

// Same shape the client already validates for the header (#2392).
const EXECUTION_ID_SHAPE = /^[A-Za-z0-9_-]{1,128}$/;

/** One tool call: the turn it is part of, and what its backend calls reported. */
interface ToolCallState {
  turn?: string;
  narrowed?: string;
}

const store = new AsyncLocalStorage<ToolCallState>();

/** Run `fn` as part of the given turn; an id of the wrong shape counts as none. */
export function runInTurn<R>(executionId: string | undefined, fn: () => R): R {
  const id = executionId && EXECUTION_ID_SHAPE.test(executionId) ? executionId : undefined;
  return store.run({ turn: id }, fn);
}

/** The turn the current tool call is part of, if any. */
export function currentTurn(): string | undefined {
  return store.getStore()?.turn;
}

const NARROWED_HEADER = "X-Trinity-Read-Narrowed";
// The backend sends one of a few lowercase codes; anything else is not a code.
const NARROWED_CODE_SHAPE = /^[a-z_]{1,64}$/;

/**
 * The narrowed-read code a backend call of the current tool call reported, if
 * any. The first one sticks: one tool call acts for one principal, so every
 * marked answer in it carries the same reason.
 */
export function currentReadNarrowing(): string | undefined {
  return store.getStore()?.narrowed;
}

const TURN_HEADER = "X-Trinity-Execution-Id";

/**
 * `fetch` with the current turn added, unless the caller set the header itself.
 * The one door every backend call in `TrinityClient` goes through: a method
 * that called `fetch` directly sent no turn (fanOut did, until 2026-10-19), and
 * the backend reads a missing header as "no platform turn", which is open.
 */
export async function turnFetch(input: string | URL, init: RequestInit = {}): Promise<Response> {
  const state = store.getStore();
  let request = init;
  if (state?.turn) {
    const headers = new Headers(init.headers);
    if (!headers.has(TURN_HEADER)) headers.set(TURN_HEADER, state.turn);
    request = { ...init, headers };
  }
  const response = await fetch(input, request);
  // With or without a turn id: a session that sends none is narrowed the same way.
  if (state && !state.narrowed) {
    const code = response.headers?.get?.(NARROWED_HEADER);
    if (code && NARROWED_CODE_SHAPE.test(code)) state.narrowed = code;
  }
  return response;
}
