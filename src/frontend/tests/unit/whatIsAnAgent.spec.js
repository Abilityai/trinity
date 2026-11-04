import { describe, expect, it } from 'vitest'
import { agentExplainer } from '../../src/components/onboarding/whatIsAnAgent'

// The captions burnt into the video, in order. The page shows them as its transcript,
// so they must match the video exactly (intro-video/src/WhatIsAnAgent.tsx, AGENT_CAPTIONS).
const CAPTIONS = [
  'You give an agent a job, in one line.',
  "It finds Nick's address in your CRM.",
  'It reads your recent emails with him.',
  'It looks up your latest client.',
  'It drafts the email in your tone, because you told it once how you write.',
  'You check it and send.',
  'It updates the CRM and remembers it happened.',
  'An agent is Claude with your tools, instructions you give it once, and a memory.',
]

describe("What's an agent?", () => {
  it('is a short video of one everyday job done by an agent, not a side-by-side comparison', () => {
    const e = agentExplainer('none')
    expect(e.title).toBe("What's an agent?")
    expect(e.video).toEqual({ src: '/intro/what-is-an-agent.mp4', poster: '/intro/what-is-an-agent-poster.jpg' })
    expect(e.columns).toBeUndefined()
    expect(e.lead).toBe('One everyday job, done by an agent: "Email Nick to send an invoice to our latest client."')
  })

  it('has a transcript that matches the video word for word', () => {
    expect(agentExplainer('none').transcript).toEqual(CAPTIONS)
  })

  it('keeps the one-line summary, and a developer gets it in more technical words', () => {
    expect(agentExplainer('none').summary).toBe('An agent is Claude with access to your tools, clear instructions you give it once, and a memory.')
    const dev = agentExplainer('developer')
    expect(dev.transcript).toEqual(CAPTIONS)
    expect(dev.summary).toMatch(/MCP/)
  })

  it('says nothing a newcomer would not understand', () => {
    const words = JSON.stringify(agentExplainer('none'))
    expect(words).not.toMatch(/MCP|deploy|scaffold|repo/i)
    expect(words).not.toMatch(/—/)
  })
})
