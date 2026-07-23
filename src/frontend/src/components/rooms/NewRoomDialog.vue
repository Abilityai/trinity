<template>
  <div class="fixed inset-0 z-40 flex items-center justify-center p-4" @click.self="$emit('close')">
    <div class="absolute inset-0 bg-black/40"></div>
    <div class="relative w-full max-w-lg rounded-2xl bg-white dark:bg-gray-900 shadow-xl border border-gray-200 dark:border-gray-800 max-h-[90vh] flex flex-col">
      <div class="px-5 py-4 border-b border-gray-200 dark:border-gray-800">
        <h2 class="text-base font-semibold">New session</h2>
        <p class="text-xs text-gray-400 mt-0.5">Pick agents to work a topic together. You can @mention any of them once the room opens.</p>
      </div>

      <div class="px-5 py-4 space-y-4 overflow-y-auto">
        <div>
          <label class="block text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">Name</label>
          <input v-model="name" type="text" placeholder="Design review" class="w-full rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-950 text-sm px-3 py-2 focus:ring-2 focus:ring-action-primary-500/40 focus:outline-none" />
        </div>
        <div>
          <label class="block text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">Topic <span class="text-gray-400">(optional)</span></label>
          <input v-model="topic" type="text" placeholder="What should they work on?" class="w-full rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-950 text-sm px-3 py-2 focus:ring-2 focus:ring-action-primary-500/40 focus:outline-none" />
        </div>

        <div>
          <label class="block text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">Agents <span class="text-gray-400">({{ selected.length }} selected)</span></label>
          <div class="max-h-44 overflow-y-auto rounded-lg border border-gray-200 dark:border-gray-800 divide-y divide-gray-100 dark:divide-gray-800">
            <label v-for="a in roster" :key="a.name" class="flex items-center gap-2.5 px-3 py-2 cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800/50">
              <input type="checkbox" :value="a.name" v-model="selected" class="rounded text-action-primary-600 focus:ring-action-primary-500" />
              <PortalAvatar :name="a.name" :avatar-url="a.avatar_url" :size="22" />
              <span class="text-sm truncate">{{ a.name }}</span>
            </label>
            <div v-if="!roster.length" class="px-3 py-4 text-center text-xs text-gray-400">No agents you can access.</div>
          </div>
        </div>

        <div class="grid grid-cols-3 gap-3">
          <div>
            <label class="block text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">Max messages</label>
            <input v-model.number="maxMessages" type="number" min="1" max="500" class="w-full rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-950 text-sm px-2.5 py-2 focus:outline-none" />
          </div>
          <div>
            <label class="block text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">Max cost $</label>
            <input v-model.number="maxCost" type="number" min="0" step="0.5" placeholder="—" class="w-full rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-950 text-sm px-2.5 py-2 focus:outline-none" />
          </div>
          <div>
            <label class="block text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">TTL hours</label>
            <input v-model.number="ttlHours" type="number" min="0" max="168" class="w-full rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-950 text-sm px-2.5 py-2 focus:outline-none" />
          </div>
        </div>

        <div v-if="selected.length > 1">
          <label class="block text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">Scribe <span class="text-gray-400">(optional — records outcomes)</span></label>
          <select v-model="scribe" class="w-full rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-950 text-sm px-3 py-2 focus:outline-none">
            <option :value="null">None</option>
            <option v-for="n in selected" :key="n" :value="n">{{ n }}</option>
          </select>
        </div>

        <p v-if="err" class="text-xs text-status-danger-600 dark:text-status-danger-400">{{ err }}</p>
      </div>

      <div class="px-5 py-4 border-t border-gray-200 dark:border-gray-800 flex justify-end gap-2">
        <button class="px-4 py-2 text-sm rounded-lg text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800" @click="$emit('close')">Cancel</button>
        <button
          class="px-4 py-2 text-sm rounded-lg bg-action-primary-600 hover:bg-action-primary-700 text-white disabled:opacity-40 disabled:cursor-not-allowed"
          :disabled="!canCreate || creating"
          @click="create"
        >{{ creating ? 'Creating…' : 'Create session' }}</button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import PortalAvatar from '../portal/PortalAvatar.vue'

const props = defineProps({
  roster: { type: Array, default: () => [] },
})
const emit = defineEmits(['close', 'created'])

const name = ref('')
const topic = ref('')
const selected = ref([])
const maxMessages = ref(60)
const maxCost = ref(null)
const ttlHours = ref(24)
const scribe = ref(null)
const creating = ref(false)
const err = ref(null)

const canCreate = computed(() => name.value.trim() && selected.value.length > 0)

async function create() {
  if (!canCreate.value) return
  creating.value = true
  err.value = null
  const payload = {
    name: name.value.trim(),
    agents: selected.value,
    topic: topic.value.trim() || undefined,
    max_messages: maxMessages.value || undefined,
    max_cost_usd: maxCost.value || undefined,
    ttl_hours: ttlHours.value ?? undefined,
    scribe: scribe.value || undefined,
  }
  try {
    emit('created', payload, (e) => { err.value = e; creating.value = false })
  } catch {
    creating.value = false
  }
}
</script>
