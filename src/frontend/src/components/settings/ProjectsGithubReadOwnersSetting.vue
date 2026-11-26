<template>
  <!--
    trinity-enterprise#1240 — the GitHub owners whose private repos Projects may
    read with the platform token. Empty means public repos only. A plain
    (non-credential) setting, saved through the admin settings route. Its own
    file, so `Settings.vue`'s raw-colour count cannot move; the field is the
    BaseInput primitive.
  -->
  <div class="mt-6" data-testid="projects-github-read-owners-setting">
    <BaseInput
      id="projects-github-read-owners"
      v-model="draft"
      label="GitHub owners Projects may read with the platform token"
      help="Comma-separated GitHub users or organisations. Empty means Projects read public repos only. Removing an owner stops new reads; titles already read stay as last read."
      placeholder="your-org, another-org"
      :error="error"
      :disabled="saving || !loaded"
      maxlength="1000"
      data-testid="projects-github-read-owners"
      @change="save"
    />
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import api from '../../api'
import BaseInput from '../base/BaseInput.vue'

const KEY = 'projects_github_read_owners'

const draft = ref('')
const loaded = ref(false)
const saving = ref(false)
const error = ref('')

/** Lower-cased, de-duplicated, comma-separated owners. */
function normalise(text) {
  const seen = []
  for (const part of String(text || '').split(',')) {
    const owner = part.trim().toLowerCase()
    if (owner && !seen.includes(owner)) seen.push(owner)
  }
  return seen.join(',')
}

async function load() {
  try {
    const { data } = await api.get(`/api/settings/${KEY}`)
    draft.value = data?.value || ''
  } catch (e) {
    // Not set yet answers 404: that is the empty default, not a failure.
    if (e?.response?.status !== 404) error.value = 'Could not load the current value.'
  } finally {
    loaded.value = true
  }
}

async function save() {
  saving.value = true
  error.value = ''
  const value = normalise(draft.value)
  try {
    if (value) await api.put(`/api/settings/${KEY}`, { value })
    else await api.delete(`/api/settings/${KEY}`).catch((e) => { if (e?.response?.status !== 404) throw e })
    draft.value = value
  } catch (e) {
    const detail = e?.response?.data?.detail
    error.value = (detail && typeof detail === 'object' ? detail.message : detail)
      || 'Could not save. Enter GitHub owners separated by commas, for example: your-org, another-org.'
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>
