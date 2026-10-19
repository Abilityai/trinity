<template>
  <!--
    trinity-enterprise#950 — the agent header's second line: the bound GitHub
    repository with its git state at a glance, then the tags. Pull / Push /
    Refresh stay on the Git tab; the full tag editor stays in Settings ›
    General as well (this opens the same TagsEditor in a dialog).

    The line is ALWAYS rendered at a reserved height (principles 4 and 6):
    git status and tags arrive after first paint, and a line that appeared
    with them would push the tab strip down on every agent page load. Below
    `sm` the repo and the tags wrap onto two lines, so two are reserved there.
    Neither set grows the line (principle 30): one repo, at most
    MAX_CHIPS tag chips plus a counted "+N", every label truncated.
  -->
  <div class="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 min-h-[3.25rem] sm:min-h-[1.5rem] min-w-0" data-testid="agent-header-meta">
    <!-- Repository -->
    <div v-if="repo" class="flex items-center gap-1.5 min-w-0 max-w-full text-[12.5px]" data-testid="agent-header-repo">
      <svg class="w-3.5 h-3.5 shrink-0 text-gray-600 dark:text-gray-400" fill="currentColor" viewBox="0 0 24 24" aria-hidden="true">
        <path d="M12 0c-6.626 0-12 5.373-12 12 0 5.302 3.438 9.8 8.207 11.387.599.111.793-.261.793-.577v-2.234c-3.338.726-4.033-1.416-4.033-1.416-.546-1.387-1.333-1.756-1.333-1.756-1.089-.745.083-.729.083-.729 1.205.084 1.839 1.237 1.839 1.237 1.07 1.834 2.807 1.304 3.492.997.107-.775.418-1.305.762-1.604-2.665-.305-5.467-1.334-5.467-5.931 0-1.311.469-2.381 1.236-3.221-.124-.303-.535-1.524.117-3.176 0 0 1.008-.322 3.301 1.23.957-.266 1.983-.399 3.003-.404 1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.653 1.653.242 2.874.118 3.176.77.84 1.235 1.911 1.235 3.221 0 4.609-2.807 5.624-5.479 5.921.43.372.823 1.102.823 2.222v3.293c0 .319.192.694.801.576 4.765-1.589 8.199-6.086 8.199-11.386 0-6.627-5.373-12-12-12z"/>
      </svg>
      <a
        v-if="repo.href"
        :href="repo.href"
        target="_blank"
        rel="noopener noreferrer"
        class="truncate max-w-[14rem] font-medium text-action-primary-700 hover:text-action-primary-800 dark:text-action-primary-300 dark:hover:text-action-primary-200 hover:underline"
        :title="`Open ${repo.label}`"
      >{{ repo.label }}</a>
      <span v-else class="truncate max-w-[14rem] font-medium text-gray-900 dark:text-gray-100" :title="repo.label">{{ repo.label }}</span>
      <code
        v-if="gitStatus.branch"
        class="truncate max-w-[10rem] font-mono text-gray-600 dark:text-gray-400"
        :title="`Branch ${gitStatus.branch}`"
        data-testid="agent-header-branch"
      >{{ gitStatus.branch }}</code>
      <BaseBadge
        v-if="gitChangesCount > 0"
        variant="warning"
        size="sm"
        class="shrink-0 tabular-nums"
        :title="`${gitChangesCount} changed file(s) not yet pushed — Push is on the Git tab`"
        data-testid="agent-header-git-changes"
      >{{ gitChangesCount }} changed</BaseBadge>
      <BaseBadge
        v-if="ahead > 0"
        variant="warning"
        size="sm"
        class="shrink-0 tabular-nums"
        :title="`${ahead} commit(s) not yet on GitHub — Push is on the Git tab`"
        data-testid="agent-header-git-ahead"
      >{{ ahead }} ahead</BaseBadge>
      <BaseBadge
        v-if="gitBehind > 0"
        variant="info"
        size="sm"
        class="shrink-0 tabular-nums"
        :title="`${gitBehind} commit(s) on GitHub not pulled yet — Pull is on the Git tab`"
        data-testid="agent-header-git-behind"
      >{{ gitBehind }} behind</BaseBadge>
      <BaseBadge
        v-if="gitChangesCount === 0 && ahead === 0 && gitBehind === 0"
        variant="neutral"
        size="sm"
        class="shrink-0"
        title="Nothing to push or pull"
        data-testid="agent-header-git-insync"
      >in sync</BaseBadge>
    </div>

    <!-- Tags -->
    <div v-if="tags.length || canEdit" class="flex items-center gap-1 min-w-0" data-testid="agent-header-tags">
      <BaseBadge
        v-for="tag in shownTags"
        :key="tag"
        variant="purple"
        size="sm"
        class="max-w-[8rem] min-w-0"
        :title="`#${tag}`"
        data-testid="agent-header-tag"
      ><!-- BaseBadge is inline-flex, so the ellipsis needs a block child. --><span class="truncate">#{{ tag }}</span></BaseBadge>
      <button
        v-if="hiddenCount > 0"
        type="button"
        class="shrink-0 rounded-full px-1.5 text-[11px] font-medium tabular-nums text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-750 focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40"
        :title="`${hiddenCount} more tag(s): ${hiddenTags.map(t => '#' + t).join(' ')}`"
        :aria-label="`Show all ${tags.length} tags`"
        aria-haspopup="dialog"
        :aria-expanded="editorOpen ? 'true' : 'false'"
        data-testid="agent-header-tags-more"
        @click="editorOpen = true"
      >+{{ hiddenCount }}</button>
      <button
        v-if="canEdit"
        type="button"
        class="shrink-0 inline-flex items-center gap-0.5 rounded-full px-1.5 py-0.5 text-[11px] font-medium text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-750 focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40"
        :title="tags.length ? 'Edit tags' : 'Add a tag'"
        :aria-label="tags.length ? 'Edit tags' : 'Add a tag'"
        aria-haspopup="dialog"
        :aria-expanded="editorOpen ? 'true' : 'false'"
        data-testid="agent-header-tags-edit"
        @click="editorOpen = true"
      >
        <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
          <path v-if="tags.length" stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.232 5.232l3.536 3.536M9 13l6.232-6.232a2.5 2.5 0 113.536 3.536L12.536 16.536 9 17l.464-3.536z" />
          <path v-else stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
        </svg>
        <span v-if="!tags.length">Add tag</span>
      </button>
    </div>

    <BaseModal
      v-model="editorOpen"
      aria-label="Agent tags"
      panel-class="relative w-full max-w-sm rounded-lg bg-white p-4 shadow-xl dark:bg-gray-800"
    >
      <h2 class="text-[14px] font-semibold text-gray-900 dark:text-gray-100 mb-3">Tags</h2>
      <!-- Esc in the tag input cancels the input only; Esc anywhere else in
           the dialog closes it (BaseModal). Without this one key did both. -->
      <div @keydown.escape="stopEscInInput">
        <TagsEditor
          :model-value="tags"
          :editable="canEdit"
          :all-tags="allTags"
          @update:model-value="$emit('update-tags', $event)"
          @add="$emit('add-tag', $event)"
          @remove="$emit('remove-tag', $event)"
        />
      </div>
      <div class="mt-4 flex justify-end">
        <BaseButton variant="secondary" size="sm" @click="editorOpen = false">Done</BaseButton>
      </div>
    </BaseModal>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import BaseBadge from './base/BaseBadge.vue'
