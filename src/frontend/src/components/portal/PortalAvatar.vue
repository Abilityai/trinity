<template>
  <span
    class="inline-flex shrink-0 items-center justify-center rounded-full overflow-hidden text-white font-semibold select-none"
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
