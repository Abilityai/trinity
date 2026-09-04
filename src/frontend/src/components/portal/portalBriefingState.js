/**
 * Workspace briefing-hydration and stage-loading rules (#2163) — pure.
 *
 * #2163 took the new-chat briefing OFF the roster's critical path: `/my-agents`
 * now ships every card with `briefing_state: "pending"` and the client hydrates
 * through `GET /briefings`. Every decidable rule that follows from that lives
 * here rather than in an SFC, because `vitest.config.js` pins
 * `environment: 'node'` with no component-mount harness — a rule written inside
 * a component is a rule no test can reach.
 *
 * Deliberately NOT in `portalUtils.js`: that module is a different lane's
 * hot file, and these rules have no consumer in common with it.
 *
 * The state vocabulary is the SERVER's (`client_portal/models.py`), so the two
 * ends cannot invent a fourth value:
 *
 *   pending      not fetched yet — the roster deferred it
 *   ready        a briefing reached a verdict (its fields may still be empty:
 *                that is an agent with nothing exposed, not a failure)
 *   unavailable  the briefing tripped its bound, raised, or was refused
 *
 * The client stamps `unavailable` too — for a failed or rate-limited hydration
 * call — using the same word rather than a fourth one.
 */

import { viewState } from '@/utils/loadingState'

export const BRIEFING_PENDING = 'pending'
export const BRIEFING_READY = 'ready'
export const BRIEFING_UNAVAILABLE = 'unavailable'

/**
 * A card's briefing state, defaulting to `ready`.
 *
 * Absent means "resolved inline, nothing to hydrate" — the payload shape from
 * a backend that predates #2163. That is the non-blocking direction: reading an
 * absent field as `pending` would leave such a client waiting forever on a
 * route its backend does not serve.
 */
export function briefingStateOf(card) {
  const state = card && card.briefing_state
  return state === BRIEFING_PENDING || state === BRIEFING_UNAVAILABLE ? state : BRIEFING_READY
}

/**
 * Carry hydrated briefings across a roster REFETCH.
 *
 * A refetch that dropped hydrated fields would re-enter the loading phase on
 * every zone that already has data — the p13 "background refresh is invisible"
 * rule, and the exact bug #1927 catalogued on four other panels.
 *
 * `unavailable` is the one state that does NOT survive: a refetch is an
 * explicit user act (both "Try again" buttons call `fetchRoster` directly), so
 * an agent stuck on one earlier 429 or blip must not read "Couldn't load
 * suggestions" until the page is reloaded. It returns to `pending`, and the
 * re-entered loading edge on that one zone is the honest retry.
 *
 * Pure: neither argument is mutated; the returned cards are the NEXT ones with
 * briefing fields merged in.
 */
export function mergeRosterBriefings(prevAgents, nextAgents) {
  const next = Array.isArray(nextAgents) ? nextAgents : []
  const prev = Array.isArray(prevAgents) ? prevAgents : []
  if (!prev.length) return next.slice()

  const byName = new Map(prev.map((a) => [a && a.name, a]))
  return next.map((card) => {
    const before = byName.get(card && card.name)
    if (!before) return card
    const state = briefingStateOf(card)
    // Only a card the server says is un-hydrated may inherit; a server that
    // sends a real briefing (the agent page's inline one, or a backend without
    // #2163) always wins.
    if (state !== BRIEFING_PENDING) return card
    const had = briefingStateOf(before)
    if (had === BRIEFING_READY) {
      return {
        ...card,
        description: before.description,
        playbooks: before.playbooks,
        searchable_playbooks: before.searchable_playbooks,
        playbooks_total: before.playbooks_total,
        briefing_state: BRIEFING_READY,
      }
    }
    return card   // pending stays pending; unavailable is re-armed as pending
  })
}

/**
 * Fold a `/briefings` response onto the roster.
 *
 * `requested` is the names the call asked for (null = the whole roster), so a
 * name that was asked for and did not come back is `unavailable` rather than
 * left pending forever. `failed` marks the whole request failed (network, 429,
 * an older backend with no route) — every requested name becomes `unavailable`
 * and whatever fields were already there are kept.
 */
