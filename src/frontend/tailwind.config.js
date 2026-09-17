const colors = require('tailwindcss/colors')
const plugin = require('tailwindcss/plugin')

/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{vue,js,ts,jsx,tsx}",
  ],
  darkMode: 'class',
  theme: {
    extend: {
      // Design-system tokens — each aliases a full Tailwind palette so every
      // shade remains available (e.g. `bg-status-success-500`,
      // `text-state-autonomous-700 dark:text-state-autonomous-300`).
      //
      //   status-*  health/result of an event (success, warning, danger, …)
      //   state-*   an operating mode (autonomous, locked, …)
      //   brand-*   third-party product identity (claude, gemini, …)
      //   accent-*  decorative highlight that isn't status (named after the
      //             literal color so future accents like `accent-green` join
      //             cleanly without renaming).
      //   action-*  interactive surface that performs a verb (primary buttons,
      //             links, focus rings).
      colors: {
        gray: { ...colors.gray, 750: 'rgb(42, 48, 60)' },
        'status-success':    colors.green,
        'status-warning':    colors.yellow,
        'status-danger':     colors.red,
        'status-info':       colors.blue,
        'status-urgent':     colors.orange,
        'state-autonomous':  colors.amber,
        'state-locked':      colors.rose,
        'brand-claude':      colors.orange,
        'brand-gemini':      colors.blue,
        'accent-purple':     colors.purple,
        'action-primary':    colors.indigo,
      },
      // The dark-theme tinted-ground recipe is `token-500 at 16%`
      // (design-system.md §5 — badges, alerts, accent-soft). 16 is not in
      // Tailwind's default opacity scale, so `/16` classes silently emitted
      // NOTHING until this entry made the documented recipe expressible.
      opacity: {
        16: '.16',
      },
    },
  },
  plugins: [
    require('@tailwindcss/typography'),
    // `list-lg:` — the dashboard List pane's own width, not the viewport's
    // (AgentListPanel.vue is the `agent-list` container). 68rem is the
    // ten-track row grid's fixed tracks and gaps plus a name track that still
    // shows a system agent's name and badge.
    plugin(({ addVariant }) => {
      addVariant('list-lg', '@container agent-list (min-width: 68rem)')
    }),
  ],
}
