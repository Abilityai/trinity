# Feature: AutonomyToggle Component

> **Created**: 2026-02-12 - New reusable component standardizing autonomy toggle across 4 UI locations

## Overview

`AutonomyToggle.vue` is a reusable Vue component that provides a standardized toggle control for agent autonomy mode. It consolidates the autonomy toggle UI into a single component used across multiple locations in the Trinity platform.

## User Story

As a platform user, I want a consistent autonomy toggle experience across all views so that I can quickly enable or disable scheduled tasks for any agent from wherever I am in the UI.

## Component Location

**File**: `src/frontend/src/components/AutonomyToggle.vue` (151 lines)

## Usage Locations

The component is used in 4 locations:

| Location | File | Lines | Context |
|----------|------|-------|---------|
| Dashboard Graph | `src/frontend/src/components/AgentNode.vue` | 78-85 | Agent card in network graph |
| Dashboard Timeline | `src/frontend/src/components/ReplayTimeline.vue` | 155-161 | Agent row in timeline view |
| Agent Detail Header | `src/frontend/src/components/AgentHeader.vue` | 120-125 | Agent detail page header |
| Dashboard List rows | `src/frontend/src/components/AgentListPanel.vue` | — | List rows (ent#260 — the Agents page retired into the Dashboard List mode) |

---

## Component API

### Props

| Prop | Type | Default | Description |
|------|------|---------|-------------|
| `modelValue` | Boolean | **required** | Current autonomy state (v-model) |
| `disabled` | Boolean | `false` | Disable toggle interaction |
| `loading` | Boolean | `false` | Show loading spinner |
| `showLabel` | Boolean | `true` | Show AUTO/Manual label |
| `size` | String | `'sm'` | Size variant: 'sm', 'md', 'lg' (default changed from 'md' to 'sm' in 2026-02-18) |

### Events

| Event | Payload | Description |
|-------|---------|-------------|
| `update:modelValue` | Boolean | Emitted on toggle for v-model binding |
| `toggle` | Boolean | Emitted with new state value |

### Size Variants

| Size | Toggle | Knob | Translate | Label | Spinner |
|------|--------|------|-----------|-------|---------|
| `sm` | `h-5 w-9` | `h-4 w-4` | `translate-x-4` | `text-xs mr-1.5` | `h-3 w-3` |
| `md` | `h-6 w-11` | `h-5 w-5` | `translate-x-5` | `text-xs mr-2` | `h-4 w-4` |
| `lg` | `h-7 w-14` | `h-6 w-6` | `translate-x-7` | `text-sm mr-3` | `h-5 w-5` |

---

## Visual Design

### States

1. **Enabled (AUTO)**:
   - Toggle: Amber background (`bg-amber-500`)
   - Knob: Right position (`translate-x-4/5/7` based on size)
   - Label: "AUTO" in amber (`text-amber-600 dark:text-amber-400`)

2. **Disabled (Manual)**:
   - Toggle: Gray background (`bg-gray-300 dark:bg-gray-600`)
   - Knob: Left position (`translate-x-0.5`)
   - Label: "Manual" in gray (`text-gray-500 dark:text-gray-400`)

3. **Loading**:
   - Toggle: 50% opacity (`opacity-50`)
   - Cursor: Not-allowed (`cursor-not-allowed`)
   - Spinner: Animated SVG overlay on knob

### Accessibility

- `role="switch"` for screen readers
- `aria-checked` bound to current state
- `aria-label` with descriptive text
- `title` tooltip explaining current state and action
- Focus ring styling (`focus:ring-2 focus:ring-offset-2`)

---

## Implementation Details

### Template Structure (Lines 1-52)

```vue
<template>
  <div class="flex items-center" :class="containerClass">
    <!-- Label (optional) -->
    <span v-if="showLabel" :class="[labelSizeClass, stateColor]">
      {{ modelValue ? 'AUTO' : 'Manual' }}
    </span>

    <!-- Toggle button -->
    <button
      @click="toggle"
      :disabled="disabled || loading"
      role="switch"
      :aria-checked="modelValue"
      :class="[sizeClasses, stateClasses, disabledClasses]"
    >
      <span class="sr-only">Toggle autonomy mode</span>

      <!-- Sliding knob -->
      <span :class="[knobSizeClasses, translateClass]" />

      <!-- Loading spinner overlay -->
      <span v-if="loading" class="absolute inset-0 flex items-center justify-center">
        <svg class="animate-spin" :class="spinnerSizeClass">...</svg>
      </span>
    </button>
  </div>
</template>
```

### Script (Lines 55-150)

```javascript
const props = defineProps({
  modelValue: { type: Boolean, required: true },
  disabled: { type: Boolean, default: false },
  loading: { type: Boolean, default: false },
  showLabel: { type: Boolean, default: true },
  size: { type: String, default: 'sm', validator: (v) => ['sm', 'md', 'lg'].includes(v) }
})

const emit = defineEmits(['update:modelValue', 'toggle'])

// Size-based computed classes
const sizeClasses = computed(() => ({
  sm: 'h-5 w-9',
  md: 'h-6 w-11',
  lg: 'h-7 w-14'
}[props.size]))

// Toggle handler
function toggle() {
  if (!props.disabled && !props.loading) {
    const newValue = !props.modelValue
    emit('update:modelValue', newValue)
    emit('toggle', newValue)
  }
}
```

---

## Integration Examples

### AgentNode.vue (Dashboard Graph)

```vue
<AutonomyToggle
  v-if="!isSystemAgent"
  :model-value="autonomyEnabled"
  :loading="autonomyLoading"
  size="sm"
  class="nodrag"
  @toggle="handleAutonomyToggle"
/>
```

**Notes**:
- Uses `nodrag` class to prevent Vue Flow drag interference
- Hidden for system agents
- Loading state managed by local `autonomyLoading` ref

### ReplayTimeline.vue (Dashboard Timeline)

```vue
<AutonomyToggle
  v-if="!row.isSystemAgent"
  :model-value="row.autonomyEnabled"
  :show-label="false"
  size="sm"
  @toggle="toggleAutonomy(row.name)"
/>
```

**Notes**:
- No label shown (`showLabel="false"`) for compact timeline rows
- Emits event to parent Dashboard.vue via `@toggle-autonomy`

### AgentHeader.vue (Agent Detail Page)

```vue
<template v-if="!agent.is_system && agent.can_share">
  <div class="h-4 w-px bg-gray-300 dark:bg-gray-600 mx-1"></div>
  <AutonomyToggle
    :model-value="agent.autonomy_enabled"
    :loading="autonomyLoading"
    size="sm"
    @toggle="$emit('toggle-autonomy')"
  />
</template>
```

**Notes**:
- Small size (`sm`) for consistent sizing across all toggle locations (changed from `md` in 2026-02-18)
- Separated with vertical divider
- Only shown for owners (not shared users or system agent)

### AgentListPanel.vue (Dashboard List mode — ent#260, replaces the retired Agents page)

```vue
<AutonomyToggle
  :model-value="agent.autonomy_enabled"
  :loading="autonomyLoading === agent.name"
  size="sm"
  :class="agent.is_system ? 'invisible' : ''"
  @toggle="handleAutonomyToggle(agent)"
/>
```

**Notes**:
- Controls column beside RunningStateToggle / ReadOnlyToggle
- Loading state tracked per-agent via `autonomyLoading` ref
- `invisible` (column-preserving) for system agents — since #2358 that holds
  at **every** List breakpoint. md and base used `v-if`, so the toggle was
  removed rather than reserved and the success bar, capacity meter and task
  counts beside it sat at a different x on exactly the system and shared rows;
  the reservation is now a wrapper around the toggle, so its width comes from
  the toggle itself and cannot drift from a hand-written one
- Handler calls `networkStore.toggleAutonomy` and toasts off the returned
  `{success, error}` — the network store returns, never throws

---

## Toggle Position Standardization

As of 2026-02-12, the Running and Autonomy toggles are positioned on the **same row** in:

1. **Dashboard Graph** (`AgentNode.vue:57-86`):
   ```
   [ RunningStateToggle ]  [ AutonomyToggle ]
   ```

2. **Agents Page** (`Agents.vue:108-123` — historical; page retired into the Dashboard List mode, ent#260):
   ```
   [ RunningStateToggle ]  [ AutonomyToggle ]
   ```

This provides visual consistency between the Dashboard and Agents page agent cards.

---

## Data Flow

```
User clicks toggle
    |
    v
AutonomyToggle.toggle()
    |
    +-- emit('update:modelValue', newValue)
    +-- emit('toggle', newValue)
    |
    v
Parent component handler (e.g., handleAutonomyToggle)
    |
    v
Store action (network.js/agents.js toggleAutonomy)
    |
    v
PUT /api/agents/{name}/autonomy
    |
    v
Backend updates agent_ownership.autonomy_enabled  (#1945: the ONLY write —
    |                                                  per-schedule `enabled` is untouched)
    v
Response: { autonomy_enabled, total_schedules, enabled_schedules, message }
    |
    v
Update local state (reactive)
```

---

## Backend Integration

The component triggers the same API endpoint regardless of location:

```
PUT /api/agents/{name}/autonomy
Body: { enabled: boolean }
Response: {
  status: "updated",
  agent_name: "my-agent",
  autonomy_enabled: true,
  total_schedules: 3,
  enabled_schedules: 2,
  message: "Autonomy enabled. 2 of 3 schedule(s) will run; per-schedule settings unchanged."
}
```

See [autonomy-mode.md](autonomy-mode.md) for full backend flow documentation.

---

## Error Handling

| Error Case | Handling |
|------------|----------|
| API failure | Loading state cleared, error shown via parent notification |
| Not authorized (403) | Handled by parent, toggle hidden for non-owners |
| Network timeout | Loading state cleared, user can retry |
| System agent | Toggle not rendered (v-if condition) |

---

## Testing

### Prerequisites
- Trinity platform running
- At least one agent with schedules

### Test Cases

#### Test 1: Dashboard Graph Toggle
| Step | Action | Expected |
|------|--------|----------|
| 1 | Open Dashboard `/` | Agent cards visible |
| 2 | Find agent with "Manual" label | Toggle is gray |
| 3 | Click toggle | Shows loading spinner |
| 4 | Wait for response | Label changes to "AUTO", toggle turns amber |
| 5 | Click again | Returns to "Manual" |

#### Test 2: Agents Page Toggle
| Step | Action | Expected |
|------|--------|----------|
| 1 | Open Agents `/agents` | Agent grid visible |
| 2 | Verify Running and Autonomy on same row | Both toggles visible side by side |
| 3 | Toggle autonomy | Updates immediately |
| 4 | Refresh page | State persists |

#### Test 3: Agent Detail Toggle
| Step | Action | Expected |
|------|--------|----------|
| 1 | Open agent detail `/agents/{name}` | Header visible |
| 2 | Find medium-sized toggle in header | Shows AUTO/Manual |
| 3 | Toggle | Notification shows schedule count |

#### Test 4: Timeline Toggle
| Step | Action | Expected |
|------|--------|----------|
| 1 | Open Dashboard in Timeline mode | Timeline visible |
| 2 | Find toggle in agent row (no label) | Small toggle only |
| 3 | Toggle | Emits event to parent |

#### Test 5: System Agent Exclusion
| Step | Action | Expected |
|------|--------|----------|
| 1 | Find trinity-system agent (admin only) | Purple badge visible |
| 2 | Check for autonomy toggle | Toggle NOT visible |

### Status
- Working (2026-02-12)

---

## Related Flows

- **[Autonomy Mode](autonomy-mode.md)** - Full autonomy feature documentation
- **[Agent Network](agent-network.md)** - Dashboard Graph context
- **[Replay Timeline](replay-timeline.md)** - Dashboard Timeline context
- **[Agents Page UI](agents-page-ui-improvements.md)** - Agents list context
- **[Agent Lifecycle](agent-lifecycle.md)** - AgentHeader context

---

## Revision History

| Date | Change |
|------|--------|
| 2026-02-18 17:50 | **Toggle Size Consistency**: All AutonomyToggle instances now use `size="sm"`. AgentHeader.vue changed from `size="md"` to `size="sm"` (line 68). Default size prop remains `'sm'`. This provides visual consistency with RunningStateToggle and ReadOnlyToggle across all locations. |
| 2026-02-12 | Initial documentation - AutonomyToggle component extracted to standardize toggle UI across 4 locations |
