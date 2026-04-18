<template>
  <div v-if="show && !isParallelHistory" class="fixed z-50 inset-0 overflow-y-auto">
    <div class="flex items-center justify-center min-h-screen pt-4 px-4 pb-20 text-center sm:p-0">
      <!-- Backdrop -->
      <div class="fixed inset-0 bg-gray-500 dark:bg-gray-900 bg-opacity-75 dark:bg-opacity-75 transition-opacity" @click="$emit('dismiss')"></div>

      <!-- Modal content -->
      <div class="inline-block align-middle bg-white dark:bg-gray-800 rounded-lg text-left shadow-xl transform transition-all sm:max-w-lg sm:w-full">
        <div class="px-4 pt-5 pb-4 sm:p-6">
          <!-- Header with icon -->
          <div class="flex items-start">
            <div class="flex-shrink-0 flex items-center justify-center h-10 w-10 rounded-full bg-yellow-100 dark:bg-yellow-900/30">
              <svg class="h-6 w-6 text-yellow-600 dark:text-yellow-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
              </svg>
            </div>
            <div class="ml-4 flex-1">
              <h3 class="text-lg font-medium text-gray-900 dark:text-white" data-testid="gcm-title">
                {{ copy.title }}
              </h3>
              <!-- Plain-English body (bullets of what's true) -->
              <ul v-if="copy.body.length" class="mt-2 text-sm text-gray-600 dark:text-gray-300 list-disc list-inside space-y-1" data-testid="gcm-body">
                <li v-for="(line, i) in copy.body" :key="i">{{ line }}</li>
              </ul>
              <!-- Recommendation: single-sentence suggested action -->
              <p class="mt-2 text-sm font-medium text-gray-900 dark:text-white" data-testid="gcm-recommendation">
                {{ copy.recommendation }}
              </p>
            </div>
          </div>

          <!-- Developer-facing raw git output (S5 / issue #386). -->
          <details v-if="rawStderr" class="mt-4 text-xs text-gray-600 dark:text-gray-400" data-testid="gcm-details">
            <summary class="cursor-pointer select-none">Git details (for developers)</summary>
            <pre class="mt-2 whitespace-pre-wrap bg-gray-50 dark:bg-gray-900/40 rounded p-2 overflow-auto max-h-60" data-testid="gcm-raw-stderr">{{ rawStderr }}</pre>
          </details>

          <!-- Options for Pull conflicts -->
          <div v-if="isPull" class="mt-6 space-y-3">
            <button
              @click="$emit('resolve', 'stash_reapply')"
              class="w-full flex items-start p-4 rounded-lg border border-gray-200 dark:border-gray-600 hover:border-blue-500 dark:hover:border-blue-400 hover:bg-blue-50 dark:hover:bg-blue-900/20 transition-colors group"
            >
              <div class="flex-shrink-0 flex items-center justify-center h-8 w-8 rounded-full bg-blue-100 dark:bg-blue-900/30 text-blue-600 dark:text-blue-400 group-hover:bg-blue-200 dark:group-hover:bg-blue-900/50">
                <svg class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 8h14M5 8a2 2 0 110-4h14a2 2 0 110 4M5 8v10a2 2 0 002 2h10a2 2 0 002-2V8m-9 4h4" />
                </svg>
              </div>
              <div class="ml-4 text-left">
                <p class="text-sm font-medium text-gray-900 dark:text-white">Stash & Reapply (Recommended)</p>
                <p class="text-xs text-gray-500 dark:text-gray-400 mt-0.5">Save local changes, pull remote, then reapply your changes</p>
              </div>
            </button>

            <button
              @click="$emit('resolve', 'force_reset')"
              class="w-full flex items-start p-4 rounded-lg border border-gray-200 dark:border-gray-600 hover:border-red-500 dark:hover:border-red-400 hover:bg-red-50 dark:hover:bg-red-900/20 transition-colors group"
            >
              <div class="flex-shrink-0 flex items-center justify-center h-8 w-8 rounded-full bg-red-100 dark:bg-red-900/30 text-red-600 dark:text-red-400 group-hover:bg-red-200 dark:group-hover:bg-red-900/50">
                <svg class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                </svg>
              </div>
              <div class="ml-4 text-left">
                <p class="text-sm font-medium text-gray-900 dark:text-white">Force Replace Local</p>
                <p class="text-xs text-red-600 dark:text-red-400 mt-0.5">Discard all local changes and reset to remote (destructive!)</p>
              </div>
            </button>
          </div>

          <!-- Options for Sync/Push conflicts -->
          <div v-else class="mt-6 space-y-3">
            <button
              @click="$emit('resolve', 'pull_first')"
              class="w-full flex items-start p-4 rounded-lg border border-gray-200 dark:border-gray-600 hover:border-blue-500 dark:hover:border-blue-400 hover:bg-blue-50 dark:hover:bg-blue-900/20 transition-colors group"
            >
              <div class="flex-shrink-0 flex items-center justify-center h-8 w-8 rounded-full bg-blue-100 dark:bg-blue-900/30 text-blue-600 dark:text-blue-400 group-hover:bg-blue-200 dark:group-hover:bg-blue-900/50">
                <svg class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                </svg>
              </div>
              <div class="ml-4 text-left">
                <p class="text-sm font-medium text-gray-900 dark:text-white">Pull First, Then Push (Recommended)</p>
                <p class="text-xs text-gray-500 dark:text-gray-400 mt-0.5">Fetch remote changes, merge with yours, then push</p>
              </div>
            </button>

            <button
              @click="$emit('resolve', 'force_push')"
              class="w-full flex items-start p-4 rounded-lg border border-gray-200 dark:border-gray-600 hover:border-red-500 dark:hover:border-red-400 hover:bg-red-50 dark:hover:bg-red-900/20 transition-colors group"
            >
              <div class="flex-shrink-0 flex items-center justify-center h-8 w-8 rounded-full bg-red-100 dark:bg-red-900/30 text-red-600 dark:text-red-400 group-hover:bg-red-200 dark:group-hover:bg-red-900/50">
                <svg class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 16V4m0 0L3 8m4-4l4 4m6 0v12m0 0l4-4m-4 4l-4-4" />
                </svg>
              </div>
              <div class="ml-4 text-left">
                <p class="text-sm font-medium text-gray-900 dark:text-white">Force Push</p>
                <p class="text-xs text-red-600 dark:text-red-400 mt-0.5">Overwrite remote with your local changes (destructive!)</p>
              </div>
            </button>
          </div>
        </div>

        <!-- Footer -->
        <div class="bg-gray-50 dark:bg-gray-700/50 px-4 py-3 sm:px-6 flex justify-end">
          <button
            @click="$emit('dismiss')"
            class="px-4 py-2 text-sm font-medium text-gray-700 dark:text-gray-300 hover:text-gray-900 dark:hover:text-white"
          >
            Cancel
          </button>
        </div>
      </div>
    </div>
  </div>
  <!-- S2 (issue #385): parallel-history variant. Rendered when the agent's
       branch and origin/<pull_branch> share no recent common ancestor AND
       behind > 0 — neither Pull First nor Force Push recovers. -->
  <div v-if="show && isParallelHistory" class="fixed z-50 inset-0 overflow-y-auto">
    <div class="flex items-center justify-center min-h-screen pt-4 px-4 pb-20 text-center sm:p-0">
      <div class="fixed inset-0 bg-gray-500 dark:bg-gray-900 bg-opacity-75 dark:bg-opacity-75 transition-opacity" @click="$emit('dismiss')"></div>

      <div class="inline-block align-middle bg-white dark:bg-gray-800 rounded-lg text-left shadow-xl transform transition-all sm:max-w-lg sm:w-full">
        <div class="px-4 pt-5 pb-4 sm:p-6">
          <div class="flex items-start">
            <div class="flex-shrink-0 flex items-center justify-center h-10 w-10 rounded-full bg-yellow-100 dark:bg-yellow-900/30">
              <svg class="h-6 w-6 text-yellow-600 dark:text-yellow-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
              </svg>
            </div>
            <div class="ml-4 flex-1">
              <h3 class="text-lg font-medium text-gray-900 dark:text-white">
                Your agent cannot sync
              </h3>
              <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
                Your agent's branch and <code class="font-mono text-xs">{{ pullBranchLabel }}</code> have diverged —
                each side has commits the other doesn't. Pull First and Force Push would both lose data.
              </p>
              <p v-if="divergenceSummary" class="mt-2 text-sm text-gray-600 dark:text-gray-300">
                {{ divergenceSummary }}
              </p>
            </div>
          </div>

          <div class="mt-6 space-y-3">
            <button
              @click="$emit('resolve', 'adopt_upstream_preserve_state')"
              class="w-full flex items-start p-4 rounded-lg border border-gray-200 dark:border-gray-600 hover:border-blue-500 dark:hover:border-blue-400 hover:bg-blue-50 dark:hover:bg-blue-900/20 transition-colors group"
            >
              <div class="flex-shrink-0 flex items-center justify-center h-8 w-8 rounded-full bg-blue-100 dark:bg-blue-900/30 text-blue-600 dark:text-blue-400 group-hover:bg-blue-200 dark:group-hover:bg-blue-900/50">
                <svg class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                </svg>
              </div>
              <div class="ml-4 text-left">
                <p class="text-sm font-medium text-gray-900 dark:text-white">Adopt latest upstream (preserve my state)</p>
                <p class="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
                  Reset the agent's source files to <code class="font-mono">{{ pullBranchLabel }}</code> while keeping
                  its workspace / persistent state intact. Recommended.
                </p>
              </div>
            </button>

            <button
              @click="$emit('resolve', 'force_push')"
              class="w-full flex items-start p-4 rounded-lg border border-gray-200 dark:border-gray-600 hover:border-red-500 dark:hover:border-red-400 hover:bg-red-50 dark:hover:bg-red-900/20 transition-colors group"
            >
              <div class="flex-shrink-0 flex items-center justify-center h-8 w-8 rounded-full bg-red-100 dark:bg-red-900/30 text-red-600 dark:text-red-400 group-hover:bg-red-200 dark:group-hover:bg-red-900/50">
                <svg class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 16V4m0 0L3 8m4-4l4 4m6 0v12m0 0l4-4m-4 4l-4-4" />
                </svg>
              </div>
              <div class="ml-4 text-left">
                <p class="text-sm font-medium text-gray-900 dark:text-white">Force push anyway</p>
                <p class="text-xs text-red-600 dark:text-red-400 mt-0.5">
                  Overwrites the remote working branch with your local history. Does NOT bring in
                  {{ pullBranchLabel }}'s commits. Destructive.
                </p>
              </div>
            </button>
          </div>
        </div>

        <div class="bg-gray-50 dark:bg-gray-700/50 px-4 py-3 sm:px-6 flex justify-end">
          <button
            @click="$emit('dismiss')"
            class="px-4 py-2 text-sm font-medium text-gray-700 dark:text-gray-300 hover:text-gray-900 dark:hover:text-white"
          >
            Cancel
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  show: {
    type: Boolean,
    default: false
  },
  conflict: {
    type: Object,
    default: null
    // {
    //   type: 'pull'|'sync',
    //   conflictType: string,
    //   message: string,
    //   // S5 (issue #386):
    //   conflictClass?: string,   // AHEAD_ONLY | BEHIND_ONLY | PARALLEL_HISTORY |
    //                             // UNCOMMITTED_LOCAL | AUTH_FAILURE |
    //                             // WORKING_BRANCH_EXTERNAL_WRITE | UNKNOWN
    //   rawStderr?: string        // full git stderr, rendered inside <details>
    // }
  },
  // S2 (issue #385): when true, render the parallel-history variant instead
  // of the default Pull First / Force Push modal. Read-only flag computed
  // upstream in useGitSync.js.
  isParallelHistory: {
    type: Boolean,
    default: false
  },
  // S2 (issue #385): label of the branch the agent pulls from (e.g. 'main',
  // 'master', 'trunk'). Avoids hardcoding 'main' in user-facing copy.
  pullBranch: {
    type: String,
    default: ''
  },
  // S2 (issue #385): optional human-readable summary like
  // "main has 3 new commits; your agent has 2 local commits".
  divergenceSummary: {
    type: String,
    default: ''
  }
})

defineEmits(['resolve', 'dismiss'])

const isPull = computed(() => props.conflict?.type === 'pull')

// S5 (issue #386) — operator-readable copy keyed on conflictClass.
// Keep one place for copy so title/body/recommendation stay aligned.
// Fallback entries preserve the pre-S5 strings verbatim for unknown classes.
const COPY = {
  AHEAD_ONLY: {
    title: 'You have local changes to push',
    body: ['Your agent has changes that are not yet on the remote.', 'The remote has not moved since your last sync.'],
    recommendation: 'Push your changes to share them with the team.'
  },
  BEHIND_ONLY: {
    title: 'The remote has new changes',
    body: ['The remote branch has commits your agent does not have yet.', 'You have no unpushed local changes.'],
    recommendation: 'Pull the latest changes to catch up.'
  },
  PARALLEL_HISTORY: {
    title: 'Your agent and main have diverged',
    body: [
      'Main has been rewritten since your agent forked from it.',
      'A normal pull cannot replay your changes on top of the new main.',
      'A force push would silently hide the improved main from your agent.'
    ],
    recommendation: 'Use "Adopt latest upstream" (blocked on #384) to re-fork from main while keeping your workspace state.'
  },
  UNCOMMITTED_LOCAL: {
    title: 'Uncommitted local changes are blocking the sync',
    body: ['Git refuses to pull because your workspace has uncommitted edits that would be overwritten.'],
    recommendation: 'Use "Stash & Reapply" to save your edits, pull the remote, then reapply them.'
  },
  AUTH_FAILURE: {
    title: 'Git could not authenticate with the remote',
    body: ['The remote rejected the credentials git sent.', 'This is usually an expired or missing GitHub PAT.'],
    recommendation: 'Update the agent\u2019s GitHub PAT on the Credentials tab and retry.'
  },
  WORKING_BRANCH_EXTERNAL_WRITE: {
    title: 'Another process wrote to this working branch',
    body: [
      'The remote working branch moved between when your agent read it and when it tried to push.',
      'This usually means a second agent or operator just pushed to the same branch.'
    ],
    recommendation: 'Refresh git status and decide whether to adopt the other side\u2019s work or overwrite it.'
  },
  UNKNOWN: {
    title: 'Your agent cannot sync',
    body: ['Git returned an error that Trinity does not yet classify.'],
    recommendation: 'Expand "Git details" below and share the output with whoever set up the agent.'
  }
}

const FALLBACK_TITLE = (isPull) => (isPull ? 'Pull Conflict' : 'Push Conflict')

const copy = computed(() => {
  const cls = props.conflict?.conflictClass
  if (cls && COPY[cls]) {
    return COPY[cls]
  }
  // Pre-S5 behavior when the backend did not send a class — keeps existing
  // agent images working without a forced upgrade.
  return {
    title: FALLBACK_TITLE(isPull.value),
    body: [],
    recommendation: props.conflict?.message || 'A conflict was detected. Choose how to proceed:'
  }
})

const rawStderr = computed(() => props.conflict?.rawStderr || '')
const pullBranchLabel = computed(() => props.pullBranch || 'upstream')
</script>
