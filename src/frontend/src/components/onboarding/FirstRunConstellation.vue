<!--
  The first-run constellation (ent#581 spec comment 3, §1) — `/setup`'s hero
  motif, quietened and made theme-aware. Used on the welcome and done steps.

  The four outer nodes ARE the four capabilities the sequence configures:
  hollow = unconfigured, lit = done. On the last panel the skipped ones are
  still hollow, so the motif that is pure atmosphere on `/setup` becomes a
  progress report here — the only thing that justifies a decorative element in
  a setup flow. Colour is `theme()`-derived only, never a literal.
-->
<template>
  <div class="first-run-constel text-action-primary-600 dark:text-action-primary-500" aria-hidden="true">
    <div class="c-orbit c-o1"></div>
    <div class="c-orbit c-o2">
      <div
        v-for="(slot, i) in 4"
        :key="i"
        class="c-slot"
        :class="[`c-s${i + 1}`, { lit: lit.includes(i + 1) }]"
      ><i></i></div>
    </div>
    <div class="c-halo"></div>
    <div class="c-core"><TrinityMark /></div>
  </div>
</template>

<script setup>
import TrinityMark from '../TrinityMark.vue'

// Which capability nodes are lit, 1-4, in registry order:
// 1 secure · 2 email/keys · 3 claude · 4 sharing (`litNodes` in firstRunSteps.js).
defineProps({ lit: { type: Array, default: () => [] } })
</script>

<style scoped>
.first-run-constel { position: relative; flex: none; width: 196px; height: 196px; }
@media (max-width: 640px) { .first-run-constel { width: 150px; height: 150px; margin-bottom: 6px; } }

.c-core {
  position: absolute; top: 50%; left: 50%; width: 54px; height: 54px; margin: -27px 0 0 -27px;
  border-radius: 50%; display: grid; place-items: center;
  background: radial-gradient(circle at 34% 28%,
              theme('colors.action-primary.500 / 20%'), transparent 74%);
  box-shadow: 0 0 0 1.25px theme('colors.action-primary.500 / 55%'),
              0 0 30px theme('colors.action-primary.500 / 40%');
  animation: c-core-pulse 4.2s ease-in-out infinite;
}
.c-core :deep(svg) { width: 30px; height: 30px; fill: currentColor; opacity: .92; }
@keyframes c-core-pulse {
  0%, 100% { box-shadow: 0 0 0 1.25px theme('colors.action-primary.500 / 55%'),
                         0 0 24px theme('colors.action-primary.500 / 40%'); }
  50%      { box-shadow: 0 0 0 1.25px theme('colors.action-primary.500 / 55%'),
                         0 0 46px theme('colors.action-primary.500 / 40%'); }
}

.c-halo {
  position: absolute; top: 50%; left: 50%; width: 54px; height: 54px; margin: -27px 0 0 -27px;
  border-radius: 50%; border: 1.25px solid currentColor; opacity: .6;
  animation: c-halo 3.8s ease-out infinite;
}
@keyframes c-halo { 0% { opacity: .45; transform: scale(1); } 100% { opacity: 0; transform: scale(2.5); } }

.c-orbit {
  position: absolute; top: 50%; left: 50%; border-radius: 50%;
  border: 1px solid currentColor; opacity: .34; transform: translate(-50%, -50%);
}
.c-o1 { width: 104px; height: 104px; border-style: dashed; animation: c-spin 30s linear infinite reverse; }
.c-o2 { width: 176px; height: 176px; animation: c-spin 52s linear infinite; }
@keyframes c-spin { to { transform: translate(-50%, -50%) rotate(360deg); } }

.c-slot { position: absolute; inset: 0; }
.c-slot > i {
  position: absolute; top: -5.5px; left: 50%; margin-left: -5.5px; width: 11px; height: 11px;
  border-radius: 50%; border: 1.5px solid currentColor; opacity: .62; display: block;
  background: theme('colors.white');
}
.dark .c-slot > i { background: theme('colors.gray.800'); }
.c-slot.lit > i {
  opacity: 1; background: currentColor;
  box-shadow: 0 0 12px theme('colors.action-primary.500 / 40%');
}
.c-s1 { transform: rotate(28deg); }  .c-s2 { transform: rotate(118deg); }
.c-s3 { transform: rotate(208deg); } .c-s4 { transform: rotate(298deg); }

/* Contract: prefers-reduced-motion handled on any animation touched. */
@media (prefers-reduced-motion: reduce) {
  .c-o1, .c-o2, .c-core, .c-halo { animation: none; }
  .c-halo { opacity: 0; }
}
</style>
