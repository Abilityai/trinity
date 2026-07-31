# Feature: Dark Mode / Theme Switching

## Overview
Client-side theme management system supporting Light, Dark, and System (OS-preference) modes with localStorage persistence. No backend involvement - entirely frontend feature using Tailwind CSS class-based dark mode strategy.

## User Story
As a user, I want to switch between light, dark, and system theme modes so that I can use Trinity comfortably in different lighting conditions and match my OS preferences.

## Entry Points
- **UI (Quick Toggle)**: `src/frontend/src/components/NavBar.vue:81-98` - Theme icon button in navbar
- **UI (Full Selector)**: `src/frontend/src/components/NavBar.vue:135-168` - Theme selector in user dropdown menu

## Frontend Layer

### Theme Store
**File**: `src/frontend/src/stores/theme.js`

```javascript
// Storage key for persistence
const STORAGE_KEY = 'trinity-theme'

// State
theme: ref(getStoredTheme())      // 'light' | 'dark' | 'system'
isDark: ref(false)                 // Computed actual dark state

// Actions
setTheme(newTheme)   // Set theme and persist to localStorage
toggleTheme()        // Cycle: light -> dark -> system -> light
applyTheme()         // Apply 'dark' class to document.documentElement
initTheme()          // Initialize on mount + add OS preference listener
```

**Key Functions**:

```javascript
// :13-19 - Get stored theme or default to 'system'
function getStoredTheme() {
  const stored = localStorage.getItem(STORAGE_KEY)
  if (stored && ['light', 'dark', 'system'].includes(stored)) {
    return stored
  }
  return 'system'
}

// :33-48 - Apply theme class to DOM
function applyTheme() {
  const root = document.documentElement
  if (theme.value === 'system') {
    // Use system preference
    isDark.value = window.matchMedia('(prefers-color-scheme: dark)').matches
  } else {
    isDark.value = theme.value === 'dark'
  }
  if (isDark.value) {
    root.classList.add('dark')
  } else {
    root.classList.remove('dark')
  }
}

// :50-61 - Initialize + listen for OS preference changes
function initTheme() {
  // Apply theme immediately
  applyTheme()
  // Listen for system preference changes
  const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)')
  mediaQuery.addEventListener('change', () => {
    if (theme.value === 'system') {
      applyTheme()
    }
  })
}

// :63-64 - Watch for theme changes
watch(theme, applyTheme)
```

### Tailwind Configuration
**File**: `src/frontend/tailwind.config.js:7`

```javascript
darkMode: 'class'  // Uses class strategy (not media query)
```

This means Tailwind generates `dark:` variants that activate when `.dark` class is on an ancestor element.

### App Initialization
**File**: `src/frontend/src/App.vue:16,20,25`

```javascript
import { useThemeStore } from './stores/theme'  // line 16

const themeStore = useThemeStore()  // line 20

onMounted(async () => {
  // Initialize theme immediately to prevent flash
  themeStore.initTheme()  // line 25
  // ...
})
```

### NavBar Component
**File**: `src/frontend/src/components/NavBar.vue`

**Theme Toggle Button** (:81-98):
- Cycles through themes on click via `themeStore.toggleTheme()`
- Displays contextual icon:
  - Sun icon for light mode
  - Moon icon for dark mode
  - Computer/monitor icon for system mode
- Shows tooltip with current mode and hint to click

**Theme Selector in Dropdown** (:135-168):
- Three buttons: Light, Dark, Auto (System)
- Active state highlighted with blue background
- Calls `themeStore.setTheme(themeName)` on click

```javascript
// :191 - Import theme store
import { useThemeStore } from '../stores/theme'

// :198 - Initialize theme store
const themeStore = useThemeStore()

// :225-232 - Theme handling in component
const themeTitle = computed(() => {
  const titles = {
    light: 'Light mode (click to switch)',
    dark: 'Dark mode (click to switch)',
    system: 'System theme (click to switch)'
  }
  return titles[themeStore.theme]
})

// :234-236 - Cycle theme
const cycleTheme = () => {
  themeStore.toggleTheme()
}

// :238-240 - Set specific theme
const setTheme = (theme) => {
  themeStore.setTheme(theme)
}
```

## CSS Application Pattern

Dark mode classes follow Tailwind's `dark:` variant pattern:

```html
<!-- Background colors -->
<div class="bg-white dark:bg-gray-800">

<!-- Text colors -->
<span class="text-gray-900 dark:text-white">
<p class="text-gray-500 dark:text-gray-400">

<!-- Borders -->
<div class="border-gray-200 dark:border-gray-700">

<!-- Shadows -->
<div class="shadow dark:shadow-gray-900">

<!-- Form inputs -->
<input class="bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 border-gray-300 dark:border-gray-600">

<!-- Buttons -->
<button class="hover:bg-gray-100 dark:hover:bg-gray-700">

<!-- Focus rings (with offset) -->
<button class="focus:ring-offset-2 dark:focus:ring-offset-gray-800">
```

## Components with Dark Mode Support

