<template>
  <!-- A row, not a <button>: it holds a second control (the star), and nesting
       an interactive element inside a button is invalid HTML — browsers vary on
       which click wins, so the star would sometimes open the chat instead. -->
  <div
    class="group w-full flex items-center gap-2 rounded-lg px-2.5 py-2 text-left cursor-pointer hover:bg-white dark:hover:bg-gray-900 transition"
    :class="active ? 'bg-white dark:bg-gray-900 ring-1 ring-gray-200 dark:ring-gray-800' : ''"
    role="button"
    tabindex="0"
    @click="$emit('open')"
    @keydown.enter.prevent="$emit('open')"
    @keydown.space.prevent="$emit('open')"
  >
    <!-- ent#359: a multi-agent chat shows every participant. One avatar for a
         room of three is the same row a 1:1 draws, which is exactly the
         distinction the sidebar has to make visible. -->
    <span class="shrink-0 flex items-center" :class="avatars.shown.length > 1 ? '-space-x-1.5' : ''">
      <PortalAvatar
        v-for="n in avatars.shown"
        :key="n"
        :name="n"
        :avatar-url="avatarFor(n)"
        :size="22"
        class="ring-2 ring-gray-50 dark:ring-gray-950 rounded-full"
      />
      <!-- The +N chip is hand-rolled rather than a PortalAvatar, so it does not
           inherit that component's edge (#2169). Without this line a 4-agent row
           draws three hairlined circles and one bare blob — and in light theme
           the chip's own gray-200 fill would otherwise sit flush against the
           gray-50 ground. Same `border-strong` recipe, class-sized and
           border-box, so it stays 22px and `-space-x-1.5` is unaffected. In dark
           the border matches the gray-700 fill and reads as a solid disc, which
           is correct: there the chip is already separated from the gray-950
           ground by its fill, and one recipe beats a second per-component one. -->
      <span
        v-if="avatars.overflow"
        class="w-[22px] h-[22px] rounded-full border border-gray-300 dark:border-gray-700 bg-gray-200 dark:bg-gray-700 text-[10px] font-semibold text-gray-600 dark:text-gray-300 flex items-center justify-center ring-2 ring-gray-50 dark:ring-gray-950"
        :title="allAgentNames"
      >+{{ avatars.overflow }}</span>
    </span>

    <!-- ent#473: the title is renameable in place. The editor stops its own
         clicks and keys, so the row's open handlers never fire from inside it;
         without a `rename` function (search results, a read-only caller) it
         is a plain span. -->
    <PortalEditableTitle
      v-if="rename"
      dense
      :value="rawTitle"
      placeholder="New chat"
      :rename="(t) => rename(thread, t)"
      :text-class="unread ? 'text-sm font-semibold' : 'text-sm'"
      label="Rename this chat"
    />
    <span v-else class="text-sm truncate flex-1" :class="unread ? 'font-semibold' : ''">{{ title }}</span>

    <span
      v-if="unread"
      class="shrink-0 min-w-[1.125rem] px-1 h-[1.125rem] rounded-full bg-action-primary-600 text-white text-[10px] font-semibold flex items-center justify-center"
    >{{ unread > 99 ? '99+' : unread }}</span>

    <PortalStarButton
      :starred="!!thread.starred"
      dense
      reveal-on-hover
      @toggle="$emit('toggle-star')"
    />
  </div>
</template>

<script setup>
/**
 * One chat row in the Workspace sidebar (ent#359) — a thread or a room.
 *
 * Extracted from PortalSidebar because the starred section and each date group
 * render the identical row, and three copies of a row that carries a star
 * toggle, an unread badge and stacked avatars is three places for them to drift.
 */
import { computed } from 'vue'
import PortalAvatar from './PortalAvatar.vue'
import PortalStarButton from './PortalStarButton.vue'
import PortalEditableTitle from './PortalEditableTitle.vue'
import { rowAgents, threadTitle } from './portalUtils'

const props = defineProps({
  thread: { type: Object, required: true },
  active: { type: Boolean, default: false },
  avatarFor: { type: Function, default: () => null },
  // ent#473: async (thread, title) => void, or null for a read-only row.
  rename: { type: Function, default: null },
})
defineEmits(['open', 'toggle-star'])

const avatars = computed(() => rowAgents(props.thread))
const allAgentNames = computed(() => (props.thread.agent_names || []).join(', '))
const unread = computed(() => Number(props.thread.unread) || 0)
const title = computed(() => threadTitle(props.thread))
// The stored title itself — the editor pre-fills from it and shows the
// placeholder when it is empty, never the fallback word as a draft.
const rawTitle = computed(() => (props.thread.title || '').trim())
</script>
