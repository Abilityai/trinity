import { buildManifest, deployFailureMessage, guideNameFor, myGuideCard } from './portalBuild'

// A deleted agent's name stays held for a while, so a held address steps on a few times.
const HELD_NAME_TRIES = 3

/**
 * The signed-in person's own guide: the one they own on their roster, or else the
 * address a new one would get. Someone else's guide is never theirs.
 */
export function myGuideName({ roster = [], username = '' } = {}) {
  return myGuideCard(roster)?.name || guideNameFor(username)
}

/**
 * Sets up the person's own guide, owned by them because they deploy it. Returns the
 * address it got and, if it failed, why in words a person can act on.
 */
export async function deployMyGuide(store, username) {
  // Without the signed-in name every new guide would get the same address.
  if (!String(username || '').trim()) {
    return { name: '', failure: "Trinity doesn't know who is signed in yet. Reload the page and try again." }
  }
  const held = []
  let name = guideNameFor(username)
  for (let attempt = 0; attempt < HELD_NAME_TRIES; attempt += 1) {
    let result = null
    let error = null
    try {
      result = await store.deployBuildSystem(buildManifest(name), name)
    } catch (e) {
      // A deploy that created nothing answers 500, with the same body as a success.
      error = e
      result = e?.response?.data || null
    }
    const failed = Array.isArray(result?.failed) ? result.failed : []
    const nameHeld = failed.some((f) => f?.name === name && /already exists/i.test(f?.reason || ''))
    if (nameHeld) {
      held.push(name)
      name = guideNameFor(username, held)
      continue
    }
    if (error) return { name, failure: deployFailureMessage({ error }) }
    // Trinity adds _2 to a name a live agent has: the guide is what it really created.
    const created = Array.isArray(result?.agents_created) ? result.agents_created[0] : ''
    return { name: created || name, failure: deployFailureMessage({ result }) }
  }
  return { name, failure: deployFailureMessage({ result: { failed: [{ name: held.at(-1), reason: 'already exists' }] } }) }
}