**Total Coverage**: 61 files with 2,293 `dark:` class occurrences

### Views (21 files, 1099 dark: occurrences)

| Component | File | dark: Count |
|-----------|------|-------------|
| App | `App.vue:2` | `bg-gray-100 dark:bg-gray-900` |
| Dashboard | `Dashboard.vue` | 43 |
| Login | `Login.vue` | 28 |
| Settings | `Settings.vue` | 78 |
| Dashboard List (ex-Agents page, ent#260) | `components/AgentListPanel.vue` | — |
| AgentDetail | `AgentDetail.vue` | 8 |
| Credentials | `Credentials.vue` | 63 |
| Templates | `Templates.vue` | 40 |
| ApiKeys | `ApiKeys.vue` | 57 |
| ProcessWizard | `ProcessWizard.vue` | 59 |
| ProcessList | `ProcessList.vue` | 59 |
| ProcessDashboard | `ProcessDashboard.vue` | 82 |
| ProcessEditor | `ProcessEditor.vue` | 104 |
| ProcessDocs | `ProcessDocs.vue` | 38 |
| ProcessExecutionDetail | `ProcessExecutionDetail.vue` | 71 |
| ExecutionList | `ExecutionList.vue` | 63 |
| ExecutionDetail | `ExecutionDetail.vue` | 91 |
| Approvals | `Approvals.vue` | 56 |
| Alerts | `Alerts.vue` | 28 |
| FileManager | `FileManager.vue` | 34 |
| PublicChat | `PublicChat.vue` | 33 |
| SetupPassword | `SetupPassword.vue` | 23 |

### Components (40 files, 1194 dark: occurrences)

| Component | File | dark: Count |
|-----------|------|-------------|
| NavBar | `NavBar.vue:2` | 31 |
| ConfirmDialog | `ConfirmDialog.vue` | 10 |
| CreateAgentModal | `CreateAgentModal.vue` | 38 |
| UnifiedActivityPanel | `UnifiedActivityPanel.vue` | 29 |
| InfoPanel | `InfoPanel.vue` | 60 |
| MetricsPanel | `MetricsPanel.vue` | 33 |
| FoldersPanel | `FoldersPanel.vue` | 47 |
| AgentNode | `AgentNode.vue` | 36 |
| GitPanel | `GitPanel.vue` | 63 |
| SchedulesPanel | `SchedulesPanel.vue` | 76 |
| TasksPanel | `TasksPanel.vue` | 85 |
| CredentialsPanel | `CredentialsPanel.vue` | 40 |
| ExecutionTimeline | `ExecutionTimeline.vue` | 93 |
| DashboardPanel | `DashboardPanel.vue` | 58 |
| ObservabilityPanel | `ObservabilityPanel.vue` | 42 |
| PublicLinksPanel | `PublicLinksPanel.vue` | 46 |
| AgentHeader | `AgentHeader.vue` | 36 |
| ProcessFlowPreview | `ProcessFlowPreview.vue` | 25 |
| ProcessChatAssistant | `ProcessChatAssistant.vue` | 26 |
| ReplayTimeline | `ReplayTimeline.vue` | 37 |
| OnboardingChecklist | `OnboardingChecklist.vue` | 28 |
| EditorHelpPanel | `EditorHelpPanel.vue` | 22 |
| SharingPanel | `SharingPanel.vue` | 14 |
| PermissionsPanel | `PermissionsPanel.vue` | 17 |
| ResourceModal | `ResourceModal.vue` | 13 |
| FilesPanel | `FilesPanel.vue` | 8 |
| FileTreeNode | `FileTreeNode.vue` | 10 |
| YamlEditor | `YamlEditor.vue` | 10 |
| GitConflictModal | `GitConflictModal.vue` | 24 |
| SystemAgentNode | `SystemAgentNode.vue` | 20 |
| HostTelemetry | `HostTelemetry.vue` | 6 |
| LogsPanel | `LogsPanel.vue` | 4 |
| RuntimeBadge | `RuntimeBadge.vue` | 3 |
| ProcessSubNav | `ProcessSubNav.vue` | 4 |
| AgentSubNav | `AgentSubNav.vue` | 3 |
| process/TemplateSelector | `process/TemplateSelector.vue` | 37 |
| process/RoleMatrix | `process/RoleMatrix.vue` | 33 |
| process/TrendChart | `process/TrendChart.vue` | 12 |
| file-manager/FileTreeNode | `file-manager/FileTreeNode.vue` | 6 |
| file-manager/FilePreview | `file-manager/FilePreview.vue` | 9 |

## Data Flow Diagram

```
User clicks theme button
        |
        v
NavBar.cycleTheme() / NavBar.setTheme(theme)
        |
        v
themeStore.toggleTheme() / themeStore.setTheme(newTheme)
        |
        +----> localStorage.setItem('trinity-theme', theme)
        |
        v
themeStore.applyTheme()
        |
        +----> Check if 'system': Query matchMedia('prefers-color-scheme: dark')
        |
        v
document.documentElement.classList.add/remove('dark')
        |
        v
Tailwind dark: variants activate/deactivate
        |
        v
All components re-render with new color scheme
```

## Persistence

- **Key**: `trinity-theme`
- **Values**: `'light'` | `'dark'` | `'system'`
- **Default**: `'system'` (follows OS preference)
- **Location**: `localStorage`
- **Read**: On page load in `getStoredTheme()`
- **Write**: On theme change in `setTheme()`

## System Theme Detection

Uses `window.matchMedia` API:

```javascript
// Query current OS preference
window.matchMedia('(prefers-color-scheme: dark)').matches  // true if OS is dark

// Listen for OS preference changes
const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)')
mediaQuery.addEventListener('change', () => {
  // Re-apply theme when OS preference changes (only if in 'system' mode)
})
```

## Error Handling

| Scenario | Handling |
|----------|----------|
| Invalid stored theme | Falls back to `'system'` |
| localStorage unavailable | Theme works but doesn't persist |
| matchMedia unavailable | System mode treated as light |

## Security Considerations

- No backend communication - entirely client-side
- No sensitive data involved
- localStorage is domain-scoped

## Performance Considerations

1. **Flash Prevention**: Theme initialized in `App.vue` `onMounted` before any visible content
2. **No Layout Shift**: CSS variables and Tailwind classes swap instantly
3. **Listener Cleanup**: OS preference listener added once, persists for app lifetime (acceptable for SPA)

## Testing

### Manual Test Steps

1. **Light Mode**:
   - Click theme toggle or select "Light" in dropdown
   - Verify white backgrounds, dark text
   - Refresh page - should persist

2. **Dark Mode**:
   - Click theme toggle or select "Dark" in dropdown
   - Verify dark backgrounds, light text
   - Refresh page - should persist

3. **System Mode**:
   - Select "Auto" in dropdown
   - Change OS dark mode setting
   - Verify app follows OS preference

4. **Persistence**:
   - Set theme to dark
   - Close and reopen browser
   - Verify dark theme restored

### localStorage Verification

```javascript
// In browser console:
localStorage.getItem('trinity-theme')  // Should return current theme
```

## Related Flows

- **Upstream**: None (user-initiated)
- **Downstream**: All views and components affected by theme change

## Implementation Status

### Core Infrastructure
- [x] Theme store with persistence (`stores/theme.js`)
- [x] Tailwind class-based dark mode (`tailwind.config.js:7`)
- [x] NavBar toggle button with icons (`:81-98`)
- [x] NavBar dropdown selector (`:135-168`)
- [x] App.vue initialization (`:25`)
- [x] System preference listener with `matchMedia`
- [x] Watch for theme changes (`:64`)

### Views (21 files complete)
- [x] Dashboard, Login, Settings, Agents, AgentDetail
- [x] Credentials, Templates, ApiKeys
- [x] ProcessWizard, ProcessList, ProcessDashboard, ProcessEditor, ProcessDocs
- [x] ProcessExecutionDetail, ExecutionList, ExecutionDetail
- [x] Approvals, Alerts, FileManager, PublicChat, SetupPassword

### Components (40 files complete)
- [x] NavBar, ConfirmDialog, CreateAgentModal
- [x] UnifiedActivityPanel, InfoPanel, MetricsPanel, FoldersPanel
- [x] AgentNode, GitPanel, SchedulesPanel, TasksPanel
- [x] CredentialsPanel, ExecutionTimeline, DashboardPanel
- [x] ObservabilityPanel, PublicLinksPanel, AgentHeader
- [x] ProcessFlowPreview, ProcessChatAssistant, ReplayTimeline
- [x] OnboardingChecklist, EditorHelpPanel, SharingPanel, PermissionsPanel
- [x] ResourceModal, FilesPanel, FileTreeNode, YamlEditor
- [x] GitConflictModal, SystemAgentNode, HostTelemetry, LogsPanel
- [x] RuntimeBadge, ProcessSubNav, AgentSubNav
- [x] process/TemplateSelector, process/RoleMatrix, process/TrendChart
- [x] file-manager/FileTreeNode, file-manager/FilePreview

## Key Files

| File | Purpose | Line References |
|------|---------|-----------------|
| `src/frontend/src/stores/theme.js` | Pinia store for theme state | 73 lines total |
| `src/frontend/tailwind.config.js` | `darkMode: 'class'` config | Line 7 |
| `src/frontend/src/App.vue` | Theme initialization | Lines 16, 20, 25 |
| `src/frontend/src/components/NavBar.vue` | Toggle button + dropdown | Lines 81-98, 135-168, 225-240 |

---

## Revision History

| Date | Changes |
|------|---------|
| 2025-12-14 | Feature implemented |
| 2025-12-19 | Added FoldersPanel, AgentNode, GitPanel, SchedulesPanel, ExecutionsPanel |
| 2025-12-30 | Verified line numbers against codebase |
| 2026-01-23 | Updated line numbers, expanded component coverage table to 61 files (2,293 dark: classes), added process and file-manager subdirectory components |
