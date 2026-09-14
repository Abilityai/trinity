<template>
  <!-- ent#556: the Workspace's brand corner — Trinity's mark and the product's
       name, in the two places that must agree (the signed-in shell and the
       sign-in screen). ONE component rather than two copies of the markup:
       "both surfaces carry the same mark and wording" is an acceptance
       criterion, and two copies is how that stops being true.

       `is` picks the element: a `router-link` where there is somewhere true to
       go, a plain `div` where there is not (signed out, the mark has no
       destination that would mean anything). Never a dead affordance, and
       never a platform route — see `WORKSPACE_ROOT`. -->
  <component
    :is="to ? 'router-link' : 'div'"
    :to="to || undefined"
    class="flex items-center gap-2 min-w-0"
    :class="to ? 'hover:opacity-80 transition-opacity' : null"
    :aria-label="WORKSPACE_BRAND_NAME"
  >
    <!-- The light/dark swap follows `components/NavBar.vue` rather than
         inventing a second pattern — a dark mark on a dark ground is the one
         failure this must not have. Both images are decorative: the accessible
         name is on the element above, so a screen reader says the product once,
         and says it whether or not the wordmark is visible at this width. -->
    <img
      src="../../assets/trinity-logo.svg"
      alt=""
      aria-hidden="true"
      :class="[markSize, 'shrink-0 dark:hidden']"
    />
    <img
      src="../../assets/trinity-logo-white.svg"
      alt=""
      aria-hidden="true"
      :class="[markSize, 'shrink-0 hidden dark:block']"
    />
    <!-- `truncate` + `min-w-0` are load-bearing, not tidiness: the sidebar is
         resizable down to `SIDEBAR_MIN` (200px, ent#492) and this row also
         carries the ask and unread badges. Without them the name pushes the
         badges out of the band. -->
    <span :class="[textSize, 'font-semibold truncate min-w-0']">{{ WORKSPACE_BRAND_NAME }}</span>
  </component>
</template>

<script setup>
import { WORKSPACE_BRAND_NAME } from './portalBrand'

defineProps({
  /** Route to navigate to, or null for an inert mark (the signed-out screen). */
  to: { type: [String, Object], default: null },
  /**
   * The mark's box. Defaults to the size the generic icon it replaces used, so
   * the `h-14` header band ent#547 is shortening does not grow.
   */
  markSize: { type: String, default: 'h-6 w-6' },
  /** Type scale for the wordmark; the shell's default, larger on sign-in. */
  textSize: { type: String, default: '' },
})
</script>
