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
      colors: {
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
      },
    },
  },
  plugins: [
    require('@tailwindcss/typography'),
  ],
}
