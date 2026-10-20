<template>
  <!-- `embedded` (trinity-enterprise#891): Settings › People, whose title is
       the pane's own — the panel keeps everything else exactly as it was. -->
  <div :class="embedded ? 'space-y-6' : 'p-6 space-y-6'">
    <!-- Header -->
    <div>
      <h3 v-if="!embedded" class="text-lg font-medium text-gray-900 dark:text-white">Access</h3>
      <p class="text-sm text-gray-500 dark:text-gray-400" :class="{ 'mt-1': !embedded }">
        <!-- trinity-enterprise#837: an admin who manages members changes the role
             right here, so the pointer to Settings is for everyone else. -->
        <template v-if="canManageMembers">
          People this agent is shared with. They use it in the Workspace, signing in with an
          emailed code. A platform role applies to every agent: <span class="font-medium">user</span>
          works in the Workspace only, <span class="font-medium">operator</span> and above also get
          the operator UI. External clients who reach the agent through channels are managed on the
          <span class="font-medium">Sharing</span> tab.
          A share lets them chat with the agent, see their own conversations, and browse and edit its
          files, except credentials, runtime config and its instructions.
        </template>
        <template v-else>
          People this agent is shared with. They use it in the Workspace, signing in with an
          emailed code; whether someone also gets the operator UI is their platform role, which an
          admin sets in Settings → Access. External clients who reach the agent through channels are
          managed on the <span class="font-medium">Sharing</span> tab.
          A share lets them chat with the agent, see their own conversations, and browse and edit its
          files, except credentials, runtime config and its instructions.
        </template>
      </p>
    </div>

    <!-- trinity-enterprise#810: who this agent serves. Above the operator list:
         an assignment is a relationship on top of an operator's access. It gets
         this list (null until it has loaded) so assigning someone can share the
         agent with them and an unshared assignment can say so (#837 D4). -->
    <AgentAssignmentsSection
      :agent-name="agentName"
      :agent-owner="agentOwner"
      :people="people"
      :reload-people="load"
    />

    <!-- Add operator. At phone width the input takes the whole line and the
         button sits below it (ent#837 eyeball: side by side, the field shrank to
         "name@compan"); `min-w-0` lets it give way in the row layout, where its
         intrinsic width plus the button once pushed the page past 390px. -->
    <form @submit.prevent="addOperator" class="flex flex-col gap-2 sm:flex-row sm:items-center sm:gap-3">
      <input
        v-model="newEmail"
        type="email"
        required
        placeholder="name@company.com"
        :disabled="adding"
        class="w-full sm:w-auto sm:flex-1 min-w-0 px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500"
      />
      <button
        type="submit"
        :disabled="adding || !newEmail.trim()"
        class="inline-flex self-start sm:self-auto shrink-0 whitespace-nowrap items-center px-4 py-2 border border-transparent text-sm font-medium rounded-lg text-white bg-blue-600 hover:bg-blue-700 disabled:opacity-50"
      >
        <svg v-if="adding" class="animate-spin -ml-1 mr-2 h-4 w-4 text-white" fill="none" viewBox="0 0 24 24">
          <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
          <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.4 0 0 5.4 0 12h4z"></path>
        </svg>
        {{ adding ? 'Adding…' : 'Add person' }}
      </button>
    </form>

    <!-- Add result -->
    <div
      v-if="message"
      role="status"
      :class="['p-3 rounded-lg text-sm', message.type === 'success'
        ? 'bg-status-success-50 dark:bg-status-success-900/30 text-status-success-700 dark:text-status-success-300'
        : 'bg-status-danger-50 dark:bg-status-danger-900/30 text-status-danger-700 dark:text-status-danger-300']"
    >{{ message.text }}</div>

    <!-- Roster. "Loading" means no list yet (#1927): a reload after a change
         swaps the rows in place and never re-shows the skeleton. -->
    <InlineError v-if="view.stale" message="Couldn't refresh the access list — showing the last one that loaded." retryable @retry="load" />
    <div v-if="view.state === 'loading'" class="py-6 space-y-2" aria-busy="true">
      <span class="sr-only">Loading who this agent is shared with…</span>
      <div
        v-for="n in 4"
        :key="n"
        class="animate-pulse motion-reduce:animate-none h-11 w-full rounded-md bg-gray-200 dark:bg-gray-700"
      ></div>
    </div>
    <div
      v-else-if="view.state === 'failed'"
      class="text-center py-6 text-sm text-status-danger-600 dark:text-status-danger-400"
    >
      Couldn't load access list.
      <button @click="load" class="ml-1 underline">Retry</button>
    </div>
    <div
      v-else-if="view.state === 'empty'"
      class="text-center py-8 text-gray-500 dark:text-gray-400 bg-gray-50 dark:bg-gray-900/50 rounded-lg border border-dashed border-gray-300 dark:border-gray-700"
    >
      No one yet. Add a person by email above.
    </div>
    <ul v-else class="divide-y divide-gray-200 dark:divide-gray-700 border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
      <li
        v-for="op in operators"
        :key="op.email"
        class="px-4 py-3 flex flex-wrap items-center justify-between gap-x-3 gap-y-2 bg-white dark:bg-gray-800"
      >
        <div class="min-w-0 flex-1 basis-56">
          <div class="flex items-center gap-2">
            <p class="text-sm font-medium text-gray-900 dark:text-gray-100 truncate">{{ op.username || op.email }}</p>
            <!-- status -->
            <span
              :class="['inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium', op.status === 'active'
                ? 'bg-status-success-100 text-status-success-800 dark:bg-status-success-900/40 dark:text-status-success-300'
                : 'bg-state-autonomous-100 text-state-autonomous-800 dark:bg-state-autonomous-900/40 dark:text-state-autonomous-300']"
            >{{ op.status === 'active' ? 'Active' : 'Pending' }}</span>
            <!-- role: read-only unless this viewer changes it on the right -->
            <span
              v-if="op.role && !roleEditable(op)"
              class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-200"
            >{{ op.role }}<template v-if="isSelf(op)"> (you)</template></span>
          </div>
          <p class="text-xs text-gray-500 dark:text-gray-400 truncate">
            {{ op.username ? op.email : 'No platform account — uses the Workspace' }}
            <span v-if="op.last_active"> · last active {{ formatLastActive(op.last_active) }}</span>
            <span v-else-if="op.username"> · never signed in</span>
          </p>
          <InlineError
            v-if="rowError.email === op.email && rowError.message"
            class="mt-2"
            :message="rowError.message"
            @dismiss="rowError = { email: '', message: '' }"
          />
        </div>
        <div class="flex flex-wrap items-center gap-x-4 gap-y-2 max-w-full">
          <!-- trinity-enterprise#837 (AC5): the platform role, for an admin, in
               one fixed-width slot on every row (empty on your own), so the
               controls after it line up. The select is bound to the value being
               confirmed or saved, so a cancel or a refusal puts it back on the
               stored role; "every agent" is in its accessible name and the
               header, not a caption. -->
          <div v-if="canManageMembers" class="w-48 shrink-0" data-testid="access-member-slot">
            <BaseSelect
              v-if="roleEditable(op)"
              size="sm"
              class="w-full"
              :model-value="shownRole(op)"
              :aria-label="`Platform role for ${nameOf(op)} (every agent)`"
              :disabled="!!savingRole"
              data-testid="access-role"
              @update:model-value="(role) => askRole(op, role)"
            >
              <option v-for="r in PLATFORM_ROLES" :key="r" :value="r">{{ platformRoleLabel(r) }}</option>
            </BaseSelect>
            <BaseButton
              v-else-if="op.status === 'pending'"
              variant="secondary"
              size="sm"
              class="w-full"
              :loading="provisioning === op.email"
              loading-label="Creating…"
              data-testid="access-give-account"
              @click="askProvision(op)"
            >Give a platform account</BaseButton>
          </div>
          <!-- #1577: per-recipient proactive-messaging toggle (allow_proactive
               on agent_sharing). The flag rides on the sharing row, which exists
               pre-resolution, so a pending invite can be pre-authorized too. -->
          <label
            class="flex items-center gap-2 cursor-pointer select-none"
            :title="proactiveTitle(op)"
          >
            <span class="text-xs text-gray-500 dark:text-gray-400">Proactive</span>
            <span class="relative inline-flex items-center">
              <input
                type="checkbox"
                class="sr-only peer"
                :checked="op.allow_proactive"
                :disabled="savingProactive === op.email"
                @change="onToggleProactive(op, $event.target.checked)"
              />
              <div class="w-9 h-5 bg-gray-200 dark:bg-gray-700 rounded-full peer peer-checked:bg-action-primary-600 peer-focus:ring-2 peer-focus:ring-action-primary-500 after:content-[''] after:absolute after:top-0.5 after:left-0.5 after:bg-white after:border after:border-gray-300 after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:after:translate-x-4 peer-disabled:opacity-50"></div>
            </span>
          </label>
          <button
            @click="removeOperator(op.email)"
            :disabled="removing === op.email"
            class="text-sm text-status-danger-600 dark:text-status-danger-400 hover:underline disabled:opacity-50"
          >{{ removing === op.email ? 'Removing…' : 'Remove' }}</button>
        </div>
      </li>
    </ul>
    <p
      v-if="view.state === 'ready'"
      class="text-xs text-gray-500 dark:text-gray-400"
    >
      <span class="font-medium">Proactive</span> lets the agent message a user without being
      prompted (e.g. Telegram alerts). The agent owner always receives proactive messages.
    </p>

    <ConfirmDialog
      :visible="!!confirming"
      :title="confirming?.title"
      :message="confirming?.message || ''"
      :confirm-text="confirming?.confirmText"
      :variant="confirming?.variant || 'warning'"
      :confirm-variant="confirming?.confirmVariant || 'danger'"
      @confirm="onConfirm"
      @cancel="onCancel"
    />
  </div>
