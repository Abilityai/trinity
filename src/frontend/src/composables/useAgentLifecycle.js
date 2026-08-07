import { ref, reactive } from 'vue'

/**
 * Composable for agent lifecycle operations (start, stop, delete)
 * Manages action loading state and confirm dialog
 */
export function useAgentLifecycle(agentRef, agentsStore, router, showNotification) {
  const actionLoading = ref(false)

  const confirmDialog = reactive({
    visible: false,
    title: '',
    message: '',
    confirmText: 'Confirm',
    variant: 'danger',
    onConfirm: () => {}
  })

  const startAgent = async () => {
    if (actionLoading.value || !agentRef.value) return
    actionLoading.value = true
    try {
      const result = await agentsStore.startAgent(agentRef.value.name)
      agentRef.value.status = 'running'
      showNotification(result.message, 'success')
    } catch (err) {
      showNotification(err.message || 'Failed to start agent', 'error')
    } finally {
      actionLoading.value = false
    }
  }

  const stopAgent = async () => {
    if (actionLoading.value || !agentRef.value) return
    actionLoading.value = true
    try {
      const result = await agentsStore.stopAgent(agentRef.value.name)
      agentRef.value.status = 'stopped'
      showNotification(result.message, 'success')
    } catch (err) {
      showNotification(err.message || 'Failed to stop agent', 'error')
    } finally {
      actionLoading.value = false
    }
  }

  const deleteAgent = () => {
    if (!agentRef.value) return
    confirmDialog.title = 'Delete Agent'
    confirmDialog.message = 'Are you sure you want to delete this agent?'
    confirmDialog.confirmText = 'Delete'
    confirmDialog.variant = 'danger'
    confirmDialog.onConfirm = async () => {
      try {
        await agentsStore.deleteAgent(agentRef.value.name)
        // ent#260: the Agents page is retired — go to the Dashboard (bare `/`,
        // not `?view=list`: a post-delete nav is "go somewhere sensible", not
        // a list intent, and must not override the user's saved view mode).
        router.push('/')
      } catch (err) {
        showNotification(err.message || 'Failed to delete agent', 'error')
      }
    }
    confirmDialog.visible = true
  }

  return {
    actionLoading,
    confirmDialog,
    startAgent,
    stopAgent,
    deleteAgent
  }
}
