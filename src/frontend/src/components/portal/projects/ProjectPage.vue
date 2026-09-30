<!--
  One project (trinity-enterprise#661): what it is for, where it stands, who is
  on it, and my way back into it.

  Chats are private (ruling 5): only MY linked chats are listed, across every
  agent; other members' chats appear only as a count. "Work on it" reopens my
  newest chat with an agent, or starts one and links it; an agent whose owner
  has not approved it yet, or that is not on my own list, is shown and not
  offered (a hidden agent would be a mystery; a disabled one says why).

  The server answers a missing project and one I can't see identically, so the
  not-found copy never says which it was.
-->
<template>
  <div class="mx-auto w-full max-w-5xl px-4 sm:px-8 py-6" data-testid="project-page">
    <SkeletonLoader v-if="view.state === 'loading'" :count="6" height="3rem" gap="0.75rem" />

    <div v-else-if="page.notFound" class="py-16 text-center" data-testid="project-not-found">
      <p class="text-base font-semibold text-gray-900 dark:text-gray-100">This project isn't available to you</p>
      <p class="mt-1.5 text-sm text-gray-600 dark:text-gray-300">It may have been removed, or you aren't a member. Ask the person who shared the link.</p>
      <BaseButton class="mt-4" variant="secondary" @click="$emit('back')">Back to Projects</BaseButton>
    </div>

    <LoadFailed
      v-else-if="view.state === 'failed'"
      title="Couldn't load this project"
      :message="page.error"
      :retrying="page.loading"
      @retry="load"
    />

    <div v-else class="space-y-6">
      <InlineError v-if="view.stale" :message="page.error" @dismiss="load" />

      <nav class="text-xs text-gray-500 dark:text-gray-400">
        <button type="button" class="hover:underline" @click="$emit('back')">Projects</button>
        <span aria-hidden="true"> / </span><span>{{ p.name }}</span>
      </nav>

      <header class="flex flex-col gap-4 sm:flex-row sm:items-start">
        <div class="min-w-0 flex-1 space-y-2">
          <div class="flex flex-wrap items-center gap-2">
            <h1 class="text-2xl font-bold text-gray-900 dark:text-gray-100 break-words">{{ p.name }}</h1>
            <BaseBadge :variant="STATUS_BADGE[p.status] || 'neutral'" dot>{{ statusLabel(p.status) }}</BaseBadge>
            <BaseBadge>{{ visibilityLabel(p.visibility) }}</BaseBadge>
            <BaseBadge v-if="p.archived_at" variant="locked">Archived</BaseBadge>
          </div>
          <p class="max-w-3xl text-sm text-gray-700 dark:text-gray-300 whitespace-pre-line">{{ p.goal }}</p>
          <p class="flex flex-wrap gap-x-4 gap-y-1 text-xs text-gray-500 dark:text-gray-400">
            <span>Steward · {{ stewardLabel }}</span>
            <span>Created by {{ p.created_by === myEmail ? 'you' : p.created_by }}</span>
            <a v-if="p.tracker_url" :href="p.tracker_url" target="_blank" rel="noopener noreferrer" class="text-action-primary-600 dark:text-action-primary-400 hover:underline">Tracker</a>
            <span :title="absolute(p.updated_at)">Updated {{ relative(p.updated_at) }}</span>
          </p>
        </div>
        <div class="flex flex-wrap items-center gap-2">
          <BaseSelect
            v-if="p.can?.edit && !p.archived_at"
            id="project-status"
            :model-value="p.status"
            variant="ghost"
            aria-label="Status"
            @update:model-value="setStatus"
          >
            <option value="active">Active</option>
            <option value="paused">Paused</option>
            <option value="done">Done</option>
          </BaseSelect>
          <BaseButton variant="secondary" data-testid="project-members-open" @click="membersOpen = true">Members &amp; access</BaseButton>
          <BaseButton
            v-if="p.can?.archive"
            variant="ghost"
            :data-testid="p.archived_at ? 'project-restore' : 'project-archive'"
            @click="p.archived_at ? restore() : (archiveConfirm = true)"
          >{{ p.archived_at ? 'Restore' : 'Archive' }}</BaseButton>
        </div>
      </header>

      <InlineError v-if="actionError" :message="actionError" @dismiss="actionError = ''" />

      <div class="grid gap-4 lg:grid-cols-3">
        <BaseCard class="lg:col-span-2">
          <h2 class="text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">Work on it</h2>
          <p v-if="!options.length" class="mt-2 text-sm text-gray-600 dark:text-gray-300">
            No agent can work on this project yet. {{ p.can?.manage_agents ? 'Add one in Members & access.' : 'Ask its creator to add one.' }}
          </p>
          <ul class="mt-2 divide-y divide-gray-100 dark:divide-gray-750">
            <li v-for="o in options" :key="o.agent" class="flex items-center gap-3 py-2.5 text-sm" :data-testid="`work-on-${o.agent}`">
              <PortalAvatar :name="o.agent" :size="24" />
              <div class="min-w-0 flex-1">
                <p class="font-medium text-gray-900 dark:text-gray-100 truncate">{{ o.agent }}</p>
                <p class="text-xs text-gray-500 dark:text-gray-400">{{ optionNote(o) }}</p>
              </div>
              <BaseButton
                v-if="o.kind === 'reopen' || o.kind === 'new'"
                size="sm"
                :variant="o === primaryOption ? 'primary' : 'secondary'"
                :loading="starting === o.agent"
                loading-label="Opening…"
                @click="workOn(o)"
              >{{ o.kind === 'reopen' ? 'Reopen' : 'New chat' }}</BaseButton>
            </li>
          </ul>
        </BaseCard>

        <BaseCard>
          <h2 class="text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">Members</h2>
          <ul class="mt-2 space-y-1.5 text-sm">
            <li v-for="m in p.members" :key="m.email" class="flex items-center gap-2">
              <span class="min-w-0 flex-1 truncate text-gray-900 dark:text-gray-100">{{ m.email === myEmail ? 'You' : m.email }}</span>
              <BaseBadge>{{ m.role === 'creator' ? 'Creator' : 'Member' }}</BaseBadge>
            </li>
          </ul>
        </BaseCard>

        <BaseCard class="lg:col-span-2">
          <h2 class="text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">My chats on this project</h2>
          <p v-if="!p.my_chats.length" class="mt-2 text-sm text-gray-600 dark:text-gray-300">
            None yet. Use Work on it, or open a chat and choose "Add to project".
          </p>
          <ul class="mt-2 max-h-80 overflow-y-auto divide-y divide-gray-100 dark:divide-gray-750">
            <li v-for="c in p.my_chats" :key="c.id" class="flex items-center gap-3 py-2 text-sm">
              <div class="min-w-0 flex-1">
                <p class="truncate text-gray-900 dark:text-gray-100">{{ c.title || 'Untitled chat' }}</p>
                <p class="text-xs text-gray-500 dark:text-gray-400">
                  {{ c.agent_name }} · <span :title="absolute(c.last_message_at)">{{ relative(c.last_message_at) }}</span>
                </p>
              </div>
              <BaseButton size="sm" variant="secondary" @click="$emit('open-thread', { id: c.id, agent_name: c.agent_name })">Reopen</BaseButton>
            </li>
          </ul>
          <p v-if="p.others_chat_count" class="mt-3 border-t border-gray-100 dark:border-gray-750 pt-3 text-xs text-gray-500 dark:text-gray-400" data-testid="others-chat-count">
            Other members have <span class="tabular-nums">{{ p.others_chat_count }}</span> {{ p.others_chat_count === 1 ? 'chat' : 'chats' }} on this project. Chats are private: only the person who had a chat can see it.
          </p>
        </BaseCard>

        <BaseCard>
          <h2 class="text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">Agents</h2>
          <ul class="mt-2 space-y-1.5 text-sm">
            <li v-for="a in p.agents" :key="a.agent_name" class="flex items-center gap-2">
              <span class="min-w-0 flex-1 truncate text-gray-900 dark:text-gray-100">{{ a.agent_name }}</span>
              <BaseBadge :variant="AGENT_BADGE[a.state] || 'neutral'">{{ agentStateLabel(a.state) }}</BaseBadge>
            </li>
          </ul>
          <h2 class="mt-4 text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">Rooms I'm in</h2>
          <p v-if="!p.rooms.length" class="mt-1 text-sm text-gray-600 dark:text-gray-300">No rooms linked yet.</p>
          <ul class="mt-1 space-y-1.5 text-sm">
            <li v-for="r in p.rooms" :key="r.id">
              <button type="button" class="text-action-primary-600 dark:text-action-primary-400 hover:underline" @click="$emit('open-room', r.id)">{{ r.name || 'Untitled room' }}</button>
            </li>
          </ul>
        </BaseCard>
      </div>
    </div>

    <ProjectMembersModal
      v-if="page.data"
      v-model="membersOpen"
      :project="page.data"
      :my-email="myEmail"
      :roster-agents="rosterAgents"
      :owned-agents="ownedAgents"
    />
    <ConfirmDialog
      v-model:visible="archiveConfirm"
      title="Archive this project?"
      message="It stays readable and its chats stay yours, but nothing new can be linked and agents stop getting its context. You can restore it later."
      confirm-text="Archive"
      cancel-text="Keep it open"
      variant="warning"
      @confirm="archive"
    />
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import BaseBadge from '@/components/base/BaseBadge.vue'
import BaseButton from '@/components/base/BaseButton.vue'
import BaseCard from '@/components/base/BaseCard.vue'
import BaseSelect from '@/components/base/BaseSelect.vue'
import SkeletonLoader from '@/components/SkeletonLoader.vue'
import LoadFailed from '@/components/LoadFailed.vue'
import InlineError from '@/components/InlineError.vue'
import ConfirmDialog from '@/components/ConfirmDialog.vue'
import PortalAvatar from '@/components/portal/PortalAvatar.vue'
import ProjectMembersModal from './ProjectMembersModal.vue'
import { useProjectsStore } from '@/stores/projects'
import { useClientPortalStore } from '@/stores/clientPortal'
import { viewState } from '@/utils/loadingState'
import { formatRelativeTime, formatLocalDateTime } from '@/utils/timestamps'
import {
  STATUS_BADGE, statusLabel, visibilityLabel, agentStateLabel, workOnItOptions, projectErrorMessage,
} from './projectsUtils'

const AGENT_BADGE = { active: 'success', pending: 'warning', declined: 'neutral' }

const props = defineProps({
  projectId: { type: String, required: true },
  myEmail: { type: String, default: '' },
})
const emit = defineEmits(['back', 'open-thread', 'open-room'])

const store = useProjectsStore()
const portal = useClientPortalStore()
const membersOpen = ref(false)
const archiveConfirm = ref(false)
const actionError = ref('')
const starting = ref('')

const page = computed(() => store.pageState(props.projectId))
const view = computed(() => viewState({
  hasLoaded: page.value.loaded, error: page.value.error, count: page.value.data ? 1 : 0,
}))
const p = computed(() => page.value.data || { members: [], agents: [], my_chats: [], rooms: [] })
const rosterAgents = computed(() => portal.agents.map((a) => a.name))
const ownedAgents = computed(() => portal.agents.filter((a) => a.owned).map((a) => a.name))
const options = computed(() => workOnItOptions(p.value, rosterAgents.value))
const primaryOption = computed(() => options.value.find((o) => o.kind === 'reopen' || o.kind === 'new') || null)
const stewardLabel = computed(() => {
  const s = p.value.steward
  if (!s) return 'not set'
  if (s.kind === 'agent') return `${s.ref} (agent)`
  return s.ref === props.myEmail ? 'you' : s.ref
})

function load() { return store.fetchProject(props.projectId) }
watch(() => props.projectId, load, { immediate: true })

const relative = (t) => (t ? formatRelativeTime(t) : '')
const absolute = (t) => (t ? formatLocalDateTime(t) : '')

function optionNote(o) {
  if (o.kind === 'reopen') return `Your chat "${o.chat.title || 'Untitled chat'}" · ${relative(o.chat.last_message_at)}`
  if (o.kind === 'new') return 'Starts a new chat in this project'
  if (o.kind === 'pending') return 'Waiting for its owner to approve'
  if (o.kind === 'archived') return 'The project is archived, so new chats can’t be added'
  return 'Not on your list, so you can’t chat with it. Ask its owner for access.'
}

async function workOn(o) {
  actionError.value = ''
  if (o.kind === 'reopen') {
    emit('open-thread', { id: o.chat.id, agent_name: o.agent })
    return
  }
  starting.value = o.agent
  try {
    const session = await portal.createSession(o.agent)
    const sid = session.id || session.session_id
    await store.link(props.projectId, 'thread', sid)
    emit('open-thread', { id: sid, agent_name: o.agent })
  } catch (err) {
    actionError.value = projectErrorMessage(err)
  } finally {
    starting.value = ''
  }
}

async function guarded(fn) {
  actionError.value = ''
  try { await fn() } catch (err) { actionError.value = projectErrorMessage(err) }
}
const setStatus = (status) => guarded(() => store.update(props.projectId, { status }))
const archive = () => guarded(() => store.setArchived(props.projectId, true))
const restore = () => guarded(() => store.setArchived(props.projectId, false))
</script>
