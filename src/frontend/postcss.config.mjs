export default {
  plugins: {
    // Tailwind v4: the PostCSS plugin moved to its own package and now
    // handles @import inlining and vendor prefixing internally, so the
    // standalone postcss-import / autoprefixer plugins are no longer wired.
    '@tailwindcss/postcss': {},
  },
}