export function applyBriefings(agents, briefings, requested = null, { failed = false } = {}) {
  const list = Array.isArray(agents) ? agents : []
  const data = briefings && typeof briefings === 'object' ? briefings : {}
  const asked = Array.isArray(requested) ? new Set(requested) : null

  return list.map((card) => {
    const name = card && card.name
    const wanted = asked ? asked.has(name) : true
    if (!wanted) return card
    if (failed) {
      return briefingStateOf(card) === BRIEFING_READY
        ? card                                   // never downgrade real data
        : { ...card, briefing_state: BRIEFING_UNAVAILABLE }
    }
    const entry = data[name]
    if (!entry) return { ...card, briefing_state: BRIEFING_UNAVAILABLE }
    const state = entry.state === BRIEFING_READY ? BRIEFING_READY : BRIEFING_UNAVAILABLE
    return {
      ...card,
      description: entry.description ?? null,
      playbooks: entry.playbooks || [],
      searchable_playbooks: entry.searchable_playbooks || [],
      playbooks_total: entry.playbooks_total || 0,
      briefing_state: state,
    }
  })
}

/**
 * Should the background batch fire after a roster load?
 *
 * ONE pending card is enough. A `>= 2` rule strands the single-agent case: the
 * active-agent single fires from the conversation stage, and a deep link
 * straight into an EXISTING thread never renders that stage — so without the
 * batch, that agent's `/` typeahead and its picker description would stay
 * pending for the whole session.
 */
export function briefingHydrationPlan(agents) {
  const list = Array.isArray(agents) ? agents : []
  const pending = list.filter((a) => briefingStateOf(a) === BRIEFING_PENDING).length
  return { pending, batch: pending >= 1 }
}

/**
 * The briefing hint zone's loading verdict.
 *
 * `unavailable` is neither: no beam (there is nothing coming) and no reveal
 * (nothing arrived) — an honest snap to the "couldn't load" line. `ready` with
 * neither hints nor a description is also a snap: revealing an empty zone is
 * the celebratory pass over nothing.
 */
export function briefingZone(card) {
  const state = briefingStateOf(card)
  const hints = (card && card.playbooks) || []
  const hasContent = hints.length > 0 || Boolean(card && card.description)
  return {
    state,
    loading: state === BRIEFING_PENDING,
    reveal: state === BRIEFING_READY && hasContent,
    unavailable: state === BRIEFING_UNAVAILABLE,
  }
}

/**
 * Should this agent's briefing be requested?
 *
 * `pending` always; `unavailable` at most ONCE per session, so a wedged agent
 * costs one extra bounded call rather than one per visit to its chat; `ready`
 * never (a background re-fetch would re-enter loading on a zone that has data).
 */
export function shouldRequestBriefing(card, attempts = 0) {
  const state = briefingStateOf(card)
  if (state === BRIEFING_PENDING) return true
  if (state === BRIEFING_UNAVAILABLE) return attempts < 1
  return false
}

/**
 * The Workspace STAGE's loading verdict — the AC4 zone.
 *
 * Over `viewState`, so the "loading means no data yet, never fetch in flight"
 * rule has one home (#1927). Four inputs, each load-bearing:
 *
 *   rosterLoaded  the roster reached a VERDICT (never `store.loading`, which is
 *                 in-flight and would re-enter loading on every refetch)
 *   resolved      `bootstrap()` finished. Without it the stage can reveal while
 *                 `activeAgentName`/`pendingSession` are still unset — those are
 *                 assigned only after `refreshThreads()` — so a deep link would
 *                 mount the conversation for `agents[0]`, flash ITS briefing,
 *                 then remount for the real target. AC4 says "while the roster
 *                 AND a thread's history hydrate".
 *   error         a failed roster snaps to its own copy, never a reveal
 *   unreachable   a link naming an agent this caller cannot reach resolves the
 *                 roster fine; that refusal card must snap, not wipe in.
 */
export function stageZone({
  rosterLoaded = false,
  resolved = false,
  error = null,
  agents = [],
  unreachable = false,
} = {}) {
  const list = Array.isArray(agents) ? agents : []
  const hasLoaded = Boolean(rosterLoaded && resolved && !error)
  const { state } = viewState({ hasLoaded, error, count: list.length })
  return {
    state,
    loading: state === 'loading',
    reveal: state === 'ready' && !unreachable,
  }
}
