# Feature: Dark Mode / Theme Switching

## Overview
Client-side theme management system supporting Light, Dark, and System (OS-preference) modes with localStorage persistence. No backend involvement - entirely frontend feature using Tailwind CSS class-based dark mode strategy.

## User Story
As a user, I want to switch between light, dark, and system theme modes so that I can use Trinity comfortably in different lighting conditions and match my OS preferences.

## Entry Points
- **UI (Quick Toggle)**: `src/frontend/src/components/NavBar.vue:64-81` - Theme icon button in navbar
- **UI (Full Selector)**: `src/frontend/src/components/NavBar.vue:117-152` - Theme selector in user dropdown menu

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
// :13-18 - Get stored theme or default to 'system'
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
  applyTheme()
  const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)')
  mediaQuery.addEventListener('change', () => {
    if (theme.value === 'system') {
      applyTheme()
    }
  })
}
```

### Tailwind Configuration
**File**: `src/frontend/tailwind.config.js:7`

```javascript
darkMode: 'class'  // Uses class strategy (not media query)
```

This means Tailwind generates `dark:` variants that activate when `.dark` class is on an ancestor element.

### App Initialization
**File**: `src/frontend/src/App.vue:20`

```javascript
onMounted(async () => {
  // Initialize theme immediately to prevent flash
  themeStore.initTheme()
  // ...
})
```

### NavBar Component
**File**: `src/frontend/src/components/NavBar.vue`

**Theme Toggle Button** (:64-81):
- Cycles through themes on click via `themeStore.toggleTheme()`
- Displays contextual icon:
  - Sun icon for light mode
  - Moon icon for dark mode
  - Computer/monitor icon for system mode
- Shows tooltip with current mode and hint to click

**Theme Selector in Dropdown** (:117-152):
- Three buttons: Light, Dark, Auto (System)
- Active state highlighted with blue background
- Calls `themeStore.setTheme(themeName)` on click

```javascript
// :188-204 - Theme handling in component
const themeTitle = computed(() => {
  const titles = {
    light: 'Light mode (click to switch)',
    dark: 'Dark mode (click to switch)',
    system: 'System theme (click to switch)'
  }
  return titles[themeStore.theme]
})

const cycleTheme = () => {
  themeStore.toggleTheme()
}

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

| Component | File | Key Dark Classes |
|-----------|------|------------------|
| App | `App.vue:2` | `bg-gray-100 dark:bg-gray-900` |
| NavBar | `NavBar.vue:2` | `bg-white dark:bg-gray-800 shadow dark:shadow-gray-900` |
| Dashboard | `Dashboard.vue:2,8,229` | Full dark mode support |
| Login | `Login.vue:2,20,35` | Dark backgrounds, inputs, buttons |
| Settings | `Settings.vue:2,15,23` | Full dark mode support |
| Agents | Multiple views | Full dark mode support |
| AgentDetail | `AgentDetail.vue` | Full dark mode support |
| Credentials | `Credentials.vue` | Full dark mode support |
| Templates | `Templates.vue` | Full dark mode support |
| ApiKeys | `ApiKeys.vue` | Full dark mode support |
| ConfirmDialog | `ConfirmDialog.vue:7,16,19,24,29,41,45,54,58,70` | Backdrop, dialog, buttons |
| CreateAgentModal | `CreateAgentModal.vue:4,8,10,15,20,38,51,54,87,106,121,140,145,154,159,163,174` | Full modal dark support |
| UnifiedActivityPanel | `UnifiedActivityPanel.vue` | Session activity, timeline, modal |
| InfoPanel | `InfoPanel.vue` | Template info sections, colored badges |
| MetricsPanel | `MetricsPanel.vue` | Metrics grid, empty states |
| FoldersPanel | `FoldersPanel.vue` | Shared folders config, toggles, cards |
| AgentNode | `AgentNode.vue` | Dashboard agent tiles, progress bars, buttons |
| GitPanel | `GitPanel.vue` | Git status, commits, changes list |
| SchedulesPanel | `SchedulesPanel.vue` | Schedule cards, form modal, execution history |
| ExecutionsPanel | `ExecutionsPanel.vue` | Stats cards, table, execution detail modal |

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

- [x] Theme store with persistence
- [x] Tailwind class-based dark mode
- [x] NavBar toggle button with icons
- [x] NavBar dropdown selector
- [x] App.vue initialization
- [x] Dashboard dark mode
- [x] Login page dark mode
- [x] Settings page dark mode
- [x] Agents views dark mode
- [x] AgentDetail dark mode
- [x] Credentials page dark mode
- [x] Templates page dark mode
- [x] ApiKeys page dark mode
- [x] ConfirmDialog dark mode
- [x] CreateAgentModal dark mode
- [x] System preference listener
- [x] UnifiedActivityPanel dark mode
- [x] InfoPanel dark mode
- [x] MetricsPanel dark mode

## Files Modified

| File | Changes |
|------|---------|
| `src/frontend/src/stores/theme.js` | **New** - Pinia store for theme state |
| `src/frontend/tailwind.config.js:7` | Added `darkMode: 'class'` |
| `src/frontend/src/App.vue:11,15,20` | Import store, initialize theme |
| `src/frontend/src/components/NavBar.vue` | Theme toggle button + dropdown selector |
| `src/frontend/src/views/Dashboard.vue` | Dark mode Tailwind classes |
| `src/frontend/src/views/Login.vue` | Dark mode Tailwind classes |
| `src/frontend/src/views/Settings.vue` | Dark mode Tailwind classes |
| `src/frontend/src/views/Agents.vue` | Dark mode Tailwind classes |
| `src/frontend/src/views/AgentDetail.vue` | Dark mode Tailwind classes |
| `src/frontend/src/views/Credentials.vue` | Dark mode Tailwind classes |
| `src/frontend/src/views/Templates.vue` | Dark mode Tailwind classes |
| `src/frontend/src/views/ApiKeys.vue` | Dark mode Tailwind classes |
| `src/frontend/src/components/ConfirmDialog.vue` | Dark mode Tailwind classes |
| `src/frontend/src/components/CreateAgentModal.vue` | Dark mode Tailwind classes |
| `src/frontend/src/components/UnifiedActivityPanel.vue` | Dark mode Tailwind classes |
| `src/frontend/src/components/InfoPanel.vue` | Dark mode Tailwind classes |
| `src/frontend/src/components/MetricsPanel.vue` | Dark mode Tailwind classes |
| `src/frontend/src/components/FoldersPanel.vue` | Dark mode Tailwind classes (2025-12-19) |
| `src/frontend/src/components/AgentNode.vue` | Dark mode Tailwind classes (2025-12-19) |
| `src/frontend/src/components/GitPanel.vue` | Dark mode Tailwind classes (2025-12-19) |
| `src/frontend/src/components/SchedulesPanel.vue` | Dark mode Tailwind classes (2025-12-19) |
| `src/frontend/src/components/ExecutionsPanel.vue` | Dark mode Tailwind classes (2025-12-19) |