</template>

<script setup>
import { computed, ref, watch, onMounted } from 'vue'
import { useAgentsStore } from '../stores/agents'
import { useAssignmentsStore } from '../stores/assignments'
import { useAuthStore } from '../stores/auth'
import { useEnterpriseStore } from '../stores/enterprise'
import { useUsersStore } from '../stores/users'
import { viewState } from '../utils/loadingState'
import { writeError } from '../utils/assignments'
import { PLATFORM_ROLES, WORKSPACE_ONLY_ROLE_LABEL, platformRoleLabel } from '../utils/workspaceOnly'
import AgentAssignmentsSection from './AgentAssignmentsSection.vue'
import BaseButton from './base/BaseButton.vue'
import BaseSelect from './base/BaseSelect.vue'
import ConfirmDialog from './ConfirmDialog.vue'
import InlineError from './InlineError.vue'

const props = defineProps({
  agentName: { type: String, required: true },
  // The owner's username: the serves section never shares the agent with them.
  agentOwner: { type: String, default: null },
  embedded: { type: Boolean, default: false },
})

const agentsStore = useAgentsStore()
const assignments = useAssignmentsStore()
const auth = useAuthStore()
const enterprise = useEnterpriseStore()
const users = useUsersStore()

const operators = ref([])
const hasLoaded = ref(false)
const loadError = ref('')
const adding = ref(false)
const removing = ref('')
const savingProactive = ref('')   // #1577: email whose proactive toggle is in flight
const newEmail = ref('')
const message = ref(null)

