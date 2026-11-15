<template>
  <!--
    Settings › Storage — "What it keeps and shares" (trinity-enterprise#888).

    Three places became one pane: the Git tab's configuration half (the bound
    repository and the GitHub token), the Git-sync card from the old Settings
    list and the Folders tab — plus file sharing, moved here from Sharing ›
    Distribution (operator call 2026-10-10: it governs what leaves the disk, not
    where outsiders reach the agent). Credentials were a section here until
    trinity-enterprise#951 gave them their own pane (CredentialsPane.vue).

    Rows that need a restart do not draw their own banner; they report to the
    one banner at the top of Settings.
  -->
  <div class="space-y-6">
    <SettingsSection title="Repository">
      <SettingRow
        label="GitHub repository"
        hint="The workspace is a clone of this repository. Status, commits, pull and push are on the Git tab."
        testid="storage-repository"
      >
        <template #value>
          <span v-if="repo.state === 'loading'" class="block h-3 w-40 rounded bg-gray-200 dark:bg-gray-750 animate-pulse motion-reduce:animate-none" aria-busy="true"><span class="sr-only">Loading repository…</span></span>
          <span v-else-if="repo.state === 'failed'" class="text-[12.5px] text-gray-600 dark:text-gray-400">Couldn't load the repository.</span>
          <template v-else-if="repo.data?.git_enabled">
            <code class="text-[12.5px] font-mono px-1.5 py-0.5 rounded bg-gray-100 dark:bg-gray-750 text-gray-800 dark:text-gray-200 break-all">{{ repo.data.github_repo }}</code>
            <span v-if="repo.data.working_branch" class="text-[12.5px] text-gray-600 dark:text-gray-400">· {{ repo.data.working_branch }}</span>
          </template>
          <span v-else class="text-[12.5px] text-gray-600 dark:text-gray-400">Not connected to a repository.</span>
        </template>
        <template #action>
          <BaseButton v-if="repo.state === 'failed'" variant="ghost" size="sm" @click="loadRepo">Try again</BaseButton>
        </template>
      </SettingRow>

      <template v-if="repo.data?.git_enabled">
        <div class="px-4 py-3">
          <BindRepoPanel
            :agent-name="agentName"
            :agent-status="agentStatus"
            :is-tokenless="isTokenless"
            embedded
            @bound="loadRepo"
          />
        </div>
        <GitHubAuthRow :agent-name="agentName" :notify="notify" @status="patStatus = $event" />
        <div class="px-4 py-3">
          <p class="text-sm font-medium text-gray-900 dark:text-gray-100">Sync</p>
          <p class="mt-0.5 mb-3 text-[12.5px] text-gray-600 dark:text-gray-400">Keep the agent's work in its repository automatically.</p>
          <GitSyncSettingsPanel :agent-name="agentName" :notify="notify" embedded />
        </div>
      </template>
    </SettingsSection>

    <SettingsSection title="Shared folders" description="What this agent exposes from its disk to other agents, and what it mounts from theirs." bare>
      <FoldersPanel :agent-name="agentName" :agent-status="agentStatus" :can-share="true" />
    </SettingsSection>

    <SettingsSection title="File sharing" description="What leaves the agent's disk: time-limited download links it mints with the share_file tool.">
      <div class="px-4 py-3">
        <FileSharingPanel :agent-name="agentName" embedded />
      </div>
    </SettingsSection>
  </div>
</template>

<script setup>
import { ref, reactive, computed, watch, onMounted } from 'vue'
import { useAgentsStore } from '../../stores/agents'
import SettingsSection from './SettingsSection.vue'
import SettingRow from './SettingRow.vue'
import GitHubAuthRow from './GitHubAuthRow.vue'
import BaseButton from '../base/BaseButton.vue'
import BindRepoPanel from '../BindRepoPanel.vue'
import GitSyncSettingsPanel from '../GitSyncSettingsPanel.vue'
import FoldersPanel from '../FoldersPanel.vue'
import FileSharingPanel from '../FileSharingPanel.vue'

const props = defineProps({
  agentName: { type: String, required: true },
  agentStatus: { type: String, default: 'stopped' },
  notify: { type: Function, default: null },
})

const agentsStore = useAgentsStore()

// ---- the bound repository ---------------------------------------------------
const repo = reactive({ state: 'loading', data: null })
async function loadRepo() {
  if (!repo.data) repo.state = 'loading'
  try {
    repo.data = await agentsStore.getGitConfig(props.agentName)
    repo.state = 'loaded'
  } catch {
    if (!repo.data) repo.state = 'failed'
  }
}

// ent#109: the bind row's copy depends on whether git can push at all — a
// per-agent token, not the platform one (which deliberately does not count).
const patStatus = ref(null)
const isTokenless = computed(() => !!patStatus.value && !patStatus.value.configured)

onMounted(loadRepo)
watch(() => props.agentName, () => {
  repo.data = null
  patStatus.value = null
  loadRepo()
})
</script>
