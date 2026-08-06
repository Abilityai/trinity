<template>
  <div class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg">
    <div class="px-6 py-4 border-b border-gray-200 dark:border-gray-700">
      <div class="flex items-start justify-between gap-4">
        <div>
          <h2 class="text-lg font-medium text-gray-900 dark:text-white">Skills Library</h2>
          <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
            Repositories the platform syncs reusable agent skills from. Skills live at
            <code class="px-1 py-0.5 bg-gray-100 dark:bg-gray-700 rounded text-xs">skills/&lt;name&gt;/SKILL.md</code>
            (declared via <code class="px-1 py-0.5 bg-gray-100 dark:bg-gray-700 rounded text-xs">catalog.yaml</code>)
            or the legacy
            <code class="px-1 py-0.5 bg-gray-100 dark:bg-gray-700 rounded text-xs">.claude/skills/&lt;name&gt;/SKILL.md</code>.
          </p>
        </div>
        <button
          @click="store.syncAll()"
          :disabled="store.loading || !store.sources.length"
          class="shrink-0 inline-flex items-center px-3 py-2 border border-gray-300 dark:border-gray-600 shadow-sm text-sm font-medium rounded-md text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-700 hover:bg-gray-50 dark:hover:bg-gray-600 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {{ store.loading ? 'Syncing…' : 'Sync all' }}
        </button>
      </div>
    </div>

    <div class="px-6 py-4 space-y-4">
      <!-- Errors are shown verbatim: a refused moved tag names the tag and says
           what to do about it, which a generic message would throw away. -->
      <div
        v-if="store.error"
        class="rounded-md bg-status-danger-50 dark:bg-status-danger-900/30 p-3 text-sm text-status-danger-700 dark:text-status-danger-300"
      >
        {{ store.error }}
      </div>

      <div
        v-if="store.shadowedCount"
        class="rounded-md bg-status-warning-50 dark:bg-status-warning-900/30 p-3 text-sm text-status-warning-800 dark:text-status-warning-300"
      >
        <strong>{{ store.shadowedCount }}</strong>
        {{ store.shadowedCount === 1 ? 'skill is' : 'skills are' }} provided by more than one
        source. The higher-precedence source wins; the other copies are not used.
      </div>

      <p v-if="!store.sources.length && !store.loading" class="text-sm text-gray-500 dark:text-gray-400">
        No sources configured — the library is empty. Add a repository below.
      </p>

      <!-- Source list, in RESOLUTION order. The first row wins a name clash. -->
      <ul v-if="store.sources.length" class="divide-y divide-gray-200 dark:divide-gray-700">
        <li v-for="(s, i) in store.resolutionOrder" :key="s.id" class="py-3">
          <div class="flex items-start justify-between gap-4">
            <div class="min-w-0">
              <div class="flex items-center gap-2 flex-wrap">
                <span class="text-sm font-medium text-gray-900 dark:text-white truncate">{{ s.name }}</span>
                <span
                  v-if="i === 0"
                  class="px-1.5 py-0.5 rounded text-xs bg-action-primary-100 dark:bg-action-primary-900/40 text-action-primary-700 dark:text-action-primary-300"
                  title="Wins when two sources ship the same skill name"
                >wins conflicts</span>
                <span
                  v-if="s.is_default"
                  class="px-1.5 py-0.5 rounded text-xs bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300"
                >bundled</span>
                <!-- ref_type is the supply-chain posture, not decoration:
                     'pinned' means a moved tag is refused. -->
                <span
                  class="px-1.5 py-0.5 rounded text-xs"
                  :class="s.ref_type === 'tag'
                    ? 'bg-status-success-100 dark:bg-status-success-900/40 text-status-success-700 dark:text-status-success-300'
                    : 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300'"
                  :title="s.ref_type === 'tag'
                    ? 'Pinned to a tag — a tag that moves is refused'
                    : 'Tracks a branch — picks up every new commit on sync'"
                >{{ s.ref_type === 'tag' ? 'pinned' : 'branch' }} · {{ s.ref }}</span>
                <span v-if="!s.enabled" class="px-1.5 py-0.5 rounded text-xs bg-gray-200 dark:bg-gray-600 text-gray-600 dark:text-gray-300">disabled</span>
              </div>
              <!-- ent#334: the backend now strips userinfo at the emitter, so
                   this is defence-in-depth, not the control. Kept because the
                   two strips are NOT equivalent — this one also covers
                   scp-style, protocol-relative and git+ssh:// shapes that
                   Python's urlparse does not. Both strip; neither is
                   load-bearing alone. Do not "unify" them. -->
              <p class="mt-0.5 text-xs text-gray-500 dark:text-gray-400 truncate">{{ stripUserinfo(s.url) }}</p>
              <p class="mt-0.5 text-xs" :class="statusClass(s)">
                {{ statusLabel(s) }}
                <span v-if="s.skill_count" class="text-gray-500 dark:text-gray-400">
                  · {{ s.skill_count }} {{ s.skill_count === 1 ? 'skill' : 'skills' }} in use
                </span>
              </p>
              <p v-if="s.last_error" class="mt-0.5 text-xs text-status-danger-600 dark:text-status-danger-400 break-words">
                {{ s.last_error }}
              </p>
            </div>

            <div class="shrink-0 flex items-center gap-2">
              <button
                @click="store.sync(s.id)"
                :disabled="store.busyId === s.id || !s.enabled"
                class="px-2 py-1 text-xs border border-gray-300 dark:border-gray-600 rounded text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-600 disabled:opacity-50"
              >Sync</button>
              <button
                @click="store.update(s.id, { enabled: !s.enabled })"
                :disabled="store.busyId === s.id"
                class="px-2 py-1 text-xs border border-gray-300 dark:border-gray-600 rounded text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-600 disabled:opacity-50"
              >{{ s.enabled ? 'Disable' : 'Enable' }}</button>
              <button
                @click="confirmRemove(s)"
                :disabled="store.busyId === s.id"
                class="px-2 py-1 text-xs border border-status-danger-300 dark:border-status-danger-700 rounded text-status-danger-700 dark:text-status-danger-400 hover:bg-status-danger-50 dark:hover:bg-status-danger-900/30 disabled:opacity-50"
              >Remove</button>
            </div>
          </div>
        </li>
      </ul>

      <!-- Add a source -->
      <div class="pt-2 border-t border-gray-200 dark:border-gray-700">
        <button
          v-if="!adding"
          @click="adding = true"
          class="text-sm text-action-primary-600 dark:text-action-primary-400 hover:underline"
        >+ Add a skills repository</button>

        <div v-else class="space-y-3">
          <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div>
              <label class="block text-xs font-medium text-gray-700 dark:text-gray-300">Name</label>
              <input v-model="form.name" placeholder="Acme internal skills" :class="inputClass" />
            </div>
            <div>
              <label class="block text-xs font-medium text-gray-700 dark:text-gray-300">Repository URL</label>
              <input v-model="form.url" placeholder="github.com/owner/repo" :class="inputClass" />
            </div>
            <div>
              <label class="block text-xs font-medium text-gray-700 dark:text-gray-300">Track</label>
              <select v-model="form.ref_type" :class="inputClass">
                <option value="branch">Branch (follows new commits)</option>
                <option value="tag">Tag (pinned — a moved tag is refused)</option>
              </select>
            </div>
            <div>
              <label class="block text-xs font-medium text-gray-700 dark:text-gray-300">
                {{ form.ref_type === 'tag' ? 'Tag' : 'Branch' }}
              </label>
              <input v-model="form.ref" :placeholder="form.ref_type === 'tag' ? 'v1.0.0' : 'main'" :class="inputClass" />
            </div>
          </div>
          <p class="text-xs text-gray-500 dark:text-gray-400">
            Pin to a tag when you don't fully control who can merge to the repository — skills can
            carry executable scripts, and a branch is picked up automatically on every sync.
          </p>
          <div class="flex justify-end gap-2">
            <button @click="cancelAdd" class="px-3 py-1.5 text-sm text-gray-600 dark:text-gray-300 hover:underline">Cancel</button>
            <button
              @click="submit"
              :disabled="!canSubmit"
              class="px-3 py-1.5 text-sm font-medium rounded-md text-white bg-action-primary-600 hover:bg-action-primary-700 disabled:opacity-50 disabled:cursor-not-allowed"
            >Add source</button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useSkillSourcesStore } from '../stores/skillSources'
