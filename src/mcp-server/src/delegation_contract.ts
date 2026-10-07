/**
 * The delegation contract (abilityai/trinity-enterprise#568) — what a dispatch
 * receipt means, and what to do instead of re-sending.
 *
 * ONE text, read on every surface a caller sees:
 *   - the platform prompt, §Agent Collaboration
 *     (src/backend/services/platform_prompt_service.py → DELEGATION_CONTRACT),
 *     which every agent receives on every turn;
 *   - the `chat_with_agent` description and every dynamic `chat_with_<agent>`
 *     tool (#846), verbatim — the only copy an external MCP client, or an agent
 *     at PromptTier.MINIMAL, ever sees;
 *   - `fan_out` and `send_message`, which repeat DELEGATION_RULE and point here.
 *
 * The two copies must stay byte-identical: tests/unit/test_ent568_delegation_contract.py
 * parses the array below as JSON and fails on any drift. Keep it a plain array of
 * double-quoted strings — no comments inside it, no template literals, no `+`.
 *
 * Budget: Claude Code shows the model only the first 2,048 characters of a tool
 * description (CLAUDE_CODE_MAX_MCP_DESCRIPTION_LENGTH), so this text plus a
 * tool's own lead must fit inside that; delegation-contract.test.ts pins it on
 * the descriptions the server publishes.
 *
 * No imports: client.ts and the tool modules both read this file.
 */
export const DELEGATION_CONTRACT_LINES: readonly string[] = [
  "**The delegation contract: a receipt means the work is running. Never re-send on silence.**",
  "- A `chat_with_*` or `fan_out` call answers with the reply or with a receipt: an `execution_id` (a `fan_out_id` for a batch) in place of the reply, whatever its status (`accepted`, `queued`, `queued_timeout`, `fan_out_timeout`). The work arrived and is queued, running or done, even if your call timed out.",
  "- Never re-send because a call timed out or its delivery could not be confirmed. An exact repeat is normally answered with the original; a reworded one can run the work twice.",
  "- Read the result with `get_execution_result(agent_name, execution_id)` (`get_fan_out_result` for a batch); `running` is not stuck. To finish later, call `set_reminder` with a message naming the `execution_id`, then end your turn. A receipt is not a result: never report the work as done.",
  "- Confirmed `failed` or `cancelled`: re-send word for word to retry; same `execution_id` back: `set_reminder`, end your turn.",
  "- An error without an `execution_id`, `agent_busy` included, is not proof that nothing ran. Look for your exact message in `list_recent_executions(agent_name)`: one match is your receipt; otherwise re-send it word for word, same options (for `agent_busy`, after `retry_after_seconds`).",
  "- `pending_approval`: nothing ran. Do not retry or route it through another agent; its `message` says whether you will hear the outcome. On `retryable: false`, do what it says.",
  "- For long work use `parallel=true, async=true`: the receipt comes back at once, and the run's end fires the target's `agent.task.completed` / `agent.task.failed`. A `subscribe_to_event` subscription to those wakes you for every run of that agent, so match the `execution_id`.",
];

export const DELEGATION_CONTRACT = DELEGATION_CONTRACT_LINES.join("\n");

/** The contract's own rule sentence, repeated verbatim by `fan_out` and `send_message`. */
export const DELEGATION_RULE =
  "Never re-send because a call timed out or its delivery could not be confirmed.";

/**
 * How every receipt the MCP server writes ends: what to do instead of
 * re-sending, with the exact call (#914 / #2661 / ent#568).
 */
export function readNotResend(agentName: string, executionId: string): string {
  return `Do not re-send: read the outcome with get_execution_result(agent_name="${agentName}", execution_id="${executionId}").`;
}
