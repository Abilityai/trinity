// "What's an agent?": a short video of one everyday job done by an agent. The
// words live here, apart from the component, so a test can read them without a browser.
// The transcript is the video's captions, word for word: change them together
// (intro-video/src/WhatIsAnAgent.tsx, AGENT_CAPTIONS; render with render-agent.sh).

const JOB = 'Email Nick to send an invoice to our latest client.'

const VIDEO = Object.freeze({ src: '/intro/what-is-an-agent.mp4', poster: '/intro/what-is-an-agent-poster.jpg' })

const TRANSCRIPT = Object.freeze([
  'You give an agent a job, in one line.',
  "It finds Nick's address in your CRM.",
  'It reads your recent emails with him.',
  'It looks up your latest client.',
  'It drafts the email in your tone, because you told it once how you write.',
  'You check it and send.',
  'It updates the CRM and remembers it happened.',
  'An agent is Claude with your tools, instructions you give it once, and a memory.',
])

const SUMMARY = 'An agent is Claude with access to your tools, clear instructions you give it once, and a memory.'
// A developer gets the same video, with the summary in their words.
const SUMMARY_DEVELOPER = 'An agent is Claude with tools (MCP servers and API keys), instructions it keeps (its CLAUDE.md and playbooks), and memory that lasts between runs. On Trinity, each agent runs in its own container.'

/** The screen's words and video, from their software answer. */
export function agentExplainer(software = '') {
  return {
    title: "What's an agent?",
    lead: `One everyday job, done by an agent: "${JOB}"`,
    video: VIDEO,
    transcript: TRANSCRIPT,
    summary: software === 'developer' ? SUMMARY_DEVELOPER : SUMMARY,
  }
}
