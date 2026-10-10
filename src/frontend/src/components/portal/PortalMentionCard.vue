<!--
  trinity-enterprise#631 — a tag, opened in the Inbox's reading pane.

  A person tagged you in a chat or a room. What you see is decided by the
  SERVER and only by it: the tagged message with a little around it when you
  could already see that conversation, and otherwise no content at all — the
  card says you can't see it and names who can let you in. A tag grants no
  access, so nothing here is fetched from the conversation itself.

  Honest state (principle 15): a skeleton until the item's read answers,
  `LoadFailed` (with retry) when it fails, then the item. It says `rendered`
  once the item is on screen — the container marks it read only then, so a
  failed load leaves it Unread.

  In-app only, said plainly: the item lives in this Inbox; nothing is emailed.
-->
<template>
  <div data-testid="inbox-mention">
    <div v-if="view.state === 'loading'" class="space-y-3" aria-busy="true" data-testid="inbox-mention-loading">
      <div v-for="i in 3" :key="i" class="animate-pulse motion-reduce:animate-none h-12 rounded-lg bg-gray-100 dark:bg-gray-800/60"></div>
      <span class="sr-only">Loading this mention…</span>
    </div>
    <LoadFailed
      v-else-if="view.state === 'failed'"
      dense
      title="Couldn't load this mention"
      message="It is still in your Inbox — try again in a moment."
      :retrying="busy"
      data-testid="inbox-mention-failed"
      @retry="load"
    />
    <template v-else>
      <p class="text-sm text-gray-900 dark:text-gray-100" data-testid="inbox-mention-who">
        <span class="font-medium">{{ detail.tagged_by || 'Someone' }}</span>
        mentioned you {{ where }}
        <span class="text-gray-500 dark:text-gray-400"> · <span class="tabular-nums" :title="absolute(detail.created_at)">{{ relative(detail.created_at) }}</span></span>
      </p>

      <!-- The reader may see the conversation: the tagged message and its
           neighbours, the tagged one marked by an edge AND a label. -->
      <template v-if="detail.can_see">
        <ol class="mt-4 space-y-3" aria-label="The message you were tagged in, with what surrounds it" data-testid="inbox-mention-context">
          <li
            v-for="m in detail.context"
            :key="m.id"
            class="rounded-lg px-3 py-2"
            :class="m.tagged
              ? 'border border-action-primary-500 dark:border-action-primary-400 bg-white dark:bg-gray-800'
              : 'border border-transparent'"
            :data-testid="m.tagged ? 'inbox-mention-tagged' : `inbox-mention-message-${m.id}`"
          >
            <p class="mb-1 text-[12.5px] text-gray-600 dark:text-gray-300">
              {{ m.sender_label }}
              <span v-if="m.tagged" class="font-medium text-action-primary-600 dark:text-action-primary-400"> · tagged you here</span>
            </p>
            <PortalAgentBubble v-if="m.sender_kind === 'agent'" :content="m.content || ''" :copyable="false" />
            <p v-else class="text-sm leading-relaxed whitespace-pre-wrap text-gray-900 dark:text-gray-100" :class="BUBBLE_WRAP_CLASS">{{ m.content }}</p>
          </li>
        </ol>
        <div v-if="openTarget" class="mt-4">
          <BaseButton variant="primary" size="sm" data-testid="inbox-mention-open" @click="$emit('open-chat', openTarget)">Open the room</BaseButton>
        </div>
      </template>

      <!-- The reader cannot see it: say so, and who can let them in. No
           content — not even the tagger's own words. -->
      <BaseCard v-else class="mt-4" data-testid="inbox-mention-cant-see">
        <p class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ cantSee.title }}</p>
        <p class="mt-1 text-[12.5px] text-gray-600 dark:text-gray-300">{{ cantSee.body }}</p>
      </BaseCard>

      <p class="mt-4 text-[12.5px] text-gray-500 dark:text-gray-400" data-testid="inbox-mention-reach">
        Tags reach you here, in your Trinity Inbox — no email is sent.
      </p>
    </template>
  </div>
</template>

<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import BaseButton from '@/components/base/BaseButton.vue'
import BaseCard from '@/components/base/BaseCard.vue'
import LoadFailed from '@/components/LoadFailed.vue'
import PortalAgentBubble from './PortalAgentBubble.vue'
import { BUBBLE_WRAP_CLASS } from './portalBubble'
import { useClientPortalStore } from '@/stores/clientPortal'
import { relativeTime } from './portalUtils'
import { cantSeeText, mentionOpenTarget, mentionWhere } from './portalMentions'
import { viewState } from '@/utils/loadingState'
import { formatLocalDateTime } from '@/utils/timestamps'

const props = defineProps({
  // The Inbox item (`portalInbox.mentionItem`): its `mention` is the list row.
  item: { type: Object, required: true },
})
const emit = defineEmits(['rendered', 'open-chat'])

const store = useClientPortalStore()
const detail = ref(null)
const failed = ref(false)
const busy = ref(false)
let gen = 0

const view = computed(() => viewState({ hasLoaded: !!detail.value, error: failed.value }))
const where = computed(() => mentionWhere(detail.value || props.item.mention))
const cantSee = computed(() => cantSeeText(detail.value))
const openTarget = computed(() => mentionOpenTarget(detail.value))

async function load() {
  const mine = ++gen
  busy.value = true
  failed.value = false
  try {
    const data = await store.openMention(props.item.id)
    if (mine !== gen) return
    detail.value = data
    await nextTick()
    emit('rendered', props.item.key)
  } catch {
    if (mine === gen) failed.value = true
  } finally {
    if (mine === gen) busy.value = false
  }
}
watch(() => props.item.key, () => { detail.value = null; load() }, { immediate: true })

const relative = (iso) => relativeTime(iso)
let zone = ''
try { zone = Intl.DateTimeFormat().resolvedOptions().timeZone || '' } catch { zone = '' }
const absolute = (iso) => (iso ? `${formatLocalDateTime(iso)}${zone ? ` (${zone})` : ''}` : undefined)

defineExpose({ reload: load })
</script>
