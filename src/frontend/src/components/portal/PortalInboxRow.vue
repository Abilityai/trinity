<!--
  trinity-enterprise#610 — the sidebar's pinned Inbox row: what needs you and
  what came back, across your agents.

  Its two counts are the SAME numbers the agent rows and the tab title show
  (`inboxCounts` over the threads the sidebar renders — the parent computes
  them), so the row and the rows never disagree (D13). An ask (decide) and an
  arrival (read) stay two badges (ent#364), the ask first because a blocked
  agent outranks unread chatter. White ink on a 700 ground (#2201): white on
  urgent-500 is 2.80:1. `sidebar-ask-count` keeps the address the existing
  specs use; `sidebar-unread-count` is new.
-->
<template>
  <router-link
    v-slot="{ href, navigate, isActive }"
    :to="WORKSPACE_INBOX"
    custom
  >
    <a
      :href="href"
      class="w-full flex items-center gap-2 rounded-xl px-3 py-2 text-sm font-medium transition focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40"
      :class="isActive
        ? 'bg-white dark:bg-gray-900 ring-1 ring-gray-200 dark:ring-gray-800 text-gray-900 dark:text-gray-100'
        : 'text-gray-700 dark:text-gray-200 hover:bg-white dark:hover:bg-gray-900'"
      :aria-current="isActive ? 'page' : undefined"
      data-testid="sidebar-inbox"
      @click="navigate"
    >
      <svg class="w-4 h-4 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20 13V6a2 2 0 00-2-2H6a2 2 0 00-2 2v7m16 0v5a2 2 0 01-2 2H6a2 2 0 01-2-2v-5m16 0h-2.586a1 1 0 00-.707.293l-2.414 2.414a1 1 0 01-.707.293h-3.172a1 1 0 01-.707-.293l-2.414-2.414A1 1 0 006.586 13H4" /></svg>
      <span class="flex-1 min-w-0 truncate">Inbox</span>
      <span
        v-if="needs"
        class="shrink-0 min-w-[1.25rem] px-1.5 h-5 rounded-full bg-status-urgent-700 text-white text-[11px] font-semibold tabular-nums flex items-center justify-center"
        data-testid="sidebar-ask-count"
        :title="askBadgeTitle(needs)"
      >{{ needs > 99 ? '99+' : needs }}</span>
      <span
        v-if="came"
        class="shrink-0 min-w-[1.25rem] px-1.5 h-5 rounded-full bg-action-primary-700 text-white text-[11px] font-semibold tabular-nums flex items-center justify-center"
        data-testid="sidebar-unread-count"
        :title="unreadBadgeTitle(came)"
      >{{ came > 99 ? '99+' : came }}</span>
    </a>
  </router-link>
</template>

<script setup>
import { WORKSPACE_INBOX, askBadgeTitle, unreadBadgeTitle } from './portalUtils'

defineProps({
  needs: { type: Number, default: 0 },
  came: { type: Number, default: 0 },
})
</script>
