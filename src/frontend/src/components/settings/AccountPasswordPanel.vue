<template>
  <!-- trinity-enterprise#709 — the signed-in admin's own account: change the
       password without touching .env or restarting the backend. -->
  <BaseCard>
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div class="min-w-0">
        <h2 class="text-lg font-medium text-gray-900 dark:text-gray-100">Account</h2>
        <p class="mt-1 text-sm text-gray-600 dark:text-gray-300">
          Change the password you sign in with. Other sessions are signed out when you do.
        </p>
      </div>
      <BaseButton variant="secondary" data-testid="open-change-password" @click="open = true">
        Change password
      </BaseButton>
    </div>
    <ChangePasswordDialog v-model="open" />
  </BaseCard>
</template>

<script setup>
import { ref } from 'vue'
import BaseCard from '../base/BaseCard.vue'
import BaseButton from '../base/BaseButton.vue'
import ChangePasswordDialog from './ChangePasswordDialog.vue'

const open = ref(false)
</script>
