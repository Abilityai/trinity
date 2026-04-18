import { ref, computed, onUnmounted } from 'vue'

/**
 * Composable for git status and sync functionality
 * Manages git sync operations, status polling, and conflict resolution
 */
export function useGitSync(agentRef, agentsStore, showNotification) {
  // Git sync - show tab for all agents (can initialize GitHub sync for any agent)
  const hasGitSync = computed(() => true)

  const gitStatus = ref(null)
  const gitLoading = ref(false)
  const gitSyncing = ref(false)
  const gitPulling = ref(false)
  const gitSyncResult = ref(null)
  let gitStatusInterval = null

  // Conflict state
  const gitConflict = ref(null) // { type: 'pull'|'sync', conflictType: string, message: string, conflictClass?: string, rawStderr?: string }
  const showConflictModal = ref(false)

  const gitHasChanges = computed(() => {
    return (gitStatus.value?.changes_count > 0) || (gitStatus.value?.ahead > 0)
  })

  const gitChangesCount = computed(() => {
    return gitStatus.value?.changes_count || 0
  })

  const gitBehind = computed(() => {
    return gitStatus.value?.behind || 0
  })

  const loadGitStatus = async () => {
    if (!agentRef.value || agentRef.value.status !== 'running' || !hasGitSync.value) return
    gitLoading.value = true
    try {
      gitStatus.value = await agentsStore.getGitStatus(agentRef.value.name)
    } catch (err) {
      console.debug('Failed to load git status:', err)
      gitStatus.value = null
    } finally {
      gitLoading.value = false
    }
  }

  const refreshGitStatus = () => {
    gitSyncResult.value = null
    loadGitStatus()
  }

  /**
   * Sync to GitHub with strategy support
   * @param {string} strategy - 'normal', 'pull_first', 'force_push'
   */
  const syncToGithub = async (strategy = 'normal') => {
    if (!agentRef.value || gitSyncing.value) return
    gitSyncing.value = true
    gitSyncResult.value = null
    gitConflict.value = null

    try {
      const result = await agentsStore.syncToGithub(agentRef.value.name, { strategy })
      gitSyncResult.value = result
      if (result.success) {
        if (result.files_changed > 0) {
          showNotification(`Synced ${result.files_changed} file(s) to GitHub`, 'success')
        } else {
          showNotification(result.message || 'Already up to date', 'success')
        }
      } else {
        showNotification(result.message || 'Sync failed', 'error')
      }
      // Refresh status after sync
      await loadGitStatus()
    } catch (err) {
      console.error('Git sync failed:', err)
      const status = err.response?.status
      const conflictType = err.response?.headers?.['x-conflict-type']
      const message = err.response?.data?.detail || 'Failed to sync to GitHub'

      if (status === 409 && conflictType) {
        // Conflict detected - show modal with options.
        // S5 (issue #386): surface the operator-readable classification and
        // the raw stderr so GitConflictModal can render per-class copy with
        // a developer-facing <details> fallback.
        const conflictClass = err.response?.headers?.['x-conflict-class'] || null
        const rawStderr = err.response?.data?.detail || ''
        gitConflict.value = {
          type: 'sync',
          conflictType,
          message,
          conflictClass,
          rawStderr
        }
        showConflictModal.value = true
      } else {
        showNotification(message, 'error')
      }
    } finally {
      gitSyncing.value = false
    }
  }

  /**
   * Pull from GitHub with strategy support
   * @param {string} strategy - 'clean', 'stash_reapply', 'force_reset'
   */
  const pullFromGithub = async (strategy = 'clean') => {
    if (!agentRef.value || gitPulling.value) return
    gitPulling.value = true
    gitConflict.value = null

    try {
      const result = await agentsStore.pullFromGithub(agentRef.value.name, { strategy })
      if (result.success) {
        showNotification(result.message || 'Pulled latest changes from GitHub', 'success')
      } else {
        showNotification(result.message || 'Pull failed', 'error')
      }
      // Refresh status after pull
      await loadGitStatus()
    } catch (err) {
      console.error('Git pull failed:', err)
      const status = err.response?.status
      const conflictType = err.response?.headers?.['x-conflict-type']
      const message = err.response?.data?.detail || 'Failed to pull from GitHub'

      if (status === 409 && conflictType) {
        // Conflict detected - show modal with options.
        // S5 (issue #386): see sync branch above for conflictClass/rawStderr.
        const conflictClass = err.response?.headers?.['x-conflict-class'] || null
        const rawStderr = err.response?.data?.detail || ''
        gitConflict.value = {
          type: 'pull',
          conflictType,
          message,
          conflictClass,
          rawStderr
        }
        showConflictModal.value = true
      } else {
        showNotification(message, 'error')
      }
    } finally {
      gitPulling.value = false
    }
  }

  /**
   * Resolve conflict by retrying with a specific strategy
   * @param {string} strategy - Strategy to use for retry
   */
  const resolveConflict = async (strategy) => {
    showConflictModal.value = false
    const conflictType = gitConflict.value?.type

    if (conflictType === 'pull') {
      await pullFromGithub(strategy)
    } else if (conflictType === 'sync') {
      await syncToGithub(strategy)
    }
  }

  /**
   * Dismiss conflict modal without taking action
   */
  const dismissConflict = () => {
    showConflictModal.value = false
    gitConflict.value = null
  }

  const startGitStatusPolling = () => {
    if (!hasGitSync.value) return
    if (gitStatusInterval) clearInterval(gitStatusInterval)  // PERF-269: guard double-registration
    loadGitStatus() // Load immediately
    gitStatusInterval = setInterval(() => {
      if (!document.hidden) loadGitStatus()  // PERF-269: skip when tab backgrounded
    }, 60000) // Then every 60 seconds
  }

  const stopGitStatusPolling = () => {
    if (gitStatusInterval) {
      clearInterval(gitStatusInterval)
      gitStatusInterval = null
    }
  }

  // Cleanup on unmount
  onUnmounted(() => {
    stopGitStatusPolling()
  })

  return {
    hasGitSync,
    gitStatus,
    gitLoading,
    gitSyncing,
    gitPulling,
    gitSyncResult,
    gitHasChanges,
    gitChangesCount,
    gitBehind,
    // Conflict state
    gitConflict,
    showConflictModal,
    // Methods
    loadGitStatus,
    refreshGitStatus,
    syncToGithub,
    pullFromGithub,
    resolveConflict,
    dismissConflict,
    startGitStatusPolling,
    stopGitStatusPolling
  }
}