const view = computed(() => viewState({
  hasLoaded: hasLoaded.value,
  error: loadError.value,
  count: operators.value.length,
}))
// What the serves section warns from: nothing until a list has loaded.
const people = computed(() => (hasLoaded.value ? operators.value : null))

async function load() {
  const agent = props.agentName
  try {
    const data = await agentsStore.getAgentAccess(agent)
    if (agent !== props.agentName) return
    operators.value = data
    hasLoaded.value = true
    loadError.value = ''
  } catch (e) {
    if (agent !== props.agentName) return
    loadError.value = writeError(e, "Couldn't load access list.")
  }
}

// Another agent: its own first load, nothing carried over.
function restart() {
  operators.value = []
  hasLoaded.value = false
  loadError.value = ''
  message.value = null
  rowError.value = { email: '', message: '' }
  confirming.value = null
  pendingRole.value = null
  return load()
}

async function addOperator() {
  const email = newEmail.value.trim()
  if (!email) return
  adding.value = true
  message.value = null
  try {
    await agentsStore.shareAgent(props.agentName, email)
    newEmail.value = ''
    message.value = { type: 'success', text: `Added ${email}.` }
    await load()
  } catch (e) {
    message.value = { type: 'error', text: e?.response?.data?.detail || 'Failed to add this person.' }
  } finally {
    adding.value = false
  }
}

async function removeOperator(email) {
  removing.value = email
  try {
    await agentsStore.unshareAgent(props.agentName, email)
    await load()
  } catch (e) {
    message.value = { type: 'error', text: e?.response?.data?.detail || 'Failed to remove operator.' }
  } finally {
    removing.value = ''
  }
}

// #1577: persist the proactive flag, reflecting the server's confirmed state
// (no optimistic-only flip). On failure, revert the row and surface the error.
async function onToggleProactive(op, next) {
  savingProactive.value = op.email
  message.value = null
  const prev = op.allow_proactive
  try {
    const res = await agentsStore.setProactive(props.agentName, op.email, next)
    op.allow_proactive = !!res.allow_proactive
  } catch (e) {
    op.allow_proactive = prev  // revert so the toggle matches persisted state
    message.value = { type: 'error', text: e?.response?.data?.detail || 'Failed to update proactive setting.' }
  } finally {
    savingProactive.value = ''
  }
}

function proactiveTitle(op) {
  return op.status === 'pending'
    ? 'Allow the agent to message this user proactively. Takes effect once they sign in and connect a channel.'
    : 'Allow the agent to send this user proactive (unprompted) messages.'
}

function formatLastActive(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  if (isNaN(d.getTime())) return ''
  return d.toLocaleString()
}

