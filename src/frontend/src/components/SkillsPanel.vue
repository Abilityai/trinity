<template>
  <div class="space-y-5 bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
    <div class="flex items-start justify-between gap-4">
      <div>
        <h2 class="text-lg font-semibold text-gray-900 dark:text-gray-100">Skills</h2>
        <p class="mt-1 text-sm text-gray-500 dark:text-gray-400 max-w-2xl">
          Assign skills from the shared library to this agent. Assignment is durable;
          the files are copied into the agent on start, or when you sync below.
        </p>
      </div>
      <button
        v-if="canManage && !store.loading"
        @click="onSync"
        :disabled="store.injecting || !agentRunning"
        :title="agentRunning ? 'Re-copy every assigned skill into the agent' : 'The agent is stopped — start it to sync skills'"
        :class="[
          'shrink-0 px-3 py-1.5 rounded-lg text-sm disabled:opacity-50 disabled:cursor-not-allowed',
          syncNeedsAttention
            ? 'bg-action-primary-600 hover:bg-action-primary-700 text-white font-medium ring-2 ring-action-primary-300 dark:ring-action-primary-700'
            : 'border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800'
        ]"
      >{{ store.injecting ? 'Syncing…' : 'Sync now' }}</button>
    </div>

    <p v-if="store.error" class="text-sm text-status-danger-600 dark:text-status-danger-400">{{ store.error }}</p>
    <p v-if="store.loading" class="text-sm text-gray-500 dark:text-gray-400">Loading skills…</p>

    <!-- Empty states: each says what is wrong AND what to do next (AC: no dead
         empty states). Which one shows is decided by the store, so the panel
         can't invent a fourth. -->
    <template v-else-if="store.emptyReason === 'library_unconfigured'">
      <div class="rounded-lg border border-gray-200 dark:border-gray-700 p-5 text-sm">
        <p class="font-medium text-gray-900 dark:text-gray-100">No skills library is configured</p>
        <p class="mt-1 text-gray-500 dark:text-gray-400">
          Skills come from a git repository shared across the fleet. Once it's configured,
          every agent can be assigned skills from it.
        </p>
        <router-link v-if="isAdmin" to="/settings?tab=agents"
                     class="mt-3 inline-block px-3 py-1.5 rounded-lg bg-action-primary-600 hover:bg-action-primary-700 text-white text-sm">
          Configure the library
        </router-link>
        <p v-else class="mt-3 text-gray-500 dark:text-gray-400">Ask an admin to configure it in Settings.</p>
      </div>
    </template>

    <template v-else-if="store.emptyReason === 'library_empty'">
      <div class="rounded-lg border border-gray-200 dark:border-gray-700 p-5 text-sm">
        <p class="font-medium text-gray-900 dark:text-gray-100">The library is configured but has no skills yet</p>
        <p class="mt-1 text-gray-500 dark:text-gray-400">
          Add a skill directory to the repository, then re-sync the library.
          <span v-if="store.libraryStatus?.url" class="block mt-1 font-mono text-xs break-all">{{ store.libraryStatus.url }}</span>
        </p>
      </div>
    </template>

    <template v-else-if="!store.loading">
      <!-- Assigned, with the honest per-skill outcome of the last sync -->
      <section>
        <h3 class="text-sm font-semibold text-gray-900 dark:text-gray-100">
          Assigned to this agent
          <span class="ml-1 text-xs font-normal text-gray-400">{{ store.assigned.length }}</span>
        </h3>

        <p v-if="store.emptyReason === 'none_assigned'" class="mt-2 text-sm text-gray-500 dark:text-gray-400">
          No skills assigned yet — pick some from the library below and save.
        </p>

        <ul v-else class="mt-2 space-y-2">
          <li v-for="s in store.assignedSkills" :key="s.name"
              class="rounded-lg border border-gray-200 dark:border-gray-700 px-4 py-3">
            <div class="flex items-start justify-between gap-3">
              <div class="min-w-0">
                <div class="flex items-center gap-2 flex-wrap">
                  <span class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ s.name }}</span>
                  <span v-if="s.version" class="text-[11px] font-mono text-gray-400">{{ s.version.slice(0, 7) }}</span>
                  <!-- The injection verdict. Absent = never synced this session,
                       which is stated rather than shown as success. -->
                  <span v-if="resultFor(s.name)"
                        class="text-[11px] px-1.5 py-0.5 rounded-full font-medium"
                        :class="statusClass(resultFor(s.name).status)">
                    {{ statusLabel(resultFor(s.name).status) }}
                  </span>
                </div>
                <p v-if="s.description" class="mt-1 text-xs text-gray-500 dark:text-gray-400">{{ s.description }}</p>

                <!-- Named warnings, verbatim and translated. A skill that landed
                     with a missing binary is NOT a success, and this is the line
                     that says so. -->
                <ul v-if="resultFor(s.name)?.warnings?.length" class="mt-2 space-y-1">
                  <li v-for="w in resultFor(s.name).warnings" :key="w"
                      class="text-xs text-status-warning-700 dark:text-status-warning-400">
                    ⚠ {{ warningText(w) }}
                  </li>
                </ul>
                <p v-if="resultFor(s.name)?.error"
                   class="mt-2 text-xs text-status-danger-600 dark:text-status-danger-400">
                  {{ resultFor(s.name).error }}
                </p>
              </div>
              <SkillMeta :skill="s" />
            </div>
          </li>
        </ul>

        <p v-if="store.lastInjectionAt" class="mt-2 text-xs text-gray-400">
          Last sync {{ new Date(store.lastInjectionAt).toLocaleString() }}
        </p>
        <p v-else-if="store.assigned.length" class="mt-2 text-xs text-gray-400">
          Not synced from this screen yet — statuses appear after a sync. Skills are
          also copied in automatically when the agent starts.
        </p>
      </section>

      <!-- Library browse + assignment -->
      <section v-if="canManage">
        <h3 class="text-sm font-semibold text-gray-900 dark:text-gray-100">Library</h3>
        <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
          {{ store.library.length }} skill{{ store.library.length === 1 ? '' : 's' }} available.
          Tick to assign, then save.
        </p>

        <ul class="mt-2 divide-y divide-gray-200 dark:divide-gray-700 rounded-lg border border-gray-200 dark:border-gray-700">
          <li v-for="s in store.library" :key="s.name" class="px-4 py-3">
            <label class="flex items-start gap-3 cursor-pointer">
              <input type="checkbox" :value="s.name" v-model="draft"
                     class="mt-1 rounded text-action-primary-600 focus:ring-action-primary-500" />
              <div class="min-w-0 flex-1">
                <div class="flex items-center gap-2 flex-wrap">
                  <span class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ s.name }}</span>
                  <span v-if="s.automation" class="text-[11px] px-1.5 py-0.5 rounded-full bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300">{{ s.automation }}</span>
                  <span v-if="!s.user_invocable" class="text-[11px] px-1.5 py-0.5 rounded-full bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400" title="Runs automatically; a user cannot invoke it directly">not user-invocable</span>
                </div>
                <p v-if="s.description" class="mt-1 text-xs text-gray-500 dark:text-gray-400">{{ s.description }}</p>
                <SkillMeta :skill="s" class="mt-1" />
                <!-- Declared dependencies, surfaced BEFORE assignment: this is
                     what turns into a missing_binary/missing_env warning later. -->
                <p v-if="deps(s)" class="mt-1 text-[11px] text-gray-500 dark:text-gray-400">
                  Requires {{ deps(s) }}
                </p>
              </div>
            </label>
          </li>
        </ul>

        <div class="mt-3 flex items-center gap-3">
          <button @click="onSave" :disabled="store.saving || !dirty"
                  class="px-3 py-1.5 rounded-lg bg-action-primary-600 hover:bg-action-primary-700 text-white text-sm disabled:opacity-50">
            {{ store.saving ? 'Saving…' : 'Save assignments' }}
          </button>
          <button v-if="dirty" @click="resetDraft"
                  class="text-sm text-gray-500 dark:text-gray-400 hover:underline">Reset</button>
          <span v-if="savedNote" class="text-xs text-status-success-600 dark:text-status-success-400">{{ savedNote }}</span>
        </div>
      </section>
    </template>
  </div>
