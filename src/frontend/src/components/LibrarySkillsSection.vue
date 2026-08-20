<template>
  <div>
    <div class="flex items-start justify-between gap-4 mb-4">
      <div class="min-w-0">
        <h2 class="text-[18px] font-[650] text-gray-900 dark:text-gray-100">Skills</h2>
        <!-- Sync-state header — leads with what the DISK knows (commit SHA +
             skill count, reliable across workers). `last_sync` is per-worker
             in-memory state and reads null on the other uvicorn worker or
             after a restart: render it only when truthy, never lead with
             "Last synced: never". -->
        <div v-if="store.status?.configured" class="mt-1 text-[12.5px] text-gray-600 dark:text-gray-400 space-y-0.5">
          <p>
            <template v-if="store.status.cloned">
              <span v-if="shortSha" class="font-mono text-[11px] tabular-nums">{{ shortSha }}</span>
              <span v-if="shortSha"> · </span>
              <span class="tabular-nums">{{ store.status.skill_count }}</span>
              <span> skill{{ store.status.skill_count === 1 ? '' : 's' }}</span>
              <span v-if="store.status.branch"> · {{ store.status.branch }}</span>
            </template>
            <template v-else>Configured — never synced</template>
            <span v-if="store.status.last_sync">
              · last synced
              <time :datetime="store.status.last_sync" :title="absoluteTime(store.status.last_sync)">
                {{ relativeTime(store.status.last_sync) }}
              </time>
            </span>
          </p>
          <!-- ent#334: the repo URL is gone from this payload. It was
               admin-only in the template, but the template is not the
               boundary — the response carried it to every authenticated
               caller and to agent-scoped MCP keys. Per-source URLs now live
               only on the admin-gated Settings sources panel, which reads
               `GET /skills/sources`. -->
        </div>
      </div>
      <BaseButton
        v-if="isAdmin && store.status?.configured"
        variant="secondary"
        size="sm"
        class="shrink-0"
        :loading="store.syncing"
        loading-label="Syncing…"
        title="Pull the latest skills from the library repository"
        @click="onSync"
      >Sync now</BaseButton>
    </div>

    <InlineError v-if="store.syncError" :message="store.syncError" class="mb-3" />

    <!-- ONE persistent ScanlineReveal instance with the branching INSIDE its
         slot (ent#245): sibling v-if branches AROUND the component remount it,
         which re-inits from loading=false so the reveal never plays. `reveal`
         is false on the error/empty terminals — they snap in rather than
         playing the celebratory pass for content that isn't there. -->
    <ScanlineReveal :loading="store.loading" :reveal="hasSkills">
      <!-- A fetch error is an ERROR, not an empty library (no-swallow rule):
           say so and offer a retry instead of a confident wrong empty state. -->
      <LoadFailed
        v-if="store.error"
        title="Couldn't load the skills library"
        message="The library list could not be fetched. This is not the same as an empty library."
        :detail="store.error"
        :retrying="store.fetching"
        @retry="store.load()"
      />

      <!-- Empty states: each teaches the next action, per viewer role. Which
           one shows is decided by the store's fleet-scoped discriminator, so
           the section can't invent a fifth. Gated on a fetch that SUCCEEDED
           (`hasLoaded`), never on `library.length === 0` (#1926). -->
      <BaseCard v-else-if="showEmpty && store.emptyReason === 'unconfigured'">
        <p class="text-[14px] font-[550] text-gray-900 dark:text-gray-100">No skills library is configured</p>
        <p class="mt-1 text-[12.5px] text-gray-600 dark:text-gray-400">
          Skills come from a git repository shared across the fleet. Once it's configured,
          every agent can be assigned skills from it.
        </p>
        <router-link v-if="isAdmin" to="/settings?tab=agents" class="mt-3 inline-block">
          <BaseButton size="sm">Configure a skills library in Settings</BaseButton>
        </router-link>
        <p v-else class="mt-3 text-[12.5px] text-gray-600 dark:text-gray-400">Ask your admin to configure a skills library.</p>
      </BaseCard>

      <BaseCard v-else-if="showEmpty && store.emptyReason === 'not_cloned'">
        <p class="text-[14px] font-[550] text-gray-900 dark:text-gray-100">Configured but never synced</p>
        <p class="mt-1 text-[12.5px] text-gray-600 dark:text-gray-400">The library repository hasn't been cloned yet.</p>
        <BaseButton
          v-if="isAdmin"
          size="sm"
          class="mt-3"
          :loading="store.syncing"
          loading-label="Syncing…"
          @click="onSync"
        >Sync now</BaseButton>
        <p v-else class="mt-3 text-[12.5px] text-gray-600 dark:text-gray-400">Ask your admin to run a sync.</p>
      </BaseCard>

      <BaseCard v-else-if="showEmpty && store.emptyReason === 'empty'">
        <p class="text-[14px] font-[550] text-gray-900 dark:text-gray-100">The library has no skills yet</p>
        <p class="mt-1 text-[12.5px] text-gray-600 dark:text-gray-400">Add a skill directory to the repository, then Sync.</p>
      </BaseCard>

      <!-- Fleet browse cards. Interpolation only — skills come from a synced
           git repo (semi-trusted content): no v-html, no :href bound to any
           library-derived string. Assignment stays a WRITE on each agent's
           Skills tab (ent#182: one skill model); ent#384 added the READ of
           who already holds each skill, not a second write path. -->
      <div v-else-if="hasSkills">
        <ul class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <li v-for="s in store.library" :key="s.name">
            <BaseCard class="h-full flex flex-col">
              <div class="flex items-center gap-2 flex-wrap">
                <span class="text-[14px] font-[550] text-gray-900 dark:text-gray-100">{{ s.name }}</span>
                <!-- ent#237 multi-source provenance. -->
                <BaseBadge v-if="s.source_name" variant="info">{{ s.source_name }}</BaseBadge>
                <BaseBadge
                  v-if="s.shadowed_by?.length"
                  variant="warning"
                  :title="`Shadowed by: ${s.shadowed_by.join(', ')}`"
                >shadowed</BaseBadge>
              </div>
              <p v-if="s.description" class="mt-1 text-[12.5px] text-gray-600 dark:text-gray-400 flex-grow">{{ s.description }}</p>
              <div v-else class="flex-grow"></div>
              <SkillContractChips :skill="s" show-version class="mt-2" />
              <p v-if="deps(s)" class="mt-1 text-[11px] text-gray-600 dark:text-gray-400">Requires {{ deps(s) }}</p>

              <!-- ent#384: who already holds this skill. -->
              <AssignedAgents :skill-name="s.name" class="mt-3" />

              <!-- ent#386 replaced the "assign via an agent's Skills tab →"
                   link-out that stood here. The block above now carries the
                   assign control itself, so the page that tells you a skill is
                   unused is also the page that can fix it. The link-out is not
                   kept as a fallback: two routes to the same write is the
                   parallel-mechanism problem ent#182 closed, and the agent's
                   Skills tab remains reachable from every holder chip. -->
            </BaseCard>
          </li>
        </ul>

      </div>

      <!-- Rendered as a SIBLING of the branches above, never nested inside the
           has-skills branch. The revocation case this exists for is precisely
           the one where the library listing is EMPTY (a tag cut that dropped
           the skill, or every skill): nested, it lost to the "library has no
           skills yet" empty card and the operator was told nothing at all
           about the agents still carrying the package. Its own v-if keeps it
           invisible when there is nothing orphaned. -->
      <!-- ent#384: assignments whose skill is no longer in the library.
           ent#237's revocation model is "cut a new tag without the offending
           skill" — after which the operator's very next question is *who
           still has it*, and a grid keyed by the library listing answers
           that with silence, permanently. -->
      <section v-if="store.orphanedAssignments.length" class="mt-6">
        <h3 class="text-[14px] font-[550] text-gray-900 dark:text-gray-100">
          Assigned but no longer in the library
          <span class="ml-1 text-[12.5px] font-[400] text-gray-600 dark:text-gray-400 tabular-nums">
            {{ store.orphanedAssignments.length }}
          </span>
        </h3>
        <p class="mt-1 text-[12.5px] text-gray-600 dark:text-gray-400">
          These skills were removed from the library, but the assignments remain. The package
          stays on each agent until it is unassigned from that agent's Skills tab.
        </p>
        <ul class="mt-3 max-h-72 overflow-y-auto space-y-2 pr-1">
          <li v-for="o in store.orphanedAssignments" :key="o.name">
            <BaseCard>
              <div class="flex items-center gap-2 flex-wrap">
                <span class="text-[14px] font-[550] text-gray-900 dark:text-gray-100">{{ o.name }}</span>
                <BaseBadge variant="warning">not in library</BaseBadge>
              </div>
              <AssignedAgents :skill-name="o.name" class="mt-2" />
            </BaseCard>
          </li>
        </ul>
      </section>
    </ScanlineReveal>
  </div>
</template>

<script setup>
import { computed, onMounted } from 'vue'
import { useSkillsLibraryStore } from '../stores/skillsLibrary'
import { useRole } from '../composables/useRole'
import SkillContractChips from './skills/SkillContractChips.vue'
import AssignedAgents from './skills/AssignedAgents.vue'
import { deps } from './skills/contract'
import BaseBadge from './base/BaseBadge.vue'
import BaseButton from './base/BaseButton.vue'
import BaseCard from './base/BaseCard.vue'
import InlineError from './InlineError.vue'
import LoadFailed from './LoadFailed.vue'
import ScanlineReveal from './ScanlineReveal.vue'

const store = useSkillsLibraryStore()
const { isAdmin } = useRole()

const shortSha = computed(() =>
  store.status?.commit_sha ? String(store.status.commit_sha).slice(0, 7) : ''
)

const hasSkills = computed(() => store.library.length > 0)

// An empty state requires a fetch that SUCCEEDED and returned zero (#1926) —
// never `library.length === 0`, which is also true mid-flight and after a
// failure. `store.error` owns the failed branch above this one.
const showEmpty = computed(() => store.hasLoaded && !hasSkills.value)

/** Relative for recency, absolute on hover (principle 22). */
function relativeTime(iso) {
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return ''
  const mins = Math.round((Date.now() - then) / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.round(mins / 60)
  if (hrs < 24) return `${hrs}h ago`
  return `${Math.round(hrs / 24)}d ago`
}

function absoluteTime(iso) {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleString()
}

async function onSync() {
  await store.sync()
}

// The panel owns its own fetch, which is what keeps the Library's per-section
// failure isolation intact. It runs once per VISIT, not once per session.
//
// The tab-switch case is already handled structurally — `Library.vue`
// lazy-mounts this panel and then keeps it mounted (`v-if` visited + `v-show`
// active), so switching tabs never unmounts us and never re-fetches. A
// `store.hasLoaded` guard here would therefore buy nothing on that path while
// breaking a different one: the store is a Pinia singleton that outlives the
// route, so guarding on "ever loaded" makes every LATER visit to /library skip
// the fetch for the rest of the SPA session. An admin who configures a library
// in Settings and comes back would keep reading "No skills library is
// configured" (the unconfigured branch sets `hasLoaded` too) until a full page
// reload, and newly-assigned or deleted agents would never appear in the
// chips. Stale-while-revalidate is what keeps the re-fetch invisible: `loading`
// is `fetching && !hasLoaded`, so a revisit shows the cached data immediately
// and swaps values in place without replaying the scanline.
onMounted(() => {
  store.load()
})
</script>
