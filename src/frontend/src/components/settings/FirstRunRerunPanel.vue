<!--
  Settings → General → Re-run setup (ent#581 AC: re-openable after completion).
  Opens the first-run overlay exactly as `?onboarding=1` does: every step this
  admin is eligible for, completed ones still showing complete, skipped ones
  where they were left — so a skip is always recoverable.
-->
<template>
  <BaseCard>
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div class="min-w-0">
        <h2 class="text-sm font-[550] text-gray-900 dark:text-gray-100">First-run setup</h2>
        <p class="mt-0.5 text-[12.5px] text-gray-500 dark:text-gray-400">
          Walk through the setup sequence again. Finished steps stay finished; anything you
          skipped is waiting where you left it.
        </p>
      </div>
      <BaseButton
        variant="secondary"
        size="sm"
        data-testid="first-run-rerun"
        @click="router.push({ path: '/', query: { onboarding: '1' } })"
      >
        Re-run setup
      </BaseButton>
    </div>
  </BaseCard>
</template>

<script setup>
import { useRouter } from 'vue-router'
import BaseCard from '../base/BaseCard.vue'
import BaseButton from '../base/BaseButton.vue'

const router = useRouter()
</script>
