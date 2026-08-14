<template>
  <!-- The edge (#2169). Gray is not a lapse from semantic tokens: the design
       contract has no semantic token for a neutral edge — `status-*`/`action-*`
       would claim this decoration reports a result or affords a click — and it
       names this exact pair, `border-strong gray-300/gray-700`, as the shade for
       separation. Do NOT "fix" it to a token, and do not soften it to the
       lighter `border gray-200/gray-750`: measured against the sidebar ground
       (gray-50) that is 1.19:1, an arc too faint to see at 26px, and the light
       theme with a light-edged image avatar is precisely the case reported.
       `border-strong` is 1.41:1 there.

       `border`, not a ring: an inset ring paints below child content and the
       <img> is exactly the padding box, so `ring-inset` is invisible on image
       avatars — inverted from the case this fixes — and an outer ring collides
       with the `ring-2` separator PortalChatRow already passes in (all Tailwind
       rings share one box-shadow). Border and ring are independent properties.
       box-sizing is border-box, so the outer footprint is unchanged at every
       size and the image insets by 1px rather than clipping. -->
  <span
    class="inline-flex shrink-0 items-center justify-center rounded-full overflow-hidden border border-gray-300 dark:border-gray-700 text-white font-semibold select-none"
    :style="{ width: size + 'px', height: size + 'px', background: showImage ? 'transparent' : tint, fontSize: Math.round(size * 0.4) + 'px' }"
    :title="name"
  >
    <img
      v-if="showImage"
      :src="avatarUrl"
      :alt="name"
      class="w-full h-full object-cover"
      @error="imgOk = false"
    />
    <template v-else>{{ initials }}</template>
  </span>
</template>

<script setup>
import { ref, computed, watch } from 'vue'
import { agentColor, initials as toInitials } from './portalUtils'

const props = defineProps({
  name: { type: String, default: '' },
  avatarUrl: { type: String, default: null },
  size: { type: Number, default: 40 },
})

const imgOk = ref(true)
watch(() => props.avatarUrl, () => { imgOk.value = true })

// An <img> only renders when there IS a URL and it hasn't errored. The tint must
// key off the SAME condition: `imgOk` alone starts true and only flips on an
// @error that can never fire without an <img>, so an avatar-less agent rendered
// white initials on a transparent background (ent#186).
const showImage = computed(() => Boolean(props.avatarUrl) && imgOk.value)
const initials = computed(() => toInitials(props.name))
const tint = computed(() => agentColor(props.name))
</script>
