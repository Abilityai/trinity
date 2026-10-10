const colors = require('tailwindcss/colors')

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
    // `list-wide:` — the Dashboard list's desktop grid (AgentListPanel.vue)
    // switches on the LIST's width, not the window's. Its non-name tracks need
    // about 920px; with the systems rail open (the first-launch default) a
    // viewport `lg:` left the name track ~70px at a 1280 window. At 68rem the
    // name track keeps about 168px; below it the tablet layout renders. 68,
    // not 72: a 1440 window with the rail open leaves 1152px, less a classic
    // scrollbar on Linux, and must still get the grid. The container is the
    // wrapper around the grid.
    //
    // `list-mid:` — the same container, one step down (#3465). The tablet row
    // keeps three reserved toggles, the success bar, the meter and the task
    // counts on one line and needs about 610px; a viewport `md:` alone gave it
    // 496px at a 768 window with the rail open, where the success cell
    // collapsed to 0 and the task counts ran past the row. Below 40rem the
    // compact (phone) row renders instead. 40, not more: a 768 window with the
    // rail collapsed leaves 664px, less a classic scrollbar, and keeps the
    // tablet row. Registered BEFORE `list-wide` so `md:list-wide:hidden` is
    // emitted after `md:list-mid:flex` and wins where both hold.
    function ({ addVariant }) {
      addVariant('list-mid', '@container agent-list (min-width: 40rem)')
      addVariant('list-wide', '@container agent-list (min-width: 68rem)')
    },
  ],
}
