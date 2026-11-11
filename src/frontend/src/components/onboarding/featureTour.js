// The tour of Trinity's features on the Dashboard, for people who build agents all the
// time. It uses the Workspace tour's component, so each stop points at one part of the
// screen and a part that isn't on screen is left out.

import { experienceRoute } from '../portal/builder/portalBuild'

export function featureTourSteps() {
  return [
    {
      target: 'view-modes',
      title: 'Three views of your agents',
      body: 'Timeline shows your agents working over time, including when one calls another. Grid and List line them up. Open any agent for its schedules, its channels like Slack and WhatsApp, which agents it may call and its public chat link.',
    },
    {
      target: 'nav-operations',
      title: 'Operations',
      body: 'Approvals your agents are waiting on, every run with its cost, and the health of the whole fleet. Agents stop here for your sign-off before anything risky.',
    },
    {
      target: 'nav-library',
      title: 'Library',
      body: 'Skills and templates. Give any agent a new skill, or start a new agent from a template.',
    },
    {
      target: 'nav-workspace',
      title: 'Workspace',
      body: 'Chat with any agent, share agents with your team, and plan new ones with Atlas.',
    },
    {
      target: 'nav-settings',
      title: 'Settings',
      body: 'Keys and integrations for the whole instance, who can sign in, and API keys for connecting other tools.',
    },
  ]
}

/** Once, for someone who builds agents all the time and has finished setup. */
export function featureTourDue(intake) {
  return Boolean(intake?.done && !intake.featureTourSeen && experienceRoute(intake.answers) === 'experienced')
}

export function markFeatureTourSeen(intake) {
  return { ...intake, featureTourSeen: true }
}
