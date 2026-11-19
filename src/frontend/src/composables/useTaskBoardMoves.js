import { ref } from 'vue'
import { boardMoveLine, projectErrorMessage } from '@/components/portal/projects/projectsUtils'

/**
 * trinity-enterprise#957 — a task's status change from the Tasks tab, with its
 * honest states: in flight, done, or refused with the reason.
 *
 * ONE in-flight map for every such change (a board move, a card's state menu,
 * Confirm in the list or on a card), keyed by task id and holding the status
 * being sent — so one task never has two writes racing, and the board can draw
 * the card where it is going, marked as saving, until the server answers.
 *
 *   error   — a refused or failed change: names the task and what the server
 *             said; stays until dismissed (principle 18).
 *   notice  — the last change that landed, for the status line.
 */
export function useTaskBoardMoves(projectId, store) {
  const inflight = ref({})
  const error = ref('')
  const notice = ref(null)

  async function send(task, status, write) {
    if (!task || Object.prototype.hasOwnProperty.call(inflight.value, task.id)) return false
    inflight.value = { ...inflight.value, [task.id]: status }
    error.value = ''
    try {
      await write()
      notice.value = { taskId: task.id, text: `${boardMoveLine(task.id, task.status, status)} · logged`, evidence: status === 'pending-verification' }
      return true
    } catch (err) {
      notice.value = null
      error.value = `${task.id} was not changed. ${projectErrorMessage(err)}`
      return false
    } finally {
      const next = { ...inflight.value }
      delete next[task.id]
      inflight.value = next
    }
  }

  /** A board move: answered as soon as the server has it. */
  const move = (task, status, note = '') => send(task, status, () => (
    note ? store.moveTask(projectId(), task.id, status, note) : store.moveTask(projectId(), task.id, status)))
  /** A person's confirmation of a done claim (ent#896), from the list. */
  const confirm = (task) => send(task, 'done', () => store.updateTask(projectId(), task.id, { status: 'done' }))
  function refuse(reason) { notice.value = null; error.value = reason }

  return { inflight, error, notice, move, confirm, refuse }
}