// ---- members: the platform role, and an account (trinity-enterprise#837) ----
// An admin whose role the SERVER verified (#2198), on an install with the
// user-management module. The routes are the boundary; this only hides
// controls that would be refused.
const canManageMembers = computed(() =>
  enterprise.isEntitled('user_management') && auth.profileVerified && auth.role === 'admin')

const rowError = ref({ email: '', message: '' })
const savingRole = ref('')
const provisioning = ref('')
const pendingRole = ref(null)   // { email, role } — the value shown while it is confirmed or saved
const confirming = ref(null)    // the open ConfirmDialog's request

const RANK = Object.fromEntries(PLATFORM_ROLES.map((r, i) => [r, i]))
const LOWER_TO_USER = "They lose the operator UI on every agent. Agents they own keep only their own runtime routes until they're raised again."

function nameOf(op) { return op.username || op.email }
// The route refuses a change to your own role; `principalId` is the JWT `sub`.
function isSelf(op) { return !!op.username && op.username === auth.principalId }
function roleEditable(op) {
  return canManageMembers.value && op.status === 'active' && !!op.username && !isSelf(op)
}
function shownRole(op) {
  return pendingRole.value?.email === op.email ? pendingRole.value.role : op.role
}

function roleChange(op, to) {
  const name = nameOf(op)
  if (to === 'user') {
    return { title: `Lower ${name} to ${WORKSPACE_ONLY_ROLE_LABEL}?`, message: LOWER_TO_USER,
      confirmText: 'Lower to user', variant: 'warning', confirmVariant: 'danger' }
  }
  const verb = RANK[to] > (RANK[op.role] ?? -1) ? 'Raise' : 'Lower'
  const parts = ['A platform role applies on every agent.']
  if (op.role === 'user') parts.push('They get the operator UI.')
  if (to === 'admin') parts.push("An admin can change anything on this instance, including other people's roles.")
  return { title: `${verb} ${name} to ${to}?`, message: parts.join(' '), confirmText: `${verb} to ${to}`,
    variant: to === 'admin' ? 'warning' : 'info', confirmVariant: to === 'admin' ? 'danger' : 'primary' }
}

// Every change asks first (the approval gate, 2026-10-12): the dialog takes
// focus, which also stops a closed select's arrow keys from firing one change
// per keypress.
function askRole(op, role) {
  if (role === op.role) return
  rowError.value = { email: '', message: '' }
  pendingRole.value = { email: op.email, role }
  confirming.value = { kind: 'role', op, role, ...roleChange(op, role) }
}

function askProvision(op) {
  rowError.value = { email: '', message: '' }
  confirming.value = {
    kind: 'provision', op,
    title: `Give ${op.email} a platform account?`,
    message: "The account is created as user — Workspace only (unless an earlier allow-list entry set another role), so they can be assigned to agents right away; they sign in at /login. A platform account sees the team's view of every agent shared with this address, so give one to colleagues, not to outside clients.",
    confirmText: 'Give an account', variant: 'info', confirmVariant: 'primary',
  }
}

function onConfirm() {
  const c = confirming.value
  confirming.value = null
  if (c?.kind === 'role') return saveRole(c.op, c.role)
  if (c?.kind === 'provision') return provision(c.op)
}

function onCancel() {
  confirming.value = null
  pendingRole.value = null
}

// After a change: the list re-reads the server, and so does the serves
// section's picker, which shows each person's role.
async function refresh() {
  await load()
  if (enterprise.isEntitled('assignments')) await assignments.loadUsers()
}

async function saveRole(op, role) {
  savingRole.value = op.email
  message.value = null
  try {
    await users.setPlatformRole(op.username, role)
    message.value = { type: 'success', text: `${nameOf(op)} is now ${platformRoleLabel(role)} on every agent.` }
    await refresh()
  } catch (e) {
    rowError.value = { email: op.email, message: writeError(e, "Couldn't change the role.") }
  } finally {
    savingRole.value = ''
    pendingRole.value = null
  }
}

async function provision(op) {
  provisioning.value = op.email
  message.value = null
  try {
    const m = await users.provisionMember(op.email, 'user')
    let text = `${m.email} now has a platform account (${platformRoleLabel(m.role)}) and can be picked as primary, approver or viewer.`
    if (m.whitelist === 'kept' && m.role !== 'user') {
      text += " That role comes from an earlier allow-list entry; change it here if it's wrong."
    }
    if (m.email_sent === false) text += " The invite email couldn't be sent, so tell them to sign in at /login."
    message.value = { type: 'success', text }
    await refresh()
  } catch (e) {
    rowError.value = { email: op.email, message: writeError(e, "Couldn't create the account.") }
    await load()   // e.g. they signed in meanwhile: the row is Active now
  } finally {
    provisioning.value = ''
  }
}

onMounted(() => {
  enterprise.loadFeatureFlags()
  load()
})
watch(() => props.agentName, restart)
</script>
