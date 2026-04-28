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
      // Semantic status tokens — alias full palettes so every shade remains
      // available (e.g. `bg-status-success-500`, `text-status-success-700
      // dark:text-status-success-400`).
      colors: {
        'status-success': colors.green,
        'status-warning': colors.yellow,
        'status-danger':  colors.red,
        'status-info':    colors.blue,
        'status-urgent':  colors.orange,
      },
    },
  },
  plugins: [
    require('@tailwindcss/typography'),
  ],
}
