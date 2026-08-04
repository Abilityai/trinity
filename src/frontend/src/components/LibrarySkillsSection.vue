<template>
  <div>
    <div class="flex items-start justify-between gap-4 mb-4">
      <div class="min-w-0">
        <h2 class="text-xl font-semibold text-gray-900 dark:text-white">Skills</h2>
        <!-- Sync-state header — leads with what the DISK knows (commit SHA +
             skill count, reliable across workers). `last_sync` is per-worker
             in-memory state and reads null on the other uvicorn worker or
             after a restart: render it only when truthy, never lead with
             "Last synced: never". -->
        <div v-if="store.status?.configured" class="mt-1 text-sm text-gray-500 dark:text-gray-400 space-y-0.5">
          <p>
            <template v-if="store.status.cloned">
              <span v-if="shortSha" class="font-mono text-xs">{{ shortSha }}</span>
              <span v-if="shortSha"> · </span>
              <span>{{ store.status.skill_count }} skill{{ store.status.skill_count === 1 ? '' : 's' }}</span>
              <span v-if="store.status.branch"> · {{ store.status.branch }}</span>
            </template>
            <template v-else>Configured — never synced</template>
            <span v-if="store.status.last_sync"> · last synced {{ new Date(store.status.last_sync).toLocaleString() }}</span>
          </p>
          <!-- Repo URL: admin-only (source URLs are admin-sensitive — ent#237
               classes them so), userinfo stripped (the clone path accepts and
               stores https://user:token@host/... verbatim), labeled as the
               PRIMARY source (post-#1901 the flat url/branch are just the
               first source in resolution order) and hidden entirely once a
               multi-source status reports more than one source. -->
          <p v-if="isAdmin && primarySourceUrl" class="text-xs break-all">
            Primary source: <span class="font-mono">{{ primarySourceUrl }}</span>
          </p>
        </div>
      </div>
      <button
        v-if="isAdmin && store.status?.configured"
        @click="onSync"
        :disabled="store.syncing"
        title="Pull the latest skills from the library repository"
        class="shrink-0 px-3 py-1.5 rounded-lg border border-gray-300 dark:border-gray-600 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800 disabled:opacity-50 disabled:cursor-not-allowed"
      >{{ store.syncing ? 'Syncing…' : 'Sync now' }}</button>
    </div>

    <p v-if="store.syncError" class="mb-3 text-sm text-status-danger-600 dark:text-status-danger-400">{{ store.syncError }}</p>

    <p v-if="store.loading" class="text-sm text-gray-500 dark:text-gray-400">Loading skills…</p>

    <!-- A fetch error is an ERROR, not an empty library (no-swallow rule):
         say so and offer a retry instead of a confident wrong empty state. -->
    <div v-else-if="store.error" class="rounded-lg border border-status-danger-200 dark:border-status-danger-800 p-5 text-sm">
      <p class="text-status-danger-600 dark:text-status-danger-400">{{ store.error }}</p>
      <button @click="store.load()" class="mt-2 text-action-primary-600 dark:text-action-primary-400 hover:underline">
        Try again
      </button>
    </div>

    <!-- Empty states (AC#5): each teaches the next action, per viewer role.
         Which one shows is decided by the store's fleet-scoped discriminator,
         so the section can't invent a fifth. -->
    <div v-else-if="store.emptyReason === 'unconfigured'" class="rounded-lg border border-gray-200 dark:border-gray-700 p-5 text-sm">
      <p class="font-medium text-gray-900 dark:text-gray-100">No skills library is configured</p>
      <p class="mt-1 text-gray-500 dark:text-gray-400">
        Skills come from a git repository shared across the fleet. Once it's configured,
        every agent can be assigned skills from it.
      </p>
      <router-link
        v-if="isAdmin"
        to="/settings?tab=agents"
        class="mt-3 inline-block px-3 py-1.5 rounded-lg bg-action-primary-600 hover:bg-action-primary-700 text-white text-sm"
      >
        Configure a skills library in Settings
      </router-link>
      <p v-else class="mt-3 text-gray-500 dark:text-gray-400">Ask your admin to configure a skills library.</p>
    </div>

    <div v-else-if="store.emptyReason === 'not_cloned'" class="rounded-lg border border-gray-200 dark:border-gray-700 p-5 text-sm">
      <p class="font-medium text-gray-900 dark:text-gray-100">Configured but never synced</p>
      <p class="mt-1 text-gray-500 dark:text-gray-400">The library repository hasn't been cloned yet.</p>
      <button
        v-if="isAdmin"
        @click="onSync"
        :disabled="store.syncing"
        class="mt-3 px-3 py-1.5 rounded-lg bg-action-primary-600 hover:bg-action-primary-700 text-white text-sm disabled:opacity-50 disabled:cursor-not-allowed"
      >{{ store.syncing ? 'Syncing…' : 'Sync now' }}</button>
      <p v-else class="mt-3 text-gray-500 dark:text-gray-400">Ask your admin to run a sync.</p>
    </div>

    <div v-else-if="store.emptyReason === 'empty'" class="rounded-lg border border-gray-200 dark:border-gray-700 p-5 text-sm">
      <p class="font-medium text-gray-900 dark:text-gray-100">The library has no skills yet</p>
      <p class="mt-1 text-gray-500 dark:text-gray-400">Add a skill directory to the repository, then Sync.</p>
    </div>

    <!-- Fleet browse cards. Interpolation only — skills come from a synced
         git repo (semi-trusted content): no v-html, no :href bound to any
         library-derived string. Assignment stays per-agent (ent#182: one
         skill model, no parallel mechanisms) — cards link to the agents list. -->
    <ul v-else class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
      <li
        v-for="s in store.library"
        :key="s.name"
        class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg p-4 flex flex-col"
      >
        <div class="flex items-center gap-2 flex-wrap">
          <span class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ s.name }}</span>
          <!-- Forward-compat slots (ent#237 / PR #1901): dormant until the
               multi-source library lands and SkillInfo carries these. -->
          <span
            v-if="s.source_name"
            class="text-[11px] px-1.5 py-0.5 rounded-full bg-action-primary-100 dark:bg-action-primary-900/50 text-action-primary-700 dark:text-action-primary-300"
          >{{ s.source_name }}</span>
          <span
            v-if="s.shadowed_by?.length"
            class="text-[11px] px-1.5 py-0.5 rounded-full bg-status-warning-100 dark:bg-status-warning-900/50 text-status-warning-800 dark:text-status-warning-300"
            :title="`Shadowed by: ${s.shadowed_by.join(', ')}`"
          >shadowed</span>
        </div>
        <p v-if="s.description" class="mt-1 text-xs text-gray-500 dark:text-gray-400 flex-grow">{{ s.description }}</p>
        <div v-else class="flex-grow"></div>
        <SkillContractChips :skill="s" show-version class="mt-2" />
        <p v-if="deps(s)" class="mt-1 text-[11px] text-gray-500 dark:text-gray-400">Requires {{ deps(s) }}</p>
        <router-link
          to="/agents"
          class="mt-3 text-xs text-action-primary-600 dark:text-action-primary-400 hover:underline"
        >
          Assign via an agent's Skills tab →
        </router-link>
      </li>
    </ul>
  </div>
</template>

<script setup>
import { computed, onMounted } from 'vue'
import { useSkillsLibraryStore } from '../stores/skillsLibrary'
import { useRole } from '../composables/useRole'
import SkillContractChips from './skills/SkillContractChips.vue'
import { deps, stripUserinfo } from './skills/contract'

const store = useSkillsLibraryStore()
const { isAdmin } = useRole()

const shortSha = computed(() =>
  store.status?.commit_sha ? String(store.status.commit_sha).slice(0, 7) : ''
)

/**
 * The flat `url` is the PRIMARY source post-#1901 (first in resolution
 * order) — hide the single-source presentation once status reports multiple
 * sources, and strip any embedded userinfo (user:token@host) before display
 * via the shared, adversarially-tested `stripUserinfo` in
 * `components/skills/contract.js`: the clone path accepts credentialed URLs
 * and stores them verbatim.
 */
const primarySourceUrl = computed(() => {
  const st = store.status
  if (!st?.url) return null
  if (Array.isArray(st.sources) && st.sources.length > 1) return null
  return stripUserinfo(st.url)
})

async function onSync() {
  await store.sync()
}

onMounted(() => {
  store.load()
})
</script>
