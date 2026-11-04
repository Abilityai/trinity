// What the Workspace tour says, and where its card goes. Kept apart from the component
// so a test can read both without a browser.

export function tourSteps(guideName = 'Your guide') {
  return [
    {
      target: 'agents',
      title: 'These are your agents',
      body: `${guideName} is yours: it helps you find the best agents to build, and builds them with you. To work with an agent, simply chat to it.`,
    },
    {
      target: 'chat',
      title: 'This is where you talk',
      body: `You're chatting with ${guideName} here. Say what you need in your own words. When it's time to build, ${guideName} builds it with you, right here.`,
    },
    {
      target: 'company',
      title: 'This is your company page',
      body: 'It fills in as you talk. "Your company" shows your teams and agents, "Findings" has the company research and the audit results, and "Context" is what every agent will know about you.',
    },
    {
      target: 'inbox',
      title: 'This is your Inbox',
      body: 'When an agent needs a decision or your approval, it asks you here. Agents wait for your answer before doing anything risky.',
    },
    {
      target: 'help',
      title: 'You can always get help',
      body: `Ask ${guideName} first. If you're stuck, this reaches the team, and you can take this tour again from there.`,
    },
  ]
}

const GAP = 14
const EDGE = 16

/** Beside the lit part where there's room, else below or above it, always on screen. */
export function tourCardPosition(rect, card, screen) {
  const fit = (value, size, max) => Math.max(EDGE, Math.min(value, max - size - EDGE))
  const top = fit(rect.top, card.height, screen.height)
  const left = fit(rect.left, card.width, screen.width)
  if (rect.left + rect.width + GAP + card.width + EDGE <= screen.width) {
    return { left: `${rect.left + rect.width + GAP}px`, top: `${top}px` }
  }
  if (rect.left - GAP - card.width >= EDGE) {
    return { left: `${rect.left - GAP - card.width}px`, top: `${top}px` }
  }
  if (rect.top + rect.height + GAP + card.height + EDGE <= screen.height) {
    return { left: `${left}px`, top: `${rect.top + rect.height + GAP}px` }
  }
  return { left: `${left}px`, top: `${fit(rect.top - GAP - card.height, card.height, screen.height)}px` }
}
