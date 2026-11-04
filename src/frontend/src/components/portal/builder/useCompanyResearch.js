import { researchExecutionId, researchMessage } from './portalBuild'
import { deployMyGuide } from './useMyGuide'

const READY_ATTEMPTS = 8
const READY_DELAY_MS = 5000
const wait = (ms) => new Promise((resolve) => { setTimeout(resolve, ms) })

/**
 * Starts the company research: a job of its own on the person's own guide, so it runs
 * while they carry on and while the audit takes place. Sets their guide up first if it
 * isn't there, because the setup panel can reach this before anything else has.
 * Takes a while when it has to wait for the guide, so the caller marks the answers
 * first and adds this id to whatever they are by then. Throws an Error whose message
 * says what happened.
 */
export async function startCompanyResearch(store, intake, { guideName, username = '', delayMs = READY_DELAY_MS } = {}) {
  const message = researchMessage(intake)
  let name = guideName
  let deployed = false
  for (let attempt = 0; attempt < READY_ATTEMPTS; attempt += 1) {
    try {
      const response = await store.startGuideTask(name, message)
      return researchExecutionId(response)
    } catch (error) {
      const status = error?.response?.status
      if (status === 404 && !deployed) {
        deployed = true
        name = await deployGuide(store, username)
      } else if (status !== 404 && status !== 409 && status !== 503) {
        throw new Error("The research couldn't start. You can carry on, and try again from Findings.")
      }
      await wait(delayMs)
    }
  }
  throw new Error("Your guide isn't ready yet, so the research hasn't started. You can carry on, and try again from Findings.")
}

async function deployGuide(store, username) {
  const { name, failure } = await deployMyGuide(store, username)
  if (failure) throw new Error(failure)
  return name
}