import BaseButton from './base/BaseButton.vue'
import BaseModal from './base/BaseModal.vue'
import TagsEditor from './TagsEditor.vue'
import { repoFromRemote } from '../utils/gitRemote'

const MAX_CHIPS = 2

const props = defineProps({
  canEdit: { type: Boolean, default: false },
  // The live `GET /api/agents/{name}/git/status` payload AgentDetail already
  // polls (useGitSync). `hasGitSync` is hard-coded true there, so the bound-
  // repo signal is the payload itself: git enabled AND an origin to name.
  gitStatus: { type: Object, default: null },
  gitChangesCount: { type: Number, default: 0 },
  gitBehind: { type: Number, default: 0 },
  tags: { type: Array, default: () => [] },
  allTags: { type: Array, default: () => [] },
})

defineEmits(['update-tags', 'add-tag', 'remove-tag'])

const editorOpen = ref(false)

const repo = computed(() => {
  if (!props.gitStatus?.git_enabled) return null
  return repoFromRemote(props.gitStatus.remote_url)
})

// What a push would send is two different units — changed files and commits
// not yet on GitHub (the pair `useGitSync.gitHasChanges` lights Push for) — so
// they are two badges: one summed number would count neither.
const ahead = computed(() => props.gitStatus?.ahead || 0)

const shownTags = computed(() => props.tags.slice(0, MAX_CHIPS))
const hiddenTags = computed(() => props.tags.slice(MAX_CHIPS))
const hiddenCount = computed(() => hiddenTags.value.length)

function stopEscInInput(event) {
  if (event.target?.tagName === 'INPUT') event.stopPropagation()
}
</script>