</template>

<script setup>
import { computed, h, onMounted, onUnmounted, ref, watch } from 'vue'
import { useSkillsStore } from '../stores/skills'
import { useRole } from '../composables/useRole'

const props = defineProps({
  agentName: { type: String, required: true },
  canManage: { type: Boolean, default: false },
  agentRunning: { type: Boolean, default: false },
})

const store = useSkillsStore()
const { isAdmin } = useRole()

const draft = ref([])
const savedNote = ref('')

/** Small inline renderer for the package facts — same markup in both lists. */
const SkillMeta = (p) => {
  const s = p.skill
  const bits = []
  if (s.multi_file) bits.push(`${s.file_count} files`)
  if (s.size_bytes) bits.push(formatBytes(s.size_bytes))
  if (!bits.length) return null
  return h('p', { class: 'text-[11px] text-gray-400 whitespace-nowrap' }, bits.join(' · '))
}

function formatBytes(n) {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

function deps(s) {
  const r = s.requires || {}
  const parts = []
  if (r.binaries?.length) parts.push(r.binaries.join(', '))
  if (r.packages?.length) parts.push(r.packages.join(', '))
  if (r.env?.length) parts.push(r.env.join(', '))
  return parts.join(' · ') || null
}

function resultFor(name) {
  return store.injectionResults?.[name] || null
}

/**
 * #183 statuses. `fallback` is the one worth naming precisely: the package
 * could not be delivered whole, so a reduced form went in — a green tick there
 * would be a lie.
 */
function statusLabel(status) {
  return {
    injected: 'synced',
    unchanged: 'up to date',
    fallback: 'partial',
    failed: 'failed',
  }[status] || status
}

function statusClass(status) {
  if (status === 'failed') return 'bg-status-danger-100 dark:bg-status-danger-900/50 text-status-danger-700 dark:text-status-danger-300'
  if (status === 'fallback') return 'bg-status-warning-100 dark:bg-status-warning-900/50 text-status-warning-800 dark:text-status-warning-300'
  if (status === 'unchanged') return 'bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300'
  return 'bg-status-success-100 dark:bg-status-success-900/50 text-status-success-700 dark:text-status-success-300'
}

/** Named warnings are machine tokens; say what they mean for this agent. */
function warningText(w) {
  const [kind, detail] = String(w).split(':')
  if (kind === 'missing_binary') return `${detail} is not installed in this agent — the skill may not run`
  if (kind === 'missing_env') return `${detail} is not set in this agent's environment`
  if (kind === 'multi_file_dropped_old_image') return 'Only SKILL.md was copied — the agent image predates multi-file skills. Rebuild the base image for the full package.'
  return w
}

const dirty = computed(() => {
  const a = [...draft.value].sort().join('|')
  const b = [...store.assignedNames].sort().join('|')
  return a !== b
})

function resetDraft() {
  draft.value = [...store.assignedNames]
}

/**
 * "Saved, but not yet inside the agent."
 *
 * Saving writes the assignment rows; the files only reach the container on a
 * sync or the next agent start. The panel said so in muted text, which is easy
 * to miss — an operator can save, message the agent, and be told the skill does
 * not exist, because it genuinely isn't there yet. Carry that gap on the button
 * that closes it.
 *
 * Deliberately session-scoped: it means "you changed assignments and haven't
 * synced since". It is NOT derived from `injectionResults` being empty, which is
 * also true on a fresh page load of an already-synced agent — that would cry
 * wolf on every visit and train people to ignore it.
 */
const pendingSync = ref(false)

// Loud only when the button can actually act. On a stopped agent sync is
// disabled and the files land at next start anyway, so shouting there would be
// noise pointing at a control the operator cannot press.
const syncNeedsAttention = computed(
  () => pendingSync.value && props.agentRunning && !store.injecting
)

async function onSave() {
  savedNote.value = ''
  if (await store.saveAssignments([...draft.value])) {
    resetDraft()
    pendingSync.value = true
    savedNote.value = 'Saved. Sync now, or the agent picks them up on next start.'
  }
}

async function onSync() {
  savedNote.value = ''
  const result = await store.inject()
  // Only clear on a real success — a failed or 409-busy sync leaves the gap
  // open, so the button must keep pointing at it.
  if (result && !store.error) pendingSync.value = false
}

watch(() => store.assigned, resetDraft, { deep: true })

onMounted(async () => {
  await store.load(props.agentName)
  resetDraft()
})
onUnmounted(() => store.clear())
</script>
