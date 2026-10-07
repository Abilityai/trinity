/**
 * abilityai/trinity-enterprise#665 — caller-declared idempotency key on the
 * human-facing sends (send_message, call_user, send_group_message).
 *
 * The per-turn guard (#1084) stops a re-delivered turn repeating itself. It
 * cannot stop the NEXT run of a recurring agent repeating what the last run
 * said: those are two executions. `idempotency_key` names the intent; the
 * backend store delivers it once per (agent, recipient, key) within the TTL.
 */
import { z } from "zod";

export const INTENT_KEY_TEACHING =
  " Recurring runs: pass `idempotency_key`, a stable name for what you are telling them " +
  "(e.g. 'gcp-ceiling-correction'), and it is delivered at most once per `idempotency_ttl` " +
  "seconds (60-86400, default 86400) across ALL your runs. When an earlier run already " +
  "sent it the result is `sent: false, suppressed_by: \"idempotency_key\"` with " +
  "`first_sent_at`: record that, and do not resend under a new key. Change the key when " +
  "the information changes. A 409 means another run is sending it now: retry with the same key.";

export const intentKeyParams = {
  idempotency_key: z.string().min(1).max(200).regex(/^[A-Za-z0-9_.:/-]+$/).optional()
    .describe(
      "Stable name for what you are telling them. Same key + same recipient within " +
      "idempotency_ttl is delivered once across all your runs. Never derived from the text."
    ),
  idempotency_ttl: z.number().int().min(60).max(86400).optional()
    .describe("Suppression window in seconds (60-86400, default 86400). Needs idempotency_key."),
};

export type IntentKeyParams = { idempotency_key?: string; idempotency_ttl?: number };

const RESULT_FIELDS = ["sent", "suppressed_by", "first_sent_at", "first_execution_id"] as const;

/** The ent#665 fields the backend returned (present only when a key was sent). */
export function intentResultFields(result: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const k of RESULT_FIELDS) {
    if (result[k] !== undefined && result[k] !== null) out[k] = result[k];
  }
  return out;
}
