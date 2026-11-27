/**
 * What a narrowed read says to the agent that made it (trinity-enterprise#978).
 *
 * While a shared person's or a visitor's turn runs on an agent, the backend
 * narrows every read of executions made with the agent's key to that person,
 * because it cannot tell which process sent a request. That is by design, but
 * it was silent: the owner's own concurrent run read an empty list, or "not
 * found" for a finished run, and concluded that nothing ran.
 *
 * The backend marks such a response with `X-Trinity-Read-Narrowed: <code>`;
 * `turnFetch` records it for the tool call in progress (`turn-context.ts`).
 * A tool that reads executions spreads `narrowedField()` into what it returns:
 * `narrowed: {code, message}` when the answer was narrowed, nothing otherwise
 * (absent, never a null placeholder).
 *
 * One thing stays readable by id inside such a view (#4173): the run of the
 * turn the call is part of, and a run that turn started. The backend decides
 * it from the turn id `turnFetch` sends; such an answer still carries the
 * marker, because the view it was read under is still narrowed.
 *
 * The backend sends codes only (`turn_tier_service.READ_NARROWED_CODES`); the
 * wording lives here. `tests/unit/test_ent978_narrowed_read_marker.py` fails
 * when a backend code has no entry below.
 */
import { currentReadNarrowing } from "./turn-context.js";

export const READ_NARROWED_MESSAGES: Record<string, string> = {
  turn_person:
    "This answer is narrowed to the person this turn is for: it shows at most the runs that " +
    "person started. Other runs are left out, and read as not found by id. This turn's own run " +
    "and the runs it started stay readable by id.",
  running_turn:
    "This answer is narrowed because a turn for a shared person or a visitor is running on this " +
    "agent: it shows at most that person's runs (none for a visitor). Other runs are left out, " +
    "and read as not found by id. The full view returns when that turn ends.",
  unverified:
    "This answer is narrowed because Trinity could not verify which turn this request belongs to, " +
    "or which turns are running on this agent. Runs may be left out or read as not found.",
};

/** A code this server has no wording for yet (a newer backend). */
const FALLBACK_MESSAGE = "This answer is narrowed: runs may be left out or read as not found.";

export interface ReadNarrowing {
  code: string;
  message: string;
}

/** The narrowing recorded for the tool call in progress, if any. */
export function readNarrowing(): ReadNarrowing | undefined {
  const code = currentReadNarrowing();
  if (!code) return undefined;
  const message = Object.hasOwn(READ_NARROWED_MESSAGES, code) ? READ_NARROWED_MESSAGES[code] : FALLBACK_MESSAGE;
  return { code, message };
}

/** `{ narrowed }` to spread into a tool's answer; `{}` when it was not narrowed. */
export function narrowedField(): { narrowed?: ReadNarrowing } {
  const narrowed = readNarrowing();
  return narrowed ? { narrowed } : {};
}

/** One sentence for the description of a tool that shows the field. */
export const NARROWED_DESCRIPTION =
  "If the answer carries `narrowed` ({code, message}), it was read while a turn for a shared person " +
  "or a visitor limits what the agent's key can see: runs outside that view are left out or read as " +
  "not found, so do not conclude that nothing ran.";
