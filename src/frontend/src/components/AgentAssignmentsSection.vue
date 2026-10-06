<template>
  <!-- trinity-enterprise#810: who this agent serves — its primary human and its
       stakeholders (ent#500 assignments). Enterprise-gated like the endpoints
       it reads: on an install without the module the section is absent. -->
  <section v-if="entitled" class="space-y-3" data-testid="agent-assignments">
    <div>
      <h4 class="text-sm font-semibold text-gray-900 dark:text-gray-100">People this agent serves</h4>
      <!-- ent#817: the seats this agent's canon defines, offered on every seat
           field. A typed id is still accepted. -->
      <datalist :id="roleListId">
        <option v-for="r in canonList" :key="r.id" :value="r.id">{{ r.title || r.id }}</option>
      </datalist>
      <p class="mt-0.5 text-xs text-gray-600 dark:text-gray-300" data-testid="assignments-seat">
        <!-- Until the roster is in, the seat is unknown — not "no seat". -->
        <span v-if="view.state === 'loading'" class="inline-block h-3 w-72 max-w-full rounded bg-gray-100 dark:bg-gray-750 align-middle animate-pulse motion-reduce:animate-none" aria-hidden="true"></span>
        <template v-else-if="seat.form === 'holds'">
          Holds the seat: <span :class="seatTitleClass">{{ seatName(seat.roleId) }}</span>
        </template>
        <template v-else-if="seat.form === 'serves'">
          Serves the seat of its primary: <span :class="seatTitleClass">{{ seatName(seat.roleId) }}</span> ({{ seat.person }})
        </template>
        <template v-else>No seat yet</template>
        <span v-if="seat.form !== 'none' && seatUpdated(canon, seat.roleId)" data-testid="assignments-seat-updated">
          · updated {{ seatUpdated(canon, seat.roleId) }}
        </span>
      </p>
    </div>

    <!-- first load: the loaded section's own shape — the admin's seat bar, a
         primary and one stakeholder, the add form — so arrival moves little. -->
    <div v-if="view.state === 'loading'" class="space-y-3" aria-busy="true" data-testid="assignments-skeleton">
      <span class="sr-only">Loading who this agent serves…</span>
      <div v-if="isAdmin" class="h-8 w-44 rounded-md bg-gray-100 dark:bg-gray-750 animate-pulse motion-reduce:animate-none"></div>
      <div class="divide-y divide-gray-200 dark:divide-gray-750 rounded-lg border border-gray-200 dark:border-gray-750">
        <div v-for="n in 2" :key="n" class="px-4 py-3 space-y-2">
          <div class="h-4 w-1/3 rounded bg-gray-100 dark:bg-gray-750 animate-pulse motion-reduce:animate-none"></div>
          <div class="h-3 w-1/2 rounded bg-gray-100 dark:bg-gray-750 animate-pulse motion-reduce:animate-none"></div>
        </div>
      </div>
      <div v-if="isAdmin" class="space-y-1">
        <div class="h-4 w-24 rounded bg-gray-100 dark:bg-gray-750 animate-pulse motion-reduce:animate-none"></div>
        <div class="h-10 rounded-md bg-gray-100 dark:bg-gray-750 animate-pulse motion-reduce:animate-none"></div>
      </div>
    </div>

    <LoadFailed
      v-else-if="view.state === 'failed'"
      title="Couldn't load who this agent serves"
      :message="current.error || 'The request failed. Try again.'"
      @retry="reload"
    />

    <template v-else>
      <InlineError v-if="view.stale" :message="current.error" retryable @retry="reload" />

      <!-- A caller who is not the owner or an admin gets their own row and the
           primary's name only (ent#500); say exactly that. -->
      <div v-if="limited" class="text-sm text-gray-700 dark:text-gray-300 space-y-1" data-testid="assignments-limited">
        <p>Primary: {{ roster.primary || 'nobody yet' }}</p>
        <p v-if="roster.mine">You are {{ article(roster.mine.kind) }} {{ kindLabel(roster.mine.kind).toLowerCase() }} on this agent.</p>
        <p v-else>You have no assignment on this agent.</p>
      </div>

      <template v-else>
        <!-- ent#811 (R50a): the agent itself holding a seat — the autonomous
             player. A companion serves its primary's seat instead. -->
        <div v-if="isAdmin" class="flex flex-wrap items-center gap-2" data-testid="assignments-holder">
          <template v-if="!editingHolder">
            <BaseButton variant="secondary" size="sm" data-testid="holder-edit" @click="startHolder">
              {{ roster.held_seat ? 'Change the seat it holds' : 'This agent holds a seat' }}
            </BaseButton>
            <BaseButton v-if="roster.held_seat" variant="ghost" size="sm" :loading="savingHolder"
                        loading-label="Clearing…" data-testid="holder-clear" @click="clearHolder">Clear the seat</BaseButton>
            <span v-else class="text-xs text-gray-600 dark:text-gray-300">
              For an agent that works a seat itself, such as an orchestrator. A companion serves its primary's seat.
            </span>
          </template>
          <form v-else class="flex flex-wrap items-end gap-2" data-testid="holder-form" @submit.prevent="saveHolder">
            <BaseInput v-model="holderForm" label="Seat this agent holds (role id)" :list="roleListId" data-testid="holder-role" />
            <BaseButton type="submit" size="sm" :disabled="!holderForm.trim()" :loading="savingHolder"
                        loading-label="Saving…" data-testid="holder-save">Save</BaseButton>
            <BaseButton variant="ghost" size="sm" :disabled="savingHolder" @click="editingHolder = false">Cancel</BaseButton>
          </form>
        </div>
        <InlineError v-if="isAdmin && holderError" :message="holderError" data-testid="holder-error" @dismiss="holderError = ''" />

        <p
          v-if="!primary"
          class="rounded-md px-3 py-2 text-sm bg-status-warning-50 dark:bg-status-warning-900/30 text-status-warning-800 dark:text-status-warning-300"
          data-testid="assignments-no-primary"
        >
          This agent has no primary human, so briefs and approvals addressed to its primary have nowhere to go.
        </p>

        <p
          v-if="rows.length === 0"
          class="rounded-md border border-dashed border-gray-300 dark:border-gray-700 px-3 py-4 text-sm text-gray-600 dark:text-gray-300"
          data-testid="assignments-empty"
        >
          Nobody is assigned yet. {{ isAdmin ? 'Add the person this agent coaches first.' : '' }}
        </p>

        <ul v-else class="divide-y divide-gray-200 dark:divide-gray-750 rounded-lg border border-gray-200 dark:border-gray-750 overflow-hidden">
          <li
            v-for="row in rows"
            :key="row.id"
            class="px-4 py-3 bg-white dark:bg-gray-800"
            :data-testid="row.kind === 'primary' ? 'assignment-primary' : 'assignment-row'"
          >
            <div class="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between sm:gap-3">
              <div class="min-w-0 space-y-1">
                <div class="flex flex-wrap items-center gap-2">
                  <span class="text-sm font-medium text-gray-900 dark:text-gray-100 truncate">{{ row.display_name }}</span>
                  <BaseBadge v-if="row.kind === 'primary' || !isAdmin" :variant="row.kind === 'primary' ? 'primary' : 'neutral'">
                    {{ kindLabel(row.kind) }}
                  </BaseBadge>
                  <span v-if="row.role_id" class="text-xs text-gray-600 dark:text-gray-300" :title="row.role_id"
                        data-testid="assignment-seat">{{ seatName(row.role_id) }}</span>
                  <BaseButton
                    v-if="isAdmin && roleEdit.id !== row.id"
                    variant="ghost"
                    size="sm"
                    data-testid="assignment-edit-role"
                    @click="startRoleEdit(row)"
                  >{{ row.role_id ? 'Change seat' : 'Add seat' }}</BaseButton>
                </div>
                <div class="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
                  <span class="text-gray-600 dark:text-gray-300" data-testid="assignment-consent">{{ consentLabel(row) }}</span>
                  <span v-if="driftNote(row)" :class="driftClass(row)" data-testid="assignment-drift">{{ driftNote(row).text }}</span>
                </div>
              </div>

              <div v-if="isAdmin && row.kind !== 'primary'" class="flex items-center gap-2 shrink-0 self-start">
                <BaseSelect
                  :model-value="row.kind"
                  class="min-w-[9.5rem]"
                  :aria-label="`Kind for ${row.display_name}`"
                  :disabled="busyId === row.id"
                  data-testid="assignment-kind"
                  @update:model-value="(k) => changeKind(row, k)"
                >
                  <option v-for="k in STAKEHOLDER_KINDS" :key="k" :value="k">{{ kindLabel(k) }}</option>
                </BaseSelect>
                <BaseButton
                  variant="ghost"
                  size="sm"
                  :loading="busyId === row.id && busyVerb === 'remove'"
                  loading-label="Removing…"
                  data-testid="assignment-remove"
                  @click="removeRow(row)"
                >Remove</BaseButton>
              </div>
              <BaseButton
                v-else-if="isAdmin && row.kind === 'primary' && !replacing"
                class="self-start shrink-0"
                variant="secondary"
                size="sm"
                data-testid="assignment-replace"
                @click="startReplace"
              >Replace primary</BaseButton>
            </div>
            <!-- ent#811: the seat this person holds — optional, and clearable. -->
            <form
              v-if="roleEdit.id === row.id"
              class="mt-2 flex flex-wrap items-end gap-2"
              data-testid="assignment-role-form"
              @submit.prevent="saveRole(row)"
            >
              <BaseInput v-model="roleEdit.value" :label="`Seat ${row.display_name} holds (role id)`" :list="roleListId" data-testid="assignment-role-input" />
              <BaseButton type="submit" size="sm" :disabled="!roleEdit.value.trim()"
                          :loading="busyId === row.id && busyVerb === 'role'" loading-label="Saving…"
                          data-testid="assignment-role-save">Save</BaseButton>
              <BaseButton v-if="row.role_id" variant="ghost" size="sm" :disabled="busyId === row.id"
                          data-testid="assignment-role-clear" @click="clearRole(row)">Clear seat</BaseButton>
              <BaseButton variant="ghost" size="sm" :disabled="busyId === row.id" @click="roleEdit = { id: null, value: '' }">Cancel</BaseButton>
            </form>
            <InlineError
              v-if="rowError.id === row.id && rowError.message"
              class="mt-2"
              :message="rowError.message"
              @dismiss="rowError = { id: null, message: '' }"
            />
          </li>
        </ul>

        <!-- Replace the primary: one flow, the old row leaves before the new one lands. -->
        <form
          v-if="isAdmin && replacing"
          class="rounded-lg border border-gray-200 dark:border-gray-750 bg-gray-50 dark:bg-gray-900 p-4 space-y-3"
          data-testid="assignment-replace-form"
          @submit.prevent="confirmReplace"
        >
          <p class="text-sm font-medium text-gray-900 dark:text-gray-100">Replace the primary</p>
          <div class="grid gap-3 sm:grid-cols-3">
            <BaseSelect v-model="replaceForm.userId" label="New primary" data-testid="replace-user">
              <option value="" disabled>Pick a person</option>
              <option v-for="u in replaceCandidates" :key="u.id" :value="String(u.id)">{{ userLabel(u) }}</option>
            </BaseSelect>
            <BaseSelect v-model="replaceForm.oldBecomes" :label="`${primary.display_name} becomes`" data-testid="replace-old-becomes">
              <option v-for="k in STAKEHOLDER_KINDS" :key="k" :value="k">{{ kindLabel(k) }}</option>
              <option value="remove">Removed from this agent</option>
            </BaseSelect>
            <BaseInput v-model="replaceForm.roleId" label="Seat (role id, optional)" help="The canon role id the new primary holds." :list="roleListId" data-testid="replace-role" />
          </div>
          <InlineError v-if="replaceError" :message="replaceError" @dismiss="replaceError = ''" />
          <div class="flex gap-2">
            <BaseButton type="submit" size="sm" :disabled="!replaceForm.userId"
                        :loading="savingReplace" loading-label="Replacing…" data-testid="replace-confirm">Replace primary</BaseButton>
            <BaseButton variant="ghost" size="sm" :disabled="savingReplace" @click="replacing = false">Cancel</BaseButton>
          </div>
        </form>

        <!-- Add a person: an instance user picked by name, never a typed email. -->
        <form
          v-if="isAdmin"
          class="grid gap-3 sm:grid-cols-[minmax(0,2fr)_minmax(0,1fr)_minmax(0,1.5fr)_auto] sm:items-end"
          data-testid="assignment-add-form"
          @submit.prevent="addPerson"
        >
          <BaseSelect v-model="addForm.userId" label="Person" data-testid="add-user">
            <option value="" disabled>{{ assignments.usersLoaded ? 'Pick a person' : 'Loading people…' }}</option>
            <option v-for="u in assignments.users" :key="u.id" :value="String(u.id)">{{ userLabel(u) }}</option>
          </BaseSelect>
          <BaseSelect v-model="addForm.kind" label="Kind" data-testid="add-kind">
            <option v-for="k in addKinds" :key="k" :value="k">{{ kindLabel(k) }}</option>
          </BaseSelect>
          <BaseInput v-model="addForm.roleId" label="Seat (role id, optional)" :list="roleListId" data-testid="add-role" />
          <BaseButton type="submit" :disabled="!addForm.userId"
                      :loading="savingAdd" loading-label="Adding…" data-testid="add-submit">Add</BaseButton>
        </form>
        <InlineError v-if="isAdmin && (addError || assignments.usersError)" :message="addError || assignments.usersError" @dismiss="addError = ''" />
        <p v-if="isAdmin && canonNote" class="text-xs text-gray-600 dark:text-gray-300" data-testid="assignments-canon-note">
          No seats to suggest: {{ canonNote }}. A typed role id still works.
        </p>

        <p v-if="!isAdmin" class="text-xs text-gray-600 dark:text-gray-300" data-testid="assignments-readonly-note">
          Only an instance admin can change who this agent serves.
        </p>
      </template>
    </template>
  </section>
</template>

<script setup>
import { computed, ref, watch, onMounted } from 'vue'
import BaseBadge from './base/BaseBadge.vue'
import BaseButton from './base/BaseButton.vue'
import BaseInput from './base/BaseInput.vue'
import BaseSelect from './base/BaseSelect.vue'
import InlineError from './InlineError.vue'
import LoadFailed from './LoadFailed.vue'
import { useAssignmentsStore } from '../stores/assignments'
import { useAuthStore } from '../stores/auth'
import { useEnterpriseStore } from '../stores/enterprise'
import { viewState } from '../utils/loadingState'
import {
  KIND_LABELS, STAKEHOLDER_KINDS, primaryOf, stakeholdersOf, seatLine,
  consentLabel, driftNote, writeError, withRole, seatTitle, seatUpdated,
} from '../utils/assignments'

const FEATURE_ID = 'assignments'

const props = defineProps({
  agentName: { type: String, required: true },
})

const assignments = useAssignmentsStore()
const auth = useAuthStore()
const enterprise = useEnterpriseStore()

const entitled = computed(() => enterprise.isEntitled(FEATURE_ID))
// #2198: a role gate reads the SERVER-verified profile, never the stored one.
const isAdmin = computed(() => auth.profileVerified && auth.role === 'admin')

const current = computed(() => assignments.peek(props.agentName))
const roster = computed(() => current.value.roster || {})
const limited = computed(() => current.value.hasLoaded && !Array.isArray(roster.value.assignments))
const list = computed(() => roster.value.assignments || [])
const primary = computed(() => primaryOf(list.value))
const rows = computed(() => [primary.value, ...stakeholdersOf(list.value)].filter(Boolean))
const seat = computed(() => seatLine(roster.value))
const view = computed(() => viewState({
  hasLoaded: current.value.hasLoaded,
  error: current.value.error,
  count: 1,
}))

// ent#817: the canon's seats — titles for the header and rows, and suggestions.
const canon = computed(() => assignments.canonRoles[props.agentName] || null)
const canonList = computed(() => (canon.value?.roles || []).filter((r) => !r.error))
const roleListId = computed(() => `canon-roles-${props.agentName}`)
const canonNote = computed(() => (canon.value && !canonList.value.length ? canon.value.message : ''))
const seatTitleClass = computed(() => 'font-medium text-gray-900 dark:text-gray-100')
function seatName(roleId) {
  const title = seatTitle(canon.value, roleId)
  return title ? `${title} (${roleId})` : roleId
}

const addKinds = computed(() => (primary.value ? STAKEHOLDER_KINDS : ['primary', ...STAKEHOLDER_KINDS]))

function kindLabel(k) { return KIND_LABELS[k] || k }
function article(k) { return /^[aeiou]/i.test(k || '') ? 'an' : 'a' }
function userLabel(u) {
  // A name, never a bare address where a name exists.
  return u.name || u.username || u.email || `User ${u.id}`
}
function driftClass(row) {
  const tone = driftNote(row).tone
  if (tone === 'warning') return 'font-medium text-status-warning-700 dark:text-status-warning-300'
  return 'text-gray-600 dark:text-gray-300'
}

// ---- add -------------------------------------------------------------------
const addForm = ref({ userId: '', kind: 'approver', roleId: '' })
const savingAdd = ref(false)
const addError = ref('')

// With no primary yet, the next person added is most likely the one the agent
// coaches — the empty state says so — so the form starts there.
watch(addKinds, (kinds) => {
  if (kinds[0] === 'primary') addForm.value.kind = 'primary'
  else if (!kinds.includes(addForm.value.kind)) addForm.value.kind = kinds[0]
}, { immediate: true })
// No prefill (ent#814): each person holds their own seat, and a run for them
// serves it — starting from the primary's would stamp everyone with that role.

async function addPerson() {
  addError.value = ''
  savingAdd.value = true
  try {
    await assignments.add(props.agentName, withRole({
      user_id: Number(addForm.value.userId),
      kind: addForm.value.kind,
    }, addForm.value.roleId))
    // The seat goes with the person: a seat left in the field would be given
    // to whoever is added next (ent#814 — each person holds their own).
    addForm.value = { ...addForm.value, userId: '', roleId: '' }
  } catch (err) {
    addError.value = writeError(err, "Couldn't add that person.")
  } finally {
    savingAdd.value = false
  }
}

// ---- row actions -------------------------------------------------------------
const busyId = ref(null)
const busyVerb = ref('')
const rowError = ref({ id: null, message: '' })

async function changeKind(row, kind) {
  if (kind === row.kind) return
  busyId.value = row.id
  busyVerb.value = 'kind'
  rowError.value = { id: null, message: '' }
  try {
    await assignments.setKind(props.agentName, row.id, kind)
  } catch (err) {
    rowError.value = { id: row.id, message: writeError(err, "Couldn't change the kind.") }
  } finally {
    busyId.value = null
    busyVerb.value = ''
  }
}

async function removeRow(row) {
  busyId.value = row.id
  busyVerb.value = 'remove'
  rowError.value = { id: null, message: '' }
  try {
    await assignments.remove(props.agentName, row.id)
  } catch (err) {
    rowError.value = { id: row.id, message: writeError(err, `Couldn't remove ${row.display_name}.`) }
  } finally {
    busyId.value = null
    busyVerb.value = ''
  }
}

// ---- a row's seat (ent#811) ---------------------------------------------------
const roleEdit = ref({ id: null, value: '' })

function startRoleEdit(row) {
  rowError.value = { id: null, message: '' }
  roleEdit.value = { id: row.id, value: row.role_id || seat.value.roleId || '' }
}

async function writeRole(row, roleId) {
  busyId.value = row.id
  busyVerb.value = 'role'
  rowError.value = { id: null, message: '' }
  try {
    await assignments.setRole(props.agentName, row.id, roleId)
    roleEdit.value = { id: null, value: '' }
  } catch (err) {
    rowError.value = { id: row.id, message: writeError(err, "Couldn't change the seat.") }
  } finally {
    busyId.value = null
    busyVerb.value = ''
  }
}

function saveRole(row) { return writeRole(row, roleEdit.value.value.trim()) }
function clearRole(row) { return writeRole(row, null) }

// ---- the seat the agent itself holds (ent#811, R50a) -------------------------
const editingHolder = ref(false)
const holderForm = ref('')
const savingHolder = ref(false)
const holderError = ref('')

function startHolder() {
  holderError.value = ''
  holderForm.value = roster.value.held_seat || ''
  editingHolder.value = true
}

async function saveHolder() {
  holderError.value = ''
  savingHolder.value = true
  try {
    await assignments.setSeatHolder(props.agentName, holderForm.value.trim())
    editingHolder.value = false
  } catch (err) {
    holderError.value = writeError(err, "Couldn't record the seat this agent holds.")
  } finally {
    savingHolder.value = false
  }
}

async function clearHolder() {
  holderError.value = ''
  savingHolder.value = true
  try {
    await assignments.clearSeatHolder(props.agentName)
  } catch (err) {
    holderError.value = writeError(err, "Couldn't clear the seat this agent holds.")
  } finally {
    savingHolder.value = false
  }
}

// ---- replace the primary ---------------------------------------------------
const replacing = ref(false)
const savingReplace = ref(false)
const replaceError = ref('')
const replaceForm = ref({ userId: '', oldBecomes: 'viewer', roleId: '' })
const replaceCandidates = computed(() => assignments.users.filter((u) => u.id !== primary.value?.user_id))

function startReplace() {
  replaceError.value = ''
  addError.value = ''
  replaceForm.value = { userId: '', oldBecomes: 'viewer', roleId: primary.value?.role_id || '' }
  replacing.value = true
}

async function confirmReplace() {
  replaceError.value = ''
  savingReplace.value = true
  try {
    await assignments.replacePrimary(props.agentName, {
      newUserId: Number(replaceForm.value.userId),
      oldBecomes: replaceForm.value.oldBecomes,
      roleId: replaceForm.value.roleId.trim(),
    })
    replacing.value = false
  } catch (err) {
    // The store has rolled back and re-read the roster: say where that left
    // the agent, from the fresh read, not from what we hoped happened.
    const now = primary.value
      ? `${primary.value.display_name} is still the primary.`
      : 'This agent has no primary now — add one below.'
    replaceError.value = `${writeError(err, "Couldn't replace the primary.")} ${now}`
  } finally {
    savingReplace.value = false
  }
}

// ---- loading ---------------------------------------------------------------
function reload() { return assignments.load(props.agentName) }

async function start() {
  await enterprise.loadFeatureFlags()
  if (!entitled.value) return
  // The canon read is a container read; it never blocks the roster.
  assignments.loadCanonRoles(props.agentName)
  await reload()
  if (isAdmin.value && !assignments.usersLoaded) await assignments.loadUsers()
}

onMounted(start)
watch(() => props.agentName, start)
</script>
