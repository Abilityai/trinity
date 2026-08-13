<template>
  <button
    type="button"
    class="shrink-0 rounded transition"
    :class="[
      dense ? 'p-0.5' : 'p-2 hover:bg-gray-100 dark:hover:bg-gray-800',
      starred
        ? 'text-amber-500'
        : revealOnHover
          ? 'text-gray-300 dark:text-gray-600 opacity-100 sm:opacity-0 sm:group-hover:opacity-100 focus:opacity-100 hover:text-amber-500'
          : 'text-gray-400 hover:text-amber-500',
    ]"
    :title="starred ? 'Unstar this chat' : 'Star this chat'"
    :aria-label="starred ? 'Unstar this chat' : 'Star this chat'"
    :aria-pressed="starred"
    @click.stop="$emit('toggle')"
  >
    <svg
      :class="dense ? 'w-4 h-4' : 'w-5 h-5'"
      :fill="starred ? 'currentColor' : 'none'"
      viewBox="0 0 24 24"
      stroke="currentColor"
    >
      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M11.48 3.5a.56.56 0 011.04 0l2.13 4.82 5.24.53c.48.05.67.65.31.97l-3.94 3.5 1.12 5.16c.1.47-.4.84-.82.6L12 16.5l-4.56 2.58c-.42.24-.92-.13-.82-.6l1.12-5.16-3.94-3.5c-.36-.32-.17-.92.31-.97l5.24-.53 2.13-4.82z" />
    </svg>
  </button>
</template>

<script setup>
/**
 * The star toggle (ent#359) — one component for its three homes: the sidebar
 * row, the 1:1 chat header, and the room header.
 *
 * AC #4 asks for star/unstar from BOTH the header and the row, which is exactly
 * the setup where an inline copy per site drifts: three icons, three hover
 * rules, three `aria-pressed` states to keep honest.
 *
 * `revealOnHover` is for the dense sidebar row, where a permanently visible
 * outline star on every row is noise. It applies only when NOT starred — a star
 * that is set is state, and state that appears only on hover is state the user
 * cannot see.
 *
 * And it applies only from `sm:` up. There is no hover on a touch screen, so
 * hiding the control behind one would have left mobile users able to UNstar
 * (that star is always drawn) but never to star — the feature reachable in
 * exactly one direction. Below `sm` the outline star is simply always visible.
 */
defineProps({
  starred: { type: Boolean, default: false },
  dense: { type: Boolean, default: false },
  revealOnHover: { type: Boolean, default: false },
})
defineEmits(['toggle'])
</script>