import { stripUserinfo } from './skills/contract'

const store = useSkillSourcesStore()
const adding = ref(false)

const inputClass =
  'mt-1 block w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm ' +
  'placeholder-gray-400 focus:outline-none focus:ring-action-primary-500 focus:border-action-primary-500 ' +
  'dark:bg-gray-700 dark:text-white text-sm'

const blank = () => ({ name: '', url: '', ref: 'main', ref_type: 'branch' })
const form = ref(blank())

const canSubmit = computed(() =>
  form.value.name.trim() && form.value.url.trim() && form.value.ref.trim()
)

function statusLabel (s) {
  if (s.last_sync_status === 'success') {
    return s.last_sync ? `Synced ${new Date(s.last_sync).toLocaleString()}` : 'Synced'
  }
  if (s.last_sync_status === 'failed') return 'Last sync failed'
  return 'Never synced'
}

function statusClass (s) {
  if (s.last_sync_status === 'success') return 'text-gray-500 dark:text-gray-400'
  if (s.last_sync_status === 'failed') return 'text-status-danger-600 dark:text-status-danger-400'
  return 'text-gray-400 dark:text-gray-500'
}

function cancelAdd () {
  adding.value = false
  form.value = blank()
  store.error = null
}

async function submit () {
  const ok = await store.create({
    name: form.value.name.trim(),
    url: form.value.url.trim(),
    ref: form.value.ref.trim(),
    ref_type: form.value.ref_type,
  })
  // Only collapse on success — on failure the form stays filled so the admin
  // can correct the URL rather than retype it.
  if (ok) cancelAdd()
}

async function confirmRemove (s) {
  // Removing a source does NOT unassign its skills — they keep resolving through
  // whatever source still provides them. Say so, so "Remove" doesn't read as
  // "strip these skills from my agents".
  const msg =
    `Remove "${s.name}" from the skills library?\n\n` +
    `Agents keep any skills already installed, and assignments are not removed — ` +
    `each skill will resolve through another source if one provides it.`
  if (window.confirm(msg)) await store.remove(s.id)
}

onMounted(store.fetch)
</script>
